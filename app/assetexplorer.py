"""CSV export for importing AM's assets into ManageEngine AssetExplorer
(user's request 2026-11-23; AssetExplorer: Assets -> Import from CSV).

Choices the user made:
- Asset Name = <branch no>-<device>-<serial>, e.g. "8061-PC-5CD1234XYZ".
  No serial -> "<branch>-<device>-NOSN-<row id>"; a name already used in
  the same file (duplicate serial) gets "-2", "-3"... so every row stays
  importable (AssetExplorer needs unique asset names).
- Site = branch English name, Department = AM's Position column (the HO
  Dept text), User = "FULL NAME(USER ID)".
- One CSV for the rows on screen (same filters / As of month as Manage
  Assets), with Product Type = AM's Device.

Column headers use AssetExplorer's field names so the import wizard's field
mapping lines up; anything AE has no field for (AM status, full name,
handover date, remark, period) goes into Description so nothing is lost.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

COLUMNS = [
    "Asset Name",
    "Product Type",
    "Product",
    "Serial Number",
    "Asset State",
    "Site",
    "Department",
    "User",
    "IP Address",
    "Description",
]

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
_NO_SERIAL = {"", "NA", "N/A", "-", "NONE", "NO", "KHÔNG CÓ"}


def _text(value: Any) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", str(value or "")).strip()


def asset_state(status: str, user_id: str) -> str:
    status = _text(status).upper()
    if status in STATE_MAP:
        return STATE_MAP[status]
    return "In Use" if _text(user_id) and _text(user_id).upper() != "NA" else "In Store"


def _clean(value: Any) -> str:
    text = _text(value)
    return "" if text.upper() == "NA" else text


def user_field(full_name: Any, user_id: Any) -> str:
    """User = "FULL NAME(ID)" (user, 2026-11-23); only one of the two ->
    just that one."""
    name, uid = _clean(full_name), _clean(user_id)
    if name and uid:
        return f"{name}({uid})"
    return name or uid


def _description(row) -> str:
    parts = [
        ("AM status", row["status"]),
        ("Handover date", row["handover_date"]),
        ("Remark", row["remark"]),
        ("AM period", row["period"]),
    ]
    return "; ".join(f"{label}: {_text(v)}" for label, v in parts if _text(v) and _text(v).upper() != "NA")


def build_rows(rows: Iterable) -> list[list[str]]:
    out = []
    used: dict[str, int] = {}
    for r in rows:
        device = _text(r["device_name"]) or "UNKNOWN DEVICE"
        branch = _text(r["branch_no"]) or "UNRESOLVED"
        serial = _text(r["serial_tag"])
        has_serial = serial.upper() not in _NO_SERIAL
        name = f"{branch}-{device}-{serial if has_serial else 'NOSN-' + str(r['id'])}"
        n = used.get(name.upper(), 0) + 1
        used[name.upper()] = n
        if n > 1:
            name = f"{name}-{n}"
        model = _text(r["model_device"])
        user = _text(r["user_id_raw"])
        out.append([
            name,
            device,
            model if model and model.upper() != "NA" else f"{device} - UNKNOWN MODEL",
            serial if has_serial else "",
            asset_state(r["status"], user),
            _text(r["branch_eng_name"]) or _text(r["branch_dept"]),
            "" if _text(r["position"]).upper() == "NA" else _text(r["position"]),
            user_field(r["full_name"], user),
            "" if _text(r["ip"]).upper() == "NA" else _text(r["ip"]),
            _description(r),
        ])
    return out


def build_csv(rows: Iterable) -> bytes:
    """UTF-8 with BOM, so Vietnamese names survive both AE's importer and
    Excel if someone opens the file first."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    writer.writerows(build_rows(rows))
    return buf.getvalue().encode("utf-8-sig")
