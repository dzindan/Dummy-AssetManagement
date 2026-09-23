from flask import Blueprint, render_template, request

from ..db import get_connection
from ..exports import build_workbook, dated_download_name, send_workbook
from ..importer import find_user, normalize_user_id
from ..paths import safe_filename
from ..queries import get_serial_history, get_user_asset_history

bp = Blueprint("user_history", __name__, url_prefix="/user-history")

USER_HISTORY_EXPORT_COLUMNS = [
    ("Branch", "branch_dept"), ("Device", "device_name"), ("Model", "model_device"),
    ("Serial/Tag", "serial_tag"), ("Status", "status"), ("Handover Date", "handover_date"),
    ("Period", "period"), ("Change", "change"),
]

SERIAL_HISTORY_EXPORT_COLUMNS = [
    ("From Period", "from_period"), ("To Period", "to_period"),
    ("User ID", "user_id_raw"), ("Full Name", "full_name"), ("Branch", "branch_dept"),
    ("Device", "device_name"), ("Model", "model_device"), ("Status", "status"),
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
        # Kept alongside "removed" so _annotate_handover_neighbors can find
        # exactly which custody segment a removed item's last period was,
        # without re-deriving group adjacency itself.
        group["older_period"] = older["period"] if older else None
    return groups


def _build_custody_segments(rows):
    """rows = get_serial_history()'s output (one period-ordered snapshot
    per period, oldest first). Collapses them into one entry per
    continuous stretch held by the same user_id_norm - a segment boundary
    is a hand-over event. Mirrors diffing.py's old->new field framing, but
    walks arbitrarily many periods for one device instead of just two
    adjacent batches."""
    segments = []
    for row in rows:
        uid = row["user_id_norm"] or ""
        if not segments or segments[-1]["user_id_norm"] != uid:
            segments.append({
                "user_id_norm": uid, "user_id_raw": row["user_id_raw"],
                "full_name": row["full_name"], "branch_dept": row["branch_dept"],
                "device_name": row["device_name"], "model_device": row["model_device"],
                "status": row["status"], "from_period": row["period"], "to_period": row["period"],
            })
        else:
            segments[-1]["to_period"] = row["period"]
            segments[-1]["status"] = row["status"]
    return segments


def _find_neighbor(segments, user_id_norm, period, edge):
    """`edge="start"` finds the segment that begins at `period` for
    `user_id_norm` (an Added event) and returns the segment right before
    it - who had the device previously. `edge="end"` finds the segment
    that ends at `period` for `user_id_norm` (a Removed event) and returns
    the segment right after it - who got the device next. None if there's
    no such neighbor in the recorded history (first-ever holder, or still
    unaccounted for after leaving this user)."""
    for i, seg in enumerate(segments):
        if seg["user_id_norm"] != user_id_norm:
            continue
        if edge == "start" and seg["from_period"] == period and i > 0:
            return segments[i - 1]
        if edge == "end" and seg["to_period"] == period and i + 1 < len(segments):
            return segments[i + 1]
    return None


def _annotate_handover_neighbors(conn, groups, user_id_norm):
    """For every Added/Removed item that carries a serial, resolve who the
    device came from / went to via the same custody-segment engine the
    serial-search mode uses - so reading one user's history also answers
    "who had this before/after them" without a second search. Items with
    no serial are left unannotated (no stable identity to trace - see
    get_serial_history's docstring)."""
    segments_by_serial: dict[str, list] = {}

    def segments_for(serial: str):
        key = serial.strip().upper()
        if key not in segments_by_serial:
            segments_by_serial[key] = _build_custody_segments(get_serial_history(conn, key))
        return segments_by_serial[key]

    for group in groups:
        group["added_from"] = {}
        for item in group["assets"]:
            if item["serial_tag"] and item["asset_key"] in group["added_keys"]:
                neighbor = _find_neighbor(segments_for(item["serial_tag"]), user_id_norm, group["period"], "start")
                if neighbor:
                    group["added_from"][item["asset_key"]] = neighbor

        group["removed_to"] = {}
        if group["older_period"]:
            for item in group["removed"]:
                if item["serial_tag"]:
                    neighbor = _find_neighbor(
                        segments_for(item["serial_tag"]), user_id_norm, group["older_period"], "end"
                    )
                    if neighbor:
                        group["removed_to"][item["asset_key"]] = neighbor


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
            if groups:
                _annotate_handover_neighbors(conn, groups, user_id_norm)
    finally:
        conn.close()
    return user, groups


def _load_serial_history(q: str):
    conn = get_connection()
    try:
        segments = _build_custody_segments(get_serial_history(conn, q)) if q else []
    finally:
        conn.close()
    return segments


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    search_by = request.args.get("search_by", "user")
    if search_by == "serial":
        segments = _load_serial_history(q)
        return render_template(
            "user_history.html",
            active_page="user_history",
            q=q, search_by=search_by,
            user=None, groups=[], segments=segments,
        )
    user, groups = _load_groups(q)
    return render_template(
        "user_history.html",
        active_page="user_history",
        q=q, search_by=search_by,
        user=user, groups=groups, segments=[],
    )


@bp.route("/export")
def export():
    q = request.args.get("q", "").strip()
    search_by = request.args.get("search_by", "user")
    safe_q = safe_filename(q, fallback="export")
    if search_by == "serial":
        segments = _load_serial_history(q)
        wb = build_workbook("Device Handover History", SERIAL_HISTORY_EXPORT_COLUMNS, segments)
        return send_workbook(wb, dated_download_name(f"device_history_{safe_q}"))
    _user, groups = _load_groups(q)
    wb = build_workbook("User Asset History", USER_HISTORY_EXPORT_COLUMNS, _flatten_history_rows(groups))
    return send_workbook(wb, dated_download_name(f"user_history_{safe_q}"))
