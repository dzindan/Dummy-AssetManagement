"""Dev-only entrypoint: runs the Flask app on a fixed port without the
browser-launching/PyInstaller-oriented logic in main.py. Used by the
Browser pane preview tooling during development."""

from app import create_app

if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=8737, debug=False, use_reloader=False)
