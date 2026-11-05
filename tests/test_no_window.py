"""No console window (2026-11-05): Quit, one copy at a time, stopping the server, AD from init-server."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_no_window_")

from app import auth, create_app, lifecycle, paths, server, server_config  # noqa: E402
from app.db import get_connection, get_setting, set_setting  # noqa: E402


def fresh_app():
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_no_window_")
    paths.use_server_data_dir(None)
    app = create_app()
    app.config["TESTING"] = True
    return app


class QuitTest(unittest.TestCase):
    def signed_in(self, app):
        client = app.test_client()
        with app.app_context():
            conn = get_connection()
            role = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()[0]
            conn.close()
            account = auth.create_account("admin", "Password-123", role)
        with client.session_transaction() as s:
            s["_csrf"] = "t"
        client.post("/login", data={"username": "admin", "password": "Password-123", "csrf_token": "t"})
        page = client.get("/settings/").get_data(as_text=True)  # signing in starts a new session, with a new token
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        return client, account

    def test_the_central_server_has_no_quit(self):
        app = fresh_app()
        client, _ = self.signed_in(app)
        with mock.patch.object(lifecycle, "quit_after_requests") as quit_:
            resp = client.post("/quit", data={"csrf_token": self.token})
        self.assertEqual(resp.status_code, 404)
        quit_.assert_not_called()
        self.assertNotIn("/quit", client.get("/settings/").get_data(as_text=True))

    def test_quit_button_on_a_pc_alone(self):
        app = fresh_app()
        app.config["QUIT_ENABLED"] = True
        client, _ = self.signed_in(app)
        self.assertIn('action="/quit"', client.get("/settings/").get_data(as_text=True))
        with mock.patch.object(lifecycle, "quit_after_requests") as quit_:
            resp = client.post("/quit", data={"csrf_token": self.token})
        self.assertEqual(resp.status_code, 200)
        quit_.assert_called_once()

    def test_quit_needs_the_settings_permission(self):
        app = fresh_app()
        app.config["QUIT_ENABLED"] = True
        client, _ = self.signed_in(app)
        with mock.patch("app.auth.current_permissions", return_value={"view_assets"}),                 mock.patch.object(lifecycle, "quit_after_requests") as quit_:
            resp = client.post("/quit", data={"csrf_token": self.token})
        self.assertEqual(resp.status_code, 302)  # the app sends it away with a message
        quit_.assert_not_called()

    def test_quit_needs_a_signed_in_user(self):
        app = fresh_app()
        app.config["QUIT_ENABLED"] = True
        client = app.test_client()
        with app.app_context():
            auth.create_account("admin", "Password-123", 1)
        with client.session_transaction() as s:
            s["_csrf"] = "t"
        with mock.patch.object(lifecycle, "quit_after_requests") as quit_:
            client.post("/quit", data={"csrf_token": "t"})
        quit_.assert_not_called()


class RunningFileTest(unittest.TestCase):
    def test_port_comes_back_then_is_forgotten(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_no_window_")
        self.assertIsNone(lifecycle.running_port())
        lifecycle.write_running(8740)
        self.assertEqual(lifecycle.running_port(), 8740)
        lifecycle.forget_running()
        self.assertIsNone(lifecycle.running_port())

    def test_a_broken_file_is_no_port(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_no_window_")
        lifecycle.write_running(8740)
        with open(lifecycle._running_path(), "w", encoding="utf-8") as f:
            f.write("{")
        self.assertIsNone(lifecycle.running_port())


class StopTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="am_stop_")
        self.config_file = os.path.join(self.dir, "server.json")
        self.data = os.path.join(self.dir, "data")
        server_config.write(self.data, 8811, self.config_file)

    def pid_file(self, text):
        with open(os.path.join(self.data, "server.pid"), "w", encoding="ascii") as f:
            f.write(text)

    def test_nothing_running(self):
        self.assertEqual(server.stop_server(self.config_file), "The server isn't running.")

    def test_an_old_pid_of_something_else_is_left_alone(self):
        self.pid_file("4242")
        answer = subprocess.CompletedProcess([], 0, stdout='"notepad.exe","4242","Console","1","10 K"\n')
        with mock.patch.object(server.subprocess, "run", return_value=answer) as run:
            self.assertEqual(server.stop_server(self.config_file), "The server isn't running.")
        self.assertEqual(run.call_count, 1)  # tasklist only, no taskkill
        self.assertFalse(os.path.exists(os.path.join(self.data, "server.pid")))

    def test_the_running_server_is_ended(self):
        self.pid_file("4242")
        answers = [subprocess.CompletedProcess([], 0, stdout='"AssetManagementTool.exe","4242"\n'),
                   subprocess.CompletedProcess([], 0, stdout="ok", stderr="")]
        with mock.patch.object(server.subprocess, "run", side_effect=answers) as run:
            self.assertEqual(server.stop_server(self.config_file), "The server is stopped.")
        self.assertEqual(run.call_args[0][0][:3], ["taskkill", "/PID", "4242"])

    def test_no_rights_says_so_and_keeps_the_file(self):
        self.pid_file("4242")
        answers = [subprocess.CompletedProcess([], 0, stdout='"AssetManagementTool.exe","4242"\n'),
                   subprocess.CompletedProcess([], 1, stdout="", stderr="Access is denied.")]
        with mock.patch.object(server.subprocess, "run", side_effect=answers):
            with self.assertRaisesRegex(RuntimeError, "elevated"):
                server.stop_server(self.config_file)
        self.assertTrue(os.path.exists(os.path.join(self.data, "server.pid")))

    def test_a_second_copy_doesnt_forget_the_first(self):
        config = server_config.load(self.config_file)
        self.pid_file("4242")
        server._forget_pid(config)
        self.assertTrue(os.path.exists(os.path.join(self.data, "server.pid")))
        server._write_pid(config)
        server._forget_pid(config)
        self.assertFalse(os.path.exists(os.path.join(self.data, "server.pid")))


class AdFromInitServerTest(unittest.TestCase):
    CONFIG = {"ad_group": "CORP\\AM Users", "ad_default_role": "Viewer"}

    def setUp(self):
        fresh_app()

    def test_turned_on_once(self):
        with mock.patch("app.winauth.available", return_value=True), \
                mock.patch("app.winauth.group_sid", return_value="S-1-5-21-1"):
            self.assertIn("AD sign-in on", auth.apply_server_ad(self.CONFIG))
        self.assertEqual((get_setting("ad_enabled"), get_setting("ad_group"), get_setting("ad_default_role")),
                         ("1", "CORP\\AM Users", "Viewer"))

    def test_settings_page_decides_after_that(self):
        set_setting("ad_enabled", "0")
        self.assertEqual(auth.apply_server_ad(self.CONFIG), "")
        self.assertEqual(get_setting("ad_enabled"), "0")

    def test_a_wrong_group_leaves_it_off(self):
        from app.winauth import AuthError
        with mock.patch("app.winauth.available", return_value=True), \
                mock.patch("app.winauth.group_sid", side_effect=AuthError("no such group")):
            self.assertIn("stays off", auth.apply_server_ad(self.CONFIG))
        self.assertIsNone(get_setting("ad_enabled"))

    def test_no_group_nothing_to_do(self):
        self.assertEqual(auth.apply_server_ad({"ad_group": ""}), "")

    def test_init_server_saves_it(self):
        folder = tempfile.mkdtemp(prefix="am_stop_")
        config_file = os.path.join(folder, "server.json")
        import main
        self.assertEqual(main.cli(["--config", config_file, "init-server", "--data-dir", os.path.join(folder, "d"),
                                   "--ad-group", "CORP\\AM Users", "--ad-role", "Viewer"]), 0)
        with open(config_file, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["ad_group"], saved["ad_default_role"]), ("CORP\\AM Users", "Viewer"))


if __name__ == "__main__":
    unittest.main()
