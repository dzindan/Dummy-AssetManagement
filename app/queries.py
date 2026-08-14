"""Shared read queries over asset_items.

Branches get re-imported on different cadences (one branch's June report
might land while another's is still from April) and each new import for a
branch is a *full replacement* of that branch's equipment list (a device
missing from the new file means it's gone, not still current). So "current
state" is computed per branch: for each branch, take every row from that
branch's single most-recent batch_id. This is NOT the same as "the rows in
the globally most-recent batch" (which would only cover whichever branch
happened to be imported last) or "the latest batch per asset_key" (which
would let a retired device linger forever just because it isn't mentioned
again). Rows whose branch couldn't be auto-resolved are grouped by their raw
branch_dept text instead, so they still surface as their own bucket rather
than disappearing from the "current" view.

Diffing (added/removed/changed) uses the same per-branch latest-batch idea:
it compares a branch's two most recent batches as full snapshots.
"""

from __future__ import annotations


CURRENT_ASSETS_CTE = """
WITH branch_key AS (
    SELECT *, COALESCE(NULLIF(branch_no, ''), 'UNRESOLVED:' || branch_dept) AS bkey
    FROM asset_items
),
latest_batch AS (
    SELECT bkey, MAX(batch_id) AS batch_id
    FROM branch_key
    GROUP BY bkey
)
SELECT bk.*
FROM branch_key bk
JOIN latest_batch lb ON bk.bkey = lb.bkey AND bk.batch_id = lb.batch_id
"""


def get_current_assets(conn, branch_no: str | None = None, user_id_norm: str | None = None):
    sql = CURRENT_ASSETS_CTE
    conditions = []
    params: list = []
    if branch_no:
        conditions.append("bk.branch_no = ?")
        params.append(branch_no)
    if user_id_norm:
        conditions.append("bk.user_id_norm = ?")
        params.append(user_id_norm)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY bk.branch_dept, bk.device_name"
    return conn.execute(sql, params).fetchall()


def search_current_assets_by_serial(conn, serial_query: str):
    """Case-insensitive partial match on serial_tag, scoped to each branch's
    current (latest-batch) snapshot - same "current state" semantics as
    get_current_assets. Used by Lookup's "Search by Serial Number" option to
    find who currently holds a given asset."""
    sql = CURRENT_ASSETS_CTE + (
        " WHERE bk.serial_tag != '' AND UPPER(bk.serial_tag) LIKE ?"
        " ORDER BY bk.branch_dept, bk.device_name"
    )
    like = f"%{serial_query.strip().upper()}%"
    return conn.execute(sql, [like]).fetchall()


def get_current_asset_count(conn) -> int:
    sql = f"SELECT COUNT(*) AS c FROM ({CURRENT_ASSETS_CTE})"
    return conn.execute(sql).fetchone()["c"]


def get_current_branch_breakdown(conn):
    """One row per branch (resolved branch_no when known, otherwise grouped
    by its raw branch_dept text - see CURRENT_ASSETS_CTE), with a
    dashboard-friendly display name and count. `branch_no` is empty for
    unresolved buckets, which the dashboard renders as plain text instead of
    a link to the branch detail page."""
    sql = f"""
    SELECT
        bk.bkey AS bkey,
        MAX(bk.branch_no) AS branch_no,
        COALESCE(NULLIF(MAX(b.eng_name), ''), NULLIF(MAX(bk.branch_dept), ''), 'Unknown') AS display_name,
        COUNT(*) AS c
    FROM ({CURRENT_ASSETS_CTE}) bk
    LEFT JOIN branches b ON b.branch_no = bk.branch_no
    GROUP BY bk.bkey
    ORDER BY c DESC
    """
    return conn.execute(sql).fetchall()


UNRESOLVED_BRANCH_FILTER = "__unresolved__"


def get_branches_with_current_assets(conn):
    """Only branches that actually have at least one current asset - used to
    populate the Manage Assets branch filter so a selection can never return
    zero rows just because that branch hasn't been imported (yet)."""
    sql = f"""
    SELECT DISTINCT b.branch_no, b.eng_name
    FROM ({CURRENT_ASSETS_CTE}) bk
    JOIN branches b ON b.branch_no = bk.branch_no
    ORDER BY b.eng_name
    """
    return conn.execute(sql).fetchall()


def has_unresolved_current_assets(conn) -> bool:
    """Whether any current asset's branch text failed to auto-resolve to a
    branch_no - surfaced as a selectable "Unresolved" filter option so those
    rows are findable and fixable instead of just vanishing into a blank
    dashboard bucket."""
    sql = f"SELECT EXISTS (SELECT 1 FROM ({CURRENT_ASSETS_CTE}) bk WHERE bk.branch_no = '') AS e"
    return bool(conn.execute(sql).fetchone()["e"])


def search_assets(conn, filters: dict, page: int = 1, per_page: int | None = None):
    """Filterable view over *current* assets (see CURRENT_ASSETS_CTE
    docstring) for the standalone Manage Assets page - deliberately scoped
    to current state, like the rest of the app, so nobody accidentally
    edits a snapshot row that's already been superseded.

    `branch_no`, `device_name`, and `status` are each a *list* (possibly
    empty, meaning "no filter on this field") so the page can multi-select
    several values per field at once - e.g. Device = PC or LCD in one go,
    rather than one value at a time.

    `per_page=None` (the default) returns every matching row unpaginated -
    the Manage Assets page shows the full result set at once and filters
    further client-side. Pass a real per_page to paginate instead.
    """
    where = []
    params: list = []

    branch_filters = filters.get("branch_no") or []
    real_branches = [b for b in branch_filters if b != UNRESOLVED_BRANCH_FILTER]
    branch_conditions = []
    if UNRESOLVED_BRANCH_FILTER in branch_filters:
        branch_conditions.append("bk.branch_no = ''")
    if real_branches:
        branch_conditions.append(f"bk.branch_no IN ({','.join('?' * len(real_branches))})")
    if branch_conditions:
        where.append("(" + " OR ".join(branch_conditions) + ")")
        params.extend(real_branches)

    device_filters = filters.get("device_name") or []
    if device_filters:
        where.append(f"bk.device_name IN ({','.join('?' * len(device_filters))})")
        params.extend(device_filters)

    status_filters = filters.get("status") or []
    if status_filters:
        where.append(f"UPPER(bk.status) IN ({','.join('?' * len(status_filters))})")
        params.extend([s.strip().upper() for s in status_filters])

    if filters.get("q"):
        like = f"%{filters['q'].strip().upper()}%"
        where.append(
            "(UPPER(bk.serial_tag) LIKE ? OR UPPER(bk.model_device) LIKE ? OR "
            "UPPER(bk.full_name) LIKE ? OR UPPER(bk.user_id_raw) LIKE ? OR UPPER(bk.branch_dept) LIKE ?)"
        )
        params.extend([like, like, like, like, like])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    from_sql = f"""
    FROM ({CURRENT_ASSETS_CTE}) bk
    LEFT JOIN branches b ON b.branch_no = bk.branch_no
    LEFT JOIN import_batches ib ON ib.id = bk.batch_id
    {where_sql}
    """

    total = conn.execute(f"SELECT COUNT(*) AS c {from_sql}", params).fetchone()["c"]
    limit_sql = ""
    query_params = list(params)
    if per_page is not None:
        limit_sql = "LIMIT ? OFFSET ?"
        query_params += [per_page, max(page - 1, 0) * per_page]
    rows = conn.execute(
        f"""
        SELECT bk.*, b.eng_name AS branch_eng_name, ib.period AS period
        {from_sql}
        ORDER BY COALESCE(b.eng_name, bk.branch_dept), bk.device_name
        {limit_sql}
        """,
        query_params,
    ).fetchall()
    return rows, total


def find_duplicate_serials_in_batch(conn, batch_id: int):
    """Serial numbers that appear more than once within one specific import
    batch - the same check the Cleaning Report shows right after upload,
    but re-derivable any time from `batch_id` alone (see
    routes/import_data.py's `result` view), so revisiting the report after
    editing/deleting a row still reflects reality instead of relying on the
    one-shot in-memory report from the original POST."""
    sql = """
    SELECT UPPER(serial_tag) AS norm_serial, COUNT(*) AS cnt
    FROM asset_items
    WHERE batch_id = ? AND serial_tag != ''
    GROUP BY UPPER(serial_tag)
    HAVING COUNT(*) > 1
    ORDER BY norm_serial
    """
    dupes = conn.execute(sql, (batch_id,)).fetchall()

    results = []
    for d in dupes:
        rows = conn.execute(
            "SELECT * FROM asset_items WHERE batch_id = ? AND UPPER(serial_tag) = ?",
            (batch_id, d["norm_serial"]),
        ).fetchall()
        results.append(
            {
                "serial": rows[0]["serial_tag"] if rows else d["norm_serial"],
                "asset_ids": [r["id"] for r in rows],
                "rows": rows,
            }
        )
    return results


def find_unrecognized_in_batch(conn, batch_id: int, raw_column: str, alias_table: str, standard_table: str):
    """Distinct raw values in one batch (device_name_raw/status_raw/
    model_device_raw) that don't match any alias or standard name -
    re-derivable any time from `batch_id` alone, same reasoning as
    find_duplicate_serials_in_batch: the Cleaning Report is revisitable (see
    import_data.result), so this can't rely on the one-shot in-memory
    CleaningReport from the original import POST."""
    known_aliases = {row["alias"] for row in conn.execute(f"SELECT alias FROM {alias_table}").fetchall()}
    known_standards = {row["name"] for row in conn.execute(f"SELECT name FROM {standard_table}").fetchall()}
    rows = conn.execute(
        f"SELECT DISTINCT {raw_column} AS v FROM asset_items WHERE batch_id = ? AND {raw_column} != ''",
        (batch_id,),
    ).fetchall()
    return sorted(r["v"] for r in rows if r["v"] not in known_aliases and r["v"] not in known_standards)


def find_current_duplicate_serials(conn):
    """Serial numbers that appear more than once among *current* assets,
    across every branch - broader than the within-one-file check done at
    import time (importer._ingest_asset_rows), since duplicates can also
    come from two different branches' files both listing the same serial,
    or drift between separately-timed monthly imports. Returns
    [{"serial": ..., "rows": [...]}], one entry per duplicated serial."""
    sql = f"""
    WITH cur AS ({CURRENT_ASSETS_CTE})
    SELECT UPPER(serial_tag) AS norm_serial, COUNT(*) AS cnt
    FROM cur
    WHERE serial_tag != ''
    GROUP BY UPPER(serial_tag)
    HAVING COUNT(*) > 1
    ORDER BY norm_serial
    """
    dupes = conn.execute(sql).fetchall()

    results = []
    for d in dupes:
        rows = conn.execute(
            f"""
            WITH cur AS ({CURRENT_ASSETS_CTE})
            SELECT cur.*, b.eng_name AS branch_eng_name
            FROM cur
            LEFT JOIN branches b ON b.branch_no = cur.branch_no
            WHERE UPPER(cur.serial_tag) = ?
            """,
            (d["norm_serial"],),
        ).fetchall()
        results.append({"serial": rows[0]["serial_tag"] if rows else d["norm_serial"], "rows": rows})
    return results


def find_existing_batch_for_branch_period(conn, branch_no: str, period: str):
    """Most recent existing import batch for this branch already stamped
    with this period, if any - used to warn before importing a file that
    would otherwise silently add a second (or third...) import on top,
    e.g. from forgetting to change the Reporting Month field away from
    today's pre-filled default."""
    return conn.execute(
        """
        SELECT ib.id, ib.imported_at, ib.label, COUNT(ai.id) AS row_count
        FROM import_batches ib
        JOIN asset_items ai ON ai.batch_id = ib.id
        WHERE ai.branch_no = ? AND ib.period = ?
        GROUP BY ib.id
        ORDER BY ib.id DESC
        LIMIT 1
        """,
        (branch_no, period),
    ).fetchone()


def get_user_asset_history(conn, user_id_norm: str):
    """Every asset row ever seen for a user across *all* import batches,
    newest period first - deliberately spans full history (unlike
    get_current_assets, which only looks at each branch's latest batch), so
    a user's month-to-month equipment changes stay visible even after a
    later import supersedes them."""
    sql = """
    SELECT ai.*, ib.period AS period, ib.imported_at AS imported_at, ib.label AS batch_label
    FROM asset_items ai
    JOIN import_batches ib ON ib.id = ai.batch_id
    WHERE ai.user_id_norm = ?
    ORDER BY ib.period DESC, ai.batch_id DESC, ai.device_name
    """
    return conn.execute(sql, [user_id_norm]).fetchall()


def get_latest_batch(conn, kind: str = "asset_report"):
    return conn.execute(
        "SELECT * FROM import_batches WHERE kind = ? ORDER BY id DESC LIMIT 1",
        (kind,),
    ).fetchone()


def get_branch(conn, branch_no: str):
    """Look up one branch row by its branch_no PK - the single shared home
    for what used to be seven independent copies of this same lookup
    scattered across diffing.py, importer.py, and several routes."""
    if not branch_no:
        return None
    return conn.execute("SELECT * FROM branches WHERE branch_no = ?", (branch_no,)).fetchone()


def get_branches_in_batch(conn, batch_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT branch_no FROM asset_items WHERE batch_id = ? AND branch_no != ''",
        (batch_id,),
    ).fetchall()
    return [r["branch_no"] for r in rows]


def get_previous_batch_for_branch(conn, branch_no: str, before_batch_id: int) -> int | None:
    row = conn.execute(
        "SELECT MAX(batch_id) AS mb FROM asset_items WHERE branch_no = ? AND batch_id < ?",
        (branch_no, before_batch_id),
    ).fetchone()
    return row["mb"] if row and row["mb"] is not None else None


def get_batch_assets_for_branch(conn, batch_id: int, branch_no: str):
    return conn.execute(
        "SELECT * FROM asset_items WHERE batch_id = ? AND branch_no = ?",
        (batch_id, branch_no),
    ).fetchall()
