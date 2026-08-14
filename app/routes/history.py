import os

from flask import Blueprint, abort, render_template, request, send_file

from ..db import get_connection
from ..exports import build_workbook, send_workbook
from ..handover import HO_TYPES
from ..queries import search_handover_records

bp = Blueprint("history", __name__, url_prefix="/history")

HO_TYPE_LABELS = dict(HO_TYPES)

HANDOVER_EXPORT_COLUMNS = [
    ("Hand-Over Date", "ho_date"),
    ("Logged At", lambda r: r["created_at"][:16] if r["created_at"] else ""),
    ("User ID", "user_no"),
    ("Branch", lambda r: r["branch_eng_name"] or r["branch_no"]),
    ("Type", lambda r: HO_TYPE_LABELS.get(r["ho_type"], r["ho_type"])),
    ("Reason", "reason"),
    ("Receiving Party", "receiving_name"),
]


def _filters_from_args() -> dict:
    return {
        "user_no": request.args.get("user_no", "").strip(),
        "branch_no": request.args.get("branch_no", "").strip(),
        "date_from": request.args.get("date_from", "").strip(),
        "date_to": request.args.get("date_to", "").strip(),
    }


@bp.route("/")
def index():
    filters = _filters_from_args()
    conn = get_connection()
    try:
        records = search_handover_records(conn, filters)
        branches = conn.execute("SELECT branch_no, eng_name FROM branches ORDER BY eng_name").fetchall()
    finally:
        conn.close()

    return render_template(
        "history.html",
        active_page="history",
        records=records,
        branches=branches,
        filters=filters,
        ho_type_labels=HO_TYPE_LABELS,
    )


@bp.route("/export")
def export():
    filters = _filters_from_args()
    conn = get_connection()
    try:
        records = search_handover_records(conn, filters)
    finally:
        conn.close()
    wb = build_workbook("Hand-Over History", HANDOVER_EXPORT_COLUMNS, records)
    return send_workbook(wb, "handover_history.xlsx")


@bp.route("/download/<int:record_id>")
def download(record_id):
    conn = get_connection()
    try:
        record = conn.execute(
            "SELECT * FROM handover_records WHERE id = ?", (record_id,)
        ).fetchone()
    finally:
        conn.close()

    if not record or not record["docx_path"] or not os.path.exists(record["docx_path"]):
        abort(404, description="Hand-over file not found. It may have been moved or deleted.")

    return send_file(
        record["docx_path"],
        as_attachment=True,
        download_name=os.path.basename(record["docx_path"]),
    )
