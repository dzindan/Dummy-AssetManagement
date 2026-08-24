"""Web UI for app/cucm.py - a Manage Assets-adjacent page that queries CUCM
directly for currently-registered IP phones (by extension mask, IP mask, or
model), rather than only checking phones already sitting in asset_items with
a recorded IP (that's Network Check's job, and it can't reach phones at all -
see its module docstring on why: no WMI/quser on an embedded device). Every
scanned phone is cross-checked against the current Manage Assets state by IP
so a mismatched or missing serial is visible immediately, without yet
writing anything back (see the module docstring below on why not)."""

from __future__ import annotations

import re

from flask import Blueprint, render_template, request

from ..auth import require_permission
from ..cucm import (
    MODEL_NAMES,
    CucmNotConfigured,
    get_cucm_config,
    scan_phones,
    scan_phones_autosplit,
)
from ..db import get_connection
from ..importer import normalize_model_device
from ..queries import CURRENT_ASSETS_CTE

bp = Blueprint("cucm_scan", __name__, url_prefix="/cucm-scan")

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


def _normalize_serial(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _with_import_crosscheck(devices: list[dict]) -> list[dict]:
    """Cross-checks each scanned phone's live serial/model against whatever
    Manage Assets currently has recorded at that same IP - read-only (no
    write-back yet, unlike Network Check's apply_updates), so a mismatch
    or "not imported at all" is at least visible without risking a bad
    auto-write from a scan result that might itself be stale/wrong.

    The model comparison runs CUCM's own model name (e.g. "Cisco 7942")
    through the exact same model_aliases table normalize_model_device()
    uses at import time, rather than a one-off heuristic - Settings >
    Model Mapping is the single place standard model names/aliases are
    curated, so that's the canonical spelling to compare against, not
    something this page invents its own comparison rule for."""
    if not devices:
        return devices
    ips = [d["ip"] for d in devices]
    conn = get_connection()
    try:
        placeholders = ",".join("?" * len(ips))
        rows = conn.execute(
            f"""
            SELECT ip, serial_tag, model_device, device_name, branch_dept
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
            d["imported_serial"] = imp["serial_tag"] if imp else ""
            d["imported_model"] = imp["model_device"] if imp else ""
            d["imported_branch"] = imp["branch_dept"] if imp else ""

            d["serial_match"] = None
            if imp and imp["serial_tag"] and d["sn"] not in ("Not supported", "No SN found", ""):
                d["serial_match"] = _normalize_serial(imp["serial_tag"]) == _normalize_serial(d["sn"])

            d["model_match"] = None
            if imp and imp["model_device"] and d["model"]:
                live_standard = normalize_model_device(conn, str(d["model"]), cache=model_cache)
                d["model_match"] = live_standard == imp["model_device"]
    finally:
        conn.close()
    return devices


def _render(devices=None, error=None, filters=None):
    filters = filters or {"num": "*", "ip": "", "model": "255", "autosplit": False}
    return render_template(
        "cucm_scan.html",
        active_page="cucm_scan",
        devices=devices,
        error=error,
        filters=filters,
        model_options=MODEL_OPTIONS,
        configured=_is_configured(),
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
    return _render(devices=devices, error=error, filters=filters)
