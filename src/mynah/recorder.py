"""Session orchestrator: combines RPC participant tracking and audio capture."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .audio import AudioRecorder
from .rpc import DiscordRPC

log = logging.getLogger(__name__)


# Names this could legitimately produce a Windows path-safe filename
# component from. Anything outside this set is collapsed to '_'. We also
# reject the small set of Windows reserved device names case-insensitively.
_FILENAME_SAFE = re.compile(r"[A-Za-z0-9 _\-.()\[\]]")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize_meeting_name(raw: Optional[str]) -> Optional[str]:
    """Coerce a free-text meeting name into a Windows-safe filename component.

    - Drops every character that is not in a small allow-list (letters,
      digits, space, `_`, `-`, `.`, parens, square brackets).
    - Collapses runs of whitespace/underscores.
    - Strips leading/trailing whitespace, dots, and underscores (Explorer
      treats trailing dots/spaces specially on Windows).
    - Rejects Windows reserved device names (CON, NUL, COM1, LPT1, …) by
      returning None so the recorder falls back to the unprefixed default.
    - Caps at 64 chars to leave room for the timestamp suffix.

    Without this, `meeting_name = "../../evil"` from the GUI would let an
    attacker (or an unlucky paste) steer the .wav and participants.json
    outside the configured recordings directory.
    """
    if not raw:
        return None
    cleaned_chars = [c if _FILENAME_SAFE.match(c) else "_" for c in raw]
    cleaned = "".join(cleaned_chars)
    # Collapse runs of "_" / whitespace introduced by the substitution.
    cleaned = re.sub(r"[_\s]+", "_", cleaned).strip("._ ")
    cleaned = cleaned[:64].strip("._ ")
    if not cleaned:
        return None
    # Windows treats reserved device basenames specially EVEN WITH
    # extensions: "CON.txt", "NUL.log", "COM1.tar.gz" all refer to the
    # device, not a file. Check the stem before the first dot, not the
    # whole cleaned string, against the reserved set.
    head = cleaned.split(".", 1)[0]
    if head.upper() in _WINDOWS_RESERVED:
        return None
    return cleaned


@dataclass
class RecordingResult:
    audio_path: Path
    participants_path: Path
    initial_participants: list[str]
    events: list[dict] = field(default_factory=list)


class RecordingSession:
    """Records one meeting. Construct, start(), then stop()."""

    POLL_INTERVAL_SEC = 2.0

    def __init__(
        self,
        rpc: DiscordRPC,
        output_dir: Path,
        meeting_name: Optional[str] = None,
        audio_source: str = "mixed",
        consent_record: Optional[dict] = None,
        loopback_device_name: str = "",
    ):
        self.rpc = rpc
        self.output_dir = Path(output_dir)
        self.meeting_name = _sanitize_meeting_name(meeting_name)
        self.audio = AudioRecorder(
            source=audio_source,
            loopback_device_name=loopback_device_name,
        )
        # Issue #25 (privacy consent gate): an attestation that the
        # local user knowingly authorised the recording before
        # capture started. Persisted verbatim to participants.json so
        # downstream tools (and the user reviewing the recording
        # later) have an auditable trail of consent. The GUI builds
        # the record from a modal dialog; tests / scripted recorders
        # may pass it directly.
        self.consent_record = consent_record
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._start_time = 0.0
        # Snapshotted ONCE during start() after waiting for the first
        # audio sample to arrive. Frozen for the rest of the session so
        # every persisted timestamp (speaking events AND join/leave
        # events) shares the same time-zero. The pre-snapshot design
        # let early events use _start_time while later events used
        # first_audio_time, which produced a backward time-step at the
        # transition that corrupted the downstream diarization
        # DataFrame .
        self._audio_anchor_snapshot = 0.0
        self._participants: list[str] = []
        self._participants_detailed: list[dict] = []
        self._self_user_id: Optional[str] = None
        self._events: list[dict] = []
        self._events_lock = threading.Lock()
        self._speaking_events: list[dict] = []
        self._speaking_lock = threading.Lock()
        self._speaking_subscribed = False
        self._voice_channel_id: Optional[str] = None
        self._base_name = ""

    def start(self) -> list[str]:
        # Query the current voice channel once so we know which channel to
        # subscribe to and so participants are populated atomically with the
        # SPEAKING_* subscriptions (no race against a join we missed).
        ch = self.rpc.get_voice_channel()
        if not ch:
            raise RuntimeError(
                "No participants found. Make sure you're joined to a Discord voice channel."
            )
        self._voice_channel_id = ch.get("id")
        if not self._voice_channel_id:
            raise RuntimeError("Discord returned a voice channel with no id field")
        detailed: list[dict] = []
        for vs in ch.get("voice_states", []):
            user = vs.get("user") or {}
            uid = user.get("id")
            if not uid:
                continue
            name = user.get("global_name") or user.get("username") or "Unknown"
            detailed.append({"id": uid, "name": name})
        if not detailed:
            raise RuntimeError(
                "Voice channel found but no participants reported. "
                "Try Refresh Participants and Start Recording again."
            )
        self._participants_detailed = detailed
        self._participants = [p["name"] for p in detailed]
        self._self_user_id = (self.rpc.identity or {}).get("id")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{self.meeting_name}_" if self.meeting_name else ""
        self._base_name = f"{prefix}discord_{ts}"
        self._events = [
            {
                "timestamp": 0.0,
                "event": "present",
                "id": p["id"],
                "username": p["name"],
            }
            for p in self._participants_detailed
        ]

        try:
            self.audio.start()
        except Exception:
            # AudioRecorder.start() already cleans up internally on raise
            # (closes streams, terminates PortAudio). We do not need to
            # call stop() here — and doing so would raise RuntimeError
            # because _running is False after a failed start.
            raise

        # Take the start timestamp as close as possible to the moment audio
        # capture is actually running so speaking-event offsets line up with
        # the audio timeline. Setting _start_time BEFORE audio.start() (as
        # the previous revision did) systematically shifted every label
        # earlier by the device-open latency (commonly 100-500 ms on
        # WASAPI), which produced visibly misaligned speaker boundaries
        # for short utterances at session start.
        self._start_time = time.time()

        # Snapshot the audio-anchor ONCE before subscribing to speaking
        # events. Wait up to _ANCHOR_WAIT_SEC for the capture thread to
        # deliver the first sample (typical WASAPI device-open latency
        # is 100-500 ms; we give ourselves 1 s of slack). The snapshot
        # is frozen for the rest of the session: every event uses the
        # same time-zero regardless of when it fires relative to the
        # first audio sample. This eliminates the backward time-step
        # between events recorded before vs after first_audio_time was
        # set that the pre-snapshot _audio_anchor() produced (in
        # ).
        self._audio_anchor_snapshot = self._snapshot_audio_anchor()

        # Subscribe to per-user speaking events for this channel. These give
        # us ground-truth "who spoke when" timestamps without needing
        # diarization heuristics. The callbacks run on the RPC reader thread
        # — keep them tiny.
        #
        # The subscriptions must succeed as a PAIR. If SPEAKING_START
        # succeeds and SPEAKING_STOP fails, we'd record opens with no
        # matching closes — every interval would stretch to audio end,
        # which is worse "ground truth" than the heuristic fallback. So we
        # roll back START on STOP failure and treat the whole session as
        # not-subscribed, letting transcription use the pyannote fallback.
        self._speaking_subscribed = False
        args = {"channel_id": self._voice_channel_id}
        try:
            self.rpc.subscribe("SPEAKING_START", args, self._on_speaking_start)
        except Exception as e:
            log.warning("SPEAKING_START subscribe failed (%s). Falling back to pyannote.", e)
        else:
            try:
                self.rpc.subscribe("SPEAKING_STOP", args, self._on_speaking_stop)
            except Exception as e:
                log.warning(
                    "SPEAKING_STOP subscribe failed (%s); rolling back SPEAKING_START. "
                    "Falling back to pyannote.", e,
                )
                try:
                    self.rpc.unsubscribe("SPEAKING_START", args,
                                         callback=self._on_speaking_start)
                except Exception:
                    pass
            else:
                self._speaking_subscribed = True
                log.info(
                    "Subscribed to SPEAKING_START/STOP for channel %s",
                    self._voice_channel_id,
                )

        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        return list(self._participants)

    # ---- speaking-event callbacks ----

    _ANCHOR_WAIT_SEC = 1.0
    _ANCHOR_POLL_SEC = 0.01

    def _snapshot_audio_anchor(self) -> float:
        """Wait briefly for the first audio sample, then return the
        anchor for the rest of the session.

        Returns `audio.first_audio_time` if the capture thread has
        delivered its first sample within `_ANCHOR_WAIT_SEC`; otherwise
        falls back to `_start_time`. Direct attribute access — no
        getattr default — so a refactor that renames or removes
        `AudioRecorder.first_audio_time` raises `AttributeError`
        instead of silently falling back to the pre-fix behavior
        .
        """
        deadline = self._start_time + self._ANCHOR_WAIT_SEC
        while self.audio.first_audio_time is None and time.time() < deadline:
            time.sleep(self._ANCHOR_POLL_SEC)
        first = self.audio.first_audio_time
        if first is not None:
            return first
        log.warning(
            "Audio capture did not deliver a sample within %.1fs of "
            "audio.start() — falling back to wall-clock session start "
            "as anchor. Speaking-event timestamps may be offset by the "
            "actual device-open latency.",
            self._ANCHOR_WAIT_SEC,
        )
        return self._start_time

    def _audio_anchor(self) -> float:
        """The session's frozen audio-timeline anchor.

        Returns the value snapshotted in `start()` via
        `_snapshot_audio_anchor()`. Every persisted timestamp in the
        session (speaking events AND join/leave events) is computed as
        `time.time() - _audio_anchor()`, so they all share the same
        time-zero.
        """
        return self._audio_anchor_snapshot

    def _on_speaking_start(self, data: dict) -> None:
        if not self._running:
            return
        t = time.time() - self._audio_anchor()
        with self._speaking_lock:
            self._speaking_events.append({
                "timestamp": t,
                "event": "speaking_start",
                "user_id": data.get("user_id"),
            })

    def _on_speaking_stop(self, data: dict) -> None:
        if not self._running:
            return
        t = time.time() - self._audio_anchor()
        with self._speaking_lock:
            self._speaking_events.append({
                "timestamp": t,
                "event": "speaking_stop",
                "user_id": data.get("user_id"),
            })

    def _monitor_loop(self) -> None:
        # Track previous state by Discord user ID, not display name. Names are
        # mutable (people can change global_name mid-call) and not unique
        # (two users can share a display name), so name-based diffing can
        # generate spurious joined/left events.
        previous: dict[str, str] = {p["id"]: p["name"] for p in self._participants_detailed}
        while self._running:
            time.sleep(self.POLL_INTERVAL_SEC)
            if not self._running:
                break
            try:
                detailed = self.rpc.get_participants_detailed()
            except Exception as e:
                log.warning("Participant poll failed: %s", e)
                continue
            current: dict[str, str] = {p["id"]: p["name"] for p in detailed}
            # Use the audio anchor — same timeline as speaking events —
            # so the join/leave timestamps in participants.json are
            # directly comparable to the speaking-event timestamps in
            # the same file. With _start_time the two timelines drifted
            # by the device-open latency.
            t = time.time() - self._audio_anchor()

            with self._events_lock:
                # Joins: present now, not before.
                for uid, name in current.items():
                    if uid not in previous:
                        self._events.append({
                            "timestamp": t,
                            "event": "joined",
                            "id": uid,
                            "username": name,
                        })
                        log.info("[%.1fs] %s joined", t, name)

                # Leaves: present before, not now.
                for uid, name in previous.items():
                    if uid not in current:
                        self._events.append({
                            "timestamp": t,
                            "event": "left",
                            "id": uid,
                            "username": name,
                        })
                        log.info("[%.1fs] %s left", t, name)

            previous = current

    def stop(self) -> RecordingResult:
        if not self._running:
            raise RuntimeError("Session not running")
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=3)

        # Be polite — unsubscribe so further DISPATCHes don't pile up on the
        # reader thread after we're done. Failures here are non-fatal.
        if self._voice_channel_id and self._speaking_subscribed:
            args = {"channel_id": self._voice_channel_id}
            for evt_name, cb in (
                ("SPEAKING_START", self._on_speaking_start),
                ("SPEAKING_STOP", self._on_speaking_stop),
            ):
                try:
                    self.rpc.unsubscribe(evt_name, args, callback=cb)
                except Exception:
                    pass

        # Snapshot the metadata FIRST, under the locks, then write the
        # participants.json BEFORE calling audio.stop(). If audio.stop()
        # raises (e.g. a mid-recording capture error surfacing via
        # _capture_error), the participant list and speaking-event
        # timeline are already persisted to disk — the user still has the
        # evidentiary metadata even though the WAV is lost. The previous
        # order discarded both.
        audio_path = self.output_dir / f"{self._base_name}_audio.wav"

        with self._speaking_lock:
            # Only surface speaking events if BOTH subscriptions were live;
            # otherwise the data is half-truth (starts without stops, or
            # vice versa) and would mislead the transcriber.
            speaking_events = list(self._speaking_events) if self._speaking_subscribed else []

        with self._events_lock:
            events_snapshot = list(self._events)

        participants_path = self.output_dir / f"{self._base_name}_participants.json"
        participants_path.write_text(
            json.dumps(
                {
                    "initial_participants": self._participants,
                    "participants_detailed": self._participants_detailed,
                    "self_user_id": self._self_user_id,
                    "voice_channel_id": self._voice_channel_id,
                    "events": events_snapshot,
                    "speaking_events": speaking_events,
                    "speaking_events_complete": self._speaking_subscribed,
                    "audio_layout": self.audio.audio_layout(),
                    "consent": self.consent_record,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        self.audio.stop(audio_path)
        if self._speaking_subscribed:
            log.info(
                "Recorded %d speaking events from %d distinct users",
                len(speaking_events),
                len({e.get("user_id") for e in speaking_events if e.get("user_id")}),
            )
        else:
            log.info(
                "Speaking-event subscription was incomplete; falling back to "
                "diarization at transcribe time."
            )

        return RecordingResult(
            audio_path=audio_path,
            participants_path=participants_path,
            initial_participants=list(self._participants),
            events=events_snapshot,
        )
