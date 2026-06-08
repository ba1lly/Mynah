"""Tests for transcription._validate_model_name ."""
from __future__ import annotations

import pytest

from mynah.transcription import _validate_model_name


class TestKnownModels:
    @pytest.mark.parametrize("name", [
        "tiny", "tiny.en",
        "base", "small.en",
        "large-v3", "large-v3-turbo",
        "distil-large-v3",
    ])
    def test_known_name_accepted(self, name: str) -> None:
        # Returns None on success.
        assert _validate_model_name(name) is None


class TestHuggingFacePattern:
    def test_org_repo_accepted(self) -> None:
        _validate_model_name("openai/whisper-large-v3")

    def test_dotted_repo_accepted(self) -> None:
        _validate_model_name("user.name/model.v1")


class TestRejection:
    def test_empty_string_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name("   ")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name(None)  # type: ignore[arg-type]

    def test_local_unix_path_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name("/etc/passwd")

    def test_local_windows_path_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name("C:\\Windows\\System32")

    def test_traversal_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name("../../../evil")

    def test_no_slash_unknown_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name("not-a-known-model")

    def test_multiple_slashes_rejected(self) -> None:
        with pytest.raises(RuntimeError):
            _validate_model_name("a/b/c")
