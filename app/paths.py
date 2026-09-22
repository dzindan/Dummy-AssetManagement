import json
import os
import sys

APP_FOLDER_NAME = "AssetManagementTool"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def read_json(path: str) -> dict:
    """Tolerant JSON read for the small config files this module hands out
    paths for (data_location.json, settings.json, local_settings.json) -
    a missing or corrupt file is treated as "nothing set yet" rather than
    raising, matching how _configured_data_dir() already behaves."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path: str, data: dict) -> None:
    """Write via a temp file + os.replace() rather than an in-place open("w")
    - this app is used by several people on the LAN at once (see
    get_connection()'s docstring), and a plain in-place write is not safe
    against two near-simultaneous writers or an interrupted write leaving a
    truncated file. SQLite gave the old settings table this durability for
    free; a JSON file needs it done explicitly."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


def is_network_path(path: str) -> bool:
    """True for a UNC path (\\\\server\\share\\...) or a drive letter mapped to
    one (`net use Z: \\\\server\\share`, or Explorer's "Map network drive").

    SQLite's WAL journal mode - the default this app connects with (see
    db.py's get_connection) - is explicitly documented upstream as
    unreliable over a network filesystem: it needs a shared memory mapping
    for its -shm file that SMB/NFS clients don't all implement the same way
    a local disk does, which shows up as spurious "database is locked"/
    "disk I/O error" failures rather than a clean rejection up front. db.py
    checks this to fall back to the traditional rollback journal instead,
    which only needs ordinary file locking and has always been fine over a
    network share (with the usual single-writer-at-a-time caveat)."""
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    if sys.platform != "win32":
        return False
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return False
    import ctypes

    DRIVE_REMOTE = 4
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE
    except OSError:
        return False


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
    return read_json(_location_pointer_path()).get("data_dir", "").strip()


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
    write_json_atomic(_location_pointer_path(), {"data_dir": new_dir})


def get_data_db_path() -> str:
    return os.path.join(get_app_data_dir(), "data.db")


def get_logs_db_path() -> str:
    return os.path.join(get_app_data_dir(), "logs.db")


def get_settings_path() -> str:
    """Portable settings (ict_rep_name/id, cucm_*) - travels with the data
    dir like data.db/logs.db, since these are safe/wanted on a new machine.
    See get_local_settings_path() for the machine-local ones that aren't."""
    return os.path.join(get_app_data_dir(), "settings.json")


def get_local_settings_path() -> str:
    """Machine-local settings (secret_key, asset_reports_folder,
    id_files_folder) - deliberately kept at the fixed default location,
    never the (possibly redirected/custom) data dir, for the same reason
    _location_pointer_path() already is: it must be structurally impossible
    to accidentally copy along with a data folder headed to another machine.
    A new machine simply finds none of these set and starts fresh (a new
    secret_key generates itself; the two folder paths get re-entered once in
    Settings) rather than inheriting values that would be wrong or unsafe
    there."""
    default_dir = get_default_app_data_dir()
    os.makedirs(default_dir, exist_ok=True)
    return os.path.join(default_dir, "local_settings.json")


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
