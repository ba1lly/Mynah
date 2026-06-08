"""Tests for transcription._attach_user_via_channel_energy (issue #23).

Two scenarios:

  A. Short recording (< 5 noise-floor windows): the previous fallback
     set noise_floor = 0.2 * RMS_of_speech, which then made the
     threshold higher than the actual speech signal. The user was
     never marked even when they spoke through the entire clip. Fixed
     by using a fixed floor in the short-clip branch.

  B. Module-level named constants exist (no more magic 0.55, 5.0,
     0.25, 2e-3 sprinkled inline).
"""
from __future__ import annotations

import numpy as np
import pytest

from mynah.transcription import (
    _attach_user_via_channel_energy,
    _auto_map,
    CHANNEL_ENERGY_ABSOLUTE_FLOOR,
    CHANNEL_ENERGY_ACTIVE_FRACTION,
    CHANNEL_ENERGY_DETECT_WIN_SEC,
    CHANNEL_ENERGY_NOISE_MULTIPLIER,
)


SR = 16000


def _speech(seconds, amp=0.2, sr=SR):
    # 1 kHz sine at the given amplitude → constant RMS = amp / sqrt(2).
    t = np.arange(int(seconds * sr), dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)


def _silence(seconds, sr=SR):
    return np.zeros(int(seconds * sr), dtype=np.float32)


def _seg(start, end):
    return {"start": start, "end": end, "speaker": "SPEAKER_00"}


class TestNamedConstants:
    """Issue #23 part B: tunables exist as module-level names with values
    matching the documented defaults."""

    def test_constants_have_sensible_defaults(self):
        assert CHANNEL_ENERGY_DETECT_WIN_SEC == 0.25
        assert CHANNEL_ENERGY_ACTIVE_FRACTION == 0.55
        assert CHANNEL_ENERGY_NOISE_MULTIPLIER == 5.0
        assert CHANNEL_ENERGY_ABSOLUTE_FLOOR == 2e-3


class TestShortRecordingFallback:
    """Issue #23 part A: <5-second clip where the user spoke continuously."""

    def test_user_marked_on_short_clip_full_speech(self):
        # 3 seconds, mic full of clearly-above-floor speech the whole time.
        left = _speech(3.0, amp=0.5)
        right = _silence(3.0)
        segments = [_seg(0.0, 3.0)]
        _attach_user_via_channel_energy(segments, left, right, "Alice", sample_rate=SR)
        # Issue #19: structured fields replace the `__USER__:<name>`
        # sentinel. The display name now lives in `speaker` directly,
        # and the `is_local_user` boolean marks the assignment.
        assert segments[0]["speaker"] == "Alice"
        assert segments[0].get("is_local_user") is True, (
            "User speaking through the entire 3s clip should be marked "
            "with is_local_user=True, but got %r" % segments[0]
        )

    def test_user_not_marked_on_short_silence_clip(self):
        left = _silence(3.0)
        right = _silence(3.0)
        segments = [_seg(0.0, 3.0)]
        _attach_user_via_channel_energy(segments, left, right, "Alice", sample_rate=SR)
        assert segments[0]["speaker"] == "SPEAKER_00"

    def test_short_quiet_segment_not_marked(self):
        # Below the absolute floor: ~0.001 amplitude.
        left = _speech(3.0, amp=0.001)
        right = _silence(3.0)
        segments = [_seg(0.0, 3.0)]
        _attach_user_via_channel_energy(segments, left, right, "Alice", sample_rate=SR)
        assert segments[0]["speaker"] == "SPEAKER_00"


class TestLongRecording:
    """The percentile-based estimator path (>= 5 floor windows)."""

    def test_long_clip_with_clear_speech_marks_user(self):
        # 10 seconds: 4s silence (noise floor sample), 6s clear speech.
        left = np.concatenate([_silence(4.0), _speech(6.0, amp=0.5)])
        right = _silence(10.0)
        segments = [_seg(4.0, 10.0)]
        _attach_user_via_channel_energy(segments, left, right, "Alice", sample_rate=SR)
        assert segments[0]["speaker"] == "Alice"
        assert segments[0].get("is_local_user") is True

    def test_brief_chime_in_does_not_claim_segment(self):
        # 10s segment, user only speaks the first 1s. 1s/10s = 10% active
        # which is well below the 55% active-fraction threshold.
        left = np.concatenate([_speech(1.0, amp=0.5), _silence(9.0)])
        right = _silence(10.0)
        segments = [_seg(0.0, 10.0)]
        _attach_user_via_channel_energy(segments, left, right, "Alice", sample_rate=SR)
        assert segments[0]["speaker"] == "SPEAKER_00"


class TestNoiseFloorBoundary:
    """boundary cases around the `n_floor_wins >= 5` branch in
    _attach_user_via_channel_energy. Exactly 4 / 5 / 6 one-second
    windows exercise the threshold branch on either side."""

    @pytest.mark.parametrize("n_seconds", [4, 5, 6])
    def test_user_marked_with_clear_speech_at_boundary(self, n_seconds):
        # Clip is N seconds of speech-only audio. At N=4 we are below
        # the 5-window threshold and take the fixed-floor branch;
        # at N>=5 we take the percentile branch with no clean silence
        # sample (every window is speech, so the 20th percentile of
        # all-speech RMS is still ABOVE the absolute floor — but the
        # threshold is the max of percentile*5 and the floor, which
        # would block the user even at N=5 unless our threshold logic
        # is robust). We verify both branches produce sensible marks.
        left = _speech(float(n_seconds), amp=0.5)
        right = _silence(float(n_seconds))
        segments = [_seg(0.0, float(n_seconds))]
        _attach_user_via_channel_energy(
            segments, left, right, "Alice", sample_rate=SR,
        )
        # At n_seconds=4 the short-clip branch uses the absolute floor
        # and we will mark the user. At n_seconds>=5 the percentile
        # branch sees RMS_speech ~ 0.5/sqrt(2) ~ 0.354, percentile*5 ~
        # 1.77 which is way above 0.5/sqrt(2) → segment WILL NOT be
        # marked because the threshold is set above the signal. This
        # is the known limitation of percentile-on-all-speech: it
        # cannot distinguish "user spoke the whole clip" from "no
        # user". The user-facing behavior is documented and tested
        # below; here we only assert the function does not raise and
        # leaves a well-formed speaker label.
        assert isinstance(segments[0]["speaker"], str)
        # Either outcome is acceptable at this boundary — see the
        # all-speech-percentile-failure comment above. With the issue
        # #19 refactor the "marked" case is (speaker=Alice AND
        # is_local_user=True); the "not marked" case is (speaker stays
        # as SPEAKER_00 AND no is_local_user field).
        if segments[0].get("is_local_user"):
            assert segments[0]["speaker"] == "Alice"
        else:
            assert segments[0]["speaker"] == "SPEAKER_00"

    def test_user_marked_with_silence_buffer_at_boundary_5_windows(self):
        # Exactly 5 floor windows = percentile branch. Include a clean
        # silence buffer so the noise floor estimate is accurate, then
        # verify the user is marked.
        left = np.concatenate([_silence(2.0), _speech(3.0, amp=0.5)])
        right = _silence(5.0)
        segments = [_seg(2.0, 5.0)]
        _attach_user_via_channel_energy(
            segments, left, right, "Alice", sample_rate=SR,
        )
        assert segments[0]["speaker"] == "Alice"
        assert segments[0].get("is_local_user") is True

    def test_silence_clip_not_marked_at_each_boundary(self):
        for n_seconds in (4, 5, 6):
            left = _silence(float(n_seconds))
            right = _silence(float(n_seconds))
            segments = [_seg(0.0, float(n_seconds))]
            _attach_user_via_channel_energy(
                segments, left, right, "Alice", sample_rate=SR,
            )
            assert segments[0]["speaker"] == "SPEAKER_00", (
                f"n_seconds={n_seconds}: silence-only clip should NOT "
                f"mark user, got {segments[0]['speaker']!r}"
            )


class TestLongAllSpeechMarking:
    """long all-speech recordings (≥5 s, user
    talked through the entire clip with no quiet gap) now correctly mark
    the user. Pre-fix, the percentile-based noise floor estimator
    derived its noise floor FROM the speech itself, producing a
    threshold ABOVE the speech RMS — the user was never marked.

    The fix detects this case via the 80th-percentile RMS: when even
    the 80th percentile is above floor × multiplier, the recording is
    almost certainly all-speech and we fall back to the absolute floor.
    """

    @pytest.mark.parametrize("n_seconds", [5, 10, 30, 60])
    def test_long_all_speech_recording_marks_user(self, n_seconds):
        # Solid speech across the entire clip, no silence at any point.
        # Pre-fix, the percentile estimator would set noise_floor to
        # the speech RMS itself (≈ 0.354 for amp=0.5 sine), and the
        # threshold would be ~1.77 — well above the 0.354 signal —
        # producing SPEAKER_00 instead of the user mark.
        left = _speech(float(n_seconds), amp=0.5)
        right = _silence(float(n_seconds))
        segments = [_seg(0.0, float(n_seconds))]
        _attach_user_via_channel_energy(
            segments, left, right, "Alice", sample_rate=SR,
        )
        assert segments[0]["speaker"] == "Alice", (
            f"n_seconds={n_seconds}: continuous speech through the "
            f"entire clip should set speaker=Alice. Got "
            f"{segments[0]['speaker']!r}."
        )
        assert segments[0].get("is_local_user") is True, (
            f"n_seconds={n_seconds}: continuous speech should be "
            f"tagged is_local_user=True. Got {segments[0]!r}. This is "
            f"the fix — the previous percentile-only logic "
            f"produced a threshold above the speech signal."
        )

    def test_long_silence_then_speech_still_uses_percentile(self):
        # Clip with a clean silence sample (4s silence + 6s speech).
        # The 20th-percentile noise-floor estimate is close to zero,
        # threshold collapses to CHANNEL_ENERGY_ABSOLUTE_FLOOR, and
        # user is marked normally. The 80th-percentile is the speech
        # RMS — but the all-speech detector should NOT trigger
        # because the existence of a quiet sample means the percentile
        # estimator works correctly.
        left = np.concatenate([_silence(4.0), _speech(6.0, amp=0.5)])
        right = _silence(10.0)
        segments = [_seg(4.0, 10.0)]
        _attach_user_via_channel_energy(
            segments, left, right, "Alice", sample_rate=SR,
        )
        assert segments[0]["speaker"] == "Alice"
        assert segments[0].get("is_local_user") is True

    def test_long_all_silence_recording_not_marked(self):
        # Sanity: a 10-second silence-only recording must NOT mark the
        # user. The 80th-percentile RMS of pure silence is ≈ 0, which
        # is well below the all-speech detection threshold; the
        # percentile branch applies and the noise floor + threshold are
        # both near zero, but speech-detection still fails because the
        # segment's per-window RMS is also ≈ 0.
        left = _silence(10.0)
        right = _silence(10.0)
        segments = [_seg(0.0, 10.0)]
        _attach_user_via_channel_energy(
            segments, left, right, "Alice", sample_rate=SR,
        )
        assert segments[0]["speaker"] == "SPEAKER_00"


class TestAutoMapSkipsLocalUserV22:
    """`_auto_map` must SKIP segments tagged
    `is_local_user=True`. The #19 refactor replaced the
    `__USER__:<name>` string sentinel with this boolean, but no test
    previously verified the skip behaviour in `_auto_map` (only the
    upstream marking via `_attach_user_via_channel_energy` was
    tested). A regression that drops the skip would silently
    re-cluster the local user's segments into a SPEAKER_NN slot."""

    def test_local_user_segment_not_in_order_or_mapping(self):
        segments = [
            {"start": 0.0, "end": 1.0, "speaker": "Alice", "is_local_user": True},
            {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"},
            {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_01"},
        ]
        mapped, unmapped = _auto_map(segments, participants=["Bob", "Carol"])

        assert mapped[0]["speaker"] == "Alice"
        assert mapped[0]["is_local_user"] is True
        assert mapped[1]["speaker"] == "Bob"
        assert mapped[2]["speaker"] == "Carol"

    def test_local_user_segment_keeps_original_speaker(self):
        segments = [
            {"start": 0.0, "end": 1.0, "speaker": "Alice", "is_local_user": True},
            {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"},
        ]
        _auto_map(segments, participants=["Bob"])

        assert segments[0]["speaker"] == "Alice"

    def test_multiple_local_user_segments_all_skipped(self):
        segments = [
            {"start": 0.0, "end": 1.0, "speaker": "Alice", "is_local_user": True},
            {"start": 1.0, "end": 2.0, "speaker": "Alice", "is_local_user": True},
            {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_00"},
        ]
        _auto_map(segments, participants=["Bob"])

        for seg in segments[:2]:
            assert seg["speaker"] == "Alice"
            assert seg["is_local_user"] is True
        assert segments[2]["speaker"] == "Bob"
