# -*- coding: utf-8 -*-
"""Windows sign-in with no password box on the client PCs (app/sso_client.py):
the address given to the clients is the dotless name (intranet by default),
the .reg file puts the server in the Local intranet zone, and Users & Roles
shows the setup on the central server only."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, sso_client  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402


def _names(short="AMSERVER", full="amserver.corp.local"):
    return mock.patch.multiple("socket", gethostname=lambda: short, getfqdn=lambda: full)


class AddressTests(unittest.TestCase):
    def test_the_dotless_name_is_given_to_the_clients(self):
        with _names():
            self.assertEqual(sso_client.address({"port": 8737, "tls_cert": ""}), "http://AMSERVER:8737")
            self.assertEqual(sso_client.address({"port": 8737, "tls_cert": "x.crt"},
                                                ["AMSERVER", "amserver.corp.local", "10.0.0.5"]),
                             "https://AMSERVER:8737")

    def test_a_certificate_without_the_short_name_keeps_the_full_one(self):
        with _names():
            self.assertEqual(sso_client.address({"port": 8737, "tls_cert": "x.crt"}, ["amserver.corp.local"]),
                             "https://amserver.corp.local:8737")

    def test_no_ip_and_no_localhost_in_the_names(self):
        with _names(full="localhost"):
            self.assertEqual(sso_client.host_names(), ["AMSERVER"])

    def test_reg_file_puts_both_names_in_the_intranet_zone(self):
        text = sso_client.reg_file(["AMSERVER", "amserver.corp.local"], "https")
        self.assertTrue(text.startswith("Windows Registry Editor Version 5.00"))
        base = r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ZoneMap\Domains"
        self.assertIn(f"[{base}\\AMSERVER]", text)
        self.assertIn(f"[{base}\\corp.local\\amserver]", text)
        self.assertEqual(text.count('"https"=dword:00000001'), 2)


class UsersPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp.name})
        self.env.start()
        self.app = create_app(csrf=False)
        conn = get_connection()
        try:
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
        finally:
            conn.close()
        create_account("admin", "password123", admin)
        self.client = self.app.test_client()
        self.client.post("/login?form=1", data={"username": "admin", "password": "password123"})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_desktop_app_shows_no_client_setup(self):
        page = self.client.get("/settings/users/").get_data(as_text=True)
        self.assertIn("AD sign-in", page)
        self.assertNotIn("Windows sign-in on the client PCs", page)
        self.assertEqual(self.client.get("/settings/users/sso-client.reg").status_code, 302)

    def test_central_server_shows_it_and_gives_the_reg_file(self):
        self.app.config.update(SERVER_MODE=True, SERVER_CONFIG={"port": 8737, "tls_cert": "x.crt"},
                               CERT_NAMES=["AMSERVER", "amserver.corp.local"], LAN_URL="https://AMSERVER:8737")
        with _names():
            page = self.client.get("/settings/users/").get_data(as_text=True)
            self.assertIn("Windows sign-in on the client PCs", page)
            self.assertIn("https://AMSERVER:8737", page)
            self.assertIn("https://amserver.corp.local", page)
            self.assertNotIn("has no name without dots", page)
            resp = self.client.get("/settings/users/sso-client.reg")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        text = resp.get_data().decode("utf-16")
        self.assertIn(r"ZoneMap\Domains\AMSERVER]", text)

    def test_warns_when_the_certificate_lacks_the_short_name(self):
        self.app.config.update(SERVER_MODE=True, SERVER_CONFIG={"port": 8737, "tls_cert": "x.crt"},
                               CERT_NAMES=["amserver.corp.local"], LAN_URL="https://amserver.corp.local:8737")
        with _names():
            page = self.client.get("/settings/users/").get_data(as_text=True)
        self.assertIn("has no name without dots", page)


if __name__ == "__main__":
    unittest.main()
