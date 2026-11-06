import sqlite3

from flask import Blueprint, flash, redirect, render_template, request, url_for

from ..auth import (
    AD_PASSWORD_HASH,
    AD_SETTING_KEYS,
    ad_account_name,
    ad_settings,
    ad_sid_of,
    ad_sign_in_on,
    create_account,
    create_ad_account,
    current_account,
    current_permissions,
    current_username,
    hash_password,
    is_last_local_admin,
    normalize_username,
    refresh_session_password,
    would_orphan_manage_users,
)
from ..db import PERMISSIONS, get_connection, log_activity

bp = Blueprint("user_admin", __name__, url_prefix="/settings/users")


@bp.before_request
def _require_manage_users():
    """Every route in this blueprint needs the same one permission, so a
    single blueprint-local hook covers it instead of decorating each view -
    see app/auth.py's require_permission for the equivalent per-route
    decorator used everywhere else in the app."""
    if "manage_users" not in current_permissions():
        flash("You do not have permission to access this page. Contact an administrator if you need access.", "error")
        return redirect(url_for("dashboard.index"))
    return None


@bp.route("/")
def index():
    conn = get_connection()
    try:
        accounts = conn.execute(
            "SELECT a.id, a.username, a.is_active, a.last_login_at, a.created_at, a.auth_source, a.ad_listed, "
            "r.id AS role_id, r.name AS role_name "
            "FROM accounts a JOIN roles r ON r.id = a.role_id "
            "ORDER BY a.username"
        ).fetchall()
        roles = conn.execute("SELECT id, name, is_builtin FROM roles ORDER BY is_builtin DESC, name").fetchall()
        role_permissions = {}
        for role in roles:
            rows = conn.execute(
                "SELECT permission_key FROM role_permissions WHERE role_id = ?", (role["id"],)
            ).fetchall()
            role_permissions[role["id"]] = {r["permission_key"] for r in rows}
    finally:
        conn.close()

    from .. import winauth

    return render_template(
        "user_admin.html",
        active_page="settings",
        ad=ad_settings(),
        ad_available=winauth.available(),
        accounts=accounts,
        roles=roles,
        role_permissions=role_permissions,
        permissions=PERMISSIONS,
    )


def _checked_ad_name(text: str) -> tuple[str | None, str | None, str]:
    """-> (DOMAIN\\USER, SID, a note); name None after flashing the problem."""
    from ..winauth import AuthError

    name = ad_account_name(text)
    if not name:
        flash("Enter the AD account as DOMAIN\\username.", "error")
        return None, None, ""
    try:
        sid, note = ad_sid_of(name)
    except AuthError as e:
        flash(str(e), "error")
        return None, None, ""
    if not ad_sign_in_on():
        note = (note + " AD sign-in is off: turn it on above before this account can sign in.").strip()
    return name, sid, note


def _add_ad_account(username: str, role_id: str):
    if not role_id:
        flash("Please choose a role.", "error")
        return redirect(url_for("user_admin.index"))
    name, sid, note = _checked_ad_name(username)
    if not name:
        return redirect(url_for("user_admin.index"))
    try:
        create_ad_account(name, int(role_id), sid)
    except sqlite3.IntegrityError:
        flash(f'Username "{name}" is already taken.', "error")
        return redirect(url_for("user_admin.index"))
    conn = get_connection()
    try:
        role = conn.execute("SELECT name FROM roles WHERE id = ?", (role_id,)).fetchone()
        log_activity(conn, "user_admin", "Created AD account", performed_by=current_username(),
                     target=name, new_value=role["name"] if role else "")
        conn.commit()
    finally:
        conn.close()
    flash(f'AD account "{name}" added.', "success")
    if note:
        flash(note, "info")
    return redirect(url_for("user_admin.index"))


@bp.route("/accounts/add", methods=["POST"])
def add_account():
    if request.form.get("sign_in") == "ad":
        return _add_ad_account(request.form.get("username", ""), request.form.get("role_id", ""))
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role_id = request.form.get("role_id", "")
    if not username or not password or not role_id:
        flash("Please enter a username, password, and role.", "error")
        return redirect(url_for("user_admin.index"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("user_admin.index"))
    try:
        create_account(username, password, int(role_id))
    except sqlite3.IntegrityError:
        flash(f'Username "{username}" is already taken.', "error")
        return redirect(url_for("user_admin.index"))
    conn = get_connection()
    try:
        role = conn.execute("SELECT name FROM roles WHERE id = ?", (role_id,)).fetchone()
        log_activity(conn, "user_admin", "Created account", performed_by=current_username(),
                     target=username, new_value=role["name"] if role else "")
        conn.commit()
    finally:
        conn.close()
    flash(f'Account "{username}" created.', "success")
    return redirect(url_for("user_admin.index"))


@bp.route("/accounts/<int:account_id>/role", methods=["POST"])
def set_account_role(account_id):
    role_id = int(request.form.get("role_id", "0") or "0")
    conn = get_connection()
    try:
        if would_orphan_manage_users(exclude_account_id=account_id) and not _role_has_manage_users(conn, role_id):
            flash("Cannot change role - this is the last account with Manage Users permission.", "error")
            return redirect(url_for("user_admin.index"))
        account = conn.execute(
            "SELECT a.username, r.name AS role_name FROM accounts a JOIN roles r ON r.id = a.role_id WHERE a.id = ?",
            (account_id,),
        ).fetchone()
        new_role = conn.execute("SELECT name FROM roles WHERE id = ?", (role_id,)).fetchone()
        conn.execute("UPDATE accounts SET role_id = ? WHERE id = ?", (role_id, account_id))
        if account:
            log_activity(conn, "user_admin", "Changed account role", performed_by=current_username(),
                         target=account["username"], old_value=account["role_name"],
                         new_value=new_role["name"] if new_role else "")
        conn.commit()
    finally:
        conn.close()
    flash("Role updated.", "success")
    return redirect(url_for("user_admin.index"))


@bp.route("/accounts/<int:account_id>/toggle-active", methods=["POST"])
def toggle_active(account_id):
    conn = get_connection()
    try:
        account = conn.execute("SELECT username, is_active FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            flash("Account not found.", "error")
            return redirect(url_for("user_admin.index"))
        if account["is_active"] and would_orphan_manage_users(exclude_account_id=account_id):
            flash("Cannot deactivate - this is the last account with Manage Users permission.", "error")
            return redirect(url_for("user_admin.index"))
        new_state = 0 if account["is_active"] else 1
        conn.execute("UPDATE accounts SET is_active = ? WHERE id = ?", (new_state, account_id))
        log_activity(conn, "user_admin", "Activated account" if new_state else "Deactivated account",
                     performed_by=current_username(), target=account["username"])
        conn.commit()
    finally:
        conn.close()
    flash("Account status updated.", "success")
    return redirect(url_for("user_admin.index"))


@bp.route("/ad", methods=["POST"])
def save_ad():
    """AD sign-in: on / off, the domain, the AD group that may use the app,
    the role a new AD account starts with."""
    from .. import winauth
    from ..db import set_setting

    enabled = request.form.get("ad_enabled") == "1"
    group = request.form.get("ad_group", "").strip()
    domain = request.form.get("ad_domain", "").strip().upper()
    role = request.form.get("ad_default_role", "").strip()
    if enabled:
        if not winauth.available():
            flash("AD sign-in needs Windows (pywin32) - not available here.", "error")
            return redirect(url_for("user_admin.index"))
        try:
            if group:
                winauth.group_sid(group)
        except winauth.AuthError as e:
            flash(str(e), "error")
            return redirect(url_for("user_admin.index"))
    conn = get_connection()
    try:
        values = {"ad_enabled": "1" if enabled else "0", "ad_domain": domain, "ad_group": group,
                  "ad_default_role": role}
        for key in AD_SETTING_KEYS:
            set_setting(key, values[key])
        log_activity(conn, "user_admin", "AD sign-in settings saved", performed_by=current_username(),
                     target=f"{'on' if enabled else 'off'}, group {group or '-'}, domain {domain or '-'}")
        conn.commit()
    finally:
        conn.close()
    if not enabled:
        done = " Only the accounts of this app can sign in."
    elif group:
        done = " Members of that group, and the AD accounts in the table, can sign in with Windows."
    else:
        done = " The AD accounts in the table can sign in with Windows."
    flash("AD sign-in saved." + done, "success")
    return redirect(url_for("user_admin.index"))


@bp.route("/accounts/<int:account_id>/sign-in", methods=["POST"])
def set_sign_in(account_id):
    """One account's way of signing in: its AD account, or an app password.
    Its open sessions end (auth.session_account watches the password hash)."""
    back = redirect(url_for("user_admin.index"))
    conn = get_connection()
    try:
        account = conn.execute("SELECT username, auth_source FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            flash("Account not found.", "error")
            return back
        if request.form.get("sign_in") == "ad":
            if is_last_local_admin(account_id) and account["auth_source"] != "ad":
                flash("Cannot switch - keep one active Admin with an app password, the way in when AD is down.",
                      "error")
                return back
            name, sid, note = _checked_ad_name(request.form.get("ad_name", ""))
            if not name:
                return back
            values, event = (name, normalize_username(name), AD_PASSWORD_HASH, "ad", sid, 1), "AD"
        else:
            password = request.form.get("password", "")
            if len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
                return back
            note = ""
            values, event = (account["username"], normalize_username(account["username"]), hash_password(password),
                             "local", None, 0), "app password"
        try:
            conn.execute("UPDATE accounts SET username = ?, username_norm = ?, password_hash = ?, auth_source = ?, "
                         "ad_sid = ?, ad_listed = ? WHERE id = ?", (*values, account_id))
        except sqlite3.IntegrityError:
            flash(f'Username "{values[0]}" is already taken.', "error")
            return back
        log_activity(conn, "user_admin", "Changed account sign-in", performed_by=current_username(),
                     target=account["username"], old_value=account["auth_source"], new_value=f"{event}: {values[0]}")
        conn.commit()
    finally:
        conn.close()
    flash(f'"{values[0]}" now signs in with {event}.', "success")
    if note:
        flash(note, "info")
    return back


@bp.route("/accounts/<int:account_id>/delete", methods=["POST"])
def delete_account(account_id):
    """Remove an account so the name can be made again. What it did stays in
    the logs under its name. An open session ends at its next page."""
    back = redirect(url_for("user_admin.index"))
    me = current_account()
    if me and me["id"] == account_id:
        flash("You can't delete the account you are signed in with.", "error")
        return back
    conn = get_connection()
    try:
        account = conn.execute(
            "SELECT a.username, a.auth_source, a.is_active, a.ad_listed, r.id AS role_id, r.name AS role_name "
            "FROM accounts a JOIN roles r ON r.id = a.role_id WHERE a.id = ?", (account_id,)).fetchone()
        if not account:
            flash("Account not found.", "error")
            return back
        manager = account["is_active"] and _role_has_manage_users(conn, account["role_id"])
        if manager and would_orphan_manage_users(exclude_account_id=account_id):
            flash("Cannot delete - this is the last account with Manage Users permission.", "error")
            return back
        if manager and account["auth_source"] == "local" and is_last_local_admin(account_id):
            flash("Cannot delete - keep one active Admin with an app password, the way in when AD is down.", "error")
            return back
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        log_activity(conn, "user_admin", "Deleted account", performed_by=current_username(),
                     target=account["username"], old_value=account["role_name"])
        conn.commit()
    finally:
        conn.close()
    flash(f'Account "{account["username"]}" deleted. Its name can be used again.', "success")
    if account["auth_source"] == "ad" and not account["ad_listed"]:
        flash("It came from the AD group: while the person stays in the group, their next sign-in makes it again.",
              "info")
    return back


@bp.route("/accounts/<int:account_id>/reset-password", methods=["POST"])
def reset_password(account_id):
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("user_admin.index"))
    conn = get_connection()
    try:
        account = conn.execute("SELECT username, auth_source FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account and account["auth_source"] == "ad":
            flash("An AD account's password is its Windows password - reset it in AD.", "error")
            return redirect(url_for("user_admin.index"))
        conn.execute(
            "UPDATE accounts SET password_hash = ? WHERE id = ?", (hash_password(password), account_id)
        )
        log_activity(conn, "user_admin", "Reset account password", performed_by=current_username(),
                     target=account["username"] if account else f"account #{account_id}")
        conn.commit()
    finally:
        conn.close()
    # that account's other sessions end (auth.session_account); one's own stays signed in
    if current_account() and current_account()["id"] == account_id:
        refresh_session_password()
    flash("Password reset.", "success")
    return redirect(url_for("user_admin.index"))


def _role_has_manage_users(conn, role_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM role_permissions WHERE role_id = ? AND permission_key = 'manage_users'", (role_id,)
        ).fetchone()
        is not None
    )


@bp.route("/roles/add", methods=["POST"])
def add_role():
    name = request.form.get("name", "").strip()
    perm_keys = [k for k in request.form.getlist("permissions") if k in PERMISSIONS]
    if not name:
        flash("Role name is required.", "error")
        return redirect(url_for("user_admin.index"))
    conn = get_connection()
    try:
        try:
            cursor = conn.execute(
                "INSERT INTO roles (name, is_builtin, created_at) VALUES (?, 0, datetime('now'))", (name,)
            )
        except sqlite3.IntegrityError:
            flash(f'Role "{name}" already exists.', "error")
            return redirect(url_for("user_admin.index"))
        role_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO role_permissions (role_id, permission_key) VALUES (?, ?)",
            [(role_id, key) for key in perm_keys],
        )
        log_activity(conn, "user_admin", "Created role", performed_by=current_username(),
                     target=name, new_value=", ".join(sorted(perm_keys)))
        conn.commit()
    finally:
        conn.close()
    flash(f'Role "{name}" created.', "success")
    return redirect(url_for("user_admin.index"))


@bp.route("/roles/<int:role_id>/edit", methods=["POST"])
def edit_role(role_id):
    perm_keys = set(k for k in request.form.getlist("permissions") if k in PERMISSIONS)
    conn = get_connection()
    try:
        role = conn.execute("SELECT is_builtin, name FROM roles WHERE id = ?", (role_id,)).fetchone()
        if not role:
            flash("Role not found.", "error")
            return redirect(url_for("user_admin.index"))
        if role["is_builtin"]:
            flash("Cannot edit permissions of a built-in role (Viewer/Editor/Admin).", "error")
            return redirect(url_for("user_admin.index"))
        if "manage_users" not in perm_keys and would_orphan_manage_users(exclude_role_id=role_id):
            flash("Cannot remove Manage Users permission - this is its last source.", "error")
            return redirect(url_for("user_admin.index"))
        old_perms = {
            r["permission_key"] for r in conn.execute(
                "SELECT permission_key FROM role_permissions WHERE role_id = ?", (role_id,)
            ).fetchall()
        }
        conn.execute("DELETE FROM role_permissions WHERE role_id = ?", (role_id,))
        conn.executemany(
            "INSERT INTO role_permissions (role_id, permission_key) VALUES (?, ?)",
            [(role_id, key) for key in perm_keys],
        )
        log_activity(conn, "user_admin", "Updated role permissions", performed_by=current_username(),
                     target=role["name"], old_value=", ".join(sorted(old_perms)),
                     new_value=", ".join(sorted(perm_keys)))
        conn.commit()
    finally:
        conn.close()
    flash("Role permissions updated.", "success")
    return redirect(url_for("user_admin.index"))


@bp.route("/roles/<int:role_id>/delete", methods=["POST"])
def delete_role(role_id):
    conn = get_connection()
    try:
        role = conn.execute("SELECT is_builtin, name FROM roles WHERE id = ?", (role_id,)).fetchone()
        if not role:
            flash("Role not found.", "error")
            return redirect(url_for("user_admin.index"))
        if role["is_builtin"]:
            flash("Cannot delete a built-in role (Viewer/Editor/Admin).", "error")
            return redirect(url_for("user_admin.index"))
        try:
            conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
            log_activity(conn, "user_admin", "Deleted role", performed_by=current_username(), target=role["name"])
            conn.commit()
        except sqlite3.IntegrityError:
            flash(f'Role "{role["name"]}" is still assigned to at least 1 account - reassign them first.', "error")
            return redirect(url_for("user_admin.index"))
    finally:
        conn.close()
    flash("Role deleted.", "success")
    return redirect(url_for("user_admin.index"))
