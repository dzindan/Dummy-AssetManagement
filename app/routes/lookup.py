import datetime as dt

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..auth import current_username, require_permission
from ..db import get_connection, get_setting
from ..handover import ASSET_CONDITIONS, HO_TYPES, REASONS, apply_handover_date, record_handover, render_handover_docx
from ..importer import find_user, normalize_user_id
from ..queries import get_branch, get_current_assets, search_current_assets_by_serial
from ..text_utils import strip_bank_prefix

bp = Blueprint("lookup", __name__, url_prefix="/lookup")

# Free-text fields that end up printed on the hand-over form and should
# always be stored/shown in caps, matching how the bank's own paperwork is
# filled in.
UPPERCASE_FIELDS = [
    "ict_rep_name",
    "ict_rep_id",
    "receiving_name",
    "receiving_title",
    "receiving_dept",
    "receiving_id",
    "other_type_text",
    "other_reason_text",
    "signature_receiving_name",
    "signature_prepared_by",
]


def _uc(value: str) -> str:
    return (value or "").strip().upper()


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    search_by = request.args.get("search_by", "user").strip()
    if search_by not in ("user", "serial"):
        search_by = "user"

    conn = get_connection()
    try:
        user = None
        branch = None
        assets = []
        serial_matches = []
        if q and search_by == "serial":
            # Serial search is a finder, not a hand-over builder: it shows who
            # currently holds the matching asset(s), then the user clicks
            # through to the normal per-user flow to actually build the form -
            # simpler and less error-prone than guessing a single owner here.
            serial_matches = search_current_assets_by_serial(conn, q)
        elif q:
            user = find_user(conn, q)
            if user:
                user_id_norm = normalize_user_id(user["user_no"])[1]
                branch = get_branch(conn, user["branch_no"])
                assets = get_current_assets(conn, user_id_norm=user_id_norm)
            else:
                _, norm = normalize_user_id(q)
                assets = get_current_assets(conn, user_id_norm=norm)

        default_ict_name = get_setting("ict_rep_name", "")
        default_ict_id = get_setting("ict_rep_id", "")
    finally:
        conn.close()

    default_receiving_dept = strip_bank_prefix(branch["eng_name"]) if branch else ""

    return render_template(
        "lookup.html",
        active_page="lookup",
        q=q,
        search_by=search_by,
        user=user,
        branch=branch,
        assets=assets,
        serial_matches=serial_matches,
        ho_types=HO_TYPES,
        reasons=REASONS,
        asset_conditions=ASSET_CONDITIONS,
        default_ict_name=default_ict_name,
        default_ict_id=default_ict_id,
        default_receiving_dept=default_receiving_dept,
        today=dt.date.today().isoformat(),
    )


def _collect_form_data(form) -> dict:
    data = {
        "user_no": form.get("user_no", "").strip(),
        "ho_date": form.get("ho_date", "").strip(),
        "ho_type": form.get("ho_type", "").strip(),
        "temp_due_date": form.get("temp_due_date", "").strip(),
        "reason": form.get("reason", "").strip(),
        "asset_id": form.getlist("asset_id"),
    }
    for field in UPPERCASE_FIELDS:
        data[field] = _uc(form.get(field, ""))
    return data


def _load_assets(conn, form, asset_ids: list[str]):
    """Re-fetch the selected assets from the DB (never trust client-supplied
    device/serial text) and pair each with its hand-over condition (NEW or
    used-from-WAREHOUSE), which the form captures separately per asset since
    it's independent of the asset's day-to-day operational status."""
    ids = [int(x) for x in asset_ids if x.strip().isdigit()]
    if not ids:
        return [], ""
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM asset_items WHERE id IN ({placeholders})", ids).fetchall()
    branch_no = ""
    assets = []
    for r in rows:
        branch_no = branch_no or r["branch_no"]
        condition = form.get(f"condition_{r['id']}", "WAREHOUSE").strip().upper()
        if condition not in ASSET_CONDITIONS:
            condition = "WAREHOUSE"
        assets.append(
            {
                "id": r["id"],
                "equipment_name": r["device_name"],
                "qty": 1,
                "serial_number": r["serial_tag"],
                "status": condition,
            }
        )
    return assets, branch_no


@bp.route("/review", methods=["POST"])
def review():
    data = _collect_form_data(request.form)

    conn = get_connection()
    try:
        assets, branch_no = _load_assets(conn, request.form, data["asset_id"])
    finally:
        conn.close()
    data["branch_no"] = branch_no

    if not data["receiving_name"]:
        flash("Receiving party full name is required.", "error")
        return redirect(url_for("lookup.index", q=data["user_no"]))
    if not assets:
        flash("Select at least one asset to include on the hand-over form.", "error")
        return redirect(url_for("lookup.index", q=data["user_no"]))

    return render_template(
        "lookup_review.html",
        active_page="lookup",
        data=data,
        assets=assets,
        total_qty=sum(int(a["qty"] or 1) for a in assets),
    )


@bp.route("/generate", methods=["POST"])
@require_permission("handover")
def generate():
    data = _collect_form_data(request.form)

    conn = get_connection()
    try:
        assets, branch_no = _load_assets(conn, request.form, data["asset_id"])
    finally:
        conn.close()
    data["branch_no"] = branch_no
    data["assets"] = assets

    if not data["receiving_name"]:
        flash("Receiving party full name is required.", "error")
        return redirect(url_for("lookup.index", q=data["user_no"]))
    if not assets:
        flash("Select at least one asset to include on the hand-over form.", "error")
        return redirect(url_for("lookup.index", q=data["user_no"]))

    # Only this action - clicking "Confirm & Download" past the review step -
    # writes the .docx and logs it. Reviewing never touches the log.
    docx_path = render_handover_docx(data)
    record_id = record_handover(data, docx_path, created_by=current_username())

    # The date on the form itself becomes each included asset's recorded
    # hand-over date in Manage Assets - the same "ho_date" already used on
    # the printed form/handover_records, applied to asset_items too, since
    # that's the actual field an import or manual edit would otherwise
    # leave blank/stale.
    apply_handover_date(
        [a["id"] for a in assets], data["ho_date"], performed_by=current_username()
    )

    return redirect(url_for("lookup.success", record_id=record_id))


@bp.route("/success/<int:record_id>")
def success(record_id):
    conn = get_connection()
    try:
        record = conn.execute(
            "SELECT * FROM handover_records WHERE id = ?", (record_id,)
        ).fetchone()
    finally:
        conn.close()
    if not record:
        flash("Hand-over record not found.", "error")
        return redirect(url_for("lookup.index"))
    return render_template("lookup_success.html", active_page="lookup", record=record)
