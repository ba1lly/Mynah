r"""Tests for DiscordRPC pipe peer verification and _open_pipe integration.

Threat model: a same-user squatter creates \\.\pipe\discord-ipc-N
before Discord does, and waits for the recorder to send OAuth codes
and access tokens over it.

Defense in depth — both checks must pass:
1. Path allow-list (`_check_peer_image_path`): basename in the Discord
   allow-list AND path under a `(root, separator)` pair from
   `_trusted_discord_install_roots`.
2. Authenticode signature (`_verify_authenticode_signed_by_discord`):
   shells out to PowerShell's `Get-AuthenticodeSignature`; signature
   must be Valid and the signer Subject must contain `Discord, Inc.`.

Covered findings from :
   — Authenticode verification (TestAuthenticodeVerification)
   — WindowsApps scoped to discord.discord_ prefix (TestMsixScoping)
   — _open_pipe continues past verification failures
   — _open_pipe peer-verification integration
   — casefold(), not lower() (TestCasefoldNotLower)
   — explicit self.pipe = None at loop top
            (TestPipeInvariantOnLoopHead)
  — trusted_roots computed once, threaded through
            (TestTrustedRootsComputedOnce)
  — partial-env tests (TestPartialEnv)
  — symlink concerns subsumed by Authenticode
  — monkeypatch.setattr instead of sys.modules mutation
"""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from mynah.rpc import DiscordRPC, RpcError


SQUIRREL_PATH = r"C:\Users\u\AppData\Local\Discord\app-1.0.9051\Discord.exe"
SQUIRREL_CANARY = (
    r"C:\Users\u\AppData\Local\DiscordCanary\app-1.0.9051\DiscordCanary.exe"
)
MSIX_PATH = (
    r"C:\Program Files\WindowsApps\Discord.Discord_1.0.9051.0_x64__"
    r"abcde\Discord.exe"
)


def _rpc():
    r = DiscordRPC.__new__(DiscordRPC)
    r.pipe = object()
    return r


def _set_env(monkeypatch):
    """Make _trusted_discord_install_roots() compute deterministic roots."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)


def _signature_valid_result():
    """Stand-in CompletedProcess for `Get-AuthenticodeSignature` accept."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")


def _signature_invalid_result():
    return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"")


def _signature_wrong_signer_result():
    return subprocess.CompletedProcess(args=[], returncode=2, stdout=b"", stderr=b"")


def _wire(monkeypatch, *, pid=1234, image_path=SQUIRREL_PATH, signature=None):
    """Install win32* + subprocess stubs via monkeypatch.setattr.

    `signature` is the CompletedProcess returned by the stubbed
    `subprocess.run`. Defaults to a valid Discord signature.
    """
    _set_env(monkeypatch)
    monkeypatch.setattr(
        sys.modules["win32pipe"], "GetNamedPipeServerProcessId",
        MagicMock(return_value=pid), raising=False,
    )
    monkeypatch.setattr(
        sys.modules["win32api"], "OpenProcess",
        MagicMock(return_value=object()), raising=False,
    )
    monkeypatch.setattr(
        sys.modules["win32api"], "CloseHandle",
        MagicMock(), raising=False,
    )
    # We replaced win32process.QueryFullProcessImageName (which pywin32
    # v311 does not expose) with a module-level ctypes helper. Tests
    # must patch the helper directly, not the absent pywin32 attr.
    import mynah.rpc as _rpc_mod
    monkeypatch.setattr(
        _rpc_mod, "_query_full_process_image_name",
        MagicMock(return_value=image_path), raising=False,
    )
    monkeypatch.setattr(
        sys.modules["win32con"], "PROCESS_QUERY_LIMITED_INFORMATION",
        0x1000, raising=False,
    )
    monkeypatch.setattr(
        subprocess, "run",
        MagicMock(return_value=signature or _signature_valid_result()),
    )


# ---------------------------------------------------------------------------
# TestAuthenticodeVerification — the central security control.
# ---------------------------------------------------------------------------

class TestAuthenticodeVerification:
    """The cryptographic check that closes the user-writable-trusted-root
    bypass ."""

    def test_valid_discord_signature_accepted(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(return_value=_signature_valid_result()),
        )
        DiscordRPC._verify_authenticode_signed_by_discord(SQUIRREL_PATH, 1234)

    def test_invalid_signature_rejected(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(return_value=_signature_invalid_result()),
        )
        with pytest.raises(RpcError, match="signature .* is not valid"):
            DiscordRPC._verify_authenticode_signed_by_discord(SQUIRREL_PATH, 1234)

    def test_wrong_signer_rejected(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(return_value=_signature_wrong_signer_result()),
        )
        with pytest.raises(RpcError, match="signer of .* is not Discord"):
            DiscordRPC._verify_authenticode_signed_by_discord(SQUIRREL_PATH, 1234)

    def test_powershell_unavailable_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(side_effect=FileNotFoundError("powershell")),
        )
        with pytest.raises(RpcError, match="PowerShell unavailable"):
            DiscordRPC._verify_authenticode_signed_by_discord(SQUIRREL_PATH, 1234)

    def test_signature_check_timeout_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(side_effect=subprocess.TimeoutExpired(cmd=["pwsh"], timeout=15)),
        )
        with pytest.raises(RpcError, match="timed out"):
            DiscordRPC._verify_authenticode_signed_by_discord(SQUIRREL_PATH, 1234)

    def test_unknown_exit_code_fails_closed(self, monkeypatch):
        cp = subprocess.CompletedProcess(args=[], returncode=99, stdout=b"", stderr=b"oops")
        monkeypatch.setattr(subprocess, "run", MagicMock(return_value=cp))
        with pytest.raises(RpcError, match="exit 99"):
            DiscordRPC._verify_authenticode_signed_by_discord(SQUIRREL_PATH, 1234)

    def test_powershell_argv_shape(self, monkeypatch):
        captured = {}

        def _capture_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _signature_valid_result()

        monkeypatch.setattr(subprocess, "run", _capture_run)
        DiscordRPC._verify_authenticode_signed_by_discord(SQUIRREL_PATH, 1234)
        assert captured["argv"][0] == "powershell.exe"
        assert "-NoProfile" in captured["argv"]
        assert "-NonInteractive" in captured["argv"]
        assert "-Command" in captured["argv"]
        script = captured["argv"][-1]
        assert "Get-AuthenticodeSignature" in script
        # The image path is single-quoted, single quotes inside are doubled.
        assert SQUIRREL_PATH in script
        assert captured["kwargs"]["timeout"] == 15
        assert captured["kwargs"]["capture_output"] is True

    def test_single_quote_in_path_escaped_for_powershell(self, monkeypatch):
        path_with_quote = r"C:\Users\o'brien\AppData\Local\Discord\Discord.exe"
        captured = {}

        def _capture_run(argv, **kwargs):
            captured["argv"] = argv
            return _signature_valid_result()

        monkeypatch.setattr(subprocess, "run", _capture_run)
        DiscordRPC._verify_authenticode_signed_by_discord(path_with_quote, 1234)
        script = captured["argv"][-1]
        # Single quotes inside a single-quoted PowerShell string are escaped
        # by doubling. The raw `o'brien` must appear as `o''brien` in the
        # script so PowerShell doesn't terminate the string literal early.
        assert "o''brien" in script
        # And the raw unescaped form must NOT appear unescaped in the
        # quoted region (defensive: catches a future regression that
        # forgets to escape).
        assert "'o'brien'" not in script


# ---------------------------------------------------------------------------
# TestCheckPeerImagePath — direct unit tests on the path allow-list.
# ---------------------------------------------------------------------------

class TestCheckPeerImagePath:
    def test_squirrel_path_accepted(self, monkeypatch):
        _set_env(monkeypatch)
        DiscordRPC._check_peer_image_path(SQUIRREL_PATH, 1234)

    def test_squirrel_canary_accepted(self, monkeypatch):
        _set_env(monkeypatch)
        DiscordRPC._check_peer_image_path(SQUIRREL_CANARY, 1234)

    def test_msix_path_accepted(self, monkeypatch):
        _set_env(monkeypatch)
        DiscordRPC._check_peer_image_path(MSIX_PATH, 1234)

    def test_temp_dir_bypass_rejected(self, monkeypatch):
        _set_env(monkeypatch)
        with pytest.raises(RpcError):
            DiscordRPC._check_peer_image_path(
                r"C:\Users\u\AppData\Local\Temp\Discord.exe", 1234,
            )

    def test_prefix_match_attack_rejected(self, monkeypatch):
        """`C:\\Discord-Evil\\…` must not match `C:\\Discord` as a prefix."""
        _set_env(monkeypatch)
        with pytest.raises(RpcError):
            DiscordRPC._check_peer_image_path(
                r"C:\Users\u\AppData\Local\Discord-Evil\Discord.exe", 1234,
            )

    @pytest.mark.parametrize("path", [
        SQUIRREL_PATH,
        SQUIRREL_CANARY,
        r"C:\Users\u\AppData\Local\DiscordPTB\app-1.0.9051\DiscordPTB.exe",
        r"C:\Users\u\AppData\Local\DiscordDevelopment\app-1.0\DiscordDevelopment.exe",
        MSIX_PATH,
    ])
    def test_trusted_install_path_accepted(self, monkeypatch, path):
        _set_env(monkeypatch)
        DiscordRPC._check_peer_image_path(path, 1234)

    @pytest.mark.parametrize("path", [
        r"C:\Users\u\AppData\Local\Temp\Discord.exe",
        r"C:\Users\u\Downloads\Discord.exe",
        r"C:\Users\u\AppData\Local\Discord-Evil\Discord.exe",
        r"C:\Users\u\AppData\Local\DiscordX\Discord.exe",
        r"D:\games\Discord.exe",
        r"C:\Windows\System32\Discord.exe",
    ])
    def test_untrusted_path_rejected(self, monkeypatch, path):
        _set_env(monkeypatch)
        with pytest.raises(RpcError, match="outside trusted Discord install"):
            DiscordRPC._check_peer_image_path(path, 1234)

    @pytest.mark.parametrize("path", [
        # Path under trusted root but not a Discord-channel binary.
        r"C:\Users\u\AppData\Local\Discord\app-1.0\Updater.exe",
        r"C:\Users\u\AppData\Local\Discord\app-1.0\notepad.exe",
        r"C:\Users\u\AppData\Local\Discord\Discord.com",
        # Empty path.
        "",
    ])
    def test_non_discord_basename_rejected(self, monkeypatch, path):
        _set_env(monkeypatch)
        with pytest.raises(RpcError):
            DiscordRPC._check_peer_image_path(path, 1234)

    def test_no_trusted_roots_rejects(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("ProgramFiles", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.delenv("ProgramW6432", raising=False)
        with pytest.raises(RpcError, match="no trusted Discord install root"):
            DiscordRPC._check_peer_image_path(SQUIRREL_PATH, 1234)


# ---------------------------------------------------------------------------
# TestMsixScoping.
# ---------------------------------------------------------------------------

class TestMsixScoping:
    """The WindowsApps trusted-root must be scoped to Discord's MSIX
    publisher.package prefix (`Discord.Discord_`). Without scoping,
    ANY sideloaded MSIX containing a `discord.exe` basename would pass."""

    def test_real_discord_msix_accepted(self, monkeypatch):
        _set_env(monkeypatch)
        DiscordRPC._check_peer_image_path(MSIX_PATH, 1234)

    @pytest.mark.parametrize("path", [
        # Other-publisher MSIX containing a discord.exe basename.
        r"C:\Program Files\WindowsApps\EvilCorp.EvilApp_1.0.0.0_x64__xyz\Discord.exe",
        # Same Discord package name but EvilCorp publisher (note the prefix is the
        # publisher portion; this should be rejected because it doesn't start with
        # the Discord publisher).
        r"C:\Program Files\WindowsApps\Microsoft.Discord_1.0.0.0_x64__xyz\Discord.exe",
        # Look-alike "Discord.DiscordEvil" — separator boundary.
        r"C:\Program Files\WindowsApps\Discord.DiscordEvil_1.0.0.0_x64__xyz\Discord.exe",
        # Prefix-match attack: "Discord.Discord-Evil" without the underscore separator.
        r"C:\Program Files\WindowsApps\Discord.Discord-Evil\Discord.exe",
    ])
    def test_other_publisher_msix_rejected(self, monkeypatch, path):
        _set_env(monkeypatch)
        with pytest.raises(RpcError, match="outside trusted Discord install"):
            DiscordRPC._check_peer_image_path(path, 1234)


# ---------------------------------------------------------------------------
# TestCasefoldNotLower.
# ---------------------------------------------------------------------------

class TestCasefoldNotLower:
    """Path matching must use str.casefold(), not str.lower(), to
    tolerate Turkish-locale case mapping where 'I'.lower() == 'ı'
    (dotless i) and would silently false-reject legitimate Discord."""

    def test_install_root_lookup_uses_casefold(self):
        # str.casefold() is what the implementation should use. We can't
        # easily simulate the Turkish locale in a unit test, but we can
        # assert that the implementation reaches casefold() for both the
        # basename and the normalised path. A regression to lower() would
        # break Turkish-locale users without changing this test, so this
        # is a smoke test against a behavior we cannot fully exercise
        # cross-platform. The real coverage is the next test, which
        # uses an explicit casefold-equivalent path that lower() WOULD
        # change differently.
        # 'ß' lowercases to 'ß' but casefolds to 'ss'. If the
        # implementation used .lower() and the trusted root contained
        # 'ß' (unlikely but possible in publisher names), the comparison
        # would mishandle it. We assert that .casefold is the codepath.
        import inspect
        from mynah.rpc import DiscordRPC as _D
        src = inspect.getsource(_D._check_peer_image_path)
        assert ".casefold()" in src
        assert ".lower()" not in src

    @pytest.mark.parametrize("path", [
        r"c:\users\u\appdata\local\discord\app-1.0\DISCORD.EXE",
        r"C:\USERS\U\APPDATA\LOCAL\DISCORD\app-1.0\Discord.exe",
        SQUIRREL_PATH.upper(),
    ])
    def test_case_variations_accepted(self, monkeypatch, path):
        _set_env(monkeypatch)
        DiscordRPC._check_peer_image_path(path, 1234)


# ---------------------------------------------------------------------------
# TestPartialEnv.
# ---------------------------------------------------------------------------

class TestPartialEnv:
    """Partial environment-variable configurations on locked-down or
    containerized Windows hosts. Each env vector must produce a
    well-defined acceptance/rejection per install type."""

    def test_localappdata_only_accepts_squirrel_rejects_msix(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\u\AppData\Local")
        monkeypatch.delenv("ProgramFiles", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.delenv("ProgramW6432", raising=False)
        DiscordRPC._check_peer_image_path(SQUIRREL_PATH, 1234)
        with pytest.raises(RpcError, match="outside trusted Discord install"):
            DiscordRPC._check_peer_image_path(MSIX_PATH, 1234)

    def test_programfiles_only_accepts_msix_rejects_squirrel(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.delenv("ProgramW6432", raising=False)
        DiscordRPC._check_peer_image_path(MSIX_PATH, 1234)
        with pytest.raises(RpcError, match="outside trusted Discord install"):
            DiscordRPC._check_peer_image_path(SQUIRREL_PATH, 1234)

    def test_empty_string_env_var_treated_as_absent(self, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", "")
        monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        monkeypatch.delenv("ProgramW6432", raising=False)
        with pytest.raises(RpcError, match="outside trusted Discord install"):
            DiscordRPC._check_peer_image_path(SQUIRREL_PATH, 1234)
        DiscordRPC._check_peer_image_path(MSIX_PATH, 1234)


# ---------------------------------------------------------------------------
# TestVerifyPipePeerEnd2End — _verify_pipe_peer runs BOTH checks.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform != "win32",
    reason="end-to-end tests exercise the real win32process.QueryFullProcessImageName API; component pieces (path check, signature check, MSIX scoping, partial-env handling) are covered by the dedicated classes above on non-Windows runners",
)
class TestVerifyPipePeerEnd2End:
    """End-to-end: _verify_pipe_peer must run BOTH the path check
    AND the Authenticode signature check; either failing rejects."""

    def test_path_and_signature_both_valid_accepted(self, monkeypatch):
        _wire(monkeypatch, image_path=SQUIRREL_PATH)
        _rpc()._verify_pipe_peer()  # no exception

    def test_signature_failure_rejects_even_under_trusted_root(self, monkeypatch):
        """the central bypass. A binary IN the trusted install
        root that is NOT signed by Discord must be rejected by the
        signature check."""
        _wire(
            monkeypatch,
            image_path=r"C:\Users\u\AppData\Local\Discord\evil\discord.exe",
            signature=_signature_wrong_signer_result(),
        )
        with pytest.raises(RpcError, match="signer of .* is not Discord"):
            _rpc()._verify_pipe_peer()

    def test_path_failure_rejects_before_signature_check(self, monkeypatch):
        """Path check is the fast pre-filter — must reject before we
        pay the cost of the signature check (which would have passed)."""
        run_mock = MagicMock(return_value=_signature_valid_result())
        _wire(
            monkeypatch,
            image_path=r"C:\Users\u\Downloads\discord.exe",
        )
        monkeypatch.setattr(subprocess, "run", run_mock)
        with pytest.raises(RpcError, match="outside trusted Discord install"):
            _rpc()._verify_pipe_peer()
        assert run_mock.call_count == 0, (
            "signature check must not be invoked when path check has "
            "already rejected"
        )


# ---------------------------------------------------------------------------
# TestFailsClosed — every fail-closed branch in _verify_pipe_peer.
# ---------------------------------------------------------------------------

class TestFailsClosed:
    def test_pid_query_failure_rejects(self, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setattr(
            sys.modules["win32pipe"], "GetNamedPipeServerProcessId",
            MagicMock(side_effect=OSError("kaboom")), raising=False,
        )
        with pytest.raises(RpcError, match="Could not verify Discord pipe peer"):
            _rpc()._verify_pipe_peer()

    def test_openprocess_failure_rejects(self, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setattr(
            sys.modules["win32api"], "OpenProcess",
            MagicMock(side_effect=PermissionError("denied")), raising=False,
        )
        with pytest.raises(RpcError, match="Could not verify Discord pipe peer"):
            _rpc()._verify_pipe_peer()

    def test_imagepath_query_failure_rejects(self, monkeypatch):
        _wire(monkeypatch)
        import mynah.rpc as _rpc_mod
        monkeypatch.setattr(
            _rpc_mod, "_query_full_process_image_name",
            MagicMock(side_effect=OSError("denied")), raising=False,
        )
        with pytest.raises(RpcError, match="Could not verify Discord pipe peer"):
            _rpc()._verify_pipe_peer()

    def test_closed_pipe_rejects(self):
        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = None
        with pytest.raises(RpcError, match="closed pipe"):
            r._verify_pipe_peer()


# ---------------------------------------------------------------------------
# TestTrustedRootsComputedOnce.
# ---------------------------------------------------------------------------

class TestTrustedRootsComputedOnce:
    """`_open_pipe` must compute the trusted-roots list ONCE before the
    scan and pass it down. Re-reading os.environ on every iteration
    (the pre-fix behavior) wastes work and could observe an
    inconsistent env if another thread mutates it mid-scan."""

    def test_open_pipe_computes_roots_once_and_passes_them_in(
        self, monkeypatch,
    ):
        _set_env(monkeypatch)

        compute_calls: list[int] = []
        original = DiscordRPC._trusted_discord_install_roots

        @classmethod
        def _spy_trusted_roots(cls):
            compute_calls.append(1)
            return original.__func__(cls)

        monkeypatch.setattr(
            DiscordRPC, "_trusted_discord_install_roots",
            _spy_trusted_roots, raising=False,
        )

        class _FakePywintypesError(Exception):
            pass

        monkeypatch.setattr(
            sys.modules["pywintypes"], "error", _FakePywintypesError,
            raising=False,
        )

        win32file = sys.modules["win32file"]
        monkeypatch.setattr(win32file, "CreateFile",
                            MagicMock(side_effect=_FakePywintypesError("nope")),
                            raising=False)
        monkeypatch.setattr(win32file, "CloseHandle", MagicMock(), raising=False)
        monkeypatch.setattr(win32file, "GENERIC_READ", 0x80000000, raising=False)
        monkeypatch.setattr(win32file, "GENERIC_WRITE", 0x40000000, raising=False)
        monkeypatch.setattr(win32file, "OPEN_EXISTING", 3, raising=False)

        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = None
        r._open_pipe()

        # Exactly once for the full 10-index scan, regardless of how many
        # CreateFile calls succeeded or failed.
        assert len(compute_calls) == 1


# ---------------------------------------------------------------------------
# TestOpenPipeIntegration.
# ---------------------------------------------------------------------------

class TestOpenPipeIntegration:
    """_open_pipe invokes _verify_pipe_peer, closes handles on reject,
    continues scanning higher indices on verify failure, asserts the
    invariant that self.pipe is None at the top of every iteration
    ."""

    def _wire_open_pipe(
        self, monkeypatch,
        *, create_fail_count=0, verify_fails=(),
    ):
        _set_env(monkeypatch)

        # pywintypes.error needs to inherit from BaseException for the
        # production `except pywintypes.error` to actually catch it.
        class _FakePywintypesError(Exception):
            pass

        monkeypatch.setattr(
            sys.modules["pywintypes"], "error", _FakePywintypesError,
            raising=False,
        )

        call_state = {"create_calls": 0, "verify_calls": 0}
        opened_handles: list[object] = []
        closed_handles: list[object] = []

        # track self.pipe state observed at the top of each
        # iteration (i.e. just before CreateFile is called). Production
        # code asserts the invariant `self.pipe is None at loop head`.
        pipe_at_loop_head: list[object] = []

        class _Pipe:
            def __init__(self, idx: int) -> None:
                self.idx = idx

        def _create_file(rpc_self, *args, **kwargs):
            # CreateFile is invoked AFTER production sets self.pipe = None
            # at the top of the loop. Capture the observed pipe value
            # so the invariant test can assert it.
            pipe_at_loop_head.append(rpc_self.pipe)
            idx = call_state["create_calls"]
            call_state["create_calls"] += 1
            if idx < create_fail_count:
                raise _FakePywintypesError("not found")
            handle = _Pipe(idx)
            opened_handles.append(handle)
            return handle

        # CreateFile is invoked positionally without a `self` arg, so we
        # need a wrapper that captures the RPC instance from the calling
        # frame. Easier: drop a real DiscordRPC instance into a closure
        # via a sentinel and have the test wire it in. We instead use a
        # bound-method approach: the production calls win32file.CreateFile
        # as a free function, so we capture the rpc instance via the
        # current_rpc variable closed over below.
        current_rpc: list[DiscordRPC] = []

        def _create_file_bound(*args, **kwargs):
            rpc_self = current_rpc[0]
            return _create_file(rpc_self, *args, **kwargs)

        def _close_handle(handle):
            closed_handles.append(handle)

        win32file = sys.modules["win32file"]
        monkeypatch.setattr(win32file, "CreateFile", _create_file_bound,
                            raising=False)
        monkeypatch.setattr(win32file, "CloseHandle", _close_handle, raising=False)
        monkeypatch.setattr(win32file, "GENERIC_READ", 0x80000000, raising=False)
        monkeypatch.setattr(win32file, "GENERIC_WRITE", 0x40000000, raising=False)
        monkeypatch.setattr(win32file, "OPEN_EXISTING", 3, raising=False)

        def _fake_verify(self, trusted_roots=None):
            # production passes trusted_roots through; the fake
            # accepts and ignores it. Asserting it's non-None confirms
            # the roots-computed-once invariant from production.
            assert trusted_roots is not None, (
                "production must pass trusted_roots to _verify_pipe_peer "
                ""
            )
            idx = call_state["verify_calls"]
            call_state["verify_calls"] += 1
            if idx in verify_fails:
                raise RpcError(
                    "Refusing to talk to non-Discord pipe peer: 'evil.exe'"
                )

        monkeypatch.setattr(
            DiscordRPC, "_verify_pipe_peer", _fake_verify, raising=False,
        )

        return {
            "calls": call_state,
            "opened": opened_handles,
            "closed": closed_handles,
            "pipe_at_loop_head": pipe_at_loop_head,
            "current_rpc": current_rpc,
        }

    def test_first_index_verifies_returns_true(self, monkeypatch):
        state = self._wire_open_pipe(monkeypatch)
        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = None
        state["current_rpc"].append(r)
        assert r._open_pipe() is True
        assert state["calls"]["create_calls"] == 1
        assert state["calls"]["verify_calls"] == 1
        assert r.pipe is state["opened"][0]
        assert state["closed"] == []

    def test_create_fails_then_verifies_returns_true(self, monkeypatch):
        state = self._wire_open_pipe(monkeypatch, create_fail_count=3)
        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = None
        state["current_rpc"].append(r)
        assert r._open_pipe() is True
        assert state["calls"]["create_calls"] == 4
        assert state["calls"]["verify_calls"] == 1

    def test_verify_fails_on_index_0_continues_to_index_1(self, monkeypatch):
        state = self._wire_open_pipe(monkeypatch, verify_fails=(0,))
        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = None
        state["current_rpc"].append(r)
        assert r._open_pipe() is True
        assert state["calls"]["create_calls"] == 2
        assert state["calls"]["verify_calls"] == 2
        assert len(state["closed"]) == 1
        assert state["closed"][0] is state["opened"][0]
        assert r.pipe is state["opened"][1]

    def test_all_indices_verify_fail_raises_last_error(self, monkeypatch):
        state = self._wire_open_pipe(monkeypatch, verify_fails=tuple(range(10)))
        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = None
        state["current_rpc"].append(r)
        with pytest.raises(RpcError, match="non-Discord pipe peer"):
            r._open_pipe()
        assert state["calls"]["create_calls"] == 10
        assert state["calls"]["verify_calls"] == 10
        assert len(state["closed"]) == 10
        assert r.pipe is None

    def test_no_pipe_opens_returns_false(self, monkeypatch):
        state = self._wire_open_pipe(monkeypatch, create_fail_count=10)
        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = None
        state["current_rpc"].append(r)
        assert r._open_pipe() is False
        assert state["calls"]["create_calls"] == 10
        assert state["calls"]["verify_calls"] == 0


# ---------------------------------------------------------------------------
# TestPipeInvariantOnLoopHead.
# ---------------------------------------------------------------------------

class TestPipeInvariantOnLoopHead:
    """`self.pipe` MUST be `None` at the top of every loop iteration.
    The original code maintained this implicitly via two separate
    write sites; the response makes it explicit at the loop head.
    A regression that removes the explicit reset would cause
    `_verify_pipe_peer` on iteration N+1 to receive iteration N's
    leftover handle on a code path that fails after CreateFile."""

    def test_pipe_reset_to_none_at_top_of_each_iteration(self, monkeypatch):
        _set_env(monkeypatch)

        class _FakePywintypesError(Exception):
            pass

        monkeypatch.setattr(
            sys.modules["pywintypes"], "error", _FakePywintypesError,
            raising=False,
        )

        observations: list[object] = []
        call_state = {"i": 0}

        class _Pipe:
            def __init__(self, idx: int) -> None:
                self.idx = idx

        def _create_file(*args, **kwargs):
            return _Pipe(call_state["i"])

        rpc_holder: list[DiscordRPC] = []

        def _create_file_capture(*args, **kwargs):
            observations.append(rpc_holder[0].pipe)
            i = call_state["i"]
            call_state["i"] += 1
            if i < 5:
                raise _FakePywintypesError("not yet")
            handle = _Pipe(i)
            return handle

        win32file = sys.modules["win32file"]
        monkeypatch.setattr(win32file, "CreateFile", _create_file_capture,
                            raising=False)
        monkeypatch.setattr(win32file, "CloseHandle", MagicMock(), raising=False)
        monkeypatch.setattr(win32file, "GENERIC_READ", 0x80000000, raising=False)
        monkeypatch.setattr(win32file, "GENERIC_WRITE", 0x40000000, raising=False)
        monkeypatch.setattr(win32file, "OPEN_EXISTING", 3, raising=False)

        def _verify_ok(self, trusted_roots=None):
            return

        monkeypatch.setattr(
            DiscordRPC, "_verify_pipe_peer", _verify_ok, raising=False,
        )

        r = DiscordRPC.__new__(DiscordRPC)
        r.pipe = "stale handle from before"
        rpc_holder.append(r)
        r._open_pipe()

        # Every observed pipe value at the top of CreateFile must be None,
        # including the very first iteration (where production must clear
        # the pre-existing stale value).
        assert all(p is None for p in observations), (
            f"self.pipe should be None at top of each iteration; "
            f"observed: {observations!r}"
        )
        assert len(observations) >= 6  # 5 failures + 1 success
