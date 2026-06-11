"""UI-toolkit-agnostic core shared by the web UI and the legacy Tk GUI.

Everything here used to live in gui.py. It was extracted so the pywebview
frontend (backend.py / webui.py) can reuse the exact same scrubbing,
settings-transaction, consent-attestation, and recordings-indexing logic
without importing tkinter. gui.py re-exports these names so existing
imports (and tests) keep working unchanged.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import secrets_store
from .config import Config

log = logging.getLogger(__name__)


# Strip ASCII control chars (except tab) and a small set of Unicode
# directional-override characters from any string that gets routed through
# the log pane or status labels. Discord display names and meeting names
# can contain these and would otherwise let a malicious participant inject
# fake log lines or visually mask paths.
#
# Built from codepoint ranges (not literal characters) so this file stays
# pure ASCII — several of these codepoints are invisible or are treated as
# line terminators by editors, which makes a literal character class easy
# to corrupt silently.
_BAD_CODEPOINT_RANGES = [
    (0x0000, 0x0008),  # ASCII control chars (kept: tab 0x09, newline 0x0a)
    (0x000B, 0x001F),  # remaining ASCII control chars
    (0x007F, 0x007F),  # DEL
    (0x0080, 0x009F),  # C1 controls (NEL, etc.)
    (0x200B, 0x200D),  # zero-width space/non-joiner/joiner
    (0x200E, 0x200F),  # LRM, RLM
    (0x2028, 0x2029),  # LINE/PARAGRAPH SEPARATOR (Tk renders as newline)
    (0x202A, 0x202E),  # LRE, RLE, PDF, LRO, RLO
    (0x2060, 0x2060),  # WORD JOINER
    (0x2066, 0x2069),  # LRI, RLI, FSI, PDI
    (0xFEFF, 0xFEFF),  # zero-width no-break space / BOM
]
_BAD_LOG_CHARS = re.compile(
    "["
    + "".join(
        re.escape(chr(lo)) if lo == hi else re.escape(chr(lo)) + "-" + re.escape(chr(hi))
        for lo, hi in _BAD_CODEPOINT_RANGES
    )
    + "]"
)


def _scrub(s: str) -> str:
    """Make a string safe to render in the log pane without log spoofing."""
    if not isinstance(s, str):
        s = str(s)
    return _BAD_LOG_CHARS.sub("?", s).replace("\r", " ").replace("\n", " ")


def scrub_multiline(s: str) -> str:
    """Like _scrub but preserves newlines — for multi-line remote text
    (release notes) rendered via textContent in the web UI.

    CR is normalised BEFORE the character-class pass: it sits inside the
    scrubbed 0x0B-0x1F range, so scrubbing first would turn every CRLF
    line ending into a stray '?'.
    """
    if not isinstance(s, str):
        s = str(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _BAD_LOG_CHARS.sub("?", s)


_CONSENT_FIELD_MAX = 256


def _capped(value: object) -> Optional[str]:
    """Length-cap an identity field before it lands in participants.json.

    Returns None unchanged so the absence-vs-empty distinction the
    consent audit trail relies on is preserved. Non-string truthy
    values are coerced to str first because the Discord RPC identity
    payload is contract-defined as a dict of strings, but a future
    schema bump could deliver an int snowflake.

    also strips bidi-override, zero-width, and
    control characters via `_scrub()` BEFORE capping. Without that, a
    malicious Discord display name containing U+202E (right-to-left
    override) or similar would land in `participants.json` and the
    consent log line, corrupting the audit trail and enabling log
    spoofing. Scrubbing first means the 256-char cap applies to the
    post-scrub length, matching the displayed/logged form.
    """
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    return _scrub(s)[:_CONSENT_FIELD_MAX]


def apply_settings_atomically(c: Config, new_values: dict):
    """Apply new Settings values to `c` atomically with respect to
    SecretWriteError.

    extracted from `SettingsDialog._save` so the
    rollback semantics are unit-testable without instantiating a Tk
    Toplevel. The contract is:

    - Snapshot the prior secret state (token, hf_token).
    - Attempt the secret writes in this order: token (cleared if the
      client_id changed — the cached OAuth token belongs to the old
      application), hf_token. If any raises SecretWriteError, roll
      back the writes that already committed (best-effort: a rollback
      that itself raises is swallowed and logged) and return the
      original exception.
    - Only after all secret writes succeed, mutate the non-secret
      fields (client_id, whisper_model, audio_source, recordings_dir).
    - Returns None on success, the SecretWriteError instance on
      secret-write failure. The caller handles the persistence
      `c.save()` call and any UI dialogs.

    The previous (non-extracted) flow mutated `discord_client_id` and
    cleared the token BEFORE the secret writes that might fail, so a
    partial failure left the dialog telling the user "Settings were
    NOT saved" while the in-memory config carried the new client_id,
    a deleted token, and a half-applied keyring state.

    (The Discord Client Secret was removed from this contract in the
    PKCE migration, issue #1.)
    """
    new_client_id = new_values["discord_client_id"]
    new_hf_token = new_values["hf_token"]
    credentials_changed = new_client_id != c.discord_client_id
    orig_hf_token = c.hf_token
    orig_token = c.token
    secret_writes_committed: list[str] = []
    try:
        if credentials_changed:
            c.token = None
            secret_writes_committed.append("token")
        c.hf_token = new_hf_token
        secret_writes_committed.append("hf_token")
    except secrets_store.SecretWriteError as e:
        for kind in reversed(secret_writes_committed):
            try:
                if kind == "hf_token":
                    c.hf_token = orig_hf_token
                elif kind == "token" and orig_token is not None:
                    c.token = orig_token
            except secrets_store.SecretWriteError as rollback_err:
                # the docstring promises
                # "swallowed and logged"; previously this was silently
                # swallowed without a log entry, leaving operators with
                # no breadcrumb to diagnose a double-failure (primary
                # write fails, rollback also fails). Matches the
                # logging pattern at config.py's migration rollback.
                log.warning(
                    "apply_settings_atomically: rollback of %s failed "
                    "(%s); credential store may be in an inconsistent "
                    "state — manual cleanup via OS credential manager "
                    "may be required.",
                    kind,
                    rollback_err,
                )
        return e
    c.discord_client_id = new_client_id
    c.whisper_model = new_values["whisper_model"]
    c.audio_source = new_values["audio_source"]
    c.recordings_dir = new_values["recordings_dir"]
    c.loopback_device_name = new_values.get("loopback_device_name", "")
    # Optional so the legacy Tk dialog (which has no toggle) and config
    # doubles in tests keep the existing value untouched.
    c.check_updates = bool(
        new_values.get("check_updates", getattr(c, "check_updates", True))
    )
    return None


CONSENT_DIALOG_TEXT = (
    "This will record audio of every participant in the current "
    "voice channel — your microphone AND Discord's system audio "
    "(everyone else you can hear).\n\n"
    "The recording is saved locally on this computer and is NOT "
    "uploaded anywhere. You are responsible for obtaining consent "
    "from the other participants before recording.\n\n"
    "Continue?"
)
# when the dialog text is reworded, bump
# `CONSENT_DIALOG_VERSION` so older participants.json entries
# remain unambiguously identifiable. The text-sha256 lets an
# auditor verify, byte-for-byte, that a recording's consent record
# references the exact dialog version that was shown at the time —
# the `dialog_text` field alone could be silently rewritten by a
# tool walking participants.json across versions.
CONSENT_DIALOG_VERSION = "v1"
CONSENT_DIALOG_SHA256 = hashlib.sha256(
    CONSENT_DIALOG_TEXT.encode("utf-8"),
).hexdigest()


def build_consent_record(identity: Optional[dict]) -> dict:
    """Build the consent attestation persisted into participants.json.

    Called by whichever UI showed the consent dialog, AFTER the user
    accepted. Length-caps identity fields before persistence: a Discord
    display name is user-controlled and reaches us via the RPC IPC
    channel. The legitimate values are tiny (snowflake IDs are ~20
    digits; usernames cap at 32 chars per Discord's own limit) so 256
    is a comfortable upper bound. Without this cap, a participant who
    chose a multi-megabyte display name would silently inflate every
    participants.json on disk.
    """
    identity = identity or {}
    granted_by_user_id = _capped(identity.get("id"))
    granted_by_username = _capped(
        identity.get("global_name") or identity.get("username")
    )
    record = {
        "granted_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ),
        "granted_by_user_id": granted_by_user_id,
        "granted_by_username": granted_by_username,
        "dialog_text": CONSENT_DIALOG_TEXT,
        "dialog_version": CONSENT_DIALOG_VERSION,
        "dialog_sha256": CONSENT_DIALOG_SHA256,
    }
    log.info(
        "Recording consent granted by %s (%s)",
        record["granted_by_username"], record["granted_by_user_id"],
    )
    return record


def format_recording_label(path: Path) -> str:
    """Build a human-friendly display string for a recording WAV.

    Examples of the underlying filename:
      MEP Landing Page Call_discord_20260528_003930_audio.wav
      discord_20260525_210051_audio.wav  (no meeting name)

    Result: "MEP Landing Page Call  --  2026-05-28 00:39"
    """
    stem = path.stem  # strip .wav
    if stem.endswith("_audio"):
        stem = stem[: -len("_audio")]
    # Recorder names files one of:
    #   "<meeting>_discord_YYYYMMDD_HHMMSS"   (custom meeting name)
    #   "discord_YYYYMMDD_HHMMSS"             (no meeting name)
    meeting = "Untitled"
    ts_str = ""
    if "_discord_" in stem:
        head, _, ts_str = stem.rpartition("_discord_")
        meeting = head or "Untitled"
    elif stem.startswith("discord_"):
        ts_str = stem[len("discord_"):]
    else:
        meeting = stem  # unrecognised pattern; fall back to whole stem

    if len(ts_str) == 15 and ts_str[8] == "_" and ts_str[:8].isdigit():
        ts_pretty = f"{ts_str[:4]}-{ts_str[4:6]}-{ts_str[6:8]} {ts_str[9:11]}:{ts_str[11:13]}"
    else:
        ts_pretty = ts_str or "?"
    sep = "—"  # em dash, matching the original Tk label format
    return f"{_scrub(meeting)}  {sep}  {ts_pretty}"


def index_recordings(recordings_path: Path) -> list[tuple[str, Path]]:
    """Scan the recordings folder and return (label, path) pairs, newest
    first, with display labels de-duplicated.

    Only entries that have a participants.json next to them are
    returned -- otherwise transcription would immediately fail.
    """
    try:
        wavs = sorted(
            recordings_path.glob("*_audio.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception as e:
        log.warning("Could not list recordings: %s", e)
        wavs = []

    valid = [
        p for p in wavs
        if (p.parent / p.name.replace("_audio.wav", "_participants.json")).exists()
    ]

    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for p in valid:
        label = format_recording_label(p)
        # Disambiguate if two recordings somehow render to the same label
        base = label
        i = 2
        while label in seen:
            label = f"{base} ({i})"
            i += 1
        seen.add(label)
        out.append((label, p))
    return out
