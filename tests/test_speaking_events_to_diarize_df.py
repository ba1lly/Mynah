"""Tests for transcription._speaking_events_to_diarize_df.

Primary target: issue #22 (ID-less participants must not become
"user:None"). Also exercises the state-machine cases highest-ROI per
the testing review (#20 ROI #1): repeated starts, unmatched stops,
dangling starts at audio end, missing user_id, mid-call joiners
without IDs.
"""
from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")

from mynah.transcription import _speaking_events_to_diarize_df


def _start(uid, t):
    return {"user_id": uid, "event": "speaking_start", "timestamp": t}


def _stop(uid, t):
    return {"user_id": uid, "event": "speaking_stop", "timestamp": t}


class TestIdLessParticipantsAreSkipped:
    """Issue #22: roster entries with id=None must not appear in the DF."""

    def test_idless_roster_not_labeled_as_user_none(self):
        # Real Discord event for user_id="111"; roster contains an extra
        # entry with id=None whose presence used to leak into id_to_name
        # under the string key "None".
        events = [_start("111", 0.5), _stop("111", 1.5)]
        roster = [
            {"id": "111", "name": "Alice"},
            {"id": None, "name": "Mystery"},
        ]
        df = _speaking_events_to_diarize_df(events, roster, audio_seconds=10.0)
        assert df is not None
        speakers = set(df["speaker"].tolist())
        assert "Mystery" not in speakers
        assert "user:None" not in speakers
        assert "Alice" in speakers

    def test_event_with_no_user_id_dropped(self):
        events = [
            {"event": "speaking_start", "timestamp": 0.5},
            _start("111", 1.0),
            _stop("111", 2.0),
        ]
        df = _speaking_events_to_diarize_df(events, [{"id": "111", "name": "Alice"}], 5.0)
        assert df is not None
        assert (df["speaker"] == "Alice").all()


class TestStateMachine:
    def test_simple_start_stop_one_interval(self):
        events = [_start("111", 1.0), _stop("111", 3.0)]
        df = _speaking_events_to_diarize_df(events, [{"id": "111", "name": "Alice"}], 10.0)
        assert df is not None
        assert len(df) == 1
        assert df.iloc[0]["start"] == 1.0
        assert df.iloc[0]["end"] == 3.0
        assert df.iloc[0]["speaker"] == "Alice"

    def test_repeated_start_clips_previous(self):
        events = [_start("111", 1.0), _start("111", 2.0), _stop("111", 3.0)]
        df = _speaking_events_to_diarize_df(events, [{"id": "111", "name": "Alice"}], 10.0)
        assert df is not None
        assert len(df) == 2
        assert df.iloc[0]["end"] == 2.0
        assert df.iloc[1]["start"] == 2.0
        assert df.iloc[1]["end"] == 3.0

    def test_dangling_start_closes_at_audio_end(self):
        events = [_start("111", 1.0)]
        df = _speaking_events_to_diarize_df(events, [{"id": "111", "name": "Alice"}], 7.0)
        assert df is not None
        assert df.iloc[0]["end"] == 7.0

    def test_unmatched_stop_treated_as_speaking_from_zero(self):
        events = [_stop("111", 4.0)]
        df = _speaking_events_to_diarize_df(events, [{"id": "111", "name": "Alice"}], 10.0)
        assert df is not None
        assert df.iloc[0]["start"] == 0.0
        assert df.iloc[0]["end"] == 4.0

    def test_unknown_user_id_falls_through_to_user_prefix(self):
        events = [_start("999", 1.0), _stop("999", 2.0)]
        df = _speaking_events_to_diarize_df(events, [{"id": "111", "name": "Alice"}], 10.0)
        assert df is not None
        assert df.iloc[0]["speaker"] == "user:999"

    def test_empty_events_returns_none(self):
        df = _speaking_events_to_diarize_df([], [{"id": "111", "name": "Alice"}], 10.0)
        assert df is None

    def test_zero_length_stop_dropped(self):
        # stop comes BEFORE start (clock skew, reordered event) → interval
        # would have negative duration; we drop it.
        events = [_start("111", 5.0), _stop("111", 4.0)]
        df = _speaking_events_to_diarize_df(events, [{"id": "111", "name": "Alice"}], 10.0)
        assert df is None
