"""MynahSetup — one-click bootstrap installer GUI (issue #9).

A dark, brand-styled tkinter window around bootstrap_lib: pick a
folder, click Install, watch progress. Styling is plain-tk with
explicit colors (matching the app's broadcast-console look) because
ttk themes can't be restyled this far; the only ttk widget is the
progress bar under a custom clam-based style.

Every step is idempotent and state-tracked: the window shows BEFORE you
click whether this folder already has a complete or partial install,
completed steps are skipped instantly, and even with lost state pip
re-runs are cheap (satisfied pins are not re-downloaded).

Built into a single ~15 MB MynahSetup.exe by installer/MynahSetup.spec
(the mynah wheel rides inside the bundle). The multi-GB ML stack is
downloaded on the user's machine — that's the whole point: the download
from GitHub stays tiny.
"""
from __future__ import annotations

import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import bootstrap_lib as lib

APP_TITLE = "Mynah Setup"

# ---- palette (mirrors the app's dark theme) ----
BG = "#131316"
PANEL = "#1b1b20"
PANEL_2 = "#0f0f12"
LINE = "#2b2b33"
TEXT = "#e9e7e1"
MUTED = "#8d8c96"
FAINT = "#5b5a64"
ACCENT = "#f0a32e"
ACCENT_HOVER = "#f7b54e"
ACCENT_INK = "#16130c"
OK = "#46d17c"
ERR = "#ff6b6b"

F_DISPLAY = ("Bahnschrift SemiBold Condensed", 22)
F_SUB = ("Bahnschrift Light", 10)
F_LABEL = ("Bahnschrift", 9)
F_BTN = ("Bahnschrift SemiBold Condensed", 13)
F_MONO = ("Cascadia Mono", 8)
F_MONO_S = ("Cascadia Mono", 9)


def _asset(name: str) -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / name
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "assets" / name


class InstallerWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("660x540")
        root.resizable(False, False)
        root.configure(bg=BG)
        try:
            ico = _asset("mynah.ico")
            if ico.exists():
                root.iconbitmap(str(ico))
        except Exception:
            pass

        self._working = False
        self.gpu = lib.detect_gpu()
        try:
            self._wheel_name = lib.bundled_wheel().name
        except lib.InstallError:
            self._wheel_name = "mynah-unknown.whl"

        # custom progressbar style (the one ttk widget)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(
            "Mynah.Horizontal.TProgressbar",
            troughcolor=PANEL, bordercolor=BG,
            background=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
            thickness=6,
        )

        outer = tk.Frame(root, bg=BG)
        outer.pack(fill=tk.BOTH, expand=True, padx=22, pady=(18, 14))

        # ---- header ----
        head = tk.Frame(outer, bg=BG)
        head.pack(fill=tk.X)
        try:
            self._logo = tk.PhotoImage(file=str(_asset("mynah-logo.png")))
            tk.Label(head, image=self._logo, bg=BG).pack(side=tk.LEFT, padx=(0, 12))
        except Exception:
            pass
        title_col = tk.Frame(head, bg=BG)
        title_col.pack(side=tk.LEFT)
        tk.Label(
            title_col, text="M Y N A H", font=F_DISPLAY, fg=TEXT, bg=BG,
        ).pack(anchor=tk.W)
        tk.Label(
            title_col,
            text="Records Discord calls locally · transcribes with "
                 "per-speaker labels · nothing leaves your machine",
            font=F_SUB, fg=MUTED, bg=BG,
            wraplength=520, justify=tk.LEFT,
        ).pack(anchor=tk.W)

        tk.Frame(outer, bg=LINE, height=1).pack(fill=tk.X, pady=(14, 14))

        # ---- install dir ----
        tk.Label(
            outer, text="INSTALL TO", font=F_LABEL, fg=FAINT, bg=BG,
        ).pack(anchor=tk.W)
        dir_row = tk.Frame(outer, bg=BG)
        dir_row.pack(fill=tk.X, pady=(4, 0))
        self.dir_var = tk.StringVar(value=str(lib.DEFAULT_INSTALL_DIR))
        self.dir_var.trace_add("write", lambda *_: self._refresh_existing())
        self.dir_entry = tk.Entry(
            dir_row, textvariable=self.dir_var,
            font=F_MONO_S, fg=TEXT, bg=PANEL, insertbackground=ACCENT,
            relief=tk.FLAT, highlightthickness=1,
            highlightbackground=LINE, highlightcolor=ACCENT,
        )
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)
        self.browse_btn = tk.Button(
            dir_row, text="…", command=self._pick_dir,
            font=F_LABEL, fg=MUTED, bg=PANEL, activebackground=LINE,
            activeforeground=TEXT, relief=tk.FLAT, width=4, cursor="hand2",
        )
        self.browse_btn.pack(side=tk.LEFT, padx=(6, 0), ipady=4)

        self.desktop_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            outer, text="Also create a Desktop shortcut",
            variable=self.desktop_var,
            font=F_SUB, fg=MUTED, bg=BG, activebackground=BG,
            activeforeground=TEXT, selectcolor=PANEL, relief=tk.FLAT,
            highlightthickness=0, cursor="hand2",
        ).pack(anchor=tk.W, pady=(10, 0))

        # ---- detection lines ----
        info = tk.Frame(outer, bg=PANEL, highlightthickness=1,
                        highlightbackground=LINE)
        info.pack(fill=tk.X, pady=(12, 0))
        self.gpu_lbl = tk.Label(
            info, text=("●  " + self.gpu.description), font=F_MONO,
            fg=(OK if self.gpu.has_cuda else MUTED), bg=PANEL,
            anchor=tk.W, justify=tk.LEFT, wraplength=560,
        )
        self.gpu_lbl.pack(anchor=tk.W, padx=10, pady=(8, 2))
        self.existing_lbl = tk.Label(
            info, text="", font=F_MONO, fg=MUTED, bg=PANEL,
            anchor=tk.W, justify=tk.LEFT, wraplength=560,
        )
        self.existing_lbl.pack(anchor=tk.W, padx=10, pady=(0, 8))

        # ---- install button ----
        self.install_btn = tk.Button(
            outer, text="INSTALL", command=self._start,
            font=F_BTN, fg=ACCENT_INK, bg=ACCENT,
            activebackground=ACCENT_HOVER, activeforeground=ACCENT_INK,
            disabledforeground="#7a6a45",
            relief=tk.FLAT, cursor="hand2",
        )
        self.install_btn.pack(fill=tk.X, pady=(14, 12), ipady=8)

        # ---- progress + status ----
        self.progress = ttk.Progressbar(
            outer, maximum=1000, style="Mynah.Horizontal.TProgressbar",
        )
        self.progress.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(
            outer, textvariable=self.status_var, font=F_MONO, fg=MUTED,
            bg=BG, anchor=tk.W,
        ).pack(fill=tk.X, pady=(6, 6))

        # ---- log ----
        self.log = tk.Text(
            outer, height=9, state=tk.DISABLED,
            font=F_MONO, fg=MUTED, bg=PANEL_2, relief=tk.FLAT,
            highlightthickness=1, highlightbackground=LINE,
            insertbackground=ACCENT, wrap=tk.WORD,
        )
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("ok", foreground=OK)
        self.log.pack(fill=tk.BOTH, expand=True)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_existing()

    # ---- pre-flight detection ----

    def _refresh_existing(self) -> None:
        """Reflect the chosen folder's install state before any click."""
        try:
            install_dir = Path(self.dir_var.get()).expanduser()
        except (ValueError, OSError):
            return
        keys = lib.step_keys(self.gpu, self._wheel_name)
        try:
            pending = lib.pending_steps(install_dir, keys)
        except OSError:
            pending = [s for s, _, _ in lib.STEPS]
        total = len(lib.STEPS)
        if not install_dir.exists() or len(pending) == total:
            self.existing_lbl.config(
                text="●  Fresh install — nothing here yet.", fg=MUTED,
            )
            self.install_btn.config(text="INSTALL")
        elif not pending:
            self.existing_lbl.config(
                text="●  Existing installation found — everything is "
                     "up to date. Nothing will be re-downloaded.",
                fg=OK,
            )
            self.install_btn.config(text="VERIFY / REPAIR")
        else:
            done = total - len(pending)
            labels = [lbl for sid, lbl, _ in lib.STEPS if sid in pending]
            self.existing_lbl.config(
                text=f"●  Partial install found ({done}/{total} steps "
                     f"done) — will resume with: {', '.join(labels)}.",
                fg=ACCENT,
            )
            self.install_btn.config(text="RESUME INSTALL")

    # ---- UI helpers (thread-safe via after()) ----

    def _log(self, line: str, tag: str = "") -> None:
        def _do():
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, line + "\n", tag or ())
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        self.root.after(0, _do)

    def _set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def _set_progress(self, fraction: float) -> None:
        self.root.after(0, lambda: self.progress.configure(
            value=max(0, min(1000, int(fraction * 1000)))
        ))

    def _pick_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
        if chosen:
            self.dir_var.set(str(Path(chosen) / "Mynah"))

    def _on_close(self) -> None:
        if self._working:
            if not messagebox.askokcancel(
                APP_TITLE,
                "Setup is still running. Quit anyway?\n\n"
                "(Progress is saved — re-running the installer resumes.)",
            ):
                return
        self.root.destroy()

    # ---- install flow ----

    def _start(self) -> None:
        if self._working:
            return
        try:
            install_dir = Path(self.dir_var.get()).expanduser()
        except (ValueError, OSError):
            messagebox.showerror(APP_TITLE, "Invalid install folder.")
            return
        self._working = True
        self.install_btn.configure(state=tk.DISABLED, text="INSTALLING…")
        self.dir_entry.configure(state=tk.DISABLED)
        self.browse_btn.configure(state=tk.DISABLED)
        threading.Thread(
            target=self._install, args=(install_dir,), daemon=True,
        ).start()

    def _install(self, install_dir: Path) -> None:
        try:
            self._run_steps(install_dir)
        except lib.InstallError as e:
            # Bind the message now: the except-variable is unbound by the
            # time the after() callback fires on the Tk thread.
            msg = str(e)
            self._log(f"ERROR: {msg}", "err")
            self._set_status("Failed — click the button to resume.")
            self.root.after(0, self._reset_button)
            self.root.after(0, lambda m=msg: messagebox.showerror(APP_TITLE, m))
        except Exception as e:  # noqa: BLE001
            msg = f"Unexpected error:\n{e!r}"
            self._log(f"UNEXPECTED ERROR: {e!r}", "err")
            self._set_status("Failed — click the button to resume.")
            self.root.after(0, self._reset_button)
            self.root.after(0, lambda m=msg: messagebox.showerror(APP_TITLE, m))
        else:
            self._set_progress(1.0)
            self._set_status("Done.")
            self.root.after(0, self._finished)

    def _reset_button(self) -> None:
        self._working = False
        self.dir_entry.configure(state=tk.NORMAL)
        self.browse_btn.configure(state=tk.NORMAL)
        self.install_btn.configure(state=tk.NORMAL)
        self._refresh_existing()

    def _finished(self) -> None:
        self._working = False
        self.install_btn.configure(text="INSTALLED ✓")
        self._log("install complete", "ok")
        self._refresh_existing()
        self.install_btn.configure(state=tk.NORMAL)
        messagebox.showinfo(
            APP_TITLE,
            "Mynah is installed.\n\n"
            "Launch it from the Start Menu (or the Desktop shortcut).\n\n"
            "First-time setup inside the app: Settings → paste your "
            "Discord application's Client ID (see the README's "
            "'Discord setup' — 2 minutes).",
        )

    def _run_steps(self, install_dir: Path) -> None:
        install_dir.mkdir(parents=True, exist_ok=True)
        wheel = lib.bundled_wheel()
        keys = lib.step_keys(self.gpu, wheel.name)
        pending = set(lib.pending_steps(install_dir, keys))
        state = lib.InstallState(install_dir)

        # Disk-space sanity, scaled to what actually remains to install.
        if "torch" in pending:
            free_gb = shutil.disk_usage(install_dir).free / 1e9
            need_gb = 12 if self.gpu.has_cuda else 5
            if free_gb < need_gb:
                raise lib.InstallError(
                    f"Not enough disk space: {free_gb:.1f} GB free, "
                    f"~{need_gb} GB needed on this drive."
                )

        total_weight = sum(w for _, _, w in lib.STEPS)
        done_weight = 0

        for step_id, label, weight in lib.STEPS:
            base = done_weight / total_weight
            if step_id not in pending:
                self._log(f"[skip] {label} — already installed")
                done_weight += weight
                self._set_progress(done_weight / total_weight)
                continue
            self._set_status(f"{label}…")
            self._log(f"[step] {label}")

            if step_id == "runtime":
                self._step_runtime(install_dir, base, weight / total_weight)
            elif step_id == "torch":
                self._log(self.gpu.description)
                lib.run_pip(
                    install_dir, lib.torch_pip_args(self.gpu),
                    on_line=self._log,
                )
            elif step_id == "whisperx":
                lib.run_pip(
                    install_dir, ["install", lib.WHISPERX_SPEC],
                    on_line=self._log,
                )
            elif step_id == "app":
                lib.run_pip(
                    install_dir,
                    ["install", "--force-reinstall", "--no-deps", str(wheel)],
                    on_line=self._log,
                )
                # Deps resolve in a second, plain pass (the forced pass
                # is --no-deps so an upgrade never re-downloads torch).
                lib.run_pip(
                    install_dir, ["install", str(wheel)], on_line=self._log,
                )
            elif step_id == "launcher":
                self._step_launcher(install_dir)

            state.mark(keys[step_id])
            done_weight += weight
            self._set_progress(done_weight / total_weight)

    def _step_runtime(self, install_dir: Path, base: float, span: float) -> None:
        downloads = install_dir / "downloads"
        nupkg = downloads / f"python-{lib.PYTHON_VERSION}.nupkg.zip"

        def on_progress(done: int, total) -> None:
            if total:
                self._set_progress(base + span * 0.8 * (done / total))
                self._set_status(
                    f"Python runtime… {done / 1e6:.0f} / {total / 1e6:.0f} MB"
                )

        lib.download(
            lib.PYTHON_NUPKG_URL, nupkg,
            sha256=lib.PYTHON_NUPKG_SHA256, progress=on_progress,
        )
        self._log(f"downloaded {nupkg.name} (sha256 verified)")
        self._set_status("Extracting Python runtime…")
        lib.extract_runtime(nupkg, install_dir / "python")
        self._set_status("Setting up pip…")
        import subprocess

        proc = subprocess.run(
            [str(lib.python_exe(install_dir)), "-m", "ensurepip"],
            capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            raise lib.InstallError(
                f"pip setup failed:\n{(proc.stderr or proc.stdout).strip()}"
            )
        self._log("python runtime ready")

    def _step_launcher(self, install_dir: Path) -> None:
        lib.write_launcher(install_dir)
        lib.create_shortcut(
            lib.start_menu_dir() / "Mynah.lnk", install_dir,
        )
        self._log("Start Menu shortcut created")
        if self.desktop_var.get():
            desktop = Path.home() / "Desktop"
            lib.create_shortcut(desktop / "Mynah.lnk", install_dir)
            self._log("Desktop shortcut created")


def main() -> int:
    root = tk.Tk()
    InstallerWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
