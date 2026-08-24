import os
import shutil
import sys
import time

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..auth import (
    SECURITY_QUESTIONS,
    SECURITY_QUESTIONS_BY_KEY,
    current_account,
    current_username,
    require_permission,
    set_security_question,
)
from ..cucm import get_cucm_config
from ..db import (
    get_connection,
    get_setting,
    get_setting_on,
    init_db,
    log_activity,
    prune_stale_unmapped,
    set_setting_on,
)
from ..exports import build_workbook, send_workbook
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

        account = current_account()
        current_question_key = None
        if account:
            row = conn.execute(
                "SELECT security_question_key FROM accounts WHERE id = ?", (account["id"],)
            ).fetchone()
            current_question_key = row["security_question_key"] if row else None
    finally:
        conn.close()

    return render_template(
        "settings.html",
        active_page="settings",
        security_questions=SECURITY_QUESTIONS,
        current_question_key=current_question_key,
        current_question_text=SECURITY_QUESTIONS_BY_KEY.get(current_question_key),
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
        reset_imported_data_options=RESET_IMPORTED_DATA_OPTIONS,
        cucm_config=get_cucm_config(),
    )


@bp.route("/account/security-question", methods=["POST"])
def set_security_question_route():
    """No @require_permission gate - any logged-in account (any role) sets
    up its own security question, same as it would set its own password."""
    account = current_account()
    question_key = request.form.get("question_key", "")
    answer = request.form.get("answer", "").strip()

    if question_key not in SECURITY_QUESTIONS_BY_KEY:
        flash("Pick one of the questions from the list.", "error")
    elif not answer:
        flash("Enter an answer to your chosen question.", "error")
    else:
        set_security_question(account["id"], question_key, answer)
        flash(
            "Security question set. Use \"Forgot password?\" on the login page if you're ever locked out - "
            "you'll be asked this same question.",
            "success",
        )
    return redirect(url_for("settings.index") + "#my-account")


GENERAL_SETTINGS_FIELDS = [
    ("ict_rep_name", True),
    ("ict_rep_id", True),
    ("asset_reports_folder", False),
    ("id_files_folder", False),
]


@bp.route("/general", methods=["POST"])
@require_permission("manage_settings")
def save_general():
    performed_by = current_username()
    conn = get_connection()
    try:
        for key, uppercase in GENERAL_SETTINGS_FIELDS:
            new_value = request.form.get(key, "").strip()
            if uppercase:
                new_value = new_value.upper()
            old_value = get_setting_on(conn, key, "")
            if new_value != old_value:
                log_activity(conn, "settings", "Updated setting", performed_by=performed_by,
                             target=key, field=key, old_value=old_value, new_value=new_value)
            set_setting_on(conn, key, new_value)
        conn.commit()
    finally:
        conn.close()
    flash("Settings saved.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/cucm", methods=["POST"])
@require_permission("manage_settings")
def save_cucm():
    """CUCM connection details for the CUCM Phone Scan page (app/cucm.py) -
    same key/value settings table as everything else here, just prefixed
    (cucm_*) so it can't collide with any other feature's settings."""
    performed_by = current_username()
    conn = get_connection()
    try:
        for key in ("ip", "axluser", "axlpassword", "riswsdl", "scan_prefixes"):
            new_value = request.form.get(key, "").strip()
            setting_key = f"cucm_{key}"
            old_value = get_setting_on(conn, setting_key, "")
            if new_value != old_value:
                # Never write the actual AXL password into the audit trail -
                # only that it changed, unlike every other setting here.
                if key == "axlpassword":
                    logged_old, logged_new = "(hidden)", "(changed)"
                else:
                    logged_old, logged_new = old_value, new_value
                log_activity(conn, "settings", "Updated setting", performed_by=performed_by,
                             target=setting_key, field=setting_key, old_value=logged_old, new_value=logged_new)
            set_setting_on(conn, setting_key, new_value)
        conn.commit()
    finally:
        conn.close()
    flash("CUCM connection settings saved.", "success")
    return redirect(url_for("settings.index") + "#cucm-settings")


@bp.route("/branch-alias/add", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Added branch alias", performed_by=current_username(),
                     target=alias_text, new_value=branch_no)
        conn.commit()
    finally:
        conn.close()
    msg = f'Branch alias "{alias_text}" saved.'
    if fixed_count:
        msg += f" Fixed {fixed_count} already-imported asset(s) that were sitting unresolved."
    flash(msg, "success")
    return redirect(url_for("settings.index"))


@bp.route("/branch-alias/delete", methods=["POST"])
@require_permission("manage_mappings")
def delete_branch_alias():
    alias = request.form.get("alias", "")
    conn = get_connection()
    try:
        conn.execute("DELETE FROM branch_aliases WHERE alias = ?", (alias,))
        log_activity(conn, "mapping", "Deleted branch alias", performed_by=current_username(), target=alias)
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
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Mapped branch hint", performed_by=current_username(),
                     target=raw_hint, new_value=branch_no)
        conn.commit()
    finally:
        conn.close()
    msg = f'"{raw_hint}" mapped - future imports will resolve it automatically.'
    if fixed_count:
        msg += f" Fixed {fixed_count} already-imported asset(s) that were sitting unresolved."
    flash(msg, "success")
    return redirect(url_for("settings.index"))


@bp.route("/branch-hint/dismiss", methods=["POST"])
@require_permission("manage_mappings")
def dismiss_branch_hint():
    """Remove a label from the unresolved pool without mapping it - e.g. it
    was a one-off typo that's already been fixed at the source and won't
    recur."""
    raw_hint = request.form.get("raw_hint", "").strip()
    conn = get_connection()
    try:
        conn.execute("DELETE FROM branch_unresolved WHERE raw_hint = ?", (raw_hint,))
        log_activity(conn, "mapping", "Dismissed branch hint", performed_by=current_username(), target=raw_hint)
        conn.commit()
    finally:
        conn.close()
    flash(f'"{raw_hint}" dismissed.', "success")
    return redirect(url_for("settings.index"))


def _rename_or_merge_standard(
    standard_table: str, alias_table: str, old_name: str, new_name: str, kind: str,
) -> str:
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
            log_activity(conn, "mapping", f"Merged standard {kind}", performed_by=current_username(),
                         old_value=old_name, new_value=new_name)
            conn.commit()
            return f'"{new_name}" already existed - merged "{old_name}" into it instead of renaming.'
        conn.execute(f"UPDATE {standard_table} SET name = ? WHERE name = ?", (new_name, old_name))
        conn.execute(f"UPDATE {alias_table} SET canonical_name = ? WHERE canonical_name = ?", (new_name, old_name))
        log_activity(conn, "mapping", f"Renamed standard {kind}", performed_by=current_username(),
                     old_value=old_name, new_value=new_name)
        conn.commit()
        return f'Renamed "{old_name}" to "{new_name}".'
    finally:
        conn.close()


# --- Device standard names (the editable canonical list) -------------------

@bp.route("/device-standard/add", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Added standard device", performed_by=current_username(), target=name)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard device name "{name}" added.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/device-standard/rename", methods=["POST"])
@require_permission("manage_mappings")
def rename_standard_name():
    old_name = request.form.get("old_name", "").strip().upper()
    new_name = request.form.get("new_name", "").strip().upper()
    if not old_name or not new_name:
        flash("A new name is required.", "error")
        return redirect(url_for("settings.index"))

    message = _rename_or_merge_standard(
        "device_standard_names", "device_aliases", old_name, new_name, "device"
    )
    flash(message, "success")
    return redirect(url_for("settings.index"))


@bp.route("/device-standard/delete", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Deleted standard device", performed_by=current_username(), target=name)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard name "{name}" removed.', "success")
    return redirect(url_for("settings.index"))


# --- Standard status list + mapping (same standard-name/alias/unmapped
# pattern as devices above) --------------------------------------------------

@bp.route("/status-standard/add", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Added standard status", performed_by=current_username(), target=name)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard status "{name}" added.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-standard/rename", methods=["POST"])
@require_permission("manage_mappings")
def rename_standard_status():
    old_name = request.form.get("old_name", "").strip().upper()
    new_name = request.form.get("new_name", "").strip().upper()
    if not old_name or not new_name:
        flash("A new name is required.", "error")
        return redirect(url_for("settings.index"))

    message = _rename_or_merge_standard(
        "status_standard_names", "status_aliases", old_name, new_name, "status"
    )
    flash(message, "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-standard/delete", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Deleted standard status", performed_by=current_username(), target=name)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard status "{name}" removed.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-alias/map", methods=["POST"])
@require_permission("manage_mappings")
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
        # See map_device_alias()'s comment on resyncing already-imported rows.
        conn.execute(
            "UPDATE asset_items SET status = ? "
            "WHERE status = UPPER(status_raw) AND UPPER(status_raw) = ?",
            (canonical_name, alias),
        )
        log_activity(conn, "mapping", "Mapped status alias", performed_by=current_username(),
                     target=alias, new_value=canonical_name)
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash(f'"{alias}" mapped to "{canonical_name}".', "success")
    return redirect(url_for("settings.index"))


@bp.route("/status-alias/unmap", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Unmapped status alias", performed_by=current_username(), target=alias)
        conn.commit()
    finally:
        conn.close()
    flash(f'"{alias}" moved back to unmapped.', "success")
    return redirect(url_for("settings.index"))


# --- Standard model list + mapping (same standard-name/alias/unmapped
# pattern as devices above) --------------------------------------------------

@bp.route("/model-standard/add", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Added standard model", performed_by=current_username(), target=name)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard model "{name}" added.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-standard/rename", methods=["POST"])
@require_permission("manage_mappings")
def rename_standard_model():
    old_name = request.form.get("old_name", "").strip().upper()
    new_name = request.form.get("new_name", "").strip().upper()
    if not old_name or not new_name:
        flash("A new name is required.", "error")
        return redirect(url_for("settings.index"))

    message = _rename_or_merge_standard(
        "model_standard_names", "model_aliases", old_name, new_name, "model"
    )
    flash(message, "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-standard/delete", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Deleted standard model", performed_by=current_username(), target=name)
        conn.commit()
    finally:
        conn.close()
    flash(f'Standard model "{name}" removed.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-alias/map", methods=["POST"])
@require_permission("manage_mappings")
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
        # See map_device_alias()'s comment on resyncing already-imported rows.
        conn.execute(
            "UPDATE asset_items SET model_device = ? "
            "WHERE model_device = UPPER(model_device_raw) AND UPPER(model_device_raw) = ?",
            (canonical_name, alias),
        )
        log_activity(conn, "mapping", "Mapped model alias", performed_by=current_username(),
                     target=alias, new_value=canonical_name)
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash(f'"{alias}" mapped to "{canonical_name}".', "success")
    return redirect(url_for("settings.index"))


@bp.route("/model-alias/unmap", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Unmapped model alias", performed_by=current_username(), target=alias)
        conn.commit()
    finally:
        conn.close()
    flash(f'"{alias}" moved back to unmapped.', "success")
    return redirect(url_for("settings.index"))


# --- Device mapping (unmapped raw name -> standard name) --------------------

@bp.route("/device-alias/map", methods=["POST"])
@require_permission("manage_mappings")
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
        # Mirror of the resync in delete/unmap: assets already sitting on
        # this raw value (device_name still equals its own raw text,
        # meaning nothing else has touched it since) now resolve through
        # this new mapping - without this they'd keep showing the old raw
        # text in Manage Assets until the branch happens to be re-imported.
        # The `device_name = UPPER(device_name_raw)` guard leaves alone any
        # row a user has since hand-edited to something else.
        conn.execute(
            "UPDATE asset_items SET device_name = ? "
            "WHERE device_name = UPPER(device_name_raw) AND UPPER(device_name_raw) = ?",
            (canonical_name, alias),
        )
        log_activity(conn, "mapping", "Mapped device alias", performed_by=current_username(),
                     target=alias, new_value=canonical_name)
        conn.commit()
    finally:
        conn.close()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True}
    flash(f'"{alias}" mapped to "{canonical_name}".', "success")
    return redirect(url_for("settings.index"))


@bp.route("/device-alias/unmap", methods=["POST"])
@require_permission("manage_mappings")
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
        log_activity(conn, "mapping", "Unmapped device alias", performed_by=current_username(), target=alias)
        conn.commit()
    finally:
        conn.close()
    flash(f'"{alias}" moved back to unmapped.', "success")
    return redirect(url_for("settings.index"))


@bp.route("/open-data-folder", methods=["POST"])
@require_permission("manage_settings")
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


def _move_with_retry(src: str, dest: str, attempts: int = 5, delay: float = 0.3) -> None:
    """shutil.move(), retried briefly on a Windows file-lock error. This app
    is served to several people on the LAN at once (see main.py) - another
    still-open request's SQLite connection can hold app.db locked for a
    moment, which turns into a PermissionError here rather than something a
    plain retry-free move can ride out. A short retry lets a momentary lock
    clear instead of failing the whole operation over it."""
    last_exc = None
    for attempt in range(attempts):
        try:
            shutil.move(src, dest)
            return
        except OSError as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    raise last_exc


@bp.route("/data-location", methods=["POST"])
@require_permission("manage_settings")
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

    # All-or-nothing: a move that fails partway (e.g. app.db locked by
    # another request) must never leave old_dir and new_dir both holding a
    # copy - the app keeps using old_dir either way (the pointer is only
    # written after every item below succeeds), so a stray, no-longer-
    # updated duplicate left behind in new_dir would just be a trap for
    # whoever finds it later and assumes it's current. On failure, every
    # item this call already moved gets moved straight back before the user
    # sees an error, and nothing is left half-migrated.
    moved = []
    current_item = None
    try:
        for name in _DATA_ITEMS_TO_MOVE:
            current_item = name
            src = os.path.join(old_dir, name)
            dest = os.path.join(new_dir, name)
            if os.path.exists(src) and not os.path.exists(dest):
                _move_with_retry(src, dest)
                moved.append(name)
        current_item = None
    except OSError as exc:
        for name in reversed(moved):
            dest = os.path.join(new_dir, name)
            src = os.path.join(old_dir, name)
            if os.path.exists(dest) and not os.path.exists(src):
                shutil.move(dest, src)
        # shutil.move() can copy-then-fail-to-delete when the source is
        # locked (its own os.rename -> copy+unlink fallback dying partway
        # through) - that leaves a stray, already-stale copy of whichever
        # single item was mid-move when this failed. Clean it up too; it's
        # not in `moved` since that move never actually completed.
        if current_item:
            stray = os.path.join(new_dir, current_item)
            src = os.path.join(old_dir, current_item)
            if os.path.exists(stray) and os.path.exists(src):
                if os.path.isdir(stray):
                    shutil.rmtree(stray, ignore_errors=True)
                else:
                    try:
                        os.remove(stray)
                    except OSError:
                        pass
        flash(
            f"Could not move the data folder: {exc}. This usually means the database is "
            "currently in use - make sure no one else is using the app right now and try "
            "again. Nothing was changed; your data is still at the original location.",
            "error",
        )
        return redirect(url_for("settings.index"))

    set_app_data_dir(new_dir)
    # The new folder may never have held this app's data before (no app.db
    # to move in) - init_db is idempotent, so it's safe to call unconditionally
    # to make sure the schema/seed data exists there.
    init_db()

    # Logged against the *new* location (get_connection() now resolves
    # there, post set_app_data_dir/init_db above) rather than the old one
    # being left behind - that's the database whose Activity Log page
    # anyone will actually still be looking at afterward.
    conn = get_connection()
    try:
        log_activity(conn, "settings", "Changed data storage location", performed_by=current_username(),
                     old_value=old_dir, new_value=new_dir)
        conn.commit()
    finally:
        conn.close()

    detail = f"Moved: {', '.join(moved)}." if moved else "The new folder was empty - starting fresh there."
    flash(f"Data location changed to {new_dir}. {detail}", "success")
    return redirect(url_for("settings.index"))


@bp.route("/data-location/reset", methods=["POST"])
@require_permission("manage_settings")
def reset_data_location():
    old_dir = get_app_data_dir()
    default_dir = get_default_app_data_dir()
    set_app_data_dir(default_dir)
    init_db()

    conn = get_connection()
    try:
        log_activity(conn, "settings", "Reset data storage location to default", performed_by=current_username(),
                     old_value=old_dir, new_value=default_dir)
        conn.commit()
    finally:
        conn.close()
    flash(f"Data location reset to default: {default_dir}", "success")
    return redirect(url_for("settings.index"))


# --- Reset Imported Data -----------------------------------------------
# All imported data lives in the same app.db as mappings/settings/accounts/
# handover_records, so there's no way to "start fresh" on imports alone by
# just deleting a file - this lets an Admin clear specific imported-data
# categories in place while leaving everything else (mappings, settings,
# accounts/roles, hand-over records) untouched. Each checkbox key here maps
# 1:1 to one block below in reset_imported_data().
RESET_IMPORTED_DATA_OPTIONS = {
    "asset_reports": "Asset Reports (asset items, import batches, diff reports, network check log)",
    "id_data": "Branch Codes & User IDs (branches, users)",
    "import_log": "Import History log",
    "unmapped": "Unmapped queues (device / status / model / branch)",
}


@bp.route("/reset-imported-data", methods=["POST"])
@require_permission("manage_settings")
def reset_imported_data():
    selected = [key for key in RESET_IMPORTED_DATA_OPTIONS if request.form.get(key)]
    if not selected:
        flash("Select at least one type of data to reset.", "error")
        return redirect(url_for("settings.index"))

    conn = get_connection()
    try:
        if "asset_reports" in selected:
            # asset_items.batch_id references import_batches(id) - delete
            # children before the parent or the FK check (PRAGMA
            # foreign_keys=ON, see get_connection) rejects it.
            conn.execute("DELETE FROM asset_items")
            conn.execute("DELETE FROM import_batches")
            conn.execute("DELETE FROM network_check_log")
            for row in conn.execute("SELECT file_path FROM diff_reports").fetchall():
                if row["file_path"] and os.path.exists(row["file_path"]):
                    try:
                        os.remove(row["file_path"])
                    except OSError:
                        pass
            conn.execute("DELETE FROM diff_reports")

        if "id_data" in selected:
            conn.execute("DELETE FROM branches")
            conn.execute("DELETE FROM users")

        if "import_log" in selected:
            conn.execute("DELETE FROM import_log")

        if "unmapped" in selected:
            conn.execute("DELETE FROM device_unmapped")
            conn.execute("DELETE FROM status_unmapped")
            conn.execute("DELETE FROM model_unmapped")
            conn.execute("DELETE FROM branch_unresolved")
        elif "asset_reports" in selected:
            # asset_items rows are gone - any device/status/model unmapped
            # entry that only existed to flag one of them is now stale.
            # branch_unresolved isn't derived from asset_items, so it's
            # untouched here (same as init_db()'s ordinary self-heal pass).
            prune_stale_unmapped(conn)

        log_activity(
            conn, "settings", "Reset imported data", performed_by=current_username(),
            new_value=", ".join(RESET_IMPORTED_DATA_OPTIONS[k] for k in selected),
        )
        conn.commit()
    finally:
        conn.close()

    flash(
        f"Reset: {', '.join(RESET_IMPORTED_DATA_OPTIONS[k] for k in selected)}. "
        "Mappings, settings, accounts, and hand-over records were left untouched.",
        "success",
    )
    return redirect(url_for("settings.index"))


# --- Activity Log ------------------------------------------------------
# General-purpose audit trail (see db.py's activity_log CREATE TABLE
# comment) - everything that doesn't already have its own dedicated log
# page (imports have Import History, Network Check has its own log).

ACTIVITY_LOG_CATEGORIES = [
    ("asset", "Manage Assets"),
    ("mapping", "Settings / Mapping"),
    ("settings", "Settings / General"),
    ("user_admin", "Users & Roles"),
]

ACTIVITY_LOG_EXPORT_COLUMNS = [
    ("Logged At", "logged_at"),
    ("Performed By", lambda r: r["performed_by"] or "-"),
    ("Category", "category"),
    ("Action", "action"),
    ("Target", "target"),
    ("Field", "field"),
    ("Old Value", "old_value"),
    ("New Value", "new_value"),
]


def _activity_log_rows(category: str):
    conn = get_connection()
    try:
        sql = "SELECT * FROM activity_log"
        params = []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY id DESC LIMIT 500"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@bp.route("/activity-log")
def activity_log():
    category = request.args.get("category", "").strip()
    rows = _activity_log_rows(category)
    return render_template(
        "activity_log.html",
        active_page="settings",
        rows=rows,
        categories=ACTIVITY_LOG_CATEGORIES,
        category_labels=dict(ACTIVITY_LOG_CATEGORIES),
        filters={"category": category},
    )


@bp.route("/activity-log/export")
def export_activity_log():
    category = request.args.get("category", "").strip()
    rows = _activity_log_rows(category)
    wb = build_workbook("Activity Log", ACTIVITY_LOG_EXPORT_COLUMNS, rows)
    return send_workbook(wb, "activity_log.xlsx")
