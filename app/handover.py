"""Rendering the hand-over .docx from templates_docx/handover_template.docx
and logging each generated form to handover_records (the hand-over history)."""

from __future__ import annotations

import datetime as dt
import json
import os

from docxtpl import DocxTemplate

from .db import get_connection, log_activity
from .paths import get_handover_template_path, get_handovers_dir

# ASSIGNMENT/TEMP hand equipment OUT to the employee; RETURN gets it back IN.
# The (Hand Over)/(Receive) suffixes make that direction explicit in the UI
# without needing a separate data field.
HO_TYPES = [
    ("ASSIGNMENT", "ASSIGNMENT (Hand Over)"),
    ("TEMP", "TEMP ASSIGNMENT (Hand Over)"),
    ("RETURN", "RETURN (Receive)"),
    ("OTHER", "OTHER"),
]
REASONS = ["NEWCOMER", "ROTATE", "MATERNITY", "RESIGNED", "OTHER"]
ASSET_CONDITIONS = ["NEW", "WAREHOUSE"]

CHECKED = "☑"
UNCHECKED = "☐"


def _box(selected: bool) -> str:
    return CHECKED if selected else UNCHECKED


def resolve_ho_date(ho_date: str | None) -> dt.date:
    """`ho_date` is an HTML <input type=date> value ("YYYY-MM-DD"). Falls
    back to today (the system date) if missing or unparseable."""
    if ho_date:
        try:
            return dt.datetime.strptime(ho_date, "%Y-%m-%d").date()
        except ValueError:
            pass
    return dt.date.today()


def render_handover_docx(data: dict) -> str:
    """`data` keys: ict_rep_name, ict_rep_id, receiving_name, receiving_title,
    receiving_dept, receiving_id, ho_type, temp_due_date, other_type_text,
    reason, other_reason_text, user_no, ho_date ("YYYY-MM-DD", defaults to
    today), signature_receiving_name, signature_prepared_by, assets (list of
    dict with equipment_name, qty, serial_number, status). Returns the saved
    .docx path.
    """
    now = dt.datetime.now()
    ho_date = resolve_ho_date(data.get("ho_date"))
    assets = []
    for i, a in enumerate(data.get("assets", []), start=1):
        assets.append(
            {
                "no": i,
                "equipment_name": a.get("equipment_name", ""),
                "qty": a.get("qty", 1),
                "serial_number": a.get("serial_number", ""),
                "status": a.get("status", ""),
            }
        )
    total_qty = sum(int(a["qty"] or 1) for a in assets)

    ho_type = (data.get("ho_type") or "").upper()
    reason = (data.get("reason") or "").upper()

    ctx = {
        "ho_day": ho_date.strftime("%d"),
        "ho_month": ho_date.strftime("%m"),
        "ho_year": ho_date.strftime("%Y"),
        "ict_rep_name": data.get("ict_rep_name", ""),
        "ict_rep_id": data.get("ict_rep_id", ""),
        "receiving_name": data.get("receiving_name", ""),
        "receiving_title": data.get("receiving_title", ""),
        "receiving_dept": data.get("receiving_dept", ""),
        "receiving_id": data.get("receiving_id", ""),
        "type_assignment_box": _box(ho_type == "ASSIGNMENT"),
        "type_temp_box": _box(ho_type == "TEMP"),
        "type_return_box": _box(ho_type == "RETURN"),
        "type_other_box": _box(ho_type == "OTHER"),
        "temp_due_date": data.get("temp_due_date", ""),
        "other_type_text": data.get("other_type_text", ""),
        "reason_newcomer_box": _box(reason == "NEWCOMER"),
        "reason_rotate_box": _box(reason == "ROTATE"),
        "reason_maternity_box": _box(reason == "MATERNITY"),
        "reason_resigned_box": _box(reason == "RESIGNED"),
        "reason_other_box": _box(reason == "OTHER"),
        "other_reason_text": data.get("other_reason_text", ""),
        "assets": assets,
        "total_qty": total_qty,
        "signature_receiving_name": data.get("signature_receiving_name") or data.get("receiving_name", ""),
        "signature_prepared_by": data.get("signature_prepared_by") or data.get("ict_rep_name", ""),
    }

    tpl = DocxTemplate(get_handover_template_path())
    tpl.render(ctx)

    safe_user = "".join(c for c in str(data.get("user_no") or "unknown") if c.isalnum()) or "unknown"
    filename = f"HANDOVER_{safe_user}_{now.strftime('%Y%m%d_%H%M%S')}.docx"
    out_path = os.path.join(get_handovers_dir(), filename)
    tpl.save(out_path)
    return out_path


def record_handover(data: dict, docx_path: str, created_by: str = "") -> int:
    conn = get_connection()
    try:
        now = dt.datetime.now().isoformat(timespec="seconds")
        ho_date = resolve_ho_date(data.get("ho_date")).isoformat()
        cur = conn.execute(
            """
            INSERT INTO handover_records (
                created_at, ho_date, user_no, user_name, branch_no, ho_type, reason, assets_json,
                ict_rep_name, ict_rep_id, receiving_name, receiving_title, receiving_dept,
                receiving_id, signature_receiving_name, signature_prepared_by, docx_path, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                ho_date,
                data.get("user_no", ""),
                data.get("receiving_name", ""),
                data.get("branch_no", ""),
                data.get("ho_type", ""),
                data.get("reason", ""),
                json.dumps(data.get("assets", [])),
                data.get("ict_rep_name", ""),
                data.get("ict_rep_id", ""),
                data.get("receiving_name", ""),
                data.get("receiving_title", ""),
                data.get("receiving_dept", ""),
                data.get("receiving_id", ""),
                data.get("signature_receiving_name", ""),
                data.get("signature_prepared_by", ""),
                docx_path,
                created_by,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def apply_handover_date(asset_ids: list[int], ho_date: str | None, performed_by: str = "") -> None:
    """Stamps asset_items.handover_date with the hand-over form's own date
    for every asset actually included on that form, once the form is
    generated - so Manage Assets reflects when the equipment was really
    handed over, rather than whatever date (often blank) an import last
    happened to carry for that row. Mirrors asset_edit.py's edit route:
    only rows whose handover_date is actually changing get updated/logged."""
    new_value = resolve_ho_date(ho_date).isoformat()
    conn = get_connection()
    try:
        for asset_id in asset_ids:
            row = conn.execute(
                "SELECT handover_date FROM asset_items WHERE id = ?", (asset_id,)
            ).fetchone()
            if row is None or (row["handover_date"] or "") == new_value:
                continue
            conn.execute(
                "UPDATE asset_items SET handover_date = ? WHERE id = ?", (new_value, asset_id)
            )
            log_activity(
                conn, "asset", "Hand-over form generated", performed_by=performed_by,
                target=f"Asset #{asset_id}", field="handover_date",
                old_value=row["handover_date"] or "", new_value=new_value,
            )
        conn.commit()
    finally:
        conn.close()
