"""Operating systems from Active Directory (user, 2026-11-25) - step 2 of
app/asset_os.py: PCs no scan has reached, or that changed OS since.

AD knows every domain computer's OS, switched off or not, but not which
asset it is - at the bank only hostname and IP are there to go by (user,
2026-11-25). A computer is matched to a PC's serial by, in this order:

- "hostname": a Network Check scan read that machine name and the serial
  from the same machine;
- "ip" (can be turned off): its address - ipHostNumber or an IP in its
  description if AD has one, else what its DNS name leads to - is one
  exactly one current PC has, and no other computer has. Stale DNS / DHCP /
  a wrong IP in a report puts the OS on another PC, so it never beats a scan;
- "serial": a PC's serial stands in the computer's name, description or
  serialNumber attribute.

One computer and one serial are only ever paired once, the surer way first.
Read through ADSI (no RSAT needed) as the account the app runs as; read-only.
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor

from .asset_os import PC_DEVICE_NAMES, usable_serial_key
from .queries import get_current_assets
from .scanner import _clean_os_name, _run
from .text_utils import serial_key

READ_TIMEOUT_S = 180
DNS_WORKERS = 40
# By IP: only computers seen lately - an old DNS record points anywhere.
IP_MATCH_DAYS = 45
# By serial: shorter or digit-less words in a description match by chance.
MIN_SERIAL_IN_TEXT = 7

_FIELDS = ("name", "dns", "description", "serial", "os", "version", "uac", "logon", "ip")
_DOMAIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,250}[A-Za-z0-9])?")

# One tab-separated line per computer, the whole answer base64 so that
# neither the console code page nor _run()'s joined stderr can spoil it.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  $server = '__SERVER__'
  $base = [string]([ADSI]"LDAP://${server}RootDSE").defaultNamingContext
  if (-not $base) { throw 'No domain answered - is this computer joined to a domain?' }
  function V($p, $name) {
    if ($p[$name].Count) { (@($p[$name] | ForEach-Object { [string]$_ }) -join ' ') -replace "[`t`r`n]", ' ' } else { '' }
  }
  $s = New-Object System.DirectoryServices.DirectorySearcher([ADSI]"LDAP://$server$base")
  $s.PageSize = 1000
  $s.Filter = '(&(objectCategory=computer)(operatingSystem=*))'
  foreach ($n in 'name','dNSHostName','description','serialNumber','operatingSystem','operatingSystemVersion',
                 'userAccountControl','lastLogonTimestamp','ipHostNumber') { [void]$s.PropertiesToLoad.Add($n) }
  $lines = New-Object System.Collections.Generic.List[string]
  $all = $s.FindAll()
  try {
    foreach ($r in $all) {
      $p = $r.Properties
      $lines.Add(((V $p 'name'), (V $p 'dnshostname'), (V $p 'description'), (V $p 'serialnumber'),
                  (V $p 'operatingsystem'), (V $p 'operatingsystemversion'), (V $p 'useraccountcontrol'),
                  (V $p 'lastlogontimestamp'), (V $p 'iphostnumber')) -join "`t")
    }
  } finally { $all.Dispose() }
  'B64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($lines -join "`n"))
} catch {
  'ERR:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_.Exception.Message))
}
"""


class AdReadError(Exception):
    pass


def clean_version(text: str) -> str:
    """AD's "10.0 (19045)" -> "10.0.19045", as WMI writes it."""
    return re.sub(r"\s*\((\d+)\)\s*$", r".\1", (text or "").strip())


def _logon_text(filetime: str) -> str:
    """FILETIME -> local "YYYY-MM-DD HH:MM:SS", "" when never / unreadable."""
    try:
        value = int(filetime)
        if value <= 0:
            return ""
        utc = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=value // 10)
        return utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return ""


def parse_computers(text: str) -> list[dict]:
    """The script's lines -> enabled computers with an OS."""
    out = []
    for line in text.split("\n"):
        parts = [p.strip() for p in line.rstrip("\r").split("\t")]
        if len(parts) != len(_FIELDS):
            continue
        c = dict(zip(_FIELDS, parts))
        disabled = c["uac"].isdigit() and int(c["uac"]) & 2
        c["os"] = _clean_os_name(c["os"])
        if not c["name"] or not c["os"] or disabled:
            continue
        c["version"] = clean_version(c["version"])
        c["logon"] = _logon_text(c["logon"])
        out.append(c)
    return out


def read_computers(domain: str = "") -> list[dict]:
    """Every enabled computer of `domain` (blank = this computer's own)."""
    domain = (domain or "").strip()
    if domain and not _DOMAIN_RE.fullmatch(domain):
        raise AdReadError("Domain / DC: a name like corp.local or DC01, without spaces or \\.")
    script = _SCRIPT.replace("__SERVER__", f"{domain}/" if domain else "")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    _code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                      timeout=READ_TIMEOUT_S)
    for line in out.splitlines():
        if line.startswith(("B64:", "ERR:")):
            text = base64.b64decode(line[4:]).decode("utf-8", "replace")
            if line.startswith("ERR:"):
                raise AdReadError(text.strip() or "Active Directory did not answer.")
            return parse_computers(text)
    raise AdReadError(out.strip()[:300] or "Active Directory did not answer.")


def _short(name: str) -> str:
    return (name or "").split(".")[0].strip().upper()


def _texts_serials(c: dict) -> set[str]:
    """What could be a serial in the computer's own AD text."""
    words = set()
    for text in (c["name"], c["description"], c["serial"]):
        words |= {serial_key(w) for w in re.split(r"[\s,;:/|()\[\]=]+", text)}
        words |= {serial_key(w) for w in re.split(r"[^A-Za-z0-9]+", text)}
    words.discard("")
    return words


def _resolve(name: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(name, None, socket.AF_INET)}
    except (OSError, UnicodeError):
        return set()


def _ips_in(text: str) -> set[str]:
    out = set()
    for word in re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\.?\d)", text or ""):
        try:
            out.add(str(ipaddress.IPv4Address(word)))
        except ValueError:
            pass
    return out


def _addresses(c: dict, resolve) -> set[str]:
    """What AD itself says, else DNS."""
    return (_ips_in(c["ip"]) or _ips_in(c["description"])
            or (resolve(c["dns"]) if c["dns"] else set()))


def match(computers: list[dict], hosts: dict[str, str], pcs: list[tuple[str, str]], by_ip: bool = True,
          resolve=_resolve, today: dt.date | None = None) -> dict[str, tuple[dict, str]]:
    """serial_key -> (computer, matched_by). `hosts`: scanned machine name ->
    serial_key. `pcs`: (serial_key, ip) of the current PCs."""
    # Several AD accounts for one PC (renamed, re-joined): the one used last.
    computers = sorted(computers, key=lambda c: (c["logon"], c["name"]), reverse=True)
    found: dict[str, tuple[dict, str]] = {}
    used: set[int] = set()

    def pair(i: int, key: str, how: str) -> None:
        if key and key not in found and i not in used:
            found[key] = (computers[i], how)
            used.add(i)

    for i, c in enumerate(computers):
        pair(i, hosts.get(_short(c["name"]), ""), "hostname")

    if by_ip:
        keys_at: dict[str, set[str]] = {}
        for key, ip in pcs:
            try:
                keys_at.setdefault(str(ipaddress.IPv4Address((ip or "").strip())), set()).add(key)
            except ValueError:
                continue
        since = ((today or dt.date.today()) - dt.timedelta(days=IP_MATCH_DAYS)).isoformat()
        # Every recent computer is looked up, matched or not: one already
        # matched standing at an address rules that address out for another.
        recent = [i for i, c in enumerate(computers) if c["logon"] >= since]
        with ThreadPoolExecutor(max_workers=DNS_WORKERS) as pool:
            answers = list(pool.map(lambda i: _addresses(computers[i], resolve), recent))
        at: dict[str, list[int]] = {}
        for i, ips in zip(recent, answers):
            for ip in ips:
                at.setdefault(ip, []).append(i)
        choices: dict[int, set[str]] = {}
        for ip, who in at.items():
            keys = keys_at.get(ip, set())
            if len(who) == 1 and len(keys) == 1:
                choices.setdefault(who[0], set()).update(keys)
        for i in sorted(choices):
            if len(choices[i]) == 1:  # two addresses, two PCs: no telling which
                pair(i, next(iter(choices[i])), "ip")

    known = {k for k, _ip in pcs if len(k) >= MIN_SERIAL_IN_TEXT and any(ch.isdigit() for ch in k)}
    for i, c in enumerate(computers):
        hits = set() if i in used else _texts_serials(c) & known
        # prefix + serial as the machine name ("SHB5CD1234XYZ")
        if i not in used and not hits:
            name = serial_key(c["name"])
            hits = {name[n:] for n in range(1, len(name) - MIN_SERIAL_IN_TEXT + 1)} & known
        if len(hits) == 1:  # two serials named: no telling which is the PC's
            pair(i, hits.pop(), "serial")
    return found


def current_pcs(conn) -> list[tuple[str, str]]:
    """(serial key, ip) of every current PC; the key is "" without a usable
    serial - it still counts as standing at its IP."""
    return [(usable_serial_key(r["serial_tag"]), r["ip"] or "")
            for r in get_current_assets(conn) if r["device_name"] in PC_DEVICE_NAMES]


def scanned_hosts(conn) -> dict[str, str]:
    """Machine name -> serial key, the latest scan of a name winning."""
    return {_short(r["hostname"]): r["serial_key"] for r in conn.execute(
        "SELECT hostname, serial_key FROM asset_os WHERE hostname != '' ORDER BY scanned_at")}


def save(conn, found: dict[str, tuple[dict, str]]) -> None:
    """The new read replaces the previous one (caller commits)."""
    conn.execute("DELETE FROM asset_os_ad")
    conn.executemany(
        "INSERT INTO asset_os_ad (serial_key, hostname, os_name, os_version, last_logon, matched_by, read_at)"
        " VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))",
        [(key, c["name"], c["os"], c["version"], c["logon"], how) for key, (c, how) in found.items()])
