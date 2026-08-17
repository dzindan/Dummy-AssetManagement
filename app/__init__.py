import os
import secrets

from flask import Flask, Response

from .auth import register_auth
from .db import get_setting, init_db, set_setting
from .paths import get_app_data_dir_status, get_bundle_dir
from .version import APP_VERSION


def _db_error_response(db_error: str) -> Response:
    from markupsafe import escape

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Asset Management Tool - Data Error</title>
<style>body{{font-family:sans-serif;max-width:640px;margin:3rem auto;padding:0 1rem;
line-height:1.5;color:#222}}h1{{color:#a33}}code{{background:#f0f0f0;padding:0.15rem 0.4rem;
border-radius:3px}}</style></head><body>
<h1>Can't open the data folder</h1>
<p>The app started, but couldn't set up its database:</p>
<p><code>{escape(db_error)}</code></p>
<p>This usually means the folder configured under <b>Settings &gt; Data Storage Location</b>
is on a drive or network share that isn't reachable right now (unplugged, disconnected, or
the path changed). Reconnect it and restart the app, or open
<code>%LOCALAPPDATA%\\AssetManagementTool\\data_location.json</code> in a text editor and
remove it to reset to the default location.</p>
</body></html>"""
    return Response(html, status=500, mimetype="text/html")


def create_app() -> Flask:
    # Explicit, absolute template/static paths (rather than Flask's default
    # root_path-relative lookup) so this keeps working once PyInstaller
    # freezes the app into a single exe, where __file__-based resolution of
    # bundled data is unreliable.
    bundle_dir = get_bundle_dir()
    app = Flask(
        __name__,
        template_folder=os.path.join(bundle_dir, "app", "templates"),
        static_folder=os.path.join(bundle_dir, "app", "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB upload cap

    # get_app_data_dir_status() (used by init_db -> get_connection -> get_db_path)
    # already falls back to the default location on its own if a configured
    # custom path (network share, external drive...) is unreachable, so this
    # almost never fails anymore. Still wrapped, rather than letting the whole
    # process crash before the browser tab even opens with no way back into
    # Settings, for the remaining edge cases that fallback can't route around
    # (e.g. the resolved directory exists but the .db file itself is
    # corrupted, or even %LOCALAPPDATA% itself isn't writable).
    db_error = None
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - see comment above
        db_error = str(exc)
    app.config["DB_INIT_ERROR"] = db_error

    if db_error:
        # Can't touch the DB at all, so don't try to read/persist the signing
        # key through it either - a throwaway one is enough to let Flask boot
        # and show the error page below instead of crashing outright. Every
        # route is intercepted before it can hit the (still broken) DB.
        app.secret_key = secrets.token_hex(32)

        @app.before_request
        def _block_on_db_error():  # noqa: ANN202
            return _db_error_response(db_error)

        return app

    # Persisted in the `settings` table (generated once, reused after) rather
    # than a fresh os.urandom() per process start - login sessions are signed
    # with this key, so a key that changes on every restart would silently
    # log everyone out each time the app relaunches.
    secret = get_setting("secret_key")
    if not secret:
        secret = secrets.token_hex(32)
        set_setting("secret_key", secret)
    app.secret_key = secret

    from .routes.auth import bp as auth_bp
    from .routes.dashboard import bp as dashboard_bp
    from .routes.import_data import bp as import_bp
    from .routes.lookup import bp as lookup_bp
    from .routes.update_compare import bp as update_bp
    from .routes.history import bp as history_bp
    from .routes.settings import bp as settings_bp
    from .routes.branch_detail import bp as branch_detail_bp
    from .routes.asset_edit import bp as asset_edit_bp
    from .routes.user_history import bp as user_history_bp
    from .routes.network_check import bp as network_check_bp
    from .routes.user_admin import bp as user_admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(import_bp)
    app.register_blueprint(lookup_bp)
    app.register_blueprint(update_bp)
    app.register_blueprint(history_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(branch_detail_bp)
    app.register_blueprint(asset_edit_bp)
    app.register_blueprint(user_history_bp)
    app.register_blueprint(network_check_bp)
    app.register_blueprint(user_admin_bp)

    register_auth(app)

    @app.context_processor
    def inject_app_version():
        return {"app_version": APP_VERSION}

    @app.context_processor
    def inject_data_dir_status():
        # Cheap (a couple of os.makedirs/exists calls) and needed on every
        # page, not just Settings, so a fallback is never invisible to
        # someone who lands anywhere else first.
        return {"data_dir_status": get_app_data_dir_status()}

    return app
