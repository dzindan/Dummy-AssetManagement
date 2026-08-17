import functools
import secrets
from datetime import timedelta

from flask import flash, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import PERMISSIONS, get_connection

# Unambiguous on-screen/on-paper: no 0/O or 1/I/L mixups.
_RECOVERY_KEY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def normalize_username(username: str) -> str:
    return (username or "").strip().upper()


def accounts_table_is_empty() -> bool:
    conn = get_connection()
    try:
        return conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None
    finally:
        conn.close()


def find_account_by_username(username: str):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM accounts WHERE username_norm = ?", (normalize_username(username),)
        ).fetchone()
    finally:
        conn.close()


def load_active_account(account_id):
    """None for a missing/deactivated account - checked fresh on every
    request (see register_auth's before_request), so deactivating someone
    takes effect on their very next request without needing a server-side
    session store."""
    if account_id is None:
        return None
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT a.id, a.username, a.role_id, r.name AS role_name "
            "FROM accounts a JOIN roles r ON r.id = a.role_id "
            "WHERE a.id = ? AND a.is_active = 1",
            (account_id,),
        ).fetchone()
    finally:
        conn.close()


def load_permissions_for_role(role_id) -> set:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT permission_key FROM role_permissions WHERE role_id = ?", (role_id,)
        ).fetchall()
        return {r["permission_key"] for r in rows}
    finally:
        conn.close()


def create_account(username: str, password: str, role_id: int) -> int:
    """Raises sqlite3.IntegrityError if the (normalized) username is already
    taken - callers turn that into a friendly flash message."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO accounts (username, username_norm, password_hash, role_id, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (username.strip(), normalize_username(username), hash_password(password), role_id),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def generate_recovery_key() -> str:
    """A one-time-use "forgot password" credential, shown to the account
    holder exactly once when (re)generated - only its hash is ever stored,
    same as a password. Formatted in dashed groups of 4 purely for
    readability when copying it down; the dashes carry no meaning and
    aren't required when it's typed back in (see verify_recovery_key)."""
    raw = "".join(secrets.choice(_RECOVERY_KEY_ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def set_recovery_key(account_id: int, key: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE accounts SET recovery_key_hash = ? WHERE id = ?",
            (hash_password(key), account_id),
        )
        conn.commit()
    finally:
        conn.close()


def clear_recovery_key(account_id: int) -> None:
    """Recovery keys are single-use: a successful /forgot-password reset
    clears it immediately, so a leaked-and-since-used key can't reset the
    password a second time. The account holder generates a fresh one from
    Settings after logging back in."""
    conn = get_connection()
    try:
        conn.execute("UPDATE accounts SET recovery_key_hash = NULL WHERE id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()


def verify_recovery_key(account, key: str) -> bool:
    key_hash = account["recovery_key_hash"] if account else None
    if not key_hash or not key:
        return False
    normalized = key.strip().upper().replace(" ", "")
    if "-" not in normalized and len(normalized) == 16:
        normalized = "-".join(normalized[i : i + 4] for i in range(0, 16, 4))
    return check_password_hash(key_hash, normalized)


def would_orphan_manage_users(exclude_account_id: int | None = None, exclude_role_id: int | None = None) -> bool:
    """True if, excluding the given account or role from consideration, zero
    active accounts would still hold manage_users. Checked before
    reassigning/deactivating an account's role or editing a custom role's
    permissions, so nobody can lock every admin out of user management."""
    conn = get_connection()
    try:
        query = (
            "SELECT COUNT(*) AS c FROM accounts a "
            "JOIN role_permissions rp ON rp.role_id = a.role_id AND rp.permission_key = 'manage_users' "
            "WHERE a.is_active = 1"
        )
        params = []
        if exclude_account_id is not None:
            query += " AND a.id != ?"
            params.append(exclude_account_id)
        if exclude_role_id is not None:
            query += " AND a.role_id != ?"
            params.append(exclude_role_id)
        return conn.execute(query, params).fetchone()["c"] == 0
    finally:
        conn.close()


def login_account(account_id: int) -> None:
    session.clear()
    session["account_id"] = account_id
    session.permanent = True
    conn = get_connection()
    try:
        conn.execute("UPDATE accounts SET last_login_at = datetime('now') WHERE id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()


def logout_account() -> None:
    session.clear()


def current_account():
    return getattr(g, "current_account", None)


def current_permissions() -> set:
    return getattr(g, "current_permissions", set())


def require_permission(permission: str):
    """Route decorator - stack under @bp.route(...). Assumes register_auth's
    before_request already ran (true for every request reaching a view),
    so current_permissions() reflects the logged-in account's role fresh
    from the DB, not a value cached in the session cookie."""
    assert permission in PERMISSIONS, f"unknown permission key: {permission!r}"

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapped(*args, **kwargs):
            if permission not in current_permissions():
                if request.is_json or request.headers.get("X-Requested-With") == "fetch":
                    return jsonify({"error": "You do not have permission to do that."}), 403
                flash("You do not have permission to do that. Contact an administrator if you need access.", "error")
                return redirect(request.referrer or url_for("dashboard.index"))
            return view_func(*args, **kwargs)

        # Introspectable by tests/test_auth.py's route-coverage guard, which
        # walks app.url_map and asserts every mutating route is either
        # gated (this attribute present) or on an explicit allowlist.
        wrapped.required_permission = permission
        return wrapped

    return decorator


# Endpoints reachable with no session at all - the login/setup flow itself,
# and static assets (CSS/JS/favicon, needed to even render the login page).
_EXEMPT_ENDPOINTS = {"auth.login", "auth.logout", "auth.setup", "auth.forgot_password", "static"}


def register_auth(app) -> None:
    """Wires the global login gate + current-account template context into
    the app. Call once from create_app(), after every blueprint (including
    auth's own) is registered."""
    app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(hours=10))

    @app.before_request
    def _enforce_login():
        if request.endpoint is None or request.endpoint in _EXEMPT_ENDPOINTS:
            return None
        if accounts_table_is_empty():
            return redirect(url_for("auth.setup"))
        account = load_active_account(session.get("account_id"))
        if account is None:
            session.pop("account_id", None)
            return redirect(url_for("auth.login", next=request.full_path))
        g.current_account = account
        g.current_permissions = load_permissions_for_role(account["role_id"])
        return None

    @app.context_processor
    def inject_current_account():
        return {"current_account": current_account(), "current_permissions": current_permissions()}
