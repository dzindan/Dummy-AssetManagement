"""Export for importing AM's assets into ManageEngine AssetExplorer
(user's request 2026-11-23; AssetExplorer: Assets -> Import).

2026-11-27 (user): laid out like the import template the user exported from
their AssetExplorer (XLSForAssets.xls, not kept in git): one .xlsx (user: an
.xlsx is fine) with one sheet per asset group - Computers, IT Assets,
Components, Non IT Assets, Mobile Devices (only the groups that have rows) -
each with that sheet's columns exactly (GROUP_COLUMNS). AM's device
goes to a group by DEVICE_GROUP (user's pick 2026-11-27; anything else = IT
Assets). The template has no Product Type column (the group is the sheet):
AM's device is the first part of Description instead.

Choices the user made:
- Asset Name = <branch no>-<device>-<serial>, e.g. "8061-PC-5CD1234XYZ".
  No serial -> "<branch>-<device>-NOSN-<code>", the code made from what
  identifies the row (not the row id: every monthly import makes new ids, and
  AssetExplorer would get the same device again as a new asset); a name used
  by several rows gets "-2", "-3"... Names are worked out over every current
  row in a fixed order, so a filter or sort doesn't change them.
- Site = branch English name, Department = the unit of the user's branch
  code in the Aither ID list (user, 2026-11-25), else AM's Position column, User = "FULL NAME(USER ID)".
- The rows on screen (same filters / As of month as Manage Assets).
- User Login Name = the user ID (= the AD logon name, like AAL / SG), only
  when it is a staff ID of 3-10 digits (login_name).

Anything AE has no field for (AM's device, status, handover date, remark,
period) goes into Description so nothing is lost - except on Mobile Devices,
whose template has no Description column.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

from .asset_os import os_text
from .exports import _append

# The columns of the user's AssetExplorer template (XLSForAssets.xls), sheet by sheet, as written
# there ("Memory Shares/Weight " with its space), except the Computers sheet's "producT" /
# "asset name" / "location", written with the capitals every other sheet uses.
_COMMON = ["Product", "Asset Name", "Location", "Asset State", "User Login Name", "User Domain", "User",
           "Department", "Site", "Vendor", "Org Serial Number", "Barcode", "Asset Tag", "Purchase Cost",
           "Acquisition Date", "Warranty Expiry Date", "Expiry Date"]
GROUP_COLUMNS = {
    "Computers": _COMMON + [
        "Total Memory (GB)", "Service Tag", "HardDisk Serial Number", "Hard Disk Model", "Processor", "Domain",
        "Capacity (GB)", "Description", "Processor Speed(in Ghz)", "MAC Address", "OS Version", "IP Address",
        "Total Slots", "Operating System", "VM Platform", "Installed Vms", "VM Name", "Memory Reservation (MB)",
        "Memory Shares/Weight ", "CPU Limit (%)", "CPU Reservation (MHz)", "CPU Shares Level", "CPU Shares/Weight",
        "Memory Shares Level", "Memory Limit (MB)", "CPU configured", "Memory configured", "CPU Reservation (%)"],
    "IT Assets": _COMMON + ["Description", "MAC Address", "IP Address", "Firmware", "Firmware Revision"],
    "Components": _COMMON + ["Description"],
    "Non IT Assets": _COMMON + ["Description"],
    "Mobile Devices": _COMMON + [
        "IMEI", "UDID", "Serial Number", "OS Type", "Total Capacity (GB)", "Available Capacity (GB)",
        "Modem Firmware Version", "MAC Address", "Build Version", "OS Version", "IP Address",
        "Hardware Encription"],
}
DEFAULT_GROUP = "IT Assets"
# AM device -> AE group (user's pick, 2026-11-27); a device not listed -> IT Assets.
DEVICE_GROUP = {
    **dict.fromkeys(("PC", "NOTEBOOK", "SERVER PC"), "Computers"),
    "TABLET": "Mobile Devices",
    **dict.fromkeys(("LCD", "KEYBOARD", "COMPUTER MOUSE", "RAM", "SSD", "PORTABLE HDD", "HEADSET", "WEBCAM",
                     "SPEAKER", "MICROPHONE", "ADAPTER", "ACCESSORIES"), "Components"),
    **dict.fromkeys(("PAPER SHREDDER", "CURRENCY COUNTER", "CURRENCY VERIFIER", "BANKNOTE BANDING MACHINE",
                     "TABLET STAND"), "Non IT Assets"),
}

# AM status -> AssetExplorer's built-in asset states (In Use, In Store,
# In Repair, Expired, Disposed). RGM/GM/DGM/CGM are job titles typed into
# the status column by branches - the device is with that person.
# Anything not listed: In Use when the row has a user, else In Store.
STATE_MAP = {
    "USING LOCAL": "In Use",
    "USING INTERNET": "In Use",
    "USING LOCAL/ITN": "In Use",
    "RENTAL": "In Use",
    "RGM": "In Use",
    "GM": "In Use",
    "DGM": "In Use",
    "CGM": "In Use",
    "WAREHOUSE": "In Store",
    "RETURN": "In Store",
    "BROKEN": "In Repair",
    "LOST": "Disposed",
    "SENT TO KOREAN": "Disposed",
}

# Serial cells that mean "no serial" in branch reports.
_NO_SERIAL = {"", "NA", "N/A", "N/AA", "NN/A", "NONE", "NO", "KHÔNG CÓ"}


def _text(value: Any) -> str:
    # one line: a line break inside a cell can break AE's import
    return " ".join(ILLEGAL_CHARACTERS_RE.sub("", str(value or "")).split())


def _has_serial(serial: str) -> bool:
    """False for blanks, N/A variants and remarks ("MẤT SERI", "LOST OF
    SERVICE TAG": several words without a digit)."""
    if serial.upper() in _NO_SERIAL or not any(ch.isalnum() for ch in serial):
        return False
    return any(ch.isdigit() for ch in serial) or " " not in serial


def asset_state(status: str, user_id: str) -> str:
    status = _text(status).upper()
    if status in STATE_MAP:
        return STATE_MAP[status]
    return "In Use" if _text(user_id) and _text(user_id).upper() != "NA" else "In Store"


def _clean(value: Any) -> str:
    text = _text(value)
    return "" if text.upper() in ("NA", "N/A") or not any(ch.isalnum() for ch in text) else text


def user_field(full_name: Any, user_id: Any) -> str:
    """User = "FULL NAME(ID)" (user, 2026-11-23); only one of the two ->
    just that one."""
    name, uid = _clean(full_name), _clean(user_id)
    if name and uid:
        return f"{name}({uid})"
    return name or uid


def login_name(user_id: Any) -> str:
    """AE's User Login Name: only a staff ID (3-10 digits). The branch reports' User ID cells also hold
    "WAREHOUSE", branch names, titles, two IDs... (242 of 17 488 rows, 2026-11-27) - AE would look
    those up as logons; they stay in the User column."""
    text = _clean(user_id)
    return text if re.fullmatch(r"\d{3,10}", text) else ""


def group_of(device: Any) -> str:
    return DEVICE_GROUP.get(_text(device).upper(), DEFAULT_GROUP)


def _description(row) -> str:
    parts = [
        ("Device", row["device_name"]),
        ("AM status", row["status"]),
        ("Handover date", row["handover_date"]),
        ("Remark", row["remark"]),
        ("AM period", row["period"]),
    ]
    return "; ".join(f"{label}: {_text(v)}" for label, v in parts if _text(v) and _text(v).upper() != "NA")


def asset_names(rows: Iterable) -> dict[int, str]:
    """row id -> Asset Name, unique (ignoring case) over `rows`."""
    base: dict[int, str] = {}
    rows = sorted(rows, key=lambda r: (_text(r["user_id_norm"]), _text(r["model_device"]), r["id"]))
    for r in rows:
        serial = _text(r["serial_tag"])
        if not _has_serial(serial):
            identity = "|".join(_text(r[k]).upper() for k in ("asset_key", "model_device", "user_id_norm"))
            serial = "NOSN-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8].upper()
        base[r["id"]] = (f"{_text(r['branch_no']) or 'UNRESOLVED'}-"
                         f"{_text(r['device_name']) or 'UNKNOWN DEVICE'}-{serial}")
    taken = {name.upper() for name in base.values()}  # a "-2" must not be another row's own name
    first: set[str] = set()
    names = {}
    for row_id, name in base.items():
        out, n = name, 1
        if name.upper() in first:
            while out.upper() in taken:
                n += 1
                out = f"{name}-{n}"
            taken.add(out.upper())
        first.add(name.upper())
        names[row_id] = out
    return names


def _values(r, name: str) -> dict[str, str]:
    """One AM row as AE fields (each group's columns take what they have)."""
    device = _text(r["device_name"]) or "UNKNOWN DEVICE"
    serial = _text(r["serial_tag"])
    serial = serial if _has_serial(serial) else ""
    model = _text(r["model_device"])
    user = _text(r["user_id_raw"])
    return {
        "Product": model if model and model.upper() != "NA" else f"{device} - UNKNOWN MODEL",
        "Asset Name": name,
        "Location": _clean(r["branch_dept"]),
        "Asset State": asset_state(r["status"], user),
        "User Login Name": login_name(user),
        "User": user_field(r["full_name"], user),
        "Department": _clean(r["user_department"]) or _clean(r["position"]),
        "Site": _text(r["branch_eng_name"]) or _text(r["branch_dept"]),
        "Org Serial Number": serial,
        "Serial Number": serial,
        "Description": _description(r),
        "IP Address": "" if _text(r["ip"]).upper() == "NA" else _text(r["ip"]),
        "Operating System": os_text(r),
    }


def build_groups(rows: Iterable, names: dict[int, str] | None = None) -> dict[str, list[list[str | None]]]:
    """{AE group: rows in that group's columns}, in GROUP_COLUMNS' order, only groups with rows.
    `names`: asset_names() of every current row when `rows` is filtered."""
    rows = list(rows)
    names = asset_names(rows) if names is None else names
    out: dict[str, list[list[str | None]]] = {group: [] for group in GROUP_COLUMNS}
    for r in rows:
        group = group_of(r["device_name"])
        values = _values(r, names[r["id"]])
        # nothing to say = a truly empty cell (None): openpyxl writes "" as an empty string cell,
        # which an importer may read as a bad number / date in Purchase Cost, the date columns...
        out[group].append([values.get(column.strip(), "") or None for column in GROUP_COLUMNS[group]])
    return {group: lines for group, lines in out.items() if lines}


def build_workbook(rows: Iterable, names: dict[int, str] | None = None) -> openpyxl.Workbook:
    """The download: one sheet per AE group that has rows, named and laid out like the template
    (header row = GROUP_COLUMNS[group]); every value written as text - a branch's "=..." stays text
    (exports._append), never a formula."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for group, lines in build_groups(rows, names).items():
        ws = wb.create_sheet(group)
        ws.append(GROUP_COLUMNS[group])
        for line in lines:
            _append(ws, line)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
    if not wb.worksheets:  # a filter with no rows: an empty sheet, so the file still opens
        wb.create_sheet(DEFAULT_GROUP).append(GROUP_COLUMNS[DEFAULT_GROUP])
    return wb
