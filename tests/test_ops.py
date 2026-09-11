"""`tracker.py diag`: the report's content and its trip into the fake Google Doc."""
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from earlyscale import config, db, meta_ads, ops
from tests.test_ad_metrics import _ad, _prod


class DiagContentTests(unittest.TestCase):
    def test_report_has_every_section_and_per_store_lines(self):
        conn = db.connect(":memory:")
        sid = db.upsert_store(conn, "elivorahealth.com", "Elivora")
        db.write_product_snapshot(conn, sid, "2026-09-10", [_prod(1, "elivora-prostate-urinary-support-softgels", "P")])
        ads = [_ad("1001", "Elivora", "2026-09-01", "https://elivorahealth.com/pages/prostate", "t")]
        meta_ads.record_scrape(conn, sid, "2026-09-10", ads, "Elivora")
        conn.execute("UPDATE meta_ads SET product_handle = 'elivora-prostate-urinary-support-softgels'")
        conn.execute("UPDATE meta_ads_daily SET low_impressions = 0")
        conn.execute("INSERT INTO rank_checks (snapshot_date, store_id, informative, country, n_sorted, note) VALUES ('2026-09-10', ?, 0, 'ALL', 40, 'ALL: 40 ads [sort control: sort control not found (seen: nothing)]')", (sid,))
        conn.commit()
        text = ops.collect_diag(conn, ["after run_daily.bat day (exit code 0)"])
        for section in ("== EarlyScale diag", "== scheduled tasks ==", "== database ==", "== rank verdicts", "== stores ==", "== landers", "== logs =="):
            self.assertIn(section, text)
        self.assertIn("elivorahealth.com/pages/prostate", text)
        self.assertIn("-> elivora-prostate-urinary-support-softgels", text)
        self.assertIn("note: after run_daily.bat day", text)
        self.assertIn("elivorahealth.com | ? | - | 2026-09-10 1 1 0 1 |", text)
        self.assertIn("sort control not found", text)
        self.assertIn("META_RANK=", text)
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
