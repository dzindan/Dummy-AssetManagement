import json
import os
import sys

APP_FOLDER_NAME = "AssetManagementTool"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def safe_filename(name: str, fallback: str = "file") -> str:
    """Strip characters Windows/Excel don't like from a name headed into a
    download filename (branch/eng names, report labels...) - keeps spaces,
    hyphens and underscores since those are fine and read better than a
    filename collapsed to underscores everywhere."""
    cleaned = "".join(c for c in name if c.isalnum() or c in " -_").strip()
    return cleaned or fallback


def get_bundle_dir() -> str:
    """Directory containing bundled read-only assets (templates, static, docx templates).

    When packaged with PyInstaller --onefile, bundled data lives under
    sys._MEIPASS. When running from source, it's the project root (parent
    of this app/ package).
    """
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_default_app_data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_FOLDER_NAME)


def _location_pointer_path() -> str:
    # Always kept at the default location (never the custom one) since that's
    # the one place guaranteed to exist and be writable before any user
    # choice has been made - it just stores where the *real* data actually is.
    default_dir = get_default_app_data_dir()
    os.makedirs(default_dir, exist_ok=True)
    return os.path.join(default_dir, "data_location.json")


def _configured_data_dir() -> str:
    """The custom path stored via Settings > Data Storage Location, if any -
    "" if none has ever been set (or the pointer file itself can't be read)."""
    pointer = _location_pointer_path()
    if not os.path.exists(pointer):
        return ""
    try:
        with open(pointer, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("data_dir", "").strip()
    except (OSError, ValueError):
        return ""


def _ensure_data_subdirs(base: str) -> None:
    os.makedirs(base, exist_ok=True)
    os.makedirs(os.path.join(base, "handovers"), exist_ok=True)
    os.makedirs(os.path.join(base, "exports"), exist_ok=True)
    os.makedirs(os.path.join(base, "uploads"), exist_ok=True)


def get_app_data_dir_status() -> dict:
    """Resolves the writable data directory for this launch, falling back to
    the default (%LOCALAPPDATA%\\AssetManagementTool) location if a
    previously-configured custom path (an external drive, a network share...)
    is no longer reachable - rather than letting the whole app fail to start
    with no way back into Settings to fix or reset it.

    The stored pointer itself is left untouched on fallback (not silently
    overwritten with the default) so a share/drive that comes back online
    later is used again on the next launch, and Settings can still show the
    user what's actually configured, not just where data ended up this time.

    Returns {"path", "is_fallback", "configured_path", "error"} - "error" is
    the OSError text explaining why the configured path couldn't be used,
    None when nothing is wrong.
    """
    configured = _configured_data_dir()
    if configured:
        try:
            _ensure_data_subdirs(configured)
            return {"path": configured, "is_fallback": False, "configured_path": configured, "error": None}
        except OSError as exc:
            default_dir = get_default_app_data_dir()
            _ensure_data_subdirs(default_dir)
            return {"path": default_dir, "is_fallback": True, "configured_path": configured, "error": str(exc)}

    default_dir = get_default_app_data_dir()
    _ensure_data_subdirs(default_dir)
    return {"path": default_dir, "is_fallback": False, "configured_path": None, "error": None}


def get_app_data_dir() -> str:
    """Writable directory for the sqlite DB and generated files.

    Defaults to %LOCALAPPDATA%\\AssetManagementTool, but can be redirected to
    any other folder (a bigger drive, a shared network path...) via
    Settings > Data Storage Location. See get_app_data_dir_status() for the
    fallback behavior when a configured custom path is unreachable.
    """
    return get_app_data_dir_status()["path"]


def set_app_data_dir(new_dir: str) -> None:
    with open(_location_pointer_path(), "w", encoding="utf-8") as f:
        json.dump({"data_dir": new_dir}, f)


def get_db_path() -> str:
    return os.path.join(get_app_data_dir(), "app.db")


def get_handovers_dir() -> str:
    return os.path.join(get_app_data_dir(), "handovers")


def get_exports_dir() -> str:
    return os.path.join(get_app_data_dir(), "exports")


def get_diff_reports_dir() -> str:
    path = os.path.join(get_exports_dir(), "diff_reports")
    os.makedirs(path, exist_ok=True)
    return path


def get_uploads_dir() -> str:
    return os.path.join(get_app_data_dir(), "uploads")


def get_handover_template_path() -> str:
    return os.path.join(get_bundle_dir(), "templates_docx", "handover_template.docx")
