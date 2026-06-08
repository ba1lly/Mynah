"""Tests for recorder._sanitize_meeting_name."""
from __future__ import annotations

import pytest

from mynah.recorder import _sanitize_meeting_name


class TestPathTraversal:
    """`/` and `\\` are outside the allow-list so they become `_`; the
    resulting leading dots/underscores are then stripped, leaving a safe
    filename component with no separator characters."""

    def test_double_dot_traversal_neutralized(self) -> None:
        result = _sanitize_meeting_name("../../evil")
        assert result == "evil"
        assert "/" not in (result or "")
        assert ".." not in (result or "")

    def test_backslash_traversal_neutralized(self) -> None:
        result = _sanitize_meeting_name("..\\..\\windows")
        assert result == "windows"
        assert "\\" not in (result or "")

    def test_absolute_path_neutralized(self) -> None:
        result = _sanitize_meeting_name("/etc/passwd")
        assert result == "etc_passwd"
        assert "/" not in (result or "")

    def test_traversal_with_legitimate_prefix(self) -> None:
        # Path separators are the actual escape primitive — `..` alone
        # inside a single basename cannot traverse. We assert no
        # separators survive, which is what keeps the output inside
        # the recordings directory.
        result = _sanitize_meeting_name("meeting/../../etc/passwd")
        assert result is not None
        assert "/" not in result
        assert "\\" not in result


class TestReservedNames:
    @pytest.mark.parametrize("name", [
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM9",
        "LPT1", "LPT5",
        "con", "Nul", "lpt3",
    ])
    def test_bare_reserved_rejected(self, name: str) -> None:
        assert _sanitize_meeting_name(name) is None

    @pytest.mark.parametrize("name", [
        "CON.txt", "NUL.log", "COM1.foo", "lpt3.tar.gz",
        "con.txt", "Nul.LOG",
    ])
    def test_reserved_with_extension_rejected(self, name: str) -> None:
        # Windows treats `CON.txt` and `NUL.tar.gz` as references to the
        # device itself, not a file. fix.
        assert _sanitize_meeting_name(name) is None

    def test_reserved_substring_allowed(self) -> None:
        # "Concord" starts with CON but is not the device name.
        assert _sanitize_meeting_name("Concord") == "Concord"


class TestEmptyAndWhitespace:
    def test_empty_string_returns_none(self) -> None:
        assert _sanitize_meeting_name("") is None

    def test_none_returns_none(self) -> None:
        assert _sanitize_meeting_name(None) is None

    def test_only_dots_returns_none(self) -> None:
        assert _sanitize_meeting_name("...") is None

    def test_only_whitespace_returns_none(self) -> None:
        assert _sanitize_meeting_name("    ") is None


class TestNormalInput:
    def test_simple_name_preserved(self) -> None:
        assert _sanitize_meeting_name("Standup") == "Standup"

    def test_spaces_become_underscores(self) -> None:
        assert _sanitize_meeting_name("Team Meeting") == "Team_Meeting"

    def test_runs_collapsed(self) -> None:
        assert _sanitize_meeting_name("Team   Meeting") == "Team_Meeting"

    def test_punctuation_allowed(self) -> None:
        assert _sanitize_meeting_name("Q4 Review (final)") == "Q4_Review_(final)"


class TestLengthCap:
    def test_long_name_truncated_to_64(self) -> None:
        long_input = "A" * 100
        result = _sanitize_meeting_name(long_input)
        assert result is not None
        assert len(result) == 64
        assert result == "A" * 64

    def test_truncation_strips_trailing_unsafe(self) -> None:
        # Construct an input whose 64th character is "." so the
        # post-truncation strip removes it.
        long_input = ("A" * 63) + "." + ("B" * 10)
        result = _sanitize_meeting_name(long_input)
        assert result is not None
        assert not result.endswith(".")
        assert not result.endswith("_")
