import os
import shutil
import sys

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..db import get_connection, get_setting, init_db, prune_stale_unmapped, set_setting
from ..importer import (
    normalize_branch_text,
    record_unmapped_device,
    record_unmapped_model,
    record_unmapped_status,
    reresolve_unresolved_assets,
)
from ..paths import get_app_data_dir, get_default_app_data_dir, set_app_data_dir

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/")
def index():
    conn = get_connection()
    try:
        branch_aliases = conn.execute(
            """
            SELECT ba.alias, ba.branch_no, b.eng_name
            FROM branch_aliases ba
            LEFT JOIN branches b ON b.branch_no = ba.branch_no
            ORDER BY ba.alias
            """
        ).fetchall()
        branches = conn.execute("SELECT branch_no, eng_name FROM branches ORDER BY eng_name").fetchall()

        standard_names = [r["name"] for r in conn.execute(
            "SELECT name FROM device_standard_names ORDER BY name"
        ).fetchall()]

        standard_statuses = [r["name"] for r in conn.execute(
            "SELECT name FROM status_standard_names ORDER BY name"
        ).fetchall()]

        standard_models = [r["name"] for r in conn.execute(
            "SELECT name FROM model_standard_names ORDER BY name"
        ).fetchall()]

        aliases_by_name: dict[str, list[str]] = {}
        for row in conn.execute("SELECT alias, canonical_name FROM device_aliases ORDER BY alias").fetchall():
            aliases_by_name.setdefault(row["canonical_name"], []).append(row["alias"])

        status_aliases_by_name: dict[str, list[str]] = {}
        for row in conn.execute("SELECT alias, canonical_name FROM status_aliases ORDER BY alias").fetchall():
            status_aliases_by_name.setdefault(row["canonical_name"], []).append(row["alias"])

        model_aliases_by_name: dict[str, list[str]] = {}
        for row in conn.execute("SELECT alias, canonical_name FROM model_aliases ORDER BY alias").fetchall():
            model_aliases_by_name.setdefault(row["canonical_name"], []).append(row["alias"])

        unmapped_devices = conn.execute(
            "SELECT raw_name, occurrences FROM device_unmapped ORDER BY occurrences DESC, raw_name"
        ).fetchall()

        unmapped_statuses = conn.execute(
            "SELECT raw_status, occurrences FROM status_unmapped ORDER BY occurrences DESC, raw_status"
        ).fetchall()

        unmapped_models = conn.execute(
            "SELECT raw_model, occurrences FROM model_unmapped ORDER BY occurrences DESC, raw_model"
        ).fetchall()

        unresolved_branches = conn.execute(
            "SELECT raw_hint, occurrences FROM branch_unresolved ORDER BY occurrences DESC, raw_hint"
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        "settings.html",
        active_page="settings",
        branch_aliases=branch_aliases,
        branches=branches,
        standard_names=standard_names,
        standard_statuses=standard_statuses,
        standard_models=standard_models,
        aliases_by_name=aliases_by_name,
        status_aliases_by_name=status_aliases_by_name,
        model_aliases_by_name=model_aliases_by_name,
        unmapped_devices=unmapped_devices,
        unmapped_statuses=unmapped_statuses,
        unmapped_models=unmapped_models,
        unresolved_branches=unresolved_branches,
        ict_rep_name=get_setting("ict_rep_name", ""),
        ict_rep_id=get_setting("ict_rep_id", ""),
        asset_reports_folder=get_setting("asset_reports_folder", ""),
        id_files_folder=get_setting("id_files_folder", ""),
        data_dir=get_app_data_dir(),
        default_data_dir=get_default_app_data_dir(),
        lan_url=current_app.config.get("LAN_URL", ""),
    )


@bp.route("/general", methods=["POST"])
def save_general():
    set_setting("ict_rep_name", request.form.get("ict_rep_name", "").strip().upper())
    set_setting("ict_rep_id", request.form.get("ict_rep_id", "").strip().upper())
    set_setting("asset_reports_folder", request.form.get("asset_reports_folder", "").strip())
    set_setting("id_files_folder", request.form.get("id_files_folder", "").strip())
    flash("Settings saved.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/branch-alias/add", methods=["POST"])
def add_branch_alias():
    alias_text = request.form.get("alias_text", "").strip()
    branch_no = request.form.get("branch_no", "").strip()
    if not alias_text or not branch_no:
        flash("Both an alias text and a target branch are required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO branch_aliases (alias, branch_no) VALUES (?, ?)",
            (normalize_branch_text(alias_text), branch_no),
        )
        fixed_count = reresolve_unresolved_assets(conn, alias_text, branch_no)
        conn.commit()
    finally:
        conn.close()
    msg = f'Branch alias "{alias_text}" saved.'
    if fixed_count:
        msg += f" Fixed {fixed_count} already-imported asset(s) that were sitting unresolved."
    flash(msg, "success")
    return redirect(url_for("settings.index"))


@bp.route("/branch-alias/delete", methods=["POST"])
def delete_branch_alias():
    alias = request.form.get("alias", "")
    conn = get_connection()
    try:
        conn.execute("DELETE FROM branch_aliases WHERE alias = ?", (alias,))
        conn.commit()
    finally:
        conn.close()
    flash("Branch alias removed.", "success")
    return redirect(url_for("settings.index"))


# --- Branch mapping (unresolved raw hint -> existing branch) ----------------
# Same idea as device mapping, but branches are a closed set from the
# official master list (IDFromAither) rather than something new that can be
# "created" here - only assigning an unresolved hint to one of the existing
# branches makes sense.

@bp.route("/branch-hint/map", methods=["POST"])
def map_branch_hint():
    raw_hint = request.form.get("raw_hint", "").strip()
    branch_no = request.form.get("branch_no", "").strip()
    if not raw_hint or not branch_no:
        flash("Both a branch label and a target branch are required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO branch_aliases (alias, branch_no) VALUES (?, ?)",
            (normalize_branch_text(raw_hint), branch_no),
        )
        conn.execute("DELETE FROM branch_unresolved WHERE raw_hint = ?", (raw_hint,))
        fixed_count = reresolve_unresolved_assets(conn, raw_hint, branch_no)
        conn.commit()
    finally:
        conn.close()
    msg = f'"{raw_hint}" mapped - future imports will resolve it automatically.'
    if fixed_count:
        msg += f" Fixed {fixed_count} already-imported asset(s) that were sitting unresolved."
    flash(msg, "success")
    return redirect(url_for("settings.index"))


@bp.route("/branch-hint/dismiss", methods=["POST"])
def dismiss_branch_hint():
    """Remove a label from the unresolved pool without mapping it - e.g. it
    was a one-off typo that's already been fixed at the source and won't
    recur."""
    raw_hint = request.form.get("raw_hint", "").strip()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM branch_unresolved WHERE raw_hint = ?", (raw_hint,))
        conn.commit()
    finally:
        conn.close()
    flash(f'"{raw_hint}" dismissed.', "success")
    return redirect(url_for("settings.index"))


def _rename_or_merge_standard(standard_table: str, alias_table: str, old_name: str, new_name: str) -> str:
    """Rename a standard name to `new_name` - shared by the Device/Status/
    Model "Rename" buttons, which all hit the exact same `name TEXT PRIMARY
    KEY` shape. A plain UPDATE crashes with a UNIQUE constraint error the
    moment `new_name` already exists as its own entry (e.g. renaming "TV"
    to an existing "TABLET"), so if that's the case this merges into it
    instead: every alias that pointed at `old_name` gets moved over to
    `new_name`, `old_name` itself is kept as an alias of `new_name` (so a
    raw value that still says "old_name" in some future import keeps
    resolving correctly), and the now-redundant `old_name` standard entry
    is removed. Returns a flash-ready message describing what happened."""
    conn = get_connection()
    try:
        if old_name == new_name:
            return f'"{old_name}" is already named that - nothing to do.'
        already_exists = conn.execute(
            f"SELECT 1 FROM {standard_table} WHERE name = ?", (new_name,)
        ).fetchone()
        if already_exists:
            conn.execute(
                f"UPDATE {alias_table} SET canonical_name = ? WHERE canonical_name = ?", (new_name, old_name)
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {alias_table} (alias, canonical_name) VALUES (?, ?)",
                (old_name, new_name),
            )
            conn.execute(f"DELETE FROM {standard_table} WHERE name = ?", (old_name,))
            conn.commit()
            return f'"{new_name}" already existed - merged "{old_name}" into it instead of renaming.'
        conn.execute(f"UPDATE {standard_table} SET name = ? WHERE name = ?", (new_name, old_name))
        conn.execute(f"UPDATE {alias_table} SET canonical_name = ? WHERE canonical_name = ?", (new_name, old_name))
        conn.commit()
        return f'Renamed "{old_name}" to "{new_name}".'
    finally:
        conn.close()


# --- Device standard names (the editable canonical list) -------------------

@bp.route("/device-standard/add", methods=["POST"])
def add_standard_name():
    name = request.form.get("name", "").strip().upper()
    if not name:
        flash("A standard device name is required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO device_standard_names (name, created_at) VALUES (?, datetime('now'))",
            (name,),
        )
        # If this exact name was sitting in the unmapped pool, it's now a
        # recognized standard name on its own - no separate alias needed.
        conn.execute("DELETE FROM device_unmapped WHERE raw_name = ?", (name,))
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard device name "{name}" added.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/device-standard/rename", methods=["POST"])
def rename_standard_name():
    old_name = request.form.get("old_name", "").strip().upper()
    new_name = request.form.get("new_name", "").strip().upper()
    if not old_name or not new_name:
        flash("A new name is required.", "error")
        return redirect(url_for("settings.index"))

    message = _rename_or_merge_standard("device_standard_names", "device_aliases", old_name, new_name)
    flash(message, "success")
    return redirect(url_for("settings.index"))


@bp.route("/device-standard/delete", methods=["POST"])
def delete_standard_name():
    name = request.form.get("name", "").strip().upper()
    conn = get_connection()
    try:
        # Aliases that pointed to this standard name go back into the
        # unmapped pool instead of silently disappearing, so nothing is lost.
        aliases = conn.execute(
            "SELECT alias FROM device_aliases WHERE canonical_name = ?", (name,)
        ).fetchall()
        for row in aliases:
            record_unmapped_device(conn, row["alias"])
        conn.execute("DELETE FROM device_aliases WHERE canonical_name = ?", (name,))
        conn.execute("DELETE FROM device_standard_names WHERE name = ?", (name,))
        # Re-sync already-imported assets that were showing this now-deleted
        # canonical name: drop each one back to its own raw text (exactly
        # what a fresh import would resolve to today, with the alias/
        # standard name gone) - without this, Manage Assets kept showing the
        # old canonical name on every affected row even though Settings
        # correctly moved its alias back to Unmapped.
        conn.execute("UPDATE asset_items SET device_name = UPPER(device_name_raw) WHERE device_name = ?", (name,))
        # After that resync, any row still showing `name` has it because its
        # own raw text literally IS `name` (no alias was ever involved, so
        # the alias loop above never queued it) - queue that into Unmapped
        # too, or it vanishes from every mapping list despite still being here.
        if conn.execute("SELECT 1 FROM asset_items WHERE device_name = ? LIMIT 1", (name,)).fetchone():
            record_unmapped_device(conn, name)
        # An alias re-queued above might itself be a phantom - e.g. a seed
        # default like COMPUTER->PC that no actual import ever used - which
        # would otherwise sit in Settings > Unmapped forever pointing at
        # zero real assets. prune_stale_unmapped() removes it right back out
        # if nothing in asset_items actually carries that value.
        prune_stale_unmapped(conn)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard name "{name}" removed.', "success")
    return redirect(url_for("settings.index"))


# --- Standard status list + mapping (same standard-name/alias/unmapped
# pattern as devices above) --------------------------------------------------

@bp.route("/status-standard/add", methods=["POST"])
def add_standard_status():
    name = request.form.get("name", "").strip().upper()
    if not name:
        flash("A standard status is required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO status_standard_names (name, created_at) VALUES (?, datetime('now'))",
            (name,),
        )
        conn.execute("DELETE FROM status_unmapped WHERE raw_status = ?", (name,))
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard status "{name}" added.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-standard/rename", methods=["POST"])
def rename_standard_status():
    old_name = request.form.get("old_name", "").strip().upper()
    new_name = request.form.get("new_name", "").strip().upper()
    if not old_name or not new_name:
        flash("A new name is required.", "error")
        return redirect(url_for("settings.index"))

    message = _rename_or_merge_standard("status_standard_names", "status_aliases", old_name, new_name)
    flash(message, "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-standard/delete", methods=["POST"])
def delete_standard_status():
    name = request.form.get("name", "").strip().upper()
    conn = get_connection()
    try:
        aliases = conn.execute(
            "SELECT alias FROM status_aliases WHERE canonical_name = ?", (name,)
        ).fetchall()
        for row in aliases:
            record_unmapped_status(conn, row["alias"])
        conn.execute("DELETE FROM status_aliases WHERE canonical_name = ?", (name,))
        conn.execute("DELETE FROM status_standard_names WHERE name = ?", (name,))
        # See delete_standard_name()'s comment - same resync for assets whose
        # displayed status pointed at this now-deleted canonical name.
        conn.execute("UPDATE asset_items SET status = UPPER(status_raw) WHERE status = ?", (name,))
        if conn.execute("SELECT 1 FROM asset_items WHERE status = ? LIMIT 1", (name,)).fetchone():
            record_unmapped_status(conn, name)
        # See delete_standard_name()'s comment on prune_stale_unmapped().
        prune_stale_unmapped(conn)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard status "{name}" removed.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-alias/map", methods=["POST"])
def map_status_alias():
    """Used both by the drag-and-drop UI (fetch POST) and the fallback
    dropdown-and-button form for the same action: assign a raw status to a
    standard status."""
    alias = request.form.get("alias", "").strip().upper()
    canonical_name = request.form.get("canonical_name", "").strip().upper()
    if not alias or not canonical_name:
        if request.headers.get("X-Requested-With") == "fetch":
            return {"ok": False, "error": "Both a status and a standard status are required."}, 400
        flash("Both a status and a standard status are required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO status_standard_names (name, created_at) VALUES (?, datetime('now'))",
            (canonical_name,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO status_aliases (alias, canonical_name) VALUES (?, ?)",
            (alias, canonical_name),
        )
        conn.execute("DELETE FROM status_unmapped WHERE raw_status = ?", (alias,))
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash(f'"{alias}" mapped to "{canonical_name}".', "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-alias/unmap", methods=["POST"])
def unmap_status_alias():
    alias = request.form.get("alias", "").strip().upper()
    conn = get_connection()
    try:
        row = conn.execute("SELECT canonical_name FROM status_aliases WHERE alias = ?", (alias,)).fetchone()
        conn.execute("DELETE FROM status_aliases WHERE alias = ?", (alias,))
        if row:
            # Assets that resolved through this alias were showing the
            # canonical status it pointed to - now that the mapping is
            # gone, drop those specific rows back to their own raw text so
            # Manage Assets shows (and can be filtered on) the same value
            # that's now sitting in Settings > Unmapped, instead of a
            # stale canonical status nothing points to anymore.
            conn.execute(
                "UPDATE asset_items SET status = UPPER(status_raw) "
                "WHERE status = ? AND UPPER(status_raw) = ?",
                (row["canonical_name"], alias),
            )
        record_unmapped_status(conn, alias)
        # If this alias (e.g. a seed default never actually used by any
        # import) has zero real assets behind it, there's nothing to
        # "move back to unmapped" - drop it right back out instead of
        # leaving a phantom entry Manage Assets can never show.
        prune_stale_unmapped(conn)
        conn.commit()
    finally:
        conn.close()
    flash(f'"{alias}" moved back to unmapped.', "success")
    return redirect(url_for("settings.index"))


# --- Standard model list + mapping (same standard-name/alias/unmapped
# pattern as devices above) --------------------------------------------------

@bp.route("/model-standard/add", methods=["POST"])
def add_standard_model():
    name = request.form.get("name", "").strip().upper()
    if not name:
        flash("A standard model is required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO model_standard_names (name, created_at) VALUES (?, datetime('now'))",
            (name,),
        )
        conn.execute("DELETE FROM model_unmapped WHERE raw_model = ?", (name,))
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard model "{name}" added.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-standard/rename", methods=["POST"])
def rename_standard_model():
    old_name = request.form.get("old_name", "").strip().upper()
    new_name = request.form.get("new_name", "").strip().upper()
    if not old_name or not new_name:
        flash("A new name is required.", "error")
        return redirect(url_for("settings.index"))

    message = _rename_or_merge_standard("model_standard_names", "model_aliases", old_name, new_name)
    flash(message, "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-standard/delete", methods=["POST"])
def delete_standard_model():
    name = request.form.get("name", "").strip().upper()
    conn = get_connection()
    try:
        aliases = conn.execute(
            "SELECT alias FROM model_aliases WHERE canonical_name = ?", (name,)
        ).fetchall()
        for row in aliases:
            record_unmapped_model(conn, row["alias"])
        conn.execute("DELETE FROM model_aliases WHERE canonical_name = ?", (name,))
        conn.execute("DELETE FROM model_standard_names WHERE name = ?", (name,))
        # See delete_standard_name()'s comment - same resync for assets whose
        # displayed model pointed at this now-deleted canonical name.
        conn.execute("UPDATE asset_items SET model_device = UPPER(model_device_raw) WHERE model_device = ?", (name,))
        if conn.execute("SELECT 1 FROM asset_items WHERE model_device = ? LIMIT 1", (name,)).fetchone():
            record_unmapped_model(conn, name)
        # See delete_standard_name()'s comment on prune_stale_unmapped().
        prune_stale_unmapped(conn)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard model "{name}" removed.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-alias/map", methods=["POST"])
def map_model_alias():
    """Used both by the drag-and-drop UI (fetch POST) and the fallback
    dropdown-and-button form for the same action: assign a raw model string
    to a standard model."""
    alias = request.form.get("alias", "").strip().upper()
    canonical_name = request.form.get("canonical_name", "").strip().upper()
    if not alias or not canonical_name:
        if request.headers.get("X-Requested-With") == "fetch":
            return {"ok": False, "error": "Both a model and a standard model are required."}, 400
        flash("Both a model and a standard model are required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO model_standard_names (name, created_at) VALUES (?, datetime('now'))",
            (canonical_name,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO model_aliases (alias, canonical_name) VALUES (?, ?)",
            (alias, canonical_name),
        )
        conn.execute("DELETE FROM model_unmapped WHERE raw_model = ?", (alias,))
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash(f'"{alias}" mapped to "{canonical_name}".', "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-alias/unmap", methods=["POST"])
def unmap_model_alias():
    alias = request.form.get("alias", "").strip().upper()
    conn = get_connection()
    try:
        row = conn.execute("SELECT canonical_name FROM model_aliases WHERE alias = ?", (alias,)).fetchone()
        conn.execute("DELETE FROM model_aliases WHERE alias = ?", (alias,))
        if row:
            # See unmap_status_alias()'s comment on resyncing affected rows.
            conn.execute(
                "UPDATE asset_items SET model_device = UPPER(model_device_raw) "
                "WHERE model_device = ? AND UPPER(model_device_raw) = ?",
                (row["canonical_name"], alias),
            )
        record_unmapped_model(conn, alias)
        # See unmap_status_alias()'s comment on prune_stale_unmapped().
        prune_stale_unmapped(conn)
        conn.commit()
    finally:
        conn.close()
    flash(f'"{alias}" moved back to unmapped.', "success")
    return redirect(url_for("settings.index"))


# --- Device mapping (unmapped raw name -> standard name) --------------------

@bp.route("/device-alias/map", methods=["POST"])
def map_device_alias():
    """Used both by the drag-and-drop UI (fetch POST) and the fallback
    dropdown-and-button form for the same action: assign a raw device name to
    a standard name."""
    alias = request.form.get("alias", "").strip().upper()
    canonical_name = request.form.get("canonical_name", "").strip().upper()
    if not alias or not canonical_name:
        if request.headers.get("X-Requested-With") == "fetch":
            return {"ok": False, "error": "Both a device name and a standard name are required."}, 400
        flash("Both a device name and a standard name are required.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO device_standard_names (name, created_at) VALUES (?, datetime('now'))",
            (canonical_name,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO device_aliases (alias, canonical_name) VALUES (?, ?)",
            (alias, canonical_name),
        )
        conn.execute("DELETE FROM device_unmapped WHERE raw_name = ?", (alias,))
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash(f'"{alias}" mapped to "{canonical_name}".', "success")
    return redirect(url_for("settings.index"))


@bp.route("/device-alias/unmap", methods=["POST"])
def unmap_device_alias():
    """Send a mapped alias back to the unmapped pool (e.g. it was assigned to
    the wrong standard name)."""
    alias = request.form.get("alias", "").strip().upper()
    conn = get_connection()
    try:
        row = conn.execute("SELECT canonical_name FROM device_aliases WHERE alias = ?", (alias,)).fetchone()
        conn.execute("DELETE FROM device_aliases WHERE alias = ?", (alias,))
        if row:
            # See unmap_status_alias()'s comment on resyncing affected rows.
            conn.execute(
                "UPDATE asset_items SET device_name = UPPER(device_name_raw) "
                "WHERE device_name = ? AND UPPER(device_name_raw) = ?",
                (row["canonical_name"], alias),
            )
        record_unmapped_device(conn, alias)
        # See unmap_status_alias()'s comment on prune_stale_unmapped().
        prune_stale_unmapped(conn)
        conn.commit()
    finally:
        conn.close()
    flash(f'"{alias}" moved back to unmapped.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/open-data-folder", methods=["POST"])
def open_data_folder():
    path = get_app_data_dir()
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - opening the app's own local data folder
        flash(f"Opened {path}", "info")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not open folder automatically: {exc}. Path: {path}", "error")
    return redirect(url_for("settings.index"))


# --- Data storage location ---------------------------------------------

# Moved as a unit so nothing already imported is lost when redirecting to a
# new folder; -wal/-shm are SQLite's WAL-mode sidecar files for app.db.
_DATA_ITEMS_TO_MOVE = ["app.db", "app.db-wal", "app.db-shm", "handovers", "exports", "uploads"]


@bp.route("/data-location", methods=["POST"])
def change_data_location():
    new_dir = request.form.get("new_data_dir", "").strip()
    if not new_dir:
        flash("Enter a folder path.", "error")
        return redirect(url_for("settings.index"))

    old_dir = get_app_data_dir()
    new_dir = os.path.abspath(new_dir)

    if os.path.normcase(new_dir) == os.path.normcase(old_dir):
        flash("That is already the current data location.", "info")
        return redirect(url_for("settings.index"))

    try:
        os.makedirs(new_dir, exist_ok=True)
        probe = os.path.join(new_dir, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as exc:
        flash(f"Cannot use that folder: {exc}", "error")
        return redirect(url_for("settings.index"))

    moved = []
    for name in _DATA_ITEMS_TO_MOVE:
        src = os.path.join(old_dir, name)
        dest = os.path.join(new_dir, name)
        if os.path.exists(src) and not os.path.exists(dest):
            shutil.move(src, dest)
            moved.append(name)

    set_app_data_dir(new_dir)
    # The new folder may never have held this app's data before (no app.db
    # to move in) - init_db is idempotent, so it's safe to call unconditionally
    # to make sure the schema/seed data exists there.
    init_db()

    detail = f"Moved: {', '.join(moved)}." if moved else "The new folder was empty - starting fresh there."
    flash(f"Data location changed to {new_dir}. {detail}", "success")
    return redirect(url_for("settings.index"))


@bp.route("/data-location/reset", methods=["POST"])
def reset_data_location():
    default_dir = get_default_app_data_dir()
    set_app_data_dir(default_dir)
    init_db()
    flash(f"Data location reset to default: {default_dir}", "success")
    return redirect(url_for("settings.index"))
