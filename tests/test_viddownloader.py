"""viddownloader unit tests: ClickUp task parsing and fetching, product selection, the download loop, manifests, throttle backoff, metadata strip, dedup, CLI."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from viddownloader import cli, clickup, dedup, download, metadata
from viddownloader.products import Product, select

CU_FIX = Path(__file__).parent / "fixtures_viddownloader" / "clickup_tasks.json"


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
    """Stands in for yt_dlp.YoutubeDL: behaviour keyed on the URL. Records whether a download was attempted."""
    last_opts = None
    downloads = []

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
        if "nothing" in url:
            return None
        vid = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        info = {"id": vid, "extractor_key": "TikTok", "title": "clip " + vid, "width": 1080, "height": 1920, "format_note": "Direct video"}
        if "carousel" in url:
            return {"_type": "playlist", "entries": [info, {"id": "second", "extractor_key": "TikTok"}]}
        return info

    def process_ie_result(self, info, download=True):
        assert download
        video = info["entries"][0] if info.get("_type") == "playlist" else info
        FakeYDL.downloads.append(video["id"])
        f = self.dest / f"TikTok-{video['id']}.mp4"
        f.write_bytes(b"\x00")
        video["requested_downloads"] = [{"filepath": str(f)}]
        return info


class YtdlpWrapperTests(unittest.TestCase):
    def test_wrapper_resolves_first_then_downloads_only_when_missing(self):
        import yt_dlp
        with tempfile.TemporaryDirectory() as d, mock.patch.object(yt_dlp, "YoutubeDL", FakeYDL):
            dest = Path(d)
            FakeYDL.downloads.clear()
            dl = download.ytdlp_downloader(cookies_from_browser="edge")
            r = dl("https://vm.tiktok.com/AAA/", dest)
            self.assertEqual((r.status, r.file, r.video_id, r.width, r.title), ("downloaded", "TikTok-AAA.mp4", "AAA", 1080, "clip AAA"))
            self.assertEqual(FakeYDL.last_opts["cookiesfrombrowser"], ("edge",))
            self.assertNotIn("download_archive", FakeYDL.last_opts)
            self.assertIn("atermark", FakeYDL.last_opts["format"])
            # the same video through another link (share token): found by id, nothing downloaded again
            r = dl("https://www.tiktok.com/@x/video/AAA?stkn=zzz", dest)
            self.assertEqual((r.status, r.file), ("exists", "TikTok-AAA.mp4"))
            self.assertEqual(FakeYDL.downloads, ["AAA"])
            # file deleted by hand: downloaded again
            (dest / "TikTok-AAA.mp4").unlink()
            r = dl("https://vm.tiktok.com/AAA/", dest)
            self.assertEqual(r.status, "downloaded")
            self.assertEqual(FakeYDL.downloads, ["AAA", "AAA"])
            # errors
            r = dl("https://vm.tiktok.com/gone/", dest)
            self.assertEqual((r.status, r.error), ("failed", "ERROR: [TikTok] 123: Video not available"))
            r = dl("https://vm.tiktok.com/nothing/", dest)
            self.assertEqual((r.status, r.error), ("failed", "no video found at this link"))
            # a multi-clip post: first clip
            r = dl("https://www.instagram.com/p/carousel/", dest)
            self.assertEqual((r.status, r.file), ("downloaded", "TikTok-carousel.mp4"))
            r = dl("https://www.instagram.com/p/carousel/", dest)
            self.assertEqual(r.status, "exists")


class CookieTests(unittest.TestCase):
    def test_missing_browser_is_a_warning_not_a_failure(self):
        with mock.patch("yt_dlp.cookies.extract_cookies_from_browser", side_effect=FileNotFoundError("could not find firefox cookies database in 'C:\\x'")):
            cookies, browser, warning = download.usable_cookies("", "firefox")
        self.assertEqual((cookies, browser), ("", ""))
        self.assertIn("firefox login not available", warning)
        self.assertIn("continuing without a login", warning)
        with mock.patch("yt_dlp.cookies.extract_cookies_from_browser", return_value=object()):
            self.assertEqual(download.usable_cookies("", "firefox"), ("", "firefox", ""))
        self.assertEqual(download.usable_cookies("", "")[2], "")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "c.txt"
            self.assertIn("not found", download.usable_cookies(str(f), "firefox")[2])
            f.write_text("# Netscape HTTP Cookie File\n")
            self.assertEqual(download.usable_cookies(str(f), "firefox"), (str(f), "", ""))

    def test_ydl_options_only_carry_what_was_verified(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(download.config, "COOKIES_FROM_BROWSER", "firefox"):
            opts = download.ydl_options(Path(d))            # config alone never puts an unverified browser in
            self.assertNotIn("cookiesfrombrowser", opts)
            self.assertNotIn("cookiefile", opts)


class ThrottleTests(unittest.TestCase):
    def test_transient_vs_permanent(self):
        self.assertTrue(download.is_transient("ERROR: [vm.tiktok] X: Unable to download webpage: ('Connection aborted.', ConnectionResetError(10054, ...))"))
        self.assertTrue(download.is_transient("HTTPSConnectionPool(host='vm.tiktok.com', port=443): Read timed out."))
        self.assertTrue(download.is_transient("HTTP Error 429: Too Many Requests"))
        self.assertFalse(download.is_transient("ERROR: [TikTok] 123: Video not available"))
        self.assertFalse(download.is_transient("no video found at this link"))
        self.assertFalse(download.is_transient(""))

    def test_rate_limited_link_is_retried_after_waits_then_recorded(self):
        calls = []

        def flaky(url, dest):
            calls.append(url)
            if len(calls) < 3:
                return download.VideoResult(url=url, status="failed", error=download.TRANSIENT_TEXT)
            f = dest / "TikTok-ok.mp4"
            f.write_bytes(b"\x00")
            return download.VideoResult(url=url, status="downloaded", file=f.name)

        p = Product(name="Throttled", links=["https://vm.tiktok.com/A/"])
        with tempfile.TemporaryDirectory() as d, mock.patch("time.sleep") as sleep, mock.patch.object(download.config, "THROTTLE_WAITS", (30, 60, 120)):
            lines = []
            res = download.download_product(p, Path(d), flaky, pause_s=0, progress=lines.append)
            self.assertEqual([r.status for r in res], ["downloaded"])
            self.assertEqual(len(calls), 3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60])
            self.assertTrue(any("rate limited, waiting 30s" in l for l in lines))
            # permanent failure: no waiting at all
            calls.clear()
            dead = lambda url, dest: download.VideoResult(url=url, status="failed", error="Video not available")
            sleep.reset_mock()
            res = download.download_product(Product(name="Dead", links=["https://vm.tiktok.com/B/"]), Path(d), dead, pause_s=0)
            self.assertEqual(res[0].status, "failed")
            sleep.assert_not_called()
            # always transient: every wait used, then recorded as failed
            always = lambda url, dest: download.VideoResult(url=url, status="failed", error=download.TRANSIENT_TEXT)
            res = download.download_product(Product(name="Never", links=["https://vm.tiktok.com/C/"]), Path(d), always, pause_s=0)
            self.assertEqual((res[0].status, res[0].error), ("failed", download.TRANSIENT_TEXT))
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 60, 120])

    def test_ydl_options_quiet_logger_and_impersonation(self):
        with tempfile.TemporaryDirectory() as d:
            opts = download.ydl_options(Path(d))
            self.assertIsInstance(opts["logger"], download._YdlLogger)
            try:
                import curl_cffi  # noqa: F401
                self.assertEqual(str(opts["impersonate"]), "chrome")
            except ImportError:
                self.assertNotIn("impersonate", opts)
            with mock.patch.object(download.config, "IMPERSONATE", ""):
                self.assertNotIn("impersonate", download.ydl_options(Path(d)))


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


class ClickUpParseTests(unittest.TestCase):
    def setUp(self):
        self.tasks = json.loads(CU_FIX.read_text())["tasks"]
        self.by = {p.name: p for p in clickup.products_from_tasks(self.tasks)}

    def test_only_video_links_from_the_description_any_heading(self):
        self.assertEqual(self.by["Trunk Horror Prop"].links, ["https://www.instagram.com/reel/Ddb7uPzx9gA/", "https://www.instagram.com/reel/Ddh6ES_REyO/"])
        self.assertEqual(self.by["The Freaky Nikki Costume"].links, ["https://vm.tiktok.com/ZN8My63PS/", "https://vm.tiktok.com/ZN8My6Dx9/"])   # competition + post ids ignored
        self.assertEqual(self.by["The Pawcket Hoodie"].links, [])

    def test_bare_tiktok_ids_are_rebuilt_from_the_prefix_note(self):
        self.assertEqual(self.by["Winking Spidey Mask"].links, ["https://www.tiktok.com/@thespideygear/video/7688192053603208470",
                                                                "https://www.tiktok.com/@thespideygear/video/7687462050565459222",
                                                                "https://www.tiktok.com/@thespideygear/video/7686184836092103938"])

    def test_free_form_task_scheme_added_profiles_ignored_share_token_kept(self):
        self.assertEqual(self.by["Rosabella"].links, ["https://www.tiktok.com/@shop/video/7400830224608382241", "https://www.instagram.com/reel/DdYSYT4O9Zx/?stkn=abc"])
        # the plain-text description (no scheme) gives the same result
        p = clickup.task_to_product({"name": "Rosabella", "description": self.tasks[-1]["description"]})
        self.assertEqual(p.links, self.by["Rosabella"].links)

    def test_cancelled_and_lesson_skipped_configurable(self):
        self.assertNotIn("Wicked For Good Tumbler", self.by)
        self.assertNotIn("Glow Panel - Illuminated Wall Art", self.by)
        names = [p.name for p in clickup.products_from_tasks(self.tasks, skip_statuses=("cancelled",))]
        self.assertIn("Glow Panel - Illuminated Wall Art", names)
        wicked = {p.name: p for p in clickup.products_from_tasks(self.tasks, skip_statuses=())}["Wicked For Good Tumbler"]
        self.assertEqual(wicked.links, ["https://vm.tiktok.com/ZN8CANCEL/"])          # pipiads is never a video link

    def test_status_filter_keeps_only_the_named_statuses(self):
        self.assertEqual(self.by["Winking Spidey Mask"].status, "ready to launch")
        ready = clickup.products_from_tasks(self.tasks, statuses=["Ready To Launch"])
        self.assertEqual([p.name for p in ready], ["Winking Spidey Mask"])
        two = clickup.products_from_tasks(self.tasks, statuses="ready to launch, scaling")
        self.assertEqual([p.name for p in two], ["Winking Spidey Mask", "Rosabella"])
        # the skip list still wins, and an empty filter means every status but the skipped ones
        self.assertEqual(clickup.products_from_tasks(self.tasks, statuses=["cancelled"]), [])
        self.assertEqual(len(clickup.products_from_tasks(self.tasks, statuses=())), len(self.by))
        self.assertEqual(clickup.normalise_statuses(None), ())

    def test_slug_is_the_folder_name_and_select_filters(self):
        self.assertEqual(self.by["Trunk Horror Prop"].slug, "trunk-horror-prop")
        self.assertEqual(self.by["Winking Spidey Mask"].slug, "winking-spidey-mask")
        products = list(self.by.values())
        self.assertEqual([p.name for p in select(products, ["spidey", "TRUNK"])], ["Trunk Horror Prop", "Winking Spidey Mask"])
        self.assertEqual(select(products, None), products)
        self.assertEqual(select(products, ["zzz"]), [])


class ClickUpFetchTests(unittest.TestCase):
    def test_pages_until_last_page_with_token_header(self):
        page0 = {"tasks": [{"name": "A", "status": {"status": "testing"}, "markdown_description": "https://vm.tiktok.com/a/"}], "last_page": False}
        page1 = {"tasks": [{"name": "B", "status": {"status": "testing"}, "markdown_description": "https://vm.tiktok.com/b/"}], "last_page": True}
        session = mock.Mock()
        session.get.side_effect = [mock.Mock(status_code=200, json=lambda: page0), mock.Mock(status_code=200, json=lambda: page1)]
        with mock.patch("time.sleep"):
            products = clickup.fetch_products("123", "pk_test", session=session)
        self.assertEqual([p.name for p in products], ["A", "B"])
        call = session.get.call_args_list[0]
        self.assertEqual(call.args[0], "https://api.clickup.com/api/v2/list/123/task")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "pk_test")
        self.assertEqual(call.kwargs["params"]["include_markdown_description"], "true")
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["page"], 1)

    def test_errors_are_explained(self):
        with self.assertRaises(clickup.ClickUpError) as cm:
            clickup.fetch_tasks("123", "", session=mock.Mock())
        self.assertIn("VIDDL_CLICKUP_TOKEN", str(cm.exception))
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=401, text="")
        with self.assertRaises(clickup.ClickUpError) as cm:
            clickup.fetch_tasks("123", "pk_bad", session=session)
        self.assertIn("401", str(cm.exception))


class CliTests(unittest.TestCase):
    """Every command goes through ClickUp; the API is a mocked session returning the fixture."""
    def _session(self):
        data = json.loads(CU_FIX.read_text())
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=200, json=lambda: data)
        return session

    def test_links_and_dry_run_download(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stdout") as out, mock.patch("requests.Session", return_value=self._session()):
            self.assertEqual(cli.main(["links", "--clickup-token", "pk_x", "--clickup-list", "1", "--urls", "--only", "trunk"]), 0)
            self.assertEqual(cli.main(["download", "--clickup-token", "pk_x", "--clickup-list", "1", "--dry-run", "--out", d, "--only", "spidey", "--max", "2"]), 0)
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
            self.assertTrue((Path(d) / "winking-spidey-mask" / "links.txt").exists())
            self.assertFalse((Path(d) / "winking-spidey-mask" / "manifest.json").exists())
        self.assertIn("ClickUp list 1", printed)
        self.assertIn("https://www.instagram.com/reel/Ddb7uPzx9gA/", printed)
        self.assertIn("2 would be fetched", printed)

    def test_dedup_command_dry_run(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stdout") as out, mock.patch("requests.Session", return_value=self._session()):
            p = Product(name="Trunk Horror Prop", links=["https://x/1", "https://x/2"])
            download.download_product(p, Path(d), FakeDownloader(), pause_s=0)
            self.assertEqual(cli.main(["dedup", "--clickup-token", "pk_x", "--clickup-list", "1", "--out", d, "--dry-run"]), 0)
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
            self.assertTrue((Path(d) / "trunk-horror-prop" / "TikTok-1.mp4").exists())
        self.assertIn("1 duplicate videos", printed)            # FakeDownloader writes identical bytes for both links
        self.assertIn("would free", printed)

    def test_clean_command_without_ffmpeg_says_so(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stdout") as out, mock.patch.object(metadata, "find_ffmpeg", return_value=None), \
                mock.patch("requests.Session", return_value=self._session()):
            self.assertEqual(cli.main(["clean", "--clickup-token", "pk_x", "--clickup-list", "1", "--out", d]), 0)
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
        self.assertIn("ffmpeg not found", printed)

    def test_missing_token_is_a_clear_error_no_fallback(self):
        with mock.patch("sys.stderr") as err, mock.patch.object(clickup.config, "CLICKUP_TOKEN", ""):
            self.assertEqual(cli.main(["links", "--clickup-list", "1"]), 2)
            printed = "".join(str(c.args[0]) for c in err.write.call_args_list)
        self.assertIn("VIDDL_CLICKUP_TOKEN", printed)
        self.assertNotIn("sheet", printed.lower())

    def test_unknown_product_is_a_clear_exit(self):
        with mock.patch("requests.Session", return_value=self._session()), self.assertRaises(SystemExit) as cm:
            cli.main(["links", "--clickup-token", "pk_x", "--clickup-list", "1", "--only", "zzz"])
        self.assertIn("no products matching zzz", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
