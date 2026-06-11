"""Backend bridging the web frontend to recorder / RPC / transcription.

This is the pywebview ``js_api`` object: every public method is callable
from the frontend JavaScript as ``window.pywebview.api.<name>(...)`` and
runs on a worker thread (pywebview dispatches each call off the GUI
thread). Long-running operations therefore return immediately and push
progress to the frontend via events; the frontend renders exclusively
from the state object returned by :meth:`get_state` / pushed by
``_emit_state``.

The flows, guards, and race-condition fixes here mirror gui.MainWindow
one-for-one (publish-after-connect, stop-in-flight flag, token
persistence fallback, consent-before-capture). When changing behaviour
here, check whether gui.py needs the same change.

No tkinter and no pywebview imports in this module: the window handle is
injected by webui.py, which keeps this importable (and unit-testable) on
any platform.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from . import __version__, secrets_store
from .config import Config, OAuthToken
from .recorder import RecordingResult, RecordingSession
from .rpc import DiscordRPC
from .uicore import (
    CONSENT_DIALOG_TEXT,
    _scrub,
    apply_settings_atomically,
    build_consent_record,
    index_recordings,
    scrub_multiline,
)

log = logging.getLogger(__name__)

_AUDIO_SOURCE_LABELS = {
    "mixed": "Mic + System (recommended for meetings)",
    "mic_only": "Mic only (just your voice)",
    "system_only": "System only (Discord output, no mic)",
}
_WHISPER_MODELS = ["large-v3-turbo", "large-v3", "medium", "small", "base"]
_LOOPBACK_DEFAULT_LABEL = "(Windows default playback)"

# The only external pages the frontend may ask us to open. JS runs our own
# code, but funnelling through an allowlist means a compromised/buggy
# frontend cannot turn the backend into an arbitrary-URL launcher.
_ALLOWED_URLS = {
    "developer_portal": "https://discord.com/developers/applications",
    "latest_release": "https://github.com/ba1lly/Mynah/releases/latest",
}

_RELEASES_API = "https://api.github.com/repos/ba1lly/Mynah/releases/latest"
_RELEASE_NOTES_MAX = 20_000  # chars; UI shows a scrollable pane
# In-app updates only ever download from this repo's own release assets;
# any other URL in the API response (a compromised/spoofed feed) is
# ignored and the UI falls back to the manual download page.
_RELEASE_DOWNLOAD_PREFIX = "https://github.com/ba1lly/Mynah/releases/download/"


def _parse_sha256_file(text: str) -> Optional[str]:
    """First token of a `sha256sum`-style file ('<hex>  MynahSetup.exe')."""
    token = (text.split() or [""])[0].strip().lower()
    if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
        return token
    return None


def _installer_install_dir() -> Optional[Path]:
    """The install dir when running from a bootstrap-installer layout,
    else None (dev checkout / frozen Mynah.exe — no in-app update)."""
    root = os.environ.get("MYNAH_APP_ROOT")
    if not root:
        return None
    p = Path(root)
    if (p / "Mynah.pyw").exists() and (p / "python" / "pythonw.exe").exists():
        return p
    return None


def _extract_installer_assets(
    release_body: dict,
) -> tuple[Optional[str], Optional[str]]:
    """(exe_url, sha_url) for MynahSetup.exe assets, allowlisted to this
    repo's release-download URLs. Anything else is treated as absent."""
    exe_url: Optional[str] = None
    sha_url: Optional[str] = None
    for asset in release_body.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        url = str(asset.get("browser_download_url") or "")
        if not url.startswith(_RELEASE_DOWNLOAD_PREFIX):
            continue
        name = str(asset.get("name") or "")
        if name == "MynahSetup.exe":
            exe_url = url
        elif name == "MynahSetup.exe.sha256":
            sha_url = url
    return exe_url, sha_url


def _parse_version(tag: str) -> tuple[int, ...]:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3); leading digits per part,
    unparseable parts end the tuple ('1.2.3rc1' -> (1, 2, 3))."""
    import re as _re

    parts: list[int] = []
    for piece in tag.lstrip("vV").split("."):
        m = _re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts)


class WebLogHandler(logging.Handler):
    """Routes log records to the frontend log pane (scrubbed)."""

    def __init__(self, backend: "MynahBackend"):
        super().__init__()
        self.backend = backend

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        self.backend._push_log(_scrub(msg), record.levelname)


class MynahBackend:
    """State machine + JS API for the web UI."""

    def __init__(self, config: Config):
        self._config = config
        self._window: Any = None  # injected by webui.attach_window()
        self._lock = threading.RLock()

        self._rpc: Optional[DiscordRPC] = None
        self._session: Optional[RecordingSession] = None
        self.last_result: Optional[RecordingResult] = None
        self._closing = False
        # Set to True while a background stop() is in flight. Prevents
        # the close path from racing stop_recording on the same session
        # (RecordingSession.stop() is not idempotent).
        self._stopping = False
        self._connecting = False
        self._starting = False
        self._transcribing = False

        self._participants: list[str] = []
        self._rpc_error = ""
        self._recording_started_at: Optional[float] = None
        self._status = "Ready"
        # None = unknown (probe still running) so the frontend can show
        # "checking…" instead of a false negative during the slow import.
        self._whisperx_available: Optional[bool] = None

        self._log_buffer: deque[dict] = deque(maxlen=2000)
        self._recordings: list[dict] = []
        # Set when a newer release exists: {"version", "notes", "url"}.
        self._update: Optional[dict] = None
        # (exe_url, sha_url) of the new release's installer assets —
        # kept backend-side; the frontend never supplies download URLs.
        self._update_assets: Optional[tuple[str, Optional[str]]] = None
        self._updating = False
        self._refresh_recordings_index()

    # ---- wiring (called from webui.py, not exposed to JS: underscore) ----

    def _attach_window(self, window: Any) -> None:
        self._window = window

    def _start_environment_check(self) -> None:
        """Async equivalent of gui._check_environment.

        whisperx_available() imports whisperx (multi-second, multi-GB
        import) — the Tk GUI did this synchronously during construction
        and froze the window; here it runs on a worker so the UI is
        interactive immediately.
        """

        def probe() -> None:
            from .transcription import whisperx_available

            available = whisperx_available()
            with self._lock:
                self._whisperx_available = available
            if not available:
                log.warning(
                    "WhisperX not installed — transcription will be unavailable."
                )
            try:
                import pyaudiowpatch  # noqa: F401
            except ImportError:
                log.error(
                    "PyAudioWPatch missing — install with: pip install PyAudioWPatch"
                )
            if not self._config.discord_client_id:
                log.warning("Discord Client ID not configured. Open Settings.")
            self._emit_state()

        threading.Thread(target=probe, daemon=True, name="env-check").start()

    def _start_update_check(self) -> None:
        """Launch-time update check — only when the user has it enabled
        (Settings → 'Check for updates'); fail-silent on any network or
        API hiccup. This is the app's only phone-home (see README)."""
        if not self._config.check_updates:
            return

        def probe() -> None:
            update, _err = self._fetch_latest_release()
            if update is None:
                return  # up to date, or a network blip — silent on launch
            with self._lock:
                self._update = update
            log.info(
                "Update available: v%s — see the UPDATE badge.",
                update["version"],
            )
            self._emit_state()

        threading.Thread(target=probe, daemon=True, name="update-check").start()

    def _fetch_latest_release(self) -> tuple[Optional[dict], Optional[str]]:
        """(update, error): the newest GitHub release when it's newer
        than this build, else (None, None) for up-to-date and
        (None, message) for fetch failures — callers that talk to the
        user must not present a failed check as 'you're current'."""
        import requests

        from . import __version__ as current

        try:
            resp = requests.get(
                _RELEASES_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"Mynah/{current}",
                },
                timeout=8,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:  # noqa: BLE001 — never break the app over this
            log.debug("Update check failed: %s", e)
            return None, _scrub(str(e))
        tag = str(body.get("tag_name") or "")
        if not tag:
            return None, "release feed returned no version tag"
        if _parse_version(tag) <= _parse_version(current):
            return None, None
        notes = scrub_multiline(str(body.get("body") or ""))[:_RELEASE_NOTES_MAX]
        exe_url, sha_url = _extract_installer_assets(body)
        update = {
            "version": _scrub(tag.lstrip("vV")),
            "notes": notes,
            "url": _ALLOWED_URLS["latest_release"],
            # The UI offers one-click "Update now" only when (a) the
            # release ships an installer asset from this repo and (b)
            # this copy was itself installed by the installer.
            "canAutoUpdate": bool(
                exe_url and _installer_install_dir() is not None
            ),
        }
        with self._lock:
            self._update_assets = (exe_url, sha_url) if exe_url else None
        return update, None

    # ---- JS API: updates ----

    def check_for_updates(self) -> dict:
        """Manual check (Settings → Check now). Runs synchronously on
        pywebview's caller thread; the frontend awaits the promise."""
        if self._update is not None:
            return {"ok": True, "update": dict(self._update)}
        update, err = self._fetch_latest_release()
        if update is not None:
            with self._lock:
                self._update = update
            self._emit_state()
            return {"ok": True, "update": update}
        if err is not None:
            return {"ok": False, "error": err}
        return {"ok": True, "update": None}

    def start_update(self) -> dict:
        """One-click update: download the new MynahSetup.exe from the
        release (sha256-verified), hand the install dir to its --update
        mode, and exit — the installer re-runs only the changed steps
        and relaunches Mynah when done."""
        with self._lock:
            if self._updating:
                return {"ok": False, "error": "Update already in progress."}
            if self._update is None or self._update_assets is None:
                return {"ok": False, "error": "No update available."}
            if self._session is not None or self._starting or self._stopping:
                return {
                    "ok": False,
                    "error": "Stop the recording before updating.",
                }
            if self._transcribing:
                return {
                    "ok": False,
                    "error": "Wait for the transcription to finish "
                             "before updating.",
                }
            install_dir = _installer_install_dir()
            if install_dir is None:
                return {
                    "ok": False,
                    "error": "This copy wasn't installed by MynahSetup — "
                             "use the download page instead.",
                }
            self._updating = True
            self._status = "Downloading update…"
            exe_url, sha_url = self._update_assets
            version = self._update["version"]
        self._emit_state()

        def worker() -> None:
            try:
                dest = self._download_installer(
                    install_dir, exe_url, sha_url, version,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("Update download failed")
                with self._lock:
                    self._updating = False
                    self._status = "Ready"
                self._emit_error("Update", str(e))
                self._emit_state()
                return
            log.info("Update downloaded and verified; restarting to apply.")
            self._emit_event("update-restarting", {})
            import subprocess

            subprocess.Popen(
                [str(dest), "--update", str(install_dir)],
                cwd=str(install_dir),
                close_fds=True,
            )
            # Same teardown as request_quit: the installer takes over.
            self.request_quit()

        threading.Thread(target=worker, daemon=True, name="update").start()
        return {"ok": True}

    def _download_installer(
        self,
        install_dir: Path,
        exe_url: str,
        sha_url: Optional[str],
        version: str,
    ) -> Path:
        """Stream the installer asset to downloads\\, verify sha256."""
        import hashlib

        import requests

        expected: Optional[str] = None
        if sha_url:
            resp = requests.get(sha_url, timeout=15)
            resp.raise_for_status()
            expected = _parse_sha256_file(resp.text)
        if expected is None:
            raise RuntimeError(
                "Release is missing a usable sha256 file; refusing to "
                "run an unverified installer. Use the download page."
            )

        downloads = install_dir / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        dest = downloads / f"MynahSetup-{version}.exe"
        digest = hashlib.sha256()
        done = 0
        with requests.get(exe_url, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0) or None
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    self._emit_event(
                        "update-progress", {"done": done, "total": total},
                    )
        actual = digest.hexdigest()
        if actual != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                "Downloaded installer failed checksum verification "
                f"(expected {expected[:12]}…, got {actual[:12]}…). "
                "Not running it."
            )
        return dest

    def _has_active_session(self) -> bool:
        with self._lock:
            return self._session is not None

    def _on_window_closing(self) -> bool:
        """pywebview ``closing`` handler. Return False to cancel the close.

        Mirrors gui._on_close: with a recording running, closing must go
        through an explicit confirm (the frontend modal) so partial audio
        is never silently discarded. Without a session, tear down RPC
        inline (fast pipe close) and allow the close.
        """
        if self._closing:
            return True
        if self._has_active_session():
            self._emit_event("confirm-close", {})
            return False
        self._closing = True
        rpc = self._rpc
        self._rpc = None
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                log.exception("RPC close during shutdown raised")
        return True

    # ---- event plumbing ----

    def _emit_event(self, event: str, payload: dict) -> None:
        window = self._window
        if window is None:
            return
        try:
            data = json.dumps({"event": event, "payload": payload})
            window.evaluate_js(
                f"window.__mynah && window.__mynah.dispatch({data})"
            )
        except Exception:
            # Window gone (shutdown) or JS bridge not ready yet. The
            # frontend pulls full state on load, so a dropped push is
            # recovered on the next state event.
            pass

    def _emit_state(self) -> None:
        self._emit_event("state", self.get_state())

    def _emit_error(self, title: str, message: str) -> None:
        self._emit_event("error", {"title": title, "message": _scrub(message)})

    def _push_log(self, line: str, level: str) -> None:
        entry = {"line": line, "level": level}
        self._log_buffer.append(entry)
        self._emit_event("log", entry)

    def _set_status(self, text: str) -> None:
        with self._lock:
            self._status = _scrub(text)

    def _refresh_recordings_index(self) -> None:
        pairs = index_recordings(self._config.recordings_path)
        with self._lock:
            self._recordings = [
                {"label": label, "path": str(path)} for label, path in pairs
            ]

    # ---- JS API: state ----

    def get_state(self) -> dict:
        with self._lock:
            return {
                "version": __version__,
                "configured": bool(self._config.discord_client_id),
                "whisperxAvailable": self._whisperx_available,
                "connected": self._rpc is not None,
                "connecting": self._connecting,
                "rpcError": self._rpc_error,
                "participants": list(self._participants),
                "recording": self._session is not None and not self._starting,
                "starting": self._starting,
                "stopping": self._stopping,
                "recordingStartedAt": self._recording_started_at,
                "transcribing": self._transcribing,
                "status": self._status,
                "recordings": list(self._recordings),
                "update": dict(self._update) if self._update else None,
            }

    def get_log_backlog(self) -> list[dict]:
        return list(self._log_buffer)

    def get_consent_text(self) -> str:
        return CONSENT_DIALOG_TEXT

    # ---- JS API: connection ----

    def connect(self) -> dict:
        with self._lock:
            if self._connecting or self._rpc is not None:
                return {"ok": False, "error": "Already connected or connecting."}
            if not self._config.discord_client_id:
                return {"ok": False, "error": "not_configured"}
            self._connecting = True
            self._rpc_error = ""
            self._status = "Connecting to Discord…"

        client_id = self._config.discord_client_id
        existing_token = asdict(self._config.token) if self._config.token else None

        def worker() -> None:
            try:
                # Build the RPC instance locally; only publish to self._rpc
                # after connect() succeeds, so other calls can't observe a
                # half-initialised DiscordRPC during the connect window.
                rpc = DiscordRPC(client_id)
                new_token = rpc.connect(existing_token=existing_token)
            except BaseException as e:  # noqa: BLE001
                log.exception("RPC connect failed")
                with self._lock:
                    self._connecting = False
                    self._rpc_error = _scrub(str(e))
                    self._status = "Connection failed"
                self._emit_error("Discord RPC", str(e))
                self._emit_state()
                return

            rpc.on_disconnect = self._on_rpc_disconnect
            with self._lock:
                self._rpc = rpc
                self._connecting = False
                self._status = "Connected"
            try:
                self._config.token = OAuthToken(**new_token)
                self._config.save()
            except (OSError, secrets_store.SecretWriteError) as e:
                # A transient keyring/disk failure must not abort the
                # connected state — the in-memory token from the OAuth
                # response is still usable for this session; persistence
                # retries on the next connect. Mirrors gui.py.
                log.warning("Could not persist refreshed token: %s", e)
            self._emit_state()
            self.refresh_participants()

        threading.Thread(target=worker, daemon=True, name="rpc-connect").start()
        self._emit_state()
        return {"ok": True}

    def _on_rpc_disconnect(self, reason: str) -> None:
        """Fires on the RPC reader thread when Discord closes the pipe."""
        if self._closing:
            return
        log.warning("Discord RPC disconnected: %s", reason)
        with self._lock:
            rpc = self._rpc
            self._rpc = None
            self._participants = []
            self._rpc_error = _scrub(reason)
            if self._session is not None:
                # Recording cannot continue without RPC. Let the user stop
                # it explicitly so partial audio isn't silently discarded.
                self._status = "Discord disconnected — stop recording manually"
            else:
                self._status = "Discord disconnected"
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                log.exception("rpc.close() after disconnect raised")
        self._emit_state()

    def refresh_participants(self) -> dict:
        with self._lock:
            rpc = self._rpc
        if rpc is None:
            return {"ok": False, "error": "Not connected."}

        def worker() -> None:
            try:
                people = rpc.get_participants()
            except BaseException as e:  # noqa: BLE001
                log.error("Failed to fetch participants: %s", e)
                return
            with self._lock:
                # rpc may have been torn down between the fetch and now
                # (reconnect race, disconnect fired). Guard like gui.py.
                if self._rpc is not rpc:
                    return
                self._participants = [_scrub(p) for p in people]
            self._emit_state()

        threading.Thread(
            target=worker, daemon=True, name="refresh-participants"
        ).start()
        return {"ok": True}

    # ---- JS API: recording ----

    def start_recording(self, meeting_name: str, consent_granted: bool) -> dict:
        """Start a recording. ``consent_granted`` must be True — the
        frontend shows the consent dialog (text from get_consent_text)
        BEFORE calling this; the attestation record is built here,
        backend-side, so the frontend can't fabricate its contents."""
        if not consent_granted:
            log.info("User declined recording consent; aborting start.")
            self._set_status("Recording cancelled (consent declined)")
            self._emit_state()
            return {"ok": False, "error": "consent_declined"}

        with self._lock:
            rpc = self._rpc
            if rpc is None:
                return {"ok": False, "error": "Not connected."}
            if self._session is not None or self._starting:
                return {"ok": False, "error": "Recording already in progress."}

            consent_record = build_consent_record(getattr(rpc, "identity", None))
            try:
                session = RecordingSession(
                    rpc=rpc,
                    output_dir=self._config.ensure_recordings_dir(),
                    meeting_name=((meeting_name or "").strip() or None),
                    audio_source=self._config.audio_source,
                    consent_record=consent_record,
                    loopback_device_name=self._config.loopback_device_name,
                )
            except Exception as e:
                log.error("Start failed during session construction: %s", e)
                self._emit_error("Recording", str(e))
                return {"ok": False, "error": _scrub(str(e))}
            self._session = session
            self._starting = True
            self._status = "Starting recording…"

        def worker() -> None:
            try:
                people = session.start()
            except BaseException as e:  # noqa: BLE001
                log.error("Start failed: %s", e)
                with self._lock:
                    if self._session is session:
                        self._session = None
                        self._starting = False
                        self._status = "Start failed"
                self._emit_error("Recording", str(e))
                self._emit_state()
                return
            with self._lock:
                if self._session is not session:
                    return  # user already stopped / disconnected
                self._starting = False
                self._recording_started_at = time.time()
                self._status = "Recording"
            log.info(
                "Recording started with: %s",
                ", ".join(_scrub(p) for p in people),
            )
            self._emit_state()

        threading.Thread(target=worker, daemon=True, name="rec-start").start()
        self._emit_state()
        return {"ok": True}

    def stop_recording(self) -> dict:
        with self._lock:
            session = self._session
            if session is None:
                return {"ok": False, "error": "No recording in progress."}
            if self._stopping:
                return {"ok": False, "error": "Stop already in progress."}
            self._stopping = True
            self._status = "Saving recording…"
        self._emit_state()

        def worker() -> None:
            try:
                result = session.stop()
            except BaseException as e:  # noqa: BLE001
                log.exception("Recording stop failed")
                self._emit_error("Recording", str(e))
                self._after_stop()
                return
            self.last_result = result
            log.info("Saved: %s", result.audio_path)
            log.info("Participants: %s", result.participants_path)
            self._after_stop()
            self._set_status(f"Saved {result.audio_path.name}")
            self._refresh_recordings_index()
            self._emit_event(
                "recording-saved", {"path": str(result.audio_path)}
            )
            self._emit_state()

        threading.Thread(target=worker, daemon=True, name="rec-stop").start()
        return {"ok": True}

    def _after_stop(self) -> None:
        with self._lock:
            self._session = None
            self._stopping = False
            self._recording_started_at = None
            if self._status == "Saving recording…":
                self._status = "Ready"
        self._emit_state()

    # ---- JS API: recordings + transcription ----

    def refresh_recordings(self) -> dict:
        self._refresh_recordings_index()
        self._emit_state()
        return {"ok": True}

    def transcribe_recording(self, path_str: str) -> dict:
        with self._lock:
            known = {r["path"] for r in self._recordings}
        # Only paths produced by our own recordings scan are accepted —
        # the JS side never gets to point transcription at an arbitrary
        # filesystem path.
        if path_str not in known:
            return {"ok": False, "error": "Pick a recording from the list first."}
        audio_path = Path(path_str)
        participants_path = audio_path.parent / audio_path.name.replace(
            "_audio.wav", "_participants.json"
        )
        if not participants_path.exists():
            return {"ok": False, "error": f"Missing {participants_path.name}"}

        with self._lock:
            if self._transcribing:
                return {"ok": False, "error": "A transcription is already running."}
            self._transcribing = True
            self._status = f"Transcribing {audio_path.name}…"
        log.info("Transcribing %s", audio_path.name)
        self._emit_state()

        hf_token = self._config.hf_token or None
        model_name = self._config.whisper_model

        def worker() -> None:
            try:
                from .transcription import transcribe

                out = transcribe(
                    audio_path=audio_path,
                    participants_path=participants_path,
                    hf_token=hf_token,
                    model_name=model_name,
                    # No progress callback: transcribe() already logs each
                    # step via its internal _p() helper.
                )
            except BaseException as e:  # noqa: BLE001
                log.exception("Transcription failed")
                self._emit_error("Transcribe", str(e))
            else:
                self._emit_event("transcribed", {"path": str(out)})
            finally:
                with self._lock:
                    self._transcribing = False
                    self._status = "Ready"
                self._emit_state()

        threading.Thread(target=worker, daemon=True, name="transcribe").start()
        return {"ok": True}

    # ---- JS API: settings ----

    def get_settings(self) -> dict:
        c = self._config
        try:
            from .audio import list_loopback_devices

            loopback_devices = [d["label"] for d in list_loopback_devices()]
        except Exception as e:
            log.warning("Could not enumerate loopback devices: %s", e)
            loopback_devices = []
        return {
            "values": {
                "discord_client_id": c.discord_client_id,
                "hf_token": c.hf_token,
                "whisper_model": c.whisper_model,
                "audio_source": c.audio_source,
                "recordings_dir": c.recordings_dir,
                "loopback_device_name": c.loopback_device_name,
                "check_updates": c.check_updates,
            },
            "options": {
                "whisper_models": _WHISPER_MODELS,
                "audio_sources": [
                    {"value": k, "label": v}
                    for k, v in _AUDIO_SOURCE_LABELS.items()
                ],
                "loopback_devices": loopback_devices,
                "loopback_default_label": _LOOPBACK_DEFAULT_LABEL,
            },
        }

    def save_settings(self, values: dict) -> dict:
        c = self._config
        new_values = {
            "discord_client_id": str(values.get("discord_client_id", "")).strip(),
            "hf_token": str(values.get("hf_token", "")).strip(),
            "whisper_model": (
                str(values.get("whisper_model", "")).strip() or "large-v3-turbo"
            ),
            "audio_source": (
                values.get("audio_source")
                if values.get("audio_source") in _AUDIO_SOURCE_LABELS
                else "mixed"
            ),
            "recordings_dir": (
                str(values.get("recordings_dir", "")).strip() or c.recordings_dir
            ),
            "loopback_device_name": (
                ""
                if values.get("loopback_device_name") in (None, _LOOPBACK_DEFAULT_LABEL)
                else str(values.get("loopback_device_name"))
            ),
            "check_updates": bool(
                values.get("check_updates", c.check_updates)
            ),
        }

        secret_err = apply_settings_atomically(c, new_values)
        if secret_err is not None:
            return {
                "ok": False,
                "error": (
                    "Could not save credentials to the OS credential store:\n"
                    f"{_scrub(str(secret_err))}\n\n"
                    "Settings were NOT saved. The OS credential store rejected "
                    "the write. Try again, or restart the app and confirm your "
                    "keyring backend is unlocked."
                ),
            }
        try:
            c.save()
        except OSError as e:
            return {
                "ok": False,
                "error": (
                    f"Could not save settings:\n{_scrub(str(e))}\n\n"
                    "Check that the install directory is writable, then try again."
                ),
            }
        log.info("Settings saved")
        self._refresh_recordings_index()
        self._emit_state()
        return {"ok": True}

    def pick_folder(self) -> Optional[str]:
        """Native folder picker for the recordings-dir setting."""
        window = self._window
        if window is None:
            return None
        try:
            import webview  # local import: keep this module pywebview-free for tests

            # pywebview 6.x deprecates the FOLDER_DIALOG constant in
            # favour of the FileDialog enum; support both (pin is >=5.1).
            file_dialog = getattr(webview, "FileDialog", None)
            dialog_kind = (
                file_dialog.FOLDER if file_dialog is not None
                else webview.FOLDER_DIALOG
            )
            current = self._config.recordings_dir or str(Path.home())
            if not Path(current).exists():
                current = str(Path.home())
            result = window.create_file_dialog(dialog_kind, directory=current)
        except Exception as e:
            log.warning("Folder picker failed: %s", e)
            return None
        if not result:
            return None
        return str(result[0])

    # ---- JS API: misc ----

    def open_recordings_folder(self) -> dict:
        try:
            path = self._config.ensure_recordings_dir()
        except OSError as e:
            log.warning("Could not create recordings folder: %s", e)
            return {
                "ok": False,
                "error": (
                    f"Could not create recordings folder:\n"
                    f"{self._config.recordings_path}\n\n{_scrub(str(e))}"
                ),
            }
        if not path.is_dir():
            return {
                "ok": False,
                "error": f"Recordings folder is not a directory:\n{path}",
            }
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 (Windows-only intentional)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            log.warning("Could not open recordings folder: %s", e)
            return {"ok": False, "error": _scrub(str(e))}
        return {"ok": True}

    def open_url(self, key: str) -> dict:
        url = _ALLOWED_URLS.get(key)
        if url is None:
            return {"ok": False, "error": f"Unknown URL key: {_scrub(key)}"}
        webbrowser.open(url)
        return {"ok": True}

    def request_quit(self) -> dict:
        """Stop any active recording, tear down RPC, destroy the window.

        Called by the frontend after the user confirmed closing during a
        recording (see _on_window_closing). Runs on a worker because
        session.stop() can block ~14s when Discord is unreachable.
        """
        if self._closing:
            return {"ok": True}
        self._closing = True

        def worker() -> None:
            with self._lock:
                session = self._session
                self._session = None
                stop_in_flight = self._stopping
            if session is not None and not stop_in_flight:
                self._set_status("Saving recording…")
                self._emit_state()
                try:
                    session.stop()
                except Exception:
                    log.exception("Session stop during shutdown raised")
            else:
                # A background stop() already owns the session; wait for
                # it to finish rather than firing a concurrent stop().
                while True:
                    with self._lock:
                        if not self._stopping:
                            break
                    time.sleep(0.1)

            rpc = self._rpc
            self._rpc = None
            if rpc is not None:
                try:
                    rpc.close()
                except Exception:
                    log.exception("RPC close during shutdown raised")

            window = self._window
            if window is not None:
                try:
                    window.destroy()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True, name="shutdown").start()
        return {"ok": True}
