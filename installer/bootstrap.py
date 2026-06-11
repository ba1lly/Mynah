"""MynahSetup — one-click bootstrap installer GUI (issue #9).

A small tkinter window around bootstrap_lib: pick a folder, click
Install, watch progress. Every step is idempotent and state-tracked, so
clicking Install after a failure resumes where it left off.

Built into a single ~15 MB MynahSetup.exe by installer/MynahSetup.spec
(the mynah wheel rides inside the bundle). The multi-GB ML stack is
downloaded on the user's machine — that's the whole point: the download
from GitHub stays tiny.
"""
from __future__ import annotations

import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import bootstrap_lib as lib

APP_TITLE = "Mynah Setup"

# (state_key_prefix, human label, weight for the progress bar)
# State keys embed pinned versions so a NEWER MynahSetup run over an old
# install re-runs exactly the steps whose pins changed (cheap upgrades).
STEPS = [
    ("runtime", "Python runtime", 1),
    ("torch", "PyTorch (the big one)", 6),
    ("whisperx", "WhisperX + transcription stack", 3),
    ("app", "Mynah", 1),
    ("launcher", "Launcher + shortcuts", 1),
]


class InstallerWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("620x460")
        root.resizable(False, False)
        try:
            icon = Path(__file__).resolve().parent / "mynah.ico"
            if not icon.exists():
                import sys

                meipass = getattr(sys, "_MEIPASS", None)
                if meipass:
                    icon = Path(meipass) / "mynah.ico"
            if icon.exists():
                root.iconbitmap(str(icon))
        except Exception:
            pass

        self._working = False
        self.gpu = lib.detect_gpu()

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frm, text="Mynah", font=("Segoe UI", 16, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            frm,
            text="Records Discord calls locally and transcribes them with "
                 "per-speaker labels.\nNothing leaves your machine.",
        ).pack(anchor=tk.W, pady=(0, 8))

        dir_row = ttk.Frame(frm)
        dir_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(dir_row, text="Install to:").pack(side=tk.LEFT)
        self.dir_var = tk.StringVar(value=str(lib.DEFAULT_INSTALL_DIR))
        self.dir_entry = ttk.Entry(dir_row, textvariable=self.dir_var, width=52)
        self.dir_entry.pack(side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        self.browse_btn = ttk.Button(dir_row, text="…", width=3, command=self._pick_dir)
        self.browse_btn.pack(side=tk.LEFT)

        self.desktop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, text="Also create a Desktop shortcut", variable=self.desktop_var,
        ).pack(anchor=tk.W, pady=(6, 0))

        ttk.Label(frm, text=self.gpu.description, foreground="#555").pack(
            anchor=tk.W, pady=(6, 0),
        )

        self.install_btn = ttk.Button(
            frm, text="Install", command=self._start, width=24,
        )
        self.install_btn.pack(pady=10)

        self.progress = ttk.Progressbar(frm, maximum=1000)
        self.progress.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frm, textvariable=self.status_var).pack(anchor=tk.W, pady=(4, 4))

        self.log = scrolledtext.ScrolledText(
            frm, height=10, state=tk.DISABLED, font=("Consolas", 8),
        )
        self.log.pack(fill=tk.BOTH, expand=True)

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI helpers (thread-safe via after()) ----

    def _log(self, line: str) -> None:
        def _do():
            self.log.configure(state=tk.NORMAL)
            self.log.insert(tk.END, line + "\n")
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
        self.install_btn.configure(state=tk.DISABLED, text="Installing…")
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
            self._log(f"ERROR: {msg}")
            self._set_status("Failed — click Install to resume.")
            self.root.after(0, self._reset_button)
            self.root.after(0, lambda m=msg: messagebox.showerror(APP_TITLE, m))
        except Exception as e:  # noqa: BLE001
            msg = f"Unexpected error:\n{e!r}"
            self._log(f"UNEXPECTED ERROR: {e!r}")
            self._set_status("Failed — click Install to resume.")
            self.root.after(0, self._reset_button)
            self.root.after(0, lambda m=msg: messagebox.showerror(APP_TITLE, m))
        else:
            self._set_progress(1.0)
            self._set_status("Done.")
            self.root.after(0, self._finished)

    def _reset_button(self) -> None:
        self._working = False
        self.install_btn.configure(state=tk.NORMAL, text="Install")
        self.dir_entry.configure(state=tk.NORMAL)
        self.browse_btn.configure(state=tk.NORMAL)

    def _finished(self) -> None:
        self._working = False
        self.install_btn.configure(text="Installed ✓")
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
        state = lib.InstallState(install_dir)
        wheel = lib.bundled_wheel()

        # Disk-space sanity: CUDA stack lands ~7 GB installed (+ model
        # cache later); CPU ~3 GB.
        free_gb = shutil.disk_usage(install_dir).free / 1e9
        need_gb = 12 if self.gpu.has_cuda else 5
        if free_gb < need_gb:
            raise lib.InstallError(
                f"Not enough disk space: {free_gb:.1f} GB free, "
                f"~{need_gb} GB needed on this drive."
            )

        keys = {
            "runtime": f"runtime-{lib.PYTHON_VERSION}",
            "torch": (
                f"torch-{lib.TORCH_VERSION}-"
                f"{'cuda' if self.gpu.has_cuda else 'cpu'}"
            ),
            "whisperx": f"whisperx-{lib.WHISPERX_SPEC}",
            "app": f"app-{wheel.name}",
            "launcher": f"launcher-{wheel.name}",
        }
        total_weight = sum(w for _, _, w in STEPS)
        done_weight = 0

        for step_id, label, weight in STEPS:
            key = keys[step_id]
            base = done_weight / total_weight
            if state.is_done(key):
                self._log(f"[skip] {label} (already done)")
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
                # Wheel deps resolve normally (not --no-deps) the first
                # time; --no-deps above is for the reinstall-on-upgrade
                # path. Run a plain install too so missing deps land.
                lib.run_pip(
                    install_dir, ["install", str(wheel)], on_line=self._log,
                )
            elif step_id == "launcher":
                self._step_launcher(install_dir)

            state.mark(key)
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
        self._log(f"downloaded {nupkg.name}")
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
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    InstallerWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
