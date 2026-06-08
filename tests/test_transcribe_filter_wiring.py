"""Integration test for transcribe() → _filter_events_by_layout wiring
.

_filter_events_by_layout has thorough unit tests, but no test verified
that transcribe() actually calls the filter with the right arguments
before passing events to _speaking_events_to_diarize_df. A regression
that bypasses the filter would not be caught by helper-level tests.

We patch the helper and run transcribe() far enough to observe the
call. We don't run the full WhisperX pipeline — just enough to reach
the filter call site.
"""
from __future__ import annotations

import json
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from mynah import transcription


@pytest.fixture
def participants_json(tmp_path):
    """Write a participants.json with mic_only layout and self_user_id
    so the filter has a meaningful gate to apply."""
    data = {
        "audio_layout": "mic_only",
        "self_user_id": "111",
        "participants_detailed": [{"id": "111", "name": "Alice"}],
        "initial_participants": ["Alice"],
        "events": [],
        "speaking_events": [
            {"event": "speaking_start", "user_id": "111", "timestamp": 0.5},
            {"event": "speaking_stop", "user_id": "111", "timestamp": 2.0},
            {"event": "speaking_start", "user_id": "222", "timestamp": 3.0},
            {"event": "speaking_stop", "user_id": "222", "timestamp": 4.0},
        ],
        "speaking_events_complete": True,
    }
    path = tmp_path / "test_participants.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def audio_path(tmp_path):
    """A fake audio path. transcribe() reads bytes via WhisperX which
    we mock, so the file's actual contents don't matter."""
    path = tmp_path / "test_audio.wav"
    path.write_bytes(b"RIFFfakeWAVE")
    return path


class TestTranscribeCallsFilter:
    def test_filter_called_with_layout_and_self_user_id(
        self, monkeypatch, participants_json, audio_path,
    ):
        """transcribe() must invoke _filter_events_by_layout with the
        participant_data's audio_layout and self_user_id BEFORE
        _speaking_events_to_diarize_df runs."""
        filter_calls = []
        real_filter = transcription._filter_events_by_layout

        def _spy_filter(events, audio_layout, self_user_id):
            filter_calls.append({
                "n_events": len(events),
                "audio_layout": audio_layout,
                "self_user_id": self_user_id,
            })
            return real_filter(events, audio_layout, self_user_id)

        monkeypatch.setattr(
            transcription, "_filter_events_by_layout", _spy_filter,
        )

        # Short-circuit the heavy WhisperX path so we never need a real
        # model. The transcribe() function reaches the filter before
        # any of these matter — we just need them to not crash if
        # transcribe() does get past the filter.
        monkeypatch.setattr(transcription, "whisperx_available", lambda: True)
        monkeypatch.setattr(transcription, "_validate_model_name", lambda _: None)
        monkeypatch.setattr(transcription, "_ensure_ffmpeg_on_path", lambda: None)

        fake_torch = MagicMock()
        fake_torch.cuda.is_available.return_value = False
        fake_whisperx = MagicMock()
        fake_audio = MagicMock()
        fake_audio.__len__ = lambda self: 16000 * 10
        fake_whisperx.load_audio = MagicMock(return_value=fake_audio)
        fake_model = MagicMock()
        fake_model.transcribe.return_value = {"segments": []}
        fake_whisperx.load_model = MagicMock(return_value=fake_model)

        monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
        monkeypatch.setitem(__import__("sys").modules, "whisperx", fake_whisperx)

        # capture any exception in a
        # sentinel rather than swallowing silently. Past the filter
        # call, the pipeline does many things we haven't mocked — those
        # exceptions are expected. But if transcribe() fails BEFORE
        # reaching the filter (e.g., participant JSON parsing breaks),
        # we want the assertion failure message to surface the actual
        # exception so a maintainer can diagnose it quickly.
        post_transcribe_exception: Optional[BaseException] = None
        try:
            transcription.transcribe(
                audio_path=audio_path,
                participants_path=participants_json,
            )
        except Exception as e:
            post_transcribe_exception = e

        assert len(filter_calls) == 1, (
            f"Expected exactly one call to _filter_events_by_layout, "
            f"got {len(filter_calls)}. "
            f"transcribe() raised: {post_transcribe_exception!r}"
        )
        call = filter_calls[0]
        assert call["audio_layout"] == "mic_only"
        assert call["self_user_id"] == "111"
        # All 4 events (2 self, 2 other) were handed to the filter.
        assert call["n_events"] == 4
