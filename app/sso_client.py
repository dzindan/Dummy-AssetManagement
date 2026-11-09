"""Windows sign-in with no typing on the client PCs (2026-11-09).

The server already answers the browser's Negotiate handshake (winauth.py).
Whether the browser answers by itself, or asks for a username and password
first, is the CLIENT's decision: Edge / Chrome / IE answer silently only for
a site in the "Local intranet" zone. A dotless name (https://AMSERVER:8737)
is in that zone by default; a full DNS name (amserver.corp.local) or an IP
address is not, so the browser pops up a password box.

So: give people the dotless address, and for the full name put the site in
the Local intranet zone - by GPO for everyone, or with the .reg file below
for one PC (a per-user setting, no admin rights needed).
"""

from __future__ import annotations

import ipaddress
import socket


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def host_names(cert_names: list[str] | None = None) -> list[str]:
    """[short name, full DNS name] of this server (no IPs, no localhost). With
    a certificate, only the names it is valid for (else HTTPS fails there)."""
    names = []
    for name in (socket.gethostname(), socket.getfqdn()):
        if name and not _is_ip(name) and name.lower() != "localhost" \
                and name.lower() not in (n.lower() for n in names):
            names.append(name)
    if cert_names is not None:
        allowed = {n.lower() for n in cert_names}
        names = [n for n in names if n.lower() in allowed]
    return names


def address(config: dict, cert_names: list[str] | None = None) -> str:
    """The address to give the clients: the dotless name when it works."""
    scheme = "https" if config.get("tls_cert") else "http"
    names = host_names(cert_names if config.get("tls_cert") else None)
    dotless = [n for n in names if "." not in n]
    host = (dotless or names or [socket.getfqdn() or "localhost"])[0]
    return f"{scheme}://{host}:{config['port']}"


def reg_file(names: list[str], scheme: str) -> str:
    """A .reg file putting the server in the current user's Local intranet zone
    (zone 1). One key per name: 'amserver' alone, 'amserver.corp.local' as
    Domains\\corp.local\\amserver."""
    base = r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap\Domains"
    lines = ["Windows Registry Editor Version 5.00", "",
             "; Asset Management Tool: Windows sign-in with no password box (Local intranet zone)", ""]
    for name in names:
        if "." in name:
            host, domain = name.split(".", 1)
            key = f"{base}\\{domain}\\{host}"
        else:
            key = f"{base}\\{name}"
        lines += [f"[{key}]", f'"{scheme}"=dword:00000001', ""]
    return "\r\n".join(lines)
