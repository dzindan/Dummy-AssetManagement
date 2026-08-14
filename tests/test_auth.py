# -*- coding: utf-8 -*-
"""Login + role-based permissions verification.

Follows this project's established isolated-DB pattern: override
%LOCALAPPDATA% to a fresh temp directory before create_app() so every test
gets its own throwaway SQLite database, then drive the app through
app.test_client() like a real browser would.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import PERMISSIONS, get_connection  # noqa: E402

PERMISSION_DENIED_TEXT = "do not have permission"
LAST_HOLDER_TEXT = "Manage Users permission"


def _fresh_app():
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_authtest_")
    return create_app()


def _role_id(conn, name):
    return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]


class SetupFlowTests(unittest.TestCase):
    def test_first_run_then_setup_creates_admin_and_logs_in(self):
        app = _fresh_app()
        client = app.test_client()

        resp = client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/setup", resp.headers["Location"])

        resp = client.post(
            "/setup",
            data={"username": "admin", "password": "adminpass123", "confirm": "adminpass123"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)

        # Logged in immediately - no separate trip through /login needed.
        self.assertEqual(client.get("/").status_code, 200)

        # /setup a second time (an account now exists) goes to /login, not
        # the account-creation form again - guards against a stale tab.
        resp = client.get("/setup", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class LoginLogoutTests(unittest.TestCase):
    def setUp(self):
        self.app = _fresh_app()
        conn = get_connection()
        try:
            admin_role_id = _role_id(conn, "Admin")
        finally:
            conn.close()
        create_account("alice", "correcthorse1", admin_role_id)
        self.client = self.app.test_client()

    def test_login_logout_round_trip(self):
        resp = self.client.post("/login", data={"username": "alice", "password": "correcthorse1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

        resp = self.client.post("/logout")
        self.assertEqual(resp.status_code, 302)

        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_wrong_password_rejected(self):
        resp = self.client.post("/login", data={"username": "alice", "password": "wrongpassword"})
        self.assertEqual(resp.status_code, 200)  # form re-rendered, not a redirect

        # No session was established.
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class SecretKeyPersistenceTests(unittest.TestCase):
    def test_secret_key_persists_across_simulated_restart(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_authtest_")

        app1 = create_app()
        conn = get_connection()
        try:
            admin_role_id = _role_id(conn, "Admin")
        finally:
            conn.close()
        create_account("bob", "correcthorse2", admin_role_id)

        client1 = app1.test_client()
        resp = client1.post("/login", data={"username": "bob", "password": "correcthorse2"})
        self.assertEqual(resp.status_code, 302)
        set_cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("session=", set_cookie)
        session_value = set_cookie.split("session=", 1)[1].split(";", 1)[0]

        # A brand-new Flask app object (simulating a process restart) against
        # the SAME %LOCALAPPDATA% must accept a cookie signed by the first one.
        app2 = create_app()
        client2 = app2.test_client()
        client2.set_cookie("session", session_value)
        resp = client2.get("/")
        self.assertEqual(resp.status_code, 200)


class PermissionMatrixTests(unittest.TestCase):
    def setUp(self):
        self.app = _fresh_app()
        conn = get_connection()
        try:
            self.role_ids = {row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM roles")}
            cursor = conn.execute(
                "INSERT INTO roles (name, is_builtin, created_at) VALUES ('NetOpsOnly', 0, datetime('now'))"
            )
            self.netops_role_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO role_permissions (role_id, permission_key) VALUES (?, 'network_check')",
                (self.netops_role_id,),
            )
            conn.execute(
                "INSERT INTO import_batches (id, imported_at, kind) VALUES (1, datetime('now'), 'test')"
            )
            cursor = conn.execute(
                "INSERT INTO asset_items (batch_id, asset_key, device_name, device_name_raw) "
                "VALUES (1, 'k1', 'PC', 'PC')"
            )
            self.asset_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
        create_account("viewer1", "correcthorse3", self.role_ids["Viewer"])
        create_account("editor1", "correcthorse4", self.role_ids["Editor"])
        create_account("netops1", "correcthorse5", self.netops_role_id)

    def _login(self, username, password):
        client = self.app.test_client()
        resp = client.post("/login", data={"username": username, "password": password})
        self.assertEqual(resp.status_code, 302)
        return client

    def test_viewer_can_view_but_not_mutate(self):
        client = self._login("viewer1", "correcthorse3")
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/assets/").status_code, 200)

        resp = client.post(
            "/assets/bulk-delete", data={"asset_ids": [str(self.asset_id)]}, follow_redirects=True
        )
        self.assertIn(PERMISSION_DENIED_TEXT, resp.get_data(as_text=True))

        conn = get_connection()
        try:
            still_there = conn.execute(
                "SELECT 1 FROM asset_items WHERE id = ?", (self.asset_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(still_there, "Viewer's bulk-delete must not actually delete the row")

    def test_editor_can_edit_assets_but_not_manage_users(self):
        client = self._login("editor1", "correcthorse4")

        resp = client.post(
            "/assets/bulk-delete", data={"asset_ids": [str(self.asset_id)]}, follow_redirects=True
        )
        self.assertNotIn(PERMISSION_DENIED_TEXT, resp.get_data(as_text=True))
        conn = get_connection()
        try:
            gone = conn.execute("SELECT 1 FROM asset_items WHERE id = ?", (self.asset_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(gone, "Editor's bulk-delete should actually delete the row")

        resp = client.get("/settings/users/", follow_redirects=True)
        self.assertIn(PERMISSION_DENIED_TEXT, resp.get_data(as_text=True))

    def test_custom_role_scoped_to_one_permission(self):
        client = self._login("netops1", "correcthorse5")

        # In scope: network_check.
        resp = client.post("/network-check/scan", json={"branch_no": ""})
        self.assertNotEqual(resp.status_code, 403)

        # Out of scope: import_data.
        resp = client.post("/import/branches", data={}, follow_redirects=True)
        self.assertIn(PERMISSION_DENIED_TEXT, resp.get_data(as_text=True))


class LastAdminSafeguardTests(unittest.TestCase):
    def setUp(self):
        self.app = _fresh_app()
        conn = get_connection()
        try:
            self.admin_role_id = _role_id(conn, "Admin")
        finally:
            conn.close()
        self.admin_id = create_account("soleadmin", "correcthorse6", self.admin_role_id)

    def test_sole_admin_cannot_self_deactivate(self):
        client = self.app.test_client()
        client.post("/login", data={"username": "soleadmin", "password": "correcthorse6"})

        resp = client.post(f"/settings/users/accounts/{self.admin_id}/toggle-active", follow_redirects=True)
        self.assertIn(LAST_HOLDER_TEXT, resp.get_data(as_text=True))

        conn = get_connection()
        try:
            still_active = conn.execute(
                "SELECT is_active FROM accounts WHERE id = ?", (self.admin_id,)
            ).fetchone()["is_active"]
        finally:
            conn.close()
        self.assertEqual(still_active, 1)

    def test_second_admin_allows_deactivation(self):
        create_account("secondadmin", "correcthorse7", self.admin_role_id)

        client = self.app.test_client()
        client.post("/login", data={"username": "soleadmin", "password": "correcthorse6"})
        resp = client.post(f"/settings/users/accounts/{self.admin_id}/toggle-active", follow_redirects=True)
        self.assertNotIn(LAST_HOLDER_TEXT, resp.get_data(as_text=True))

        conn = get_connection()
        try:
            still_active = conn.execute(
                "SELECT is_active FROM accounts WHERE id = ?", (self.admin_id,)
            ).fetchone()["is_active"]
        finally:
            conn.close()
        self.assertEqual(still_active, 0)

    def test_cannot_strip_manage_users_from_last_holding_custom_role(self):
        conn = get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO roles (name, is_builtin, created_at) VALUES ('CustomAdmin', 0, datetime('now'))"
            )
            custom_role_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO role_permissions (role_id, permission_key) VALUES (?, 'manage_users')",
                (custom_role_id,),
            )
            # Demote the seed admin so the custom role is the ONLY manage_users holder.
            conn.execute("UPDATE accounts SET role_id = ?", (_role_id(conn, "Viewer"),))
            conn.commit()
        finally:
            conn.close()
        create_account("customadmin1", "correcthorse8", custom_role_id)

        client = self.app.test_client()
        client.post("/login", data={"username": "customadmin1", "password": "correcthorse8"})
        resp = client.post(f"/settings/users/roles/{custom_role_id}/edit", data={}, follow_redirects=True)
        self.assertIn(LAST_HOLDER_TEXT, resp.get_data(as_text=True))


class RouteCoverageGuardTests(unittest.TestCase):
    """Regression protection: a future mutating route added without a
    permission decorator should fail this test immediately, not ship
    silently ungated."""

    # Explicit, reviewed exceptions - not an oversight.
    INTENTIONALLY_UNGATED = {
        "auth.login",
        "auth.logout",
        "auth.setup",
        "lookup.review",  # builds a preview only, no DB write
    }

    def test_every_mutating_route_is_gated_or_allowlisted(self):
        app = _fresh_app()
        offenders = []
        for rule in app.url_map.iter_rules():
            if not ({"POST", "PUT", "DELETE"} & rule.methods):
                continue
            endpoint = rule.endpoint
            if endpoint in self.INTENTIONALLY_UNGATED or endpoint.startswith("user_admin."):
                continue
            view_func = app.view_functions[endpoint]
            if not hasattr(view_func, "required_permission"):
                offenders.append(endpoint)
        self.assertEqual(offenders, [], f"Mutating routes with no permission gate: {offenders}")

    def test_every_decorated_permission_key_is_known(self):
        app = _fresh_app()
        for endpoint, view_func in app.view_functions.items():
            perm = getattr(view_func, "required_permission", None)
            if perm is not None:
                self.assertIn(perm, PERMISSIONS, f"{endpoint} uses unknown permission {perm!r}")


if __name__ == "__main__":
    unittest.main()
