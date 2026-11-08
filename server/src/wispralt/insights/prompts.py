"""System + user prompts for weekly AI insights — schema v2 (source refs, no numbers).

Pure data module. Templates are ``.format()``-ed by ``cron.run_weekly_insights``; do NOT
turn them into f-strings. Every record in the prompt carries a bracketed ref (``[d:12]``,
``[f:<id>]``); the model must cite those refs and invent nothing. Numbers (counts,
percentages, week-over-week deltas, filler counts) are NOT asked for: those are measured
from the database (``insights/measured.py``). The prompt carries no ids beyond the refs.
"""

from __future__ import annotations

PERSON_SYSTEM_PROMPT = """You summarise one person's transcribed dictations and file
transcripts from one week. Each record starts with a bracketed id such as [d:12] or
[f:3f2a...]. Output strict JSON with exactly this shape:

{
  "digest": [{"text": "one sentence on what they worked on", "sources": ["d:12"]}],
  "action_items": [{"text": "...", "owner": "name or 'self'", "sources": ["d:12"]}],
  "decisions": [{"text": "...", "sources": ["f:..."]}],
  "blockers": [{"text": "...", "sources": ["d:13"]}],
  "projects": [{"name": "...", "sources": ["d:12", "d:14"]}],
  "topics": [{"label": "short topic", "sources": ["d:12"]}],
  "quotable_line": {"text": "an exact sentence copied from one record", "source": "d:12"}
}

Rules:
- Every item MUST list in "sources" the bracketed ids of the records it comes from.
  Use only ids that appear in the transcripts. Items without a valid source are discarded.
- digest: up to 3 sentences, present tense.
- action_items: at most 5, only things explicitly committed to.
- decisions: only explicit decisions ("we decided", "going with", "approved").
- blockers: only friction the person named.
- topics: at most 7 short labels.
- quotable_line: copy one sentence VERBATIM from a single record and cite that record,
  or use null. It is discarded unless it appears exactly in the cited record.
- Do not output numbers, percentages, counts, or comparisons with other weeks.
- If the week is thin, return short or empty lists. Never invent content."""


PERSON_USER_PROMPT_TEMPLATE = """Person: {display_name}
Week: ISO {iso_year}-W{iso_week:02d}
Records: {dictation_count} dictations, {file_count} file transcriptions ({total_words} words;
the transcripts below are capped at {input_word_cap} words, newest first)

TRANSCRIPTS:
---
{transcript_blob}
---

Return JSON matching the system schema."""


TEAM_SYSTEM_PROMPT = """You summarise one week of a team's work from per-person JSON
insights. Each person insight's items carry "sources" (record ids such as "d:12").
Output strict JSON with exactly this shape:

{
  "themes": [{"label": "...", "sources": ["d:12", "f:..."]}],
  "team_action_items": [{"text": "...", "owner": "...", "sources": ["d:12"]}],
  "team_blockers": [{"text": "...", "sources": ["d:13"]}],
  "decisions": [{"text": "...", "sources": ["f:..."]}],
  "knowledge_gaps": [{"text": "a question raised with no answer in the records", "sources": ["d:14"]}]
}

Rules:
- Every item MUST cite in "sources" ids that appear in the person insights' sources.
  Items without a valid source are discarded.
- themes: at most 5, most active first. Merge and de-duplicate across people.
- Do not output numbers, counts, rankings by number, or comparisons with other weeks.
- Never invent content."""


TEAM_USER_PROMPT_TEMPLATE = """Week: ISO {iso_year}-W{iso_week:02d}
Per-person insights (one JSON object per line):
---
{digests_jsonl}
---

Return JSON matching the system schema."""
