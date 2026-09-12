"""`tracker.py diag`: the report's content and its trip into the fake Google Doc."""
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from earlyscale import db, meta_ads, ops
from tests.test_winners import ad, prod


class DiagContentTests(unittest.TestCase):
    def test_report_has_every_section_and_per_store_lines(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "elivorahealth.com", "Elivora")
        db.write_product_snapshot(conn, sid, "2026-09-10", [prod(1, "elivora-prostate-urinary-support-softgels", "P", "2026-08-01")])
        ads = [ad(str(i), "Elivora", "2026-09-01", "https://elivorahealth.com/pages/prostate") for i in range(4)] + \
              [ad("9", "Elivora", "2026-09-01", "https://elivorahealth.com/pages/prostate", low=None)]
        meta_ads.record_scrape(conn, sid, "2026-09-10", ads, "Elivora")
        conn.commit()
        text = ops.collect_diag(conn, ["after run_daily.bat day (exit code 0)"])
        for section in ("== EarlyScale diag", "== scheduled tasks ==", "== database ==", "== stores ==", "== winners", "== logs =="):
            self.assertIn(section, text)
        self.assertIn("note: after run_daily.bat day", text)
        self.assertIn("elivorahealth.com | ? | - | - | 2026-09-10 5 4 1 1 |", text)      # active 5, delivering 4, badge unknown 1, one URL >= 3
        self.assertIn("elivorahealth.com | elivorahealth.com/pages/prostate | 4 |", text)
        self.assertIn("META_MAX_SCROLLS=", text)
        self.assertLess(len(ops.collect_diag(conn, max_chars=500)), 560)


class DiagDocTests(unittest.TestCase):
    """The real Code.gs (inside tests/fake_gas.js) writes mode=diag into a Google Doc; read it back."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not installed")
        cls.port = 8097
        cls.proc = subprocess.Popen(["node", str(Path(__file__).parent / "fake_gas.js"), "--port", str(cls.port)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/exec", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()

    def test_push_creates_then_replaces_the_doc(self):
        import requests
        url = f"http://127.0.0.1:{self.port}/exec"
        r1 = ops.push_diag(requests.Session(), url, "== EarlyScale diag 1 ==\nline")
        self.assertTrue(r1["ok"]); self.assertEqual(r1["chars"], len("== EarlyScale diag 1 ==\nline"))
        ops.push_diag(requests.Session(), url, "== EarlyScale diag 2 ==")
        doc = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/__doc?name=EarlyScale%20Diag", timeout=5).read())
        self.assertEqual((doc["count"], doc["text"]), (1, "== EarlyScale diag 2 =="))
