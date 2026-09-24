import os
import shutil
import sqlite3

from .paths import (
    get_app_data_dir,
    get_data_db_path,
    get_local_settings_path,
    get_logs_db_path,
    get_settings_path,
    is_network_path,
    read_json,
    write_json_atomic,
)
from .text_utils import clean_ip, normalize_handover_date, normalize_user_id, strip_bank_prefix

# Machine-local settings can never live in data.db (that's exactly what gets
# copied to a new machine) or in settings.json (portable, meant to travel) -
# see get_local_settings_path()'s docstring. secret_key must never be copied
# between machines (it would let one machine's session cookies be replayed
# on another); the two folder paths are literal local filesystem paths,
# meaningless on another machine. Every other setting key defaults to
# portable, which is the right default for anything added here later.
LOCAL_SETTING_KEYS = frozenset({"secret_key", "asset_reports_folder", "id_files_folder"})

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

-- CCTV equipment (DVR/recorder + attached cameras/monitors) lives in its own
-- table rather than asset_items: it isn't assigned to a person (no user_id/
-- full_name/position/handover_date) and carries fields no PC/phone row ever
-- has (camera_count/hdd_count/hdd_capacity/location). Device/status/model
-- normalization still goes through the same alias tables as asset_items -
-- Settings' existing mapping pools cover CCTV values too.
CREATE TABLE IF NOT EXISTS cctv_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    asset_key TEXT NOT NULL,
    branch_dept TEXT,
    branch_no TEXT,
    device_name TEXT,
    device_name_raw TEXT,
    model_device TEXT,
    model_device_raw TEXT,
    manufacturer TEXT,
    serial_tag TEXT,
    status TEXT,
    status_raw TEXT,
    ip TEXT,
    camera_count TEXT,
    hdd_count TEXT,
    hdd_capacity TEXT,
    location TEXT,
    remark TEXT,
    source_file TEXT,
    -- The asset_items row this CCTV row was mirrored into at import (or the
    -- OA-sheet row with the same serial it was deduped against) - see
    -- importer._ingest_cctv_rows. Lets an edit on either side be copied to
    -- the other (sync_cctv_asset_link). NULL when there's no such row.
    asset_item_id INTEGER,
    FOREIGN KEY (batch_id) REFERENCES import_batches (id)
);

CREATE INDEX IF NOT EXISTS idx_cctv_items_batch ON cctv_items (batch_id);
CREATE INDEX IF NOT EXISTS idx_cctv_items_key ON cctv_items (asset_key);
CREATE INDEX IF NOT EXISTS idx_cctv_items_branch ON cctv_items (branch_no);
CREATE INDEX IF NOT EXISTS idx_cctv_items_branch_batch ON cctv_items (branch_no, batch_id);

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
    docx_path TEXT,
    created_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_handover_user ON handover_records (user_no);

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
    security_question_key TEXT,
    security_answer_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_role ON accounts (role_id);
"""

# Audit-trail tables, split into their own attached database (logsdb, see
# get_connection()) so they can be copied to a new machine independently of
# the business data in the main file (or left behind entirely). Every
# statement is qualified `logsdb.` - required for CREATE TABLE/INDEX (there's
# no "current database" to default to the way plain queries default to
# `main`), and kept qualified on purpose everywhere else too, for clarity.
LOGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS logsdb.network_check_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_at TEXT NOT NULL,
    branch_no TEXT,
    ip TEXT,
    asset_id INTEGER,
    field TEXT,
    old_value TEXT,
    new_value TEXT
);

CREATE TABLE IF NOT EXISTS logsdb.import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT,
    period TEXT,
    rows_processed INTEGER,
    result TEXT,
    imported_by TEXT
);

-- General-purpose audit trail for every hand-edit that doesn't already have
-- its own dedicated log table (imports have import_log, Network Check has
-- network_check_log) - Manage Assets edits/deletes, Settings/Mapping
-- changes, and Users & Roles changes. `category` groups entries for the
-- Activity Log page's filter (asset/mapping/settings/user_admin);
-- field/old_value/new_value are populated for a field-level edit and left
-- blank for a create/delete/action-only entry, where `target` alone
-- (plus `action`) already says what happened.
CREATE TABLE IF NOT EXISTS logsdb.activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    performed_by TEXT,
    category TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    field TEXT,
    old_value TEXT,
    new_value TEXT
);

CREATE INDEX IF NOT EXISTS logsdb.idx_activity_log_logged_at ON activity_log (logged_at);
CREATE INDEX IF NOT EXISTS logsdb.idx_activity_log_category ON activity_log (category);

CREATE INDEX IF NOT EXISTS logsdb.idx_import_log_imported_at ON import_log (imported_at);
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
    "edit_assets": "Edit/delete assets (Manage Assets, Manage CCTV)",
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
    "CISCO CP-7821", "CISCO CP-7911G", "CISCO CP-6921", "CISCO CP-6961", "CISCO CP-7941",
    "CISCO CP-7942", "CISCO CP-7962",
    # Yealink (IP phones) - a different brand mixed into the same IP PHONE
    # device category as the Cisco phones above; kept as its own manufacturer
    # section rather than folded into the Cisco aliases, same as every other
    # brand in this list.
    "YEALINK SIP-T19 E2",
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
    # CP-7821 - "-K9" is Cisco's own worldwide-locale SKU suffix (not a
    # different model), and "CISO" is a plain spelling typo of "CISCO" seen
    # directly in an imported file - both safe to fold in here, unlike a
    # genuinely different model number (see the module-level caution above).
    "CP-7821": "CISCO CP-7821",
    "CP - 7821": "CISCO CP-7821",
    "CISCO 7821": "CISCO CP-7821",
    "CP7821": "CISCO CP-7821",
    "CP 7821-K9": "CISCO CP-7821",
    "CISCO CP 7821-K9": "CISCO CP-7821",
    "CISCO CP7821-K9": "CISCO CP-7821",
    "CISO-7821": "CISCO CP-7821",
    "CISO 7821": "CISCO CP-7821",
    # 7822/7823/7824/7825 aren't real Cisco model numbers (confirmed against
    # Cisco's own RisPort70 model enum - see app/cucm.py's MODEL_NAMES) -
    # a sequential typo of 7821 seen across several imported files, not
    # genuinely different hardware, confirmed by the user rather than
    # guessed (see the module-level caution above on why that matters here).
    "CISCO CP-7822": "CISCO CP-7821",
    "CP - 7822": "CISCO CP-7821",
    "CP-7822": "CISCO CP-7821",
    "CP-7823": "CISCO CP-7821",
    "CP-7824": "CISCO CP-7821",
    "CP-7825": "CISCO CP-7821",
    "CP7822": "CISCO CP-7821",
    "CP7823": "CISCO CP-7821",
    "CP7824": "CISCO CP-7821",
    "CP7825": "CISCO CP-7821",
    # CP-7911G
    "CP - 7911": "CISCO CP-7911G",
    "CISCO 7911": "CISCO CP-7911G",
    "CP-7911G": "CISCO CP-7911G",
    "CP-7911": "CISCO CP-7911G",
    "CP7911": "CISCO CP-7911G",
    # CP-6921
    "CP - 6921": "CISCO CP-6921",
    "CISCO 6921": "CISCO CP-6921",
    "CP-6921": "CISCO CP-6921",
    "CP6921": "CISCO CP-6921",
    # CP-6961 - not previously a standard name at all, despite being seen
    # in real imported files (see the module-level comment above this list).
    "CP - 6961": "CISCO CP-6961",
    "CISCO 6961": "CISCO CP-6961",
    "CP-6961": "CISCO CP-6961",
    "CP6961": "CISCO CP-6961",
    # CP-7941
    "CP - 7941": "CISCO CP-7941",
    "CISCO 7941": "CISCO CP-7941",
    "CP-7941": "CISCO CP-7941",
    "CP7941": "CISCO CP-7941",
    # CP-7942
    "CP - 7942": "CISCO CP-7942",
    "CISCO 7942": "CISCO CP-7942",
    "CP-7942": "CISCO CP-7942",
    "CP7942": "CISCO CP-7942",
    "CP-7942G": "CISCO CP-7942",
    "CP7942G": "CISCO CP-7942",
    # CP-7962
    "CP - 7962": "CISCO CP-7962",
    "CISCO 7962": "CISCO CP-7962",
    "CP-7962": "CISCO CP-7962",
    "CP7962": "CISCO CP-7962",
    "CP-7962G": "CISCO CP-7962",
    "CP7962G": "CISCO CP-7962",
    # Yealink SIP-T19 E2 - confirmed against Yealink's own product page
    # (yealink.com/en/product-resource/ip-phone-t19pe2). The PoE variant is
    # officially "SIP-T19P E2", not aliased here since nothing in the real
    # data distinguishes which variant is actually in use.
    "T19 E2": "YEALINK SIP-T19 E2",
    "T19E2": "YEALINK SIP-T19 E2",
    "SIP-T19 E2": "YEALINK SIP-T19 E2",
    "YEALINK T19 E2": "YEALINK SIP-T19 E2",
    "SGI-SK5310": "SYNKEY SK5310",
    "SK5310": "SYNKEY SK5310",
    "SGI-SK5320": "SYNKEY SK5320",
    "SK5320": "SYNKEY SK5320",
}


def _migrate_legacy_single_file_db(data_dir: str) -> None:
    """One-time split of a pre-existing single `app.db` (business data +
    settings table + log tables all together) into the current data.db/
    logs.db/settings.json/local_settings.json layout - see LOGS_SCHEMA's and
    get_connection()'s comments above for why the split exists.

    Gated on file existence (old app.db present, new data.db absent) rather
    than a schema-version flag, matching every other one-time fixup in this
    file (e.g. _renormalize_handover_dates below) - the difference here is
    this one only needs to run once ever (subsequent boots see data.db
    already exists and return immediately), not on every boot.

    All the risky work happens on `.migrating` temp files; the original
    app.db is never touched until every step below has succeeded and
    committed, and even then it's renamed aside (app.db.pre-split-backup),
    never deleted. If this is interrupted at any point before the final
    renames, the next launch finds app.db untouched and new data.db still
    absent, and retries the whole thing from scratch - so a crash mid-split
    can never leave a half-migrated data.db that this function would
    mistake for "already done" and skip.
    """
    old_path = os.path.join(data_dir, "app.db")
    new_data_path = get_data_db_path()
    if not os.path.exists(old_path) or os.path.exists(new_data_path):
        return

    data_tmp = new_data_path + ".migrating"
    logs_tmp = get_logs_db_path() + ".migrating"
    settings_tmp = get_settings_path() + ".migrating"
    local_settings_tmp = get_local_settings_path() + ".migrating"
    for tmp in (data_tmp, logs_tmp, settings_tmp, local_settings_tmp):
        if os.path.exists(tmp):
            os.remove(tmp)

    try:
        # Checkpoint first: a plain file copy of app.db alone, without also
        # copying whatever's still only sitting in app.db-wal, could
        # silently drop the most recently committed rows.
        old_conn = sqlite3.connect(old_path)
        try:
            old_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            old_conn.close()

        shutil.copy2(old_path, data_tmp)

        conn = sqlite3.connect(data_tmp)
        try:
            conn.execute("ATTACH DATABASE ? AS logsdb", (logs_tmp,))
            conn.executescript(LOGS_SCHEMA)

            settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
            portable_settings = {}
            local_settings = {}
            for key, value in settings_rows:
                (local_settings if key in LOCAL_SETTING_KEYS else portable_settings)[key] = value

            for table in ("network_check_log", "import_log", "activity_log"):
                conn.execute(f"INSERT INTO logsdb.{table} SELECT * FROM {table}")
                conn.execute(f"DROP TABLE {table}")
            conn.execute("DROP TABLE settings")
            conn.commit()
        finally:
            conn.close()

        write_json_atomic(settings_tmp, portable_settings)
        write_json_atomic(local_settings_tmp, local_settings)

        # Finalize: only now does anything touch the real filenames.
        os.replace(data_tmp, new_data_path)
        os.replace(logs_tmp, get_logs_db_path())
        os.replace(settings_tmp, get_settings_path())
        os.replace(local_settings_tmp, get_local_settings_path())
        for suffix in ("-wal", "-shm"):
            leftover = old_path + suffix
            if os.path.exists(leftover):
                os.remove(leftover)
        os.replace(old_path, old_path + ".pre-split-backup")
    except Exception:
        for tmp in (data_tmp, logs_tmp, settings_tmp, local_settings_tmp):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        raise


def get_connection() -> sqlite3.Connection:
    data_dir = get_app_data_dir()
    # timeout: wait for a lock instead of failing immediately - matters once
    # several people on the network are using this at once and one of them
    # is mid-import (imports hold a write lock for the whole batch).
    conn = sqlite3.connect(get_data_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Audit-trail tables (import_log/activity_log/network_check_log) live in
    # their own physical file so they can be copied to a new machine
    # independently of the business data - see LOGS_SCHEMA's docstring. They
    # get attached to every connection so every existing query/transaction
    # that touches both a business table and a log table (imports, Network
    # Check's apply_updates, Settings' reset-imported-data) keeps working
    # unchanged: one connection, one commit, same atomicity as before the
    # split. Parameter-bound, not an f-string - this app's data dir can
    # contain spaces/apostrophes (OneDrive paths, user-redirected drives).
    conn.execute("ATTACH DATABASE ? AS logsdb", (get_logs_db_path(),))
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
        #
        # This PRAGMA must run AFTER the ATTACH above: an unqualified
        # `PRAGMA journal_mode = ...` applies to every currently-attached
        # database in one call, but only to the ones already attached at the
        # time it runs - attaching logsdb after this line would silently
        # leave it on SQLite's DELETE-mode default even on local disk.
        conn.execute("PRAGMA journal_mode = DELETE")
    else:
        # WAL lets readers (dashboard, lookup) proceed without blocking on a
        # concurrent writer (import, hand-over generation) - matters once
        # this is a shared server multiple people hit at the same time.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    _migrate_legacy_single_file_db(get_app_data_dir())
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.executescript(LOGS_SCHEMA)
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
        _clean_existing_ip_data(conn)
        _renormalize_model_device(conn)
        _renormalize_handover_dates(conn)
        for source_table in ("asset_items", "cctv_items"):
            backfill_unmapped(conn, source_table, "device_name", "device_aliases", "device_standard_names",
                               "device_unmapped", "raw_name")
            backfill_unmapped(conn, source_table, "status", "status_aliases", "status_standard_names",
                               "status_unmapped", "raw_status")
            backfill_unmapped(conn, source_table, "model_device", "model_aliases", "model_standard_names",
                               "model_unmapped", "raw_model")
        prune_stale_unmapped(conn)
        _backfill_user_no_norm(conn)
        _backfill_cctv_asset_links(conn)
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
    if "security_question_key" not in existing_account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN security_question_key TEXT")
    if "security_answer_hash" not in existing_account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN security_answer_hash TEXT")

    existing_import_log_cols = {
        row["name"] for row in conn.execute("PRAGMA logsdb.table_info(import_log)").fetchall()
    }
    if "imported_by" not in existing_import_log_cols:
        conn.execute("ALTER TABLE logsdb.import_log ADD COLUMN imported_by TEXT")

    existing_handover_cols = {row["name"] for row in conn.execute("PRAGMA table_info(handover_records)").fetchall()}
    if "created_by" not in existing_handover_cols:
        conn.execute("ALTER TABLE handover_records ADD COLUMN created_by TEXT")

    existing_cctv_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cctv_items)").fetchall()}
    if "asset_item_id" not in existing_cctv_cols:
        conn.execute("ALTER TABLE cctv_items ADD COLUMN asset_item_id INTEGER")
    # Same reason as idx_users_no_norm above - must run after the ALTER.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cctv_items_asset_item ON cctv_items (asset_item_id)")


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


def _backfill_cctv_asset_links(conn: sqlite3.Connection) -> None:
    """Set cctv_items.asset_item_id for rows imported before that column
    existed. The mirror batch is the asset_report batch created by the
    same import (same imported_at + source_files_json - see
    importer.import_asset_report). Within it:

    - a CCTV row with a serial links to the batch's one asset row with
      that serial (its mirror, or the OA-sheet row it was deduped against);
      skipped if there's no such row or more than one (e.g. a serial edited
      since import) rather than guessing.
    - a CCTV row with no serial was always mirrored, and mirrored rows are
      the last ones inserted into the batch, in the same order as the CCTV
      rows - so walk both from the end, pairing on the raw device/model/
      status text (never edited after import).

    Only looks at batches that still have unlinked rows, so it's close to
    a no-op on every startup after the first."""
    cctv_batches = conn.execute(
        """
        SELECT DISTINCT ib.id, ib.imported_at, ib.source_files_json
        FROM cctv_items ci JOIN import_batches ib ON ib.id = ci.batch_id
        WHERE ci.asset_item_id IS NULL AND ib.kind = 'cctv_report'
        """
    ).fetchall()
    if not cctv_batches:
        return
    claimed = {
        r["asset_item_id"] for r in conn.execute(
            "SELECT asset_item_id FROM cctv_items WHERE asset_item_id IS NOT NULL"
        ).fetchall()
    }

    def raw_key(r):
        return (r["device_name_raw"] or "", r["model_device_raw"] or "", r["status_raw"] or "")

    for batch in cctv_batches:
        mirror = conn.execute(
            "SELECT id FROM import_batches WHERE kind = 'asset_report' AND imported_at = ? "
            "AND source_files_json = ? ORDER BY id LIMIT 1",
            (batch["imported_at"], batch["source_files_json"]),
        ).fetchone()
        if mirror is None:
            continue
        asset_rows = conn.execute(
            "SELECT id, serial_tag, device_name_raw, model_device_raw, status_raw "
            "FROM asset_items WHERE batch_id = ? ORDER BY id",
            (mirror["id"],),
        ).fetchall()
        cctv_rows = conn.execute(
            "SELECT id, serial_tag, device_name_raw, model_device_raw, status_raw "
            "FROM cctv_items WHERE batch_id = ? AND asset_item_id IS NULL ORDER BY id",
            (batch["id"],),
        ).fetchall()

        by_serial: dict[str, list[int]] = {}
        for r in asset_rows:
            serial = (r["serial_tag"] or "").strip().upper()
            if serial:
                by_serial.setdefault(serial, []).append(r["id"])
        for c in cctv_rows:
            serial = (c["serial_tag"] or "").strip().upper()
            matches = by_serial.get(serial, []) if serial else []
            if len(matches) == 1:
                conn.execute("UPDATE cctv_items SET asset_item_id = ? WHERE id = ?", (matches[0], c["id"]))
                claimed.add(matches[0])

        candidates = [r for r in asset_rows if not (r["serial_tag"] or "").strip() and r["id"] not in claimed]
        for c in reversed([c for c in cctv_rows if not (c["serial_tag"] or "").strip()]):
            for i in range(len(candidates) - 1, -1, -1):
                if raw_key(candidates[i]) == raw_key(c):
                    conn.execute(
                        "UPDATE cctv_items SET asset_item_id = ? WHERE id = ?", (candidates[i]["id"], c["id"])
                    )
                    claimed.add(candidates[i]["id"])
                    del candidates[i]
                    break


# Fields both Manage Assets and Manage CCTV can edit, and that mean the same
# thing on both sides - an edit to one of these on a linked row is copied
# across (see cctv_items.asset_item_id).
CCTV_ASSET_SYNCED_FIELDS = (
    "device_name", "model_device", "serial_tag", "status", "branch_dept", "ip", "remark",
    # Not a form field - recomputed from branch_dept on edit
    # (importer.branch_for_edited_dept) and carried across with it.
    "branch_no",
)


def sync_cctv_asset_link(conn, source_table: str, row_id: int, changed: dict, performed_by: str = "") -> None:
    """Copy `changed` ({field: new value}, only fields that actually
    changed) from one side of a CCTV <-> asset link to the other, logging
    each copied field to the Activity Log under the other side's category.
    `source_table` is the table that was just edited. Fields outside
    CCTV_ASSET_SYNCED_FIELDS (user/handover fields, camera/HDD fields) are
    ignored. Doesn't commit."""
    fields = {f: v for f, v in changed.items() if f in CCTV_ASSET_SYNCED_FIELDS}
    if not fields:
        return
    if source_table == "cctv_items":
        link = conn.execute("SELECT asset_item_id FROM cctv_items WHERE id = ?", (row_id,)).fetchone()
        target_table, category, label, source_label = "asset_items", "asset", "Asset", "CCTV"
        action = "Edited asset"  # same action names Manage Assets/CCTV log themselves
        target_ids = [link["asset_item_id"]] if link and link["asset_item_id"] else []
    else:
        target_table, category, label, source_label = "cctv_items", "cctv", "CCTV", "Asset"
        action = "Edited CCTV item"
        target_ids = [
            r["id"] for r in conn.execute("SELECT id FROM cctv_items WHERE asset_item_id = ?", (row_id,)).fetchall()
        ]

    columns = ", ".join(fields)
    for target_id in target_ids:
        target = conn.execute(f"SELECT {columns} FROM {target_table} WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            continue
        diff = {f: v for f, v in fields.items() if (target[f] or "") != v}
        if not diff:
            continue
        set_clause = ", ".join(f"{f} = ?" for f in diff)
        conn.execute(f"UPDATE {target_table} SET {set_clause} WHERE id = ?", [*diff.values(), target_id])
        for f, v in diff.items():
            log_activity(
                conn, category, f"{action} (synced from {source_label} #{row_id})",
                performed_by=performed_by, target=f"{label} #{target_id}",
                field=f, old_value=target[f] or "", new_value=v,
            )


def backfill_unmapped(
    conn: sqlite3.Connection,
    source_table: str,
    source_column: str,
    alias_table: str,
    standard_table: str,
    unmapped_table: str,
    unmapped_column: str,
) -> int:
    """Visibility fix for device/status/model mapping: a row's value only
    ever gets flagged as unmapped at its own import time, so anything that
    bypasses that - a database upgraded from before this mapping system
    existed, or a value hand-typed into Manage Assets' edit form rather
    than imported - would stay invisible until that same branch happens to
    be re-imported again, maybe months later. This scans every distinct
    current value and queues any non-standard one into the Unmapped pool -
    cheap no-op once everything is either mapped or already queued
    (ON CONFLICT DO NOTHING, since this must never bump the occurrence
    count itself; only a real import row seen should do that). Called once
    per column here in init_db() (startup self-heal) and again on demand
    from Settings' "Check All for Unmapped Values" (routes/settings.py
    rescan_unmapped) - same function either way. Returns how many new
    entries were actually queued, so callers can report that count."""
    known_aliases = {row["alias"] for row in conn.execute(f"SELECT alias FROM {alias_table}").fetchall()}
    known_standards = {row["name"] for row in conn.execute(f"SELECT name FROM {standard_table}").fetchall()}
    rows = conn.execute(
        f"SELECT DISTINCT {source_column} AS v FROM {source_table} WHERE {source_column} != ''"
    ).fetchall()
    added = 0
    for row in rows:
        value = row["v"]
        if value not in known_aliases and value not in known_standards:
            cursor = conn.execute(
                f"""
                INSERT INTO {unmapped_table} ({unmapped_column}, first_seen_at, last_seen_at, occurrences)
                VALUES (?, datetime('now'), datetime('now'), 1)
                ON CONFLICT({unmapped_column}) DO NOTHING
                """,
                (value,),
            )
            if cursor.rowcount > 0:
                added += 1
    return added


def prune_stale_unmapped(conn: sqlite3.Connection) -> None:
    """Unmapped entries are a queue of raw values still needing an
    assignment - once the last asset carrying that value is deleted, edited
    away, or dropped by a re-import, the entry has nothing left to assign
    and should disappear too. Nothing else prunes this queue (import only
    ever adds to it, and delete_standard_name()/_status()/_model() only
    re-queue), so without this a value can sit in Settings > Unmapped
    forever pointing at zero actual assets, even though Manage Assets has
    nothing to show for it. Call after any operation that deletes or edits
    asset_items OR cctv_items rows (see asset_edit.py/cctv_edit.py) - both
    tables feed the same device/status/model unmapped queues, so a value
    only cctv_items still uses must survive a prune triggered by an
    asset_items-only edit, and vice versa. Plus once here in init_db() to
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
                UNION
                SELECT DISTINCT {source_column} FROM cctv_items WHERE {source_column} != ''
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


def _clean_existing_ip_data(conn: sqlite3.Connection) -> None:
    """One-time-per-value self-heal for `asset_items.ip` cells that predate
    text_utils.clean_ip's validation (e.g. an accidental double-dot typo, or
    free text like "DYNAMIC IP" typed into the IP column instead of an
    actual address) - same idea as _backfill_branch_names above. Without
    this, an already-imported bad value stays broken until that branch
    happens to be re-imported, and in the meantime Network Check keeps
    trying to ping literal garbage instead of a real, reachable machine.
    Cheap no-op once every stored value is already clean."""
    for row in conn.execute("SELECT DISTINCT ip FROM asset_items WHERE ip IS NOT NULL AND ip != ''").fetchall():
        old = row["ip"]
        new = clean_ip(old)
        if new != old:
            conn.execute("UPDATE asset_items SET ip = ? WHERE ip = ?", (new, old))


def _renormalize_model_device(conn: sqlite3.Connection) -> None:
    """One-time-per-value self-heal for `asset_items.model_device`: it's
    normalized against model_aliases at import time (see
    importer.normalize_model_device), but only using whatever aliases
    existed *then* - a row imported before an alias was added (e.g.
    "CP-7942G" sitting unmapped because the alias for it didn't exist yet)
    keeps that stale, un-normalized text forever otherwise, with nothing to
    ever re-check it. Re-applies the (possibly now more complete) alias
    table to every distinct existing value; cheap no-op once everything's
    already canonical. Must run before the model_device backfill_unmapped()
    call below, so a value this just resolved isn't queued into Unmapped."""
    for row in conn.execute(
        "SELECT DISTINCT model_device FROM asset_items WHERE model_device != ''"
    ).fetchall():
        old = row["model_device"]
        alias_row = conn.execute(
            "SELECT canonical_name FROM model_aliases WHERE alias = ?", (old.upper(),)
        ).fetchone()
        if alias_row and alias_row["canonical_name"] != old:
            conn.execute(
                "UPDATE asset_items SET model_device = ? WHERE model_device = ?",
                (alias_row["canonical_name"], old),
            )


def _renormalize_handover_dates(conn: sqlite3.Connection) -> None:
    """One-time-per-value self-heal for `asset_items.handover_date`: it's
    normalized at write time (importer.py, asset_edit.py, handover.py) into
    text_utils.normalize_handover_date's dd/mm/yyyy-or-"NA" convention, but a
    row written before that convention existed keeps its old raw form
    forever otherwise (typically ISO YYYY-MM-DD from the previous
    convention, or a bare blank/NULL) - same idea as _clean_existing_ip_data
    above. Re-applies the same normalizer to every distinct existing value,
    including blank/NULL ones (both become the explicit "NA" placeholder);
    cheap no-op once everything's already canonical. NULL needs its own
    branch since SQL's "x = NULL" never matches (see queries.py's own note
    on NULL-safe equality)."""
    for row in conn.execute("SELECT DISTINCT handover_date FROM asset_items").fetchall():
        old = row["handover_date"]
        new = normalize_handover_date(old)
        if new == old:
            continue
        if old is None:
            conn.execute("UPDATE asset_items SET handover_date = ? WHERE handover_date IS NULL", (new,))
        else:
            conn.execute("UPDATE asset_items SET handover_date = ? WHERE handover_date = ?", (new, old))


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


def _setting_path_for(key: str) -> str:
    return get_local_settings_path() if key in LOCAL_SETTING_KEYS else get_settings_path()


def get_setting_on(conn, key: str, default: str | None = None) -> str | None:
    """Same read as get_setting() - `conn` is accepted but unused, kept only
    so callers that batch a settings read alongside other `conn`-based work
    (e.g. routes/settings.py's save_general(), which also writes an
    activity_log entry) don't need two different call shapes. Settings live
    in settings.json/local_settings.json (see LOCAL_SETTING_KEYS), not a
    SQLite table, so there's nothing to actually read through `conn` for."""
    return read_json(_setting_path_for(key)).get(key, default)


def get_setting(key: str, default: str | None = None) -> str | None:
    return get_setting_on(None, key, default)


def set_setting_on(conn, key: str, value: str) -> None:
    """Same upsert as set_setting() - `conn` is accepted but unused, same
    reasoning as get_setting_on() above. Unlike the old SQLite-table version,
    this write is immediate, not deferred into the caller's transaction: a
    JSON file has no transaction to defer into. A caller that logs an
    activity_log entry for this same change and then fails before its own
    conn.commit() will end up with the setting changed but that one
    audit-log line rolled back - narrow (admin-only, rare action), accepted
    rather than re-plumbing every caller to write settings only after its
    own commit succeeds."""
    path = _setting_path_for(key)
    data = read_json(path)
    data[key] = value
    write_json_atomic(path, data)


def set_setting(key: str, value: str) -> None:
    set_setting_on(None, key, value)


def log_import(conn, kind: str, source_file: str, rows_processed: int | None = None,
                period: str = "", result: str = "OK", imported_by: str = "") -> None:
    """Record one row per import action (branch codes, user IDs, an asset
    report, a Total Asset baseline period) to a permanent audit log, viewable
    on the Import History page - independent of import_batches, which only
    exists for asset-report "current state"/diffing and is never shown as a
    plain list on its own.

    `imported_by` is the logged-in account's username - importer.py can't
    read that itself (it deliberately never talks to Flask/session state,
    see its module docstring), so every caller in routes/import_data.py
    passes it down from auth.current_account() instead."""
    conn.execute(
        "INSERT INTO logsdb.import_log (imported_at, kind, source_file, period, rows_processed, result, imported_by) "
        "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)",
        (kind, source_file, period, rows_processed, result, imported_by),
    )


def log_activity(
    conn, category: str, action: str, performed_by: str = "", target: str = "",
    field: str = "", old_value: str = "", new_value: str = "",
) -> None:
    """Record one row to the general-purpose activity_log - see its
    CREATE TABLE comment above for what belongs here vs. import_log/
    network_check_log. Doesn't commit - same convention as log_import,
    callers batch this into their own transaction."""
    conn.execute(
        "INSERT INTO logsdb.activity_log "
        "(logged_at, performed_by, category, action, target, field, old_value, new_value) "
        "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?)",
        (performed_by, category, action, target, field, old_value, new_value),
    )
