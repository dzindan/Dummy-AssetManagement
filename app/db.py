import os
import sqlite3

from .paths import get_app_data_dir, is_network_path
from .text_utils import normalize_user_id, strip_bank_prefix

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
    branch_no TEXT PRIMARY KEY,
    local_name TEXT,
    eng_name TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    user_no TEXT PRIMARY KEY,
    user_no_norm TEXT,
    branch_no TEXT,
    user_name TEXT,
    eng_name TEXT,
    banker_key TEXT,
    status TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_files_json TEXT,
    label TEXT,
    period TEXT,
    sheet_name TEXT,
    branch_hint TEXT,
    unmapped_columns_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_import_batches_period ON import_batches (period);

CREATE TABLE IF NOT EXISTS asset_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    asset_key TEXT NOT NULL,
    branch_dept TEXT,
    branch_no TEXT,
    device_name TEXT,
    device_name_raw TEXT,
    user_id_raw TEXT,
    user_id_norm TEXT,
    full_name TEXT,
    full_name_raw TEXT,
    model_device TEXT,
    model_device_raw TEXT,
    serial_tag TEXT,
    status TEXT,
    status_raw TEXT,
    remark TEXT,
    position TEXT,
    handover_date TEXT,
    ip TEXT,
    extra_json TEXT,
    source_file TEXT,
    FOREIGN KEY (batch_id) REFERENCES import_batches (id)
);

CREATE INDEX IF NOT EXISTS idx_asset_items_batch ON asset_items (batch_id);
CREATE INDEX IF NOT EXISTS idx_asset_items_key ON asset_items (asset_key);
CREATE INDEX IF NOT EXISTS idx_asset_items_user_norm ON asset_items (user_id_norm);
CREATE INDEX IF NOT EXISTS idx_asset_items_branch ON asset_items (branch_no);
-- Backs CURRENT_ASSETS_CTE (queries.py) - "current state" for a branch is
-- computed as MAX(batch_id) per branch, and that per-branch grouping/join
-- is by far the most frequently run query in the app (Dashboard, Manage
-- Assets, Lookup, Branch Detail, Duplicate Check all go through it).
CREATE INDEX IF NOT EXISTS idx_asset_items_branch_batch ON asset_items (branch_no, batch_id);

CREATE TABLE IF NOT EXISTS handover_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ho_date TEXT,
    user_no TEXT,
    user_name TEXT,
    branch_no TEXT,
    ho_type TEXT,
    reason TEXT,
    assets_json TEXT,
    ict_rep_name TEXT,
    ict_rep_id TEXT,
    receiving_name TEXT,
    receiving_title TEXT,
    receiving_dept TEXT,
    receiving_id TEXT,
    signature_receiving_name TEXT,
    signature_prepared_by TEXT,
    docx_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_handover_user ON handover_records (user_no);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS branch_aliases (
    alias TEXT PRIMARY KEY,
    branch_no TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_aliases (
    alias TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_standard_names (
    name TEXT PRIMARY KEY,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS status_standard_names (
    name TEXT PRIMARY KEY,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS status_aliases (
    alias TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_unmapped (
    raw_status TEXT PRIMARY KEY,
    first_seen_at TEXT,
    last_seen_at TEXT,
    occurrences INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS model_standard_names (
    name TEXT PRIMARY KEY,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS model_aliases (
    alias TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_unmapped (
    raw_model TEXT PRIMARY KEY,
    first_seen_at TEXT,
    last_seen_at TEXT,
    occurrences INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS device_unmapped (
    raw_name TEXT PRIMARY KEY,
    first_seen_at TEXT,
    last_seen_at TEXT,
    occurrences INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS branch_unresolved (
    raw_hint TEXT PRIMARY KEY,
    first_seen_at TEXT,
    last_seen_at TEXT,
    occurrences INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS diff_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    period TEXT,
    batch_ids TEXT,
    branch_labels TEXT,
    file_path TEXT NOT NULL,
    label TEXT
);

CREATE TABLE IF NOT EXISTS network_check_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_at TEXT NOT NULL,
    branch_no TEXT,
    ip TEXT,
    asset_id INTEGER,
    field TEXT,
    old_value TEXT,
    new_value TEXT
);

CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT,
    period TEXT,
    rows_processed INTEGER,
    result TEXT
);

CREATE INDEX IF NOT EXISTS idx_import_log_imported_at ON import_log (imported_at);

-- Login/permissions. Deliberately named `accounts`/`roles`, not `users` -
-- `users` above is bank-staff domain data imported from IDFromAither
-- (PK user_no), unrelated to who can log into this tool.
CREATE TABLE IF NOT EXISTS permissions (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    permission_key TEXT NOT NULL REFERENCES permissions (key),
    PRIMARY KEY (role_id, permission_key)
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    username_norm TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role_id INTEGER NOT NULL REFERENCES roles (id),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    last_login_at TEXT,
    recovery_key_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_role ON accounts (role_id);
"""

# Single source of truth for what a role can be granted - `view` isn't
# here on purpose: it's just "you have a valid session" (see auth.py's
# before_request), not a stored permission, since read access isn't the
# problem this app needs to gate; uncontrolled writes are. Keys are load-
# bearing (referenced by role_permissions.permission_key and by every
# @require_permission(...) call) - only ever add a key here, never rename
# one, or existing role_permissions rows would silently point at nothing.
PERMISSIONS = {
    "import_data": "Import Data (upload branch codes, user IDs, asset reports)",
    "edit_assets": "Edit/delete assets (Manage Assets)",
    "handover": "Generate hand-over forms (Lookup & Hand-Over)",
    "network_check": "Network Check (run scans and apply results)",
    "manage_mappings": "Manage Device/Status/Model/Branch Mapping (Settings)",
    "manage_settings": "Change system settings and data storage location (Settings)",
    "manage_users": "Manage user accounts and roles",
}

# Built-in roles are fully locked (not deletable/renamable/permission-
# editable via UI) and re-synced to this canonical definition on every
# init_db() run - see _seed_permissions_and_roles(). Anyone wanting a
# different combination creates a custom role instead.
BUILTIN_ROLES = {
    "Viewer": [],
    "Editor": ["import_data", "edit_assets", "handover", "network_check", "manage_mappings"],
    "Admin": list(PERMISSIONS.keys()),
}

# The editable "standard" device list, seeded once on first run from the
# device types actually observed across the real asset report files.
DEFAULT_STANDARD_DEVICE_NAMES = [
    "PC", "LCD", "IP PHONE", "NOTEBOOK", "CARD READER", "CCTV",
    "PRINTER", "COLOR PRINTER", "CARD PRINTER", "SCANNER", "TABLET",
    "PASSBOOK", "PINPAD", "PROJECTOR", "SPEAKER", "SERVER PC",
    "HEADSET", "PAPER SHREDDER", "FAX", "PHOTOCOPIER", "WEBCAM",
    "KEYBOARD", "COMPUTER MOUSE", "ROUTER", "SWITCH", "UPS",
]

# Seeded once on first run so device-name normalization is useful immediately,
# based on spelling variants already observed across the real asset report
# files, mapped onto the standard list above.
DEFAULT_DEVICE_ALIASES = {
    "IPPHONE": "IP PHONE",
    "IP-PHONE": "IP PHONE",
    "ID CARD READER": "CARD READER",
    "IDCARD READER": "CARD READER",
    "HHD PORTABLE": "NOTEBOOK",
    "LAPTOP": "NOTEBOOK",
    "DESKTOP": "PC",
    "COMPUTER": "PC",
    "MONITOR": "LCD",
    "SCREEN": "LCD",
    "CCTV CAMERA": "CCTV",
    "CAMERA": "CCTV",
    "PHOTOCOPY": "PHOTOCOPIER",
    "PRINTER COLOR": "COLOR PRINTER",
    "PRINTER CARD": "CARD PRINTER",
}

# The editable "standard" status list - same standard-name/alias/unmapped
# pattern as devices above, normalized at import time via
# importer.normalize_status. Also used to populate the Manage Assets Status
# filter dropdown.
DEFAULT_STANDARD_STATUSES = ["WAREHOUSE", "USING LOCAL", "USING INTERNET"]

# The editable "standard" model list, grouped by manufacturer and seeded
# from the models actually observed most often across the real asset report
# files (~217 distinct raw model_device strings exist in practice - this
# covers the clear high-frequency ones; everything else lands in the
# Unmapped pool for the user to assign, same as devices/branches).
DEFAULT_STANDARD_MODELS = [
    # Dell (PCs/monitors)
    "DELL OPTIPLEX 3050", "DELL OPTIPLEX 3060", "DELL OPTIPLEX 3070",
    "DELL OPTIPLEX 5080", "DELL OPTIPLEX 9010", "DELL PRO SLIM QCS1250",
    "DELL VOSTRO 3888", "DELL E2216HV", "DELL E2225HM", "DELL P2212HB",
    # HP (PCs/printers/laptops)
    "HP PRODESK 400 G2", "HP PRODESK 400 G3", "HP PRODESK 400 G4",
    "HP PRO SFF 400 G9", "HP LASERJET PRO 402DN", "HP LASERJET PRO 404DN",
    "HP LASERJET PRO 400 M404DN", "HP LASERJET PRO MFP M1536DNF",
    "HP PROBOOK 440 G1",
    # Cisco (IP phones)
    "CISCO CP-7821", "CISCO CP-7911G", "CISCO CP-6921", "CISCO CP-7941",
    "CISCO CP-7942", "CISCO CP-7962",
    # Samsung (monitors)
    "SAMSUNG S22F350FHE", "SAMSUNG S22A330NHE", "SAMSUNG S22F355FHE",
    "SAMSUNG S22D300NY", "SAMSUNG S22E310HY",
    # Synkey (passbook printers)
    "SYNKEY SK5310", "SYNKEY SK5320",
    # Kodak (scanners)
    "KODAK ALARIS S2050", "KODAK I2420",
    # Lenovo (PCs)
    "LENOVO THINKCENTRE M92P", "LENOVO THINKCENTRE M93P",
    # Logitech (webcam/headset)
    "LOGITECH C930E", "LOGITECH H540",
]

# Only very obvious spacing/typo variants of the exact same model, seen
# directly in the real files - unlike device aliases, model numbers are
# risky to guess-merge (a wrong guess would silently misreport hardware),
# so this stays conservative on purpose.
DEFAULT_MODEL_ALIASES = {
    "DELL OPTIPLEX  5080": "DELL OPTIPLEX 5080",
    "CP-7821": "CISCO CP-7821",
    "CP - 7821": "CISCO CP-7821",
    "CISCO 7821": "CISCO CP-7821",
    "CP7821": "CISCO CP-7821",
    "CP - 7911": "CISCO CP-7911G",
    "CISCO 7911": "CISCO CP-7911G",
    "CP-7911G": "CISCO CP-7911G",
    "CP - 6921": "CISCO CP-6921",
    "CISCO 6921": "CISCO CP-6921",
    "CP - 7941": "CISCO CP-7941",
    "CISCO 7941": "CISCO CP-7941",
    "CP - 7942": "CISCO CP-7942",
    "SGI-SK5310": "SYNKEY SK5310",
    "SK5310": "SYNKEY SK5310",
    "SGI-SK5320": "SYNKEY SK5320",
    "SK5320": "SYNKEY SK5320",
}


def get_connection() -> sqlite3.Connection:
    data_dir = get_app_data_dir()
    # timeout: wait for a lock instead of failing immediately - matters once
    # several people on the network are using this at once and one of them
    # is mid-import (imports hold a write lock for the whole batch).
    conn = sqlite3.connect(os.path.join(data_dir, "app.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if is_network_path(data_dir):
        # WAL (below) needs shared-memory locking for its -shm file that
        # SMB/NFS network filesystems don't all support the way a local
        # disk does - see is_network_path()'s docstring. That shows up as
        # spurious "database is locked"/"disk I/O error" failures, not a
        # clean rejection, so detect it up front rather than let every
        # connection risk hitting it. The traditional rollback journal only
        # needs ordinary file locking, which network shares have always
        # handled fine (one writer at a time, same as WAL's real limit here
        # anyway once mmap isn't reliable).
        conn.execute("PRAGMA journal_mode = DELETE")
    else:
        # WAL lets readers (dashboard, lookup) proceed without blocking on a
        # concurrent writer (import, hand-over generation) - matters once
        # this is a shared server multiple people hit at the same time.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _add_missing_columns(conn)
        for name in DEFAULT_STANDARD_DEVICE_NAMES:
            conn.execute(
                "INSERT OR IGNORE INTO device_standard_names (name, created_at) VALUES (?, datetime('now'))",
                (name,),
            )
        for alias, canonical in DEFAULT_DEVICE_ALIASES.items():
            conn.execute(
                "INSERT OR IGNORE INTO device_aliases (alias, canonical_name) VALUES (?, ?)",
                (alias, canonical),
            )
        for name in DEFAULT_STANDARD_STATUSES:
            conn.execute(
                "INSERT OR IGNORE INTO status_standard_names (name, created_at) VALUES (?, datetime('now'))",
                (name,),
            )
        for name in DEFAULT_STANDARD_MODELS:
            conn.execute(
                "INSERT OR IGNORE INTO model_standard_names (name, created_at) VALUES (?, datetime('now'))",
                (name,),
            )
        for alias, canonical in DEFAULT_MODEL_ALIASES.items():
            conn.execute(
                "INSERT OR IGNORE INTO model_aliases (alias, canonical_name) VALUES (?, ?)",
                (alias, canonical),
            )
        _backfill_branch_names(conn)
        _backfill_unmapped(conn, "asset_items", "device_name", "device_aliases", "device_standard_names",
                            "device_unmapped", "raw_name")
        _backfill_unmapped(conn, "asset_items", "status", "status_aliases", "status_standard_names",
                            "status_unmapped", "raw_status")
        _backfill_unmapped(conn, "asset_items", "model_device", "model_aliases", "model_standard_names",
                            "model_unmapped", "raw_model")
        prune_stale_unmapped(conn)
        _backfill_user_no_norm(conn)
        _seed_permissions_and_roles(conn)
        conn.commit()
    finally:
        conn.close()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS never adds columns to a table that already
    exists, so new columns added to asset_items after a database was first
    created (e.g. full_name_raw) need an explicit ALTER TABLE here - a
    no-op once a database already has them."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(asset_items)").fetchall()}
    if "full_name_raw" not in existing:
        conn.execute("ALTER TABLE asset_items ADD COLUMN full_name_raw TEXT")
    if "model_device_raw" not in existing:
        conn.execute("ALTER TABLE asset_items ADD COLUMN model_device_raw TEXT")
    if "status_raw" not in existing:
        conn.execute("ALTER TABLE asset_items ADD COLUMN status_raw TEXT")

    existing_user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "user_no_norm" not in existing_user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN user_no_norm TEXT")
    # Created here rather than in the static SCHEMA script - on an existing
    # database, the ALTER TABLE just above only just added the column this
    # index is on, and CREATE INDEX would fail if it ran any earlier.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_no_norm ON users (user_no_norm)")

    existing_batch_cols = {row["name"] for row in conn.execute("PRAGMA table_info(import_batches)").fetchall()}
    if "sheet_name" not in existing_batch_cols:
        conn.execute("ALTER TABLE import_batches ADD COLUMN sheet_name TEXT")
    if "branch_hint" not in existing_batch_cols:
        conn.execute("ALTER TABLE import_batches ADD COLUMN branch_hint TEXT")
    if "unmapped_columns_json" not in existing_batch_cols:
        conn.execute("ALTER TABLE import_batches ADD COLUMN unmapped_columns_json TEXT")

    existing_account_cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "recovery_key_hash" not in existing_account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN recovery_key_hash TEXT")


def _backfill_user_no_norm(conn: sqlite3.Connection) -> None:
    """Populate `users.user_no_norm` for rows imported before that column
    existed (or, degenerately, any row where it's still blank for some
    reason) - find_user() looks this column up directly instead of pulling
    every users row into Python to normalize it there, so a database
    upgraded from an older version needs this to actually benefit."""
    rows = conn.execute(
        "SELECT user_no FROM users WHERE user_no_norm IS NULL OR user_no_norm = ''"
    ).fetchall()
    for row in rows:
        _, norm = normalize_user_id(row["user_no"])
        conn.execute("UPDATE users SET user_no_norm = ? WHERE user_no = ?", (norm, row["user_no"]))


def _backfill_unmapped(
    conn: sqlite3.Connection,
    source_table: str,
    source_column: str,
    alias_table: str,
    standard_table: str,
    unmapped_table: str,
    unmapped_column: str,
) -> None:
    """One-time-per-value visibility fix for status/model mapping: rows
    imported before this mapping system existed never got a chance to be
    flagged as unmapped (that only happens at import time). Without this,
    an already-imported oddball value would stay invisible until that
    branch happens to be re-imported again, maybe months later. This scans
    existing distinct values and queues any non-standard ones into the
    Unmapped pool - cheap no-op once everything is either mapped or already
    queued (ON CONFLICT DO NOTHING, since this must never bump the
    occurrence count itself; only a real import row seen should do that)."""
    known_aliases = {row["alias"] for row in conn.execute(f"SELECT alias FROM {alias_table}").fetchall()}
    known_standards = {row["name"] for row in conn.execute(f"SELECT name FROM {standard_table}").fetchall()}
    rows = conn.execute(
        f"SELECT DISTINCT {source_column} AS v FROM {source_table} WHERE {source_column} != ''"
    ).fetchall()
    for row in rows:
        value = row["v"]
        if value not in known_aliases and value not in known_standards:
            conn.execute(
                f"""
                INSERT INTO {unmapped_table} ({unmapped_column}, first_seen_at, last_seen_at, occurrences)
                VALUES (?, datetime('now'), datetime('now'), 1)
                ON CONFLICT({unmapped_column}) DO NOTHING
                """,
                (value,),
            )


def prune_stale_unmapped(conn: sqlite3.Connection) -> None:
    """Unmapped entries are a queue of raw values still needing an
    assignment - once the last asset carrying that value is deleted, edited
    away, or dropped by a re-import, the entry has nothing left to assign
    and should disappear too. Nothing else prunes this queue (import only
    ever adds to it, and delete_standard_name()/_status()/_model() only
    re-queue), so without this a value can sit in Settings > Unmapped
    forever pointing at zero actual assets, even though Manage Assets has
    nothing to show for it. Call after any operation that deletes or edits
    asset_items rows (see asset_edit.py), plus once here in init_db() to
    self-heal a database that already went stale before this existed."""
    for source_column, unmapped_table, unmapped_column in (
        ("device_name", "device_unmapped", "raw_name"),
        ("status", "status_unmapped", "raw_status"),
        ("model_device", "model_unmapped", "raw_model"),
    ):
        conn.execute(
            f"""
            DELETE FROM {unmapped_table}
            WHERE {unmapped_column} NOT IN (
                SELECT DISTINCT {source_column} FROM asset_items WHERE {source_column} != ''
            )
            """
        )


def _backfill_branch_names(conn: sqlite3.Connection) -> None:
    """One-time cleanup for branches imported before eng_name started having
    its "SHINHAN BANK VIETNAM(E)" prefix stripped at import time (see
    importer.import_branch_file) - cheap no-op once everything is clean."""
    for row in conn.execute("SELECT branch_no, eng_name FROM branches").fetchall():
        cleaned = strip_bank_prefix(row["eng_name"])
        if cleaned != row["eng_name"]:
            conn.execute(
                "UPDATE branches SET eng_name = ? WHERE branch_no = ?", (cleaned, row["branch_no"])
            )


def _seed_permissions_and_roles(conn: sqlite3.Connection) -> None:
    """Keeps the `permissions` catalog and the 3 built-in roles in sync with
    the canonical PERMISSIONS/BUILTIN_ROLES definitions above, every launch.
    Labels/permission-sets can change across app versions; INSERT OR REPLACE
    (permissions) and a delete-then-reinsert of each built-in role's
    role_permissions rows means a version that adds a new permission just
    updates the Python constant, and every existing deployed database picks
    it up automatically on next launch - no migration step, same self-
    healing idea as _backfill_user_no_norm/prune_stale_unmapped above.
    Custom (non-built-in) roles are never touched here."""
    for i, (key, label) in enumerate(PERMISSIONS.items()):
        conn.execute(
            "INSERT INTO permissions (key, label, sort_order) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET label = excluded.label, sort_order = excluded.sort_order",
            (key, label, i),
        )
    for name, perm_keys in BUILTIN_ROLES.items():
        conn.execute(
            "INSERT OR IGNORE INTO roles (name, is_builtin, created_at) VALUES (?, 1, datetime('now'))",
            (name,),
        )
        role_id = conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("DELETE FROM role_permissions WHERE role_id = ?", (role_id,))
        conn.executemany(
            "INSERT INTO role_permissions (role_id, permission_key) VALUES (?, ?)",
            [(role_id, key) for key in perm_keys],
        )


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def log_import(conn, kind: str, source_file: str, rows_processed: int | None = None,
                period: str = "", result: str = "OK") -> None:
    """Record one row per import action (branch codes, user IDs, an asset
    report, a Total Asset baseline period) to a permanent audit log, viewable
    on the Import History page - independent of import_batches, which only
    exists for asset-report "current state"/diffing and is never shown as a
    plain list on its own."""
    conn.execute(
        "INSERT INTO import_log (imported_at, kind, source_file, period, rows_processed, result) "
        "VALUES (datetime('now'), ?, ?, ?, ?, ?)",
        (kind, source_file, period, rows_processed, result),
    )
