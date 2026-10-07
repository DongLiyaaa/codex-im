"""Preflight run inside the Codex sandbox, before any model is contacted.

Exit code 0 only when the sandbox really enforces its limits: writing is refused and no outbound connection can be
made. A sandbox may refuse to create a socket at all (Codex's Linux sandbox does), so creating the socket belongs
inside the same try as connecting: that is enforcement, not a failure.
"""
import pathlib
import socket
import sys

WRITABLE = 41
NETWORK = 42


def check():
    try:
        pathlib.Path('sandbox-write-probe').write_text('forbidden')
    except OSError:
        pass
    else:
        return WRITABLE
    try:
        probe = socket.socket()
        probe.settimeout(1)
        probe.connect(('1.1.1.1', 443))
    except OSError:
        pass
    else:
        return NETWORK
    return 0


if __name__ == '__main__':
    sys.exit(check())
