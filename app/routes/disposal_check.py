"""Disposal Check page: upload a disposal / inventory list exported from
Aither and see, item by item, whether it agrees with this app's data
(app/disposal.py does the reading and checking). Read-only.

The uploaded file is kept in the uploads folder under a random name so the
result page and its Excel export can re-read it; the URL carries that
name, the "only items marked for disposal" switch and a result filter."""

from __future__ import annotations

import os
import re
import uuid

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..db import get_connection
from ..disposal import RESULTS, check_items, read_disposal_list, summary
from ..exports import build_workbook, dated_download_name, send_workbook
from ..paths import get_uploads_dir, safe_filename

bp = Blueprint("disposal_check", __name__, url_prefix="/disposal-check")

_TOKEN_RE = re.compile(r"[0-9a-f]{32}\.(xlsx|xlsm)")
_PREFIX = "disposal_"


def _token_path(token: str) -> str:
    if not _TOKEN_RE.fullmatch(token or ""):
        abort(404)
    path = os.path.join(get_uploads_dir(), _PREFIX + token)
    if not os.path.exists(path):
        abort(404)
    return path


def _checked(token: str, only_marked: bool):
    """(shown items, all checked items, sheet notes). Duplicate serials are
    looked for across the whole list even when only marked items are shown."""
    items, notes = read_disposal_list(_token_path(token))
    conn = get_connection()
    try:
        check_items(conn, items)
    finally:
        conn.close()
    shown = [i for i in items if i.marked_for_disposal] if only_marked else items
    order = list(RESULTS)
    shown.sort(key=lambda i: order.index(i.result))
    return shown, items, notes


@bp.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        upload = request.files.get("file")
        ext = os.path.splitext(upload.filename if upload else "")[1].lower()
        if not upload or not upload.filename or ext not in (".xlsx", ".xlsm"):
            flash("Choose the disposal list (.xlsx) exported from Aither.", "error")
            return redirect(url_for("disposal_check.index"))
        token = uuid.uuid4().hex + ext
        upload.save(os.path.join(get_uploads_dir(), _PREFIX + token))
        return redirect(url_for("disposal_check.result", token=token, name=upload.filename,
                                only="1" if request.form.get("only_marked") else "0"))
    return render_template("disposal_check.html", active_page="disposal_check", items=None)


@bp.route("/<token>")
def result(token):
    only_marked = request.args.get("only", "1") == "1"
    shown, all_items, notes = _checked(token, only_marked)
    picked = request.args.get("result", "")
    rows = [i for i in shown if not picked or i.result == picked]
    return render_template(
        "disposal_check.html", active_page="disposal_check", items=rows, counts=summary(shown),
        total=len(shown), total_all=len(all_items), only_marked=only_marked, picked=picked,
        token=token, name=request.args.get("name", ""), notes=notes, results=RESULTS,
    )


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


@bp.route("/<token>/export.xlsx")
def export(token):
    shown, _, _ = _checked(token, request.args.get("only", "1") == "1")
    picked = request.args.get("result", "")
    rows = [i for i in shown if not picked or i.result == picked]
    wb = build_workbook("Disposal Check", EXPORT_COLUMNS, rows, widths=EXPORT_WIDTHS)
    base = safe_filename(os.path.splitext(request.args.get("name", ""))[0], fallback="list")
    return send_workbook(wb, dated_download_name(f"disposal_check_{base}"))
