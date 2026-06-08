"""Tkinter GUI for the Mynah."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

from . import __version__, secrets_store
from .config import Config, OAuthToken
from .recorder import RecordingResult, RecordingSession
from .rpc import DiscordRPC, RpcError
from .transcription import transcribe, whisperx_available

log = logging.getLogger(__name__)


# Strip ASCII control chars (except tab) and a small set of Unicode
# directional-override characters from any string that gets routed through
# the log widget or status labels. Discord display names and meeting names
# can contain these and would otherwise let a malicious participant inject
# fake log lines or visually mask paths.
_BAD_LOG_CHARS = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f"          # ASCII control chars (kept: tab)
    r"\x80-\x9f"                         # C1 controls (NEL, etc.)
    r"\u200b-\u200d"                     # zero-width space/non-joiner/joiner
    r"\u200e\u200f"                      # LRM, RLM
    r"\u2028\u2029"                      # LINE/PARAGRAPH SEPARATOR (Tk renders as newline)
    r"\u202a-\u202e"                     # LRE, RLE, PDF, LRO, RLO
    r"\u2060"                            # WORD JOINER
    r"\u2066-\u2069"                     # LRI, RLI, FSI, PDI
    r"\ufeff]"                           # zero-width no-break space / BOM
)


def _scrub(s: str) -> str:
    """Make a string safe to render in the log widget without log spoofing."""
    if not isinstance(s, str):
        s = str(s)
    return _BAD_LOG_CHARS.sub("?", s).replace("\r", " ").replace("\n", " ")


_CONSENT_FIELD_MAX = 256


def apply_settings_atomically(c: Config, new_values: dict):
    """Apply new Settings values to `c` atomically with respect to
    SecretWriteError.

    extracted from `SettingsDialog._save` so the
    rollback semantics are unit-testable without instantiating a Tk
    Toplevel. The contract is:

    - Snapshot the prior secret state (token, client_secret, hf_token).
    - Attempt the secret writes in this order: token (if creds changed),
      client_secret, hf_token. If any raises SecretWriteError, roll
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
    """
    new_client_id = new_values["discord_client_id"]
    new_secret = new_values["discord_client_secret"]
    new_hf_token = new_values["hf_token"]
    credentials_changed = (
        new_client_id != c.discord_client_id
        or new_secret != c.discord_client_secret
    )
    orig_secret = c.discord_client_secret
    orig_hf_token = c.hf_token
    orig_token = c.token
    secret_writes_committed: list[str] = []
    try:
        if credentials_changed:
            c.token = None
            secret_writes_committed.append("token")
        c.discord_client_secret = new_secret
        secret_writes_committed.append("client_secret")
        c.hf_token = new_hf_token
        secret_writes_committed.append("hf_token")
    except secrets_store.SecretWriteError as e:
        for kind in reversed(secret_writes_committed):
            try:
                if kind == "hf_token":
                    c.hf_token = orig_hf_token
                elif kind == "client_secret":
                    c.discord_client_secret = orig_secret
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
    return None


def _capped(value: object) -> Optional[str]:
    """Length-cap an identity field before it lands in participants.json.

    Returns None unchanged so the absence-vs-empty distinction the
    consent audit trail relies on is preserved. Non-string truthy
    values are coerced to str first because the Discord RPC identity
    payload is contract-defined as a dict of strings, but a future
    schema bump could deliver an int snowflake.

    also strips bidi-override, zero-width, and
    control characters via `_scrub()` BEFORE capping. Without that, a
    malicious Discord display name containing `\\u202e` (right-to-left
    override) or similar would land in `participants.json` and the
    consent log line, corrupting the audit trail and enabling log
    spoofing. Scrubbing first means the 256-char cap applies to the
    post-scrub length, matching the displayed/logged form.
    """
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    return _scrub(s)[:_CONSENT_FIELD_MAX]


class TextHandler(logging.Handler):
    def __init__(self, widget: scrolledtext.ScrolledText):
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        msg = _scrub(msg)

        widget = self.widget

        def _append():
            try:
                if not widget.winfo_exists():
                    return
                widget.configure(state=tk.NORMAL)
                widget.insert(tk.END, msg + "\n")
                widget.see(tk.END)
                widget.configure(state=tk.DISABLED)
            except tk.TclError:
                # Widget was destroyed between winfo_exists() and the call.
                # Daemon threads logging during shutdown hit this.
                pass

        try:
            widget.after(0, _append)
        except (tk.TclError, RuntimeError):
            # Widget already destroyed; drop the record.
            pass


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, config: Config):
        super().__init__(parent)
        self.title("Settings")
        self.transient(parent)
        self.resizable(False, False)
        self.config_obj = config
        self.result = False

        frm = ttk.Frame(self, padding=12)
        frm.grid(row=0, column=0)

        row = 0
        ttk.Label(frm, text="Discord Application Setup", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 4)
        )
        row += 1
        link = ttk.Label(
            frm,
            text="Open Discord Developer Portal →",
            foreground="#0066cc",
            cursor="hand2",
        )
        link.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        link.bind("<Button-1>", lambda _e: webbrowser.open("https://discord.com/developers/applications"))
        row += 1

        ttk.Label(frm, text="Client ID:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.client_id = ttk.Entry(frm, width=48)
        self.client_id.grid(row=row, column=1, columnspan=2, pady=3)
        self.client_id.insert(0, config.discord_client_id)
        row += 1

        ttk.Label(frm, text="Client Secret:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.client_secret = ttk.Entry(frm, width=48, show="•")
        self.client_secret.grid(row=row, column=1, columnspan=2, pady=3)
        self.client_secret.insert(0, config.discord_client_secret)
        row += 1

        ttk.Separator(frm, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky=tk.EW, pady=10
        )
        row += 1

        ttk.Label(frm, text="HF Token (diarization):").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.hf_token = ttk.Entry(frm, width=48, show="•")
        self.hf_token.grid(row=row, column=1, columnspan=2, pady=3)
        self.hf_token.insert(0, config.hf_token)
        row += 1

        ttk.Label(frm, text="Whisper Model:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.whisper_model = ttk.Combobox(
            frm,
            values=["large-v3-turbo", "large-v3", "medium", "small", "base"],
            width=45,
        )
        self.whisper_model.grid(row=row, column=1, columnspan=2, pady=3)
        self.whisper_model.set(config.whisper_model)
        row += 1

        ttk.Label(frm, text="Audio Source:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self._audio_source_labels = {
            "mixed":       "Mic + System (recommended for meetings)",
            "mic_only":    "Mic only (just your voice)",
            "system_only": "System only (Discord output, no mic)",
        }
        self.audio_source = ttk.Combobox(
            frm,
            values=list(self._audio_source_labels.values()),
            width=45,
            state="readonly",
        )
        self.audio_source.grid(row=row, column=1, columnspan=2, pady=3)
        self.audio_source.set(
            self._audio_source_labels.get(config.audio_source, self._audio_source_labels["mixed"])
        )
        row += 1

        # Loopback device picker. Enumerated at dialog-open time via
        # audio.list_loopback_devices() so newly attached devices show
        # up without an app restart. The blank "(Windows default)"
        # entry maps to an empty string in config and is the safe
        # zero-config choice for most users.
        ttk.Label(frm, text="Loopback device:").grid(
            row=row, column=0, sticky=tk.W, pady=3
        )
        self._loopback_default_label = "(Windows default playback)"
        try:
            from .audio import list_loopback_devices
            self._loopback_devices = list_loopback_devices()
        except Exception as e:
            log.warning("Could not enumerate loopback devices: %s", e)
            self._loopback_devices = []
        loopback_values = [self._loopback_default_label] + [
            d["label"] for d in self._loopback_devices
        ]
        self.loopback_device = ttk.Combobox(
            frm,
            values=loopback_values,
            width=55,
            state="readonly",
        )
        self.loopback_device.grid(row=row, column=1, columnspan=2, pady=3)
        if config.loopback_device_name and any(
            d["label"] == config.loopback_device_name
            for d in self._loopback_devices
        ):
            self.loopback_device.set(config.loopback_device_name)
        else:
            # Either no override saved, or the saved label no longer
            # matches an enumerated device — show the "use the default"
            # row so the user can see something is selected.
            self.loopback_device.set(self._loopback_default_label)
        row += 1

        ttk.Label(frm, text="Recordings folder:").grid(row=row, column=0, sticky=tk.W, pady=3)
        self.recordings_dir = ttk.Entry(frm, width=38)
        self.recordings_dir.grid(row=row, column=1, pady=3)
        self.recordings_dir.insert(0, config.recordings_dir)
        ttk.Button(frm, text="…", width=3, command=self._pick_dir).grid(row=row, column=2, pady=3)
        row += 1

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=3, pady=(12, 0), sticky=tk.E)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Save", command=self._save).pack(side=tk.RIGHT)

        self.grab_set()
        self.client_id.focus_set()

    def _pick_dir(self) -> None:
        current = self.recordings_dir.get().strip() or str(Path.home())
        if not Path(current).exists():
            current = str(Path.home())
        try:
            path = filedialog.askdirectory(initialdir=current)
        except tk.TclError:
            path = ""
        if path:
            self.recordings_dir.delete(0, tk.END)
            self.recordings_dir.insert(0, path)

    def _save(self) -> None:
        c = self.config_obj
        new_values = {
            "discord_client_id": self.client_id.get().strip(),
            "discord_client_secret": self.client_secret.get().strip(),
            "hf_token": self.hf_token.get().strip(),
            "whisper_model": (
                self.whisper_model.get().strip() or "large-v3-turbo"
            ),
            "audio_source": next(
                (
                    k for k, v in self._audio_source_labels.items()
                    if v == self.audio_source.get()
                ),
                "mixed",
            ),
            "recordings_dir": (
                self.recordings_dir.get().strip() or c.recordings_dir
            ),
            "loopback_device_name": (
                "" if self.loopback_device.get() == self._loopback_default_label
                else self.loopback_device.get()
            ),
        }

        secret_err = apply_settings_atomically(c, new_values)
        if secret_err is not None:
            messagebox.showerror(
                "Settings",
                f"Could not save credentials to the OS credential store:\n"
                f"{_scrub(str(secret_err))}\n\n"
                "Settings were NOT saved. The OS credential store rejected "
                "the write. Try again, or restart the app and confirm your "
                "keyring backend is unlocked.",
                parent=self,
            )
            return
        try:
            c.save()
        except OSError as e:
            messagebox.showerror(
                "Settings",
                f"Could not save settings:\n{_scrub(str(e))}\n\n"
                "Check that the install directory is writable, then try again.",
                parent=self,
            )
            return
        self.result = True
        self.destroy()


class MainWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Mynah {__version__}")
        self.root.geometry("720x600")
        self.root.minsize(680, 540)

        # Forward both the window-close (X) and the File→Exit menu through
        # a single shutdown path so RPC handles and active recording
        # sessions are always cleaned up. Previously File→Exit called
        # root.quit() and the X button did nothing, so closing during a
        # recording silently killed the capture threads mid-write.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config = Config.load()
        self.rpc: Optional[DiscordRPC] = None
        self.session: Optional[RecordingSession] = None
        self.last_result: Optional[RecordingResult] = None
        self._closing = False
        # Set to True while a background stop() is in flight. Prevents
        # _on_close from racing with _stop_recording on the same session.
        self._stopping = False

        self._build_menu()
        self._build_ui()
        self._wire_logging()
        self._check_environment()
        # Seed the dropdown with existing recordings so Transcribe is usable
        # immediately — the previous "Last" button silently picked whatever
        # mtime-ranked first, which surprised users when a still-being-saved
        # file wasn't yet visible at click time.
        self._refresh_recordings()

    # ---- UI construction ----

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_m = tk.Menu(menubar, tearoff=False)
        file_m.add_command(label="Open recordings folder", command=self._open_recordings)
        file_m.add_separator()
        file_m.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_m)

        edit_m = tk.Menu(menubar, tearoff=False)
        edit_m.add_command(label="Settings…", command=self._open_settings)
        menubar.add_cascade(label="Edit", menu=edit_m)

        help_m = tk.Menu(menubar, tearoff=False)
        help_m.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=help_m)

        self.root.config(menu=menubar)

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        status_frame = ttk.LabelFrame(self.root, text="Status", padding=8)
        status_frame.pack(fill=tk.X, **pad)
        self.rpc_lbl = ttk.Label(status_frame, text="Discord RPC: not connected", foreground="#a33")
        self.rpc_lbl.pack(anchor=tk.W)
        self.parts_lbl = ttk.Label(status_frame, text="Participants: —", foreground="#666")
        self.parts_lbl.pack(anchor=tk.W)
        self.rec_lbl = ttk.Label(status_frame, text="Idle", foreground="#080")
        self.rec_lbl.pack(anchor=tk.W)

        conn_frame = ttk.Frame(self.root)
        conn_frame.pack(fill=tk.X, **pad)
        self.connect_btn = ttk.Button(
            conn_frame, text="Connect to Discord", command=self._connect, width=22
        )
        self.connect_btn.pack(side=tk.LEFT)
        self.refresh_btn = ttk.Button(
            conn_frame, text="Refresh Participants", command=self._refresh_participants, width=22, state=tk.DISABLED
        )
        self.refresh_btn.pack(side=tk.LEFT, padx=8)

        meeting_frame = ttk.LabelFrame(self.root, text="Meeting", padding=8)
        meeting_frame.pack(fill=tk.X, **pad)
        ttk.Label(meeting_frame, text="Name (optional):").grid(row=0, column=0, sticky=tk.W)
        self.meeting_name = ttk.Entry(meeting_frame, width=40)
        self.meeting_name.grid(row=0, column=1, sticky=tk.EW, padx=6)
        meeting_frame.columnconfigure(1, weight=1)

        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, **pad)
        self.start_btn = ttk.Button(
            ctrl_frame, text="● Start Recording", command=self._start_recording, width=20, state=tk.DISABLED
        )
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(
            ctrl_frame, text="■ Stop Recording", command=self._stop_recording, width=20, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=8)
        # --- Transcribe row ---------------------------------------------
        tx_frame = ttk.LabelFrame(self.root, text="Transcribe", padding=8)
        tx_frame.pack(fill=tk.X, **pad)

        ttk.Label(tx_frame, text="Recording:").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        # display_name -> Path mapping populated by _refresh_recordings()
        self._recordings_index: dict[str, Path] = {}
        self.recording_pick = ttk.Combobox(tx_frame, state="readonly", width=55)
        self.recording_pick.grid(row=0, column=1, sticky=tk.EW)
        tx_frame.columnconfigure(1, weight=1)
        self.refresh_recs_btn = ttk.Button(
            tx_frame, text="↻", width=3, command=self._refresh_recordings
        )
        self.refresh_recs_btn.grid(row=0, column=2, padx=(6, 0))
        self.transcribe_btn = ttk.Button(
            tx_frame, text="Transcribe selected", command=self._transcribe_selected, width=22
        )
        self.transcribe_btn.grid(row=1, column=0, columnspan=3, sticky=tk.E, pady=(8, 0))

        log_frame = ttk.LabelFrame(self.root, text="Log", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self.log_widget = scrolledtext.ScrolledText(log_frame, height=14, state=tk.DISABLED, font=("Consolas", 9))
        self.log_widget.pack(fill=tk.BOTH, expand=True)

        self.statusbar = ttk.Label(self.root, text="Ready", relief=tk.SUNKEN, anchor=tk.W, padding=(8, 2))
        self.statusbar.pack(fill=tk.X, side=tk.BOTTOM)

    def _wire_logging(self) -> None:
        handler = TextHandler(self.log_widget)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        # The TextHandler is THE log destination for this app. Without
        # removing existing handlers we'd double-print every log line —
        # once to stderr (from logging.basicConfig in app.py) and once to
        # the GUI pane, with different formatters.
        for h in list(root_logger.handlers):
            if not isinstance(h, TextHandler):
                root_logger.removeHandler(h)
        root_logger.addHandler(handler)
        self._log_handler = handler

    def _check_environment(self) -> None:
        if not whisperx_available():
            log.warning("WhisperX not installed — transcription will be unavailable.")
        try:
            import pyaudiowpatch  # noqa: F401
        except ImportError:
            log.error("PyAudioWPatch missing — install with: pip install PyAudioWPatch")
        if not self.config.discord_client_id or not self.config.discord_client_secret:
            log.warning("Discord credentials not configured. Open Edit → Settings.")

    # ---- background-task plumbing ----

    def _run_background(
        self,
        target: Callable[[], object],
        on_success: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        on_finally: Optional[Callable[[], None]] = None,
        log_label: str = "background task",
    ) -> threading.Thread:
        """Run `target()` on a daemon thread, marshalling completion callbacks
        back to the Tk main thread via root.after(). Used by every
        long-running GUI action (connect, stop, transcribe) so the boilerplate
        for thread+exception+after() lives in exactly one place.
        """

        def _post(callback: Callable[[], None]) -> None:
            try:
                self.root.after(0, callback)
            except (tk.TclError, RuntimeError):
                # Mainloop is gone (we're shutting down). Drop silently.
                pass

        def worker() -> None:
            try:
                result = target()
            except BaseException as e:  # noqa: BLE001
                log.exception("%s failed", log_label)
                if on_error is not None:
                    _post(lambda exc=e: on_error(exc))
            else:
                if on_success is not None:
                    _post(lambda res=result: on_success(res))
            finally:
                if on_finally is not None:
                    _post(on_finally)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    # ---- actions ----

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.root, self.config)
        self.root.wait_window(dlg)
        if dlg.result:
            log.info("Settings saved")

    def _open_recordings(self) -> None:
        # Use ensure_recordings_dir() rather than the read-only
        # recordings_path property, so the folder gets created on first
        # launch instead of erroring out.
        try:
            path = self.config.ensure_recordings_dir()
        except OSError as e:
            log.warning("Could not create recordings folder: %s", e)
            messagebox.showerror(
                "Open recordings folder",
                f"Could not create recordings folder:\n{self.config.recordings_path}\n\n{e}",
                parent=self.root,
            )
            return
        if not path.is_dir():
            messagebox.showerror(
                "Open recordings folder",
                f"Recordings folder is not a directory:\n{path}",
                parent=self.root,
            )
            return
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 (Windows-only intentional)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            log.warning("Could not open recordings folder: %s", e)

    def _about(self) -> None:
        messagebox.showinfo(
            "About",
            f"Mynah {__version__}\n\n"
            "Records Discord meetings with mic + system audio,\n"
            "tracks participants via Discord RPC, and transcribes\n"
            "with WhisperX.",
        )

    def _set_status(self, text: str) -> None:
        self.statusbar.config(text=_scrub(text))

    def _connect(self) -> None:
        if not self.config.discord_client_id or not self.config.discord_client_secret:
            messagebox.showwarning(
                "Configuration required",
                "Please set your Discord Client ID and Client Secret in Settings first.",
            )
            self._open_settings()
            return

        self._set_status("Connecting to Discord…")
        self.connect_btn.config(state=tk.DISABLED)

        client_id = self.config.discord_client_id
        client_secret = self.config.discord_client_secret
        existing_token = asdict(self.config.token) if self.config.token else None

        def task():
            # Build the RPC instance locally; only publish to self.rpc after
            # connect() succeeds. This prevents the (previously real) race
            # where _refresh_participants / _start_recording could observe a
            # half-initialised DiscordRPC during the connect window.
            rpc = DiscordRPC(client_id, client_secret)
            new_token = rpc.connect(existing_token=existing_token)
            return rpc, new_token

        def on_success(result):
            rpc, new_token = result  # type: ignore[misc]
            self.rpc = rpc
            # Wire on_disconnect so the reader thread dying (Discord
            # closed the pipe, PING/PONG failed, etc.) flips the UI
            # back to disconnected. Without this, the GUI would
            # continue to show "connected ✓" while every participant
            # query silently returned [].
            rpc.on_disconnect = self._on_rpc_disconnect
            try:
                self.config.token = OAuthToken(**new_token)
                self.config.save()
            except (OSError, secrets_store.SecretWriteError) as e:
                # Catching SecretWriteError here is the matching half of
                # the fix's GUI contract (Settings dialog already
                # surfaces it). Without this, a transient keyring backend
                # failure on token persistence would abort the Tk
                # `on_success` callback before `_on_connected()` runs —
                # RPC stayed connected in memory but the UI froze in the
                # "connecting…" state with no way back. The in-memory
                # token from the OAuth response is still usable for this
                # session; persistence will retry on the next connect.
                log.warning("Could not persist refreshed token: %s", e)
            self._on_connected()

        def on_error(exc):
            self._on_connect_failed(str(exc))

        self._run_background(task, on_success=on_success, on_error=on_error,
                             log_label="RPC connect")

    def _on_connected(self) -> None:
        self.rpc_lbl.config(text="Discord RPC: connected ✓", foreground="#080")
        self.refresh_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.NORMAL)
        self._set_status("Connected")
        self._refresh_participants()

    def _on_rpc_disconnect(self, reason: str) -> None:
        """Fires on the RPC reader thread when Discord closes the pipe.

        Marshals onto the Tk main thread to flip the UI back to a
        disconnected state. Without this, the GUI continued to show
        "connected ✓" while participant queries silently returned [].
        """
        def _apply() -> None:
            if self._closing:
                return
            log.warning("Discord RPC disconnected: %s", reason)
            try:
                if self.rpc is not None:
                    try:
                        self.rpc.close()
                    except Exception:
                        log.exception("rpc.close() after disconnect raised")
                    self.rpc = None
                if self.session is not None:
                    # Recording cannot continue without RPC. Let the
                    # user stop it explicitly so partial audio isn't
                    # silently discarded — just surface the state.
                    self._set_status("Discord disconnected — stop recording manually")
                    self.rec_lbl.config(text="● Discord disconnected", foreground="#a33")
                self.rpc_lbl.config(text=f"Discord RPC: disconnected — {_scrub(reason)}",
                                    foreground="#a33")
                self.connect_btn.config(state=tk.NORMAL)
                self.refresh_btn.config(state=tk.DISABLED)
                if self.session is None:
                    self.start_btn.config(state=tk.DISABLED)
                self._set_status("Discord disconnected")
            except tk.TclError:
                pass

        try:
            self.root.after(0, _apply)
        except (tk.TclError, RuntimeError):
            pass

    def _on_connect_failed(self, msg: str) -> None:
        # Make absolutely sure a failed half-connect doesn't leave an
        # orphan reader thread holding the Discord pipe — that's the
        # leading cause of "Discord won't connect again after one bad
        # attempt".
        if self.rpc is not None:
            try:
                self.rpc.close()
            except Exception:
                log.exception("rpc.close() after failed connect raised")
            self.rpc = None
        safe = _scrub(msg)
        self.rpc_lbl.config(text=f"Discord RPC: failed — {safe}", foreground="#a33")
        self.connect_btn.config(state=tk.NORMAL)
        self.refresh_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        self._set_status("Connection failed")
        messagebox.showerror("Discord RPC", safe)

    def _refresh_participants(self) -> None:
        """Fetch participants from Discord and update the label.

        The fetch is a blocking pipe round-trip — Discord's IPC pipe can
        block the calling thread for several seconds (and in pathological
        cases indefinitely, e.g. when Discord's server-side write stalls).
        Doing it inline on the Tk main thread froze the entire GUI: the
        window stopped responding to clicks and typing while the pipe
        write was outstanding. Issue surfaced immediately after a
        successful Connect, when _on_connected() called this synchronously.

        Fix: do the pipe I/O on a worker thread and marshal only the
        label updates back to the Tk thread via _run_background().
        """
        if not self.rpc:
            return
        rpc = self.rpc

        def fetch() -> "list[str]":
            return rpc.get_participants()

        def on_success(people) -> None:
            # rpc may have been torn down between the fetch and now (user
            # clicked Connect twice fast, or a disconnect fired). Guard.
            if self.rpc is not rpc:
                return
            if people:
                joined = ", ".join(_scrub(p) for p in people)
                self.parts_lbl.config(
                    text=f"Participants ({len(people)}): {joined}",
                    foreground="#080",
                )
            else:
                self.parts_lbl.config(
                    text="Participants: none (join a voice channel)",
                    foreground="#666",
                )

        def on_error(exc: BaseException) -> None:
            log.error("Failed to fetch participants: %s", exc)

        self._run_background(
            fetch, on_success=on_success, on_error=on_error,
            log_label="refresh participants",
        )

    def _start_recording(self) -> None:
        if not self.rpc:
            return

        # Issue #25: privacy consent gate. The user must explicitly
        # authorise the recording before any audio capture begins.
        # The consent attestation is persisted verbatim into
        # participants.json so the recording's provenance is auditable.
        # Cancel here means no audio is captured at all — we abort
        # before constructing the AudioRecorder, so PortAudio is never
        # opened and no chunk is ever buffered.
        consent_record = self._collect_recording_consent()
        if consent_record is None:
            self._set_status("Recording cancelled (consent declined)")
            return

        # Construct the session on the Tk thread (cheap — just stashes
        # parameters) but run .start() on a worker. .start() does multiple
        # blocking RPC round-trips (GET_SELECTED_VOICE_CHANNEL, SUBSCRIBE
        # SPEAKING_*, GET_CHANNEL, etc) plus PortAudio device probing —
        # any of which can stall the Tk main thread for seconds. Same
        # pattern as _refresh_participants; see also _on_connected.
        try:
            session = RecordingSession(
                rpc=self.rpc,
                output_dir=self.config.ensure_recordings_dir(),
                meeting_name=(self.meeting_name.get().strip() or None),
                audio_source=self.config.audio_source,
                consent_record=consent_record,
                loopback_device_name=self.config.loopback_device_name,
            )
        except Exception as e:
            log.error("Start failed during session construction: %s", e)
            messagebox.showerror("Recording", _scrub(str(e)))
            return
        self.session = session
        self.start_btn.config(state=tk.DISABLED)
        self._set_status("Starting recording…")

        def begin() -> "list[str]":
            return session.start()

        def on_success(people) -> None:
            if self.session is not session:
                return  # user already stopped / disconnected
            self.stop_btn.config(state=tk.NORMAL)
            self.rec_lbl.config(
                text=f"● Recording — {len(people)} participant(s)",
                foreground="#c00",
            )
            self._set_status("Recording")
            log.info(
                "Recording started with: %s",
                ", ".join(_scrub(p) for p in people),
            )

        def on_error(exc: BaseException) -> None:
            log.error("Start failed: %s", exc)
            if self.session is session:
                self.session = None
                self.start_btn.config(state=tk.NORMAL)
                self._set_status("Start failed")
            messagebox.showerror("Recording", _scrub(str(exc)))

        self._run_background(
            begin, on_success=on_success, on_error=on_error,
            log_label="start recording",
        )

    _CONSENT_DIALOG_TEXT = (
        "This will record audio of every participant in the current "
        "voice channel — your microphone AND Discord's system audio "
        "(everyone else you can hear).\n\n"
        "The recording is saved locally on this computer and is NOT "
        "uploaded anywhere. You are responsible for obtaining consent "
        "from the other participants before recording.\n\n"
        "Continue?"
    )
    # when the dialog text is reworded, bump
    # `_CONSENT_DIALOG_VERSION` so older participants.json entries
    # remain unambiguously identifiable. The text-sha256 lets an
    # auditor verify, byte-for-byte, that a recording's consent record
    # references the exact dialog version that was shown at the time —
    # the `dialog_text` field alone could be silently rewritten by a
    # tool walking participants.json across versions.
    _CONSENT_DIALOG_VERSION = "v1"
    _CONSENT_DIALOG_SHA256 = hashlib.sha256(
        _CONSENT_DIALOG_TEXT.encode("utf-8"),
    ).hexdigest()

    def _collect_recording_consent(self) -> Optional[dict]:
        """Show the privacy consent dialog and return an attestation
        record on accept, or None on decline.

        Issue #25: the recording-consent UX gate. Called from
        `_start_recording` BEFORE any audio capture begins. The
        returned dict is persisted verbatim into participants.json so
        the recording carries an auditable trail of the user's
        authorisation. Decline aborts the recording entirely — no
        PortAudio open, no chunk buffered.
        """
        ok = messagebox.askokcancel(
            "Recording consent",
            self._CONSENT_DIALOG_TEXT,
            parent=self.root,
            icon=messagebox.WARNING,
        )
        if not ok:
            log.info("User declined recording consent; aborting start.")
            return None

        identity = getattr(self.rpc, "identity", None) or {}
        # Length-cap identity fields before persistence: a Discord
        # display name is user-controlled and reaches us via the RPC
        # IPC channel. The legitimate values are tiny (snowflake IDs
        # are ~20 digits; usernames cap at 32 chars per Discord's own
        # limit) so 256 is a comfortable upper bound. Without this
        # cap, a participant who chose a multi-megabyte display name
        # would silently inflate every participants.json on disk.
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
            "dialog_text": self._CONSENT_DIALOG_TEXT,
            "dialog_version": self._CONSENT_DIALOG_VERSION,
            "dialog_sha256": self._CONSENT_DIALOG_SHA256,
        }
        log.info(
            "Recording consent granted by %s (%s)",
            record["granted_by_username"], record["granted_by_user_id"],
        )
        return record

    def _stop_recording(self) -> None:
        if not self.session:
            return
        if self._stopping:
            return
        self._set_status("Saving recording…")
        self.stop_btn.config(state=tk.DISABLED)

        # Mark stop in flight so _on_close can detect "stop already running"
        # and skip a second concurrent session.stop() call (which would
        # raise RuntimeError because RecordingSession.stop() is not
        # idempotent).
        self._stopping = True
        session = self.session

        def task():
            return session.stop()

        def on_success(result):
            self.last_result = result  # type: ignore[assignment]
            self._on_stopped(result)  # type: ignore[arg-type]

        def on_error(exc):
            messagebox.showerror("Recording", _scrub(str(exc)))
            self._after_stop()

        self._run_background(task, on_success=on_success, on_error=on_error,
                             log_label="recording stop")

    def _on_stopped(self, result: RecordingResult) -> None:
        log.info("Saved: %s", result.audio_path)
        log.info("Participants: %s", result.participants_path)
        self._after_stop()
        self._set_status(f"Saved {result.audio_path.name}")
        # Refresh and auto-select the recording we just finished so the
        # next click of Transcribe is unambiguous.
        self._refresh_recordings(select=result.audio_path)

    def _after_stop(self) -> None:
        self.session = None
        self._stopping = False
        self.start_btn.config(state=tk.NORMAL)
        self.rec_lbl.config(text="Idle", foreground="#080")

    # ---- Recording-selector helpers ----

    def _format_recording_label(self, path: Path) -> str:
        """Build a human-friendly display string for the dropdown.

        Examples of the underlying filename:
          MEP Landing Page Call_discord_20260528_003930_audio.wav
          discord_20260525_210051_audio.wav  (no meeting name)

        Result: "MEP Landing Page Call  —  2026-05-28 00:39"
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
        return f"{_scrub(meeting)}  —  {ts_pretty}"

    def _refresh_recordings(self, select: Optional[Path] = None) -> None:
        """Re-scan the recordings folder and update the dropdown.

        Newest first. If `select` is given, that path is pre-selected;
        otherwise the newest entry is selected.
        """
        try:
            wavs = sorted(
                self.config.recordings_path.glob("*_audio.wav"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception as e:
            log.warning("Could not list recordings: %s", e)
            wavs = []

        # Only show entries that have a participants.json next to them —
        # otherwise transcription will immediately fail.
        valid = [
            p for p in wavs
            if (p.parent / p.name.replace("_audio.wav", "_participants.json")).exists()
        ]

        labels: list[str] = []
        self._recordings_index = {}
        for p in valid:
            label = self._format_recording_label(p)
            # Disambiguate if two recordings somehow render to the same label
            base = label
            i = 2
            while label in self._recordings_index:
                label = f"{base} ({i})"
                i += 1
            self._recordings_index[label] = p
            labels.append(label)

        self.recording_pick["values"] = labels
        if not labels:
            self.recording_pick.set("(no recordings yet)")
            self.transcribe_btn.config(state=tk.DISABLED)
            return

        self.transcribe_btn.config(state=tk.NORMAL)
        if select is not None:
            for lbl, p in self._recordings_index.items():
                if p == select:
                    self.recording_pick.set(lbl)
                    return
        self.recording_pick.set(labels[0])

    def _transcribe_selected(self) -> None:
        label = self.recording_pick.get()
        audio_path = self._recordings_index.get(label)
        if audio_path is None:
            messagebox.showinfo("Transcribe", "Pick a recording from the dropdown first.")
            return
        participants_path = audio_path.parent / audio_path.name.replace(
            "_audio.wav", "_participants.json"
        )
        if not participants_path.exists():
            messagebox.showerror("Transcribe", f"Missing {participants_path.name}")
            return

        self.transcribe_btn.config(state=tk.DISABLED)
        self._set_status(f"Transcribing {audio_path.name}…")
        log.info("Transcribing %s", audio_path.name)

        hf_token = self.config.hf_token or None
        model_name = self.config.whisper_model

        def task():
            return transcribe(
                audio_path=audio_path,
                participants_path=participants_path,
                hf_token=hf_token,
                model_name=model_name,
                # No progress callback: transcribe() already logs each
                # step via its internal _p() helper. Passing log.info here
                # caused every line to land in the log pane twice.
            )

        def on_success(out):
            messagebox.showinfo("Transcribe", f"Saved:\n{out}")

        def on_error(exc):
            messagebox.showerror("Transcribe", _scrub(str(exc)))

        def on_finally():
            self.transcribe_btn.config(state=tk.NORMAL)
            self._set_status("Ready")

        self._run_background(task, on_success=on_success, on_error=on_error,
                             on_finally=on_finally, log_label="transcribe")

    # ---- shutdown ----

    def _on_close(self) -> None:
        """Shutdown handler wired to both WM_DELETE_WINDOW and File→Exit."""
        if self._closing:
            return
        self._closing = True

        if self.session is not None:
            try:
                ok = messagebox.askokcancel(
                    "Recording in progress",
                    "A recording is still running.\n\n"
                    "Closing now will end it. Proceed?",
                    parent=self.root,
                    icon=messagebox.WARNING,
                )
            except tk.TclError:
                ok = True
            if not ok:
                self._closing = False
                return

            # Offload stop() to a background thread. Calling it on the Tk
            # main thread froze the GUI for up to ~14s (2x5s RPC unsubscribe
            # timeouts + 4s audio teardown) when Discord was unreachable,
            # which on Windows triggered "Not Responding".
            self._set_status("Saving recording…")
            session = self.session
            self.session = None

            if self._stopping:
                # _stop_recording's background worker already owns this
                # session. Don't fire a second concurrent stop(); just wait
                # for the worker to finish and tear down then.
                self.root.after(100, self._finish_close)
                return

            self._stopping = True

            def task() -> None:
                try:
                    session.stop()
                except Exception:
                    log.exception("Session stop during shutdown raised")

            self._run_background(
                task,
                on_finally=self._finish_close,
                log_label="shutdown stop",
            )
            return

        self._finish_close()

    def _finish_close(self) -> None:
        """Tear down the RPC pipe and destroy the root.

        Always runs on the Tk main thread (either directly from _on_close
        when no session was active, or via root.after from the background
        stop's on_finally).
        """
        if self._stopping:
            # Background stop is still in flight (e.g. user clicked Stop
            # then closed the window before stop completed). Poll until it
            # finishes; we cannot block the Tk thread here.
            self.root.after(100, self._finish_close)
            return

        rpc = self.rpc
        self.rpc = None
        if rpc is not None:
            try:
                rpc.close()
            except Exception:
                log.exception("RPC close during shutdown raised")

        try:
            self.root.destroy()
        except tk.TclError:
            pass
