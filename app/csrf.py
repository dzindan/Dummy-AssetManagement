"""CSRF protection: every POST (form or fetch) must carry this session's
secret token, so a page on another site can't make a signed-in user's
browser change anything here.

The token lives in the (signed) session cookie; static/csrf.js reads it
from <meta name="csrf-token"> and adds it to every POST form (a hidden
csrf_token field, also for forms built and .submit()ted by script) and to
every same-site fetch() (an X-CSRF-Token header) - so no template has to
remember a hidden field.
"""

from __future__ import annotations

import hmac
import secrets

from flask import jsonify, render_template_string, request, session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
SESSION_KEY = "_csrf"
FIELD = "csrf_token"
HEADER = "X-CSRF-Token"

_REFUSED_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Please try again</title>
<style>body{font-family:sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;line-height:1.5}</style>
</head><body><h1>Please try again</h1>
<p>This page was open from before you signed in again (or before the app was updated), so that last
action wasn't done. <a href="{{ back }}">Go back</a>, reload the page and try again.</p></body></html>"""


def csrf_token() -> str:
    token = session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def _sent_token() -> str:
    sent = request.headers.get(HEADER, "")
    if not sent and request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        sent = request.form.get(FIELD, "")
    return sent


def register_csrf(app) -> None:
    """Call from create_app() after register_auth (the login check answers
    an expired session first, with its own redirect / 401)."""
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _check_csrf():
        if not app.config.get("CSRF_ENABLED", True) or request.method in SAFE_METHODS:
            return None
        if request.endpoint is None:
            return None  # 404 / 405 anyway
        expected = session.get(SESSION_KEY, "")
        sent = _sent_token()
        if expected and sent and hmac.compare_digest(str(sent), str(expected)):
            return None
        app.logger.warning("CSRF token missing/wrong: %s %s from %s", request.method, request.path,
                           request.remote_addr)
        if request.is_json or request.headers.get("X-Requested-With") == "fetch" or request.headers.get(HEADER):
            return jsonify({"error": "This page is out of date - reload it and try again."}), 400
        back = request.referrer or "/"
        if not back.startswith(request.host_url):
            back = "/"
        return render_template_string(_REFUSED_PAGE, back=back), 400
