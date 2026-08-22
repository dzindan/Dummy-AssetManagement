from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..auth import current_username, require_permission
from ..db import get_connection, log_activity, prune_stale_unmapped
from ..exports import ASSET_ROW_COLUMNS, build_asset_rows_workbook, build_duplicates_workbook, send_workbook
from ..queries import (
    UNRESOLVED_BRANCH_FILTER,
    find_current_duplicate_serials,
    get_branch,
    get_branches_with_current_assets,
    has_unresolved_current_assets,
    search_assets,
)

bp = Blueprint("asset_edit", __name__, url_prefix="/assets")

# Manage Assets used to load every matching row at once (see search_assets'
# docstring) - fine at a few hundred rows, but painfully slow to render once
# a deployment's asset_items grew into the thousands. Paginated server-side
# now; the per-column header filter (app.js) still runs client-side, just
# scoped to whatever page is currently on screen.
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
    """Current query string (filters + per_page) with `page` stripped out -
    Prev/Next links append their own `&page=N` onto this rather than onto
    the raw request query string, so paging never accidentally carries a
    stale page number along for the ride."""
    pairs = [(k, v) for k in request.args if k != "page" for v in request.args.getlist(k)]
    return urlencode(pairs)

# Free-text fields a user can correct by hand (e.g. to resolve a duplicate
# serial flagged by the cleaning report). branch_no/batch_id/asset_key are
# deliberately not editable here - they're derived by the importer, not
# something to hand-edit.
EDITABLE_FIELDS = [
    "device_name",
    "model_device",
    "serial_tag",
    "status",
    "remark",
    "position",
    "branch_dept",
    "full_name",
    "user_id_raw",
    "handover_date",
    "ip",
]

# Every editable field is uppercased except handover_date, which is a date,
# not free text - matches the all-caps convention used everywhere else data
# ends up on a printed form or a report (see lookup.py's UPPERCASE_FIELDS).
UPPERCASE_FIELDS = set(EDITABLE_FIELDS) - {"handover_date"}


def _filters_from_args() -> dict:
    return {
        "branch_no": [v for v in request.args.getlist("branch_no") if v],
        "device_name": [v for v in request.args.getlist("device_name") if v],
        "status": [v for v in request.args.getlist("status") if v],
        "period": [v for v in request.args.getlist("period") if v],
        "q": request.args.get("q", "").strip(),
    }


@bp.route("/")
def index():
    filters = _filters_from_args()
    per_page = _parse_per_page()
    page = _parse_page()

    conn = get_connection()
    try:
        rows, total = search_assets(conn, filters, page=page, per_page=per_page)
        total_pages = (total + per_page - 1) // per_page if total else 1
        if page > total_pages:
            # Stale page number (filters changed, or rows were deleted out
            # from under it) - fall back to the last real page instead of
            # rendering an empty table.
            page = total_pages
            rows, total = search_assets(conn, filters, page=page, per_page=per_page)
        branches = get_branches_with_current_assets(conn)
        show_unresolved_option = has_unresolved_current_assets(conn)
        device_names = [
            r["device_name"]
            for r in conn.execute(
                "SELECT DISTINCT device_name FROM asset_items WHERE device_name != '' ORDER BY device_name"
            ).fetchall()
        ]
        # Union of the standard status list and whatever status values are
        # actually in use right now, so the filter covers legacy/non-standard
        # values too instead of hiding them the moment a standard list exists.
        current_statuses = {
            r["status"]
            for r in conn.execute(
                "SELECT DISTINCT status FROM asset_items WHERE status != ''"
            ).fetchall()
        }
        standard_statuses = {
            r["name"] for r in conn.execute("SELECT name FROM status_standard_names").fetchall()
        }
        status_options = sorted(current_statuses | standard_statuses)
        period_options = [
            r["period"]
            for r in conn.execute(
                "SELECT DISTINCT period FROM import_batches WHERE period != '' ORDER BY period DESC"
            ).fetchall()
        ]
        # Only worth showing the "filtered to this one branch" convenience
        # message/link (see template) when exactly one branch is selected -
        # with several selected at once there's no single branch to link to.
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
        "asset_management.html",
        active_page="assets",
        rows=rows,
        total=total,
        filters=filters,
        branches=branches,
        show_unresolved_option=show_unresolved_option,
        unresolved_value=UNRESOLVED_BRANCH_FILTER,
        device_names=device_names,
        status_options=status_options,
        period_options=period_options,
        selected_branch=selected_branch,
        page=page,
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
        total_pages=total_pages,
        pagination_qs=_pagination_qs(),
    )


@bp.route("/export")
def export():
    """Exports exactly what's currently on screen - same `filters` the page
    itself builds from request.args, so a filtered view exports filtered,
    matching branch_detail.export()'s existing "export what you're looking
    at" pattern rather than always dumping every asset."""
    filters = _filters_from_args()
    conn = get_connection()
    try:
        rows, _total = search_assets(conn, filters)
    finally:
        conn.close()

    columns = (
        [("Branch", lambda r: r["branch_eng_name"] or r["branch_dept"])]
        + list(ASSET_ROW_COLUMNS)
        + [("Period", lambda r: r["period"])]
    )
    wb = build_asset_rows_workbook(rows, sheet_title="Manage Assets", columns=columns)
    return send_workbook(wb, "manage_assets.xlsx")


@bp.route("/duplicates")
def duplicates():
    """On-demand, system-wide duplicate-serial check across every branch's
    current assets - broader than the automatic check shown right after an
    import, which only looks within that one file's own rows."""
    conn = get_connection()
    try:
        dupes = find_current_duplicate_serials(conn)
    finally:
        conn.close()
    return render_template("duplicate_check.html", active_page="assets", dupes=dupes)


@bp.route("/duplicates/export")
def export_duplicates():
    conn = get_connection()
    try:
        dupes = find_current_duplicate_serials(conn)
    finally:
        conn.close()
    if not dupes:
        flash("No duplicate serials found among current assets.", "error")
        return redirect(url_for("asset_edit.duplicates"))
    return send_workbook(build_duplicates_workbook(dupes), "duplicate_assets.xlsx")


@bp.route("/bulk-delete", methods=["POST"])
@require_permission("edit_assets")
def bulk_delete():
    ids = [int(x) for x in request.form.getlist("asset_ids") if x.isdigit()]
    if not ids:
        flash("No assets selected.", "error")
    else:
        conn = get_connection()
        try:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT id, device_name, serial_tag FROM asset_items WHERE id IN ({placeholders})", ids
            ).fetchall()
            performed_by = current_username()
            for row in rows:
                target = f"Asset #{row['id']} ({row['device_name']} {row['serial_tag']})".strip()
                log_activity(conn, "asset", "Deleted asset", performed_by=performed_by, target=target)
            conn.execute(f"DELETE FROM asset_items WHERE id IN ({placeholders})", ids)
            prune_stale_unmapped(conn)
            conn.commit()
            flash(f"Deleted {len(ids)} asset(s).", "success")
        finally:
            conn.close()
    next_url = request.form.get("next") or url_for("dashboard.index")
    return redirect(next_url)


@bp.route("/<int:asset_id>/delete", methods=["POST"])
@require_permission("edit_assets")
def delete(asset_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, device_name, serial_tag FROM asset_items WHERE id = ?", (asset_id,)
        ).fetchone()
        if not row:
            flash("Asset not found.", "error")
        else:
            target = f"Asset #{asset_id} ({row['device_name']} {row['serial_tag']})".strip()
            log_activity(conn, "asset", "Deleted asset", performed_by=current_username(), target=target)
            conn.execute("DELETE FROM asset_items WHERE id = ?", (asset_id,))
            prune_stale_unmapped(conn)
            conn.commit()
            flash(f"Asset #{asset_id} deleted.", "success")
    finally:
        conn.close()
    next_url = request.form.get("next") or url_for("dashboard.index")
    return redirect(next_url)


@bp.route("/<int:asset_id>/edit", methods=["GET", "POST"])
@require_permission("edit_assets")
def edit(asset_id):
    conn = get_connection()
    try:
        if request.method == "POST":
            asset = conn.execute("SELECT * FROM asset_items WHERE id = ?", (asset_id,)).fetchone()
            if not asset:
                flash("Asset not found.", "error")
                return redirect(url_for("dashboard.index"))

            values = {}
            for field_name in EDITABLE_FIELDS:
                value = request.form.get(field_name, "").strip()
                values[field_name] = value.upper() if field_name in UPPERCASE_FIELDS else value

            set_clause = ", ".join(f"{f} = ?" for f in EDITABLE_FIELDS)
            conn.execute(
                f"UPDATE asset_items SET {set_clause} WHERE id = ?",
                [*values.values(), asset_id],
            )
            performed_by = current_username()
            for field_name, new_value in values.items():
                old_value = asset[field_name] or ""
                if old_value != new_value:
                    log_activity(
                        conn, "asset", "Edited asset", performed_by=performed_by,
                        target=f"Asset #{asset_id}", field=field_name, old_value=old_value, new_value=new_value,
                    )
            prune_stale_unmapped(conn)
            conn.commit()
            flash(f"Asset #{asset_id} updated.", "success")
            next_url = request.form.get("next") or url_for("dashboard.index")
            return redirect(next_url)

        asset = conn.execute("SELECT * FROM asset_items WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            flash("Asset not found.", "error")
            return redirect(url_for("dashboard.index"))
        batch = conn.execute(
            "SELECT * FROM import_batches WHERE id = ?", (asset["batch_id"],)
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
        "asset_edit.html",
        active_page="assets",
        asset=asset,
        batch=batch,
        standard_names=standard_names,
        standard_statuses=standard_statuses,
        next_url=next_url,
    )
