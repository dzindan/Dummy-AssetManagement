"""Core scanning logic: ping, hostname/domain resolution, quser sessions,
uptime, hardware - lifted from the standalone IP Scanner tool. Target-range
parsing (CIDR/dash ranges) and the concurrency-limit constants from that
tool aren't included here: Network Check always derives its target list
from a branch's own imported asset IPs (see routes/network_check.py), never
from free-text user input."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Optional

PING_TIMEOUT_MS = 800
SUBPROCESS_TIMEOUT_S = 4
WMI_TIMEOUT_S = 8
HARDWARE_TIMEOUT_S = 20

DEFAULT_CONCURRENCY = 20


def _run(cmd: list[str], timeout: float = SUBPROCESS_TIMEOUT_S) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        out = proc.stdout.decode("utf-8", errors="ignore") or proc.stdout.decode("cp850", errors="ignore")
        err = proc.stderr.decode("utf-8", errors="ignore") or proc.stderr.decode("cp850", errors="ignore")
        return proc.returncode, (out + err)
    except subprocess.TimeoutExpired:
        return -1, "Timeout"
    except FileNotFoundError as exc:
        return -1, str(exc)


def ping_host(ip: str) -> bool:
    code, _ = _run(["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip])
    return code == 0


_PING_HOSTNAME_RE = re.compile(r"Pinging\s+(\S+)\s+\[")


def resolve_hostname(ip: str) -> dict:
    """Best-effort hostname/domain resolution: `ping -a` first, then reverse DNS."""
    code, out = _run(["ping", "-a", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip])
    fqdn: Optional[str] = None
    match = _PING_HOSTNAME_RE.search(out)
    if match and match.group(1) != ip:
        fqdn = match.group(1)

    if not fqdn:
        try:
            fqdn = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            fqdn = None

    if not fqdn:
        return {"hostname": None, "domain": None, "fqdn": None}

    if "." in fqdn:
        host, domain = fqdn.split(".", 1)
        return {"hostname": host, "domain": domain, "fqdn": fqdn}
    return {"hostname": fqdn, "domain": None, "fqdn": fqdn}


_QUSER_LINE_RE = re.compile(
    r"^>?\s*(?P<username>\S+)\s+(?:(?P<sessionname>\S+)\s+)?(?P<id>\d+)\s+"
    r"(?P<state>Active|Disc|Conn)\S*\s+(?P<idle>\S+)\s+(?P<logon>.+?)\s*$"
)


def get_sessions(ip: str) -> dict:
    """Run `quser /server:<ip>` and parse logged-on user sessions."""
    code, out = _run(["quser", f"/server:{ip}"], timeout=SUBPROCESS_TIMEOUT_S)
    lines = [line.rstrip() for line in out.splitlines() if line.strip()]

    if code != 0 or not lines:
        message = lines[-1] if lines else "No response"
        return {"ok": False, "error": message, "sessions": []}

    if not lines[0].upper().lstrip().startswith("USERNAME"):
        return {"ok": False, "error": lines[0], "sessions": []}

    sessions = []
    for line in lines[1:]:
        m = _QUSER_LINE_RE.match(line)
        if not m:
            continue
        sessions.append(
            {
                "username": m.group("username"),
                "sessionname": m.group("sessionname") or "",
                "id": m.group("id"),
                "state": m.group("state"),
                "idle_time": m.group("idle"),
                "logon_time": m.group("logon"),
            }
        )
    if not sessions:
        return {"ok": True, "error": None, "sessions": []}
    return {"ok": True, "error": None, "sessions": sessions}


_UPTIME_PS_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$os = Get-WmiObject -Class Win32_OperatingSystem -ComputerName '{ip}'; "
    "$os.ConvertToDateTime($os.LastBootUpTime).ToString('yyyy-MM-ddTHH:mm:ss')"
)


def _format_uptime(delta_seconds: float) -> str:
    if delta_seconds < 0:
        delta_seconds = 0
    total_minutes = int(delta_seconds // 60)
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _validate_ip(ip: str) -> Optional[str]:
    """Reject anything that isn't a syntactically valid IP address. Both
    get_uptime and get_hardware interpolate `ip` directly into a PowerShell
    script string below - an unvalidated value (e.g. from an imported Excel
    cell) could otherwise break out of the quoted literal and inject
    arbitrary PowerShell. A real IP address can never contain the quote/
    semicolon characters needed to do that."""
    try:
        ipaddress.ip_address(ip)
        return None
    except ValueError:
        return f"Invalid IP address: {ip!r}"


def get_uptime(ip: str) -> dict:
    """Run Get-WmiObject Win32_OperatingSystem to compute uptime from LastBootUpTime."""
    invalid = _validate_ip(ip)
    if invalid:
        return {"ok": False, "error": invalid, "boot_time": None, "uptime": None}
    script = _UPTIME_PS_SCRIPT.format(ip=ip)
    code, out = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=WMI_TIMEOUT_S,
    )
    text = out.strip()
    if code != 0 or not text:
        message = text.splitlines()[-1] if text else "No response"
        return {"ok": False, "error": message, "boot_time": None, "uptime": None}

    try:
        boot_time = datetime.fromisoformat(text.splitlines()[-1].strip())
    except ValueError:
        return {"ok": False, "error": f"Unrecognized date format: {text}", "boot_time": None, "uptime": None}

    delta = (datetime.now() - boot_time).total_seconds()
    return {
        "ok": True,
        "error": None,
        "boot_time": boot_time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": _format_uptime(delta),
    }


_HARDWARE_PS_SCRIPT = r"""
$ip = '__IP__'
$result = @{}

try {
    $csys = Get-WmiObject -Class Win32_ComputerSystem -ComputerName $ip -ErrorAction Stop
    $bios = Get-WmiObject -Class Win32_BIOS -ComputerName $ip -ErrorAction Stop
    $result.system = @([PSCustomObject]@{Manufacturer=$csys.Manufacturer; Model=$csys.Model; Serial=$bios.SerialNumber})
} catch {
    $result.system_error = $_.Exception.Message
}

try {
    $disks = Get-WmiObject -Class Win32_DiskDrive -ComputerName $ip -ErrorAction Stop |
        Select-Object Model, SerialNumber, InterfaceType, @{Name='SizeGB';Expression={[math]::Round($_.Size/1GB,2)}}
    $result.disks = @($disks)
} catch {
    $result.disks_error = $_.Exception.Message
}

try {
    $monitors = Get-WmiObject -Namespace root\wmi -Class WmiMonitorID -ComputerName $ip -ErrorAction Stop | ForEach-Object {
        $model = ""
        if ($_.UserFriendlyName) {
            $model = ($_.UserFriendlyName | Where-Object {$_ -ne 0} | ForEach-Object {[char]$_}) -join ""
        }
        $serial = ""
        if ($_.SerialNumberID) {
            $serial = ($_.SerialNumberID | Where-Object {$_ -ne 0} | ForEach-Object {[char]$_}) -join ""
        }
        [PSCustomObject]@{Model=$model; SerialNumber=$serial}
    }
    $result.monitors = @($monitors)
} catch {
    $result.monitors_error = $_.Exception.Message
}

try {
    $nics = Get-WmiObject -Class Win32_NetworkAdapterConfiguration -ComputerName $ip -Filter "IPEnabled='True'" -ErrorAction Stop
    $nic = $nics | Where-Object { $_.IPAddress -contains $ip } | Select-Object -First 1
    if (-not $nic) { $nic = $nics | Select-Object -First 1 }
    $result.mac_address = $nic.MACAddress
} catch {
    $result.mac_address_error = $_.Exception.Message
}

try {
    $cpu = Get-WmiObject -Class Win32_Processor -ComputerName $ip -ErrorAction Stop |
        Select-Object Name, NumberOfCores, NumberOfLogicalProcessors
    $result.cpu = @($cpu)
} catch {
    $result.cpu_error = $_.Exception.Message
}

try {
    $mem = Get-WmiObject -Class Win32_PhysicalMemory -ComputerName $ip -ErrorAction Stop |
        Select-Object Manufacturer, Speed, @{Name='CapacityGB';Expression={[math]::Round($_.Capacity/1GB,2)}}, DeviceLocator
    $result.memory = @($mem)
} catch {
    $result.memory_error = $_.Exception.Message
}

$result | ConvertTo-Json -Depth 5 -Compress
"""


def _wmi_category(raw: dict, key: str) -> dict:
    error = raw.get(f"{key}_error")
    if error:
        return {"ok": False, "error": error, "items": []}
    items = raw.get(key)
    if items is None:
        items = []
    elif isinstance(items, dict):
        items = [items]
    return {"ok": True, "error": None, "items": items}


def _mac_result(raw: dict) -> dict:
    error = raw.get("mac_address_error")
    if error:
        return {"ok": False, "error": error, "mac": None}
    return {"ok": True, "error": None, "mac": raw.get("mac_address")}


def get_hardware(ip: str) -> dict:
    """Run Get-WmiObject against ComputerSystem/BIOS, DiskDrive, WmiMonitorID, Processor, PhysicalMemory, NIC MAC."""
    invalid = _validate_ip(ip)
    if invalid:
        empty = {"ok": False, "error": invalid, "items": []}
        empty_mac = {"ok": False, "error": invalid, "mac": None}
        return {
            "system": empty, "disks": empty, "monitors": empty, "cpu": empty, "memory": empty,
            "mac_address": empty_mac,
        }
    script = _HARDWARE_PS_SCRIPT.replace("__IP__", ip)
    code, out = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=HARDWARE_TIMEOUT_S,
    )
    text = out.strip()
    if not text:
        message = "Timeout" if code == -1 else "No response"
        empty = {"ok": False, "error": message, "items": []}
        empty_mac = {"ok": False, "error": message, "mac": None}
        return {
            "system": empty, "disks": empty, "monitors": empty, "cpu": empty, "memory": empty,
            "mac_address": empty_mac,
        }

    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        message = f"Could not parse PowerShell output: {text[:200]}"
        empty = {"ok": False, "error": message, "items": []}
        empty_mac = {"ok": False, "error": message, "mac": None}
        return {
            "system": empty, "disks": empty, "monitors": empty, "cpu": empty, "memory": empty,
            "mac_address": empty_mac,
        }

    return {
        "system": _wmi_category(raw, "system"),
        "disks": _wmi_category(raw, "disks"),
        "monitors": _wmi_category(raw, "monitors"),
        "cpu": _wmi_category(raw, "cpu"),
        "memory": _wmi_category(raw, "memory"),
        "mac_address": _mac_result(raw),
    }


def scan_ip(ip: str, include_hardware: bool = False) -> dict:
    alive = ping_host(ip)
    result = {
        "ip": ip,
        "alive": alive,
        "hostname": None,
        "domain": None,
        "sessions": None,
        "uptime": None,
        "hardware": None,
    }
    if alive:
        info = resolve_hostname(ip)
        result["hostname"] = info["hostname"]
        result["domain"] = info["domain"]
        result["sessions"] = get_sessions(ip)
        result["uptime"] = get_uptime(ip)
        if include_hardware:
            result["hardware"] = get_hardware(ip)
    return result


def run_scan(
    targets: list[str],
    on_result: Callable[[dict], None],
    max_workers: int = DEFAULT_CONCURRENCY,
    include_hardware: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(scan_ip, ip, include_hardware): ip for ip in targets}
        pending = set(futures)
        for future in as_completed(futures):
            pending.discard(future)
            ip = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - surface any per-host failure, don't kill the scan
                result = {
                    "ip": ip,
                    "alive": False,
                    "hostname": None,
                    "domain": None,
                    "sessions": None,
                    "uptime": None,
                    "hardware": None,
                    "error": str(exc),
                }
            on_result(result)

            if should_stop is not None and should_stop():
                # Cancel whatever hasn't started yet; futures already running are
                # bounded by their own subprocess timeouts and finish on their own.
                for f in pending:
                    f.cancel()
                break
