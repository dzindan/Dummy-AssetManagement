import logging

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session, url_for

from ..auth import (
    SECURITY_QUESTIONS_BY_KEY,
    SignInRefused,
    ad_settings,
    ad_sign_in_on,
    require_permission,
    sign_in_ad,
    tell_new_ad_account,
    accounts_table_is_empty,
    create_account,
    find_account_by_username,
    hash_password,
    login_account,
    logout_account,
    safe_next,
    session_account,
    verify_password,
    verify_security_answer,
)
from .. import throttle
from ..db import get_connection

bp = Blueprint("auth", __name__)
log = logging.getLogger(__name__)


def _safe_next(next_url: str) -> str:
    return safe_next(next_url, url_for("dashboard.index"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if accounts_table_is_empty():
        return redirect(url_for("auth.setup"))

    next_url = request.values.get("next", "")
    if session_account() is not None:
        return redirect(_safe_next(next_url))
    ad_on = ad_sign_in_on()
    # Windows sign-in first, once per visit: a domain PC signs in with no typing
    if (request.method == "GET" and ad_on and not request.args.get("form")
            and not session.get("sso_tried")):
        return redirect(url_for("auth.sso", next=next_url))

    def form(status=200):
        return render_template("login.html", next_url=next_url, ad=ad_settings(), ad_on=ad_on), status

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        name, address = throttle.name_key(username), request.remote_addr or ""
        wait = max(throttle.sign_in_by_name.wait_seconds(name), throttle.sign_in_by_address.wait_seconds(address))
        if wait:
            # not even checked: no guess counts, and an AD account isn't locked in Windows through here
            flash(throttle.wait_message(wait), "error")
            return form(429)
        account = find_account_by_username(username)
        local = account is not None and (account["auth_source"] or "local") == "local"
        if local and account["is_active"] and verify_password(password, account["password_hash"]):
            throttle.sign_in_by_name.clear(name)
            login_account(account["id"])
            return redirect(_safe_next(next_url))
        message = "Incorrect username or password."
        if ad_on and not local:
            from .. import winauth
            try:
                identity = winauth.logon(username, password, ad_settings()["domain"])
            except winauth.AuthError as e:
                message = str(e)
            else:
                throttle.sign_in_by_name.clear(name)
                try:
                    login_account(sign_in_ad(identity))
                    tell_new_ad_account()
                    return redirect(_safe_next(next_url))
                except SignInRefused as e:
                    flash(str(e), "error")
                    return form()
        locked = throttle.sign_in_by_name.fail(name) | throttle.sign_in_by_address.fail(address)
        log.warning("Failed sign-in as %r from %s%s", username, address, " - now locked for a while" if locked else "")
        flash(throttle.wait_message(throttle.LOCK) if locked else message, "error")
        return form(429 if locked else 200)

    return form()


@bp.route("/quit", methods=["POST"], endpoint="quit")
@require_permission("manage_settings")
def quit_app():
    """The Quit button of the app on a PC alone (it has no window to close)."""
    from .. import lifecycle

    if not current_app.config.get("QUIT_ENABLED"):
        abort(404)
    lifecycle.quit_after_requests()
    return ("<!doctype html><title>Asset Management Tool</title>"
            "<p>Asset Management Tool has stopped. You can close this tab.</p>")


@bp.route("/sso")
def sso():
    """Windows sign-in: answer the browser's Negotiate (Kerberos / NTLM)
    handshake. A browser that can't (not a domain PC, the site not in its
    Local intranet zone) lands back on the form."""
    from .. import winauth

    next_url = request.args.get("next", "")
    session["sso_tried"] = True
    form_url = url_for("auth.login", next=next_url, form=1)
    if not ad_sign_in_on():
        return redirect(form_url)
    header = request.headers.get("Authorization", "")

    def challenge(token: str = "", note: str = ""):
        # the body only shows when the browser gives up (no Windows sign-in)
        from markupsafe import escape

        url = escape(form_url)
        body = (f'<!doctype html><meta http-equiv="refresh" content="0;url={url}">'
                f'<p>{escape(note or "Windows sign-in is not available here.")} '
                f'<a href="{url}">Sign in with your AD username and password</a>.</p>')
        resp = Response(body, status=401, mimetype="text/html")
        resp.headers["WWW-Authenticate"] = f"Negotiate {token}".strip()
        return resp

    if not header.lower().startswith("negotiate "):
        return challenge()
    connection = (request.environ.get("REMOTE_ADDR"), request.environ.get("REMOTE_PORT"))
    try:
        identity, token = winauth.negotiate.step(connection, header)
    except winauth.AuthError as e:
        flash(f"{e} - sign in with your AD username and password.", "error")
        return redirect(form_url)
    if identity is None:
        return challenge(token)  # NTLM: one more round on this connection
    try:
        login_account(sign_in_ad(identity))
        tell_new_ad_account()
    except SignInRefused as e:
        flash(str(e), "error")
        return redirect(form_url)
    resp = redirect(_safe_next(next_url))
    if token:
        resp.headers["WWW-Authenticate"] = f"Negotiate {token}"
    return resp


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Two-step, no-JS flow: step 1 is just a username field; submitting it
    reloads this same page with ?username=, which (if that account has a
    security question set) reveals the question plus the answer/new-password
    fields. Unlike the recovery key this replaced, the question/answer isn't
    cleared after a successful reset - it's meant to be reusable, the same
    way the question itself never changes."""
    if accounts_table_is_empty():
        return redirect(url_for("auth.setup"))

    no_question_error = (
        "That username doesn't exist, isn't active, or doesn't have a security question set. "
        "Ask another Admin to reset your password from Settings > Users & Roles."
    )

    if request.method == "POST":
        username = request.form.get("username", "")
        answer = request.form.get("answer", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        account = find_account_by_username(username)
        if account and (account["auth_source"] or "local") == "ad":
            flash("This is an AD account: its password is your Windows password - change it in Windows.", "error")
            return redirect(url_for("auth.login", form=1))
        # Same generic message whether the username doesn't exist, isn't
        # active, has no security question set, or the answer just doesn't
        # match - doesn't hint to an attacker which of those is true.
        name = throttle.name_key(username)
        wait = throttle.reset_by_name.wait_seconds(name)
        if wait:
            flash(throttle.wait_message(wait), "error")
            return redirect(url_for("auth.login", form=1))
        error = "Username or answer is incorrect."
        if account and account["is_active"] and verify_security_answer(account, answer):
            error = None
            throttle.reset_by_name.clear(name)
        else:
            if throttle.reset_by_name.fail(name):
                log.warning("Forgot password: too many wrong answers for %r from %s", username, request.remote_addr)
                flash(throttle.wait_message(throttle.LOCK), "error")
                return redirect(url_for("auth.login", form=1))
        if error is None and password != confirm:
            error = "Passwords do not match."
        elif error is None and len(password) < 8:
            error = "Password must be at least 8 characters."

        if error:
            flash(error, "error")
            question_text = (
                SECURITY_QUESTIONS_BY_KEY.get(account["security_question_key"]) if account else None
            )
            return render_template("forgot_password.html", username=username, question_text=question_text)

        conn = get_connection()
        try:
            conn.execute(
                "UPDATE accounts SET password_hash = ? WHERE id = ?",
                (hash_password(password), account["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        flash("Password reset. You can now log in with your new password.", "success")
        return redirect(url_for("auth.login"))

    username = request.args.get("username", "").strip()
    question_text = None
    if username:
        # which names exist (and their question) can't be listed by trying many
        address = request.remote_addr or ""
        wait = throttle.reset_lookups_by_address.wait_seconds(address)
        if wait or throttle.reset_lookups_by_address.fail(address):
            flash(throttle.wait_message(wait or throttle.LOCK), "error")
            return render_template("forgot_password.html", username="", question_text=None), 429
        account = find_account_by_username(username)
        if account and account["is_active"] and account["security_question_key"]:
            question_text = SECURITY_QUESTIONS_BY_KEY.get(account["security_question_key"])
        else:
            flash(no_question_error, "error")
            username = ""

    return render_template("forgot_password.html", username=username, question_text=question_text)


@bp.route("/logout", methods=["POST"])
def logout():
    logout_account()
    # signed out on purpose: no Windows sign-in straight back in (another account may sign in)
    session["sso_tried"] = True
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
