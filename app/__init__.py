import os

from flask import Flask

from .db import init_db
from .paths import get_bundle_dir
from .version import APP_VERSION


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
    # Only used to sign the flash-message session cookie for this local
    # tool - a fresh random key per process start is fine.
    app.secret_key = os.urandom(24)

    init_db()

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

    @app.context_processor
    def inject_app_version():
        return {"app_version": APP_VERSION}

    return app
