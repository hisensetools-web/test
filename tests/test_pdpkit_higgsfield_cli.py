"""CLI backend against a fake `higgsfield` executable (records argv, prints JSON with result URLs)."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pdpkit import config, higgsfield_cli

FAKE = r'''#!/usr/bin/env python3
import json, sys, os
log = os.environ["FAKE_HF_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1] == "account":
    print("Logged in as tester@example.com"); sys.exit(0)
if "--fail-auth" in sys.argv:
    print("Error: Not authenticated.", file=sys.stderr); sys.exit(1)
if sys.argv[1] == "product-photoshoot":
    print("3 shots ready:\n- https://cdn.higgsfield.ai/a/job_1.jpg\n- https://cdn.higgsfield.ai/a/job_2.jpg"); sys.exit(0)
print(json.dumps([{"id": "j1", "status": "completed", "results": [{"url": "https://cdn.higgsfield.ai/r/j1.png", "type": "image"}],
                   "params": {"prompt": "x"}, "raw_url": "https://cdn.higgsfield.ai/r/j1_raw.png"}]))
'''


class CliBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        exe = root / "higgsfield"
        exe.write_text(FAKE)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.log = root / "argv.log"
        self.env = mock.patch.dict(os.environ, {"PATH": f"{root}{os.pathsep}{os.environ['PATH']}", "FAKE_HF_LOG": str(self.log)})
        self.env.start()
        self.pdir = root / "glow"
        (self.pdir / "competitor_imgs").mkdir(parents=True)
        for n in ("gallery_01.jpg", "gallery_02.jpg", "page_01.jpg"):
            (self.pdir / "competitor_imgs" / n).write_bytes(b"x")
        self.dl = mock.patch("pdpkit.higgsfield_cli.download", side_effect=lambda url, dest: (dest.write_bytes(url.encode()), dest)[1])
        self.dl.start()

    def tearDown(self):
        self.dl.stop()
        self.env.stop()
        self.tmp.cleanup()

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def test_extract_urls_json_and_plain(self):
        self.assertEqual(higgsfield_cli.extract_urls('[{"results":[{"url":"https://cdn.higgsfield.ai/r/1.png"}],"x":"https://x.com/page"}]'),
                         ["https://cdn.higgsfield.ai/r/1.png"])
        self.assertEqual(higgsfield_cli.extract_urls("ready:\n- https://cdn.higgsfield.ai/a.jpg\n- https://cdn.higgsfield.ai/a.jpg\n"), ["https://cdn.higgsfield.ai/a.jpg"])

    def test_generic_model_repeats_job_n_times_with_references(self):
        out = higgsfield_cli.generate(self.pdir, "Glow", ["studio shot"], num_images=2, refs=[self.pdir / "competitor_imgs" / "gallery_01.jpg"])
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        c = calls[0]
        self.assertEqual(c[:3], ["generate", "create", config.HIGGSFIELD_CLI_MODEL])
        self.assertIn("--image", c)
        self.assertTrue(c[c.index("--image") + 1].endswith("gallery_01.jpg"))
        self.assertEqual(c[-2:], ["--wait", "--json"])
        self.assertNotIn("--num_images", c)
        files = sorted(p.name for p in out.iterdir() if p.suffix == ".jpg")
        self.assertEqual(files, ["gen_01.jpg", "gen_02.jpg", "gen_03.jpg", "gen_04.jpg"])   # 2 urls per fake job x 2 jobs
        entries = json.loads((out / "generation_log.json").read_text())
        self.assertEqual(entries[0]["backend"], "cli")
        self.assertEqual(entries[0]["result_urls"][0], "https://cdn.higgsfield.ai/r/j1.png")

    def test_photoshoot_mode_uses_count_and_all_references(self):
        out = higgsfield_cli.generate(self.pdir, "Glow", ["on a kitchen counter"], num_images=3, photoshoot_mode="lifestyle_scene")
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c[:4], ["product-photoshoot", "create", "--mode", "lifestyle_scene"])
        self.assertEqual(c[c.index("--count") + 1], "3")
        self.assertEqual(c.count("--image"), 3)
        self.assertEqual(sorted(p.name for p in out.iterdir() if p.suffix == ".jpg"), ["gen_01.jpg", "gen_02.jpg"])

    def test_bad_photoshoot_mode_and_auth_error(self):
        with self.assertRaises(SystemExit):
            higgsfield_cli.build_command("x", [], photoshoot_mode="nope")
        with mock.patch.object(config, "HIGGSFIELD_CLI_EXTRA", "--fail-auth"):
            with self.assertRaises(SystemExit) as cm:
                higgsfield_cli.generate(self.pdir, "Glow", ["x"], num_images=1)
        self.assertIn("auth login", str(cm.exception))

    def test_check_runs_account_status(self):
        self.assertIn("Logged in", higgsfield_cli.check())
        self.assertEqual(self.calls()[0], ["account", "status"])


if __name__ == "__main__":
    unittest.main()
