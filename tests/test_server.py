# -*- coding: utf-8 -*-
"""The central server (client/server mode): server.json, the data folder
it puts the database in, /health, and the real cheroot server answering
over a socket."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import paths, server, server_config  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="am_servertest_")
        os.environ["LOCALAPPDATA"] = os.path.join(self.tmp, "profile")  # must stay unused in server mode
        self.config_file = os.path.join(self.tmp, "conf", "server.json")
        self.data_dir = os.path.join(self.tmp, "AMData")

    def tearDown(self):
        paths.use_server_data_dir(None)


class ConfigTests(ServerTestBase):
    def test_init_server_writes_a_checked_config(self):
        path = server_config.write(self.data_dir, 8800, self.config_file)
        self.assertEqual(path, self.config_file)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual((saved["data_dir"], saved["port"], saved["host"]), (self.data_dir, 8800, "0.0.0.0"))
        self.assertTrue(os.path.isdir(self.data_dir))
        self.assertEqual(server_config.load(self.config_file)["port"], 8800)

    def test_the_database_never_goes_on_a_network_share(self):
        for bad in ("\\\\FS01\\AMData", "//fs01/am", "AMData", ""):
            with self.assertRaises(server_config.ConfigError, msg=bad):
                server_config.validate({"data_dir": bad})

    def test_bad_values_refused(self):
        for bad in ({"port": 0}, {"port": "x"}, {"threads": 0}, {"tls_cert": "C:\\nope.pem"}):
            with self.assertRaises(server_config.ConfigError, msg=bad):
                server_config.validate({"data_dir": self.data_dir, **bad})

    def test_no_config_says_how_to_make_one(self):
        with self.assertRaisesRegex(server_config.ConfigError, "init-server"):
            server_config.load(os.path.join(self.tmp, "none.json"))


class ServerAppTests(ServerTestBase):
    def test_the_data_goes_in_the_servers_folder_not_the_profile(self):
        server_config.write(self.data_dir, explicit=self.config_file)
        app, error = server.create_server_app(server_config.load(self.config_file))
        self.assertEqual(error, "")
        self.assertTrue(app.config["SERVER_MODE"])
        for name in ("data.db", "logs.db", "local_settings.json"):
            self.assertTrue(os.path.exists(os.path.join(self.data_dir, name)), name)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "profile", paths.APP_FOLDER_NAME, "data.db")))
        # /health without a login; the pages still ask for one
        client = app.test_client()
        resp = client.get("/health")
        self.assertEqual((resp.status_code, resp.get_json()["status"]), (200, "ok"))
        self.assertEqual(client.get("/").status_code, 302)

    def test_no_silent_fallback_to_another_folder(self):
        paths.use_server_data_dir(self.data_dir)
        self.assertEqual(paths.get_app_data_dir_status()["path"], self.data_dir)
        self.assertEqual(paths.get_local_settings_path(), os.path.join(self.data_dir, "local_settings.json"))


class InstallTaskTests(ServerTestBase):
    """install-task: schtasks + the PowerShell that lifts the 72 h limit
    (subprocess mocked - nothing is registered)."""

    def _run(self, ps_returncode=0):
        from unittest import mock
        server_config.write(self.data_dir, 8800, self.config_file)
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            rc = ps_returncode if args[0] == "powershell" else 0
            return mock.Mock(returncode=rc, stdout="", stderr="Access is denied." if rc else "")

        with mock.patch.object(server.subprocess, "run", side_effect=fake_run):
            done = server.install_task("CORP\svc-am", "S3cret'pw", self.config_file)
        return calls, done

    def test_password_never_on_a_command_line(self):
        calls, done = self._run()
        ps = [kw for args, kw in calls if args[0] == "powershell"]
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]["env"]["AM_TASK_PASSWORD"], "S3cret'pw")
        self.assertEqual(ps[0]["env"]["AM_TASK_USER"], "CORP\svc-am")
        ps_args = [args for args, kw in calls if args[0] == "powershell"][0]
        self.assertNotIn("S3cret", " ".join(ps_args))
        self.assertIn("-Password $env:AM_TASK_PASSWORD", " ".join(ps_args))
        self.assertTrue(any("No time limit" in line for line in done), done)

    def test_a_failed_time_limit_change_is_reported(self):
        _calls, done = self._run(ps_returncode=1)
        warning = [line for line in done if line.startswith("WARNING")]
        self.assertEqual(len(warning), 1, done)
        self.assertIn("3 days", warning[0])
        self.assertIn("Access is denied", warning[0])


class TaskUserTests(unittest.TestCase):
    def test_reads_the_account_from_the_task_xml(self):
        from unittest import mock
        xml = "<Task><Principals><Principal id=\"Author\"><UserId>CORP\svc-am</UserId></Principal></Principals></Task>"
        with mock.patch.object(server.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=xml)):
            self.assertEqual(server.task_user(), "CORP\svc-am")
        with mock.patch.object(server.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(server.task_user(), "")

    def test_grant_key_takes_a_sid_as_star_sid(self):
        from unittest import mock
        from app import tls
        key = tempfile.NamedTemporaryFile(delete=False)
        key.close()
        with mock.patch.object(tls, "_icacls", return_value=True) as icacls:
            self.assertTrue(tls.grant_key(key.name, "S-1-5-21-1-2-3-1105"))
        icacls.assert_called_once_with(key.name, "/grant", "*S-1-5-21-1-2-3-1105:R")
        os.unlink(key.name)


class RealServerTests(ServerTestBase):
    def test_serve_answers_over_the_network_and_stops(self):
        port = _free_port()
        server_config.write(self.data_dir, port, self.config_file, host="127.0.0.1")
        config = server_config.load(self.config_file)
        app, _ = server.create_server_app(config)
        srv = server.make_server(config, app)
        thread = threading.Thread(target=srv.start, daemon=True)
        thread.start()
        try:
            body = None
            for _ in range(50):
                try:
                    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).read()
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.1)
            self.assertEqual(json.loads(body)["status"], "ok")
            # several at once (cheroot threads, not the one-at-a-time dev server)
            results = []

            def fetch():
                results.append(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10).status)

            workers = [threading.Thread(target=fetch) for _ in range(8)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
            self.assertEqual(results, [200] * 8)
            # an upload over the limit gets the 413 page, not a reset connection
            big = b"x" * (app.config["MAX_CONTENT_LENGTH"] + 1)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/setup", data=big, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=30)
            self.assertEqual(ctx.exception.code, 413)
        finally:
            srv.stop()
            thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_cli_init_server(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import main
        self.assertEqual(main.cli(["--config", self.config_file, "init-server", "--data-dir", self.data_dir,
                                   "--port", "8811"]), 0)
        self.assertEqual(server_config.load(self.config_file)["port"], 8811)
        self.assertEqual(main.cli(["--config", self.config_file, "init-server", "--data-dir", "\\\\fs\\x"]), 2)


if __name__ == "__main__":
    unittest.main()
