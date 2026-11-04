# -*- coding: utf-8 -*-
"""AD sign-in (app/winauth.py): the Users & Roles panel, the form with an
AD name + password (LogonUser - stood in for here: this PC isn't on a
domain), Windows sign-in (/sso) with a REAL SSPI Negotiate handshake against
this PC's own Windows, the access group, the account made on the first
sign-in, and that the app's own accounts keep working."""
import base64
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, winauth  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection, set_setting  # noqa: E402

EVERYONE = "S-1-1-0"


def _role_id(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


def _account(username):
    conn = get_connection()
    try:
        return conn.execute("SELECT a.*, r.name AS role_name FROM accounts a JOIN roles r ON r.id = a.role_id "
                            "WHERE username_norm = ?", (username.upper(),)).fetchone()
    finally:
        conn.close()


def _identity(sam="NVA", groups=(EVERYONE,), sid="S-1-5-21-9-9-9-1105"):
    return winauth.Identity(sid=sid, domain="CORP", sam=sam, group_sids=set(groups))


class AdLoginBase(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_adtest_")
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        create_account("admin", "adminpass1", _role_id("Admin"))

    def turn_on(self, group="Everyone", role=""):
        for k, v in (("ad_enabled", "1"), ("ad_domain", "CORP"), ("ad_group", group), ("ad_default_role", role)):
            set_setting(k, v)

    def login_local(self):
        return self.client.post("/login?form=1", data={"username": "admin", "password": "adminpass1"})


@unittest.skipUnless(winauth.available(), "Windows + pywin32 only")
class AdLoginTests(AdLoginBase):
    def test_off_by_default_the_form_is_as_before(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Windows session", resp.get_data(as_text=True))
        with mock.patch.object(winauth, "logon") as logon:
            self.client.post("/login", data={"username": "CORP\\nva", "password": "x"})
        logon.assert_not_called()

    def test_ad_name_and_password_first_sign_in_makes_the_account(self):
        self.turn_on()
        with mock.patch.object(winauth, "logon", return_value=_identity()) as logon:
            resp = self.client.post("/login?form=1", data={"username": "corp\\nva", "password": "Secret1!"})
        logon.assert_called_once_with("corp\\nva", "Secret1!", "CORP")
        self.assertEqual(resp.status_code, 302)
        acc = _account("CORP\\NVA")
        self.assertEqual((acc["auth_source"], acc["role_name"], acc["ad_sid"]), ("ad", "Viewer", "S-1-5-21-9-9-9-1105"))
        self.assertTrue(acc["password_hash"].startswith("!"))
        self.assertEqual(self.client.get("/").status_code, 200)  # signed in

    def test_not_in_the_access_group_is_refused(self):
        self.turn_on()
        with mock.patch.object(winauth, "logon", return_value=_identity(groups=("S-1-5-21-9-9-9-513",))):
            resp = self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
        self.assertIn("isn&#39;t a member of Everyone", resp.get_data(as_text=True))
        self.assertIsNone(_account("CORP\\NVA"))

    def test_wrong_ad_password_says_so(self):
        self.turn_on()
        with mock.patch.object(winauth, "logon", side_effect=winauth.AuthError("Incorrect username or password.")):
            resp = self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "bad"})
        self.assertIn("Incorrect username or password.", resp.get_data(as_text=True))

    def test_role_is_kept_and_deactivation_wins(self):
        self.turn_on(role=str(_role_id("Editor")))
        with mock.patch.object(winauth, "logon", return_value=_identity()):
            self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
            self.assertEqual(_account("CORP\\NVA")["role_name"], "Editor")
            conn = get_connection()
            conn.execute("UPDATE accounts SET role_id = ?, is_active = 0 WHERE username_norm = 'CORP\\NVA'",
                         (_role_id("Admin"),))
            conn.commit()
            conn.close()
            self.client.post("/logout")
            resp = self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
        self.assertIn("deactivated", resp.get_data(as_text=True))

    def test_renamed_in_ad_same_account(self):
        self.turn_on()
        with mock.patch.object(winauth, "logon", return_value=_identity()):
            self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
        self.client.post("/logout")
        with mock.patch.object(winauth, "logon", return_value=_identity(sam="NVA2")):
            self.client.post("/login?form=1", data={"username": "CORP\\nva2", "password": "x"})
        self.assertIsNone(_account("CORP\\NVA"))
        self.assertEqual(_account("CORP\\NVA2")["ad_sid"], "S-1-5-21-9-9-9-1105")

    def test_same_ad_name_other_person_doesnt_take_the_old_account(self):
        # CORP\\nva deleted in AD, the name given to someone else (another SID)
        self.turn_on()
        with mock.patch.object(winauth, "logon", return_value=_identity()):
            self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
        conn = get_connection()
        conn.execute("UPDATE accounts SET role_id = ? WHERE username_norm = 'CORP\\NVA'", (_role_id("Admin"),))
        conn.commit()
        conn.close()
        self.client.post("/logout")
        with mock.patch.object(winauth, "logon", return_value=_identity(sid="S-1-5-21-9-9-9-2222")):
            resp = self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
        self.assertIn("already named", resp.get_data(as_text=True))
        self.assertEqual(_account("CORP\\NVA")["ad_sid"], "S-1-5-21-9-9-9-1105")  # untouched
        self.assertNotEqual(self.client.get("/").status_code, 200)  # not signed in

    def test_renamed_in_ad_onto_a_taken_name_is_refused_not_an_error(self):
        self.turn_on()
        create_account("CORP\\NVA2", "localpass1", _role_id("Viewer"))
        with mock.patch.object(winauth, "logon", return_value=_identity()):
            self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
        # the form would check CORP\NVA2 as the app's own account; Windows
        # sign-in (/sso) hands the AD identity straight to sign_in_ad
        from app.auth import SignInRefused, sign_in_ad
        with self.app.app_context(), self.assertRaises(SignInRefused) as refused:
            sign_in_ad(_identity(sam="NVA2"))
        self.assertIn("already has that name", str(refused.exception))
        self.assertEqual(_account("CORP\\NVA")["ad_sid"], "S-1-5-21-9-9-9-1105")

    def test_local_accounts_still_sign_in_and_ad_accounts_have_no_app_password(self):
        self.turn_on()
        self.assertEqual(self.login_local().status_code, 302)
        with mock.patch.object(winauth, "logon", return_value=_identity()):
            self.client.post("/logout")
            self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "x"})
        self.client.post("/logout")
        # the "!ad" marker never works as a password, and forgot-password is refused
        with mock.patch.object(winauth, "logon", side_effect=winauth.AuthError("Incorrect username or password.")):
            resp = self.client.post("/login?form=1", data={"username": "CORP\\nva", "password": "!ad"})
        self.assertIn("Incorrect", resp.get_data(as_text=True))
        resp = self.client.post("/forgot-password", data={"username": "CORP\\nva", "answer": "a", "password": "x" * 9,
                                                          "confirm": "x" * 9}, follow_redirects=True)
        self.assertIn("This is an AD account", resp.get_data(as_text=True))

    def test_settings_panel_saves_and_checks_the_group(self):
        self.login_local()
        html = self.client.get("/settings/users/").get_data(as_text=True)
        self.assertIn("AD sign-in", html)
        resp = self.client.post("/settings/users/ad", data={"ad_enabled": "1", "ad_group": "No Such Group 12345",
                                                             "ad_domain": "corp"}, follow_redirects=True)
        self.assertIn("AD group not found", resp.get_data(as_text=True))
        resp = self.client.post("/settings/users/ad", data={"ad_enabled": "1", "ad_group": "Everyone", "ad_domain": "corp",
                                                             "ad_default_role": str(_role_id("Viewer"))},
                                follow_redirects=True)
        self.assertIn("AD sign-in saved", resp.get_data(as_text=True))
        from app.auth import ad_settings
        self.assertEqual((ad_settings()["enabled"], ad_settings()["domain"]), (True, "CORP"))

    def test_logout_doesnt_sign_straight_back_in(self):
        self.turn_on()
        self.login_local()
        self.client.post("/logout")
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)  # the form, not /sso

    def test_split_name(self):
        self.assertEqual(winauth.split_name("corp\\nva", "X"), ("corp", "nva"))
        self.assertEqual(winauth.split_name("nva@corp.local", "X"), ("", "nva@corp.local"))
        self.assertEqual(winauth.split_name(" nva ", "CORP"), ("CORP", "nva"))


@unittest.skipUnless(winauth.available(), "Windows + pywin32 only")
class WindowsSignInTests(AdLoginBase):
    """A real Negotiate handshake: Windows' own SSPI client (this PC's
    logged-on user) against /sso - what a browser does on a domain PC."""

    def handshake(self, client, next_url="/"):
        import sspi

        resp = client.get(f"/sso?next={next_url}", environ_base={"REMOTE_PORT": "50001"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers["WWW-Authenticate"], "Negotiate")
        auth = sspi.ClientAuth("Negotiate")
        data = None
        for _ in range(5):
            _err, out = auth.authorize(data)
            token = base64.b64encode(out[0].Buffer).decode()
            resp = client.get(f"/sso?next={next_url}", headers={"Authorization": f"Negotiate {token}"},
                              environ_base={"REMOTE_PORT": "50001"})
            if resp.status_code != 401:
                return resp
            data = base64.b64decode(resp.headers["WWW-Authenticate"].split(" ", 1)[1])
        self.fail("handshake didn't finish")

    def test_windows_sign_in_end_to_end(self):
        self.turn_on(group="Everyone")
        resp = self.client.get("/login?next=/lookup/")
        self.assertIn("/sso", resp.headers["Location"])  # tried first, once
        resp = self.handshake(self.client, "/lookup/")
        self.assertEqual((resp.status_code, resp.headers["Location"]), (302, "/lookup/"))
        me = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".upper()
        acc = _account(me)
        self.assertIsNotNone(acc, me)
        self.assertEqual(acc["auth_source"], "ad")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_windows_sign_in_not_in_the_group(self):
        self.turn_on(group="Guests")  # this PC's user isn't a Guest
        resp = self.handshake(self.client)
        self.assertIn("form=1", resp.headers["Location"])
        html = self.client.get(resp.headers["Location"]).get_data(as_text=True)
        self.assertIn("isn&#39;t a member of Guests", html)

    def test_a_browser_that_cant_lands_on_the_form(self):
        self.turn_on()
        resp = self.client.get("/sso?next=/")
        self.assertEqual(resp.status_code, 401)
        self.assertRegex(resp.get_data(as_text=True), re.escape("/login?next=/&amp;form=1"))
        resp = self.client.get("/sso", headers={"Authorization": "Negotiate !!notbase64"})
        self.assertIn("form=1", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
