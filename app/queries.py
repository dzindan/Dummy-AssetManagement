"""Shared read queries over asset_items.

Branches get re-imported on different cadences (one branch's June report
might land while another's is still from April) and each new import for a
branch is a *full replacement* of that branch's equipment list (a device
missing from the new file means it's gone, not still current). So "current
state" is computed per branch: for each branch, take every row from that
branch's single most-recent batch, going by that batch's *reporting period*
(import_batches.period, "YYYY-MM" - the "Reporting month" field on the
import form), not by batch_id/import order. Files don't always get uploaded
in period order (a June report landing before a backfilled February one is
normal), so batch_id alone would make the actually-newer period look stale
just because it happened to be imported first - see queries.py's git
history for the bug report this fixed. batch_id only breaks a tie when the
exact same period gets re-imported (a correction supersedes the earlier
upload of that same month). This is NOT the same as "the rows in the
globally most-recent batch" (which would only cover whichever branch
happened to be imported last) or "the latest batch per asset_key" (which
would let a retired device linger forever just because it isn't mentioned
again). Rows whose branch couldn't be auto-resolved are grouped by their raw
branch_dept text instead, so they still surface as their own bucket rather
than disappearing from the "current" view.

Diffing (added/removed/changed) uses the same per-branch latest-batch idea:
it compares a branch's two most recent batches (by period) as full
snapshots - see get_previous_batch_for_branch().
"""

from __future__ import annotations


def current_assets_cte(batch_where: str = "") -> str:
    """The "current state per branch" CTE (see this module's docstring),
    shared between every plain caller below (`batch_where` empty, exactly
    today's CURRENT_ASSETS_CTE) and analytics.get_year_comparison_table's
    "as of a given cutoff" snapshot (`batch_where` a `WHERE ib.period <= ?`
    clause) - the two used to be two near-identical copies of this same SQL.

    `batch_where`, when given, is inserted as raw SQL text right after the
    `branch_batches` JOIN, same as if it had been written directly into this
    string - always a small literal supplied by this module's own callers,
    never user input, so there's no injection risk in not parameterizing it
    the normal way.

    Deliberately NOT the same as adding an unconditional "exclude NULL/empty
    period" filter here for every caller: an asset-report batch's period
    should always be resolved (see importer.py) but isn't guaranteed to be
    for old/legacy data, and a branch whose only batch has no period must
    still show up as "current" for callers that don't ask for a `batch_where`
    (get_current_assets and friends) - only a cutoff-bounded caller like the
    year snapshot needs to also exclude those, since "as of some past date"
    can't mean anything for a batch with no reporting period to compare
    against the cutoff."""
    return f"""
WITH branch_key AS (
    SELECT ai.*, COALESCE(NULLIF(ai.branch_no, ''), 'UNRESOLVED:' || ai.branch_dept) AS bkey
    FROM asset_items ai
),
-- One row per (branch, batch) actually imported, paired with that batch's
-- own reporting period - kept to distinct (bkey, batch_id) pairs so this
-- stays cheap regardless of how many asset rows a batch has.
branch_batches AS (
    SELECT DISTINCT bk.bkey, bk.batch_id, ib.period
    FROM branch_key bk
    JOIN import_batches ib ON ib.id = bk.batch_id
    {batch_where}
),
latest_period AS (
    SELECT bkey, MAX(period) AS period
    FROM branch_batches
    GROUP BY bkey
),
latest_batch AS (
    -- "IS", not "=": a NULL period (shouldn't happen for a real import - see
    -- importer.py, which always resolves one - but must not silently vanish
    -- from "current" if it ever does) needs NULL-safe equality, since SQL's
    -- "NULL = NULL" is never true and would drop every such row here.
    SELECT bb.bkey, MAX(bb.batch_id) AS batch_id
    FROM branch_batches bb
    JOIN latest_period lp ON lp.bkey = bb.bkey AND lp.period IS bb.period
    GROUP BY bb.bkey
)
SELECT bk.*
FROM branch_key bk
JOIN latest_batch lb ON bk.bkey = lb.bkey AND bk.batch_id = lb.batch_id
"""


CURRENT_ASSETS_CTE = current_assets_cte()


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
    used by the Excel export, which dumps the full filtered set rather than
    just whatever page happens to be on screen. The Manage Assets page
    itself always passes a real page/per_page.
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

    period_filters = filters.get("period") or []
    if period_filters:
        where.append(f"ib.period IN ({','.join('?' * len(period_filters))})")
        params.extend(period_filters)

    if filters.get("q"):
        like = f"%{filters['q'].strip().upper()}%"
        where.append(
            "(UPPER(bk.serial_tag) LIKE ? OR UPPER(bk.model_device) LIKE ? OR "
            "UPPER(bk.full_name) LIKE ? OR UPPER(bk.user_id_raw) LIKE ? OR UPPER(bk.branch_dept) LIKE ? OR "
            "UPPER(bk.remark) LIKE ? OR UPPER(bk.position) LIKE ?)"
        )
        params.extend([like, like, like, like, like, like, like])

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
    """The batch that came before `before_batch_id` for this branch, by
    reporting period rather than raw batch_id/import order - same reasoning
    as CURRENT_ASSETS_CTE above. Diffing a backfilled, out-of-period-order
    import (e.g. uploading February after June is already on file) must
    compare against whatever period actually precedes it, not just
    whichever batch happened to be imported most recently in wall-clock
    time."""
    period_row = conn.execute(
        "SELECT ib.period FROM asset_items ai JOIN import_batches ib ON ib.id = ai.batch_id "
        "WHERE ai.batch_id = ? LIMIT 1",
        (before_batch_id,),
    ).fetchone()
    if period_row is None:
        return None
    row = conn.execute(
        """
        SELECT ai.batch_id
        FROM asset_items ai
        JOIN import_batches ib ON ib.id = ai.batch_id
        WHERE ai.branch_no = ? AND ib.period < ?
        ORDER BY ib.period DESC, ai.batch_id DESC
        LIMIT 1
        """,
        (branch_no, period_row["period"]),
    ).fetchone()
    return row["batch_id"] if row else None


def get_batch_assets_for_branch(conn, batch_id: int, branch_no: str):
    return conn.execute(
        "SELECT * FROM asset_items WHERE batch_id = ? AND branch_no = ?",
        (batch_id, branch_no),
    ).fetchall()


def search_handover_records(conn, filters: dict):
    """Filterable hand-over history - shared by the History page and its
    Excel export (routes/history.py) so both run the exact same query
    instead of the export copy-pasting the page's WHERE-building. `filters`
    is the same {"user_no", "branch_no", "date_from", "date_to"} shape
    History already builds from request.args. LEFT JOINs branches for
    eng_name (the page itself only shows the raw branch_no) since a
    downloaded report is more useful with the readable branch name."""
    where = ["1=1"]
    params: list = []
    if filters.get("user_no"):
        where.append("hr.user_no LIKE ?")
        params.append(f"%{filters['user_no']}%")
    if filters.get("branch_no"):
        where.append("hr.branch_no = ?")
        params.append(filters["branch_no"])
    if filters.get("date_from"):
        where.append("hr.ho_date >= ?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("hr.ho_date <= ?")
        params.append(filters["date_to"])

    sql = f"""
        SELECT hr.*, b.eng_name AS branch_eng_name
        FROM handover_records hr
        LEFT JOIN branches b ON b.branch_no = hr.branch_no
        WHERE {" AND ".join(where)}
        ORDER BY hr.id DESC
    """
    return conn.execute(sql, params).fetchall()
