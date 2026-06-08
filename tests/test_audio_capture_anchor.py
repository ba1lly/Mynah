"""Integration tests for AudioRecorder._capture setting first_audio_time
.

The audio-anchor fix (#24) is only correct if the capture thread
actually populates `_first_audio_time` on the first read. Unit tests
for the consumer (test_audio_anchor.py) mock the value; these tests
exercise the producer.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from mynah.audio import AudioRecorder


def _fake_stream(chunks):
    """Stream stub whose `read()` returns `chunks` then blocks until the
    test releases the recorder."""
    stream = MagicMock()
    counter = {"i": 0}

    def _read(*args, **kwargs):
        idx = counter["i"]
        counter["i"] += 1
        if idx < len(chunks):
            return chunks[idx]
        # No more chunks → sleep briefly so the producer loops without
        # spinning the CPU. The test will set _running=False to stop it.
        time.sleep(0.005)
        return b""

    stream.read = _read
    return stream


class TestCaptureSetsFirstAudioTime:
    def test_first_audio_time_set_on_first_read(self, monkeypatch):
        rec = AudioRecorder(source="mic_only")
        rec._running = True
        assert rec.first_audio_time is None

        sink: list[bytes] = []
        stream = _fake_stream([b"chunk0", b"chunk1", b"chunk2"])

        clock = {"t": 1000.0}

        def _now():
            clock["t"] += 1.0
            return clock["t"]

        monkeypatch.setattr(time, "time", _now)

        thread = threading.Thread(
            target=rec._capture, args=(stream, sink, "mic"), daemon=True,
        )
        thread.start()
        # Wait until the capture loop has produced at least 3 chunks.
        for _ in range(200):
            if len(sink) >= 3:
                break
            time.sleep(0.001)
        rec._running = False
        thread.join(timeout=1)

        # The anchor must be set, and set to the FIRST _now() value
        # (1001.0). _capture short-circuits the assignment after the
        # first read, so the anchor reflects the first sample's clock,
        # not the last one's.
        assert rec.first_audio_time == 1001.0
        assert sink[:3] == [b"chunk0", b"chunk1", b"chunk2"]

    def test_first_audio_time_not_overwritten_by_later_reads(self, monkeypatch):
        rec = AudioRecorder(source="mic_only")
        rec._running = True

        sink: list[bytes] = []
        stream = _fake_stream([b"a", b"b", b"c", b"d", b"e"])

        clock = {"t": 0.0}

        def _now():
            clock["t"] += 1.0
            return clock["t"]

        monkeypatch.setattr(time, "time", _now)

        thread = threading.Thread(
            target=rec._capture, args=(stream, sink, "mic"), daemon=True,
        )
        thread.start()
        for _ in range(50):
            if len(sink) >= 5:
                break
            time.sleep(0.001)
        rec._running = False
        thread.join(timeout=1)

        # Each _now() advances by 1.0. The first read's anchor must be
        # 1.0 — NOT the wall-clock of the latest read.
        assert rec.first_audio_time == 1.0


class TestStartResetsAnchor:
    """AudioRecorder.start() must clear
    first_audio_time so a reused instance does not anchor a new
    recording to the timestamp of the previous one's first sample.

    The original test was hollow — it manually set
    `_first_audio_time = None` and asserted the property returned None,
    never invoking `start()` itself. A regression that removed the
    reset line from `start()` passed the test silently. These tests
    now invoke `start()` directly with the conftest pyaudiowpatch
    MagicMock stub, then call `stop()` to clean up the capture threads.
    """

    def _build_pa_stub(self, monkeypatch):
        """Make pyaudio.PyAudio() return a stub whose streams produce
        a single chunk and then return empty bytes. Keeps the capture
        threads alive for at most one read so `start()` can complete
        and `stop()` can join them within the test timeout."""
        import mynah.audio as audio_mod

        stream = MagicMock()
        # `stream.read(N, ...)` returns bytes; produce a single small
        # int16 chunk then empty so the capture thread loops a few times
        # before _running flips False.
        stream.read = MagicMock(return_value=b"\x00\x01" * 1024)

        pa_instance = MagicMock()
        # Default device info: 48 kHz / 2 channel int16, index 0.
        pa_instance.get_default_input_device_info = MagicMock(
            return_value={
                "defaultSampleRate": 48000,
                "maxInputChannels": 2,
                "maxOutputChannels": 0,
                "index": 0,
                "name": "mock-mic",
            }
        )
        pa_instance.open = MagicMock(return_value=stream)
        pa_instance.terminate = MagicMock()

        monkeypatch.setattr(
            audio_mod.pyaudio, "PyAudio", MagicMock(return_value=pa_instance),
            raising=False,
        )
        # pyaudio.paInt16/paFloat32 are referenced as constants.
        monkeypatch.setattr(audio_mod.pyaudio, "paInt16", 8, raising=False)
        monkeypatch.setattr(audio_mod.pyaudio, "paFloat32", 1, raising=False)

    def test_start_actually_resets_stale_anchor(self, monkeypatch):
        """The real behavior: stale anchor + start() → anchor cleared."""
        self._build_pa_stub(monkeypatch)
        rec = AudioRecorder(source="mic_only")
        # Simulate a previous recording having set the anchor to a
        # bogus value. start() must clear it before launching capture
        # threads; otherwise the threads would not overwrite it (they
        # short-circuit on the `if self._first_audio_time is None:`
        # check) and the new recording would anchor to the old
        # wall-clock time.
        rec._first_audio_time = 5000.0
        assert rec.first_audio_time == 5000.0

        rec.start()
        try:
            # After start(), the field should have been cleared (then
            # potentially re-set by a capture-thread read; either way it
            # MUST not be 5000.0 anymore).
            assert rec._first_audio_time != 5000.0, (
                "AudioRecorder.start() failed to reset the stale "
                "first_audio_time anchor"
            )
        finally:
            rec._running = False
            for t in (rec._mic_thread, rec._sys_thread):
                if t is not None:
                    t.join(timeout=1)

    def test_first_audio_time_starts_none_on_new_instance(self):
        rec = AudioRecorder(source="mic_only")
        assert rec.first_audio_time is None
