"""Tests for RecordingSession audio-anchor snapshot (issue #24).

The session takes a SINGLE snapshot of the audio anchor during start(),
after briefly waiting for the capture thread to deliver its first
sample. Every persisted timestamp (speaking events AND join/leave
events) uses that fixed snapshot — eliminating the backward time-step
between events fired before vs after first_audio_time was set that
the pre-fix _audio_anchor() produced .
"""
from __future__ import annotations

import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from mynah.recorder import RecordingSession


def _build_session(snapshot):
    sess = RecordingSession.__new__(RecordingSession)
    sess.audio = MagicMock()
    sess._start_time = 1000.0
    sess._audio_anchor_snapshot = snapshot
    sess._running = True
    sess._speaking_lock = threading.Lock()
    sess._speaking_events = []
    sess._speaking_subscribed = True
    return sess


class TestAudioAnchorReturnsSnapshot:
    """_audio_anchor() is a simple getter for the snapshot."""

    def test_returns_snapshot_when_set(self):
        sess = _build_session(snapshot=1001.5)
        assert sess._audio_anchor() == 1001.5

    def test_returns_snapshot_even_if_audio_first_audio_time_changes(self):
        # The whole point of the snapshot design: once start() has frozen
        # the anchor, subsequent reads must NOT re-derive it from
        # audio.first_audio_time. A future capture-thread reset would
        # otherwise produce a backward time-step.
        sess = _build_session(snapshot=1001.5)
        sess.audio.first_audio_time = 9999.0  # would have been picked up pre-fix
        assert sess._audio_anchor() == 1001.5

    def test_returns_start_time_when_snapshot_is_start_time(self):
        sess = _build_session(snapshot=1000.0)
        assert sess._audio_anchor() == 1000.0


class TestSpeakingEventTimestampsUseSnapshot:
    def test_start_timestamp_offset_by_snapshot(self, monkeypatch):
        # snapshot = 1000.4 (a 400ms device-open delay snapshotted at start).
        # The event fires at wall-clock 1002.0, so the audio-timeline
        # offset is 1002 - 1000.4 = 1.6, NOT 1002 - 1000 = 2.0.
        sess = _build_session(snapshot=1000.4)
        monkeypatch.setattr(time, "time", lambda: 1002.0)
        sess._on_speaking_start({"user_id": "111"})
        ev = sess._speaking_events[-1]
        assert ev["event"] == "speaking_start"
        assert ev["user_id"] == "111"
        assert ev["timestamp"] == pytest.approx(1.6)

    def test_stop_timestamp_offset_by_snapshot(self, monkeypatch):
        sess = _build_session(snapshot=1000.4)
        monkeypatch.setattr(time, "time", lambda: 1002.0)
        sess._on_speaking_stop({"user_id": "111"})
        assert sess._speaking_events[-1]["timestamp"] == pytest.approx(1.6)


class TestNoBackwardTimeStep:
    """with the snapshot design, two events firing before vs
    after the capture thread sets first_audio_time MUST produce
    monotonically-increasing timestamps. The pre-fix _audio_anchor()
    produced a backward step when audio.first_audio_time transitioned
    from None to a value greater than _start_time."""

    def test_consecutive_events_monotonic_across_first_audio_time_set(
        self, monkeypatch,
    ):
        # Simulate the post-fix design: snapshot was frozen at start().
        # No mid-session anchor mutation can produce a backward step.
        sess = _build_session(snapshot=1000.4)

        wall_clock = {"t": 1000.5}

        def _now():
            t = wall_clock["t"]
            wall_clock["t"] += 0.001  # advance ~1ms per call
            return t

        monkeypatch.setattr(time, "time", _now)

        # Event 1 fires BEFORE the capture thread (in a hypothetical
        # pre-fix world) would have set first_audio_time.
        sess._on_speaking_start({"user_id": "111"})
        ts1 = sess._speaking_events[-1]["timestamp"]

        # Simulate the capture thread "setting" first_audio_time mid-run.
        # In the snapshot design this MUST NOT affect _audio_anchor().
        sess.audio.first_audio_time = 1000.4

        wall_clock["t"] = 1000.6
        sess._on_speaking_stop({"user_id": "111"})
        ts2 = sess._speaking_events[-1]["timestamp"]

        assert ts2 >= ts1, (
            f"timestamps must be monotonically increasing; got "
            f"ts1={ts1!r} > ts2={ts2!r} (backward step from anchor change)"
        )


class TestSnapshotAudioAnchor:
    """_snapshot_audio_anchor() waits briefly for the first audio
    sample and returns it; falls back to _start_time on timeout."""

    def test_returns_first_audio_time_when_already_set(self):
        sess = RecordingSession.__new__(RecordingSession)
        sess.audio = MagicMock()
        sess.audio.first_audio_time = 1000.4
        sess._start_time = 1000.0
        assert sess._snapshot_audio_anchor() == 1000.4

    def test_waits_then_returns_first_audio_time_when_late(self, monkeypatch):
        sess = RecordingSession.__new__(RecordingSession)
        sess.audio = MagicMock()
        sess.audio.first_audio_time = None
        sess._start_time = 1000.0

        # Have first_audio_time become non-None after ~3 polls.
        readings = {"count": 0}

        def _get_first_audio():
            readings["count"] += 1
            if readings["count"] < 3:
                return None
            return 1000.4

        type(sess.audio).first_audio_time = property(
            lambda _self: _get_first_audio()
        )

        monkeypatch.setattr(time, "time", lambda: 1000.0)  # well before deadline
        monkeypatch.setattr(time, "sleep", lambda _: None)

        assert sess._snapshot_audio_anchor() == 1000.4

    def test_falls_back_to_start_time_on_timeout(self, monkeypatch):
        sess = RecordingSession.__new__(RecordingSession)
        sess.audio = MagicMock()
        sess.audio.first_audio_time = None
        sess._start_time = 1000.0

        # Simulate time advancing past the deadline.
        clock = {"t": 1000.0}

        def _now():
            clock["t"] += 0.5
            return clock["t"]

        monkeypatch.setattr(time, "time", _now)
        monkeypatch.setattr(time, "sleep", lambda _: None)

        assert sess._snapshot_audio_anchor() == 1000.0

    def test_attribute_missing_raises_attributeerror(self):
        """direct attribute access — no getattr default. A future
        AudioRecorder refactor that renames or removes `first_audio_time`
        must produce AttributeError, NOT silently fall back to the
        pre-fix _start_time behavior."""
        sess = RecordingSession.__new__(RecordingSession)
        sess.audio = types.SimpleNamespace()  # no first_audio_time attribute
        sess._start_time = 1000.0
        with pytest.raises(AttributeError):
            sess._snapshot_audio_anchor()


class TestSessionInitConstructsAnchorAttr:
    """The snapshot field is initialised to 0.0 in __init__ so a session
    that hasn't called start() doesn't crash on _audio_anchor()."""

    def test_init_sets_snapshot_to_zero(self, tmp_path):
        rpc = MagicMock()
        sess = RecordingSession(
            rpc=rpc,
            output_dir=tmp_path,
            meeting_name=None,
            audio_source="mixed",
        )
        assert sess._audio_anchor_snapshot == 0.0
        assert sess._audio_anchor() == 0.0
