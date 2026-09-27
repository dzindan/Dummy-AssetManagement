# -*- coding: utf-8 -*-
"""Settings page split into tabs: every panel belongs to a tab, the tab row
is there, and unmapped counts show on the mapping tabs. Same isolated-DB
pattern as tests/test_exports.py."""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402

TABS = ["account", "general", "branches", "devices", "statuses", "models", "cucm", "system"]


class SettingsTabsTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_settingstabs_")
        self.app = create_app()
        self.client = self.app.test_client()
        conn = get_connection()
        try:
            admin = conn.execute("SELECT id FROM roles WHERE name = 'Admin'").fetchone()["id"]
            conn.execute("INSERT INTO device_unmapped (raw_name, occurrences, first_seen_at, last_seen_at) "
                         "VALUES ('WEIRD DEVICE', 3, datetime('now'), datetime('now'))")
            conn.commit()
        finally:
            conn.close()
        create_account("tabber", "tabpass123", admin)
        self.client.post("/login", data={"username": "tabber", "password": "tabpass123"})

    def test_every_panel_is_in_a_known_tab_and_every_tab_has_a_button(self):
        page = self.client.get("/settings/").data.decode()
        self.assertIn("data-settings-tabs", page)
        panels = re.findall(r'<div class="panel"( data-settings-tab="(\w+)")?', page)
        self.assertTrue(panels)
        for tagged, tab in panels:
            self.assertTrue(tagged, "a Settings panel isn't assigned to a tab")
            self.assertIn(tab, TABS)
        for tab in TABS:
            self.assertIn(f'data-tab="{tab}"', page)

    def test_unmapped_count_shows_on_the_tab(self):
        page = self.client.get("/settings/").data.decode()
        self.assertRegex(page, r'data-tab="devices">Device Mapping <span class="badge badge-changed">1</span>')
