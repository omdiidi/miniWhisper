"""
insights/sources.py — source refs for AI insights (schema v2).

Every AI claim must point at the records it came from:

1. ``assemble`` puts each record in the prompt behind its ref (``[d:12]`` = dictation 12,
   ``[f:<job id>]`` = file job) and returns the set of refs actually placed there.
2. The LLM JSON is parsed with pydantic (``extra='ignore'``); a shape error is an
   ``InsightShapeError`` (the run records it as a person failure).
3. ``validate_person`` / ``validate_team`` keep only refs from the allowed set and drop
   any item left without one (a team reply left with no items at all is unusable); ``quotable_line`` must be an exact substring of the record
   it cites. Team insights may cite only refs present in the person insights they summarise.
4. At render time ``build_view`` hides refs to records that were deleted (or that the
   viewer may not open), hides items left without refs, links each ref to its record,
   flags a stale insight (records created after ``generated_at``) and marks topics seen
   in earlier weeks. Rows without ``schema_version: 2`` render a "regenerate" notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from wispralt.jobs.store import JobStore

SCHEMA_VERSION = 2
_KIND_BY_PREFIX = {"d": "dictation", "f": "file"}
_PREFIX_BY_KIND = {v: k for k, v in _KIND_BY_PREFIX.items()}

PERSON_LISTS = ("digest", "action_items", "decisions", "blockers", "projects", "topics")
TEAM_LISTS = ("themes", "team_action_items", "team_blockers", "decisions", "knowledge_gaps")


class InsightShapeError(ValueError):
    """The LLM JSON does not match the v2 schema (wrong types, not an object)."""


def ref_for(kind: str, row_id: str | int) -> str:
    return f"{_PREFIX_BY_KIND[kind]}:{row_id}"


def parse_ref(ref: str) -> tuple[str, str] | None:
    prefix, sep, row_id = ref.partition(":")
    kind = _KIND_BY_PREFIX.get(prefix)
    return (kind, row_id) if sep and kind and row_id else None


# ── prompt assembly ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Assembled:
    blob: str
    allowed: frozenset[str]  # refs actually placed in the prompt
    texts: dict[str, str]  # ref → the record's full text (quote checks)
    total_words: int  # before the cap


def assemble(records: Iterable[dict[str, Any]], *, cap: int) -> Assembled:
    """``records`` newest first (``kind``, ``row_id``, ``text``). Each goes in as
    ``[ref] text``; the blob stops at *cap* words (the last record may be cut short)."""
    chunks: list[str] = []
    allowed: set[str] = set()
    texts: dict[str, str] = {}
    total = used = 0
    for r in records:
        text = r["text"] or ""
        words = text.split()
        total += len(words)
        if used >= cap:
            continue
        ref = ref_for(r["kind"], r["row_id"])
        take = words[: cap - used]
        chunks.append(f"[{ref}] {' '.join(take)}")
        used += len(take)
        allowed.add(ref)
        texts[ref] = text
    return Assembled("\n---\n".join(chunks), frozenset(allowed), texts, total)


# ── LLM JSON shapes (lenient: unknown keys ignored, missing lists default empty) ──


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Item(_M):
    text: str
    sources: list[str] = []


class OwnedItem(Item):
    owner: str | None = None


class Project(_M):
    name: str
    sources: list[str] = []


class Topic(_M):
    label: str
    sources: list[str] = []


class Quote(_M):
    text: str
    source: str


class PersonInsightV2(_M):
    digest: list[Item] = []
    action_items: list[OwnedItem] = []
    decisions: list[Item] = []
    blockers: list[Item] = []
    projects: list[Project] = []
    topics: list[Topic] = []
    quotable_line: Quote | None = None


class TeamInsightV2(_M):
    themes: list[Topic] = []
    team_action_items: list[OwnedItem] = []
    team_blockers: list[Item] = []
    decisions: list[Item] = []
    knowledge_gaps: list[Item] = []


def _parse(model: type[_M], raw: Any) -> _M:
    if not isinstance(raw, dict):
        raise InsightShapeError("insight is not a JSON object")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise InsightShapeError(f"{exc.error_count()} schema error(s)") from exc


def _keep(items: list[Any], allowed: Iterable[str]) -> list[dict[str, Any]]:
    allowed = set(allowed)
    out = []
    for item in items:
        refs = list(dict.fromkeys(r for r in item.sources if r in allowed))
        if refs:
            out.append({**item.model_dump(), "sources": refs})
    return out


def _require_usable(out: dict[str, Any], lists: Iterable[str], what: str) -> None:
    """A cleaned insight with no items in any list (and no quote) is unusable: ``{}``
    parses, since every field defaults to empty, and so does a reply whose every item
    cited refs it may not. ``InsightShapeError``, so the run records a failure and
    retries instead of saving an empty insight as the week's."""
    if not any(out[name] for name in lists) and not out.get("quotable_line"):
        raise InsightShapeError(f"{what} insight has no usable items")


def validate_person(raw: Any, assembled: Assembled) -> dict[str, Any]:
    """Parse and clean a person insight: refs ∩ prompt refs, empty items dropped, the
    quote kept only when it is an exact substring of the record it cites. Nothing usable
    left (no item, no quote) → ``InsightShapeError`` (``_require_usable``)."""
    p = _parse(PersonInsightV2, raw)
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for name in PERSON_LISTS:
        out[name] = _keep(getattr(p, name), assembled.allowed)
    q = p.quotable_line
    quote_ok = (
        q is not None
        and q.text.strip()
        and q.source in assembled.allowed
        and q.text.strip() in assembled.texts.get(q.source, "")
    )
    out["quotable_line"] = {"text": q.text.strip(), "source": q.source} if quote_ok else None
    _require_usable(out, PERSON_LISTS, "person")
    return out


def refs_in(insight: dict[str, Any]) -> set[str]:
    """Every ref an (already validated) insight cites."""
    refs: set[str] = set()
    for value in insight.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    refs.update(item.get("sources") or [])
    quote = insight.get("quotable_line")
    if isinstance(quote, dict) and quote.get("source"):
        refs.add(quote["source"])
    return refs


def validate_team(raw: Any, allowed: Iterable[str]) -> dict[str, Any]:
    """Team items may cite only refs found in the person insights they summarise.

    A reply left with no items is unusable (``_require_usable``): the run retries it and
    never saves an empty team insight as the week's.
    """
    t = _parse(TeamInsightV2, raw)
    allowed = set(allowed)
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    for name in TEAM_LISTS:
        out[name] = _keep(getattr(t, name), allowed)
    _require_usable(out, TEAM_LISTS, "team")
    return out


# ── render-time view ─────────────────────────────────────────────────────────


def build_view(
    job_store: JobStore,
    row: dict[str, Any] | None,
    *,
    viewer_id: int,
    is_admin: bool,
    scope_user_id: int | None,
    bounds: tuple[float, float],
    iso_week: tuple[int, int],
    link_base: str,
) -> dict[str, Any] | None:
    """The template model for one stored insight row (or None when there is none).

    ``link_base``: ``/me/history`` (own records) or ``/admin/records`` (admin).
    """
    if row is None:
        return None
    insight = row["insight"]
    meta = {k: row[k] for k in ("model", "generated_at", "cost_usd")}
    if not isinstance(insight, dict) or insight.get("schema_version") != SCHEMA_VERSION:
        return {**meta, "legacy": True}
    existing = job_store.refs_exist(refs_in(insight), viewer_id=viewer_id, is_admin=is_admin)

    def link(ref: str) -> dict[str, str]:
        kind, row_id = parse_ref(ref) or ("", "")
        label = f"dictation {row_id}" if kind == "dictation" else f"file {row_id[:8]}"
        return {"ref": ref, "label": label, "href": f"{link_base}/{kind}/{row_id}"}

    lists = TEAM_LISTS if scope_user_id is None and "themes" in insight else PERSON_LISTS
    view: dict[str, Any] = {**meta, "legacy": False}
    for name in lists:
        items = []
        for item in insight.get(name) or []:
            refs = [r for r in item.get("sources") or [] if r in existing]
            if refs:
                items.append({**item, "links": [link(r) for r in refs]})
        view[name] = items
    quote = insight.get("quotable_line")
    view["quotable_line"] = (
        {**quote, "link": link(quote["source"])}
        if isinstance(quote, dict) and quote.get("source") in existing
        else None
    )
    seen = job_store.recent_topic_labels(scope_user_id, *iso_week, weeks=4)
    for topic in view.get("topics", []) + view.get("themes", []):
        prior = seen.get(topic["label"].casefold(), 0)
        topic["seen_weeks"] = prior + 1 if prior else None
    view["stale_count"] = job_store.count_records_after(
        scope_user_id, bounds[0], bounds[1], row["generated_at"]
    )
    return view
