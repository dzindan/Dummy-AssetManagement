"""Disposal Check page: upload a disposal / inventory list exported from
Aither and see, item by item, whether it agrees with this app's data
(app/disposal.py does the reading and checking).

Every check is saved to the history (disposal_checks) with each item's
result as it was at check time; the result page, its Excel export and the
History list all read that saved snapshot. The uploaded file itself is
kept in the uploads folder (disposal_<random>.xlsx) and can be downloaded
again from the history."""

from __future__ import annotations

import json
import os
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

from ..auth import current_username
from ..db import get_connection
from ..disposal import RESULTS, check_items, list_checks, load_check, read_disposal_list, save_check
from ..exports import build_workbook, dated_download_name, send_workbook
from ..paths import get_uploads_dir, safe_filename

bp = Blueprint("disposal_check", __name__, url_prefix="/disposal-check")

_PREFIX = "disposal_"


def _shown(items, only_marked: bool, picked: str):
    """Items for one view: marked-for-disposal only (or all), one result
    (or all), worst result first."""
    shown = [i for i in items if i.marked_for_disposal] if only_marked else list(items)
    order = list(RESULTS)
    shown.sort(key=lambda i: order.index(i.result))
    return shown, [i for i in shown if not picked or i.result == picked]


@bp.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        upload = request.files.get("file")
        ext = os.path.splitext(upload.filename if upload else "")[1].lower()
        if not upload or not upload.filename or ext not in (".xlsx", ".xlsm"):
            flash("Choose the disposal list (.xlsx) exported from Aither.", "error")
            return redirect(url_for("disposal_check.index"))
        stored = _PREFIX + uuid.uuid4().hex + ext
        path = os.path.join(get_uploads_dir(), stored)
        upload.save(path)
        try:
            items, notes = read_disposal_list(path)
        except Exception as exc:  # noqa: BLE001 - a broken/encrypted file is reported, not a 500
            os.remove(path)
            flash(f"Could not read that file: {exc}", "error")
            return redirect(url_for("disposal_check.index"))
        conn = get_connection()
        try:
            check_items(conn, items)
            check_id = save_check(conn, items, notes, upload.filename, stored, current_username())
            conn.commit()
        finally:
            conn.close()
        return redirect(url_for("disposal_check.result", check_id=check_id,
                                only="1" if request.form.get("only_marked") else "0"))
    conn = get_connection()
    try:
        history = list_checks(conn)
    finally:
        conn.close()
    return render_template("disposal_check.html", active_page="disposal_check", check=None,
                           history=history, results=RESULTS)


def _load(check_id: int):
    conn = get_connection()
    try:
        found = load_check(conn, check_id)
    finally:
        conn.close()
    if not found:
        abort(404)
    return found


@bp.route("/<int:check_id>")
def result(check_id):
    check, items = _load(check_id)
    only_marked = request.args.get("only", "1") == "1"
    picked = request.args.get("result", "")
    shown, rows = _shown(items, only_marked, picked)
    counts = [(k, label, badge, sum(1 for i in shown if i.result == k)) for k, (label, badge) in RESULTS.items()]
    return render_template(
        "disposal_check.html", active_page="disposal_check", check=check, items=rows, counts=counts,
        total=len(shown), total_all=len(items), only_marked=only_marked, picked=picked,
        notes=json.loads(check["notes_json"] or "[]"), results=RESULTS, history=None,
        has_file=bool(check["stored_name"]) and os.path.exists(os.path.join(get_uploads_dir(), check["stored_name"])),
    )


@bp.route("/<int:check_id>/original")
def original(check_id):
    check, _ = _load(check_id)
    path = os.path.join(get_uploads_dir(), check["stored_name"] or "")
    if not check["stored_name"] or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=check["file_name"] or os.path.basename(path))


EXPORT_COLUMNS = [
    ("Result", lambda i: RESULTS[i.result][0]),
    ("Note", "note"),
    ("Sheet", "sheet"),
    ("Row", "row"),
    ("No", "no"),
    ("Branch ID", "branch"),
    ("Ref. Number", "ref"),
    ("FA/WT Name", "name"),
    ("Classification", "classification"),
    ("Serial", "serial"),
    ("Register Date", "register_date"),
    ("Inventory Status", "inventory_status"),
    ("Remark/Suggestion", "remark"),
    ("GAD Opinion", "gad_opinion"),
    ("App: Branch", lambda i: "; ".join(f"{r['branch_no']} {r['branch_name']}" for r in i.app_rows)),
    ("App: Device", lambda i: "; ".join(r["device_name"] or "" for r in i.app_rows)),
    ("App: Model", lambda i: "; ".join(r["model_device"] or "" for r in i.app_rows)),
    ("App: User ID", lambda i: "; ".join(r["user_id_raw"] or "" for r in i.app_rows)),
    ("App: Full Name", lambda i: "; ".join(r["full_name"] or "" for r in i.app_rows)),
    ("App: Status", lambda i: "; ".join(r["status"] or "" for r in i.app_rows)),
    ("App: Period", lambda i: "; ".join(r["period"] for r in i.app_rows)),
]
EXPORT_WIDTHS = [22, 50, 8, 6, 6, 9, 16, 40, 18, 18, 12, 14, 30, 30, 28, 14, 22, 12, 22, 14, 9]


@bp.route("/<int:check_id>/export.xlsx")
def export(check_id):
    check, items = _load(check_id)
    _, rows = _shown(items, request.args.get("only", "1") == "1", request.args.get("result", ""))
    wb = build_workbook("Disposal Check", EXPORT_COLUMNS, rows, widths=EXPORT_WIDTHS)
    base = safe_filename(os.path.splitext(check["file_name"] or "")[0], fallback="list")
    return send_workbook(wb, dated_download_name(f"disposal_check_{base}"))
