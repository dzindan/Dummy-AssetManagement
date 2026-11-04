# -*- coding: utf-8 -*-
"""P2: on the central server the pages leave out what only works on the PC
running the app (its import folders, moving / opening its data folder),
files come in by upload (also a whole folder picked in the browser), and
the import confirmation pages can only ever touch this import's own
uploaded files - never another file of the server."""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, paths  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection, get_setting, set_setting  # noqa: E402


def _role_id(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


class Base(unittest.TestCase):
    server = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="am_p2_")
        os.environ["LOCALAPPDATA"] = os.path.join(self.tmp, "local")
        if self.server:
            paths.use_server_data_dir(os.path.join(self.tmp, "AMData"))
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        create_account("admin", "adminpass1", _role_id("Admin"))
        self.client.post("/login?form=1", data={"username": "admin", "password": "adminpass1"})

    def tearDown(self):
        paths.use_server_data_dir(None)


class ServerModeTests(Base):
    server = True

    def test_settings_page_leaves_out_desktop_only_parts(self):
        html = self.client.get("/settings/").get_data(as_text=True)
        self.assertNotIn("Default Import Folders", html)
        self.assertNotIn("Change Location", html)
        self.assertNotIn("Open Data Folder", html)
        self.assertIn("on the server", html)
        self.assertIn(os.path.join(self.tmp, "AMData"), html)

    def test_desktop_only_routes_are_refused(self):
        with mock.patch("os.startfile", create=True) as startfile:
            resp = self.client.post("/settings/open-data-folder", follow_redirects=True)
        startfile.assert_not_called()
        self.assertIn("Not on the central server", resp.get_data(as_text=True))
        other = os.path.join(self.tmp, "elsewhere")
        self.client.post("/settings/data-location", data={"new_data_dir": other})
        self.assertFalse(os.path.exists(other))
        self.assertEqual(paths.get_app_data_dir(), os.path.join(self.tmp, "AMData"))

    def test_folder_settings_and_folder_import_are_off(self):
        set_setting("asset_reports_folder", self.tmp)
        self.client.post("/settings/general", data={"ict_rep_name": "A", "asset_reports_folder": "C:\\x"})
        self.assertEqual(get_setting("asset_reports_folder"), self.tmp)  # not changed from the page
        html = self.client.get("/import/").get_data(as_text=True)
        self.assertNotIn("Import all from folder", html)
        self.assertIn("Choose a folder", self.client.get("/static/app.js").get_data(as_text=True))
        self.assertIn("data-folder-pick", html)
        resp = self.client.post("/import/from-folder", data={"kind": "asset_reports"}, follow_redirects=True)
        self.assertIn("isn&#39;t available on the central server", resp.get_data(as_text=True))

    def test_server_mode_flag_reaches_templates(self):
        html = self.client.get("/settings/").get_data(as_text=True)
        self.assertIn("This is the central server", html)


class UploadTests(Base):
    def test_confirm_pages_never_touch_another_file(self):
        victim = os.path.join(self.tmp, "important.xlsx")
        with open(victim, "wb") as f:
            f.write(b"keep me")
        with mock.patch("app.routes.import_data._run_asset_report_imports", return_value=[]) as run:
            self.client.post("/import/asset-reports/confirm", data={
                "period": "2026-10", "source": "upload", "group_count": "1",
                "choice_0": f"{victim}::x.xlsx", "group_files_0": victim,
                "unique_files": f"..\\..\\{os.path.basename(victim)}::y.xlsx"})
            self.client.post("/import/asset-reports/confirm-period", data={
                "period": "2026-10", "source": "upload", "files": f"{victim}::x.xlsx", "mode": "all"})
        self.assertTrue(os.path.exists(victim))
        for call in run.call_args_list:
            self.assertEqual(call.args[0], [])  # nothing outside the uploads folder reaches the importer

    def test_an_uploads_own_copy_is_still_accepted(self):
        name = "0123456789abcdef0123456789abcdef.xlsx"
        copy = os.path.join(paths.get_uploads_dir(), name)
        with open(copy, "wb") as f:
            f.write(b"x")
        with mock.patch("app.routes.import_data._run_asset_report_imports", return_value=[]) as run, \
                mock.patch("app.routes.import_data._check_period_conflicts", return_value=[]):
            self.client.post("/import/asset-reports/confirm", data={
                "period": "2026-10", "source": "upload", "group_count": "0",
                "unique_files": f"C:\\anything\\{name}::report.xlsx"})
        self.assertEqual(run.call_args.args[0], [{"filename": "report.xlsx", "path": copy}])

    def test_folder_source_only_inside_the_configured_folder(self):
        folder = os.path.join(self.tmp, "Asset reports")
        os.makedirs(folder)
        inside = os.path.join(folder, "a.xlsx")
        open(inside, "wb").close()
        set_setting("asset_reports_folder", folder)
        from app.routes import import_data
        with self.app.test_request_context():
            self.assertEqual(import_data._checked_path(inside, "folder"), os.path.realpath(inside))
            self.assertIsNone(import_data._checked_path(os.path.join(folder, "..", "b.xlsx"), "folder"))
            self.assertIsNone(import_data._checked_path(os.path.join(self.tmp, "local", "x.db"), "folder"))

    def test_non_excel_files_of_a_picked_folder_are_left_out(self):
        with mock.patch("app.routes.import_data.import_id_file",
                        return_value={"file_type": "branches", "rows": 0}) as imp:
            resp = self.client.post("/import/id-files", data={"files": [
                (io.BytesIO(b"x"), "IDFromAither/branches.xlsx"),
                (io.BytesIO(b"x"), "IDFromAither/notes.txt")]}, content_type="multipart/form-data",
                                    follow_redirects=True)
        self.assertEqual(imp.call_count, 1)
        html = resp.get_data(as_text=True)
        self.assertIn("branches", html)
        self.assertIn("aren&#39;t Excel", html)
        self.assertEqual(os.listdir(paths.get_uploads_dir()), [])  # temp copies removed

    def test_desktop_mode_keeps_its_folder_features(self):
        html = self.client.get("/settings/").get_data(as_text=True)
        self.assertIn("Default Import Folders", html)
        self.assertIn("Change Location", html)


if __name__ == "__main__":
    unittest.main()
