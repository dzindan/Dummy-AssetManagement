"""Helpers for running this app as a shared server on the local network,
so other machines can point a browser at the host machine instead of each
running their own local copy with its own separate database."""

import socket


def get_lan_ip() -> str:
    """Best-effort LAN-facing IP address of this machine.

    Opens a UDP "connection" to an address that doesn't need to actually be
    reachable - this just makes the OS pick which local interface/IP would
    be used, without sending any real traffic.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
