"""Operating system per PC, learnt from Network Check scans (user, 2026-11-25).

The monthly reports carry no OS, and every import replaces a branch's rows -
so it isn't a column of asset_items but its own table, keyed by the PC's
serial as WMI reports it, joined in when the list is read
(queries.search_assets). A serial corrected later (Network Check's Update, a
hand edit, the next report) picks its OS up by itself.

One row per serial = the last scan that read both the serial (needs "Scan
hardware") and the OS; a failed scan never blanks an earlier result.
"""

from __future__ import annotations

from .text_utils import serial_key

PC_DEVICE_NAMES = ("PC", "NOTEBOOK", "SERVER PC")

# Manage Assets' OS filter: PCs neither a scan nor AD knows the OS of.
NO_OS = "(No OS info)"

# asset_os_ad.matched_by -> the OS source shown (see app/ad_os.py).
AD_MATCH_LABELS = {"hostname": "AD (by hostname)", "ip": "AD (by IP)", "serial": "AD (by serial)"}

# What boards/VMs without a real serial answer - shared by many machines, so
# one scan would put its OS on all of them.
_JUNK_SERIALS = {serial_key(s) for s in (
    "To be filled by O.E.M.", "Default string", "System Serial Number", "Not Specified", "Not Applicable",
    "Not Available", "None", "N/A", "Unknown", "Invalid", "O.E.M.", "Serial Number",
    "Chassis Serial Number", "123456789", "1234567890", "0123456789")}


def usable_serial_key(serial: str) -> str:
    """The key to keep a scanned serial under, "" for a placeholder."""
    key = serial_key(serial)
    if len(key) < 4 or key in _JUNK_SERIALS or len(set(key)) == 1:
        return ""
    return key


def save_scan(conn, serial: str, ip: str, hostname: str, os_info: dict | None) -> bool:
    """Keep one scanned machine's OS. -> whether there was anything to keep."""
    key = usable_serial_key(serial)
    name = (os_info or {}).get("name") or ""
    if not key or not name:
        return False
    conn.execute(
        "INSERT INTO asset_os (serial_key, serial, hostname, ip, os_name, os_version, os_arch, install_date, scanned_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))"
        " ON CONFLICT (serial_key) DO UPDATE SET serial = excluded.serial,"
        " hostname = COALESCE(NULLIF(excluded.hostname, ''), hostname), ip = excluded.ip,"
        " os_name = excluded.os_name, os_version = excluded.os_version, os_arch = excluded.os_arch,"
        " install_date = excluded.install_date, scanned_at = excluded.scanned_at",
        # the name the machine itself gives: DNS may still hold another PC's
        (key, serial.strip(), os_info.get("hostname") or hostname or "", ip, name, os_info.get("version") or "",
         os_info.get("arch") or "", os_info.get("install_date") or ""))
    return True


def os_options(conn) -> list[str]:
    return [NO_OS] + [r["os_name"] for r in conn.execute(
        "SELECT os_name FROM asset_os UNION SELECT os_name FROM asset_os_ad ORDER BY os_name")]


def os_text(row) -> str:
    """"Windows 10 Pro (10.0.19045)" from a row with os_name / os_version."""
    name, version = row["os_name"] or "", row["os_version"] or ""
    return f"{name} ({version})" if name and version else name
