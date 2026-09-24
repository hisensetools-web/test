"""adspykit unit tests: sheet CSV parsing, link extraction, product selection, the per-product download loop and manifests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adspykit import cli, dedup, download, metadata, sheet
from adspykit.sheet import Product

FIX = Path(__file__).parent / "fixtures_adspy" / "main_tiktok_prods_v2.csv"


class LinkExtractTests(unittest.TestCase):
    def test_urls_split_on_whitespace_and_commas_trailing_punctuation_dropped(self):
        cell = "https://vm.tiktok.com/ZN8My63PS/ https://vm.tiktok.com/ZN8My6Dx9/, https://vm.tiktok.com/ZN8MyLv8A/.\nhttps://a.example/x?y=1&z=2)"
        self.assertEqual(sheet.extract_links(cell), ["https://vm.tiktok.com/ZN8My63PS/", "https://vm.tiktok.com/ZN8My6Dx9/",
                                                     "https://vm.tiktok.com/ZN8MyLv8A/", "https://a.example/x?y=1&z=2"])

    def test_duplicates_and_markdown_escapes(self):
        self.assertEqual(sheet.extract_links("https://x.example/a\\_b?c=1\\&d=2 https://x.example/a_b?c=1&d=2 nothing here"),
                         ["https://x.example/a_b?c=1&d=2"])
        self.assertEqual(sheet.extract_links(""), [])
        self.assertEqual(sheet.extract_links(None), [])


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.products = sheet.parse_products(FIX.read_text(encoding="utf-8"))

    def test_products_in_sheet_order_across_header_blocks(self):
        self.assertEqual([p.name for p in self.products], ["Trunk Horror Prop", "The Freaky Nikki Costume",
                                                             "B&BW x Nightmare Before Christmas Candle Holder", "Wicked For Good Tumbler"])

    def test_multiline_cell_and_continuation_row_dedup(self):
        by = {p.name: p for p in self.products}
        self.assertEqual(by["Trunk Horror Prop"].links, ["https://www.instagram.com/reel/Ddb7uPzx9gA/?stkn=aWFodzFjMzB0cDhi",
                                                          "https://www.instagram.com/reel/Ddb9dcGRfqr/?stkn=MWx2eXphaXlzb3diMw=="])
        self.assertEqual(by["The Freaky Nikki Costume"].links, ["https://vm.tiktok.com/ZN8My63PS/", "https://vm.tiktok.com/ZN8My6Dx9/",
                                                                 "https://vm.tiktok.com/ZN8MyLv8A/", "https://vm.tiktok.com/ZN8Myf7ck/"])
        self.assertEqual(by["B&BW x Nightmare Before Christmas Candle Holder"].links, [])
        self.assertEqual(by["Wicked For Good Tumbler"].links, ["https://www.pipiads.com/product-search/68e9773876337861bef2b330/"])   # Adspy column only, not Competition / Result
        self.assertEqual(by["Trunk Horror Prop"].row, 4)

    def test_slug_is_the_folder_name(self):
        by = {p.name: p for p in self.products}
        self.assertEqual(by["B&BW x Nightmare Before Christmas Candle Holder"].slug, "bbw-x-nightmare-before-christmas-candle-holder")
        self.assertEqual(by["Trunk Horror Prop"].slug, "trunk-horror-prop")

    def test_missing_header_is_an_error(self):
        with self.assertRaises(sheet.SheetAccessError):
            sheet.parse_products("a,b\n1,2\n")

    def test_select_by_name_fragment(self):
        self.assertEqual([p.name for p in sheet.select(self.products, ["nikki", "TRUNK"])], ["Trunk Horror Prop", "The Freaky Nikki Costume"])
        self.assertEqual(len(sheet.select(self.products, None)), 4)
        self.assertEqual(sheet.select(self.products, ["nothing"]), [])


class _Resp:
    def __init__(self, status, text, ctype="text/csv"):
        self.status_code, self.content, self.headers = status, text.encode(), {"Content-Type": ctype}


class FetchTests(unittest.TestCase):
    def test_gid_export_first_then_gviz_by_name(self):
        session = mock.Mock()
        session.get.side_effect = [_Resp(200, "<!DOCTYPE html><html>sign in</html>", "text/html"), _Resp(200, "Product Name,Adspy\nA,https://x/1\n")]
        text = sheet.fetch_tab_csv("SHEET", "42", "Main TikTok Prods V2", session=session)
        self.assertTrue(text.startswith("Product Name"))
        urls = [c.args[0] for c in session.get.call_args_list]
        self.assertEqual(urls[0], "https://docs.google.com/spreadsheets/d/SHEET/export?format=csv&gid=42")
        self.assertEqual(urls[1], "https://docs.google.com/spreadsheets/d/SHEET/gviz/tq?tqx=out:csv&sheet=Main%20TikTok%20Prods%20V2")

    def test_private_sheet_explains_itself(self):
        session = mock.Mock()
        session.get.return_value = _Resp(403, "")
        with self.assertRaises(sheet.SheetAccessError) as cm:
            sheet.fetch_tab_csv("SHEET", "42", "Tab", session=session)
        self.assertIn("--csv", str(cm.exception))
        self.assertIn("Anyone with the link", str(cm.exception))


class FakeDownloader:
    """Writes a file per URL; URLs containing 'bad' fail."""
    def __init__(self):
        self.calls = []

    def __call__(self, url, dest):
        self.calls.append(url)
        if "bad" in url:
            return download.VideoResult(url=url, status="failed", error="HTTP Error 404")
        vid = url.rstrip("/").rsplit("/", 1)[-1]
        f = dest / f"TikTok-{vid}.mp4"
        f.write_bytes(b"\x00" * 10)
        return download.VideoResult(url=url, status="downloaded", file=f.name, video_id=vid, width=1080, height=1920)


class DownloadLoopTests(unittest.TestCase):
    def test_folder_per_product_manifest_and_resume(self):
        p = Product(name="Skull Candle Warmer", links=["https://vm.tiktok.com/AAA/", "https://vm.tiktok.com/bad1/", "https://vm.tiktok.com/BBB/"])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fake = FakeDownloader()
            res = download.download_product(p, root, fake, pause_s=0)
            folder = root / "skull-candle-warmer"
            self.assertEqual([r.status for r in res], ["downloaded", "failed", "downloaded"])
            self.assertEqual(sorted(f.name for f in folder.glob("*.mp4")), ["TikTok-AAA.mp4", "TikTok-BBB.mp4"])
            self.assertEqual((folder / "links.txt").read_text().splitlines(), p.links)
            m = json.loads((folder / "manifest.json").read_text())
            self.assertEqual((m["product"], m["links"], m["downloaded"]), ("Skull Candle Warmer", 3, 2))
            self.assertEqual({v["url"]: v["status"] for v in m["videos"]}, {p.links[0]: "downloaded", p.links[1]: "failed", p.links[2]: "downloaded"})
            self.assertEqual(download.summarise(res), {"downloaded": 2, "exists": 0, "failed": 1, "dry-run": 0, "duplicate": 0})
            # second run: only the failed link is retried
            fake2 = FakeDownloader()
            res2 = download.download_product(p, root, fake2, pause_s=0)
            self.assertEqual(fake2.calls, ["https://vm.tiktok.com/bad1/"])
            self.assertEqual([r.status for r in res2], ["exists", "failed", "exists"])
            # a deleted file is fetched again; --force refetches everything
            (folder / "TikTok-AAA.mp4").unlink()
            fake3 = FakeDownloader()
            download.download_product(p, root, fake3, pause_s=0)
            self.assertEqual(fake3.calls, ["https://vm.tiktok.com/AAA/", "https://vm.tiktok.com/bad1/"])
            fake4 = FakeDownloader()
            download.download_product(p, root, fake4, pause_s=0, force=True)
            self.assertEqual(len(fake4.calls), 3)

    def test_max_and_dry_run_touch_nothing(self):
        p = Product(name="Swinging Ghost Decor", links=["https://vm.tiktok.com/A/", "https://vm.tiktok.com/B/", "https://vm.tiktok.com/C/"])
        with tempfile.TemporaryDirectory() as d:
            fake = FakeDownloader()
            res = download.download_product(p, Path(d), fake, max_videos=2, dry_run=True, pause_s=0)
            self.assertEqual(fake.calls, [])
            self.assertEqual([r.status for r in res], ["dry-run", "dry-run"])
            self.assertFalse((Path(d) / "swinging-ghost-decor" / "manifest.json").exists())
            self.assertTrue((Path(d) / "swinging-ghost-decor" / "links.txt").exists())

    def test_format_rejects_watermarked_formats(self):
        self.assertEqual(download.format_expression(True), "bv*[format_note!*=atermark]+ba/b[format_note!*=atermark]/bv*+ba/b")
        self.assertEqual(download.format_expression(False), "b[format_note!*=atermark]/b")
        with tempfile.TemporaryDirectory() as d:
            opts = download.ydl_options(Path(d), cookies_from_browser="chrome")
            self.assertEqual(opts["cookiesfrombrowser"], ("chrome",))
            self.assertTrue(opts["outtmpl"].endswith("%(extractor_key)s-%(id)s.%(ext)s"))
            self.assertEqual(opts["merge_output_format"], "mp4")
            opts = download.ydl_options(Path(d), cookies="c.txt", cookies_from_browser="chrome")
            self.assertEqual(opts["cookiefile"], "c.txt")
            self.assertNotIn("cookiesfrombrowser", opts)


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: behaviour keyed on the URL."""
    last_opts = None

    def __init__(self, opts):
        FakeYDL.last_opts = opts
        self.dest = Path(opts["outtmpl"]).parent

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        import yt_dlp
        if "gone" in url:
            raise yt_dlp.utils.DownloadError("ERROR: [TikTok] 123: Video not available\nmore")
        vid = url.rstrip("/").rsplit("/", 1)[-1]
        info = {"id": vid, "extractor_key": "TikTok", "title": "clip " + vid, "width": 1080, "height": 1920, "format_note": "Direct video"}
        if "archived" in url:                       # already in the archive: yt-dlp writes nothing, no filepath
            info["requested_downloads"] = [{"format_id": "x"}]
            return info
        f = self.dest / f"TikTok-{vid}.mp4"
        f.write_bytes(b"\x00")
        info["requested_downloads"] = [{"filepath": str(f)}]
        return info


class YtdlpWrapperTests(unittest.TestCase):
    def test_wrapper_maps_info_errors_and_archive_hits(self):
        import yt_dlp
        with tempfile.TemporaryDirectory() as d, mock.patch.object(yt_dlp, "YoutubeDL", FakeYDL):
            dest = Path(d)
            dl = download.ytdlp_downloader(cookies_from_browser="edge")
            r = dl("https://vm.tiktok.com/AAA/", dest)
            self.assertEqual((r.status, r.file, r.video_id, r.width, r.title), ("downloaded", "TikTok-AAA.mp4", "AAA", 1080, "clip AAA"))
            self.assertEqual(FakeYDL.last_opts["cookiesfrombrowser"], ("edge",))
            self.assertIn("atermark", FakeYDL.last_opts["format"])
            r = dl("https://vm.tiktok.com/gone/", dest)
            self.assertEqual((r.status, r.error), ("failed", "ERROR: [TikTok] 123: Video not available"))
            (dest / "TikTok-archived.mp4").write_bytes(b"\x00")
            r = dl("https://vm.tiktok.com/archived/", dest)
            self.assertEqual((r.status, r.file), ("exists", "TikTok-archived.mp4"))
            (dest / "TikTok-archived.mp4").unlink()
            r = dl("https://vm.tiktok.com/archived/", dest)
            self.assertEqual(r.status, "failed")
            self.assertIn(".downloaded.txt", r.error)


class StripTests(unittest.TestCase):
    def _setup(self, root):
        p = Product(name="Skull Candle Warmer", links=["https://vm.tiktok.com/AAA/", "https://vm.tiktok.com/BBB/"])
        download.download_product(p, root, FakeDownloader(), pause_s=0)
        (root / p.slug / "by-hand.mp4").write_bytes(b"\x00")      # not in the manifest, still cleaned
        (root / p.slug / "notes.txt").write_text("x")
        return p

    def test_strip_marks_manifest_skips_clean_files_and_records_failures(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p = self._setup(root)
            seen = []

            def fake_strip(path):
                seen.append(path.name)
                return (False, "boom") if path.name == "TikTok-BBB.mp4" else (True, "")

            counts = download.strip_product(p, root, stripper=fake_strip)
            self.assertEqual(sorted(seen), ["TikTok-AAA.mp4", "TikTok-BBB.mp4", "by-hand.mp4"])
            self.assertEqual(counts, {"cleaned": 2, "skipped": 0, "failed": 1})
            m = {v["file"]: v["clean"] for v in json.loads((root / p.slug / "manifest.json").read_text())["videos"]}
            self.assertEqual(m, {"TikTok-AAA.mp4": True, "TikTok-BBB.mp4": False})
            seen.clear()
            counts = download.strip_product(p, root, stripper=fake_strip)
            self.assertEqual(sorted(seen), ["TikTok-BBB.mp4", "by-hand.mp4"])              # AAA is marked clean; the hand-copied file has no manifest row
            self.assertEqual(counts, {"cleaned": 1, "skipped": 1, "failed": 1})
            forced = []
            counts = download.strip_product(p, root, stripper=lambda pth: forced.append(pth.name) or (True, ""), force=True)
            self.assertEqual(sorted(forced), ["TikTok-AAA.mp4", "TikTok-BBB.mp4", "by-hand.mp4"])
            self.assertEqual(counts, {"cleaned": 3, "skipped": 0, "failed": 0})
            # the clean flag survives a re-download run (manifest round trip)
            res = download.download_product(p, root, FakeDownloader(), pause_s=0)
            self.assertEqual([(r.status, r.clean) for r in res], [("exists", True), ("exists", True)])

    def test_missing_folder_and_no_ffmpeg(self):
        with tempfile.TemporaryDirectory() as d:
            p = Product(name="Nothing Yet", links=[])
            self.assertEqual(download.strip_product(p, Path(d)), {"cleaned": 0, "skipped": 0, "failed": 0})
            with mock.patch.object(metadata, "find_ffmpeg", return_value=None):
                ok, err = metadata.strip_file(Path(d) / "x.mp4")
                self.assertFalse(ok)
                self.assertIn("ffmpeg not found", err)

    @unittest.skipUnless(metadata.find_ffmpeg(), "ffmpeg not available")
    def test_real_ffmpeg_removes_every_tag_without_reencoding(self):
        import subprocess
        ff = metadata.find_ffmpeg()
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "TikTok-1.mp4"
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                            "-metadata", "title=TikTok clip", "-metadata", "comment=@creator 7381", "-metadata", "description=desc",
                            "-metadata:s:v", "handler_name=VideoHandler X", str(src)], check=True)
            before = metadata.tags(src)
            self.assertEqual(before.get("title"), "TikTok clip")
            self.assertIn("encoder", before)
            ok, err = metadata.strip_file(src)
            self.assertTrue(ok, err)
            self.assertEqual(metadata.tags(src), {})
            self.assertFalse((Path(d) / "TikTok-1.clean.mp4").exists())
            probe = subprocess.run([ff, "-hide_banner", "-i", str(src)], capture_output=True, text=True).stderr
            self.assertIn("h264", probe)                       # still the same codec: stream copy, not re-encoded
            self.assertIn("aac", probe)


class DedupTests(unittest.TestCase):
    def _folders(self, root):
        a = Product(name="Skull Candle Warmer", links=["https://vm.tiktok.com/A1/", "https://vm.tiktok.com/A2/", "https://vm.tiktok.com/A3/"])
        b = Product(name="Swinging Ghost Decor", links=["https://vm.tiktok.com/B1/", "https://vm.tiktok.com/B2/"])
        for p in (a, b):
            download.download_product(p, root, FakeDownloader(), pause_s=0)
        fa, fb = root / a.slug, root / b.slug
        (fa / "TikTok-A1.mp4").write_bytes(b"same clip" * 1000)
        (fa / "TikTok-A2.mp4").write_bytes(b"same clip" * 1000)          # repost of A1 under another id
        (fa / "TikTok-A3.mp4").write_bytes(b"other clip" * 1000)
        (fa / "TikTok-A3.webm").write_bytes(b"other clip, smaller")      # same id, second container
        (fa / "TikTok-A9.mp4.part").write_bytes(b"\x00" * 50)            # interrupted download
        (fa / "TikTok-A3.clean.mp4").write_bytes(b"\x00")                 # interrupted strip
        (fb / "TikTok-B1.mp4").write_bytes(b"same clip" * 1000)          # the A1 clip again, other product
        (fb / "TikTok-B2.mp4").write_bytes(b"b only" * 1000)
        return a, b, fa, fb

    def test_removes_reposts_second_containers_leftovers_and_cross_product_copies(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, b, fa, fb = self._folders(root)
            lines = []
            counts = dedup.dedup([a, b], root, across_products=True, progress=lines.append)
            self.assertEqual((counts["duplicates"], counts["leftovers"]), (3, 2))
            self.assertGreater(counts["bytes"], 9000 * 2)
            self.assertEqual(sorted(f.name for f in fa.iterdir()), ["TikTok-A1.mp4", "TikTok-A3.mp4", "links.txt", "manifest.json"])
            self.assertEqual(sorted(f.name for f in fb.iterdir()), ["TikTok-B2.mp4", "links.txt", "manifest.json"])
            ma = {v["url"]: v for v in json.loads((fa / "manifest.json").read_text())["videos"]}
            self.assertEqual((ma["https://vm.tiktok.com/A2/"]["status"], ma["https://vm.tiktok.com/A2/"]["file"]), ("downloaded", "TikTok-A1.mp4"))
            mb = {v["url"]: v for v in json.loads((fb / "manifest.json").read_text())["videos"]}
            self.assertEqual((mb["https://vm.tiktok.com/B1/"]["status"], mb["https://vm.tiktok.com/B1/"]["file"], mb["https://vm.tiktok.com/B1/"]["duplicate_of"]),
                             ("duplicate", "", "skull-candle-warmer/TikTok-A1.mp4"))
            self.assertTrue(any("TikTok-A9.mp4.part" in l for l in lines))
            # a second download run refetches nothing: A2 maps to the kept file, B1 is a known duplicate
            fake = FakeDownloader()
            res = download.download_product(b, root, fake, pause_s=0)
            self.assertEqual(fake.calls, [])
            self.assertEqual([r.status for r in res], ["duplicate", "exists"])
            fake = FakeDownloader()
            res = download.download_product(a, root, fake, pause_s=0)
            self.assertEqual(fake.calls, [])
            self.assertEqual([(r.status, r.file) for r in res], [("exists", "TikTok-A1.mp4"), ("exists", "TikTok-A1.mp4"), ("exists", "TikTok-A3.mp4")])
            # and a second dedup pass finds nothing
            self.assertEqual(dedup.dedup([a, b], root, across_products=True), {"leftovers": 0, "duplicates": 0, "bytes": 0})

    def test_per_product_scope_and_dry_run_keep_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a, b, fa, fb = self._folders(root)
            counts = dedup.dedup([a, b], root, across_products=True, dry_run=True)
            self.assertEqual((counts["duplicates"], counts["leftovers"]), (3, 2))
            self.assertTrue((fb / "TikTok-B1.mp4").exists() and (fa / "TikTok-A2.mp4").exists() and (fa / "TikTok-A9.mp4.part").exists())
            counts = dedup.dedup([a, b], root, across_products=False)
            self.assertEqual(counts["duplicates"], 2)
            self.assertTrue((fb / "TikTok-B1.mp4").exists())                # cross-product copy kept in this scope
            self.assertFalse((fa / "TikTok-A2.mp4").exists())


class CliTests(unittest.TestCase):
    def test_links_and_dry_run_download_from_csv(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stdout") as out:
            self.assertEqual(cli.main(["links", "--csv", str(FIX), "--urls", "--only", "trunk"]), 0)
            self.assertEqual(cli.main(["download", "--csv", str(FIX), "--dry-run", "--out", d, "--only", "nikki", "--max", "2"]), 0)
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
            self.assertTrue((Path(d) / "the-freaky-nikki-costume" / "links.txt").exists())
            self.assertFalse((Path(d) / "the-freaky-nikki-costume" / "manifest.json").exists())
        self.assertIn("Trunk Horror Prop", printed)
        self.assertIn("https://www.instagram.com/reel/Ddb7uPzx9gA/", printed)
        self.assertIn("2 would be fetched", printed)

    def test_clean_command_without_ffmpeg_says_so(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stdout") as out, mock.patch.object(metadata, "find_ffmpeg", return_value=None):
            self.assertEqual(cli.main(["clean", "--csv", str(FIX), "--out", d]), 0)
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
        self.assertIn("ffmpeg not found", printed)

    def test_dedup_command_dry_run(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stdout") as out:
            p = Product(name="Trunk Horror Prop", links=["https://x/1", "https://x/2"])
            download.download_product(p, Path(d), FakeDownloader(), pause_s=0)
            self.assertEqual(cli.main(["dedup", "--csv", str(FIX), "--out", d, "--dry-run"]), 0)
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
            self.assertTrue((Path(d) / "trunk-horror-prop" / "TikTok-1.mp4").exists())
        self.assertIn("1 duplicate videos", printed)            # FakeDownloader writes identical bytes for both links
        self.assertIn("would free", printed)

    def test_unknown_product_is_a_clear_exit(self):
        with self.assertRaises(SystemExit) as cm:
            cli.main(["links", "--csv", str(FIX), "--only", "zzz"])
        self.assertIn("no products matching zzz", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
