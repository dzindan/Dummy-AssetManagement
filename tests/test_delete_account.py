"""Users & Roles > Delete: the name can be made again; nobody locks the app out."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import AD_PASSWORD_HASH, create_account, create_ad_account  # noqa: E402
from app.db import get_connection  # noqa: E402


def _role_id(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


def _account(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM accounts WHERE username_norm = ?", (name.upper(),)).fetchone()
    finally:
        conn.close()


class DeleteAccountTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_delete_")
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        create_account("admin", "adminpass1", _role_id("Admin"))
        self.client.post("/login", data={"username": "admin", "password": "adminpass1"})

    def delete(self, name):
        return self.client.post(f"/settings/users/accounts/{_account(name)['id']}/delete",
                                follow_redirects=True).get_data(as_text=True)

    def test_deleted_then_the_name_can_be_made_again(self):
        create_account("staff", "staffpass1", _role_id("Viewer"))
        self.assertIn("deleted", self.delete("staff"))
        self.assertIsNone(_account("staff"))
        self.client.post("/settings/users/accounts/add", data={
            "username": "staff", "password": "newpass123", "role_id": _role_id("Editor")})
        self.assertEqual(_account("staff")["role_id"], _role_id("Editor"))

    def test_the_log_keeps_who_was_deleted(self):
        create_account("staff", "staffpass1", _role_id("Viewer"))
        self.delete("staff")
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM logsdb.activity_log WHERE action = 'Deleted account'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["target"], "staff")

    def test_not_your_own_account(self):
        create_account("other", "otherpass1", _role_id("Admin"))
        self.assertIn("signed in with", self.delete("admin"))
        self.assertIsNotNone(_account("admin"))

    def test_the_page_has_no_delete_for_your_own_row(self):
        create_account("staff", "staffpass1", _role_id("Viewer"))
        html = self.client.get("/settings/users/").get_data(as_text=True)
        self.assertIn(f"/accounts/{_account('staff')['id']}/delete", html)
        self.assertNotIn(f"/accounts/{_account('admin')['id']}/delete", html)

    def test_the_last_local_admin_stays(self):
        boss = create_ad_account("CORP\\BOSS", _role_id("Admin"), None)
        with mock.patch("app.routes.user_admin.current_account", return_value={"id": boss}):
            html = self.delete("admin")
        self.assertIn("keep one active Admin", html)
        self.assertIsNotNone(_account("admin"))

    def test_a_second_local_admin_can_go(self):
        create_account("second", "secondpass1", _role_id("Admin"))
        self.assertIn("deleted", self.delete("second"))

    def test_an_ad_admin_can_go_while_a_local_admin_stays(self):
        create_ad_account("CORP\\BOSS", _role_id("Admin"), None)
        self.assertIn("deleted", self.delete("CORP\\BOSS"))

    def test_an_account_from_the_ad_group_warns_it_can_come_back(self):
        conn = get_connection()
        conn.execute("INSERT INTO accounts (username, username_norm, password_hash, role_id, is_active, auth_source, "
                     "ad_listed) VALUES ('CORP\\NVA', 'CORP\\NVA', ?, ?, 1, 'ad', 0)", (AD_PASSWORD_HASH, _role_id("Viewer")))
        conn.commit()
        conn.close()
        self.assertIn("makes it again", self.delete("CORP\\NVA"))

    def test_a_viewer_cant_delete(self):
        create_account("staff", "staffpass1", _role_id("Viewer"))
        create_account("target", "targetpass1", _role_id("Viewer"))
        other = self.app.test_client()
        other.post("/login", data={"username": "staff", "password": "staffpass1"})
        other.post(f"/settings/users/accounts/{_account('target')['id']}/delete")
        self.assertIsNotNone(_account("target"))


if __name__ == "__main__":
    unittest.main()
