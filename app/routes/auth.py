from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..auth import (
    accounts_table_is_empty,
    create_account,
    find_account_by_username,
    load_active_account,
    login_account,
    logout_account,
    verify_password,
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
        flash("Sai tên đăng nhập hoặc mật khẩu.", "error")

    return render_template("login.html", next_url=next_url)


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
            flash("Vui lòng nhập đầy đủ tên đăng nhập và mật khẩu.", "error")
        elif password != confirm:
            flash("Mật khẩu xác nhận không khớp.", "error")
        elif len(password) < 8:
            flash("Mật khẩu cần ít nhất 8 ký tự.", "error")
        else:
            conn = get_connection()
            try:
                admin_role = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()
            finally:
                conn.close()
            account_id = create_account(username, password, admin_role["id"])
            login_account(account_id)
            flash(f'Tài khoản quản trị "{username}" đã được tạo.', "success")
            return redirect(url_for("dashboard.index"))

    return render_template("setup.html")
