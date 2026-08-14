"""The upload+diff flow lives on Import Data's Monthly Asset Reports section
(app/routes/import_data.py) - uploading a report there already shows the
Added/Removed/Changed comparison, so there's no separate "Update & Compare"
page anymore (it was doing the exact same import, which meant uploading the
same file on both pages double-imported it). This blueprint just keeps the
Excel diff export endpoint that page's result links to."""

from flask import Blueprint, flash, redirect, request, url_for

from ..db import get_connection
from ..diffing import diff_batch
from ..exports import build_diff_workbook, send_workbook

bp = Blueprint("update_compare", __name__, url_prefix="/update")


@bp.route("/export")
def export():
    batch_ids_param = request.args.get("batch_ids", "")
    batch_ids = [int(x) for x in batch_ids_param.split(",") if x.strip().isdigit()]
    if not batch_ids:
        flash("Nothing to export.", "error")
        return redirect(url_for("import_data.index"))

    conn = get_connection()
    try:
        all_diffs = []
        for batch_id in batch_ids:
            all_diffs.extend(diff_batch(conn, batch_id))
    finally:
        conn.close()

    return send_workbook(build_diff_workbook(all_diffs), "asset_diff_report.xlsx")
