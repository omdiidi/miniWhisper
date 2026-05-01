# Research Dossier

## Executive Summary

The smart-format gate today is a literal `< 20` word check (`mercury_client.py:200`) followed by a strict word-multiset equality safety guard (`_is_safe_cleanup`, `mercury_client.py:112-116`) that compares pre/post tokenized multisets after a hand-curated contraction whitelist. That guarantees Parakeet's tokens survive byte-for-byte except for punctuation/casing/whitelisted apostrophes — and that is exactly why filler removal, disfluency cleanup, and bullet/list formatting cannot work today.

The brief at `tmp/briefs/2026-04-30-smart-format-100-word-threshold.md` directs three coupled changes: (1) gate threshold 20 → 100 words, env-driven via a new `smart_format_min_words: int = 100` setting; (2) replace `_is_safe_cleanup`'s strict multiset equality with a soft length-window check (cleaned word count must fall in `[0.7×, 1.05×]` of raw); (3) rewrite `_PROMPT_SYSTEM` to permit filler removal, immediate self-repetition collapse, mid-utterance correction fixes, bullet/numbered list output, em-dashes, and smart quotes — while explicitly forbidding meaning change, summarization, and Markdown literals (`**bold**`, `# headings`).

All required wiring already exists: the Mercury client is constructed in `main.py:170-188` from `Settings`, called in `routes/dictate.py:124`, and toggled by the Swift client at `DictationAPI.swift:42-44`. The change is server-side only. The existing test file `server/tests/test_mercury_safety.py` (205 lines, two test classes) is mostly tied to behavior being deleted: `TestIsSafeCleanup` will be obsolete once the strict multiset is gone; `TestExtractText` is independent of the change and should be preserved. The contraction whitelist + multiset machinery (lines 35-116) becomes dead code and is targeted for deletion.

One non-obvious load-bearing detail: the `reasoning.enabled=False` flag at `mercury_client.py:222` is required for Mercury 2 to populate `content` instead of `reasoning`. Preserve it. `_extract_text` (lines 119-163) handles three OpenAI-compat content shapes plus nested wrappers and must remain intact.

## Critical Codebase Anchors

- Anchor: Smart-format threshold (the literal `< 20`)
  Evidence: `server/src/wispralt_server/smart_format/mercury_client.py:196-201`
  Why it matters: The single site that gates the LLM round-trip. The check uses `sum(_word_multiset(raw_text).values())` — if `_word_multiset` is deleted as planned, this needs replacement with a small `_word_count(s)` helper. The `min_words` constant must come from an injected parameter (not module-level constant) so config can flow through `MercuryClient.__init__`.

- Anchor: `_PROMPT_SYSTEM` (current "punctuation-only" prompt)
  Evidence: `server/src/wispralt_server/smart_format/mercury_client.py:23-33`
  Why it matters: Six explicit STRICT RULES forbidding word add/remove/spelling/word-choice. This is the entire trust contract today; it must be rewritten end-to-end. Rule 5 ("Return ONLY the cleaned text … No quotes, no explanation, no JSON") and Rule 6 (prompt-injection guard against user message hijacking) must be preserved verbatim — they are independent of the new behavior allowance.

- Anchor: Strict multiset safety guard
  Evidence: `server/src/wispralt_server/smart_format/mercury_client.py:112-116` (`_is_safe_cleanup`), called at `mercury_client.py:246-253`
  Why it matters: The check that currently rejects every filler-removal and disfluency-cleanup attempt. Replacement is a length-window check on `_word_count`. The warning-and-return-None contract on failure (`mercury_client.py:246-253`) must be preserved — caller relies on `clean_up()` returning `None` on safety violation.

- Anchor: Multiset/canonicalize machinery + contraction whitelist (dead code after change)
  Evidence: `server/src/wispralt_server/smart_format/mercury_client.py:35-89` (`_WORD_RE`, `_CONTRACTION_EXPANSIONS`), `mercury_client.py:92-109` (`_word_multiset`, `_canonicalize`)
  Why it matters: ~75 LOC of intricately reasoned logic (3 reviewer rounds of hardening — see comments at lines 39-54 and 84-89 explaining why specific contractions are whitelisted/dropped). Becomes dead code once the strict guard is gone. Brief explicitly prefers deletion over flag-gating ("Prefer delete (simpler)" — brief line 31).

- Anchor: Settings class — OpenRouter section
  Evidence: `server/src/wispralt_server/config.py:77-87`
  Why it matters: The home for the new `smart_format_min_words: int = 100` field. Existing pattern is a comment block above each setting explaining the chosen default and failure modes (see the 1500ms timeout comment at lines 82-84 for the comment-style template).

- Anchor: MercuryClient construction site
  Evidence: `server/src/wispralt_server/main.py:167-191`
  Why it matters: Where the new `min_words=settings.smart_format_min_words` kwarg must be threaded. Existing failure mode is fail-soft — wrap any init error and disable smart-format silently (`main.py:184-188`). The new param is a plain int with a safe default, so no new failure surface.

- Anchor: `clean_up` call site (and existing log line)
  Evidence: `server/src/wispralt_server/routes/dictate.py:109-127`
  Why it matters: The only caller of `mercury_client.clean_up`. The brief calls for extending the success log at lines 109-114 with `raw_words=N cleaned_words=M`. The current `applied_smart_format` flag (line 122, 127) and the `cleaned is not None` fallback contract (lines 125-127) are downstream invariants that must continue to hold.

- Anchor: Client-side header send (no change required)
  Evidence: `client/WisprAlt/Server/DictationAPI.swift:42-44`
  Why it matters: Confirms the threshold/safety change is purely server-side. The `X-Smart-Format: true` header is set unconditionally based on `Settings.shared.smartFormatting`; server is free to ignore (return raw) for short utterances and the client treats either path identically.

- Anchor: Existing safety tests (target for replacement)
  Evidence: `server/tests/test_mercury_safety.py:22-123` (`TestIsSafeCleanup`), lines 126-205 (`TestExtractText`)
  Why it matters: `TestIsSafeCleanup` (16 cases) is tied to behavior being deleted — every "negative" case (added word, dropped word, synonym swap, ambiguous-contraction reject) becomes either obsolete or actively wrong under the new policy (filler removal IS dropping words; disfluency fix IS substituting words). Must be removed or rewritten. `TestExtractText` (16 cases, lines 128-205) is orthogonal to the safety change — keep it untouched.

## Existing Patterns to Reuse

- Pattern: Pydantic Settings field with comment-block default justification
  Source: `server/src/wispralt_server/config.py:64-70` (`meeting_idle_eviction_seconds`) and `config.py:66-70` (`dictation_max_duration_s`)
  Reuse for: Adding `smart_format_min_words: int = 100` with a 3-5 line comment block explaining the default (covers ~30s of speech at 200 wpm; `dictation_max_duration_s = 300` caps raw audio at ~750 words at conversational pace, so 100 leaves a healthy band).

- Pattern: `.env.example` documentation block style
  Source: `server/.env.example:19-23` (idle eviction) and `.env.example:25-29` (dictation cap), plus `.env.example:41-48` (existing OpenRouter block)
  Reuse for: Documenting `SMART_FORMAT_MIN_WORDS=100` adjacent to the existing `OPENROUTER_*` block at lines 41-48. Keep the 2-3 line wrapping comment style: what it does, what setting it to 0 does, default rationale.

- Pattern: Constructor injection from Settings to client
  Source: `server/src/wispralt_server/main.py:172-178` — every Mercury param flows from `settings.openrouter_*` through `MercuryClient(...)` kwargs
  Reuse for: Threading `min_words=settings.smart_format_min_words` as a sibling kwarg, same comma-list style.

- Pattern: Single success log line with structured key=val pairs
  Source: `server/src/wispralt_server/routes/dictate.py:109-114` — `logger.info("dictate: queue_wait_ms=%.1f inference_ms=%.1f chars=%d", ...)`
  Reuse for: The brief's telemetry requirement. The existing line is a single-statement `logger.info` with `%.1f`/`%d` format specifiers; extend with `raw_words=%d cleaned_words=%d` (and optionally `dropped=%d`) computed only when smart formatting was applied.

- Pattern: Fail-soft `clean_up` returning `None` on any error
  Source: `server/src/wispralt_server/smart_format/mercury_client.py:189-263` — every error path (timeout, exception, empty content, safety violation) returns `None` and emits a `logger.warning`. Module docstring (`mercury_client.py:1-10`) pins this contract.
  Reuse for: The new length-window check should follow the same pattern — return `None` + `logger.warning` with both word counts when the window is violated. Do not raise.

## Gotchas / Load-Bearing Decisions

- Gotcha: Strict word-multiset safety check is being *intentionally* weakened — this is the entire point of the change
  Evidence: `mercury_client.py:112-116` and brief lines 27-30
  Risk if missed: Reviewers seeing `_is_safe_cleanup` deletion may flag it as a regression. The new soft guardrail is a length window: `0.7 * raw_word_count <= cleaned_word_count <= 1.05 * raw_word_count`. The trust shift moves onto the prompt + length window. This is a documented, deliberate weakening — flag it explicitly in the plan and in commit message body.

- Gotcha: `reasoning.enabled=False` is load-bearing on Mercury 2
  Evidence: `server/src/wispralt_server/smart_format/mercury_client.py:218-222` (and the doubled fallback at lines 237-241 reads both `content` and `reasoning`)
  Risk if missed: Mercury 2 by default routes ALL output through the `reasoning` channel, leaving `content=null`. Removing this flag (or the `_extract_text(msg.get("reasoning"))` fallback at line 239) would silently turn every cleanup into an empty-output → raw-fallback. Preserve both lines verbatim.

- Gotcha: `_extract_text` handles three output shapes — preserve untouched
  Evidence: `server/src/wispralt_server/smart_format/mercury_client.py:119-163`
  Risk if missed: Round-4 review hardening (see comment at line 180 in `test_mercury_safety.py` and the `_depth > 4` guard at line 132) addresses real provider-shape edge cases. The new prompt may yield different content (bullets, em-dashes), but the *transport shape* is unchanged. Do not modify `_extract_text` or any of its 16 tests.

- Gotcha: Threshold check currently calls `_word_multiset` — deleting the multiset means rebuilding the threshold check
  Evidence: `mercury_client.py:200` reads `sum(_word_multiset(raw_text).values())`
  Risk if missed: Naive deletion of `_word_multiset` will break the threshold check. Add a small `_word_count(s: str) -> int` helper (`len(s.split())` or a regex-based tokenizer matching the brief's word-count semantics) and use it in *both* the threshold check and the new length-window safety check. Same helper, two call sites — single source of truth.

- Gotcha: `dictation_max_duration_s = 300` already caps raw audio length, which constrains the upper end of the threshold band
  Evidence: `server/src/wispralt_server/config.py:66-70`, `server/src/wispralt_server/dictate/parakeet.py:39-46` (referenced in the post-compact handoff)
  Risk if missed: 300s × ~150 wpm conversational ≈ 750 words max raw. A threshold of 100 leaves an active range of 100–750 words. Document this band in the comment for `smart_format_min_words` so future tuners understand both ends of the window.

- Gotcha: Markdown literals must be explicitly forbidden in the prompt
  Evidence: Brief lines 33 and 40 — output is pasted into ChatGPT/Claude/Slack/email/code editors where `**bold**` and `# heading` render as raw asterisks/hashes.
  Risk if missed: Without an explicit prohibition, Mercury 2 will *naturally* emit Markdown for "bullet/numbered list formatting" (which the new prompt explicitly enables). Bullets must be `•` or `-`, em-dash `—`, smart quotes `"…"`. The forbidden list (`**bold**`, `# headings`, backticks, `>` blockquotes, `__italic__`) needs to be enumerated in the prompt — not left implicit.

- Gotcha: Length window asymmetric (0.7 lower, 1.05 upper)
  Evidence: Brief lines 28-29 — drop floor is generous (~30% removal allowed for filler-heavy speech), add ceiling is tight (5% — the model should rarely need to add words; the only legitimate adds are restored apostrophes/contractions).
  Risk if missed: A symmetric window (e.g., 0.8–1.2) defeats the "no hallucination" guardrail on the upper side. Asymmetry is intentional. Encode the constants as named locals or module-level finals so the asymmetry is self-documenting.

- Gotcha: `MercuryClient.__init__` must accept the new `min_words` kwarg with a sensible default
  Evidence: `mercury_client.py:166-187`
  Risk if missed: Tests instantiating `MercuryClient(api_key=...)` without the new kwarg should still construct successfully. Default `min_words: int = 100` (matching `Settings` default) keeps test surface unchanged. `__init__` signature pattern follows existing kwarg style at lines 167-174.

- Gotcha: `test_mercury_safety.py::TestIsSafeCleanup` will fail under the new policy and must be deleted/rewritten before the change ships
  Evidence: `server/tests/test_mercury_safety.py:71-123` — every "negative" assertion (`test_reject_added_word`, `test_reject_dropped_word`, `test_reject_synonym_swap`, all 9 `test_reject_*_apostrophe` cases) directly tests the multiset equality being deleted.
  Risk if missed: Test suite breaks. Decision needed in plan: delete `TestIsSafeCleanup` outright (preferred — the new guardrail is structurally simpler and a few new unit tests on `_word_count` plus the length-window logic suffice), or rewrite into a `TestLengthWindowGuard` class with the asymmetric thresholds.

- Gotcha: Documentation discipline — `mercury_client.py` is mapped to TWO docs
  Evidence: `docs/OVERVIEW.md:62` — covers both `ARCHITECTURE.md` and `SETUP-SERVER.md`. `config.py` is mapped to `SETUP-SERVER.md` (line 26). `routes/dictate.py` is mapped to `API.md` (line 41). `.env.example` is mapped to `SETUP-SERVER.md` (line 24).
  Risk if missed: CLAUDE.md "Documentation discipline" rule requires every code change to update mapped docs. Plan must call out edits to `docs/ARCHITECTURE.md`, `docs/SETUP-SERVER.md`, and `docs/API.md` (the last for the new log line / behavior shift in `/transcribe/dictate`).

## External References

None — every load-bearing decision is anchored in repo evidence. The brief already pins the operational facts about Mercury 2 (~1000 tok/s, $0.25/M in + $0.75/M out, `reasoning.enabled=false`) at brief line 12, and the existing implementation at `mercury_client.py:218-241` is itself the authoritative reference for the transport quirks. No external doc lookup materially reduces risk for this change.

## Suggested Implementation Shape

1. **Add `smart_format_min_words: int = 100` to `Settings`** in `config.py:77-87` (place adjacent to the existing OpenRouter block; comment style per `dictation_max_duration_s` at lines 66-70).

2. **Document `SMART_FORMAT_MIN_WORDS=100`** in `server/.env.example` adjacent to the existing OpenRouter block at lines 41-48 (pattern from idle-eviction block at lines 19-23).

3. **In `mercury_client.py`**: 
   - Add a small `_word_count(s: str) -> int` helper (single source of truth for both the gate and the length-window check).
   - Delete `_WORD_RE` (line 37), `_CONTRACTION_EXPANSIONS` (lines 55-89), `_word_multiset` (lines 92-96), `_canonicalize` (lines 99-109), `_is_safe_cleanup` (lines 112-116). Drop `from collections import Counter` at line 15 once `_word_multiset` is gone; consider whether `import re` is still needed.
   - Rewrite `_PROMPT_SYSTEM` (lines 23-33): allow filler removal (`um`, `uh`, contextual `like`, `you know`, `I mean`, `sort of`), immediate self-repetition collapse, mid-utterance correction fixes, bullet/numbered lists with `•` or `-`, em-dashes (`—`), smart quotes (`"…"`); forbid meaning change, summarization, paraphrase, new claims, and Markdown literals (`**bold**`, `# heading`, `>`, backticks, `__italic__`); preserve existing Rule 5 (return ONLY cleaned text, no JSON/quotes) and Rule 6 (prompt-injection ignore).
   - Add `min_words: int = 100` kwarg to `MercuryClient.__init__` (line 166-174); store on `self._min_words`.
   - Replace gate at line 200 with `if _word_count(raw_text) < self._min_words: return None`.
   - Replace `_is_safe_cleanup` call at lines 246-253 with the asymmetric length-window check (`0.7 * raw_words <= cleaned_words <= 1.05 * raw_words`); keep the empty-cleaned reject at lines 243-245; same fail-soft contract (`return None` + `logger.warning`).

4. **In `main.py:172-178`**, add `min_words=settings.smart_format_min_words` to the `MercuryClient(...)` kwarg list.

5. **In `routes/dictate.py:109-114`**, extend the success `logger.info` line to include `raw_words=%d cleaned_words=%d` (computed via the same `_word_count` helper or a route-local one). Compute only when `applied_smart_format` is True; for the non-smart-format path keep the existing format.

6. **Tests** — delete or rewrite `server/tests/test_mercury_safety.py::TestIsSafeCleanup` (lines 22-123). Preserve `TestExtractText` (lines 126-205) untouched. Add a small replacement test class for the new length-window guard (3-4 cases: in-window pass, below-floor reject, above-ceiling reject, empty-cleaned reject) and a `_word_count` smoke test.

7. **Docs** — update `docs/SETUP-SERVER.md` (new env var, new threshold), `docs/ARCHITECTURE.md` (smart-format section: relaxed safety contract, new prompt scope), `docs/API.md` (new log fields, behavior shift on `/transcribe/dictate` smart-format path). Per `docs/OVERVIEW.md` mapping at lines 24, 26, 41, 62.

8. **Sequencing** — settings + .env.example first, then `mercury_client.py` rewrite, then `main.py` wiring, then `routes/dictate.py` log extension, then tests, then docs. Each step independently testable. Local `pytest server/tests/` should pass at every commit boundary except the one that deletes `TestIsSafeCleanup` (which couples test + implementation deletion).

## Open Risks / Unknowns

- **No live measurement of Mercury 2's behavior under the new prompt.** The 0.7×–1.05× window is a reasoned proxy, not an empirically-tuned bound. After deploy, the new `raw_words=N cleaned_words=M` log line is the primary feedback signal — plan should call out a follow-up: pull a week of logs and check what fraction of cleanups land near the 0.7 floor vs. cluster around 0.85–1.0.

- **Brief's ambiguous-contraction examples (`well` ↔ `we'll`, `its` ↔ `it's`) are no longer mathematically blocked.** Today these are explicitly rejected by the contraction whitelist (see `mercury_client.py:42-52` comment block). Under the new prompt, a poor cleanup ("the dog wagged it's tail") would pass the length-window check (same word count). Mitigation lives entirely in prompt clarity — the rewrite should explicitly call out "preserve possessive pronouns and ambiguous contractions as-is" or accept the residual risk. Brief does not address this directly; flag for plan author decision.

- **Bullet output may break downstream paste targets.** ChatGPT and Claude both render `•` as a literal bullet, but some clipboard-target apps (e.g., legacy Slack message composer, plain `<input>` elements) display `•` as a raw glyph rather than a styled list. No blocker, but the brief's "bullets use `•` or `-`" choice should be exercised against the user's actual paste destinations before declaring success. Not blocking the implementation.

- **The handoff in `CLAUDE.local.md` notes** `pytest server/tests/...` fails with `ModuleNotFoundError: wispralt_server` from a fresh shell (need `pip install -e server/` or a `conftest.py` `sys.path` insert). If new tests are added in this change, this surfaces again. Pre-existing issue, not introduced by this change, but worth noting in the verification step.
