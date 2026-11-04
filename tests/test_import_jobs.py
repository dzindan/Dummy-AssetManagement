# -*- coding: utf-8 -*-
"""P3: imports are jobs run one at a time (app/jobs.py) - in a worker
thread on the central server (the page waits, then shows the result),
inline on the desktop."""
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, jobs  # noqa: E402
from app.auth import create_account  # noqa: E402
from app.db import get_connection  # noqa: E402


def _role_id(name):
    conn = get_connection()
    try:
        return conn.execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class RunnerTests(unittest.TestCase):
    def test_one_at_a_time_in_order(self):
        runner = jobs.JobRunner()
        running, order, peak = [0], [], [0]
        gate = threading.Event()
        lock = threading.Lock()

        def work(n):
            def fn():
                with lock:
                    running[0] += 1
                    peak[0] = max(peak[0], running[0])
                gate.wait(5)
                order.append(n)
                with lock:
                    running[0] -= 1
                return {"n": n}
            return fn

        js = [runner.submit(None, "t", f"job {n}", "u", work(n), background=True) for n in range(4)]
        self.assertTrue(_wait(lambda: js[0].status == "running"))
        self.assertEqual([j.status for j in js[1:]], ["queued"] * 3)
        self.assertEqual([runner.ahead_of(j) for j in js], [0, 1, 2, 3])
        gate.set()
        self.assertTrue(_wait(lambda: all(j.done for j in js)))
        self.assertEqual(order, [0, 1, 2, 3])
        self.assertEqual(peak[0], 1)
        self.assertEqual(js[2].outcome, {"n": 2})

    def test_messages_and_errors_stay_on_the_job(self):
        runner = jobs.JobRunner()

        def fn():
            jobs.notify("hello", "info")
            raise ValueError("bad file")

        job = runner.submit(None, "t", "x", "u", fn, background=False)
        self.assertEqual((job.status, job.messages), ("error", [("info", "hello")]))
        self.assertIn("bad file", job.error)
        job2 = runner.submit(None, "t", "y", "u", lambda: {"ok": 1}, background=False)
        self.assertEqual(job2.status, "done")  # an error doesn't stop the next one


class BackgroundImportTests(unittest.TestCase):
    def setUp(self):
        os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="am_jobs_")
        self.app = create_app(csrf=False)
        self.app.config["IMPORT_JOBS_BACKGROUND"] = True
        admin = _role_id("Admin")
        create_account("ann", "annpass12", admin)
        create_account("bob", "bobpass12", admin)
        self.ann, self.bob = self.app.test_client(), self.app.test_client()
        self.ann.post("/login?form=1", data={"username": "ann", "password": "annpass12"})
        self.bob.post("/login?form=1", data={"username": "bob", "password": "bobpass12"})

    def upload(self, client, name="branches.xlsx"):
        return client.post("/import/branches", data={"files": [(io.BytesIO(b"x"), name)]},
                           content_type="multipart/form-data")

    def test_waits_then_shows_the_result_and_its_messages(self):
        gate = threading.Event()
        calls = []

        def slow_import(path, performed_by=""):
            calls.append(performed_by)
            gate.wait(5)
            return {"file_type": "branches", "rows": 3}

        with mock.patch("app.routes.import_data.import_branch_file", side_effect=slow_import):
            first = self.upload(self.ann).headers["Location"]
            second = self.upload(self.bob).headers["Location"]
            self.assertTrue(_wait(lambda: calls == ["ann"]))
            page = self.bob.get(second).get_data(as_text=True)
            self.assertIn("Waiting to import", page)
            self.assertIn("Now importing: ID files (1 file(s)) (ann", page)
            self.assertIn("Importing", self.ann.get(first).get_data(as_text=True))
            self.assertIn("Imports in progress", self.bob.get("/import/").get_data(as_text=True))
            self.assertEqual(self.bob.get(first).status_code, 404)  # someone else's import
            self.assertEqual(self.ann.get(first + "/status").get_json()["status"], "running")
            gate.set()
            job_id = int(second.rsplit("/", 1)[1])
            self.assertTrue(_wait(lambda: jobs.runner.get(job_id).done))
        self.assertEqual(calls, ["ann", "bob"])
        html = self.bob.get(second).get_data(as_text=True)
        self.assertIn("branches.xlsx", html)
        self.assertIn("ID Master Import Result", html)

    def test_a_failing_import_says_so(self):
        with mock.patch("app.routes.import_data.import_branch_file", side_effect=RuntimeError("boom")):
            loc = self.upload(self.ann).headers["Location"]
            job_id = int(loc.rsplit("/", 1)[1])
            self.assertTrue(_wait(lambda: jobs.runner.get(job_id).done))
        resp = self.ann.get(loc, follow_redirects=True)
        self.assertIn("The import failed: RuntimeError: boom", resp.get_data(as_text=True))
        self.assertEqual(os.listdir(os.path.join(os.environ["LOCALAPPDATA"], "AssetManagementTool", "uploads")), [])

    def test_unknown_job(self):
        resp = self.ann.get("/import/jobs/99999", follow_redirects=True)
        self.assertIn("isn&#39;t kept any more", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
