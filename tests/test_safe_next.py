# -*- coding: utf-8 -*-
"""?next= after signing in only ever goes to a path of this app: browsers
read "/\\evil.com" as "//evil.com" (another site)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402


class SafeNextTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_nexttest_")
        self.app = create_app(csrf=False)
        self.client = self.app.test_client()
        conn = get_connection()
        role = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
        conn.close()
        create_account("admin", "adminpass1", role)

    def _after_login(self, next_url):
        resp = self.client.post("/login", data={"username": "admin", "password": "adminpass1", "next": next_url})
        self.assertEqual(resp.status_code, 302)
        return resp.headers["Location"]

    def test_other_sites_are_refused(self):
        for bad in ("//evil.com", "/\\evil.com", "/\\/evil.com", "/\t/evil.com", "/\n/evil.com",
                    "https://evil.com", "evil.com"):
            with self.subTest(next=bad):
                self.client.post("/logout")
                location = self._after_login(bad)
                self.assertNotIn("evil", location)

    def test_the_shared_helper_used_by_asset_and_cctv_edit(self):
        from app.auth import safe_next
        for bad in ("", "//evil.com", "/\\evil.com", "/\t/evil.com", "http://evil.com", "javascript:alert(1)"):
            self.assertEqual(safe_next(bad, "/fallback"), "/fallback", bad)
        self.assertEqual(safe_next("/assets?page=3", "/fallback"), "/assets?page=3")

    def test_a_path_of_this_app_is_kept(self):
        self.assertTrue(self._after_login("/history?page=2").endswith("/history?page=2"))


if __name__ == "__main__":
    unittest.main()
