# -*- coding: utf-8 -*-
"""P4b: HTTPS with a self-signed certificate (a real cheroot TLS server),
CSRF tokens, the session cookie flags, sign-in / forgot-password lockout,
sessions ending on a password change, and the 2026-09-22 audit fixes (the
CUCM AXL password never on the page, CUCM's certificate pinned, the diff
report file name)."""
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, server, server_config, throttle, tls  # noqa: E402
from app.auth import create_account, set_security_question  # noqa: E402
from app.db import get_connection, get_setting, set_setting  # noqa: E402


def _role_id(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    csrf = False

    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_p4b_")
        self.app = create_app(csrf=self.csrf)
        self.client = self.app.test_client()
        self.admin_id = create_account("admin", "adminpass1", _role_id("Admin"))

    def login(self, client=None, username="admin", password="adminpass1"):
        return (client or self.client).post("/login?form=1", data={"username": username, "password": password})


class CsrfTests(Base):
    csrf = True

    def token(self, client=None):
        html = (client or self.client).get("/login?form=1").get_data(as_text=True)
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def test_post_without_the_token_is_refused(self):
        resp = self.client.post("/login?form=1", data={"username": "admin", "password": "adminpass1"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Please try again", resp.get_data(as_text=True))

    def test_form_field_and_header_both_work(self):
        token = self.token()
        resp = self.client.post("/login?form=1", data={"username": "admin", "password": "adminpass1",
                                                       "csrf_token": token})
        self.assertEqual(resp.status_code, 302)
        # sign-in starts a new session: a new token, from the next page
        html = self.client.get("/settings/").get_data(as_text=True)
        token2 = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        self.assertNotEqual(token, token2)
        resp = self.client.post("/settings/cucm", data={"ip": "10.0.0.5"}, headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())
        resp = self.client.post("/settings/cucm", data={"ip": "10.0.0.5"}, headers={"X-CSRF-Token": token2})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(get_setting("cucm_ip"), "10.0.0.5")

    def test_every_page_has_the_token_and_script(self):
        token = self.token()
        self.client.post("/login?form=1", data={"username": "admin", "password": "adminpass1", "csrf_token": token})
        for url in ("/", "/settings/", "/settings/users/"):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn('name="csrf-token"', html, url)
            self.assertIn("csrf.js", html, url)
        for url in ("/forgot-password",):
            self.assertIn("csrf.js", self.app.test_client().get(url).get_data(as_text=True))

    def test_cookie_flags_and_headers(self):
        resp = self.client.get("/login?form=1")
        cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)  # plain-HTTP desktop mode
        self.assertEqual(resp.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")


class LockoutTests(Base):
    def test_five_wrong_passwords_lock_the_name_even_the_right_password(self):
        for _ in range(4):
            self.assertIn("Incorrect", self.login(password="bad").get_data(as_text=True))
        resp = self.login(password="bad")
        self.assertEqual(resp.status_code, 429)
        resp = self.login()  # right password, still refused
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Too many failed attempts", resp.get_data(as_text=True))
        # another name from the same address isn't locked yet
        create_account("bob", "bobpass12", _role_id("Viewer"))
        self.assertEqual(self.login(username="bob", password="bobpass12").status_code, 302)

    def test_lock_ends_and_success_clears_the_count(self):
        clock = [1000.0]
        t = throttle.Throttle(limit=3, window=60, lock=60, clock=lambda: clock[0])
        t.fail("a"), t.fail("a")
        t.clear("a")
        self.assertFalse(t.fail("a"))
        t.fail("a")
        self.assertTrue(t.fail("a"))
        self.assertGreater(t.wait_seconds("a"), 0)
        clock[0] += 61
        self.assertEqual(t.wait_seconds("a"), 0)

    def test_name_variants_are_one_name(self):
        self.assertEqual({throttle.name_key(n) for n in ("CORP\\Nva", "nva@corp.local", " nva ")}, {"nva"})

    def test_a_locked_ad_name_is_not_tried_in_windows(self):
        from app import winauth
        for k, v in (("ad_enabled", "1"), ("ad_domain", "CORP"), ("ad_group", "Everyone")):
            set_setting(k, v)
        with mock.patch.object(winauth, "available", return_value=True), \
                mock.patch.object(winauth, "logon", side_effect=winauth.AuthError("Incorrect username or password.")) \
                as logon:
            for _ in range(5):
                self.login(username="CORP\\nva", password="bad")
            self.assertEqual(logon.call_count, 5)
            self.login(username="CORP\\nva", password="bad")
            self.assertEqual(logon.call_count, 5)  # locked: Windows not asked again

    def test_security_answer_guessing_is_locked(self):
        set_security_question(self.admin_id, "food_fight", "pizza")
        data = {"username": "admin", "answer": "x", "password": "newpass123", "confirm": "newpass123"}
        for _ in range(4):
            self.assertIn("incorrect", self.client.post("/forgot-password", data=data).get_data(as_text=True))
        resp = self.client.post("/forgot-password", data=data, follow_redirects=True)
        self.assertIn("Too many failed attempts", resp.get_data(as_text=True))
        resp = self.client.post("/forgot-password", data={**data, "answer": "pizza"}, follow_redirects=True)
        self.assertIn("Too many failed attempts", resp.get_data(as_text=True))  # right answer, still locked
        self.assertEqual(self.login(password="adminpass1").status_code, 302)  # password unchanged

    def test_name_lookups_are_limited(self):
        for _ in range(9):
            self.assertEqual(self.client.get("/forgot-password?username=nobody").status_code, 200)
        self.assertEqual(self.client.get("/forgot-password?username=admin").status_code, 429)


class SessionTests(Base):
    def test_a_password_reset_ends_the_other_sessions(self):
        bob = create_account("bob", "bobpass12", _role_id("Viewer"))
        bob_client = self.app.test_client()
        self.login(bob_client, "bob", "bobpass12")
        self.assertEqual(bob_client.get("/").status_code, 200)
        self.login()
        self.client.post(f"/settings/users/accounts/{bob}/reset-password", data={"password": "newbobpass1"})
        self.assertEqual(bob_client.get("/").status_code, 302)  # signed out
        self.assertEqual(self.login(bob_client, "bob", "newbobpass1").status_code, 302)

    def test_resetting_ones_own_password_keeps_this_session(self):
        other = self.app.test_client()
        self.login(other)
        self.login()
        self.client.post(f"/settings/users/accounts/{self.admin_id}/reset-password", data={"password": "adminpass2"})
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(other.get("/").status_code, 302)


class AuditFixTests(Base):
    def test_axl_password_is_never_on_the_page_and_blank_keeps_it(self):
        self.login()
        self.client.post("/settings/cucm", data={"ip": "10.0.0.5", "axluser": "axl", "axlpassword": "S3cret-AXL!"})
        html = self.client.get("/settings/").get_data(as_text=True)
        self.assertNotIn("S3cret-AXL!", html)
        self.assertIn("Saved - leave blank to keep it", html)
        self.client.post("/settings/cucm", data={"ip": "10.0.0.5", "axluser": "axl", "axlpassword": ""})
        self.assertEqual(get_setting("cucm_axlpassword"), "S3cret-AXL!")

    def test_diff_report_name_cant_leave_its_folder(self):
        from app.routes import import_data
        with mock.patch.object(import_data, "diff_batch", return_value=[{"x": 1}]), \
                mock.patch.object(import_data, "build_diff_workbook") as build, \
                mock.patch.object(import_data, "diff_branch_labels", return_value=""):
            import_data._save_diff_report([1], "..\\..\\evil/..\\x")
        saved = build.return_value.save.call_args[0][0]
        folder = import_data.get_diff_reports_dir()
        self.assertEqual(os.path.dirname(saved), folder)
        self.assertNotIn("..", os.path.basename(saved))


class TlsServerTests(unittest.TestCase):
    """make-cert + a real cheroot HTTPS server; CUCM's pin against it too."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="am_tls_")
        os.environ["LOCALAPPDATA"] = os.path.join(cls.tmp, "local")
        cls.config_file = os.path.join(cls.tmp, "conf", "server.json")
        server_config.write(os.path.join(cls.tmp, "AMData"), _free_port(), cls.config_file, host="127.0.0.1")
        cls.made = server_config.make_self_signed(cls.config_file, ["localhost", "127.0.0.1"])
        cls.config = server_config.load(cls.config_file)
        cls.app, _ = server.create_server_app(cls.config)
        cls.srv = server.make_server(cls.config, cls.app)
        cls.thread = threading.Thread(target=cls.srv.start, daemon=True)
        cls.thread.start()
        for _ in range(50):
            if cls.srv.ready:
                break
            threading.Event().wait(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        from app import paths
        paths.use_server_data_dir(None)  # the next tests run as the desktop app again

    def ctx(self):
        return ssl.create_default_context(cafile=self.made["cert"])

    def test_config_and_files(self):
        self.assertEqual(self.config["tls_cert"], self.made["cert"])
        for k in ("cert", "key", "cer"):
            self.assertTrue(os.path.isfile(self.made[k]), k)
        info = tls.cert_info(self.made["cert"])
        self.assertEqual(set(info["names"]), {"localhost", "127.0.0.1"})
        self.assertTrue(info["self_signed"])

    def test_https_with_the_certificate_trusted(self):
        port = self.config["port"]
        with urllib.request.urlopen(f"https://localhost:{port}/health", context=self.ctx(), timeout=10) as r:
            self.assertEqual(json.loads(r.read())["status"], "ok")
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/login", context=self.ctx(), timeout=10) as r:
            self.assertIn("Secure", r.headers.get("Set-Cookie", ""))

    def test_not_trusted_is_refused_by_the_client(self):
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(f"https://localhost:{self.config['port']}/health",
                                   context=ssl.create_default_context(), timeout=10)

    def test_cucm_pin_trusts_first_then_refuses_another(self):
        from app import cucm
        port = self.config["port"]
        with self.app.app_context():
            for k in (cucm.CERT_PIN_KEY, cucm.CERT_PIN_AT_KEY):
                set_setting(k, "")
            s = cucm.pinned_session("127.0.0.1", port)
            self.assertEqual(s.get(f"https://127.0.0.1:{port}/health", timeout=10).status_code, 200)
            self.assertEqual(cucm.cert_pin()["sha256"], self.made["sha256"])
            set_setting(cucm.CERT_PIN_KEY, "0" * 64)  # what an interceptor's certificate looks like
            s = cucm.pinned_session("127.0.0.1", port)
            import requests
            with self.assertRaises(requests.exceptions.SSLError) as ctx:
                s.get(f"https://127.0.0.1:{port}/health", timeout=10)
            self.assertTrue(cucm._cert_changed(ctx.exception))


if __name__ == "__main__":
    unittest.main()
