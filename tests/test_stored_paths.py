# -*- coding: utf-8 -*-
"""Files the database names (hand-over docx_path, diff report file_path,
disposal stored_name) are only ever served from their own folder: a
database brought in (import-desktop-data, restore) could name any file of
the server."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.paths import file_in, find_moved_file  # noqa: E402


class StoredPathTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="am_paths_")
        self.folder = os.path.join(self.root, "handovers")
        os.makedirs(self.folder)
        self.secret = os.path.join(self.root, "server.key")
        for path in (self.secret, os.path.join(self.folder, "HO-1.docx")):
            with open(path, "w") as f:
                f.write("x")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_file_outside_the_folder_is_never_given(self):
        self.assertIsNone(find_moved_file(self.secret, self.folder))
        self.assertIsNone(find_moved_file(os.path.join(self.folder, "..", "server.key"), self.folder))

    def test_its_own_file_and_a_moved_one(self):
        own = os.path.join(self.folder, "HO-1.docx")
        self.assertEqual(find_moved_file(own, self.folder), own)
        # recorded on another PC / the old data folder: same name in today's folder
        self.assertEqual(find_moved_file(r"D:\Old\handovers\HO-1.docx", self.folder), own)
        self.assertIsNone(find_moved_file(r"D:\Old\handovers\missing.docx", self.folder))

    def test_file_in_takes_plain_names_only(self):
        self.assertEqual(file_in(self.folder, "HO-1.docx"), os.path.join(self.folder, "HO-1.docx"))
        for bad in ("..\\server.key", "../server.key", os.path.join(self.root, "server.key"), "C:x", "..", "", None):
            with self.subTest(name=bad):
                self.assertIsNone(file_in(self.folder, bad))


if __name__ == "__main__":
    unittest.main()
