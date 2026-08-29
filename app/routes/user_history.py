from flask import Blueprint, render_template, request

from ..db import get_connection
from ..exports import build_workbook, send_workbook
from ..importer import find_user, normalize_user_id
from ..paths import safe_filename
from ..queries import get_user_asset_history

bp = Blueprint("user_history", __name__, url_prefix="/user-history")

USER_HISTORY_EXPORT_COLUMNS = [
    ("Branch", "branch_dept"), ("Device", "device_name"), ("Model", "model_device"),
    ("Serial/Tag", "serial_tag"), ("Status", "status"), ("Handover Date", "handover_date"),
    ("Period", "period"), ("Change", "change"),
]


def _group_by_period(rows):
    """Group history rows (already ordered newest period first) into one
    entry per period, then compute what changed against the next-older
    period so a transition like "June: A1,A2,A3 -> July: B1,B2,B3" is
    visible directly under July's heading."""
    groups: list[dict] = []
    index_by_period: dict[str, int] = {}
    for row in rows:
        period = row["period"] or "(unknown period)"
        if period not in index_by_period:
            index_by_period[period] = len(groups)
            groups.append({"period": period, "batch_label": row["batch_label"], "assets": []})
        groups[index_by_period[period]]["assets"].append(row)

    for i, group in enumerate(groups):
        older = groups[i + 1] if i + 1 < len(groups) else None
        cur_keys = {item["asset_key"] for item in group["assets"]}
        older_keys = {item["asset_key"] for item in older["assets"]} if older else set()
        group["added_keys"] = cur_keys - older_keys if older else set()
        group["removed"] = (
            [item for item in older["assets"] if item["asset_key"] not in cur_keys] if older else []
        )
    return groups


def _flatten_history_rows(groups: list[dict]) -> list[dict]:
    """One row per (period, asset) for export - each currently-held asset
    marked "Added" if it's new since the previous period, plus one row per
    dropped asset marked "Removed" - same Added/Removed distinction the
    page itself shows, just flattened into a single sheet."""
    flat = []
    for g in groups:
        for item in g["assets"]:
            flat.append(
                {
                    "period": g["period"], "device_name": item["device_name"], "model_device": item["model_device"],
                    "serial_tag": item["serial_tag"], "status": item["status"], "branch_dept": item["branch_dept"],
                    "handover_date": item["handover_date"],
                    "change": "Added" if item["asset_key"] in g["added_keys"] else "",
                }
            )
        for item in g["removed"]:
            flat.append(
                {
                    "period": g["period"], "device_name": item["device_name"], "model_device": item["model_device"],
                    "serial_tag": item["serial_tag"], "status": item["status"], "branch_dept": item["branch_dept"],
                    "handover_date": item["handover_date"],
                    "change": "Removed",
                }
            )
    return flat


def _load_groups(q: str):
    conn = get_connection()
    try:
        user = None
        groups = []
        if q:
            user = find_user(conn, q)
            user_id_norm = normalize_user_id(user["user_no"])[1] if user else normalize_user_id(q)[1]
            rows = get_user_asset_history(conn, user_id_norm)
            groups = _group_by_period(rows)
    finally:
        conn.close()
    return user, groups


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    user, groups = _load_groups(q)
    return render_template(
        "user_history.html",
        active_page="user_history",
        q=q,
        user=user,
        groups=groups,
    )


@bp.route("/export")
def export():
    q = request.args.get("q", "").strip()
    _user, groups = _load_groups(q)
    wb = build_workbook("User Asset History", USER_HISTORY_EXPORT_COLUMNS, _flatten_history_rows(groups))
    safe_q = safe_filename(q, fallback="export")
    return send_workbook(wb, f"user_history_{safe_q}.xlsx")
