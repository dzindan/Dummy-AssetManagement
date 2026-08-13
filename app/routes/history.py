import os

from flask import Blueprint, abort, render_template, request, send_file

from ..db import get_connection
from ..handover import HO_TYPES

bp = Blueprint("history", __name__, url_prefix="/history")

HO_TYPE_LABELS = dict(HO_TYPES)


@bp.route("/")
def index():
    user_no = request.args.get("user_no", "").strip()
    branch_no = request.args.get("branch_no", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    sql = "SELECT * FROM handover_records WHERE 1=1"
    params: list = []
    if user_no:
        sql += " AND user_no LIKE ?"
        params.append(f"%{user_no}%")
    if branch_no:
        sql += " AND branch_no = ?"
        params.append(branch_no)
    if date_from:
        sql += " AND ho_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND ho_date <= ?"
        params.append(date_to)
    sql += " ORDER BY id DESC"

    conn = get_connection()
    try:
        records = conn.execute(sql, params).fetchall()
        branches = conn.execute("SELECT branch_no, eng_name FROM branches ORDER BY eng_name").fetchall()
    finally:
        conn.close()

    return render_template(
        "history.html",
        active_page="history",
        records=records,
        branches=branches,
        filters={"user_no": user_no, "branch_no": branch_no, "date_from": date_from, "date_to": date_to},
        ho_type_labels=HO_TYPE_LABELS,
    )


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
