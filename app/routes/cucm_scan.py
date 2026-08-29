"""Web UI for app/cucm.py - a Manage Assets-adjacent page that queries CUCM
directly for currently-registered IP phones (by extension mask, IP mask, or
model), rather than only checking phones already sitting in asset_items with
a recorded IP (that's Network Check's job, and it can't reach phones at all -
see its module docstring on why: no WMI/quser on an embedded device). Every
scanned phone is cross-checked against the current Manage Assets state by IP
so a mismatched or missing serial, model, or assigned user is visible
immediately, with a per-row Update button (model/serial only - see
apply_updates below) to write the live-scanned value back, mirroring Network
Check's apply_updates()."""

from __future__ import annotations

import re
import threading
import uuid

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from ..auth import require_permission
from ..cucm import (
    MODEL_NAMES,
    CucmNotConfigured,
    get_cucm_config,
    scan_phones,
    scan_phones_autosplit,
)
from ..db import get_connection
from ..exports import build_workbook, dated_download_name, match_text, send_workbook
from ..importer import normalize_model_device
from ..queries import CURRENT_ASSETS_CTE

bp = Blueprint("cucm_scan", __name__, url_prefix="/cucm-scan")

# Every successful scan's results, keyed by a fresh id, so the Export button
# on its results page can download exactly what's on screen without
# re-querying CUCM (a second live scan could return different phones/values
# than what the user is looking at, and is needlessly slow to boot). Mirrors
# Network Check's own _SCANS store (app/routes/network_check.py), just
# without the polling/progress fields this page's synchronous form-POST scan
# doesn't need.
_LAST_SCANS: dict[str, list[dict]] = {}
_LOCK = threading.Lock()

# 255 is excluded from the sorted, dict-derived part - CUCM's own enum
# already defines it as "Unknown" (used as the API's wildcard/no-filter
# value), which would otherwise show up as a second, competing option
# with the same value as the friendlier "Any" prepended below.
MODEL_OPTIONS = [("255", "Any")] + sorted(
    ((str(k), v) for k, v in MODEL_NAMES.items() if k != 255), key=lambda kv: kv[1]
)


def _is_configured() -> bool:
    config = get_cucm_config()
    return bool(config["ip"] and config["axluser"] and config["axlpassword"])


def _normalize_alnum(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _with_import_crosscheck(devices: list[dict]) -> list[dict]:
    """Cross-checks each scanned phone's live serial/model against whatever
    Manage Assets currently has recorded at that same IP, so a mismatch or
    "not imported at all" is visible, and (when it is imported) carries the
    asset_items id and the canonical value a click on the row's Update
    button would write - see apply_updates() below.

    The model comparison runs CUCM's own model name (e.g. "Cisco 7942")
    through the exact same model_aliases table normalize_model_device()
    uses at import time, rather than a one-off heuristic - Settings >
    Model Mapping is the single place standard model names/aliases are
    curated, so that's the canonical spelling to compare against (and to
    write back on Update), not something this page invents its own
    comparison rule for."""
    if not devices:
        return devices
    ips = [d["ip"] for d in devices]
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(ips))
        rows = conn.execute(
            f"""
            SELECT id, ip, branch_no, serial_tag, model_device, device_name, branch_dept,
                   user_id_raw, user_id_norm, full_name
            FROM ({CURRENT_ASSETS_CTE}) bk
            WHERE ip IN ({placeholders})
            """,
            ips,
        ).fetchall()
        imported_by_ip = {r["ip"]: r for r in rows}
        model_cache: dict = {}
        for d in devices:
            imp = imported_by_ip.get(d["ip"])
            d["imported"] = imp is not None
            d["imported_asset_id"] = imp["id"] if imp else None
            d["imported_serial"] = imp["serial_tag"] if imp else ""
            d["imported_model"] = imp["model_device"] if imp else ""
            d["imported_branch"] = imp["branch_dept"] if imp else ""
            d["imported_user"] = imp["full_name"] if imp else ""
            d["imported_user_id"] = (imp["user_id_norm"] or imp["user_id_raw"]) if imp else ""

            d["serial_match"] = None
            if imp and imp["serial_tag"] and d["sn"] not in ("Not supported", "No SN found", ""):
                d["serial_match"] = _normalize_alnum(imp["serial_tag"]) == _normalize_alnum(d["sn"])

            d["model_match"] = None
            d["live_model_normalized"] = ""
            if d["model"]:
                d["live_model_normalized"] = normalize_model_device(conn, str(d["model"]), cache=model_cache)
            if imp and imp["model_device"] and d["model"]:
                d["model_match"] = d["live_model_normalized"] == imp["model_device"]

            # CUCM's RisPort70 API (what scan_phones/cucm_rt_phones calls) has
            # no "assigned owner" field - only AXL's getPhone exposes that,
            # and it's not queried here. The closest live per-phone identity
            # signal already on hand is the phone's own Description, which
            # sites conventionally set to the assigned user's name and/or ID
            # - so that's what's compared against Manage Assets' imported
            # user, as a substring match (case/punctuation-insensitive)
            # rather than an exact one, since Description's exact format
            # (e.g. "SEPxxxx - Nguyen Van A" vs just a name) isn't fixed.
            d["user_match"] = None
            if imp and (imp["full_name"] or imp["user_id_norm"] or imp["user_id_raw"]) and d["desc"]:
                desc_norm = _normalize_alnum(d["desc"])
                name_norm = _normalize_alnum(imp["full_name"])
                id_norm = _normalize_alnum(imp["user_id_norm"] or imp["user_id_raw"])
                if desc_norm:
                    d["user_match"] = bool((name_norm and name_norm in desc_norm) or (id_norm and id_norm in desc_norm))
    finally:
        conn.close()
    return devices


def _render(devices=None, error=None, filters=None, scan_id=None):
    filters = filters or {"num": "*", "ip": "", "model": "255", "autosplit": False}
    return render_template(
        "cucm_scan.html",
        active_page="cucm_scan",
        devices=devices,
        error=error,
        filters=filters,
        model_options=MODEL_OPTIONS,
        configured=_is_configured(),
        scan_id=scan_id,
    )


@bp.route("/")
def index():
    return _render()


@bp.route("/scan", methods=["POST"])
@require_permission("network_check")
def scan():
    num = request.form.get("num", "").strip()
    ip = request.form.get("ip", "").strip()
    model = request.form.get("model", "255").strip()
    autosplit = request.form.get("autosplit") == "on"
    filters = {"num": num or "*", "ip": ip, "model": model, "autosplit": autosplit}

    devices: list[dict] = []
    error = None
    try:
        if autosplit:
            devices = scan_phones_autosplit(model=model, ip="", name="")
        elif ip:
            # IP mask takes priority over Number mask when both are filled
            # in - cucm_rt_phones only ever searches by one of name/num/ip
            # per call, whichever is non-empty, checked in that order.
            devices = scan_phones(model=model, num="", ip=ip, name="")
        else:
            devices = scan_phones(model=model, num=(num or "*"), ip="", name="")
    except CucmNotConfigured as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as-is, matches importer.py's own guards
        error = f"Scan failed: {exc}"

    devices = _with_import_crosscheck(devices)

    scan_id = None
    if not error:
        scan_id = uuid.uuid4().hex
        with _LOCK:
            _LAST_SCANS[scan_id] = devices

    return _render(devices=devices, error=error, filters=filters, scan_id=scan_id)


def _cucm_match_text(d: dict, match_key: str) -> str:
    """Same NOT IMPORTED / MATCH / MISMATCH / N/A text the page's own badges
    show (cucm_scan.html), for the three match columns below."""
    return "NOT IMPORTED" if not d["imported"] else match_text(d[match_key])


CUCM_SCAN_COLUMNS = [
    ("Extension", "num"),
    ("IP", "ip"),
    ("Live Model", "model"),
    ("Imported Model", "imported_model"),
    ("Model Match", lambda d: _cucm_match_text(d, "model_match")),
    ("Live SN", "sn"),
    ("Imported Serial", "imported_serial"),
    ("Serial Match", lambda d: _cucm_match_text(d, "serial_match")),
    ("Imported User", "imported_user"),
    ("Imported User ID", "imported_user_id"),
    ("User Match", lambda d: _cucm_match_text(d, "user_match")),
    ("Branch/Dept", "imported_branch"),
]
CUCM_SCAN_COLUMN_WIDTHS = [12, 16, 20, 20, 14, 20, 20, 14, 22, 18, 12, 20]


@bp.route("/scan/<scan_id>/export.xlsx")
def export_xlsx(scan_id: str):
    with _LOCK:
        devices = _LAST_SCANS.get(scan_id)
    if devices is None:
        flash("That scan result has expired - run the scan again to export.", "error")
        return redirect(url_for("cucm_scan.index"))

    wb = build_workbook("CUCM Phone Scan", CUCM_SCAN_COLUMNS, devices, widths=CUCM_SCAN_COLUMN_WIDTHS)
    return send_workbook(wb, dated_download_name("cucm_phone_scan", with_time=True))


# Field key (from the page's Update button) -> asset_items column. User is
# deliberately excluded - _with_import_crosscheck's own docstring on
# user_match already flags CUCM's Description-substring match as a "worth
# double-checking" heuristic, not reliable enough to auto-overwrite
# full_name/user_id with.
CUCM_UPDATABLE_FIELDS = {
    "model": "model_device",
    "serial": "serial_tag",
}


@bp.route("/apply", methods=["POST"])
@require_permission("network_check")
def apply_updates():
    """Write a mismatched model/serial's live CUCM value into the matching
    asset_items row - the write-back this page didn't have before. Logged to
    the same network_check_log table Network Check's own apply_updates()
    uses, so both live-scan tools share one audit trail (see
    network_check.log). A value that already matches the current DB row is
    skipped (no-op, not logged)."""
    body = request.get_json(silent=True) or {}
    updates = body.get("updates") or []

    applied = []
    conn = get_connection()
    try:
        for u in updates:
            asset_id = u.get("asset_id")
            field = u.get("field")
            ip = (u.get("ip") or "").strip()
            scan_id = u.get("scan_id")
            new_value = (u.get("value") or "").strip().upper()
            column = CUCM_UPDATABLE_FIELDS.get(field)
            if not (asset_id and column and new_value):
                continue

            row = conn.execute(
                f"SELECT {column} AS current_value, branch_no FROM asset_items WHERE id = ?", (asset_id,)
            ).fetchone()
            if row is None or row["current_value"] == new_value:
                continue
            old_value = row["current_value"]
            conn.execute(f"UPDATE asset_items SET {column} = ? WHERE id = ?", (new_value, asset_id))
            conn.execute(
                "INSERT INTO network_check_log "
                "(applied_at, branch_no, ip, asset_id, field, old_value, new_value) "
                "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)",
                (row["branch_no"], ip, asset_id, field, old_value, new_value),
            )
            applied.append({"asset_id": asset_id, "field": field, "new_value": new_value})

            # Keep the cached scan this update came from in sync, so a
            # subsequent Export reflects it instead of the stale value the
            # live scan itself returned - same idea as Network Check's
            # apply_updates mutating its own `snapshot` in place.
            if scan_id:
                with _LOCK:
                    for d in _LAST_SCANS.get(scan_id, []):
                        if d.get("imported_asset_id") == asset_id:
                            if field == "model":
                                d["imported_model"] = new_value
                                d["model_match"] = True
                            elif field == "serial":
                                d["imported_serial"] = new_value
                                d["serial_match"] = True
        conn.commit()
    finally:
        conn.close()

    return jsonify({"applied": applied})
