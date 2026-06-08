"""Minimal Discord RPC AUTHORIZE diagnostic.

Bypasses the mynah app entirely. Opens the pipe, does the
handshake inline, sends AUTHORIZE, then reads frames one at a time with
verbose printing. Tells you whether Discord is responding at all and what
it's actually saying.

Usage:
    python diagnose_authorize.py <client_id>

Reads its client_id from argv. Doesn't need Client Secret (not exchanging
the code, just trying to elicit the popup).
"""
import json
import struct
import sys
import time
import uuid


def main(client_id: str) -> int:
    import pywintypes
    import win32file

    print(f"Looking for Discord pipe...")
    pipe = None
    for i in range(10):
        name = rf"\\.\pipe\discord-ipc-{i}"
        try:
            pipe = win32file.CreateFile(
                name,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            print(f"  Opened {name}")
            break
        except pywintypes.error:
            continue
    if pipe is None:
        print("FAIL: no Discord pipe found. Is Discord running?")
        return 1

    def write(opcode: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", opcode, len(data))
        win32file.WriteFile(pipe, header + data)

    def read_one():
        _, hdr = win32file.ReadFile(pipe, 8)
        opcode, length = struct.unpack("<II", hdr)
        chunks = []
        remaining = length
        while remaining > 0:
            _, chunk = win32file.ReadFile(pipe, remaining)
            chunks.append(chunk)
            remaining -= len(chunk)
        body = json.loads(b"".join(chunks).decode("utf-8"))
        return opcode, body

    # ---- Handshake ----
    print("\n[1] Sending HANDSHAKE...")
    write(0, {"v": 1, "client_id": client_id})
    op, data = read_one()
    print(f"    <- opcode={op}, body={json.dumps(data, indent=2)[:400]}")
    if data.get("evt") != "READY":
        print("FAIL: handshake did not return READY")
        return 1
    print("    OK")

    # ---- AUTHORIZE ----
    nonce = str(uuid.uuid4())
    auth_payload = {
        "cmd": "AUTHORIZE",
        "args": {
            "client_id": client_id,
            "scopes": ["rpc", "rpc.voice.read", "identify"],
        },
        "nonce": nonce,
    }
    print(f"\n[2] Sending AUTHORIZE  (nonce={nonce[:8]}...)")
    print(f"    payload: {json.dumps(auth_payload, indent=2)}")
    write(1, auth_payload)
    print()
    print("    NOW LOOK AT DISCORD — a popup should appear within a")
    print("    couple of seconds asking you to authorize the app.")
    print("    Click ALLOW.")
    print()
    print("    Reading frames as they arrive (waits up to 180 sec)...")
    deadline = time.time() + 180

    while time.time() < deadline:
        try:
            op, body = read_one()
        except Exception as e:
            print(f"\n    !!! Pipe read failed: {e}")
            print(f"    !!! Discord may have closed the pipe.")
            return 1
        print(f"\n    <- opcode={op}")
        print(f"       {json.dumps(body, indent=4)[:1200]}")
        if body.get("nonce") == nonce:
            evt = body.get("evt")
            if evt == "ERROR":
                print(f"\nFAIL: Discord returned ERROR for AUTHORIZE.")
                print(f"  message: {body.get('data', {}).get('message')}")
                print(f"  code:    {body.get('data', {}).get('code')}")
                return 1
            code = body.get("data", {}).get("code")
            if code:
                print(f"\nOK: AUTHORIZE returned code (truncated): {str(code)[:12]}...")
                print("    The popup-and-allow flow worked. Discord RPC is functional.")
                return 0
        # otherwise it's some other event (DISPATCH READY etc.); keep reading

    print("\nFAIL: 180s went by with no AUTHORIZE response.")
    print("  Discord received our request but never sent a reply.")
    print("  Most likely: it's silently refusing to show the popup.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python diagnose_authorize.py <client_id>")
        sys.exit(1)
    raise SystemExit(main(sys.argv[1]))
