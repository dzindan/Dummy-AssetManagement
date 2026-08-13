"""The upload+diff flow lives on Import Data's Monthly Asset Reports section
(app/routes/import_data.py) - uploading a report there already shows the
Added/Removed/Changed comparison, so there's no separate "Update & Compare"
page anymore (it was doing the exact same import, which meant uploading the
same file on both pages double-imported it). This blueprint just keeps the
Excel diff export endpoint that page's result links to."""

import io

import openpyxl
from flask import Blueprint, flash, redirect, request, send_file, url_for

from ..db import get_connection
from ..diffing import COMPARE_FIELDS, FIELD_LABELS, diff_batch

bp = Blueprint("update_compare", __name__, url_prefix="/update")


def build_diff_workbook(all_diffs) -> openpyxl.Workbook:
    """Shared by the on-demand /export download below and the auto-saved
    archive copy written after every asset-report import (see
    import_data._save_diff_report) - both need the exact same sheets."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Diff Summary"
    ws.append(["Branch", "Added", "Removed", "Changed", "Unchanged"])
    for d in all_diffs:
        ws.append([d.branch_label, len(d.added), len(d.removed), len(d.changed), d.unchanged_count])

    detail_ws = wb.create_sheet("Details")
    # Every identifying column from the original asset report (same names/
    # order as the source file), plus the change columns at the end - so a
    # changed/added/removed row can be located and cross-checked against the
    # source spreadsheet without guessing which physical asset it was.
    header = [
        "Branch (Matched)", "Branch / Dept (as in file)", "Device Name", "Model Device",
        "User ID", "Full Name", "Serial / Service Tag", "Status", "Remark", "Position",
        "Handover Date", "IP",
        "Change Type", "Field Changed", "Old Value", "New Value",
    ]
    detail_ws.append(header)

    def _row_columns(branch_label: str, row) -> list:
        return [
            branch_label, row["branch_dept"], row["device_name"], row["model_device"],
            row["user_id_raw"], row["full_name"], row["serial_tag"], row["status"],
            row["remark"], row["position"], row["handover_date"], row["ip"],
        ]

    for d in all_diffs:
        for row in d.added:
            detail_ws.append([*_row_columns(d.branch_label, row), "ADDED", "", "", ""])
        for row in d.removed:
            detail_ws.append([*_row_columns(d.branch_label, row), "REMOVED", "", "", ""])
        for c in d.changed:
            for f in COMPARE_FIELDS:
                if f in c["diffs"]:
                    old_val, new_val = c["diffs"][f]
                    detail_ws.append(
                        [*_row_columns(d.branch_label, c["new"]), "CHANGED", FIELD_LABELS[f], old_val, new_val]
                    )

    return wb


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

    wb = build_diff_workbook(all_diffs)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="asset_diff_report.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
