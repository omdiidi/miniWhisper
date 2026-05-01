# Brief: smart-format threshold 100 words + prettier output

## Why

Smart-formatting today is gated at 20 words and is barely visible to the user — it can only fix punctuation, casing, and paragraph breaks under a strict word-multiset safety check that rejects any added/removed/substituted word. The user's actual workflow is dictating long LLM prompts (multi-paragraph, often a few hundred words) and pasting them into ChatGPT/Claude. For that use case, the current cleanup is too conservative: it leaves "um"s, "uh"s, doubled words, and run-on prose that the user then has to clean up by hand. Raise the bar so the LLM is only invoked on substantial dictations, and let it actually polish the result for that workload.

## Context

- **Threshold lives at one site**: `server/src/wispralt_server/smart_format/mercury_client.py:200` — `if sum(_word_multiset(raw_text).values()) < 20: return None`. No env var; literal magic number. Comment above (line 196) says "< 20 words isn't worth the LLM round-trip."
- **Prompt lives at**: `server/src/wispralt_server/smart_format/mercury_client.py:23-33` (`_PROMPT_SYSTEM`). Current rules: only add punctuation/casing/paragraph breaks; do NOT add, remove, or change words.
- **Safety check**: `_is_safe_cleanup` (line 112) and `_word_multiset` / `_canonicalize` (lines 92-109). Compares multisets after a small contraction-restoration whitelist (`_CONTRACTION_EXPANSIONS`, lines 55-89). Any word delta → fall back to raw. This is what currently blocks filler removal.
- **Mercury 2 model**: `inception/mercury-2` via OpenRouter, ~1000 tok/s, $0.25/M in + $0.75/M out, 1500ms hard timeout. Diffusion LLM. Works with `reasoning.enabled=false`.
- **Wiring**: client opt-in via `X-Smart-Format` header, set in `client/WisprAlt/Server/DictationAPI.swift:42-44` from `Settings.shared.smartFormatting`. Server reads it in `server/src/wispralt_server/routes/dictate.py:116-127`. `/v1/audio/transcriptions` (OpenAI-compat path) never sets it — raw-by-contract there.
- **Current safety guarantee being relaxed**: today the user is mathematically guaranteed Parakeet's word-for-word transcript with only punctuation overlay. After this change, Mercury can drop fillers and substitute obvious slips. Trust shifts onto the prompt and a softer length-window guardrail.

## Decisions

- **Raise threshold from 20 → 100 words** at `mercury_client.py:200`. Below 100 words, return None (raw passthrough). Make it a config-driven constant (`settings.smart_format_min_words`, default 100) so it can be tuned without a redeploy, but no UI surface.
- **Loosen the prompt rules** in `_PROMPT_SYSTEM` to allow:
  - Removing filler tokens: "um", "uh", "like" (filler usage), "you know" (filler), "I mean" (filler), "sort of" (filler).
  - Removing immediate self-repetitions ("the the" → "the", "I I I went" → "I went", "go to the to the store" → "go to the store").
  - Fixing obvious mid-utterance corrections ("I went to the store and got an get a coffee" → "I went to the store and got a coffee").
  - Adding bullet/numbered list formatting when the speaker is enumerating ("first… second… third…" → bullets).
  - Em-dashes and smart quotes where natural.
  - Aggressive paragraph breaks for long-form content.
  - Preserves: meaning, every substantive noun/verb/adjective/proper noun. Cannot rephrase, cannot summarize, cannot add new claims.
- **Replace the strict multiset-equality safety check with a soft guardrail**:
  - Reject if `cleaned_word_count < 0.7 * raw_word_count` (model dropped too much — likely summarized).
  - Reject if `cleaned_word_count > 1.05 * raw_word_count` (model added content — likely hallucinated).
  - Keep the empty-output reject and the safety-fallback-to-raw behavior.
  - The multiset check + contraction whitelist machinery becomes dead code for dictation; either delete or keep behind a flag for forensic comparison. Prefer delete (simpler).
- **No client changes** — threshold is server-side; client just keeps sending the header.
- **Plain-text output, no Markdown literals** — no `**bold**`, no `# headings`. Bullets use `•` or `-`, em-dash is `—`, quotes are smart `"…"`. Output gets pasted into LLMs/email/Slack/code editors where Markdown asterisks/hashes show as raw chars.
- **Telemetry**: log `raw_words=N cleaned_words=M dropped=K` on every cleanup so we can see in production what the model is actually doing. No new metric endpoint needed; existing log line in `dictate.py:109-114` extension is fine.

## Rejected Alternatives

- **Doubling literally (20 → 40)** — user explicitly said 100 minimum; the "double" framing was based on a misremembered current value (50).
- **Keep strict multiset safety check + add filler whitelist** — extending the contraction whitelist pattern to cover all fillers and disfluencies is brittle (every "like" / "I mean" is context-dependent — sometimes filler, sometimes substantive). Cleaner to trust the prompt + length guardrail.
- **Allow Markdown formatting (`**bold**`, `# headings`)** — destination is plain-text contexts (LLM prompts, Slack, email). Markdown literals would render as raw asterisks for most users.
- **Aggressive rewriting / summarization** — explicitly out of scope. User said meaning must not fundamentally change.
- **Embedding-similarity safety check** — heavy, slow, requires another model call. The 0.7×–1.05× word-count window is a cheap proxy that catches the failure modes that matter (drop too much = summarized; add too much = hallucinated).
- **Configurable threshold via UI toggle** — not asked for, adds setting clutter. Env-var (`SMART_FORMAT_MIN_WORDS`) is enough for the rare tune.
- **Per-destination formatting (e.g., bullets only when pasting into doc apps)** — overengineered. One output, plain text with typography.

## Direction

In `server/src/wispralt_server/smart_format/mercury_client.py`: bump the gate to 100 words (config-driven, default 100), rewrite `_PROMPT_SYSTEM` to permit filler removal / disfluency cleanup / list formatting / typography while explicitly forbidding meaning change, and replace `_is_safe_cleanup`'s strict multiset comparison with a soft length-window check (0.7×–1.05× of raw word count). Add `smart_format_min_words: int = 100` to `config.py`. Delete the now-unused contraction whitelist and multiset/canonicalize machinery. Extend the `dictate` route's success log line to include cleaned-vs-raw word counts. No client changes.
