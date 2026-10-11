"""Host port checks: is a port free, and if not, which nearby one is."""

from __future__ import annotations

import socket
import sys
from collections.abc import Iterable


def _bind_fails(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform == "win32":
            # Without exclusive use, Windows lets a wildcard bind succeed next to a 127.0.0.1 listener.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def _accepts_connections(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def port_in_use(port: int) -> bool:
    return _accepts_connections(port) or _bind_fails("127.0.0.1", port) or _bind_fails("0.0.0.0", port)


def suggest_free_port(start: int, *, taken: Iterable[int] = (), span: int = 100) -> int | None:
    """First free port after `start`, skipping `taken` (ports already claimed by containers)."""
    skip = set(taken)
    for candidate in range(start + 1, min(start + span, 65535) + 1):
        if candidate not in skip and not port_in_use(candidate):
            return candidate
    return None
