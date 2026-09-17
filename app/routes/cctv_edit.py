from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..auth import current_username, require_permission
from ..db import get_connection, log_activity, prune_stale_unmapped
from ..exports import CCTV_ROW_COLUMNS, build_cctv_rows_workbook, dated_download_name, send_workbook
from ..queries import (
    UNRESOLVED_BRANCH_FILTER,
    get_branch,
    get_branches_with_current_cctv,
    search_cctv,
)

bp = Blueprint("cctv_edit", __name__, url_prefix="/cctv")

# Same pagination shape as asset_edit.py's Manage Assets page.
PER_PAGE_OPTIONS = [100, 200, 300, 400, 500, 1000, 2000, 3000, 4000, 5000]
DEFAULT_PER_PAGE = 200


def _parse_per_page() -> int:
    try:
        value = int(request.args.get("per_page", DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        return DEFAULT_PER_PAGE
    return value if value in PER_PAGE_OPTIONS else DEFAULT_PER_PAGE


def _parse_page() -> int:
    try:
        value = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        return 1
    return max(value, 1)


def _pagination_qs() -> str:
    pairs = [(k, v) for k in request.args if k != "page" for v in request.args.getlist(k)]
    return urlencode(pairs)


# Free-text fields a user can correct by hand - no user_id/full_name/
# position/handover_date here (CCTV gear isn't assigned to a person), and
# camera_count/hdd_count/hdd_capacity/location/manufacturer take their place.
EDITABLE_FIELDS = [
    "device_name",
    "model_device",
    "manufacturer",
    "serial_tag",
    "status",
    "branch_dept",
    "ip",
    "camera_count",
    "hdd_count",
    "hdd_capacity",
    "location",
    "remark",
]

UPPERCASE_FIELDS = set(EDITABLE_FIELDS)


def _filters_from_args() -> dict:
    return {
        "branch_no": [v for v in request.args.getlist("branch_no") if v],
        "device_name": [v for v in request.args.getlist("device_name") if v],
        "status": [v for v in request.args.getlist("status") if v],
        "q": request.args.get("q", "").strip(),
    }


@bp.route("/")
def index():
    filters = _filters_from_args()
    per_page = _parse_per_page()
    page = _parse_page()

    conn = get_connection()
    try:
        rows, total = search_cctv(conn, filters, page=page, per_page=per_page)
        total_pages = (total + per_page - 1) // per_page if total else 1
        if page > total_pages:
            page = total_pages
            rows, total = search_cctv(conn, filters, page=page, per_page=per_page)
        branches = get_branches_with_current_cctv(conn)
        device_names = [
            r["device_name"]
            for r in conn.execute(
                "SELECT DISTINCT device_name FROM cctv_items WHERE device_name != '' ORDER BY device_name"
            ).fetchall()
        ]
        current_statuses = {
            r["status"]
            for r in conn.execute("SELECT DISTINCT status FROM cctv_items WHERE status != ''").fetchall()
        }
        standard_statuses = {
            r["name"] for r in conn.execute("SELECT name FROM status_standard_names").fetchall()
        }
        status_options = sorted(current_statuses | standard_statuses)
        selected_branch = None
        if len(filters["branch_no"]) == 1:
            only_branch = filters["branch_no"][0]
            if only_branch == UNRESOLVED_BRANCH_FILTER:
                selected_branch = {"branch_no": "", "eng_name": "Unresolved / unmatched branch"}
            else:
                selected_branch = get_branch(conn, only_branch)
    finally:
        conn.close()

    return render_template(
        "cctv_management.html",
        active_page="cctv",
        rows=rows,
        total=total,
        filters=filters,
        branches=branches,
        unresolved_value=UNRESOLVED_BRANCH_FILTER,
        device_names=device_names,
        status_options=status_options,
        selected_branch=selected_branch,
        page=page,
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
        total_pages=total_pages,
        pagination_qs=_pagination_qs(),
    )


@bp.route("/export")
def export():
    filters = _filters_from_args()
    conn = get_connection()
    try:
        rows, _total = search_cctv(conn, filters)
    finally:
        conn.close()

    columns = (
        [("Branch", lambda r: r["branch_eng_name"] or r["branch_dept"])]
        + list(CCTV_ROW_COLUMNS)
    )
    wb = build_cctv_rows_workbook(rows, sheet_title="Manage CCTV", columns=columns)
    return send_workbook(wb, dated_download_name("manage_cctv"))


@bp.route("/bulk-delete", methods=["POST"])
@require_permission("edit_assets")
def bulk_delete():
    ids = [int(x) for x in request.form.getlist("asset_ids") if x.isdigit()]
    if not ids:
        flash("No CCTV items selected.", "error")
    else:
        conn = get_connection()
        try:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, device_name, serial_tag FROM cctv_items WHERE id IN ({placeholders})", ids
            ).fetchall()
            performed_by = current_username()
            for row in rows:
                target = f"CCTV #{row['id']} ({row['device_name']} {row['serial_tag']})".strip()
                log_activity(conn, "cctv", "Deleted CCTV item", performed_by=performed_by, target=target)
            conn.execute(f"DELETE FROM cctv_items WHERE id IN ({placeholders})", ids)
            prune_stale_unmapped(conn)
            conn.commit()
            flash(f"Deleted {len(ids)} CCTV item(s).", "success")
        finally:
            conn.close()
    next_url = request.form.get("next") or url_for("cctv_edit.index")
    return redirect(next_url)


@bp.route("/<int:asset_id>/delete", methods=["POST"])
@require_permission("edit_assets")
def delete(asset_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, device_name, serial_tag FROM cctv_items WHERE id = ?", (asset_id,)
        ).fetchone()
        if not row:
            flash("CCTV item not found.", "error")
        else:
            target = f"CCTV #{asset_id} ({row['device_name']} {row['serial_tag']})".strip()
            log_activity(conn, "cctv", "Deleted CCTV item", performed_by=current_username(), target=target)
            conn.execute("DELETE FROM cctv_items WHERE id = ?", (asset_id,))
            prune_stale_unmapped(conn)
            conn.commit()
            flash(f"CCTV #{asset_id} deleted.", "success")
    finally:
        conn.close()
    next_url = request.form.get("next") or url_for("cctv_edit.index")
    return redirect(next_url)


@bp.route("/<int:asset_id>/edit", methods=["GET", "POST"])
@require_permission("edit_assets")
def edit(asset_id):
    conn = get_connection()
    try:
        if request.method == "POST":
            item = conn.execute("SELECT * FROM cctv_items WHERE id = ?", (asset_id,)).fetchone()
            if not item:
                flash("CCTV item not found.", "error")
                return redirect(url_for("cctv_edit.index"))

            values = {}
            for field_name in EDITABLE_FIELDS:
                value = request.form.get(field_name, "").strip()
                if field_name in UPPERCASE_FIELDS:
                    value = value.upper()
                values[field_name] = value

            set_clause = ", ".join(f"{f} = ?" for f in EDITABLE_FIELDS)
            conn.execute(
                f"UPDATE cctv_items SET {set_clause} WHERE id = ?",
                [*values.values(), asset_id],
            )
            performed_by = current_username()
            for field_name, new_value in values.items():
                old_value = item[field_name] or ""
                if old_value != new_value:
                    log_activity(
                        conn, "cctv", "Edited CCTV item", performed_by=performed_by,
                        target=f"CCTV #{asset_id}", field=field_name, old_value=old_value, new_value=new_value,
                    )
            prune_stale_unmapped(conn)
            conn.commit()
            flash(f"CCTV #{asset_id} updated.", "success")
            next_url = request.form.get("next") or url_for("cctv_edit.index")
            return redirect(next_url)

        item = conn.execute("SELECT * FROM cctv_items WHERE id = ?", (asset_id,)).fetchone()
        if not item:
            flash("CCTV item not found.", "error")
            return redirect(url_for("cctv_edit.index"))
        batch = conn.execute(
            "SELECT * FROM import_batches WHERE id = ?", (item["batch_id"],)
        ).fetchone()
        standard_names = [
            r["name"] for r in conn.execute("SELECT name FROM device_standard_names ORDER BY name").fetchall()
        ]
        standard_statuses = [
            r["name"] for r in conn.execute("SELECT name FROM status_standard_names ORDER BY name").fetchall()
        ]
    finally:
        conn.close()

    next_url = request.args.get("next", "")
    return render_template(
        "cctv_edit.html",
        active_page="cctv",
        asset=item,
        batch=batch,
        standard_names=standard_names,
        standard_statuses=standard_statuses,
        next_url=next_url,
    )
