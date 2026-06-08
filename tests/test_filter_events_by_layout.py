"""Tests for transcription._filter_events_by_layout (issue #21).

mic_only / system_only recordings only capture a subset of the speakers
Discord emits SPEAKING events for. Applying the unfiltered events to
the audio timeline silently mislabels speech with names whose audio
was never captured. The filter gates events by audio_layout.
"""
from __future__ import annotations

import pytest

from mynah.transcription import _filter_events_by_layout


def _ev(user_id, kind="speaking_start", ts=1.0):
    return {"user_id": user_id, "event": kind, "timestamp": ts}


class TestMicOnly:
    def test_keeps_only_self(self):
        events = [_ev("111"), _ev("222"), _ev("111")]
        out = _filter_events_by_layout(events, "mic_only", self_user_id="111")
        assert all(e["user_id"] == "111" for e in out)
        assert len(out) == 2

    def test_string_int_self_user_id_coerced(self):
        events = [_ev("111"), _ev("222")]
        out = _filter_events_by_layout(events, "mic_only", self_user_id=111)
        assert [e["user_id"] for e in out] == ["111"]

    def test_no_self_user_id_returns_empty_for_fallback(self):
        # docstring contract. Without self_user_id we can't tell
        # which events are local-user, so we MUST return [] to trigger
        # acoustic-diarization fallback instead of fabricating labels by
        # applying remote-user events to mic-only audio.
        events = [_ev("111"), _ev("222")]
        assert _filter_events_by_layout(events, "mic_only", self_user_id=None) == []


class TestSystemOnly:
    def test_drops_self(self):
        events = [_ev("111"), _ev("222"), _ev("111")]
        out = _filter_events_by_layout(events, "system_only", self_user_id="111")
        assert all(e["user_id"] != "111" for e in out)
        assert len(out) == 1

    def test_all_self_events_returns_empty(self):
        events = [_ev("111"), _ev("111")]
        out = _filter_events_by_layout(events, "system_only", self_user_id="111")
        assert out == []

    def test_no_self_user_id_returns_empty_for_fallback(self):
        # docstring contract — see TestMicOnly equivalent.
        events = [_ev("111"), _ev("222")]
        assert _filter_events_by_layout(events, "system_only", self_user_id=None) == []


class TestPassthrough:
    @pytest.mark.parametrize("layout", ["channel_split", "mixed", None, "anything_else"])
    def test_other_layouts_pass_through(self, layout):
        events = [_ev("111"), _ev("222")]
        out = _filter_events_by_layout(events, layout, self_user_id="111")
        assert out == events

    def test_empty_input_returns_empty(self):
        assert _filter_events_by_layout([], "mic_only", "111") == []


class TestNoneUserIdDropped:
    """events with missing user_id are dropped in every layout
    so the caller's progress-log count reflects only mappable events."""

    def test_none_user_id_dropped_passthrough_layout(self):
        events = [_ev(None), _ev("111"), _ev(None)]
        out = _filter_events_by_layout(events, "mixed", self_user_id="111")
        assert all(e["user_id"] is not None for e in out)
        assert len(out) == 1
        assert out[0]["user_id"] == "111"

    def test_none_user_id_dropped_mic_only(self):
        events = [_ev(None), _ev("111")]
        out = _filter_events_by_layout(events, "mic_only", self_user_id="111")
        assert [e["user_id"] for e in out] == ["111"]

    def test_none_user_id_dropped_system_only(self):
        events = [_ev(None), _ev("222"), _ev("111")]
        out = _filter_events_by_layout(events, "system_only", self_user_id="111")
        assert [e["user_id"] for e in out] == ["222"]

    def test_only_none_user_ids_returns_empty(self):
        events = [_ev(None), _ev(None)]
        out = _filter_events_by_layout(events, "mixed", self_user_id="111")
        assert out == []
