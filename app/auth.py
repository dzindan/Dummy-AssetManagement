import functools
import hashlib
import re
import sqlite3
from datetime import timedelta

from flask import flash, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import PERMISSIONS, get_connection

# Deliberately silly - these exist so the answer is something the account
# holder will actually remember (and enjoy typing), not a serious "mother's
# maiden name" style question. Keyed (not just indexed) so stored answers
# stay tied to the right question even if this list is ever reordered -
# only ever append here, never renumber an existing key.
SECURITY_QUESTIONS = [
    ("useless_talent", "What's your most useless talent?"),
    ("weird_nickname", "What's the weirdest nickname anyone's ever called you?"),
    ("wifi_name", "If you were a Wi-Fi network, what would your name be?"),
    ("food_fight", "What food would you fight someone over?"),
    ("lame_excuse", "What's the lamest excuse you've used to skip a meeting?"),
]
SECURITY_QUESTIONS_BY_KEY = dict(SECURITY_QUESTIONS)


def hash_password(password: str) -> str:
    return generate_password_hash(password)


AD_PASSWORD_HASH = "!ad"  # an AD account: no password of its own, Windows checks it


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash or password_hash.startswith("!"):
        return False  # an AD account never signs in with an app password
    return check_password_hash(password_hash, password)


# --- AD sign-in (winauth.py) --------------------------------------------------------

AD_SETTING_KEYS = ("ad_enabled", "ad_domain", "ad_group", "ad_default_role", "ad_open")


def ad_settings() -> dict:
    """Users & Roles > AD sign-in. On by default (2026-11-09): once there is an
    Admin, whoever opens the app is signed in with their Windows session, and
    "open" makes their account on that first sign-in with the default role
    (Viewer) - an Admin gives more. Off only when an administrator turns it off."""
    from .db import get_setting
    from .winauth import computer_domain

    return {"enabled": get_setting("ad_enabled", "1") == "1",
            "domain": (get_setting("ad_domain", "") or "").strip() or computer_domain(),
            "group": (get_setting("ad_group", "") or "").strip(),
            "default_role": (get_setting("ad_default_role", "") or "").strip(),
            "open": get_setting("ad_open", "1") == "1"}


def ad_sign_in_on() -> bool:
    from .winauth import available

    return ad_settings()["enabled"] and available()


def apply_server_ad(config: dict) -> str:
    """The server's first start with init-server's --ad-group: AD sign-in on.
    Only while nobody has set it: Users & Roles > AD sign-in decides after
    that. -> a line for the log, '' when there was nothing to do."""
    from .db import get_setting, set_setting
    from .winauth import AuthError, available, group_sid

    group = config.get("ad_group", "")
    if not group or get_setting("ad_enabled", None) is not None:
        return ""
    if not available():
        return "AD sign-in stays off: it needs Windows (pywin32)"
    try:
        group_sid(group)
    except AuthError as e:
        return f"AD sign-in stays off: {e}"
    set_setting("ad_group", group)
    set_setting("ad_default_role", config.get("ad_default_role", ""))
    set_setting("ad_enabled", "1")
    return f"AD sign-in on for members of {group}"


class SignInRefused(Exception):
    pass


def sign_in_ad(identity) -> int:
    """An AD user Windows vouched for -> their account id. An account an
    administrator set to AD may sign in as it is. Anyone else must be in the
    access group when there is one, or - with no group - "open" must be on.
    Their account is made on the first sign-in, with the default role.
    Refused (SignInRefused) otherwise, or when their account here is
    deactivated."""
    from . import winauth
    from .db import log_activity

    s = ad_settings()
    if not ad_sign_in_on():
        raise SignInRefused("AD sign-in is off - use an account of this app.")
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM accounts WHERE ad_sid = ?", (identity.sid,)).fetchone() or \
            conn.execute("SELECT * FROM accounts WHERE username_norm = ? AND auth_source = 'ad' AND ad_sid IS NULL",
                         (normalize_username(identity.username),)).fetchone()
        listed = row is not None and row["ad_listed"]
        if not listed and not s["group"] and not s["open"]:
            raise SignInRefused(f"{identity.username} has no account here - ask an administrator "
                                "to add it in Users & Roles.")
        if not listed and s["group"]:
            try:
                allowed = winauth.group_sid(s["group"])
            except winauth.AuthError as e:
                raise SignInRefused(f"{e} - check Settings > Users & Roles > AD sign-in.") from None
            if allowed not in identity.group_sids:
                raise SignInRefused(f"{identity.username} isn't a member of {s['group']} - ask an administrator "
                                    "to add you to that AD group, or to add your account in Users & Roles.")
        # By name only when no SID is kept yet: an AD name given to someone
        # else later (the old account deleted in AD) is another SID - that
        # person must not take over the old account and its role.
        if row is None:
            if conn.execute("SELECT 1 FROM accounts WHERE username_norm = ?",
                            (normalize_username(identity.username),)).fetchone():
                raise SignInRefused(f"An account of this app is already named {identity.username} - "
                                    "rename it in Users & Roles first.")
            role = None
            if s["default_role"]:
                role = conn.execute("SELECT id FROM roles WHERE id = ? OR name = ?",
                                    (s["default_role"], s["default_role"])).fetchone()
            role = role or conn.execute("SELECT id FROM roles WHERE name = 'Viewer'").fetchone()
            cursor = conn.execute(
                "INSERT INTO accounts (username, username_norm, password_hash, role_id, is_active, created_at, "
                "auth_source, ad_sid) VALUES (?, ?, ?, ?, 1, datetime('now'), 'ad', ?)",
                (identity.username, normalize_username(identity.username), AD_PASSWORD_HASH, role["id"],
                 identity.sid))
            log_activity(conn, "user_admin", "AD account created on its first sign-in",
                         performed_by=identity.username, target=identity.username)
            conn.commit()
            try:  # said after login_account (it starts a fresh session) - see tell_new_ad_account
                from flask import g
                g.new_ad_account = (identity.username,
                                    conn.execute("SELECT name FROM roles WHERE id = ?", (role["id"],)).fetchone()["name"])
            except RuntimeError:  # outside a request
                pass
            return cursor.lastrowid
        if not row["is_active"]:
            raise SignInRefused("Your account is deactivated in this app - ask an administrator.")
        if row["username"] != identity.username or row["ad_sid"] != identity.sid:  # renamed in AD
            try:
                conn.execute("UPDATE accounts SET username = ?, username_norm = ?, ad_sid = ? WHERE id = ?",
                             (identity.username, normalize_username(identity.username), identity.sid, row["id"]))
                conn.commit()
            except sqlite3.IntegrityError:
                raise SignInRefused(f"Your AD name is now {identity.username}, but an account of this app "
                                    "already has that name - ask an administrator to rename it.") from None
        return row["id"]
    finally:
        conn.close()


def tell_new_ad_account() -> None:
    """After login_account: tell someone whose account sign_in_ad just made
    which role they got, and who gives more."""
    from flask import flash, g

    made = g.pop("new_ad_account", None)
    if made:
        flash(f"Signed in with Windows as {made[0]}. Your account was made with the {made[1]} role - "
              "ask an administrator if you need other access.", "success")


def safe_next(next_url: str, fallback: str) -> str:
    """A ?next= / "next" form value to redirect to - only ever a path of
    this app (it's user-supplied: a link can carry any value), else fallback.
    Browsers read "/\\evil.com" (and "/<tab>/evil.com", tabs and newlines
    removed) as "//evil.com" - another site; so no backslash or control
    character either."""
    if (next_url and next_url.startswith("/") and not next_url.startswith("//")
            and "\\" not in next_url and not any(ord(c) < 32 or ord(c) == 127 for c in next_url)):
        return next_url
    return fallback


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
            "SELECT a.id, a.username, a.role_id, a.password_hash, r.name AS role_name "
            "FROM accounts a JOIN roles r ON r.id = a.role_id "
            "WHERE a.id = ? AND a.is_active = 1",
            (account_id,),
        ).fetchone()
    finally:
        conn.close()


def _password_mark(password_hash: str) -> str:
    """Kept in the session at sign-in: a new password (reset by an admin,
    or via Forgot password) changes it, and every session signed in with
    the old one ends on its next request."""
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


def session_account():
    """The signed-in account of this request's session, or None (no
    session, the account deactivated, or its password changed since)."""
    account = load_active_account(session.get("account_id"))
    if account is None or session.get("pwv") != _password_mark(account["password_hash"]):
        return None
    return account


def refresh_session_password() -> None:
    """After someone resets their *own* password: keep this session."""
    account = load_active_account(session.get("account_id"))
    if account is not None:
        session["pwv"] = _password_mark(account["password_hash"])


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


def _normalize_answer(answer: str) -> str:
    """Case/whitespace-insensitive so "Pizza" and " pizza " count as the
    same answer - the point is remembering *what* you answered, not
    reproducing its exact capitalization months later."""
    return (answer or "").strip().lower()


def set_security_question(account_id: int, question_key: str, answer: str) -> None:
    if question_key not in SECURITY_QUESTIONS_BY_KEY:
        raise ValueError(f"unknown security question key: {question_key!r}")
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE accounts SET security_question_key = ?, security_answer_hash = ? WHERE id = ?",
            (question_key, hash_password(_normalize_answer(answer)), account_id),
        )
        conn.commit()
    finally:
        conn.close()


def verify_security_answer(account, answer: str) -> bool:
    answer_hash = account["security_answer_hash"] if account else None
    if not answer_hash or not answer:
        return False
    return check_password_hash(answer_hash, _normalize_answer(answer))


def ad_account_name(text: str) -> str | None:
    """What an administrator typed -> DOMAIN\\USER as Windows gives it at
    sign-in, None when it can't be one. 'nva' takes the default domain."""
    from .winauth import split_name

    domain, user = split_name(text, ad_settings()["domain"])
    if not domain or not user or re.search(r'[\\/@:*?"<>|\s]', domain + user):
        return None
    return f"{domain}\\{user}".upper()


def ad_sid_of(name: str) -> tuple[str | None, str]:
    """-> (the account's SID, a note when it couldn't be looked up). Without
    an answer from AD the account is still set: the name ties it to the person
    at their first sign-in. AD saying "no such account" is an AuthError."""
    from . import winauth

    if not winauth.on_domain():
        return None, "This computer isn't on a domain, so the name wasn't checked with AD."
    return winauth.user_sid(name), ""


def create_ad_account(name: str, role_id: int, sid: str | None) -> int:
    """Raises sqlite3.IntegrityError if the name is already taken."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO accounts (username, username_norm, password_hash, role_id, is_active, created_at, "
            "auth_source, ad_sid, ad_listed) VALUES (?, ?, ?, ?, 1, datetime('now'), 'ad', ?, 1)",
            (name, normalize_username(name), AD_PASSWORD_HASH, role_id, sid))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def is_last_local_admin(account_id: int) -> bool:
    """True if no other active account with an app password holds
    manage_users: with AD down nobody could sign in to manage the app."""
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM accounts a "
            "JOIN role_permissions rp ON rp.role_id = a.role_id AND rp.permission_key = 'manage_users' "
            "WHERE a.is_active = 1 AND a.auth_source = 'local' AND a.id != ?", (account_id,)).fetchone()["c"] == 0
    finally:
        conn.close()


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
        row = conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
        session["pwv"] = _password_mark(row["password_hash"] if row else "")
        conn.execute("UPDATE accounts SET last_login_at = datetime('now') WHERE id = ?", (account_id,))
        conn.commit()
    finally:
        conn.close()


def logout_account() -> None:
    session.clear()


def current_account():
    return getattr(g, "current_account", None)


def current_username() -> str:
    """The logged-in account's username, "" if none - the one piece of
    `current_account()` every activity-log call site actually needs (see
    routes/asset_edit.py, import_data.py, settings.py, user_admin.py),
    without each of them re-deriving it from current_account() themselves."""
    account = current_account()
    return account["username"] if account else ""


def current_permissions() -> set:
    return getattr(g, "current_permissions", set())


def _wants_json_response() -> bool:
    """True when this request is a fetch()/AJAX call expecting a JSON
    response rather than a full-page HTML redirect - shared by
    require_permission's 403 branch and _enforce_login's 401 branch below,
    so the two never drift on what counts as "a fetch call"."""
    return request.is_json or request.headers.get("X-Requested-With") == "fetch"


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
                if _wants_json_response():
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
_EXEMPT_ENDPOINTS = {"auth.login", "auth.logout", "auth.setup", "auth.forgot_password", "auth.sso", "static",
                     "health"}


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
        account = session_account()
        if account is None:
            session.pop("account_id", None)
            # Same fetch-detection as require_permission's 403 branch (see
            # _wants_json_response) - without it, a session that expires
            # mid-page (e.g. during a long-running Network Check scan) makes
            # every subsequent fetch() silently receive the login page's
            # HTML instead of JSON. None of the JS callers expect that, so
            # resp.json() throws and the failure is invisible: no error
            # banner, results just never arrive - indistinguishable from the
            # scan being stuck.
            if _wants_json_response():
                return jsonify({"error": "Your session has expired. Please log in again."}), 401
            return redirect(url_for("auth.login", next=request.full_path))
        g.current_account = account
        g.current_permissions = load_permissions_for_role(account["role_id"])
        return None

    @app.context_processor
    def inject_current_account():
        return {"current_account": current_account(), "current_permissions": current_permissions()}
