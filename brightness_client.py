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
import sys

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
    # Print so --list / --ping work from a real console; pythonw discards this.
    print(reply)
    return rc


if __name__ == "__main__":
    sys.exit(main())
