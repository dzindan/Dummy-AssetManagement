# -*- coding: utf-8 -*-
"""Legacy single-file app.db -> data.db/logs.db/settings.json/local_settings.json.

Before this split, the app only ever wrote one app.db (business tables + a
settings key/value table + the 3 log tables, all together). These tests
hand-build that exact legacy shape directly with sqlite3 (create_app()/
init_db() now only ever produce the new layout, so they can't be used to
seed it) and confirm: a clean migration on first boot, a crash mid-migration
never corrupts the original file and retries cleanly, a business write and a
log write in one connection/commit are still durable together post-split,
and Settings > Data Storage Location leaves local_settings.json behind.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import db as db_module  # noqa: E402
from app.db import LOCAL_SETTING_KEYS, get_connection, init_db, log_activity  # noqa: E402
from app.paths import (  # noqa: E402
    get_app_data_dir,
    get_data_db_path,
    get_local_settings_path,
    get_logs_db_path,
    get_settings_path,
    read_json,
)

LEGACY_SCHEMA = """
CREATE TABLE branches (branch_no TEXT PRIMARY KEY, local_name TEXT, eng_name TEXT, updated_at TEXT);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE network_check_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, applied_at TEXT NOT NULL, branch_no TEXT, ip TEXT,
    asset_id INTEGER, field TEXT, old_value TEXT, new_value TEXT
);
CREATE TABLE import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, imported_at TEXT NOT NULL, kind TEXT NOT NULL,
    source_file TEXT, period TEXT, rows_processed INTEGER, result TEXT, imported_by TEXT
);
CREATE TABLE activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, logged_at TEXT NOT NULL, performed_by TEXT,
    category TEXT NOT NULL, action TEXT NOT NULL, target TEXT, field TEXT, old_value TEXT, new_value TEXT
);
"""

LEGACY_SETTING_VALUES = {
    "secret_key": "legacy-secret-abc123",
    "asset_reports_folder": r"C:\old\reports",
    "id_files_folder": r"C:\old\ids",
    "ict_rep_name": "Nguyen Van A",
    "ict_rep_id": "IT001",
    "cucm_ip": "10.0.0.5",
}


def _seed_legacy_app_db(data_dir: str) -> str:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "app.db")
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
            "VALUES ('001', 'Test Branch', 'TEST BRANCH', datetime('now'))"
        )
        for key, value in LEGACY_SETTING_VALUES.items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.execute(
            "INSERT INTO network_check_log (applied_at, branch_no, ip, asset_id, field, old_value, new_value) "
            "VALUES (datetime('now'), '001', '10.0.0.1', 1, 'serial', 'OLD', 'NEW')"
        )
        conn.execute(
            "INSERT INTO import_log (imported_at, kind, source_file, rows_processed, result) "
            "VALUES (datetime('now'), 'asset_report', 'legacy.xlsx', 5, 'OK')"
        )
        conn.execute(
            "INSERT INTO activity_log (logged_at, performed_by, category, action) "
            "VALUES (datetime('now'), 'admin', 'settings', 'Legacy test entry')"
        )
        conn.commit()
    finally:
        conn.close()
    return path


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_splittest_")
        self.data_dir = get_app_data_dir()
        self.legacy_path = _seed_legacy_app_db(self.data_dir)

    def test_migration_splits_legacy_db_into_four_files(self):
        create_app()  # triggers init_db() -> _migrate_legacy_single_file_db()

        self.assertTrue(os.path.exists(get_data_db_path()))
        self.assertTrue(os.path.exists(get_logs_db_path()))
        self.assertTrue(os.path.exists(get_settings_path()))
        self.assertTrue(os.path.exists(get_local_settings_path()))
        self.assertFalse(os.path.exists(self.legacy_path))
        self.assertTrue(os.path.exists(self.legacy_path + ".pre-split-backup"))

        settings = read_json(get_settings_path())
        local_settings = read_json(get_local_settings_path())
        for key in LEGACY_SETTING_VALUES:
            if key in LOCAL_SETTING_KEYS:
                self.assertIn(key, local_settings)
                self.assertNotIn(key, settings)
            else:
                self.assertIn(key, settings)
                self.assertNotIn(key, local_settings)
        self.assertEqual(local_settings["secret_key"], "legacy-secret-abc123")
        self.assertEqual(settings["ict_rep_name"], "Nguyen Van A")

        conn = get_connection()
        try:
            branch = conn.execute("SELECT eng_name FROM branches WHERE branch_no = '001'").fetchone()
            self.assertEqual(branch["eng_name"], "TEST BRANCH")
            log_row = conn.execute("SELECT * FROM logsdb.network_check_log").fetchone()
            self.assertEqual(log_row["ip"], "10.0.0.1")
            import_row = conn.execute("SELECT * FROM logsdb.import_log").fetchone()
            self.assertEqual(import_row["source_file"], "legacy.xlsx")
            activity_row = conn.execute("SELECT * FROM logsdb.activity_log").fetchone()
            self.assertEqual(activity_row["action"], "Legacy test entry")
        finally:
            conn.close()

        # Idempotent: re-running against the already-migrated dir is a no-op.
        init_db()
        self.assertTrue(os.path.exists(get_data_db_path()))

    def test_interrupted_migration_retries_cleanly(self):
        # Let the two JSON-file writes (settings.json.migrating/
        # local_settings.json.migrating - still just temp files at this
        # point) go through, then fail on the very first rename that would
        # touch a real final filename (data.db itself).
        real_replace = os.replace
        calls = {"count": 0}

        def _flaky_replace(src, dst):
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("simulated crash during finalize")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=_flaky_replace):
            with self.assertRaises(OSError):
                db_module._migrate_legacy_single_file_db(self.data_dir)

        # Nothing that matters was touched: original app.db is intact, none
        # of the new files exist yet.
        self.assertTrue(os.path.exists(self.legacy_path))
        self.assertFalse(os.path.exists(get_data_db_path()))
        self.assertFalse(os.path.exists(get_logs_db_path()))
        self.assertFalse(os.path.exists(self.legacy_path + ".pre-split-backup"))
        for name in os.listdir(self.data_dir):
            self.assertFalse(name.endswith(".migrating"), f"leftover temp file: {name}")

        # A clean retry (no mocking) completes the migration from scratch.
        create_app()
        self.assertTrue(os.path.exists(get_data_db_path()))
        self.assertTrue(os.path.exists(get_logs_db_path()))
        self.assertFalse(os.path.exists(self.legacy_path))


class CrossDbAtomicityTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_splittest_")
        create_app()

    def test_business_write_and_log_write_are_durable_together(self):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO branches (branch_no, local_name, eng_name, updated_at) "
                "VALUES ('002', 'New Branch', 'NEW BRANCH', datetime('now'))"
            )
            log_activity(conn, "settings", "Test atomic write", performed_by="tester")
            conn.commit()
        finally:
            conn.close()

        # Visible through the normal attached-db connection...
        conn = get_connection()
        try:
            branch = conn.execute("SELECT eng_name FROM branches WHERE branch_no = '002'").fetchone()
            self.assertEqual(branch["eng_name"], "NEW BRANCH")
            row = conn.execute(
                "SELECT * FROM logsdb.activity_log WHERE action = 'Test atomic write'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

        # ...and opening logs.db directly (a separate physical file) confirms
        # it's really there, not just visible through the attachment.
        raw = sqlite3.connect(get_logs_db_path())
        try:
            row = raw.execute(
                "SELECT performed_by FROM activity_log WHERE action = 'Test atomic write'"
            ).fetchone()
            self.assertEqual(row[0], "tester")
        finally:
            raw.close()


class DataLocationMoveTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_splittest_")
        self.app = create_app()
        self.client = self.app.test_client()
        resp = self.client.post(
            "/setup",
            data={"username": "admin", "password": "adminpass123", "confirm": "adminpass123"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

    def test_local_settings_stay_behind_on_move(self):
        local_settings_before = read_json(get_local_settings_path())
        secret_before = local_settings_before["secret_key"]
        local_path_before = get_local_settings_path()

        new_dir = tempfile.mkdtemp(prefix="am_splittest_newdir_")
        resp = self.client.post("/settings/data-location", data={"new_data_dir": new_dir}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        self.assertTrue(os.path.exists(os.path.join(new_dir, "data.db")))
        self.assertTrue(os.path.exists(os.path.join(new_dir, "logs.db")))

        # local_settings.json never moves - still at the original fixed
        # default location, with the same secret_key.
        self.assertEqual(get_local_settings_path(), local_path_before)
        local_settings_after = read_json(get_local_settings_path())
        self.assertEqual(local_settings_after["secret_key"], secret_before)


if __name__ == "__main__":
    unittest.main()
