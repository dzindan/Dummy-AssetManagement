from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..auth import (
    accounts_table_is_empty,
    clear_recovery_key,
    create_account,
    find_account_by_username,
    hash_password,
    load_active_account,
    login_account,
    logout_account,
    verify_password,
    verify_recovery_key,
)
from ..db import get_connection

bp = Blueprint("auth", __name__)


def _safe_next(next_url: str) -> str:
    """Only ever redirect to a same-site path - a bare '/' fallback refuses
    anything that could send someone off this app (a bad ?next= value is
    user-supplied input)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for("dashboard.index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if accounts_table_is_empty():
        return redirect(url_for("auth.setup"))

    next_url = request.values.get("next", "")
    if load_active_account(session.get("account_id")) is not None:
        return redirect(_safe_next(next_url))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        account = find_account_by_username(username)
        if account and account["is_active"] and verify_password(password, account["password_hash"]):
            login_account(account["id"])
            return redirect(_safe_next(next_url))
        flash("Incorrect username or password.", "error")

    return render_template("login.html", next_url=next_url)


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if accounts_table_is_empty():
        return redirect(url_for("auth.setup"))

    if request.method == "POST":
        username = request.form.get("username", "")
        recovery_key = request.form.get("recovery_key", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        account = find_account_by_username(username)
        # Same generic message whether the username doesn't exist, isn't
        # active, has no recovery key set, or the key just doesn't match -
        # doesn't hint to an attacker which of those is true.
        error = "Username or recovery key is incorrect."
        if account and account["is_active"] and verify_recovery_key(account, recovery_key):
            error = None
        if error is None and password != confirm:
            error = "Passwords do not match."
        elif error is None and len(password) < 8:
            error = "Password must be at least 8 characters."

        if error:
            flash(error, "error")
        else:
            conn = get_connection()
            try:
                conn.execute(
                    "UPDATE accounts SET password_hash = ? WHERE id = ?",
                    (hash_password(password), account["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            clear_recovery_key(account["id"])
            flash("Password reset. You can now log in with your new password.", "success")
            return redirect(url_for("auth.login"))

    return render_template("forgot_password.html")


@bp.route("/logout", methods=["POST"])
def logout():
    logout_account()
    return redirect(url_for("auth.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    # Independently re-check emptiness here rather than relying solely on
    # register_auth's exemption list - that list only stops the *redirect*
    # to /setup, it doesn't stop a second direct POST once an admin already
    # exists (e.g. a stale browser tab left open on this page).
    if not accounts_table_is_empty():
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if not username or not password:
            flash("Please enter both a username and a password.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        else:
            conn = get_connection()
            try:
                admin_role = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()
            finally:
                conn.close()
            account_id = create_account(username, password, admin_role["id"])
            login_account(account_id)
            flash(f'Admin account "{username}" created.', "success")
            return redirect(url_for("dashboard.index"))

    return render_template("setup.html")
