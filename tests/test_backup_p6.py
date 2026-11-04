# -*- coding: utf-8 -*-
"""P6/P7: the central server's backups (nightly, "Back up now", the CLI),
restoring one, and moving a PC's data onto the server with the row counts
checked table by table."""
import datetime as dt
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from app import backup, paths, server, server_config  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _add_branch(data_dir, branch_no, name):
    conn = sqlite3.connect(os.path.join(data_dir, "data.db"))
    try:
        conn.execute("INSERT INTO branches (branch_no, eng_name) VALUES (?, ?)", (branch_no, name))
        conn.commit()
    finally:
        conn.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="am_p6_")
        os.environ["LOCALAPPDATA"] = os.path.join(self.tmp, "profile")
        self.config_file = os.path.join(self.tmp, "conf", "server.json")
        self.data_dir = os.path.join(self.tmp, "AMData")
        self.backup_dir = os.path.join(self.tmp, "AMBackup")
        server_config.write(self.data_dir, _free_port(), self.config_file, host="127.0.0.1",
                            backup_dir=self.backup_dir, backup_keep=3)
        self.config = server_config.load(self.config_file)
        self.app, error = server.create_server_app(self.config)
        self.assertEqual(error, "")

    def tearDown(self):
        paths.use_server_data_dir(None)

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main.cli(["--config", self.config_file, *args])
        return code, out.getvalue()


class ConfigTests(Base):
    def test_backup_settings_checked(self):
        ok = server_config.validate({"data_dir": self.data_dir, "backup_dir": "\\\\fs01\\share\\b",
                                     "backup_time": "2:05"})
        self.assertEqual((ok["backup_dir"], ok["backup_time"], ok["backup_keep"]), ("\\\\fs01\\share\\b", "02:05", 14))
        for bad in ({"backup_time": "25:00"}, {"backup_time": "x"}, {"backup_dir": "relative\\x"},
                    {"backup_keep": 0}, {"backup_keep": "many"}):
            with self.assertRaises(server_config.ConfigError, msg=bad):
                server_config.validate({"data_dir": self.data_dir, **bad})

    def test_init_server_backup_dir(self):
        code, _ = self.cli("init-server", "--data-dir", self.data_dir, "--backup-dir", "E:\\AMBackup")
        self.assertEqual(code, 0)
        self.assertEqual(server_config.load(self.config_file)["backup_dir"], "E:\\AMBackup")


class BackupTests(Base):
    def test_backup_is_complete_and_checked(self):
        _add_branch(self.data_dir, "9001", "TEST BRANCH")
        os.makedirs(os.path.join(self.data_dir, "handovers", "2026"), exist_ok=True)
        with open(os.path.join(self.data_dir, "handovers", "2026", "form.docx"), "wb") as f:
            f.write(b"docx")
        with open(os.path.join(self.data_dir, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"ict_rep_name": "REP"}, f)
        os.makedirs(os.path.join(self.data_dir, "exports", "diff_reports"), exist_ok=True)
        for rel in ("exports/temp.xlsx", "exports/diff_reports/asset_diff_report_2026-08.xlsx",
                    "uploads/disposal_abc.xlsx", "uploads/0123456789abcdef.xlsx"):
            with open(os.path.join(self.data_dir, *rel.split("/")), "wb") as f:
                f.write(b"x")
        result = backup.run_backup(self.config, "manual", by="ann")
        self.assertTrue(result["path"].startswith(self.backup_dir))
        with zipfile.ZipFile(result["path"]) as z:
            names = set(z.namelist())
            manifest = json.loads(z.read("backup.json"))
            z.extract("data.db", os.path.join(self.tmp, "x"))
        # the files the database points to; not one-off downloads or imports waiting to be confirmed
        self.assertEqual(names, {"data.db", "logs.db", "settings.json", "handovers/2026/form.docx", "backup.json",
                                 "exports/diff_reports/asset_diff_report_2026-08.xlsx", "uploads/disposal_abc.xlsx"})
        # never the server's secret key, never temp files
        self.assertNotIn("local_settings.json", names)
        self.assertEqual(manifest["kind"], "manual")
        self.assertEqual(manifest["counts"]["data.db"]["branches"], 1)
        self.assertEqual(_count(os.path.join(self.tmp, "x", "data.db"), "branches"), 1)
        status = backup.read_status(self.data_dir)
        self.assertEqual((status["last_ok_kind"], status["last_by"], status["last_error"]), ("manual", "ann", ""))

    def test_rows_still_only_in_the_wal_are_in_the_backup(self):
        conn = sqlite3.connect(os.path.join(self.data_dir, "data.db"))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("INSERT INTO branches (branch_no, eng_name) VALUES ('9002', 'IN WAL')")
        conn.commit()  # connection kept open: the row is in data.db-wal only
        try:
            self.assertGreater(os.path.getsize(os.path.join(self.data_dir, "data.db-wal")), 0)
            result = backup.make_backup(self.data_dir, self.backup_dir)
        finally:
            conn.close()
        self.assertEqual(result["counts"]["data.db"]["branches"], 1)

    def test_each_kind_keeps_its_newest(self):
        start = dt.datetime(2026, 11, 1, 2, 0)
        for i in range(5):
            backup.make_backup(self.data_dir, self.backup_dir, "nightly", keep=3, now=start + dt.timedelta(days=i))
        backup.make_backup(self.data_dir, self.backup_dir, "manual", keep=3, now=start)
        open(os.path.join(self.backup_dir, "notes.txt"), "w").close()
        found = backup.list_backups(self.backup_dir)
        self.assertEqual([b["kind"] for b in found], ["nightly"] * 3 + ["manual"])
        self.assertEqual(found[0]["created"], start + dt.timedelta(days=4))
        self.assertTrue(os.path.exists(os.path.join(self.backup_dir, "notes.txt")))

    def test_leftovers_of_a_killed_run_cleared(self):
        old_work = os.path.join(self.data_dir, ".backup-killed")
        new_work = os.path.join(self.data_dir, ".restore-running")
        os.makedirs(old_work)
        os.makedirs(new_work)
        os.makedirs(self.backup_dir, exist_ok=True)
        part = os.path.join(self.backup_dir, backup.PREFIX + "2026-11-01_020000_nightly.zip.part")
        open(part, "w").close()
        stale = time.time() - backup.LEFTOVER_AFTER - 60
        for path in (old_work, part):
            os.utime(path, (stale, stale))
        backup.make_backup(self.data_dir, self.backup_dir)
        self.assertFalse(os.path.exists(old_work))
        self.assertFalse(os.path.exists(part))
        self.assertTrue(os.path.exists(new_work))  # maybe another process's, still at work

    def test_settings_list_cached_until_the_next_backup(self):
        self.assertEqual(backup.list_backups_cached(self.backup_dir), [])
        with mock.patch("app.backup.list_backups", side_effect=AssertionError("asked again")):
            self.assertEqual(backup.list_backups_cached(self.backup_dir), [])
        backup.make_backup(self.data_dir, self.backup_dir)
        self.assertEqual(len(backup.list_backups_cached(self.backup_dir)), 1)

    def test_failure_recorded(self):
        os.remove(os.path.join(self.data_dir, "data.db"))
        with self.assertRaises(backup.BackupError):
            backup.run_backup(self.config, "nightly")
        self.assertIn("not found", backup.read_status(self.data_dir)["last_error"])
        self.assertEqual(os.listdir(self.backup_dir), [])  # no half-made zip left


class NightlyTests(Base):
    def test_next_run(self):
        self.assertEqual(backup.next_run(dt.datetime(2026, 11, 3, 1, 0), "02:00"), dt.datetime(2026, 11, 3, 2, 0))
        self.assertEqual(backup.next_run(dt.datetime(2026, 11, 3, 2, 0), "02:00"), dt.datetime(2026, 11, 4, 2, 0))

    def test_catch_up_then_nightly_then_stop(self):
        now = [dt.datetime(2026, 11, 3, 1, 59, 0)]

        def clock():
            return now[0]

        stop = threading.Event()
        nightly = backup.Nightly(self.config, stop, clock)
        real_wait = stop.wait

        def fast_wait(seconds):  # time moves on as the thread waits
            now[0] += dt.timedelta(seconds=seconds)
            return real_wait(0.01)

        with mock.patch.object(stop, "wait", side_effect=fast_wait):
            nightly.start()
            end = time.time() + 20
            while time.time() < end and len(backup.list_backups(self.backup_dir)) < 2:
                time.sleep(0.05)
            stop.set()
            nightly.join(10)
        self.assertFalse(nightly.is_alive())
        made = [b["kind"] for b in backup.list_backups(self.backup_dir)]
        self.assertGreaterEqual(len(made), 2)  # catch-up (no backup yet), then 02:00
        self.assertEqual(set(made), {"nightly"})

    def test_no_catch_up_after_a_recent_backup(self):
        backup.run_backup(self.config, "manual")
        stop = threading.Event()
        stop.set()  # the first wait ends it
        nightly = backup.Nightly(self.config, stop)
        nightly.run()
        self.assertEqual([b["kind"] for b in backup.list_backups(self.backup_dir)], ["manual"])


class LoadTests(Base):
    def test_restore_brings_the_backup_back(self):
        _add_branch(self.data_dir, "9001", "KEPT")
        made = backup.run_backup(self.config, "manual")
        _add_branch(self.data_dir, "9003", "AFTER THE BACKUP")
        paths.use_server_data_dir(None)
        code, out = self.cli("restore", "--from", made["path"])
        self.assertEqual(code, 0, out)
        self.assertEqual(_count(os.path.join(self.data_dir, "data.db"), "branches"), 1)
        self.assertIn("Every table came over complete", out)
        pre = [b for b in backup.list_backups(self.backup_dir) if b["kind"] == "pre-restore"]
        self.assertEqual(len(pre), 1)  # what was replaced is kept
        with zipfile.ZipFile(pre[0]["path"]) as z:
            self.assertEqual(json.loads(z.read("backup.json"))["counts"]["data.db"]["branches"], 2)

    def test_not_while_the_server_runs(self):
        made = backup.run_backup(self.config, "manual")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.config["port"]))
            listener.listen()
            code, out = self.cli("restore", "--from", made["path"])
        self.assertEqual(code, 1)
        self.assertIn("stop it first", out)

    def _desktop_folder(self):
        """A PC's data folder (%LOCALAPPDATA%\\AssetManagementTool) with some data."""
        desktop = os.path.join(self.tmp, "desktop")
        os.environ["LOCALAPPDATA"] = desktop
        paths.use_server_data_dir(None)
        from app import create_app
        create_app(csrf=False)
        folder = os.path.join(desktop, paths.APP_FOLDER_NAME)
        _add_branch(folder, "1001", "DESKTOP BRANCH")
        _add_branch(folder, "1002", "DESKTOP BRANCH 2")
        with open(os.path.join(folder, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"ict_rep_name": "DESKTOP REP"}, f)
        return folder

    def test_import_a_desktop_folder(self):
        folder = self._desktop_folder()
        code, out = self.cli("import-desktop-data", "--from", folder)
        self.assertEqual(code, 0, out)
        self.assertEqual(_count(os.path.join(self.data_dir, "data.db"), "branches"), 2)
        with open(os.path.join(self.data_dir, "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["ict_rep_name"], "DESKTOP REP")
        self.assertRegex(out, r"branches\s+2\s+2")

    def test_import_a_desktop_backup_and_a_data_zip(self):
        folder = self._desktop_folder()
        os.environ["LOCALAPPDATA"] = os.path.join(self.tmp, "desktop")
        with redirect_stdout(io.StringIO()):
            code = main.cli(["backup", "--desktop", "--to", os.path.join(self.tmp, "out")])
        self.assertEqual(code, 0)
        made = backup.list_backups(os.path.join(self.tmp, "out"))
        self.assertEqual([b["kind"] for b in made], ["desktop"])
        # an old DATA zip: the files at the top, with the instructions text
        data_zip = os.path.join(self.tmp, "AssetManagementTool_DATA_2026-10-16_1247.zip")
        with zipfile.ZipFile(data_zip, "w") as z:
            for name in ("data.db", "logs.db", "settings.json"):
                z.write(os.path.join(folder, name), name)
            z.writestr("HUONG DAN KHOI PHUC DU LIEU.txt", "...")
            z.writestr("../evil.txt", "x")  # never written outside
        for source in (made[0]["path"], data_zip):
            code, out = self.cli("import-desktop-data", "--from", source, "--replace")
            self.assertEqual(code, 0, out)
            self.assertEqual(_count(os.path.join(self.data_dir, "data.db"), "branches"), 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "HUONG DAN KHOI PHUC DU LIEU.txt")))

    def test_import_refuses_to_replace_data_unasked(self):
        folder = self._desktop_folder()
        conn = sqlite3.connect(os.path.join(self.data_dir, "data.db"))
        conn.execute("INSERT INTO import_batches (kind, imported_at) VALUES ('asset', '2026-11-03')")
        conn.commit()
        conn.close()
        code, out = self.cli("import-desktop-data", "--from", folder)
        self.assertEqual(code, 1)
        self.assertIn("--replace", out)
        self.assertEqual(_count(os.path.join(self.data_dir, "data.db"), "branches"), 0)

    def test_bad_sources_refused(self):
        other = os.path.join(self.tmp, "other.db")
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE t (x)")
        conn.close()
        nodb = os.path.join(self.tmp, "nodb.zip")
        with zipfile.ZipFile(nodb, "w") as z:
            z.writestr("readme.txt", "x")
        notam = os.path.join(self.tmp, "notam.zip")
        with zipfile.ZipFile(notam, "w") as z:
            z.write(other, "data.db")
        for source, message in ((nodb, "no data.db"), (notam, "isn't this app's database"),
                                (os.path.join(self.tmp, "missing"), "neither a folder nor a zip")):
            code, out = self.cli("import-desktop-data", "--from", source)
            self.assertEqual(code, 1, source)
            self.assertIn(message, out)

    def test_import_keeps_the_servers_ad_sign_in(self):
        with open(os.path.join(self.data_dir, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"ad_enabled": "1", "ad_domain": "CORP", "ad_group": "AM Users", "ict_rep_name": "SERVER"}, f)
        folder = self._desktop_folder()
        code, out = self.cli("import-desktop-data", "--from", folder)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.data_dir, "settings.json"), encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["ad_enabled"], saved["ad_domain"], saved["ad_group"], saved["ict_rep_name"]),
                         ("1", "CORP", "AM Users", "DESKTOP REP"))
        self.assertIn("Kept the server's own AD sign-in settings", out)
        # a restore brings back the backup's settings as they were
        made = backup.run_backup(self.config, "manual")
        with open(os.path.join(self.data_dir, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"ad_enabled": "0"}, f)
        paths.use_server_data_dir(None)
        self.assertEqual(self.cli("restore", "--from", made["path"])[0], 0)
        with open(os.path.join(self.data_dir, "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["ad_enabled"], "1")

    def test_half_done_swap_says_how_to_undo(self):
        made = backup.run_backup(self.config, "manual")
        real_replace = os.replace

        def failing(src, dst):
            if dst == os.path.join(self.data_dir, "logs.db"):  # the swap, after data.db went in
                raise PermissionError("logs.db is in use")
            return real_replace(src, dst)

        with mock.patch("app.backup.os.replace", side_effect=failing):
            code, out = self.cli("restore", "--from", made["path"])
        self.assertEqual(code, 1)
        self.assertIn("half replaced", out)
        self.assertIn("restore --from", out)
        self.assertIn("pre-restore", out)

    def test_kept_files_come_over(self):
        folder = self._desktop_folder()
        os.makedirs(os.path.join(folder, "exports", "diff_reports"), exist_ok=True)
        for rel in ("exports/diff_reports/r.xlsx", "uploads/disposal_1.xlsx", "uploads/pending.xlsx",
                    "handovers/HANDOVER_1.docx"):
            with open(os.path.join(folder, *rel.split("/")), "wb") as f:
                f.write(b"x")
        code, out = self.cli("import-desktop-data", "--from", folder)
        self.assertEqual(code, 0, out)
        for rel in ("exports/diff_reports/r.xlsx", "uploads/disposal_1.xlsx", "handovers/HANDOVER_1.docx"):
            self.assertTrue(os.path.isfile(os.path.join(self.data_dir, *rel.split("/"))), rel)
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "uploads", "pending.xlsx")))
        self.assertIn("uploads/disposal_* (1)", out)

    def test_nothing_replaced_said_so(self):
        made = backup.run_backup(self.config, "manual")
        real_replace = os.replace

        def failing(src, dst):
            if dst == os.path.join(self.data_dir, "data.db"):
                raise PermissionError("data.db is in use")
            return real_replace(src, dst)

        with mock.patch("app.backup.os.replace", side_effect=failing):
            code, out = self.cli("restore", "--from", made["path"])
        self.assertEqual(code, 1)
        self.assertIn("nothing was replaced", out)

    def test_damaged_database_refused_before_anything(self):
        bad = os.path.join(self.tmp, "bad.zip")
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("data.db", b"this is not a database at all" * 100)
        _add_branch(self.data_dir, "9001", "KEEP")
        code, out = self.cli("restore", "--from", bad)
        self.assertEqual(code, 1)
        self.assertIn("can't be copied", out)
        self.assertEqual(_count(os.path.join(self.data_dir, "data.db"), "branches"), 1)
        self.assertEqual(backup.list_backups(self.backup_dir), [])  # refused before the pre-restore backup

    def test_read_only_copy_falls_back(self):
        calls = []
        real = backup._copy_db

        def flaky(src, dest, read_only):
            calls.append(read_only)
            if read_only:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return real(src, dest, read_only)

        with mock.patch("app.backup._copy_db", side_effect=flaky):
            backup.make_backup(self.data_dir, self.backup_dir)
        self.assertEqual(calls[:2], [True, False])

    def test_reconcile_flags_lost_rows(self):
        rows = backup.reconcile({"data.db": {"asset_items": 10, "model_unmapped": 5}, "logs.db": {"activity_log": 3}},
                                {"data.db": {"asset_items": 9, "model_unmapped": 2}, "logs.db": {"activity_log": 4}})
        self.assertEqual({r["table"]: r["state"] for r in rows},
                         {"asset_items": "MISMATCH", "model_unmapped": "rebuilt", "activity_log": "ok"})


class PageTests(Base):
    def setUp(self):
        super().setUp()
        self.app.config["CSRF_ENABLED"] = False
        from app.auth import create_account
        from app.db import get_connection
        conn = get_connection()
        admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
        conn.close()
        create_account("ann", "annpass12", admin)
        self.client = self.app.test_client()
        self.client.post("/login?form=1", data={"username": "ann", "password": "annpass12"})

    def test_settings_shows_backups_and_backs_up_now(self):
        page = self.client.get("/settings/").get_data(as_text=True)
        self.assertIn("No backup yet.", page)
        self.assertIn(self.backup_dir, page)
        resp = self.client.post("/settings/backup-now")
        self.assertEqual(resp.status_code, 302)
        made = backup.list_backups(self.backup_dir)
        self.assertEqual([b["kind"] for b in made], ["manual"])
        page = self.client.get("/settings/").get_data(as_text=True)
        self.assertIn(made[0]["name"], page)
        self.assertIn("Last backup:", page)
        self.assertNotIn("same disk as the database", page)

    def test_hand_over_form_found_after_the_data_moved(self):
        from app.db import get_connection
        name = "HANDOVER_HO-000001_U1_20261001_100000.docx"
        with open(os.path.join(self.data_dir, "handovers", name), "wb") as f:
            f.write(b"docx")
        conn = get_connection()
        old_path = "C:\\Users\\someone\\AppData\\Local\\AssetManagementTool\\handovers\\" + name
        conn.execute("INSERT INTO handover_records (created_at, docx_path) VALUES ('2026-10-01', ?)", (old_path,))
        record_id = conn.execute("SELECT MAX(id) FROM handover_records").fetchone()[0]
        conn.execute("INSERT INTO handover_records (created_at, docx_path) VALUES ('2026-10-01', ?)",
                     ("C:\\gone\\..",))
        bad_id = conn.execute("SELECT MAX(id) FROM handover_records").fetchone()[0]
        conn.commit()
        conn.close()
        resp = self.client.get(f"/history/download/{record_id}")
        self.assertEqual((resp.status_code, resp.data), (200, b"docx"))
        resp.close()
        self.assertEqual(self.client.get(f"/history/download/{bad_id}").status_code, 404)

    def test_saved_diff_report_found_after_the_data_moved(self):
        from app.db import get_connection
        name = "asset_diff_report_2026-08_20261001-175447.xlsx"
        with open(os.path.join(paths.get_diff_reports_dir(), name), "wb") as f:
            f.write(b"xlsx")
        conn = get_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(diff_reports)")]
        values = {c: "x" for c in cols if c != "id"}
        values["file_path"] = "C:\\Users\\someone\\AppData\\Local\\AssetManagementTool\\exports\\diff_reports\\" + name
        conn.execute(f"INSERT INTO diff_reports ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                     list(values.values()))
        report_id = conn.execute("SELECT MAX(id) FROM diff_reports").fetchone()[0]
        conn.commit()
        conn.close()
        resp = self.client.get(f"/import/diff-reports/{report_id}/download")
        self.assertEqual((resp.status_code, resp.data), (200, b"xlsx"))
        resp.close()

    def test_warns_when_backups_share_the_disk(self):
        self.app.config["SERVER_CONFIG"]["backup_dir"] = ""
        page = self.client.get("/settings/").get_data(as_text=True)
        self.assertIn("same disk as the database", page)


if __name__ == "__main__":
    unittest.main()
