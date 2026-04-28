"""
test_mercury_safety.py — Regression tests for the smart-format Mercury client's
two safety-critical helpers: `_is_safe_cleanup` (word-multiset guard) and
`_extract_text` (provider-shape parser).

These are pure-function tests — no network, no event loop, no fixtures. They
pin the contracts that prevent Mercury from silently changing dictation
content (`_is_safe_cleanup`) and that prevent unrecognized OpenAI-compat
response shapes from crashing or silently dropping content (`_extract_text`).

Both helpers were progressively tightened across multiple codex-review rounds.
Every accepted/rejected case below maps to a specific finding from those
rounds; do NOT loosen these without re-running the review pipeline.
"""

from wispralt_server.smart_format.mercury_client import (
    _extract_text,
    _is_safe_cleanup,
)


# ---- _is_safe_cleanup -------------------------------------------------------

class TestIsSafeCleanup:
    """Guard must accept punctuation+casing+whitelisted contraction restoration,
    and reject any added/removed/substituted word — including ambiguous pairs
    where the no-apostrophe form is itself a real English word."""

    # Positive: pure punctuation/casing.
    def test_pure_punct_casing(self):
        assert _is_safe_cleanup(
            "hello world how are you",
            "Hello, world! How are you?",
        )

    # Positive: whitelisted contraction restorations.
    def test_im_to_im_apostrophe(self):
        assert _is_safe_cleanup("im going home", "I'm going home.")

    def test_dont_to_dont_apostrophe(self):
        assert _is_safe_cleanup("dont stop", "Don't stop.")

    def test_youre_to_youre_apostrophe(self):
        assert _is_safe_cleanup("youre right", "You're right.")

    def test_thats_to_thats_apostrophe(self):
        assert _is_safe_cleanup("thats fine", "That's fine.")

    def test_curly_apostrophe_normalized(self):
        # U+2019 right single quotation mark must compare equal to ASCII '.
        # Use a whitelisted contraction (im → I'm) so the multisets compare
        # equal regardless of which apostrophe character the cleaned text uses.
        assert _is_safe_cleanup("im going home", "I’m going home.")
        assert _is_safe_cleanup("im going home", "I'm going home.")

    # Positive: full multi-sentence dictation cleanup.
    def test_full_paragraph_cleanup(self):
        raw = (
            "okay so today im going to be talking about a few things "
            "first the weather is really nice second i think we should "
            "grab lunch later third dont forget to bring the documents"
        )
        cleaned = (
            "Okay, so today I'm going to be talking about a few things. "
            "First, the weather is really nice. Second, I think we should "
            "grab lunch later. Third, don't forget to bring the documents."
        )
        assert _is_safe_cleanup(raw, cleaned)

    # Negative: added word.
    def test_reject_added_word(self):
        assert not _is_safe_cleanup("hello world", "Hello, beautiful world.")

    # Negative: dropped word.
    def test_reject_dropped_word(self):
        assert not _is_safe_cleanup("hello big world", "Hello world.")

    # Negative: synonym substitution.
    def test_reject_synonym_swap(self):
        assert not _is_safe_cleanup(
            "the weather is nice", "The climate is nice."
        )

    # Negative: ambiguous contractions where no-apostrophe form is a real word.
    # ALL must be rejected — Mercury must not silently change meaning.
    def test_reject_well_to_well_apostrophe(self):
        assert not _is_safe_cleanup("well done", "we'll done.")

    def test_reject_were_to_were_apostrophe(self):
        assert not _is_safe_cleanup("we were going", "we we're going.")

    def test_reject_shell_to_shell_apostrophe(self):
        assert not _is_safe_cleanup(
            "a shell on the beach", "a she'll on the beach."
        )

    def test_reject_ill_to_ill_apostrophe(self):
        assert not _is_safe_cleanup(
            "i feel ill today", "I feel i'll today."
        )

    def test_reject_its_to_its_apostrophe(self):
        assert not _is_safe_cleanup(
            "the dog wagged its tail", "The dog wagged it's tail."
        )

    def test_reject_cant_to_cant_apostrophe(self):
        # "cant" = jargon/tilt; not in whitelist.
        assert not _is_safe_cleanup(
            "the cant of the roof", "The can't of the roof."
        )

    def test_reject_wont_to_wont_apostrophe(self):
        # "wont" = accustomed to; not in whitelist.
        assert not _is_safe_cleanup(
            "his wont was to wake at dawn", "His won't was to wake at dawn."
        )

    def test_reject_whats_to_whats_apostrophe(self):
        assert not _is_safe_cleanup("whats up", "What's up?")

    def test_reject_theres_to_theres_apostrophe(self):
        assert not _is_safe_cleanup("theres no time", "There's no time.")


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
