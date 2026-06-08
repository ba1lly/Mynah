"""WhisperX transcription + speaker-to-username mapping."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

# Same scrubbing policy as gui._scrub. Lifted here so transcript output is
# protected from log-injection / line-break spoofing via Discord display
# names. Keep in sync with gui._BAD_LOG_CHARS.
_BAD_TRANSCRIPT_CHARS = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f"
    r"\x80-\x9f"
    r"\u200b-\u200d"
    r"\u200e\u200f"
    r"\u2028\u2029"
    r"\u202a-\u202e"
    r"\u2060"
    r"\u2066-\u2069"
    r"\ufeff]"
)


def _split_segment_by_word_speakers(
    seg: dict, seg_fallback_speaker: str,
) -> "list[tuple[str, float, str]]":
    """Split one Whisper segment into per-speaker sub-blocks.

    Returns a list of ``(speaker, start_time, text)`` tuples — one per
    run of consecutive words that share the same speaker label. Used
    by the transcript writer to avoid the "one segment, many speakers"
    flattening problem: WhisperX puts per-word speaker labels under
    ``seg["words"][i]["speaker"]`` after ``assign_word_speakers``, but
    the segment-level ``seg["speaker"]`` collapses those into one
    label, so any speaker change inside a 10–30s Whisper segment gets
    misattributed.

    Fallbacks (each preserves the segment so the transcript line still
    makes sense, just with the coarse label we had before this split):

    - No ``words`` field, or empty / non-list: one block using
      ``seg_fallback_speaker`` + ``seg["text"]`` + ``seg["start"]``.
    - A word lacks a ``speaker`` field: inherit the previous word's
      speaker; if there is no previous word, use
      ``seg_fallback_speaker``.
    - Words have ``speaker`` but no ``start``: use ``seg["start"]`` for
      the first sub-block and keep the prior block's end-time as the
      next sub-block's start (Whisper's segment-level timestamps are
      always present even when word alignment was skipped).
    """
    seg_start = float(seg.get("start") or 0.0)
    seg_text = (seg.get("text") or "").strip()

    words = seg.get("words")
    if not isinstance(words, list) or not words:
        return [(seg_fallback_speaker, seg_start, seg_text)]

    blocks: list[tuple[str, float, str]] = []
    cur_speaker: str = ""
    cur_start: float = seg_start
    cur_words: list[str] = []
    last_known_speaker: str = seg_fallback_speaker

    def flush() -> None:
        if not cur_words:
            return
        # whisperx.align() returns bare word tokens (no leading space);
        # raw faster-whisper / Whisper segments include a leading space
        # on each non-initial word. Handle both: concatenate verbatim
        # first (preserving any built-in spaces), then normalise runs
        # of whitespace so accidental " ,"-style artefacts collapse.
        text = "".join(cur_words)
        # If concatenation produced no internal whitespace at all but
        # there are multiple tokens, the words must have been bare —
        # rejoin with single spaces so the output is readable.
        if len(cur_words) > 1 and not any(
            t and (t[0].isspace() or t[-1].isspace()) for t in cur_words
        ):
            text = " ".join(t for t in cur_words if t)
        text = " ".join(text.split())  # collapse any runs of whitespace
        if not text:
            return
        blocks.append((cur_speaker or seg_fallback_speaker, cur_start, text))

    for i, w in enumerate(words):
        if not isinstance(w, dict):
            continue
        speaker = w.get("speaker") or last_known_speaker
        last_known_speaker = speaker
        # Whisper word entries usually store the surface form under "word"
        # and include the leading space — preserve it verbatim so the
        # joined text matches the original tokenisation.
        token = w.get("word") if "word" in w else w.get("text")
        if token is None:
            continue
        if speaker != cur_speaker:
            flush()
            cur_speaker = speaker
            # Prefer the word's own start timestamp when available so
            # the block's timecode lines up with the actual transition,
            # not the segment start.
            word_start = w.get("start")
            cur_start = (
                float(word_start)
                if word_start is not None
                else (blocks[-1][1] if blocks else seg_start)
            )
            cur_words = [str(token)]
        else:
            cur_words.append(str(token))

    flush()

    if not blocks:
        # All words were malformed; fall back to the segment-level block
        # so the transcript line isn't silently dropped.
        return [(seg_fallback_speaker, seg_start, seg_text)]
    return blocks


def _scrub_name(s: object) -> str:
    """Make a Discord-supplied name safe to write into the transcript.

    Discord display names are attacker-controllable: a name containing a
    literal newline followed by '[CEO]' would otherwise inject a fake
    speaker line into the produced *_transcript.txt. We strip newlines /
    bracket-shaped separators / Unicode line separators / bidi controls so
    each speaker label stays on its own single rendered line.
    """
    if not isinstance(s, str):
        s = str(s) if s is not None else "UNKNOWN"
    safe = _BAD_TRANSCRIPT_CHARS.sub("?", s).replace("\r", " ").replace("\n", " ")
    # Brackets close our own [speaker] marker. Strip them so a name like
    # 'Alice]\n[CEO' (newline already neutralised above) cannot end the
    # bracket early either.
    safe = safe.replace("[", "(").replace("]", ")")
    return safe

import numpy as np

log = logging.getLogger(__name__)


# Allow-list for the whisper model name. Accepts a known set of upstream
# names plus an explicit `org/repo` HuggingFace pattern so power users can
# point at their own fine-tunes without us silently shelling absolute paths
# or commands into whisperx → faster-whisper → huggingface_hub.
_KNOWN_WHISPER_MODELS = {
    "tiny", "tiny.en",
    "base", "base.en",
    "small", "small.en",
    "medium", "medium.en",
    "large", "large-v1", "large-v2", "large-v3", "large-v3-turbo",
    "distil-small.en", "distil-medium.en",
    "distil-large-v2", "distil-large-v3",
}
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_model_name(model_name: str) -> None:
    """Reject obviously dangerous model names before passing them to whisperx.

    `whisperx.load_model()` accepts both bare model names (resolved via
    faster-whisper's own table) and HuggingFace repo IDs. Without any
    validation, a tampered config.json could point at an attacker-controlled
    HF repo. We don't claim to fully prevent supply-chain abuse — that needs
    pinning and signature verification — but we DO refuse local filesystem
    paths and `..` traversal, which are the obviously-wrong cases.
    """
    if not isinstance(model_name, str) or not model_name.strip():
        raise RuntimeError("Whisper model name is empty")
    name = model_name.strip()
    if name in _KNOWN_WHISPER_MODELS:
        return
    if _HF_REPO_RE.match(name):
        log.info("Using custom HuggingFace whisper model: %s", name)
        return
    raise RuntimeError(
        f"Unsupported Whisper model name {model_name!r}. "
        f"Expected one of {sorted(_KNOWN_WHISPER_MODELS)} or an HF 'org/repo' identifier."
    )


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _ensure_ffmpeg_on_path() -> None:
    """WhisperX shells out to a binary literally named ``ffmpeg`` (or
    ``ffmpeg.exe``). The imageio-ffmpeg package bundles a Windows binary, but
    it's named e.g. ``ffmpeg-win-x86_64-v7.0.2.exe`` — so we copy it once to a
    cache directory as ``ffmpeg.exe`` and put that directory on PATH.

    Called LAZILY from `transcribe()`, not at module import time. Importing
    this module used to mutate `os.environ['PATH']` and write files into
    `%LOCALAPPDATA%` as a side effect, which made the module unsafe for
    capability checks (e.g. `whisperx_available()`) and added a Windows
    binary-planting primitive to anyone with write access to the cache dir.

    Integrity is checked by SHA-256, not file size, so a same-size planted
    binary will be detected and replaced.

    Idempotent: subsequent calls are no-ops once the canonical copy exists.
    """
    if sys.platform != "win32":
        # On Linux/macOS we don't ship a bundled ffmpeg; rely on the system one.
        return

    import shutil

    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError:
        log.warning("imageio-ffmpeg not installed. Transcription will fail.")
        return

    try:
        src = Path(get_ffmpeg_exe())
    except Exception as e:
        log.warning("Could not locate bundled ffmpeg (%s).", e)
        return

    cache_root = Path(
        os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    ) / "Mynah" / "ffmpeg"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("Could not create ffmpeg cache dir (%s); transcription may fail.", e)
        return

    # Best-effort: restrict the cache directory ACL to the current user
    # only. Default Windows ACLs inherit from %LOCALAPPDATA% (already
    # per-user), so this is defense-in-depth — a same-user attacker can
    # still write here, but cross-user planting is blocked even if the
    # parent ACL is misconfigured. icacls is shipped with every modern
    # Windows install.
    _lock_down_cache_dir(cache_root)

    target = cache_root / "ffmpeg.exe"
    src_hash = _sha256(src)

    needs_copy = True
    if target.exists():
        try:
            if _sha256(target) == src_hash:
                needs_copy = False
            else:
                log.info("Cached ffmpeg.exe SHA differs from bundled; replacing.")
        except Exception as e:
            log.warning("Could not hash cached ffmpeg (%s); replacing.", e)

    if needs_copy:
        try:
            shutil.copyfile(src, target)
            log.info("Cached ffmpeg.exe at %s", target)
        except Exception as e:
            log.warning("Could not stage ffmpeg.exe (%s). Transcription may fail.", e)
            return

    # Re-verify the hash IMMEDIATELY before we trust the cache directory
    # enough to put it on PATH. Narrows the TOCTOU window between
    # initial verify and PATH-add to milliseconds, so a same-user attacker
    # has to win a much tighter race to plant a binary. Cannot fully
    # close the window because whisperx execs ffmpeg by PATH lookup at
    # an unpredictable later moment.
    try:
        if _sha256(target) != src_hash:
            log.warning(
                "Cached ffmpeg.exe failed re-verification; refusing to add to PATH."
            )
            return
    except Exception as e:
        log.warning("Could not re-verify cached ffmpeg (%s); refusing PATH add.", e)
        return

    sep = os.pathsep
    parts = os.environ.get("PATH", "").split(sep)
    cache_str = str(cache_root)
    if cache_str not in parts:
        os.environ["PATH"] = cache_str + sep + os.environ.get("PATH", "")
        log.info("Added ffmpeg to PATH: %s", cache_str)


def _lock_down_cache_dir(path: Path) -> None:
    """Restrict the ffmpeg cache directory ACL on Windows."""
    if sys.platform != "win32":
        return
    import subprocess
    try:
        # /inheritance:r removes inherited ACEs; /grant:r %USERNAME%:(OI)(CI)F
        # grants only the current user full access (object-inherit +
        # container-inherit). Silent on success.
        subprocess.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant:r", f"{os.environ.get('USERNAME', '')}:(OI)(CI)F"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except Exception as e:
        log.debug("icacls lockdown of %s failed (%s); continuing", path, e)


def whisperx_available() -> bool:
    try:
        import whisperx  # noqa: F401
        return True
    except ImportError:
        return False


def _merge_late_joiners(
    participants_detailed: list[dict],
    initial_participants: list[str],
    events: list[dict],
) -> list[dict]:
    """Return the participant roster extended with anyone who joined mid-call.

    Order = first appearance (initial roster first, then joiners by event
    timestamp). Identity is keyed by Discord user ID when available.

    Anyone who left the channel during recording is still kept in the pool —
    they spoke (or might have spoken) before leaving, and their cluster still
    needs a name.

    Dedup rules:
      - Seed (initial roster) is preserved verbatim, including duplicate
        display names — two distinct users called "Alex" each occupy a
        slot, and dropping one would corrupt the auto-map.
      - Joiners are dedup'd against the seen IDs (always) and, when the
        joiner event has no ID, against already-seen names. The no-ID joiner
        case is genuinely ambiguous (same person rejoining vs new person
        with the same name); we err on the conservative side and skip,
        which is no worse than the pre-PR behaviour of ignoring joiners
        entirely.
    """
    result: list[dict] = []
    seen_ids: set[str] = set()
    seen_names_without_id: set[str] = set()

    # --- Seed: preserve the initial roster verbatim ---------------------------
    if participants_detailed:
        for entry in participants_detailed:
            uid = entry.get("id")
            name = entry.get("name") or entry.get("username") or "Unknown"
            result.append({"id": uid, "name": name})
            if uid is not None:
                seen_ids.add(str(uid))
            else:
                seen_names_without_id.add(name)
    else:
        # Old recording: no detailed list, just the bare names.
        for name in initial_participants:
            result.append({"id": None, "name": name})
            seen_names_without_id.add(name)

    # --- Add joiners ----------------------------------------------------------
    for ev in events:
        if ev.get("event") != "joined":
            continue
        uid = ev.get("id")
        name = ev.get("username") or ev.get("name") or "Unknown"
        if uid is not None:
            if str(uid) in seen_ids:
                continue
            seen_ids.add(str(uid))
        else:
            if name in seen_names_without_id:
                continue
            seen_names_without_id.add(name)
        result.append({"id": uid, "name": name})

    return result


def _filter_events_by_layout(
    events: list[dict],
    audio_layout: Optional[str],
    self_user_id: Optional[str],
) -> list[dict]:
    """Drop speaking events that can't possibly be in the captured audio.

    Discord SPEAKING_* events are emitted for everyone in the voice
    channel regardless of which streams we actually recorded. Applying
    every event to the audio timeline is correct only for `mixed` /
    `channel_split` layouts; for `mic_only` / `system_only` it
    silently mislabels speech with names whose audio was never captured.

      - mic_only:    keep only events for self_user_id.
      - system_only: keep only events for OTHER user_ids.
      - other:       pass through.

    Returning an empty list lets the caller fall back to acoustic
    diarization, which is the right behavior when our gate cannot safely
    apply — most notably mic_only / system_only without a known
    self_user_id (we cannot tell which events are local-user, so
    fabricating labels from raw events would mislabel; acoustic
    diarization is strictly safer).

    Events with missing `user_id` are dropped up front in every mode:
    they can never be mapped to a speaker downstream
    (`_speaking_events_to_diarize_df` skips them), and counting them in
    the caller's progress log misled users on event totals.
    """
    if not events:
        return events
    events = [e for e in events if e.get("user_id") is not None]
    if not events:
        return events
    if audio_layout == "mic_only":
        if not self_user_id:
            return []
        return [e for e in events if str(e.get("user_id")) == str(self_user_id)]
    if audio_layout == "system_only":
        if not self_user_id:
            return []
        return [e for e in events if str(e.get("user_id")) != str(self_user_id)]
    return events


def _speaking_events_to_diarize_df(
    speaking_events: list[dict],
    participants_detailed: list[dict],
    audio_seconds: float,
):
    """Convert Discord SPEAKING_START / SPEAKING_STOP events into a DataFrame
    compatible with whisperx.assign_word_speakers.

    Pair each SPEAKING_START with the matching SPEAKING_STOP for the same
    user_id. Any dangling SPEAKING_START gets closed at audio_seconds (the
    end of the recording).

    The DataFrame puts the participant's DISPLAY NAME (not user_id) in the
    `speaker` column so the result of assign_word_speakers is already
    properly labelled — no SPEAKER_NN → name auto-mapping step needed.
    """
    import pandas as pd  # transitive dep via pyannote

    # Skip roster entries with no Discord ID. Their key would stringify to
    # the literal "None" which can't match any real Discord snowflake; the
    # speaker would silently fall through to `user:None` instead of by
    # name. (Real Discord events always carry a string snowflake.)
    id_to_name: dict[str, str] = {}
    for p in participants_detailed:
        pid = p.get("id")
        if pid is None:
            continue
        id_to_name[str(pid)] = p.get("name") or "Unknown"

    open_starts: dict[str, float] = {}
    intervals: list[tuple[float, float, str]] = []

    for ev in speaking_events:
        uid = ev.get("user_id")
        if uid is None:
            continue
        uid = str(uid)
        t = float(ev.get("timestamp", 0.0))
        kind = ev.get("event")
        if kind == "speaking_start":
            # Discord can fire repeated starts without an intervening stop
            # (clip the previous interval at this point).
            if uid in open_starts:
                intervals.append((open_starts[uid], t, uid))
            open_starts[uid] = t
        elif kind == "speaking_stop":
            start = open_starts.pop(uid, None)
            if start is None:
                # Unmatched stop: the user was already speaking when our
                # subscription started, so we never saw their SPEAKING_START.
                # Treat the speech as having been going since recording
                # start (0.0). Without this, the opening interval is lost
                # and those early Whisper segments fall through as UNKNOWN.
                start = 0.0
            if t > start:
                intervals.append((start, t, uid))

    # Close any still-open starts at the end of the recording.
    for uid, start in open_starts.items():
        if audio_seconds > start:
            intervals.append((start, audio_seconds, uid))

    if not intervals:
        return None

    rows = []
    for start, end, uid in sorted(intervals, key=lambda x: x[0]):
        name = id_to_name.get(uid)
        if not name:
            # User who spoke but isn't in participants_detailed — shouldn't
            # happen for current-channel events, but fall back to user_id so
            # the user can spot them and fix mapping if needed.
            name = f"user:{uid}"
        rows.append({"start": start, "end": end, "speaker": name})

    df = pd.DataFrame(rows, columns=["start", "end", "speaker"])
    log.info(
        "Built %d speaking intervals for %d distinct user(s) from Discord events",
        len(df), df["speaker"].nunique(),
    )
    return df


def _resolve_user_and_others(
    participants_detailed: list[dict],
    participants: list[str],
    self_user_id: Optional[str],
) -> tuple[Optional[str], list[str]]:
    """Find which participant is the authenticated user and the ordered list
    of everyone else.

    Match by Discord user ID rather than name/position. Display names can
    collide (two people called "Alex" in the same channel); filtering with a
    value comparison would strip both, leaving the diarizer's cluster for the
    non-user duplicate unmapped. Using the ID + index keeps duplicates intact.

    Returns (user_name, others). user_name is None when we can't resolve the
    local user, in which case the caller handles the fallback.
    """
    if not self_user_id or not participants_detailed:
        return None, list(participants)
    user_name: Optional[str] = None
    others: list[str] = []
    matched = False
    for entry in participants_detailed:
        name = entry.get("name") or "Unknown"
        if not matched and str(entry.get("id")) == str(self_user_id):
            user_name = name
            matched = True
            continue
        others.append(name)
    if not matched:
        return None, list(participants)
    return user_name, others


def _load_channels_if_split(audio_path: Path, layout: Optional[str]):
    """If the WAV is in channel-split layout (L=mic, R=loopback), return
    (True, left_float32, right_float32). Otherwise return (False, None, None).

    Both channels are returned at 16 kHz mono float32, the same rate pyannote
    expects, so callers can hand them straight to the diarization pipeline.
    """
    if layout != "channel_split":
        return False, None, None
    try:
        import soundfile as sf  # type: ignore
    except ImportError:
        log.error(
            "audio_layout=channel_split but `soundfile` is not installed; "
            "channel-aware diarization is DISABLED for this transcription. "
            "Install it with: pip install soundfile"
        )
        return False, None, None
    try:
        from scipy.signal import resample_poly
        data, sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
        if data.shape[1] < 2:
            log.warning("audio_layout=channel_split but file is mono; falling back")
            return False, None, None
        left = data[:, 0]
        right = data[:, 1]
        # If the two channels are essentially identical, this isn't really a
        # split — probably an old recording before this code landed.
        if np.allclose(left, right, atol=1e-3):
            log.info("Channel-split layout claimed but channels are identical; "
                     "falling back to standard diarization")
            return False, None, None
        if sr != 16000:
            from fractions import Fraction
            ratio = Fraction(16000, sr).limit_denominator(1000)
            left = resample_poly(left, ratio.numerator, ratio.denominator)
            right = resample_poly(right, ratio.numerator, ratio.denominator)
        return True, left.astype(np.float32), right.astype(np.float32)
    except Exception as e:
        log.warning("Could not load stereo channels (%s); falling back", e)
        return False, None, None


CHANNEL_ENERGY_DETECT_WIN_SEC = 0.25
CHANNEL_ENERGY_ACTIVE_FRACTION = 0.55
CHANNEL_ENERGY_NOISE_MULTIPLIER = 5.0
CHANNEL_ENERGY_ABSOLUTE_FLOOR = 2e-3


def _attach_user_via_channel_energy(
    segments: list[dict],
    left: "np.ndarray",
    right: "np.ndarray",
    user_name: str,
    sample_rate: int = 16000,
) -> None:
    """For each Whisper segment, decide whether the local user spoke by
    looking at sub-second voice activity on the LEFT (mic) channel.

    Whisper segments span 10-30s and often contain several alternating
    speakers. A single segment-level RMS check labels the whole thing as
    the user even when the user only chimed in for 3 seconds at the start
    and someone else dominated the rest. We avoid this by chopping each
    segment into 250 ms windows and only claiming a segment as the user
    when the user has voice activity in MOST of those windows.

    Comparing L to R doesn't work in practice: Discord plays back 1..N
    other voices through the system mixer, so R is typically several times
    louder than L throughout the whole call. The mic channel is the only
    one that contains the user's voice though, so a voice-activity test
    on L alone (vs its own quiet noise floor) is what we use.
    """
    n = min(len(left), len(right))
    if n == 0:
        return

    # Estimate the mic's quiet noise floor from 1-second windows. The 20th
    # percentile gives a stable estimate even if 70-80% of the audio is
    # speech, which is common in a recorded meeting.
    floor_win = sample_rate
    n_floor_wins = len(left) // floor_win
    if n_floor_wins >= 5:
        rms_per_win = np.sqrt(
            np.mean(
                left[: n_floor_wins * floor_win]
                .reshape(n_floor_wins, floor_win)
                .astype(np.float64) ** 2,
                axis=1,
            )
            + 1e-12
        )
        noise_floor = float(np.percentile(rms_per_win, 20))
        # All-speech detection . If even the
        # 80th-percentile per-window RMS is above the floor-times-
        # multiplier threshold, the recording almost certainly contains
        # speech in every window and there is no quiet sample to estimate
        # noise from. In that case `noise_floor` IS the speech RMS and
        # `noise_floor * 5` produces a threshold ABOVE the speech signal
        # itself, causing the user to never be marked — the exact failure
        # mode the previous fix for #23 left uncovered for the typical
        # meeting-recording duration (≥5 s). Fall back to the absolute
        # floor — the same threshold the short-clip branch uses for the
        # symmetric "no clean noise sample" case.
        p80 = float(np.percentile(rms_per_win, 80))
        if p80 > CHANNEL_ENERGY_ABSOLUTE_FLOOR * CHANNEL_ENERGY_NOISE_MULTIPLIER:
            log.info(
                "Mic noise floor estimate: 80th-percentile RMS=%.5f exceeds "
                "absolute floor × multiplier (%.5f); recording is likely "
                "all-speech with no quiet sample. Falling back to absolute "
                "floor threshold.",
                p80,
                CHANNEL_ENERGY_ABSOLUTE_FLOOR * CHANNEL_ENERGY_NOISE_MULTIPLIER,
            )
            threshold = CHANNEL_ENERGY_ABSOLUTE_FLOOR
        else:
            threshold = max(
                noise_floor * CHANNEL_ENERGY_NOISE_MULTIPLIER,
                CHANNEL_ENERGY_ABSOLUTE_FLOOR,
            )
    else:
        # Recordings shorter than 5 noise-floor windows give us no clean
        # noise sample. The previous fallback was 0.2 × RMS(whole-clip),
        # but when the user spoke through the entire clip RMS(whole-clip)
        # IS the speech level, so 0.2× × 5 (the noise multiplier) put the
        # threshold ABOVE the actual signal and the user was never
        # marked. Use the absolute floor directly — it is tuned for
        # quiet bleed-through and is independent of the signal we are
        # trying to detect.
        noise_floor = 0.0
        threshold = CHANNEL_ENERGY_ABSOLUTE_FLOOR
        log.info(
            "Mic noise floor: clip too short (%d wins < 5) for percentile estimate; "
            "using fixed absolute threshold.", n_floor_wins,
        )

    detect_win = int(sample_rate * CHANNEL_ENERGY_DETECT_WIN_SEC)
    active_fraction_threshold = CHANNEL_ENERGY_ACTIVE_FRACTION

    log.info(
        "Mic noise floor=%.5f, speech threshold=%.5f, active-fraction cut=%.0f%%",
        noise_floor, threshold, active_fraction_threshold * 100,
    )

    marked = 0
    for seg in segments:
        s = int(max(0, seg.get("start", 0)) * sample_rate)
        e = int(max(s + 1, seg.get("end", 0) * sample_rate))
        if e > n:
            e = n
        if s >= e:
            continue

        # Count 250 ms windows in this segment where the mic was active.
        n_wins = max(1, (e - s) // detect_win)
        active = 0
        for w in range(n_wins):
            ws = s + w * detect_win
            we = min(ws + detect_win, e)
            if we <= ws:
                continue
            l_rms = float(np.sqrt(np.mean(left[ws:we] ** 2)) + 1e-9)
            if l_rms > threshold:
                active += 1

        if active / n_wins >= active_fraction_threshold:
            # Structured pre-assignment (issue #19): write the display
            # name directly to `speaker` and tag the segment with a
            # typed boolean. The previous `__USER__:<name>` string
            # sentinel was an internal protocol between this function
            # and `_auto_map` that leaked through to a `.split(":", 1)`
            # in transcribe() and a `.startswith("__")` filter in the
            # mapping JSON writer — every downstream consumer had to
            # know the encoding to handle it. With the boolean, the
            # protocol is explicit and the speaker string is already
            # the value we want in the transcript.
            seg["speaker"] = user_name
            seg["is_local_user"] = True
            marked += 1

    log.info(
        "Channel-energy: marked %d/%d segments as %s",
        marked, len(segments), user_name,
    )


def _resolve_diarization_api(whisperx_mod):
    """WhisperX moved DiarizationPipeline + assign_word_speakers from module
    root (<3.x) to whisperx.diarize (3.x+). Try the new location first."""
    try:
        from whisperx.diarize import DiarizationPipeline, assign_word_speakers
        return DiarizationPipeline, assign_word_speakers
    except ImportError:
        pass
    # Old API (pre-3.x)
    return whisperx_mod.DiarizationPipeline, whisperx_mod.assign_word_speakers


def _format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _auto_map(segments: list[dict], participants: list[str]) -> tuple[list[dict], set[str]]:
    """Map SPEAKER_NN labels to participant usernames by order of first speech.

    Whatever labels are unmapped after best-effort matching are returned in
    `unmapped`. We map by speaking order for as many speakers/participants as
    overlap; any extras keep their SPEAKER_NN tag and are listed for manual
    correction.

    Segments tagged with `is_local_user=True` (issue #19: structured
    replacement for the legacy `__USER__:<name>` string sentinel) are
    skipped — they were already assigned to the local user by
    `_attach_user_via_channel_energy` and do not participate in the
    speaker-cluster numbering.
    """
    order: list[str] = []
    speakers: set[str] = set()
    for seg in segments:
        if seg.get("is_local_user"):
            continue
        sp = seg.get("speaker")
        if sp:
            speakers.add(sp)
            if sp not in order:
                order.append(sp)

    n = min(len(order), len(participants))
    mapping = {order[i]: participants[i] for i in range(n)}
    unmapped = {sp for sp in speakers if sp not in mapping}

    if mapping:
        log.info(
            "Auto-mapped %d/%d speakers by speaking order (%d participant(s), %d cluster(s))",
            len(mapping), max(len(order), len(participants)), len(participants), len(order),
        )
    if unmapped:
        log.warning("Could not map: %s", sorted(unmapped))

    for seg in segments:
        original = seg.get("speaker", "UNKNOWN")
        seg["original_speaker"] = original
        if not seg.get("is_local_user"):
            seg["speaker"] = mapping.get(original, original)
    return segments, unmapped


def transcribe(
    audio_path: Path,
    participants_path: Path,
    hf_token: Optional[str] = None,
    model_name: str = "large-v3-turbo",
    progress=None,
) -> Path:
    """Transcribe audio_path, map speakers using participants_path, write transcript.

    progress: optional callable(str) for status updates.
    Returns the path to the written transcript .txt.
    """
    if not whisperx_available():
        raise RuntimeError(
            "WhisperX is not installed. Run: "
            "pip install git+https://github.com/m-bain/whisperx.git"
        )
    _validate_model_name(model_name)
    _ensure_ffmpeg_on_path()

    audio_path = Path(audio_path)
    participants_path = Path(participants_path)

    import torch
    import whisperx

    def _p(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    try:
        participant_data = json.loads(participants_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError(f"Participants file not found: {participants_path}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise RuntimeError(
            f"Participants file is not valid JSON ({participants_path}): {e}"
        ) from e
    audio_layout = participant_data.get("audio_layout")  # absent for old recordings
    self_user_id = participant_data.get("self_user_id")
    participants_detailed = participant_data.get("participants_detailed") or []
    events = participant_data.get("events") or []
    speaking_events = participant_data.get("speaking_events") or []
    # Recorder writes this True iff BOTH SPEAKING_START and SPEAKING_STOP
    # subscriptions were live. Older recordings predate the field; if it's
    # missing we trust the events on the assumption that the recorder which
    # wrote them didn't know to be honest about partial failures. New
    # recordings that flag False produced incomplete data, so we ignore the
    # events and fall through to pyannote.
    speaking_events_complete = participant_data.get("speaking_events_complete", True)
    if not speaking_events_complete:
        speaking_events = []

    initial_participants = list(participant_data.get("initial_participants") or [])

    # Build the mapping pool from initial participants plus anyone who joined
    # mid-call. Without this, late joiners get no name to map to and the
    # diarizer's cluster for them falls through as an unmapped SPEAKER_NN
    # (or shifts later mappings).
    participants_detailed = _merge_late_joiners(
        participants_detailed, initial_participants, events
    )
    participants = (
        [p["name"] for p in participants_detailed]
        if participants_detailed else initial_participants
    )

    # If recorded with channel-split mode, the user's mic is on L only and the
    # Discord output on R only. We use that physical separation to make speaker
    # labelling reliable: L-dominant segments are unambiguously the user; we
    # only need pyannote to cluster the others on the R channel.
    channel_split, left_pcm, right_pcm = _load_channels_if_split(audio_path, audio_layout)

    _p(f"Loading Whisper model {model_name}…")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "float32"
    model = whisperx.load_model(model_name, device=device, compute_type=compute_type)
    diarize_model = None

    try:
        _p("Transcribing audio…")
        # Whisper always sees the full mix (mono sum). load_audio downmixes for us.
        audio = whisperx.load_audio(str(audio_path))
        result = model.transcribe(audio, batch_size=16)

        # WhisperX alignment pass: gives each WORD a precise start/end
        # timestamp. Required for assign_word_speakers to label per-word
        # (rather than per-segment) — without this, a single Whisper
        # segment can span 25+ seconds and several alternating speakers,
        # and assign_word_speakers collapses them all onto one
        # segment-level label (the "snax2theFuture says Irie's lines"
        # bug observed in recordings 002045 and 003831).
        #
        # The alignment model is small (~80 MB per language, cached
        # locally after first run) and runs in ~1-2 s per minute of
        # audio on GPU. Failure is non-fatal — we log and continue with
        # the unaligned result so downstream still produces a transcript,
        # just at the coarser segment-level labelling.
        try:
            lang = result.get("language") or "en"
            _p(f"Aligning words for speaker labelling (language={lang})…")
            align_model, align_metadata = whisperx.load_align_model(
                language_code=lang, device=device,
            )
            result = whisperx.align(
                result["segments"], align_model, align_metadata,
                audio, device, return_char_alignments=False,
            )
            # Free the alignment model promptly so it doesn't sit alongside
            # whisper + (later) diarization in VRAM.
            del align_model
            if device == "cuda":
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
        except Exception as e:
            log.warning(
                "Word alignment failed (%s); speaker labels will be "
                "segment-level only.", e,
            )

        # Primary path: ground-truth Discord SPEAKING_* events. If present, they
        # tell us exactly which user_id was speaking at every moment, which we
        # then map to display names via participants_detailed. Dramatically more
        # reliable than acoustic diarization on a Discord call.
        used_speaking_events = False
        if speaking_events:
            # Gate by audio_layout: a mic_only WAV contains only the local
            # user's speech, so applying remote users' SPEAKING events to
            # it labels silence (or the local user's voice) with someone
            # else's name. Symmetric for system_only. If the gate empties
            # the list (e.g. system_only with unknown self_user_id), drop
            # through to acoustic diarization rather than fabricate labels.
            speaking_events = _filter_events_by_layout(
                speaking_events, audio_layout, self_user_id
            )
        if speaking_events:
            try:
                _p(f"Using {len(speaking_events)} Discord SPEAKING_* events for speaker labels…")
                _, assign_word_speakers = _resolve_diarization_api(whisperx)
                diarize_df = _speaking_events_to_diarize_df(
                    speaking_events,
                    participants_detailed,
                    audio_seconds=len(audio) / 16000.0,
                )
                if diarize_df is not None and not diarize_df.empty:
                    result = assign_word_speakers(diarize_df, result)
                    used_speaking_events = True
                else:
                    _p("Speaking events present but produced no usable intervals; falling back.")
            except Exception as e:
                log.warning("Speaking-event assignment failed (%s); falling back.", e)

        # Fallback: pyannote diarization (when speaking events are absent or
        # unusable — e.g. recordings made before this branch, or non-Discord audio).
        if not used_speaking_events:
            if hf_token:
                _p("Running speaker diarization (pyannote fallback)…")
                try:
                    DiarizationPipeline, assign_word_speakers = _resolve_diarization_api(whisperx)
                    diar_model_name = "pyannote/speaker-diarization-community-1"
                    try:
                        diarize_model = DiarizationPipeline(
                            model_name=diar_model_name, token=hf_token, device=device
                        )
                    except TypeError:
                        diarize_model = DiarizationPipeline(
                            model_name=diar_model_name, use_auth_token=hf_token, device=device
                        )

                    if channel_split and right_pcm is not None:
                        _p("Diarizing right channel (loopback only)…")
                        diar_input = np.ascontiguousarray(right_pcm, dtype=np.float32)
                    else:
                        diar_input = audio

                    diarize_segments = diarize_model(diar_input)
                    result = assign_word_speakers(diarize_segments, result)
                except Exception as e:
                    log.warning("Diarization failed: %s", e)
                    _p(f"Diarization failed: {e}")
            else:
                _p("Skipping diarization (no HF token)")

        segments = result.get("segments", [])

        # Channel-aware override: any segment whose L energy dominates R is the
        # user, no matter what pyannote said. Tag with a sentinel that bypasses
        # _auto_map's renaming.
        #
        # SKIPPED when we already have ground-truth speaking events. With those,
        # the user's own mic activity is already represented in the timeline via
        # their user_id, and overriding with an energy heuristic would only
        # introduce noise (the previous tuning-treadmill problem).
        user_name: Optional[str] = None
        others: list[str] = participants
        if (
            not used_speaking_events
            and channel_split
            and left_pcm is not None
            and right_pcm is not None
            and participants
        ):
            user_name, others = _resolve_user_and_others(
                participants_detailed, participants, self_user_id
            )
            if user_name is None:
                log.warning(
                    "Could not resolve which participant is the local user "
                    "(self_user_id=%r); falling back to participants[0]=%r",
                    self_user_id, participants[0],
                )
                user_name = participants[0]
                # Remove only the first occurrence so duplicate display names in
                # the same channel don't all get stripped from the others list.
                others = list(participants)
                others.remove(user_name)
            _attach_user_via_channel_energy(segments, left_pcm, right_pcm, user_name)
            _p(f"Identified {user_name} via mic channel")

        if used_speaking_events:
            # assign_word_speakers already labelled segments with display names
            # (because our diarize_df put names directly in the speaker column).
            # No further mapping is required, and _auto_map's order-based logic
            # would actively wreck things by treating the display names as
            # SPEAKER_NN clusters to re-shuffle.
            for seg in segments:
                seg.setdefault("original_speaker", seg.get("speaker"))
                if not seg.get("speaker"):
                    seg["speaker"] = "UNKNOWN"
            mapped, unmapped = segments, set()
            _p("Speaker labels assigned from Discord ground truth")
        else:
            _p("Mapping speakers to usernames…")
            mapped, unmapped = _auto_map(segments, others if channel_split else participants)
            # Issue #19: the legacy `__USER__:<name>` sentinel-strip loop
            # used to live here. The structured `is_local_user` field
            # carries the same information without requiring downstream
            # consumers to parse a string, and
            # `_attach_user_via_channel_energy` now writes the display
            # name to `speaker` directly — no post-processing needed.

        # Roster shown to the user = the same merged roster we mapped against,
        # so the transcript header and the _mapping.json file reflect everyone
        # who could plausibly own a speaker label (initial roster + late
        # joiners). Falling back to initial_participants here would hide the
        # late joiners from the user even after they're correctly labelled
        # below.
        roster = [p["name"] for p in participants_detailed] or initial_participants

        out_path = audio_path.parent / f"{audio_path.stem}_transcript.txt"
        with out_path.open("w", encoding="utf-8") as f:
            # Scrub each roster entry: Discord display names are attacker-
            # controlled and can contain literal newlines or bidi/control
            # chars that would otherwise inject fake roster lines.
            safe_roster = [_scrub_name(p) for p in roster]
            f.write(f"Participants: {', '.join(safe_roster)}\n")
            f.write("=" * 60 + "\n\n")
            for seg in mapped:
                # Use .get() so a malformed segment doesn't kill the whole
                # transcript and discard 10+ minutes of GPU work.
                seg_speaker = seg.get("speaker") or "UNKNOWN"
                # WhisperX assigns a speaker per WORD via assign_word_speakers,
                # but a single Whisper segment can span 10–30 seconds and
                # cover several alternating speakers. Writing one
                # ``[speaker] text`` block per segment flattens those
                # alternations onto the segment-level label (typically the
                # majority speaker), producing transcripts where one block
                # contains both "Did you say X?" and the other speaker's
                # answer. Walk the per-word labels and split the segment
                # into one block per speaker run instead.
                blocks = _split_segment_by_word_speakers(seg, seg_speaker)
                for block_speaker, block_start, block_text in blocks:
                    f.write(
                        f"[{_scrub_name(block_speaker)}] "
                        f"({_format_time(block_start)})\n"
                    )
                    f.write(f"{block_text}\n\n")

        if unmapped:
            mapping_path = audio_path.parent / f"{audio_path.stem}_mapping.json"
            # Skip locally-attached segments (is_local_user=True) when
            # building the mapping file: those labels never came from
            # the diarizer's SPEAKER_NN clusters and don't represent a
            # decision the user would override. Issue #19: replaces the
            # legacy `not str.startswith("__")` string-sentinel filter.
            mapping_path.write_text(
                json.dumps(
                    {
                        "unmapped_speakers": sorted(unmapped),
                        "participants": roster,
                        "auto_mapping": {
                            seg["original_speaker"]: seg["speaker"]
                            for seg in mapped
                            if "original_speaker" in seg
                            and isinstance(seg.get("original_speaker"), str)
                            and not seg.get("is_local_user")
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            _p(f"Some speakers could not be auto-mapped. See {mapping_path.name}")

        _p(f"Transcript saved: {out_path}")
        return out_path
    finally:
        # Free GPU memory so a follow-up transcription on the same process
        # (e.g. clicking Transcribe again) doesn't OOM after 2-3 runs.
        try:
            del model
        except Exception:
            pass
        if diarize_model is not None:
            try:
                del diarize_model
            except Exception:
                pass
        if device == "cuda":
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
