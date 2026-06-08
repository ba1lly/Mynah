"""Discord RPC client with full OAuth (AUTHORIZE + AUTHENTICATE) flow.

Talks to Discord's local named pipe (\\\\.\\pipe\\discord-ipc-*) and exchanges
the AUTHORIZE code for an OAuth token at https://discord.com/api/oauth2/token.

Required scopes for participant tracking + speaking events: rpc,
rpc.voice.read, identify. The client_id must be a real Discord Application
ID. The user running this app must be listed as a tester on the Discord
developer portal application unless the app is publicly approved.

Threading model
---------------
After AUTHENTICATE succeeds, a background reader thread takes over the pipe
and dispatches each incoming frame either to:
  - a pending synchronous command (matched by nonce), or
  - a registered event listener (matched by evt name).

This is what lets us hold subscriptions (SPEAKING_START / SPEAKING_STOP /
VOICE_STATE_*) while still issuing synchronous queries.

Protocol opcodes
----------------
Discord IPC sends control frames in addition to FRAME (JSON command/event)
payloads. We MUST answer PING with PONG; without it Discord will sever the
pipe after a liveness timeout, which manifests as the recorder appearing to
"randomly stop connecting" after some idle minutes.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import threading
import time
import uuid
from typing import Callable, Optional, Tuple

import requests

log = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
REDIRECT_URI = "http://localhost"
SCOPES = ["rpc", "rpc.voice.read", "identify"]

# Hard cap on a single IPC frame. Discord IPC payloads are JSON and are
# always small in practice (kilobytes). Anything larger is a hostile peer or
# a corrupted pipe and must be refused before we try to allocate.
_MAX_FRAME_BYTES = 1 << 20  # 1 MiB


class RpcError(RuntimeError):
    pass


class _PipeReadIdle(RpcError):
    """Distinguishable timeout used by the reader loop to poll the pipe
    without holding it in a permanent blocking ReadFile.

    Windows named pipes opened without FILE_FLAG_OVERLAPPED serialise
    I/O: only one operation can be outstanding at a time on the handle.
    If the reader thread sits in a blocking ReadFile waiting for the
    next frame, every concurrent WriteFile from a command call queues
    behind it forever — Discord never speaks first, so the read never
    completes, so the write never starts. Result: every RPC command
    after _start_reader() deadlocks instantly.

    Fix: the reader passes a short deadline to _read_frame() and treats
    a deadline-expiry as "no data this tick, keep looping". Between
    ticks the pipe has no outstanding I/O, so writes can interleave.
    Other RpcError subclasses still mean the pipe is genuinely dead.
    """


def _query_full_process_image_name(handle: int, pid: int) -> str:
    """Return the full image path of a process given an open handle.

    Calls Win32 ``QueryFullProcessImageNameW`` via ctypes. We avoid
    ``win32process.QueryFullProcessImageName`` because pywin32 (as of
    v311) does not expose it, and avoid ``GetModuleFileNameEx`` because
    that requires PROCESS_QUERY_INFORMATION | PROCESS_VM_READ — strictly
    more privilege than the PROCESS_QUERY_LIMITED_INFORMATION we open
    the handle with (and which is the whole security point of using
    QueryFullProcessImageName: it works across UAC boundaries).

    Raises OSError on failure; the caller maps that to RpcError.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    QueryFullProcessImageNameW = kernel32.QueryFullProcessImageNameW
    QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    QueryFullProcessImageNameW.restype = wintypes.BOOL

    # MAX_PATH is 260 but long-path support can exceed it; start at 1024
    # and grow once if the buffer is too small.
    for buf_len in (1024, 32768):
        buf = ctypes.create_unicode_buffer(buf_len)
        size = wintypes.DWORD(buf_len)
        ok = QueryFullProcessImageNameW(
            wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)
        )
        if ok:
            return buf.value
        err = ctypes.get_last_error()
        # ERROR_INSUFFICIENT_BUFFER = 122; retry with larger buffer.
        if err != 122:
            raise OSError(
                err,
                f"QueryFullProcessImageNameW failed for PID {pid}: WinError {err}",
            )
    raise OSError(
        122,
        f"QueryFullProcessImageNameW: path longer than 32k for PID {pid}",
    )


class DiscordRPC:
    HANDSHAKE = 0
    FRAME = 1
    CLOSE = 2
    PING = 3
    PONG = 4

    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise RpcError(
                "Discord client_id and client_secret are required. Create an "
                "application at https://discord.com/developers/applications and "
                "enter them in Settings."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.pipe = None
        self.connected = False
        self.authenticated = False
        # Populated after a successful AUTHENTICATE — contains at minimum
        # {"id": ..., "username": ..., "global_name": ...} for the local user.
        # Invariant: authenticated == True implies identity is not None.
        self.identity: Optional[dict] = None

        # Background reader thread + dispatch tables.
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_running = False
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict]] = {}
        self._event_listeners: dict[str, list[Callable[[dict], None]]] = {}

        # Optional callback fired exactly once when the reader thread
        # exits after a successful connect (Discord closed the pipe,
        # PONG send failed, …). The GUI uses this to drop out of the
        # "connected" state so the user sees a reconnect prompt instead
        # of an apparently-healthy UI that silently returns empty
        # participant lists.
        self.on_disconnect: Optional[Callable[[str], None]] = None
        self._disconnect_fired = False

    # ---- pipe I/O ----

    _DISCORD_PEER_BASENAMES = frozenset({
        "discord.exe",
        "discordcanary.exe",
        "discordptb.exe",
        "discorddevelopment.exe",
    })

    # Subdirectory names Discord's Squirrel installer creates under
    # %LOCALAPPDATA% (one per channel: stable / Canary / PTB / Development).
    _DISCORD_LOCALAPPDATA_DIRS = (
        "Discord",
        "DiscordCanary",
        "DiscordPTB",
        "DiscordDevelopment",
    )

    # Discord's MSIX package family name prefix. Microsoft Store packages
    # live under `%ProgramFiles%\WindowsApps\<publisher>.<package>_<version>_<arch>__<hash>\`,
    # so the publisher.package portion is the stable allow-list prefix.
    # Without this scoping, ANY sideloaded MSIX with a discord.exe basename
    # would pass the install-root check .
    _DISCORD_MSIX_PACKAGE_PREFIXES = (
        "discord.discord",
    )

    # Authenticode signer-Subject substring that identifies Discord-signed
    # binaries. Discord's code-signing certificate Subject contains the
    # company name in O= and CN= fields; we match liberally to tolerate
    # the punctuation variations Discord has used over time.
    _DISCORD_AUTHENTICODE_SIGNER_PATTERN = (
        r"(?:CN|O)=Discord(?:,?\s+Inc\.?)?(?:,|$)"
    )

    @classmethod
    def _trusted_discord_install_roots(cls) -> list[tuple[str, str]]:
        """Casefolded, ntpath-normalised on-disk roots where the real
        Discord binary lives, paired with the path-segment separator
        that follows each root.

        Each returned tuple is `(root, separator)`. A pipe-peer image
        path is accepted only if `normalised.startswith(root + separator)`,
        which constrains the match to a path-segment boundary and
        prevents prefix-collision attacks like `C:\\Discord-Evil`
        matching `C:\\Discord`.

        Discord ships through three install paths on Windows:

        - Squirrel (default, per-user) — separator '\\\\':
          %LOCALAPPDATA%\\Discord{,Canary,PTB,Development}\\app-X.Y.Z\\Discord.exe
        - Manual install (rare, system-wide) — separator '\\\\':
          %ProgramFiles*%\\Discord{,Canary,PTB,Development}\\…
        - Microsoft Store (MSIX, per-user) — separator '_':
          %ProgramFiles%\\WindowsApps\\Discord.Discord_<version>_<arch>__<hash>\\Discord.exe
          The MSIX package directory name is `<publisher>.<package>_<version>_…`,
          so the stable allow-list prefix is `Discord.Discord` and the
          following character is an underscore introducing the version
          (NOT a backslash). We must use `_` as the separator for these
          roots to allow legitimate Discord while still rejecting
          `Discord.Discord-Evil_…` style attempts.

        Path policy fails closed: if no roots can be computed (every
        relevant env var is missing or empty), the caller will refuse
        the peer rather than accept anything.

        Casefolding (not lower()): tolerates locale-specific case
        mapping. `lower()` returns 'ı' for 'I' under Turkish locale,
        which would silently false-reject legitimate Discord on
        Turkish-locale Windows .
        """
        import ntpath

        roots: list[tuple[str, str]] = []

        def _add_squirrel(prefix: Optional[str]) -> None:
            if not prefix:
                return
            for sub in cls._DISCORD_LOCALAPPDATA_DIRS:
                root = ntpath.normpath(ntpath.join(prefix, sub)).casefold()
                roots.append((root, "\\"))

        _add_squirrel(os.environ.get("LOCALAPPDATA"))

        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            pf = os.environ.get(var)
            if not pf:
                continue
            # Rare manual install: %ProgramFiles*%\Discord{,Canary,...}
            _add_squirrel(pf)
            # MSIX / Store: %ProgramFiles%\WindowsApps\Discord.Discord_…
            windowsapps = ntpath.normpath(ntpath.join(pf, "WindowsApps"))
            for msix_prefix in cls._DISCORD_MSIX_PACKAGE_PREFIXES:
                root = ntpath.normpath(
                    ntpath.join(windowsapps, msix_prefix)
                ).casefold()
                roots.append((root, "_"))

        return roots

    def _verify_pipe_peer(
        self,
        trusted_roots: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        """Reject the open pipe if its server process is not Discord.

        Threat model
        ------------
        A same-user process (malware from an earlier compromise, another
        local app, a sandboxed plugin) races Discord at startup and
        creates one of the `discord-ipc-N` named pipes itself. Without
        this check the recorder sends OAuth codes and access tokens
        over the pipe to whoever opened it first.

        Defense in depth
        ----------------
        Two independent checks; BOTH must pass.

        1. Path allow-list (fast pre-filter, `_check_peer_image_path`):
           - Basename in `_DISCORD_PEER_BASENAMES`
           - Image path lives under a `_trusted_discord_install_roots`
             tuple (root + separator)
           Cheap; rejects obvious squatters before we pay the cost of
           the cryptographic check.

        2. Authenticode signature (real cryptographic check,
           `_verify_authenticode_signed_by_discord`):
           - Image is Authenticode-signed
           - Signature chains to a trusted root in the OS trust store
           - Signer Subject contains `Discord, Inc.`
           This is independent of the file path — a binary anywhere on
           disk that is genuinely Discord-signed will pass; a binary
           anywhere on disk that is NOT genuinely Discord-signed will
            fail, even if it was dropped into `%LOCALAPPDATA%\\Discord\\`
            (which is user-writable and was therefore the bypass surface
            a path-only check cannot close).

        Why the path check stays
        ------------------------
        After the signature check is in place, the path check is
        technically redundant for security. It is retained because (a)
        it produces faster, clearer error messages for the common
        misconfiguration cases (Discord not running, third-party process
        squatting the pipe) and (b) defence-in-depth limits the blast
        radius if the OS trust store is compromised at some point in
        the future.

        Resolution sequence
        -------------------
        1. `GetNamedPipeServerProcessId` resolves the server PID.
        2. `OpenProcess` with `PROCESS_QUERY_LIMITED_INFORMATION` only
           (no `PROCESS_VM_READ`) — the minimum rights modern
           `QueryFullProcessImageName` needs. Avoids false-rejection
           against PPL/EDR-protected processes.
        3. `QueryFullProcessImageName` reads the canonical image path.
        4. `_check_peer_image_path` enforces the path allow-list.
        5. `_verify_authenticode_signed_by_discord` enforces the
           cryptographic check.

        Residual gaps
        -------------
        - PID-reuse race: between `GetNamedPipeServerProcessId` and
          `OpenProcess`, the original server can exit and the kernel
          can reuse the PID for an unrelated process. The window is
          small (sub-millisecond typical) AND the Authenticode check
          on the binary at the resolved path makes the window-exploit
          much harder (the attacker's binary would still have to be
          Discord-signed). We treat this as an acceptable residual race.

        - PowerShell availability: the Authenticode check shells out to
          PowerShell's `Get-AuthenticodeSignature`. PowerShell is part of
          every Windows install since Windows 7. If it is genuinely
          unavailable (locked-down systems, malformed PATH), we fail
          closed with a clear error rather than fall back to path-only
          (which is the bypassable check we're trying to supersede).

        Fails closed: any failure to resolve the peer raises `RpcError`.
        """
        import win32api
        import win32con
        import win32pipe
        import win32process

        if self.pipe is None:
            raise RpcError("Cannot verify peer on a closed pipe")

        try:
            pid = win32pipe.GetNamedPipeServerProcessId(self.pipe)
        except Exception as e:
            log.warning("Could not query pipe server PID (%s); refusing peer.", e)
            raise RpcError("Could not verify Discord pipe peer; refusing connection.") from e

        access = getattr(
            win32con, "PROCESS_QUERY_LIMITED_INFORMATION", 0x1000
        )
        try:
            handle = win32api.OpenProcess(access, False, pid)
        except Exception as e:
            log.warning("Could not open peer process %d (%s); refusing peer.", pid, e)
            raise RpcError(
                "Could not verify Discord pipe peer; refusing connection."
            ) from e

        try:
            try:
                image_path = _query_full_process_image_name(int(handle), pid)
            except Exception as e:
                log.warning(
                    "Could not read peer image path for PID %d (%s); refusing peer.",
                    pid, e,
                )
                raise RpcError(
                    "Could not verify Discord pipe peer; refusing connection."
                ) from e
        finally:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass

        image_path = str(image_path)

        # Defense in depth: path check first (fast reject), then signature.
        self._check_peer_image_path(image_path, pid, trusted_roots=trusted_roots)
        self._verify_authenticode_signed_by_discord(image_path, pid)

    @classmethod
    def _check_peer_image_path(
        cls,
        image_path: str,
        pid: int,
        trusted_roots: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        """Basename + trusted-install-root allow-list check.

        Separated from `_verify_pipe_peer` so the path policy is fully
        testable in isolation without having to stub pywin32.

        `trusted_roots` lets `_open_pipe` compute the env-derived roots
        once per connect-attempt and pass them to up to 10 verification
        calls, instead of re-reading `os.environ` on every call .
        When omitted, falls back to computing on demand for direct unit
        tests.
        """
        # Use ntpath, not os.path: the pipe peer is always a Windows
        # process so the image path uses backslash separators. On a POSIX
        # test runner os.path is posixpath and would mishandle the
        # entire 'C:\\Program Files\\…' string.
        import ntpath

        basename = ntpath.basename(image_path).casefold()
        if basename not in cls._DISCORD_PEER_BASENAMES:
            log.error(
                "Pipe peer is %r (PID %d), not a known Discord binary; refusing.",
                image_path, pid,
            )
            raise RpcError(
                f"Refusing to talk to non-Discord pipe peer: {basename!r}"
            )

        normalised = ntpath.normpath(image_path).casefold()
        roots = trusted_roots if trusted_roots is not None \
            else cls._trusted_discord_install_roots()
        if not roots:
            log.error(
                "No trusted Discord install roots configured (env missing); "
                "refusing peer %r (PID %d).",
                image_path, pid,
            )
            raise RpcError(
                "Refusing peer: no trusted Discord install root available."
            )
        for root, sep in roots:
            # `root` is a path-segment prefix; `sep` is the character
            # that must follow it. For Squirrel/manual roots the
            # separator is '\\' (the next character must start a new
            # directory component). For MSIX publisher-package roots
            # the separator is '_' (the next character begins the
            # package version). Without the explicit separator,
            # `C:\Discord-Evil` would match `C:\Discord` as a prefix
            # and bypass the gate.
            if normalised.startswith(root + sep):
                log.info(
                    "Verified Discord pipe peer path: %s (PID %d, root %s)",
                    basename, pid, root,
                )
                return

        log.error(
            "Pipe peer %r (PID %d) is not under a trusted Discord install root; "
            "refusing. Trusted roots: %s",
            image_path, pid, roots,
        )
        raise RpcError(
            f"Refusing to talk to peer outside trusted Discord install: {image_path!r}"
        )

    @classmethod
    def _verify_authenticode_signed_by_discord(
        cls, image_path: str, pid: int,
    ) -> None:
        """Verify the image file is Authenticode-signed by Discord, Inc.

        Cryptographic peer identity. Independent of the file path —
        passes only if the binary is genuinely Discord-signed,
        regardless of where it lives on disk. This is the control that
        closes the user-writable-trusted-root bypass: even if an
        attacker drops their own binary under
        `%LOCALAPPDATA%\\Discord\\`, the binary lacks Discord's
        signature and will be rejected here.

        Implementation: shells out to PowerShell's
        `Get-AuthenticodeSignature`, which validates the full
        certificate chain against the OS trust store and exposes the
        signer Subject for issuer-name matching. PowerShell ships with
        every modern Windows install, so this dependency is universal.

        Exit codes from the PowerShell helper:
          0 — signature Valid AND signer Subject matches Discord pattern
          1 — signature not Valid (missing, malformed, or chain failure)
          2 — signature Valid but signer is NOT Discord, Inc.
          other / non-zero — PowerShell or script-internal failure

        Any non-zero exit (or PowerShell unavailability, or timeout)
        raises `RpcError` — fail closed.
        """
        import subprocess

        # Escape the image path for the PowerShell single-quoted
        # string literal. Single quotes within a single-quoted string
        # are escaped by doubling them.
        pwsh_quoted_path = image_path.replace("'", "''")
        pwsh_signer_pattern = cls._DISCORD_AUTHENTICODE_SIGNER_PATTERN

        # `-NoProfile`: skip user/host profile scripts (faster + avoids
        #   surprise side-effects from third-party PowerShell profiles).
        # `-NonInteractive`: never prompt; treat any prompt as failure.
        # `-Command`: run an inline script string.
        script = (
            "$ErrorActionPreference='Stop';"
            f"$sig = Get-AuthenticodeSignature -FilePath '{pwsh_quoted_path}';"
            "if ($sig.Status -ne 'Valid') { exit 1 };"
            "$subject = $sig.SignerCertificate.Subject;"
            f"if ($subject -match '{pwsh_signer_pattern}')"
            "  { exit 0 } else { exit 2 }"
        )

        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command", script,
                ],
                capture_output=True,
                timeout=15,
            )
        except FileNotFoundError as e:
            log.error(
                "PowerShell unavailable for Authenticode verification of "
                "%r (PID %d): %s. Failing closed.",
                image_path, pid, e,
            )
            raise RpcError(
                "Refusing peer: PowerShell unavailable for signature verification."
            ) from e
        except subprocess.TimeoutExpired as e:
            log.error(
                "Authenticode verification of %r (PID %d) timed out after 15s.",
                image_path, pid,
            )
            raise RpcError(
                f"Refusing peer: signature verification of {image_path!r} timed out."
            ) from e

        if result.returncode == 0:
            log.info(
                "Verified Discord pipe peer Authenticode signature: %s (PID %d)",
                image_path, pid,
            )
            return

        if result.returncode == 1:
            log.error(
                "Pipe peer %r (PID %d): Authenticode signature is not Valid; refusing.",
                image_path, pid,
            )
            raise RpcError(
                f"Refusing peer: Authenticode signature on {image_path!r} is not valid."
            )
        if result.returncode == 2:
            log.error(
                "Pipe peer %r (PID %d): signer is not Discord, Inc.; refusing.",
                image_path, pid,
            )
            raise RpcError(
                f"Refusing peer: signer of {image_path!r} is not Discord, Inc."
            )
        log.error(
            "PowerShell signature verification of %r (PID %d) failed (exit %d). "
            "stderr=%r",
            image_path, pid, result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace")[:500],
        )
        raise RpcError(
            f"Refusing peer: signature verification of {image_path!r} "
            f"failed (PowerShell exit {result.returncode})."
        )

    def _open_pipe(self) -> bool:
        """Walk `discord-ipc-0..9`, return True at the first VERIFIED peer.

        Continues past verification failures so a benign or hostile
        collision on a lower index does not block a legitimate Discord
        sitting on a higher one. Each verification failure is logged at
        ERROR with the rejected image path so an operator can detect
        active squat attempts. If we exhaust every index without finding
        a verified Discord, the last verification error is re-raised so
        the GUI can surface what was rejected; if no pipe even opened,
        we return False (Discord is not running).

        Computes the trusted-install-roots ONCE before the loop and
        passes them to each `_verify_pipe_peer` call. This avoids
        re-reading `os.environ` on every of up to 10 iterations
         and guarantees a consistent allow-list for the
        duration of one scan.
        """
        import pywintypes
        import win32file

        last_verify_error: Optional[RpcError] = None
        trusted_roots = self._trusted_discord_install_roots()

        for i in range(10):
            # Explicit invariant: self.pipe is None at the top of every
            # iteration. The success path sets it via CreateFile and the
            # verify-failure path nulls it before `continue`; making the
            # reset explicit at the loop head removes the dependency on
            # those side-effects scattered across two write sites
            # .
            self.pipe = None

            pipe_name = f"\\\\.\\pipe\\discord-ipc-{i}"
            try:
                self.pipe = win32file.CreateFile(
                    pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
            except pywintypes.error:
                continue

            try:
                self._verify_pipe_peer(trusted_roots=trusted_roots)
            except RpcError as e:
                last_verify_error = e
                log.warning(
                    "discord-ipc-%d failed peer verification (%s); continuing to next index.",
                    i, e,
                )
                try:
                    win32file.CloseHandle(self.pipe)
                except Exception:
                    pass
                self.pipe = None
                continue

            log.info("Opened Discord pipe %d", i)
            return True

        if last_verify_error is not None:
            # At least one pipe was open but failed verification on every
            # index — surface the rejection so the GUI can warn the user
            # that something is squatting Discord's pipe names.
            raise last_verify_error
        return False

    def _write(self, opcode: int, payload: dict) -> None:
        import win32file

        if self.pipe is None:
            raise RpcError("RPC pipe is not open")

        data = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", opcode, len(data))
        with self._write_lock:
            if self.pipe is None:  # re-check under lock; close() may have just nulled it
                raise RpcError("RPC pipe is not open")
            win32file.WriteFile(self.pipe, header + data)

    def _read_frame(self, deadline: Optional[float] = None) -> Tuple[int, bytes]:
        """Read one IPC frame. Returns (opcode, raw_payload_bytes).

        Decoding the payload as JSON is deferred to callers, because control
        frames (PING/PONG/CLOSE) carry opaque payloads we should echo back
        verbatim without re-encoding.

        If `deadline` (an absolute time.time()) is given, the header read
        polls PeekNamedPipe until data is available or the deadline is
        exceeded — at which point RpcError is raised. Without this, the
        inline bootstrap mode of _cmd() would block indefinitely inside
        the OS pipe read regardless of its timeout parameter, which is
        how a wedged Discord could leave "Connecting…" stuck forever.
        """
        import win32file
        import win32pipe

        if self.pipe is None:
            raise RpcError("RPC pipe is not open")

        if deadline is not None:
            # Poll for the header bytes so we can honor the deadline.
            # PeekNamedPipe returns (data_read, total_avail, msg_left)
            # without consuming anything. Once at least 8 bytes are
            # buffered we know the subsequent ReadFile won't block.
            while True:
                if self.pipe is None:
                    raise RpcError("RPC pipe is not open")
                try:
                    _, avail, _ = win32pipe.PeekNamedPipe(self.pipe, 0)
                except Exception as e:
                    raise RpcError(f"PeekNamedPipe failed: {e}") from e
                if avail >= 8:
                    break
                if time.time() >= deadline:
                    raise _PipeReadIdle(
                        "RPC pipe read timed out waiting for header"
                    )
                time.sleep(0.05)

        _, header = win32file.ReadFile(self.pipe, 8)
        if len(header) < 8:
            raise RpcError("Pipe closed (short header)")
        opcode, length = struct.unpack("<II", header)
        if length > _MAX_FRAME_BYTES:
            # A frame this large is never legitimate; refuse before we
            # allocate. Defends against a malicious/buggy pipe peer that
            # could otherwise OOM the recorder.
            raise RpcError(f"Refusing oversized IPC frame ({length} bytes)")
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            if deadline is not None:
                # Same deadline-honoring poll for the payload bytes. Once
                # the peer has sent the header we expect the payload
                # promptly; if it doesn't arrive in time, fail rather
                # than block forever.
                while True:
                    if self.pipe is None:
                        raise RpcError("RPC pipe is not open")
                    try:
                        _, avail, _ = win32pipe.PeekNamedPipe(self.pipe, 0)
                    except Exception as e:
                        raise RpcError(f"PeekNamedPipe failed: {e}") from e
                    if avail > 0:
                        break
                    if time.time() >= deadline:
                        raise _PipeReadIdle(
                            "RPC pipe read timed out waiting for payload"
                        )
                    time.sleep(0.05)
            _, chunk = win32file.ReadFile(self.pipe, remaining)
            if not chunk:
                # Zero-byte read on a half-closed pipe. Without this guard
                # the loop would spin forever, pinning a CPU core and
                # stalling every RPC command.
                raise RpcError("Pipe closed mid-read")
            chunks.append(chunk)
            remaining -= len(chunk)
        return opcode, b"".join(chunks)

    # ---- background reader ----

    def _wake_pending(self, reason: str) -> None:
        """Release every thread blocked in _cmd() with an error result.

        Called when the reader is about to exit or when close() is invoked
        mid-command — without this, callers would sit on `event_obj.wait(timeout)`
        for the full timeout (default 10s, up to 120s for AUTHORIZE) before
        learning the connection is gone, making shutdown feel like a hang.
        """
        with self._pending_lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _nonce, (event_obj, result_slot) in pending:
            result_slot.setdefault("error", reason)
            event_obj.set()

    def _handle_control_frame(
        self, opcode: int, raw: bytes, *, ctx: str
    ) -> Optional[str]:
        """Dispatch PING / PONG / CLOSE without decoding as a JSON command.

        Both the reader thread (post-auth) and the inline bootstrap _cmd
        (pre-reader-thread) must answer Discord's keepalive PINGs with
        PONG carrying the same payload — Discord severs the pipe after
        a few unanswered PINGs, which is one root of the "Discord
        randomly stops accepting commands" regression. Centralising the
        opcode dispatch here removes the second source of truth.

        Returns:
            None        - not a control frame; caller should process the
                          frame as a normal JSON command/event payload.
            "continue"  - control frame handled; caller should continue
                          to the next read.
            "close"     - CLOSE received from Discord. Caller decides the
                          exit semantics (reader-mode breaks; bootstrap
                          raises RpcError so the in-flight _cmd fails
                          loudly instead of timing out silently).
            "pong_fail" - PING was received but PONG send failed. Reader
                          mode treats this as pipe-dead and exits; the
                          bootstrap loop tolerates it and lets the next
                          read or the caller deadline surface the break.
        """
        if opcode == self.PING:
            try:
                ping_payload = json.loads(raw.decode("utf-8")) if raw else {}
                self._write(self.PONG, ping_payload)
            except Exception:
                log.exception("Failed to send PONG (%s); pipe likely dead", ctx)
                return "pong_fail"
            return "continue"
        if opcode == self.PONG:
            return "continue"
        if opcode == self.CLOSE:
            log.info("Discord requested CLOSE (%s)", ctx)
            return "close"
        if opcode != self.FRAME:
            log.warning("Unknown IPC opcode %d (%s); ignoring", opcode, ctx)
            return "continue"
        return None

    # How long the reader will block on a single _read_frame call before
    # releasing the pipe and re-checking _reader_running. A short tick is
    # important: while the reader is inside _read_frame, the pipe has an
    # outstanding I/O (Peek+Read), and concurrent WriteFile from a
    # command can be delayed by up to one tick. 500 ms is short enough
    # that interactive commands feel snappy and long enough that the
    # reader doesn't waste CPU peeking when Discord is quiet.
    _READER_POLL_INTERVAL = 0.5

    def _reader_loop(self) -> None:
        """Pump frames from the pipe into the appropriate destination.

        Each iteration reads with a short deadline rather than blocking
        indefinitely in ReadFile. The deadline-expiry path (signalled by
        _PipeReadIdle) is the normal "nothing to do this tick" outcome
        and is silently swallowed; every other RpcError means the pipe
        is genuinely dead and we exit. See _PipeReadIdle for the
        non-overlapped-pipe rationale.
        """
        log.info("RPC reader thread started")
        try:
            while self._reader_running:
                try:
                    opcode, raw = self._read_frame(
                        deadline=time.time() + self._READER_POLL_INTERVAL,
                    )
                except _PipeReadIdle:
                    continue
                except Exception as e:
                    if self._reader_running:
                        log.info("RPC reader exiting: %s", e)
                    break

                control = self._handle_control_frame(opcode, raw, ctx="reader")
                if control == "continue":
                    continue
                if control in ("close", "pong_fail"):
                    break
                # control is None → fall through to normal frame dispatch.

                # Normal command/event frame.
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    log.exception("RPC frame was not valid JSON")
                    continue

                nonce = data.get("nonce")
                if nonce:
                    with self._pending_lock:
                        entry = self._pending.pop(nonce, None)
                    if entry is not None:
                        event_obj, result_slot = entry
                        result_slot["data"] = data
                        event_obj.set()
                        continue

                if data.get("cmd") == "DISPATCH":
                    evt = data.get("evt")
                    with self._pending_lock:
                        listeners = list(self._event_listeners.get(evt, ()))
                    for cb in listeners:
                        try:
                            cb(data.get("data") or {})
                        except Exception:
                            log.exception("RPC event listener for %s raised", evt)
        finally:
            # Whatever exit path we took, ensure connection state reflects
            # reality and no caller is left blocked forever.
            self.connected = False
            self.authenticated = False
            self._wake_pending("RPC reader thread exited")
            # Notify the GUI exactly once that the connection is gone.
            # Without this, the GUI continues to show "connected ✓" while
            # every participant query silently returns []. The callback
            # is invoked from this background thread — listeners that
            # touch the UI must marshal back to the main thread.
            if self.on_disconnect is not None and not self._disconnect_fired:
                self._disconnect_fired = True
                try:
                    self.on_disconnect("RPC reader thread exited")
                except Exception:
                    log.exception("on_disconnect callback raised")

    def _start_reader(self) -> None:
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="discord-rpc-reader", daemon=True
        )
        self._reader_thread.start()

    # ---- synchronous command (now nonce-multiplexed) ----

    def _cmd(self, cmd: str, args: dict, evt: Optional[str] = None, timeout: float = 10.0) -> dict:
        """Send a command and block for the response keyed by its nonce.

        Dual-mode based on whether the background reader is running:

        - **Async mode** (reader running): register nonce, write, wait on a
          threading.Event. The reader thread dispatches the matching frame.
          Used for queries issued AFTER connect() completes (participant
          polling, subscribe/unsubscribe).
        - **Inline mode** (reader NOT running): write, then loop ReadFile
          in this thread until our nonce arrives, handling PING/PONG and
          CLOSE control frames as they appear. Used during the bootstrap
          (HANDSHAKE → AUTHORIZE → AUTHENTICATE) — because Discord's RPC
          server empirically wedges and never responds to AUTHORIZE when
          a reader thread is already pumping the pipe, even though the
          wire payload is byte-identical. Deferring the reader avoids
          the wedge entirely. Originally diagnosed in 

        `evt` is set as a top-level field on the payload — required for
        SUBSCRIBE/UNSUBSCRIBE which carry the event name there (not in args).
        """
        if self.pipe is None:
            raise RpcError("RPC client is not connected")

        nonce = str(uuid.uuid4())
        payload = {"cmd": cmd, "args": args, "nonce": nonce}
        if evt is not None:
            payload["evt"] = evt

        if self._reader_running:
            event_obj = threading.Event()
            result_slot: dict = {}
            with self._pending_lock:
                self._pending[nonce] = (event_obj, result_slot)
            try:
                self._write(self.FRAME, payload)
            except Exception:
                with self._pending_lock:
                    self._pending.pop(nonce, None)
                raise

            if not event_obj.wait(timeout):
                with self._pending_lock:
                    self._pending.pop(nonce, None)
                raise RpcError(f"Timeout waiting for response to {cmd!r}")

            if "error" in result_slot:
                raise RpcError(result_slot["error"])

            data = result_slot.get("data") or {}
        else:
            self._write(self.FRAME, payload)
            deadline = time.time() + timeout
            data: dict = {}
            while time.time() < deadline:
                # Pass the deadline so a wedged Discord can't make the
                # blocking pipe read outlive the caller's timeout. The
                # previous version checked the deadline only between
                # ReadFile calls, so a single never-completing ReadFile
                # would hang forever despite the `while time.time() <
                # deadline` guard.
                opcode, raw = self._read_frame(deadline=deadline)
                control = self._handle_control_frame(opcode, raw, ctx="bootstrap")
                if control == "continue":
                    continue
                if control == "close":
                    raise RpcError("Discord closed the pipe during bootstrap")
                if control == "pong_fail":
                    # Existing bootstrap behavior: tolerate one PONG send
                    # failure and let the deadline-honoring _read_frame on
                    # the next iteration surface the broken pipe. Reader
                    # mode is more conservative; bootstrap stays lenient.
                    continue
                # control is None → normal FRAME, dispatch as command response.
                try:
                    frame = json.loads(raw.decode("utf-8"))
                except Exception:
                    log.exception("RPC bootstrap frame was not valid JSON")
                    continue
                if frame.get("nonce") == nonce:
                    data = frame
                    break
                # Unrelated frame arriving before our reply — drop it.
            else:
                raise RpcError(f"Timeout waiting for response to {cmd!r}")

        if data.get("evt") == "ERROR":
            raise RpcError(data.get("data", {}).get("message", "RPC error"))
        return data.get("data") or {}

    # ---- handshake (pre-reader-thread, uses blocking read directly) ----

    def _handshake_read(self) -> Tuple[int, dict]:
        opcode, raw = self._read_frame()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            raise RpcError(f"Handshake frame was not valid JSON: {e}")
        return opcode, data

    def connect(self, existing_token: Optional[dict] = None):
        """Connect, handshake, and authenticate. Returns the (possibly refreshed) token dict.

        The reader thread is intentionally NOT started until authentication
        succeeds. The bootstrap (HANDSHAKE → AUTHORIZE → AUTHENTICATE) runs
        in inline mode via _cmd()/_handshake_read(), because Discord's RPC
        server can wedge and never reply to AUTHORIZE when the reader is
        already pumping the pipe (reproduced this side-by-side
        against an inline diagnostic).

        On any failure after the pipe is opened, the partially-connected
        DiscordRPC is torn down (pipe closed, reader stopped if it was
        started) before the exception propagates. Without that cleanup,
        a denied AUTHORIZE prompt or an expired refresh token would leave
        an orphan reader thread holding the pipe handle, preventing the
        next reconnect from succeeding.

        `_open_pipe()` itself runs INSIDE the try block — in PR
         Pre-fix it sat above the try, so any non-RpcError
        escape (e.g., ImportError from one of the lazy pywin32 imports
        inside `_verify_pipe_peer`) propagated up with `self.pipe` left
        pointing at a freshly-opened handle that never got closed.
        Inside the try, the same exception triggers the `self.close()`
        in the except branch.
        """
        # Reset the once-fired guard so a reconnect after a previous
        # disconnect can fire on_disconnect again on the next pipe drop.
        self._disconnect_fired = False

        try:
            if not self._open_pipe():
                raise RpcError(
                    "Discord is not running, or RPC pipe is unavailable."
                )
            self._write(self.HANDSHAKE, {"v": 1, "client_id": self.client_id})
            opcode, data = self._handshake_read()
            if not (opcode == self.FRAME and data.get("evt") == "READY"):
                raise RpcError(f"Handshake failed: {data!r}")
            self.connected = True

            token = existing_token

            if token and time.time() < (token.get("expires_at", 0) - 60):
                try:
                    self._authenticate(token["access_token"])
                    return token
                except Exception as e:
                    log.info("Cached token rejected (%s); will try refresh.", e)

            if token and token.get("refresh_token"):
                try:
                    new_token = self._refresh_token(token["refresh_token"])
                    self._authenticate(new_token["access_token"])
                    return new_token
                except Exception as e:
                    log.info("Refresh failed (%s); doing full authorize.", e)

            token = self._authorize()
            self._authenticate(token["access_token"])
            return token
        except BaseException:
            try:
                self.close()
            except Exception:
                log.exception("Cleanup after failed connect() raised")
            raise
        finally:
            # Bring the reader up only on the success path. close() in the
            # except branch resets self.authenticated to False, so this
            # check is also the failure-path no-op gate.
            if self.authenticated:
                self._start_reader()

    def _authorize(self) -> dict:
        log.info("Requesting AUTHORIZE — accept the prompt in Discord")
        data = self._cmd("AUTHORIZE", {"client_id": self.client_id, "scopes": SCOPES}, timeout=120.0)
        code = data.get("code")
        if not code:
            raise RpcError("AUTHORIZE returned no code")
        resp = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        try:
            return {
                "access_token": body["access_token"],
                "refresh_token": body["refresh_token"],
                "expires_at": time.time() + body["expires_in"],
            }
        except KeyError as e:
            raise RpcError(f"OAuth response missing required field {e!s}")

    def _refresh_token(self, refresh_token: str) -> dict:
        resp = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        try:
            return {
                "access_token": body["access_token"],
                "refresh_token": body.get("refresh_token", refresh_token),
                "expires_at": time.time() + body["expires_in"],
            }
        except KeyError as e:
            raise RpcError(f"OAuth refresh response missing required field {e!s}")

    def _authenticate(self, access_token: str) -> None:
        data = self._cmd("AUTHENTICATE", {"access_token": access_token})
        user = (data or {}).get("user") or {}
        # Invariant: only mark authenticated when we actually received a
        # usable identity. Downstream code (recorder.py self_user_id) treats
        # `authenticated` as "identity is populated".
        if not user.get("id"):
            raise RpcError("AUTHENTICATE returned no user identity")
        self.identity = {
            "id": user.get("id"),
            "username": user.get("username"),
            "global_name": user.get("global_name"),
        }
        log.info(
            "Authenticated as %s (%s)",
            self.identity.get("global_name") or self.identity.get("username"),
            self.identity.get("id"),
        )
        self.authenticated = True

    # ---- queries ----

    def get_voice_channel(self) -> Optional[dict]:
        if not self.authenticated:
            return None
        try:
            return self._cmd("GET_SELECTED_VOICE_CHANNEL", {})
        except RpcError as e:
            log.warning("GET_SELECTED_VOICE_CHANNEL failed: %s", e)
            return None

    def get_participants_detailed(self) -> list[dict]:
        """Return [{id, name}] for everyone currently in the voice channel."""
        if not self.authenticated:
            return []
        ch = self.get_voice_channel()
        if not ch:
            return []
        out: list[dict] = []
        for vs in ch.get("voice_states", []):
            user = vs.get("user") or {}
            uid = user.get("id")
            if not uid:
                continue
            name = user.get("global_name") or user.get("username") or "Unknown"
            out.append({"id": uid, "name": name})
        return out

    def get_participants(self) -> list[str]:
        if not self.authenticated:
            return []
        ch = self.get_voice_channel()
        if not ch:
            return []
        out = []
        for vs in ch.get("voice_states", []):
            user = vs.get("user", {})
            name = user.get("global_name") or user.get("username") or "Unknown"
            out.append(name)
        return out

    # ---- subscriptions ----

    def subscribe(self, evt: str, args: dict, callback: Callable[[dict], None]) -> None:
        """Subscribe to a DISPATCH event. The callback runs on the reader
        thread, so keep it short and offload heavy work to a queue.

        The listener is registered before the SUBSCRIBE wire call so that an
        event arriving between Discord's ack and our registration isn't lost.
        If the SUBSCRIBE itself fails, we roll the listener back — so callers
        can rely on the invariant "subscribe() raised => no callback will
        ever fire".
        """
        with self._pending_lock:
            self._event_listeners.setdefault(evt, []).append(callback)
        try:
            self._cmd("SUBSCRIBE", args, evt=evt)
        except Exception:
            with self._pending_lock:
                listeners = self._event_listeners.get(evt, [])
                if callback in listeners:
                    listeners.remove(callback)
                if not listeners:
                    self._event_listeners.pop(evt, None)
            raise

    def unsubscribe(self, evt: str, args: dict,
                    callback: Optional[Callable[[dict], None]] = None) -> None:
        """Unsubscribe from a DISPATCH event.

        If `callback` is provided, only that specific callback is removed
        from the local listener list, and the wire UNSUBSCRIBE is only
        issued when the last listener for `evt` is gone. If `callback` is
        omitted (legacy behavior), all listeners for the event are removed.
        """
        # Decide whether the wire UNSUBSCRIBE is needed FIRST, under the
        # lock, and update the local listener bookkeeping atomically. If we
        # sent the wire command unconditionally (as the previous version
        # did) we would silently disable Discord event delivery for any
        # callbacks that remained registered against the same evt.
        with self._pending_lock:
            if callback is None:
                # Legacy: drop everything for this event.
                self._event_listeners.pop(evt, None)
                send_wire = True
            else:
                listeners = self._event_listeners.get(evt, [])
                if callback in listeners:
                    listeners.remove(callback)
                if not listeners:
                    self._event_listeners.pop(evt, None)
                    send_wire = True
                else:
                    send_wire = False

        if send_wire:
            try:
                self._cmd("UNSUBSCRIBE", args, evt=evt, timeout=5.0)
            except Exception:
                log.debug(
                    "UNSUBSCRIBE %s failed; local listeners already cleared",
                    evt,
                )

    # ---- shutdown ----

    def close(self) -> None:
        """Tear down the RPC connection.

        Idempotent: callable from any thread, callable multiple times,
        callable while _cmd waiters are pending. Pending waiters are
        woken with an error result before the reader is joined, so the
        GUI doesn't appear frozen during shutdown.
        """
        self._reader_running = False
        # Wake any in-flight _cmd waiters first; otherwise they'd block
        # for their full timeout (up to 120s for AUTHORIZE) before noticing
        # the connection is gone.
        self._wake_pending("RPC connection closed")

        pipe = self.pipe
        self.pipe = None
        if pipe is not None:
            import win32file
            try:
                win32file.CloseHandle(pipe)
            except Exception:
                pass
        self.connected = False
        self.authenticated = False
        reader = self._reader_thread
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=2)
