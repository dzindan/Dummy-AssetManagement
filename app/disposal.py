"""Disposal Check (user request 2026-10-08): read a disposal / fixed-asset
inventory list exported from Aither (e.g. "8064_Tran Duy Hung_ Small_work_
Oct 2026.xlsx": sheet1 = the branch's FA/WT list with the inventory result,
"Remark/Suggestion" / "GAD opinion" saying which items to dispose; sheet2 =
working tools newly registered for it) and check every item against what
this app has, by serial number. Read-only - nothing is written to the DB.

Each list item ends in one result (RESULTS, worst first for sorting):
- in_use:        found in the branch's current assets but still USING ... -
                 an item to dispose should be in the warehouse / broken;
- other_branch:  found in current assets, but at a different branch than
                 the list's Branch ID;
- not_current:   the serial was imported before but isn't in any branch's
                 current snapshot any more (last seen branch/period shown);
- not_in_app:    the serial was never imported;
- duplicate:     the same serial is on the list more than once;
- ok:            current, same branch, not in use (WAREHOUSE, BROKEN...);
- no_serial:     no serial on the list ("N/A" - furniture, software...),
                 nothing to check against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import openpyxl
from unidecode import unidecode

from .queries import CURRENT_ASSETS_CTE

RESULTS = {
    "in_use": ("Still in use", "badge-removed"),
    "other_branch": ("Different branch", "badge-changed"),
    "not_current": ("Not in current assets", "badge-changed"),
    "not_in_app": ("Not in the app", "badge-removed"),
    "duplicate": ("Duplicate serial on the list", "badge-changed"),
    "ok": ("OK", "badge-added"),
    "no_serial": ("No serial - not checked", "badge-unchanged"),
}

# Header text (accent-stripped, upper case, spaces collapsed) -> field.
# The first alias found in a sheet's header row wins.
HEADER_ALIASES = {
    "no": ["NO", "NO.", "STT"],
    "branch": ["BRANCH ID", "BRANCH", "BRANCH NO"],
    "name": ["FA/ WT NAME", "FA/WT NAME", "ASSET NAME", "DEVICE NAME", "NAME"],
    "ref": ["REF. NUMBER", "REF NUMBER", "REF. NO", "REF NO", "ASSET CODE"],
    "register_date": ["REGISTER DATE"],
    "classification": ["CLASSIFICATION"],
    "serial": ["SERIAL NUMBER/ SPECIFICATION", "SERIAL NUMBER/SPECIFICATION", "SERIAL NUMBER", "SERIAL NO",
               "SERIAL/ SERVICE TAG", "SERIAL", "SERVICE TAG"],
    "inventory_status": ["INVENTORY STATUS"],
    "remark": ["REMARK/SUGGESTION", "REMARK / SUGGESTION", "REMARK", "SUGGESTION"],
    "gad_opinion": ["GAD OPINION"],
    "current_location": ["CURRENT LOCATION"],
}
NO_SERIAL = {"", "NA", "N/A", "-", "NONE", "0"}
DISPOSAL_WORDS = ("DISPOS", "THANH LY")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def _header_key(value) -> str:
    return re.sub(r"\s+", " ", unidecode(_text(value)).upper()).strip()


def normalize_serial(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", _text(value).upper())


@dataclass
class ListItem:
    sheet: str
    row: int
    no: str = ""
    branch: str = ""
    name: str = ""
    ref: str = ""
    register_date: str = ""
    classification: str = ""
    serial: str = ""
    inventory_status: str = ""
    remark: str = ""
    gad_opinion: str = ""
    current_location: str = ""
    also_in: list[str] = field(default_factory=list)
    # Filled by check_items.
    result: str = ""
    note: str = ""
    app_rows: list = field(default_factory=list)

    def __getitem__(self, key):
        # exports.write_sheet reads row[key] for plain column keys.
        return getattr(self, key)

    @property
    def marked_for_disposal(self) -> bool:
        text = unidecode(f"{self.remark} {self.gad_opinion}").upper()
        return any(word in text for word in DISPOSAL_WORDS)


def _find_header(ws) -> tuple[int, dict[str, int]] | None:
    """(header row number, field -> column index) of the first row in the
    top 10 that has a serial column and a name or ref column."""
    for row_no, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True), start=1):
        keys = [_header_key(v) for v in row]
        cols: dict[str, int] = {}
        for fld, aliases in HEADER_ALIASES.items():
            for alias in aliases:
                if alias in keys:
                    cols[fld] = keys.index(alias)
                    break
        if "serial" in cols and ("name" in cols or "ref" in cols):
            return row_no, cols
    return None


def read_disposal_list(path: str) -> tuple[list[ListItem], list[str]]:
    """All item rows of every sheet that has a recognizable header. The
    same item on several sheets (same Ref. Number - sheet2's new working
    tools are also copied to the bottom of sheet1) is kept once, with
    remark / GAD opinion merged from all copies. Returns (items, notes)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    items: list[ListItem] = []
    by_ref: dict[str, ListItem] = {}
    notes: list[str] = []
    try:
        for ws in wb.worksheets:
            found = _find_header(ws)
            if not found:
                notes.append(f'Sheet "{ws.title}": no Serial + Name/Ref. Number header row found - skipped.')
                continue
            header_row, cols = found
            count = 0
            for row_no, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
                get = lambda f: _text(row[cols[f]]) if f in cols and cols[f] < len(row) else ""  # noqa: E731
                if not (get("name") or get("ref") or get("serial")):
                    continue
                # Second header line under merged headers: either empty in
                # these columns ("Quantity", "Historical Cost Price" ...) or,
                # in Aither's export, repeating the header text itself.
                if any(_header_key(get(f)) in HEADER_ALIASES[f] for f in ("name", "ref", "serial") if get(f)):
                    continue
                item = ListItem(sheet=ws.title, row=row_no, **{f: get(f) for f in cols})
                count += 1
                twin = by_ref.get(item.ref) if item.ref else None
                if twin:
                    twin.also_in.append(f"{ws.title} row {row_no}")
                    for f in ("remark", "gad_opinion", "inventory_status", "classification", "serial", "branch"):
                        if not getattr(twin, f) and getattr(item, f):
                            setattr(twin, f, getattr(item, f))
                    continue
                if item.ref:
                    by_ref[item.ref] = item
                items.append(item)
            notes.append(f'Sheet "{ws.title}": {count} rows read.')
    finally:
        wb.close()
    return items, notes


def check_items(conn, items: list[ListItem]) -> list[ListItem]:
    """Set result / note / app_rows on every item (see the module doc)."""
    periods = {r["id"]: r["period"] for r in conn.execute("SELECT id, period FROM import_batches")}
    names = {r["branch_no"]: r["eng_name"] for r in conn.execute("SELECT branch_no, eng_name FROM branches")}
    current: dict[str, list] = {}
    for r in conn.execute(CURRENT_ASSETS_CTE + " WHERE bk.serial_tag != ''"):
        current.setdefault(normalize_serial(r["serial_tag"]), []).append(r)
    last_seen: dict[str, tuple] = {}
    for r in conn.execute("SELECT serial_tag, branch_no, status, batch_id FROM asset_items WHERE serial_tag != ''"):
        key, seen = normalize_serial(r["serial_tag"]), (periods.get(r["batch_id"]) or "", r["branch_no"], r["status"])
        if key not in last_seen or seen > last_seen[key]:
            last_seen[key] = seen
    # Other list items with the same serial - checked across the whole list,
    # not only the disposal-marked rows: a PC to dispose carrying the serial
    # of a printer still in use (an Aither data error) matters too.
    same_serial: dict[str, list[ListItem]] = {}
    for item in items:
        key = normalize_serial(item.serial)
        if key not in NO_SERIAL:
            same_serial.setdefault(key, []).append(item)

    for item in items:
        key = normalize_serial(item.serial)
        if key in NO_SERIAL:
            item.result, item.note = "no_serial", ""
            continue
        rows = current.get(key, [])
        item.app_rows = [{**dict(r), "period": periods.get(r["batch_id"]) or "",
                          "branch_name": names.get(r["branch_no"], r["branch_no"])} for r in rows]
        if rows:
            here = [r for r in rows if r["branch_no"] == item.branch] or rows
            in_use = [r for r in here if (r["status"] or "").upper().startswith("USING")]
            if not any(r["branch_no"] == item.branch for r in rows):
                item.result = "other_branch"
                item.note = "In the app at " + ", ".join(sorted({f"{r['branch_no']} {names.get(r['branch_no'], '')}".strip() for r in rows}))
            elif in_use:
                r = in_use[0]
                item.result = "in_use"
                item.note = f"Status {r['status']}" + (f", user {r['user_id_raw']} {r['full_name'] or ''}".rstrip() if r["user_id_raw"] else "")
            else:
                item.result, item.note = "ok", f"Status {here[0]['status']}"
            if len(rows) > 1:
                item.note += f" ({len(rows)} current rows with this serial)"
        elif key in last_seen:
            period, branch_no, status = last_seen[key]
            item.result = "not_current"
            item.note = f"Last seen {period} at {branch_no} {names.get(branch_no, '')}".rstrip() + f", status {status}"
        else:
            item.result, item.note = "not_in_app", ""
        others = [o for o in same_serial.get(key, []) if o is not item]
        if others:
            item.note = (item.note + "; " if item.note else "") + "same serial on the list: " + ", ".join(
                f"{o.ref or '-'} {o.name[:40]} ({o.sheet} row {o.row})" for o in others)
            # A worse finding (still in use, other branch...) stays the result.
            if item.result in ("ok", "not_in_app"):
                item.result = "duplicate"
    return items


def summary(items: list[ListItem]) -> list[tuple[str, str, str, int]]:
    """[(result key, label, badge class, count)] in RESULTS order."""
    return [(k, label, badge, sum(1 for i in items if i.result == k)) for k, (label, badge) in RESULTS.items()]
