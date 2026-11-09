"""Dev-only entrypoint: runs the Flask app on a fixed port without the
browser-launching/PyInstaller-oriented logic in main.py. Used by the
Browser pane preview tooling during development."""

from app import create_app
from app.lifecycle import serve_local

if __name__ == "__main__":
    app = create_app()
    serve_local(app, "127.0.0.1", 8737)
