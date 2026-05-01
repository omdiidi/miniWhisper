"""test_mercury_safety.py — Regression tests for the smart-format Mercury client's
three pure-function helpers: `_word_count`, `_is_within_length_window`
(soft length-window safety rail), and `_extract_text` (provider-shape parser).

These are pure-function tests — no network, no event loop, no fixtures. They
pin the contracts that:
  - Gate the LLM round-trip (only above the configured min-words threshold).
  - Catch hallucination/summarization at the safety rail (length window).
  - Prevent unrecognized OpenAI-compat response shapes from crashing or
    silently dropping content (`_extract_text`).

Note: this module formerly tested a strict word-multiset equality check
(`_is_safe_cleanup`) which has been replaced by `_is_within_length_window`.
The new rail is intentionally looser to allow filler removal and light list
formatting; the strong system prompt is now the primary guard.
"""

from wispralt_server.smart_format.mercury_client import (
    _extract_text,
    _is_within_length_window,
    _word_count,
)


# ---- _word_count ------------------------------------------------------------


class TestWordCount:
    def test_empty_string(self):
        assert _word_count("") == 0

    def test_whitespace_only(self):
        assert _word_count("   \n\t  ") == 0

    def test_single_word(self):
        assert _word_count("hello") == 1

    def test_paragraph(self):
        text = "Okay so today I am going to be talking about a few things"
        assert _word_count(text) == 13

    def test_punctuation_tokens_count(self):
        # Whitespace-bounded — "Hello," is one token, "world!" is another.
        assert _word_count("Hello, world!") == 2

    def test_bullet_glyphs_count(self):
        # Bullets and dashes count as their own tokens; this is intentional —
        # the length window is a ratio, and the same convention applies on
        # both sides.
        assert _word_count("• item one\n• item two") == 6


# ---- _is_within_length_window -----------------------------------------------


class TestLengthWindow:
    """Soft safety rail: cleaned word count must fall within 0.7×–1.10× of raw.

    Floor 0.7×: allows ~30% shrinkage for filler removal / repeat collapse.
    Ceiling 1.10×: allows minor token expansion — a long-form dictation
    reformatted into bullet items adds one bullet glyph per item — while
    still rejecting added-content hallucination.
    """

    def test_exact_match(self):
        # Same word count = ratio 1.0 → accept.
        assert _is_within_length_window("a b c d e", "a, b, c, d, e.")

    def test_minor_punctuation_growth(self):
        # 5-word raw, 5-word cleaned → ratio 1.0; cleaned has more chars but
        # the safety check is on tokens, not chars.
        raw = "the cat sat on the mat"
        cleaned = "The cat sat on the mat."
        assert _is_within_length_window(raw, cleaned)

    def test_filler_removal_accepted(self):
        # Filler removal that lands inside the 0.7× floor.
        # 17-word raw → 12-word cleaned (ratio ≈ 0.71). Accept.
        raw = (
            "so I uh think we should you know go to the store and uh "
            "buy some milk"
        )
        cleaned = "I think we should go to the store and buy some milk."
        assert _is_within_length_window(raw, cleaned)

    def test_repeat_collapse_accepted(self):
        # 10-word raw, 7-word cleaned → ratio 0.7, exactly at floor. Accept.
        raw = "I I went to to the the store yesterday morning"
        cleaned = "I went to the store yesterday morning."
        assert _is_within_length_window(raw, cleaned)

    def test_summarization_rejected(self):
        # 20-word raw → 6-word cleaned (ratio 0.3). Reject as summarization.
        raw = (
            "the weather today is really nice and I think we should go for "
            "a long walk outside in the park"
        )
        cleaned = "Nice weather; let's walk."
        assert not _is_within_length_window(raw, cleaned)

    def test_hallucination_rejected(self):
        # 10-word raw → 13-word cleaned (ratio 1.3). Reject as hallucination.
        raw = "the meeting starts at three pm in the main room"
        cleaned = (
            "The important quarterly planning meeting starts promptly at "
            "three pm in the main conference room near the lobby."
        )
        assert not _is_within_length_window(raw, cleaned)

    def test_bullet_formatting_accepted(self):
        # 30-word raw enumeration → 32-token cleaned with two bullet glyphs
        # added (ratio ≈ 1.07). Sanity check that bullet-formatted output
        # passes the rail. The ceiling itself is pinned by
        # `test_just_inside_ceiling` / `test_just_outside_ceiling`; trailing
        # punctuation like "milk." does not add a whitespace-bounded token.
        raw = (
            "first thing we should do is buy milk second thing we need to "
            "do is pick up the mail third thing we should remember is to "
            "feed the cat please"
        )
        cleaned = (
            "First thing we should do is buy milk.\n"
            "• Second thing we need to do is pick up the mail.\n"
            "• Third thing we should remember is to feed the cat please."
        )
        assert _is_within_length_window(raw, cleaned)

    def test_empty_raw_rejected(self):
        # Defensive: division by zero protection. Reject.
        assert not _is_within_length_window("", "anything at all here")

    def test_empty_cleaned_rejected(self):
        # Cleaned is empty → ratio 0 → far below floor. Reject.
        assert not _is_within_length_window("hello world how are you", "")

    def test_just_inside_ceiling(self):
        # 100-word raw → 110-word cleaned (ratio 1.10). Exactly at ceiling.
        raw = " ".join(["word"] * 100)
        cleaned = " ".join(["word"] * 110)
        assert _is_within_length_window(raw, cleaned)

    def test_just_outside_ceiling(self):
        # 100-word raw → 111-word cleaned (ratio 1.11). Reject.
        raw = " ".join(["word"] * 100)
        cleaned = " ".join(["word"] * 111)
        assert not _is_within_length_window(raw, cleaned)


# ---- _extract_text ----------------------------------------------------------


class TestExtractText:
    """Helper must extract plain text from every observed OpenAI-compat
    content/reasoning shape, return "" on unrecognized shapes, and never
    blow the stack on deep nesting."""

    def test_plain_string(self):
        assert _extract_text("hello world") == "hello world"

    def test_empty_string(self):
        assert _extract_text("") == ""

    def test_none(self):
        assert _extract_text(None) == ""

    def test_list_of_text_parts(self):
        parts = [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]
        assert _extract_text(parts) == "hello world"

    def test_list_skips_image_parts(self):
        parts = [
            {"type": "image", "url": "https://example.com/x.png"},
            {"type": "text", "text": "only this"},
        ]
        assert _extract_text(parts) == "only this"

    def test_list_with_bare_string_element(self):
        assert _extract_text(["plain"]) == "plain"

    def test_list_empty(self):
        assert _extract_text([]) == ""

    def test_dict_text_field(self):
        assert _extract_text({"text": "hi"}) == "hi"

    def test_dict_content_field(self):
        assert _extract_text({"content": "yo"}) == "yo"

    def test_dict_unknown_keys(self):
        assert _extract_text({"foo": "bar"}) == ""

    def test_nested_dict_content_holds_string(self):
        assert _extract_text({"content": {"text": "deep"}}) == "deep"

    def test_nested_dict_content_holds_parts(self):
        # Provider wraps content as a dict-of-parts.
        shape = {"content": [{"type": "text", "text": "hi"}]}
        assert _extract_text(shape) == "hi"

    def test_list_element_with_nested_content(self):
        # Round 4 fix: list element whose text-bearing key is "content", not "text".
        shape = [{"content": [{"text": "hi"}]}]
        assert _extract_text(shape) == "hi"

    def test_list_element_with_doubly_nested_content(self):
        shape = [{"content": [{"content": [{"text": "deep"}]}]}]
        assert _extract_text(shape) == "deep"

    def test_list_mixed_text_and_content_elements(self):
        shape = [
            {"text": "hello "},
            {"content": [{"text": "world"}]},
        ]
        assert _extract_text(shape) == "hello world"

    def test_depth_limit_prevents_stack_blow(self):
        # Too deep → return "" rather than crash.
        deep = {"text": {"text": {"text": {"text": {"text": {"text": "too deep"}}}}}}
        assert _extract_text(deep) == ""

    def test_unrecognized_top_level(self):
        # Integers, floats, bools — none are valid content shapes.
        assert _extract_text(42) == ""
        assert _extract_text(3.14) == ""
        assert _extract_text(True) == ""
