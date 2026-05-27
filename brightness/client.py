"""brightness_client.py — tiny TCP client Loupedeck calls.

Sends one command to brightness_daemon.py and exits. Kept minimal so the
Python cold-start dominates per-tick latency (~100ms) instead of doing any
real work here.

Usage:
    brightness_client.py <idx> <delta>
    brightness_client.py --list
    brightness_client.py --ping
    brightness_client.py --cmd "<raw command>"
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

HOST = "127.0.0.1"
PORT = 48736


def send(cmd: str) -> tuple[int, str]:
    try:
        with socket.create_connection((HOST, PORT), timeout=2.0) as s:
            s.sendall((cmd + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                buf = s.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
        reply = b"".join(chunks).decode("utf-8", errors="replace").rstrip()
        return (0 if reply.startswith("ok") or reply.startswith("pong")
                or reply[:1].isdigit() else 1), reply
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        return 3, f"daemon not reachable: {e}"


def _spawn_daemon() -> None:
    """Launch the daemon detached so the client can exit while it keeps running.

    Called on first "daemon not reachable" — the user no longer has to
    start the daemon manually before using the brightness dials. Costs
    ~500ms on the first dial press of a session; subsequent presses hit
    the live daemon.
    """
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    root = Path(__file__).resolve().parent.parent
    subprocess.Popen(
        [sys.executable, "-m", "brightness.daemon"],
        cwd=str(root),
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_daemon(timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rc, _ = send("ping")
        if rc == 0:
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--list":
        cmd = "list"
    elif args[0] == "--ping":
        cmd = "ping"
    elif args[0] == "--cmd" and len(args) >= 2:
        cmd = " ".join(args[1:])
    elif len(args) == 2:
        cmd = f"adjust {args[0]} {args[1]}"
    else:
        print("usage: brightness_client.py <idx> <delta> | --list | --ping | --cmd <raw>")
        return 2

    rc, reply = send(cmd)
    if rc == 3 and cmd != "ping":
        _spawn_daemon()
        if _wait_for_daemon():
            rc, reply = send(cmd)
    print(reply)
    return rc


if __name__ == "__main__":
    sys.exit(main())
