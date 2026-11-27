"""Nothing leaves for the internet (2026-11-27, user: "no hole that sends data
out to the internet"). The same module in AM, MM and AAL - LAN apps, so
unlike SG (this PC only) they may reach the bank's own network.

- `block_outbound_sockets()`: in this process a Python socket may only
  connect / send to an address inside the bank's network - loopback,
  private (10.x, 172.16-31.x, 192.168.x, fc00::/7), link-local; a name is
  resolved and every address must be. Anything else (the internet) is refused
  before a byte goes: a library phoning home, a server setting pointed
  outside, an SMTP relay on the internet...
- `require_internal(server, what)`: the same rule for a server setting, said
  when it is saved (a name in this PC's own AD domain - USERDNSDOMAIN - also
  passes there: a bank may use public ranges inside; the socket rule still
  applies when connecting). A name that doesn't resolve is let through:
  nothing can be reached by it.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time


class NotInternal(ValueError):
    """An address outside the bank's network (refused)."""


_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_installed = False
_cache: dict[str, tuple[float, bool]] = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 300


def host_of(server: str) -> str:
    """'tcp:DBSRV01\\SQL2019,1433' -> 'DBSRV01'; 'https://cucm01:8443/x' -> 'cucm01';
    'mail.bank.local:25' -> 'mail.bank.local'; '[fe80::1]' -> 'fe80::1'."""
    text = (server or "").strip()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.I)   # a URL's scheme
    text = re.sub(r"^(tcp|np|lpc|admin):", "", text, flags=re.I)
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    text = text.rsplit("@", 1)[-1]
    text = text.split("\\", 1)[0].split(",", 1)[0].strip()
    if text.startswith("["):
        return text[1:].split("]", 1)[0]
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text.strip().rstrip(".")


def address_ok(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(addr).strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _resolves_inside(host: str) -> bool:
    key = host.lower()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        ok = all(address_ok(info[4][0]) for info in _real_getaddrinfo(host, None))
    except (OSError, UnicodeError):
        ok = True  # doesn't resolve: nothing can be reached by it
    with _cache_lock:
        _cache[key] = (time.monotonic(), ok)
    return ok


def _own_domain(name: str) -> bool:
    domain = (os.environ.get("USERDNSDOMAIN") or "").strip().strip(".").lower()
    low = name.lower()
    return bool(domain) and (low == domain or low.endswith("." + domain))


def is_internal(server: str) -> bool:
    host = host_of(server)
    if not host or host.lower() in ("localhost", ".", "(local)"):
        return True
    try:
        ipaddress.ip_address(host)
        return address_ok(host)
    except ValueError:
        pass
    return _own_domain(host) or _resolves_inside(host)


def require_internal(server: str, what: str) -> str:
    if server and not is_internal(server):
        raise NotInternal(f"{what} \"{host_of(server)}\" is outside the bank's network (an internet address) - "
                          "this app only talks to servers inside it.")
    return server


# --- this process's own sockets ---------------------------------------------------------

def _target_ok(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # not an internet socket (AF_UNIX...)
    host = str(address[0])
    if host.lower() == "localhost" or address_ok(host):
        return True
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
        return False  # an internet IP
    except ValueError:
        return bool(host) and _resolves_inside(host) and _resolves_known(host)


def _resolves_known(host: str) -> bool:
    """A name passed straight to connect(): it must resolve (to inside addresses, checked above)."""
    try:
        return bool(_real_getaddrinfo(host, None))
    except (OSError, UnicodeError):
        return False


def _refuse(address):
    raise ConnectionRefusedError(f"Refused: {address[0]!r} is outside the bank's network - this app never "
                                 "connects to the internet.")


def _guarded_connect(self, address):
    if not _target_ok(address):
        _refuse(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _target_ok(address):
        _refuse(address)
    return _real_connect_ex(self, address)


def _guarded_sendto(self, data, *args):
    if args and not _target_ok(args[-1]):
        _refuse(args[-1])
    return _real_sendto(self, data, *args)


def block_outbound_sockets() -> None:
    """Once per process (create_app / main)."""
    global _installed
    if _installed:
        return
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.socket.sendto = _guarded_sendto
    _installed = True
