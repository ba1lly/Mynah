"""Unit tests for installer/bootstrap_lib.py (issue #9) — the pure
logic: GPU detection, wheel selection, resumable download, runtime
extraction safety, and install-state resume bookkeeping."""
from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

import bootstrap_lib as lib  # noqa: E402


# ---- GPU detection ----------------------------------------------------------

class TestParseDriverVersion:
    def test_simple(self):
        assert lib.parse_nvidia_driver_version("581.42\n") == 581

    def test_multi_gpu_first_wins(self):
        assert lib.parse_nvidia_driver_version("560.81\n581.42\n") == 560

    def test_garbage_returns_none(self):
        assert lib.parse_nvidia_driver_version("NVIDIA-SMI has failed") is None

    def test_empty_returns_none(self):
        assert lib.parse_nvidia_driver_version("") is None


class TestDetectGpu:
    def _run_returning(self, stdout="", returncode=0):
        def fake_run(*_a, **_k):
            return SimpleNamespace(stdout=stdout, returncode=returncode)
        return fake_run

    def test_no_nvidia_smi_means_cpu(self):
        def raise_fnf(*_a, **_k):
            raise FileNotFoundError("nvidia-smi")
        gpu = lib.detect_gpu(run=raise_fnf)
        assert gpu.has_cuda is False
        assert "CPU" in gpu.description

    def test_smi_error_means_cpu(self):
        gpu = lib.detect_gpu(run=self._run_returning(returncode=9))
        assert gpu.has_cuda is False

    def test_old_driver_means_cpu_with_upgrade_hint(self):
        gpu = lib.detect_gpu(run=self._run_returning(stdout="537.13\n"))
        assert gpu.has_cuda is False
        assert "too old" in gpu.description

    def test_current_driver_means_cuda(self):
        gpu = lib.detect_gpu(run=self._run_returning(stdout="581.42\n"))
        assert gpu.has_cuda is True
        assert "CUDA" in gpu.description

    def test_timeout_means_cpu(self):
        def raise_timeout(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=15)
        gpu = lib.detect_gpu(run=raise_timeout)
        assert gpu.has_cuda is False


class TestTorchPipArgs:
    def test_cuda_uses_extra_index(self):
        args = lib.torch_pip_args(lib.GpuInfo(True, ""))
        assert "--extra-index-url" in args
        assert lib.TORCH_CUDA_INDEX in args
        assert f"torch=={lib.TORCH_VERSION}" in args

    def test_cpu_uses_plain_pypi(self):
        args = lib.torch_pip_args(lib.GpuInfo(False, ""))
        assert "--extra-index-url" not in args


# ---- resumable download -----------------------------------------------------

class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200, length: bool = True):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))} if length else {}

    def read(self, n: int) -> bytes:
        return self._stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDownload:
    BODY = b"0123456789" * 100  # 1000 bytes
    SHA = __import__("hashlib").sha256(BODY).hexdigest()

    def test_fresh_download_verifies_and_renames(self, tmp_path: Path):
        dest = tmp_path / "file.bin"

        def opener(req, timeout=None):
            assert req.get_header("Range") is None
            return _FakeResponse(self.BODY)

        out = lib.download("http://x/f", dest, sha256=self.SHA, _opener=opener)
        assert out == dest
        assert dest.read_bytes() == self.BODY
        assert not dest.with_suffix(".bin.part").exists()

    def test_resume_sends_range_and_appends(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(self.BODY[:400])
        seen = {}

        def opener(req, timeout=None):
            seen["range"] = req.get_header("Range")
            return _FakeResponse(self.BODY[400:], status=206)

        lib.download("http://x/f", dest, sha256=self.SHA, _opener=opener)
        assert seen["range"] == "bytes=400-"
        assert dest.read_bytes() == self.BODY

    def test_server_ignoring_range_restarts_clean(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        part = tmp_path / "file.bin.part"
        part.write_bytes(b"junk-partial")

        def opener(req, timeout=None):
            return _FakeResponse(self.BODY, status=200)  # not 206

        lib.download("http://x/f", dest, sha256=self.SHA, _opener=opener)
        assert dest.read_bytes() == self.BODY

    def test_sha_mismatch_raises_and_clears_part(self, tmp_path: Path):
        dest = tmp_path / "file.bin"

        def opener(req, timeout=None):
            return _FakeResponse(b"tampered")

        with pytest.raises(lib.InstallError, match="Checksum mismatch"):
            lib.download("http://x/f", dest, sha256=self.SHA, _opener=opener)
        assert not dest.exists()
        assert not dest.with_suffix(".bin.part").exists()

    def test_existing_verified_file_short_circuits(self, tmp_path: Path):
        dest = tmp_path / "file.bin"
        dest.write_bytes(self.BODY)

        def opener(req, timeout=None):  # must never be called
            raise AssertionError("network touched for an existing file")

        out = lib.download("http://x/f", dest, sha256=self.SHA, _opener=opener)
        assert out == dest


# ---- runtime extraction -----------------------------------------------------

class TestExtractRuntime:
    def _nupkg(self, tmp_path: Path, entries: dict[str, bytes]) -> Path:
        pkg = tmp_path / "py.nupkg.zip"
        with zipfile.ZipFile(pkg, "w") as z:
            for name, data in entries.items():
                z.writestr(name, data)
        return pkg

    def test_strips_tools_prefix(self, tmp_path: Path):
        pkg = self._nupkg(tmp_path, {
            "tools/python.exe": b"exe",
            "tools/Lib/site.py": b"site",
            "metadata.xml": b"ignored",  # non-tools entries skipped
        })
        out = tmp_path / "python"
        lib.extract_runtime(pkg, out)
        assert (out / "python.exe").read_bytes() == b"exe"
        assert (out / "Lib" / "site.py").read_bytes() == b"site"
        assert not (out / "metadata.xml").exists()
        assert not (out / "tools").exists()

    def test_zip_slip_rejected(self, tmp_path: Path):
        pkg = self._nupkg(tmp_path, {"tools/../../evil.txt": b"x"})
        with pytest.raises(lib.InstallError, match="unsafe archive path"):
            lib.extract_runtime(pkg, tmp_path / "python")
        assert not (tmp_path.parent / "evil.txt").exists()


# ---- install state ----------------------------------------------------------

class TestInstallState:
    def test_roundtrip(self, tmp_path: Path):
        s = lib.InstallState(tmp_path)
        assert not s.is_done("torch-2.8.0-cuda")
        s.mark("torch-2.8.0-cuda")
        # Fresh object re-reads from disk.
        s2 = lib.InstallState(tmp_path)
        assert s2.is_done("torch-2.8.0-cuda")

    def test_corrupt_state_tolerated(self, tmp_path: Path):
        (tmp_path / lib.STATE_FILENAME).write_text("{not json", encoding="utf-8")
        s = lib.InstallState(tmp_path)
        assert s.done == set()


# ---- launcher ----------------------------------------------------------------

class TestLauncher:
    def test_launcher_sets_app_root_and_runs_main(self, tmp_path: Path):
        path = lib.write_launcher(tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "MYNAH_APP_ROOT" in text
        assert "from mynah.app import main" in text
        compile(text, str(path), "exec")  # must at least be valid Python

    def test_shortcut_targets_point_into_install(self, tmp_path: Path):
        t = lib.shortcut_targets(tmp_path)
        assert t["target"].endswith("pythonw.exe")
        assert lib.LAUNCHER_NAME in t["arguments"]
        assert t["workdir"] == str(tmp_path)
        assert t["icon"].endswith("mynah.ico")

    def test_ps_quote_escapes_single_quotes(self):
        assert lib._ps_quote("C:\\O'Brien") == "'C:\\O''Brien'"

    def test_shortcut_script_embeds_quoted_paths(self, tmp_path: Path):
        script = lib.build_shortcut_script(tmp_path / "Mynah.lnk", tmp_path)
        assert f"'{tmp_path / 'Mynah.lnk'}'" in script
        assert "$args" not in script  # regression: -Command never binds $args

    @pytest.mark.skipif(sys.platform != "win32", reason="needs PowerShell/COM")
    def test_create_shortcut_real_powershell(self, tmp_path: Path):
        """Run the REAL PowerShell + WScript.Shell COM path end to end —
        the mocked-run unit tests cannot catch -Command vs -EncodedCommand
        argument-binding bugs (one shipped; this is the regression test)."""
        lnk = tmp_path / "Mynah Test's Shortcut.lnk"  # space + apostrophe
        lib.create_shortcut(lnk, tmp_path)
        assert lnk.exists()
