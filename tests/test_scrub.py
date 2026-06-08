"""Tests for gui._scrub and transcription._scrub_name."""
from __future__ import annotations

import pytest

from mynah.gui import _scrub
from mynah.transcription import _scrub_name


class TestGuiScrubAsciiControls:
    def test_null_byte_replaced(self) -> None:
        assert "\x00" not in _scrub("a\x00b")

    def test_esc_replaced(self) -> None:
        # ESC (0x1b) — used to inject ANSI escape sequences. Must be in
        # the stripped range.
        assert "\x1b" not in _scrub("\x1b[31mRED")

    def test_del_replaced(self) -> None:
        assert "\x7f" not in _scrub("a\x7fb")

    def test_tab_preserved(self) -> None:
        # Tab is intentionally NOT scrubbed.
        assert "\t" in _scrub("a\tb")


class TestGuiScrubCrLf:
    def test_lf_replaced_with_space(self) -> None:
        assert "\n" not in _scrub("foo\nbar")
        assert _scrub("foo\nbar") == "foo bar"

    def test_cr_replaced_with_space(self) -> None:
        assert "\r" not in _scrub("foo\rbar")

    def test_crlf_neutralized(self) -> None:
        # CR (0x0d) is in the regex character class and becomes `?`;
        # LF (0x0a) is outside the class but is replaced by space via
        # the .replace() pass. The end result has no newlines either way.
        result = _scrub("a\r\nb")
        assert "\r" not in result
        assert "\n" not in result


class TestGuiScrubUnicodeBidi:
    @pytest.mark.parametrize("ch", [
        "\u200e",  # LRM
        "\u200f",  # RLM
        "\u202a",  # LRE
        "\u202e",  # RLO
        "\u2066",  # LRI
        "\u2069",  # PDI
    ])
    def test_bidi_overrides_replaced(self, ch: str) -> None:
        assert ch not in _scrub(f"a{ch}b")


class TestGuiScrubUnicodeLine:
    # — these were missing in the previous regex.
    @pytest.mark.parametrize("ch", [
        "\u2028",  # LINE SEPARATOR — Tk renders as newline
        "\u2029",  # PARAGRAPH SEPARATOR
    ])
    def test_line_separators_replaced(self, ch: str) -> None:
        assert ch not in _scrub(f"a{ch}b")


class TestGuiScrubZeroWidth:
    # — zero-width chars enable homoglyph attacks.
    @pytest.mark.parametrize("ch", [
        "\u200b",  # ZWSP
        "\u200c",  # ZWNJ
        "\u200d",  # ZWJ
        "\u2060",  # WORD JOINER
        "\ufeff",  # ZWNBSP / BOM
    ])
    def test_zero_widths_replaced(self, ch: str) -> None:
        assert ch not in _scrub(f"Alic{ch}e")


class TestGuiScrubC1Controls:
    # — U+0080..U+009F C1 control range (incl. NEL).
    @pytest.mark.parametrize("ch", ["\u0080", "\u0085", "\u009f"])
    def test_c1_controls_replaced(self, ch: str) -> None:
        assert ch not in _scrub(f"a{ch}b")


class TestGuiScrubNormalText:
    def test_ascii_unchanged(self) -> None:
        assert _scrub("Hello World 123") == "Hello World 123"

    def test_legitimate_unicode_unchanged(self) -> None:
        # Accented letters / CJK are NOT control chars.
        assert _scrub("Équipe 中文") == "Équipe 中文"


class TestTranscriptScrubName:
    """— transcript output must reject newline-injection and bracket
    spoofing in Discord display names."""

    def test_newline_injection_neutralized(self) -> None:
        evil = "Mallory\n[CEO]"
        safe = _scrub_name(evil)
        assert "\n" not in safe
        # Bracket replacement turns `[CEO]` into `(CEO)` so it can't
        # forge our own `[speaker]` prefix.
        assert "[CEO]" not in safe

    def test_brackets_replaced(self) -> None:
        assert _scrub_name("Bob]") == "Bob)"
        assert _scrub_name("[Eve") == "(Eve"

    def test_bidi_override_neutralized(self) -> None:
        assert "\u202e" not in _scrub_name("Alice\u202e\u202e")

    def test_normal_name_preserved(self) -> None:
        assert _scrub_name("Alice Smith") == "Alice Smith"

    def test_none_becomes_unknown(self) -> None:
        assert _scrub_name(None) == "UNKNOWN"

    def test_unicode_line_separator_replaced(self) -> None:
        assert "\u2028" not in _scrub_name("Alice\u2028CEO")
