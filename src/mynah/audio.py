"""Audio capture: microphone + WASAPI loopback of the default output, mixed.

Each device is opened at its OWN native sample rate / channels / format, then
resampled and converted to a canonical 48kHz stereo int16 stream at mix time.
This way we tolerate the wildly varied native formats of USB headsets,
Bluetooth devices, virtual cables, etc., that refuse fixed-format opens.
"""
from __future__ import annotations

import logging
import threading
import time
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional

import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger(__name__)

TARGET_RATE = 48000
TARGET_CHANNELS = 2
TARGET_SAMPWIDTH = 2
CHUNK_FRAMES = 1024


@dataclass
class _StreamSpec:
    rate: int
    channels: int
    pa_format: int   # pyaudio.paInt16 / paFloat32
    np_dtype: type   # np.int16 / np.float32


SOURCE_MIXED = "mixed"
SOURCE_MIC = "mic_only"
SOURCE_SYS = "system_only"
VALID_SOURCES = {SOURCE_MIXED, SOURCE_MIC, SOURCE_SYS}


def list_loopback_devices() -> "list[dict]":
    """Enumerate every WASAPI loopback-capable device PortAudio can see.

    Returns a list of dicts with three keys:
      ``raw_name`` — exact PortAudio device name (may collide with peers).
      ``index``   — PortAudio device index at enumeration time.
      ``label``   — disambiguated display string, suffixed with
                    ``" (#N)"`` when raw_name collides with another entry,
                    plus ``" (Windows default)"`` for the current default.

    The ``label`` is what gets shown in the Settings picker AND what
    gets persisted to config.json. Persisting the disambiguated label
    (rather than raw_name) is the only way to round-trip a user's
    choice when two sinks share a name — e.g. two of the same monitor
    model on the same GPU both expose ``"Odyssey G70B [Loopback]"``.
    """
    import pyaudiowpatch as pyaudio
    pa = pyaudio.PyAudio()
    try:
        try:
            default_loopback_name = pa.get_default_wasapi_loopback().get("name") or ""
        except Exception:
            default_loopback_name = ""

        raw: list[dict] = []
        for i in range(pa.get_device_count()):
            try:
                d = pa.get_device_info_by_index(i)
            except OSError:
                continue
            name = d.get("name") or ""
            if "[Loopback]" in name or "loopback" in name.lower():
                raw.append({"raw_name": name, "index": int(d["index"])})

        # Suffix duplicates with " (#N)" so the picker can disambiguate.
        # Count first so even the first occurrence of a duplicated name
        # gets a "(#1)" tag — otherwise the user can't tell whether the
        # plain entry is "the first one" or "the only one".
        name_counts: dict[str, int] = {}
        for d in raw:
            name_counts[d["raw_name"]] = name_counts.get(d["raw_name"], 0) + 1
        seen: dict[str, int] = {}
        out: list[dict] = []
        for d in raw:
            base = d["raw_name"]
            if name_counts[base] > 1:
                seen[base] = seen.get(base, 0) + 1
                label = f"{base} (#{seen[base]})"
            else:
                label = base
            if base == default_loopback_name and (
                name_counts[base] == 1 or seen.get(base) == 1
            ):
                label = f"{label} (Windows default)"
            out.append({
                "raw_name": base,
                "index": d["index"],
                "label": label,
            })
        return out
    finally:
        pa.terminate()


class AudioRecorder:
    def __init__(
        self,
        source: str = SOURCE_MIXED,
        loopback_device_name: str = "",
    ) -> None:
        if source not in VALID_SOURCES:
            raise ValueError(f"audio source must be one of {VALID_SOURCES}, got {source!r}")
        self.source = source
        # Empty string => use whatever Windows currently flags as the
        # default playback device (legacy behaviour). A non-empty value
        # is matched against PortAudio's device "name" field at start()
        # time. We deliberately match by NAME, not index, because PA
        # device indices are not stable across reboots / device hotplug.
        self.loopback_device_name = loopback_device_name or ""
        self._pa: Optional[pyaudio.PyAudio] = None
        self._mic_stream = None
        self._sys_stream = None
        self._mic_spec: Optional[_StreamSpec] = None
        self._sys_spec: Optional[_StreamSpec] = None
        self._mic_frames: list[bytes] = []
        self._sys_frames: list[bytes] = []
        self._mic_thread: Optional[threading.Thread] = None
        self._sys_thread: Optional[threading.Thread] = None
        self._running = False
        # Populated by a capture thread if it dies early. stop() uses this
        # so the recording isn't silently reported as successful when only
        # part of the audio was actually captured.
        self._capture_error: Optional[str] = None
        # time.time() of the FIRST sample delivered by EITHER capture
        # thread (mic or sys), set in _capture() via an unsynchronised
        # check-then-set idiom.
        #
        # In mixed mode the two capture threads can both observe `None`
        # on the load and both write; the later write wins under CPython
        # scheduling. The practical delta is microseconds (the two
        # WASAPI streams open within a few buffer periods of each other,
        # by definition before the first stream.read() returns), well
        # below the 100-500 ms device-open latency this anchor exists
        # to absorb. The race outcome is therefore "one of the two
        # sources' first samples, no guaranteed ordering" — not the
        # strict "earliest" the previous comment claimed. (in
        # .) If future code needs a strict guarantee,
        # gate this with a threading.Lock.
        #
        # Read via the public `first_audio_time` property;
        # RecordingSession.start() snapshots that ONCE per session
        # before subscribing to speaking events so all persisted
        # timestamps share a fixed time-zero.
        self._first_audio_time: Optional[float] = None

    @property
    def first_audio_time(self) -> Optional[float]:
        """Wall-clock time of the first delivered audio sample, or None.

        Public read-only accessor for the speaking-event anchor.
        Exposed as a real attribute (rather than reaching into
        `_first_audio_time` from outside) so a future refactor that
        changes how the anchor is sourced (e.g. PortAudio stream_time
        callbacks) cannot silently regress the consumer.
        """
        return self._first_audio_time

    def _want_mic(self) -> bool:
        return self.source in (SOURCE_MIXED, SOURCE_MIC)

    def _want_sys(self) -> bool:
        return self.source in (SOURCE_MIXED, SOURCE_SYS)

    def start(self) -> None:
        if self._running:
            return
        self._pa = pyaudio.PyAudio()
        self._mic_frames = []
        self._sys_frames = []
        self._capture_error = None
        # Reset the anchor every start() so a reused AudioRecorder
        # instance does not silently anchor a new recording to the
        # wall-clock time of a previous one's first sample.
        self._first_audio_time = None

        log.info("Audio source: %s", self.source)

        try:
            if self._want_mic():
                mic_info = self._pa.get_default_input_device_info()
                log.info("Mic: %s", mic_info.get("name"))
                self._mic_stream, self._mic_spec = self._open_native(
                    mic_info, prefer_format=pyaudio.paInt16, label="mic"
                )

            if self._want_sys():
                loopback_info = self._resolve_loopback_device()
                log.info("Loopback: %s", loopback_info.get("name"))
                self._sys_stream, self._sys_spec = self._open_native(
                    loopback_info, prefer_format=pyaudio.paFloat32, label="loopback"
                )
        except Exception:
            # Roll back any resources we opened before the failure so we
            # don't leak streams or hold the audio device open. Without
            # this, a "mic OK, loopback failed" partial-start path leaks
            # the mic stream and the PortAudio instance.
            self._teardown_streams()
            if self._pa is not None:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None
            raise

        self._running = True
        if self._mic_stream is not None:
            self._mic_thread = threading.Thread(
                target=self._capture, args=(self._mic_stream, self._mic_frames, "mic"), daemon=True
            )
            self._mic_thread.start()
        if self._sys_stream is not None:
            self._sys_thread = threading.Thread(
                target=self._capture, args=(self._sys_stream, self._sys_frames, "sys"), daemon=True
            )
            self._sys_thread.start()

    def _resolve_loopback_device(self) -> dict:
        """Pick the WASAPI loopback device this recording should capture.

        The configured value is the ``label`` produced by
        ``list_loopback_devices()``. We strip the optional
        ``" (Windows default)"`` tail (which is decoration only, not
        part of the device identity) and the optional ``" (#N)"``
        occurrence tag (which selects the Nth duplicate when two
        sinks share a raw name). What's left is matched against the
        live PortAudio device list.

        Fall-throughs:
        - configured label parses but no matching device exists →
          WARNING + use Windows default.
        - no override configured → use Windows default verbatim
          (legacy behaviour).
        - even the default isn't available → propagate as RpcError
          (the existing failure path).
        """
        if self._pa is None:
            raise RuntimeError("PortAudio is not initialised")

        if self.loopback_device_name:
            # Strip the " (Windows default)" tag if present — it's
            # informational, not part of the device name.
            base = self.loopback_device_name
            if base.endswith(" (Windows default)"):
                base = base[: -len(" (Windows default)")]
            # Parse a trailing " (#N)" if present (1-indexed).
            import re
            m = re.search(r"\s\(#(\d+)\)\s*$", base)
            wanted_occurrence = 1
            if m:
                wanted_occurrence = int(m.group(1))
                base = base[: m.start()]

            seen = 0
            for i in range(self._pa.get_device_count()):
                try:
                    d = self._pa.get_device_info_by_index(i)
                except OSError:
                    continue
                name = d.get("name") or ""
                if name == base and (
                    "[Loopback]" in name or "loopback" in name.lower()
                ):
                    seen += 1
                    if seen == wanted_occurrence:
                        return d
            log.warning(
                "Configured loopback device %r not found among enumerated "
                "devices (matched %d of %d expected occurrences) — falling "
                "back to Windows default playback loopback. Open "
                "Settings → Loopback device to pick a currently-attached "
                "device.",
                self.loopback_device_name, seen, wanted_occurrence,
            )

        try:
            return self._pa.get_default_wasapi_loopback()
        except OSError as e:
            raise RuntimeError(
                "Could not find a WASAPI loopback device. Make sure Windows "
                "audio is working and at least one playback device is enabled."
            ) from e

    def _open_native(self, info: dict, prefer_format: int, label: str):
        """Open a stream at the device's native rate/channels, trying both
        int16 and float32 if needed."""
        rate = int(info["defaultSampleRate"])
        # WASAPI loopback devices typically report maxInputChannels=0 — the
        # actual channel count lives in maxOutputChannels (the device is
        # being captured *from* the renderer side). Without this fallback,
        # `pa.open(channels=0, ...)` raises OSError on every format tried
        # and the system-audio path breaks entirely on common configs.
        channels = int(info.get("maxInputChannels") or 0)
        if channels <= 0:
            channels = int(info.get("maxOutputChannels") or 0)
        if channels <= 0:
            raise RuntimeError(
                f"Could not determine channel count for {label} device {info.get('name')!r}"
            )

        index = int(info["index"])
        formats_to_try = [prefer_format, pyaudio.paFloat32, pyaudio.paInt16]
        # de-dup while preserving order
        seen: set = set()
        formats_to_try = [f for f in formats_to_try if not (f in seen or seen.add(f))]

        last_err: Optional[Exception] = None
        for fmt in formats_to_try:
            try:
                stream = self._pa.open(
                    format=fmt,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=index,
                    frames_per_buffer=CHUNK_FRAMES,
                )
                np_dtype = np.float32 if fmt == pyaudio.paFloat32 else np.int16
                log.info(
                    "%s opened at %d Hz / %d ch / %s",
                    label, rate, channels,
                    "float32" if fmt == pyaudio.paFloat32 else "int16",
                )
                return stream, _StreamSpec(rate=rate, channels=channels, pa_format=fmt, np_dtype=np_dtype)
            except OSError as e:
                last_err = e
                continue
        raise RuntimeError(f"Could not open {label} device {info.get('name')!r}: {last_err}")

    def _capture(self, stream, sink: list[bytes], label: str) -> None:
        while self._running:
            try:
                data = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                if self._first_audio_time is None:
                    self._first_audio_time = time.time()
                sink.append(data)
            except Exception as e:
                # Record the failure so stop() can surface it, instead of
                # silently writing a truncated WAV that the user thinks is
                # a complete recording.
                log.error("%s capture error: %s", label, e)
                self._capture_error = f"{label} capture stopped: {e}"
                break

    def _stop_streams(self) -> None:
        """Force any in-flight ``stream.read()`` to return so the capture
        threads can exit their loop and be joined.

        Must run BEFORE joining the capture threads. PortAudio's WASAPI
        host (AUDIOSES.DLL) blocks ``read()`` until the next buffer is
        ready; setting ``self._running = False`` doesn't unblock that —
        only ``stop_stream()`` does. If we instead skip straight to
        ``close()`` while ``read()`` is still in flight on the same
        stream object, PA tears the WASAPI callback state down under
        the capture thread and crashes with an access violation
        (0xc0000005) inside AUDIOSES.DLL — Stop kills the entire
        process before the WAV can be written.
        """
        for attr in ("_mic_stream", "_sys_stream"):
            s = getattr(self, attr, None)
            if s is None:
                continue
            try:
                s.stop_stream()
            except Exception:
                pass

    def _close_streams(self) -> None:
        """Close stopped streams and forget the handles. Idempotent."""
        for attr in ("_mic_stream", "_sys_stream"):
            s = getattr(self, attr, None)
            if s is None:
                continue
            try:
                s.close()
            except Exception:
                pass
            setattr(self, attr, None)

    def _teardown_streams(self) -> None:
        """Stop + close streams in a single call. Used for partial-start
        failure paths where no capture threads are running yet, so the
        stop-then-close split that ``stop()`` needs doesn't apply.
        Idempotent.
        """
        self._stop_streams()
        self._close_streams()

    def stop(self, out_path: Path) -> Path:
        if not self._running:
            raise RuntimeError("Recorder is not running")
        self._running = False
        # Order matters: stop_stream() FIRST so blocking read() returns,
        # THEN join the capture threads, THEN close. See _stop_streams
        # for the AUDIOSES.DLL access-violation rationale.
        self._stop_streams()
        if self._mic_thread:
            self._mic_thread.join(timeout=2)
        if self._sys_thread:
            self._sys_thread.join(timeout=2)
        self._close_streams()
        if self._pa is not None:
            try:
                self._pa.terminate()
            finally:
                self._pa = None

        # If a capture thread bailed out early, surface that loudly rather
        # than writing whatever partial bytes ended up in the buffers and
        # pretending success. A truncated transcript in an evidentiary
        # context is worse than a clear error.
        if self._capture_error:
            raise RuntimeError(self._capture_error)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_mixed_wav(out_path)
        return out_path

    def _write_mixed_wav(self, out_path: Path) -> None:
        """Write a 48 kHz stereo int16 WAV.

        Layout depends on the audio_source setting:
          - "mixed":      L = mic only, R = system loopback only (channel-split).
                          This is critical for diarization quality: the user's
                          voice can only ever appear on L, everyone else's on R,
                          so the diarizer can't confuse them.
          - "mic_only":   L = R = mic (mono content in a stereo container).
          - "system_only":L = R = loopback.
        """
        mic = (
            _to_canonical_mono(b"".join(self._mic_frames), self._mic_spec)
            if self._mic_spec else np.empty(0, dtype=np.int16)
        )
        sys_ = (
            _to_canonical_mono(b"".join(self._sys_frames), self._sys_spec)
            if self._sys_spec else np.empty(0, dtype=np.int16)
        )

        if self.source == SOURCE_MIXED:
            mic_n = mic.shape[0]
            sys_n = sys_.shape[0]
            if mic_n == 0 and sys_n == 0:
                raise RuntimeError("No audio captured")
            diff_ms = abs(mic_n - sys_n) * 1000 / TARGET_RATE
            # Divergence policy:
            #   <250 ms:  normal sample-rate drift between two devices —
            #             truncate to min silently.
            #   <5 s:     driver hiccup / brief glitch on one side —
            #             warn and truncate to min so the channels stay
            #             time-aligned; the lost tail is short enough
            #             that a diarizer/transcriber won't be confused.
            #   ≥5 s:     one stream silently produced no audio for most
            #             of the recording — almost certainly a powered-
            #             off mic, headset disconnect, or Discord
            #             audio routed to a different output device than
            #             we loopback'd. Truncating in this regime
            #             destroys the recording: the GOOD side gets
            #             chopped down to ~21 ms of the bad side's tail
            #             buffer. Instead, pad the short side with
            #             zeros and keep the long side intact, so at
            #             least one channel of the recording survives
            #             the next user can recover something from.
            #             This matches what users want when their
            #             headset dies mid-call.
            if diff_ms >= 5000:
                n = max(mic_n, sys_n)
                if mic_n < sys_n:
                    log.error(
                        "Mic side captured only %.2fs of %.2fs — left "
                        "channel will be SILENT. Check that the "
                        "microphone device is powered on, not muted, "
                        "and selected as the Windows default input.",
                        mic_n / TARGET_RATE, sys_n / TARGET_RATE,
                    )
                    mic = np.concatenate(
                        [mic, np.zeros(n - mic_n, dtype=np.int16)]
                    )
                else:
                    log.error(
                        "Loopback side captured only %.2fs of %.2fs — "
                        "right channel will be SILENT. Check that "
                        "Discord audio is actually playing through the "
                        "Windows default output device.",
                        sys_n / TARGET_RATE, mic_n / TARGET_RATE,
                    )
                    sys_ = np.concatenate(
                        [sys_, np.zeros(n - sys_n, dtype=np.int16)]
                    )
                left = mic
                right = sys_
            else:
                n = min(mic_n, sys_n)
                if n == 0:
                    raise RuntimeError("No audio captured")
                if diff_ms > 250:
                    log.warning(
                        "Mic and loopback streams diverged by %.0f ms "
                        "— longer side will be truncated to match "
                        "shorter side",
                        diff_ms,
                    )
                left = mic[:n]
                right = sys_[:n]
        elif self.source == SOURCE_MIC:
            if mic.size == 0:
                raise RuntimeError("No microphone audio captured")
            left = right = mic
        else:  # SOURCE_SYS
            if sys_.size == 0:
                raise RuntimeError("No system audio captured")
            left = right = sys_

        # Interleave: [L0, R0, L1, R1, ...]
        interleaved = np.empty(len(left) * 2, dtype=np.int16)
        interleaved[0::2] = left
        interleaved[1::2] = right

        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(TARGET_CHANNELS)
            wf.setsampwidth(TARGET_SAMPWIDTH)
            wf.setframerate(TARGET_RATE)
            wf.writeframes(interleaved.tobytes())
        log.info("Wrote %s (%.1f sec)", out_path, len(left) / TARGET_RATE)

    def audio_layout(self) -> str:
        """Return a tag the transcriber uses to decide how to label speakers.
        - "channel_split": L=mic, R=loopback (only when source == mixed)
        - "mic_only":      L=R=mic
        - "system_only":   L=R=loopback
        """
        return {
            SOURCE_MIXED: "channel_split",
            SOURCE_MIC: "mic_only",
            SOURCE_SYS: "system_only",
        }[self.source]


def _to_canonical_mono(raw: bytes, spec: _StreamSpec) -> np.ndarray:
    """Convert raw bytes from a device to int16 mono @ TARGET_RATE.

    Multi-channel sources are downmixed (mean across channels). Returns a
    1-D int16 array of length = number of frames.
    """
    if not raw:
        return np.empty(0, dtype=np.int16)

    arr = np.frombuffer(raw, dtype=spec.np_dtype)
    if spec.channels > 1:
        arr = arr.reshape(-1, spec.channels)
    else:
        arr = arr.reshape(-1, 1)

    arr_f = arr.astype(np.float32)
    if spec.np_dtype == np.float32:
        arr_f = arr_f * 32767.0  # float32 audio is -1.0..1.0

    # Downmix to mono by averaging channels.
    arr_f = arr_f.mean(axis=1)  # shape (frames,)

    if spec.rate != TARGET_RATE:
        try:
            from scipy.signal import resample_poly
        except ImportError as e:
            raise RuntimeError(
                "scipy is required for sample-rate conversion. pip install scipy"
            ) from e
        ratio = Fraction(TARGET_RATE, spec.rate).limit_denominator(1000)
        arr_f = resample_poly(arr_f, ratio.numerator, ratio.denominator)

    arr_f = np.clip(arr_f, -32768, 32767)
    return arr_f.astype(np.int16)
