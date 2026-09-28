"""Download every link of a product into its folder with yt-dlp: no watermark, highest quality, resumable.

Watermarks: TikTok serves two kinds of file. The "download" file carries the moving TikTok logo + handle;
the "play" streams (what the app itself plays) are clean. yt-dlp labels the former `watermarked`, so the
format expression below rejects any format whose note contains that word and takes the best of the rest
(resolution, then bitrate). Instagram reels are served clean. Nothing is re-encoded.
"""
from __future__ import annotations

import json
import logging
import random
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import config, metadata
from .sheet import Product

log = logging.getLogger("viddownloader.download")

MANIFEST = "manifest.json"
LINKS = "links.txt"
OUTTMPL = "%(extractor_key)s-%(id)s.%(ext)s"   # the file name is the video id, so any link to the same video finds the same file

NO_WATERMARK = "[format_note!*=atermark]"     # case-insensitive enough: 'watermarked' / 'Watermarked'
FORMAT_WITH_FFMPEG = f"bv*{NO_WATERMARK}+ba/b{NO_WATERMARK}/bv*+ba/b"
FORMAT_NO_FFMPEG = f"b{NO_WATERMARK}/b"       # a single muxed file; nothing to merge


@dataclass
class VideoResult:
    url: str
    status: str                  # downloaded | exists | failed | dry-run | duplicate
    file: str = ""
    video_id: str = ""
    title: str = ""
    width: int | None = None
    height: int | None = None
    format_note: str = ""
    error: str = ""
    at: str = ""
    clean: bool = False          # metadata stripped after the download
    duplicate_of: str = ""       # "<slug>/<file>" when the dedup pass removed this file as a copy of that one


class Downloader(Protocol):
    def __call__(self, url: str, dest: Path) -> VideoResult: ...


TRANSIENT_MARKERS = ("connection aborted", "connection reset", "forcibly closed", "10054", "10053", "timed out", "timeout",
                     "http error 429", "too many requests", "http error 503", "http error 502", "temporarily", "remote end closed",
                     "unable to download webpage", "unable to download json", "network is unreachable", "name resolution")
TRANSIENT_TEXT = "the site closed the connection or timed out (rate limit); retried with backoff"


def is_transient(error: str) -> bool:
    """A network / rate-limit failure worth retrying after a pause, as opposed to a dead or private video."""
    e = (error or "").lower()
    if "not available" in e or "private" in e or "removed" in e or "no video found" in e or "login required" in e:
        return False
    return any(m in e for m in TRANSIENT_MARKERS)


class _YdlLogger:
    """Keeps yt-dlp's own chatter out of the terminal (it is in `-v` debug logging); errors come back through the result."""
    def __init__(self):
        self.last_error = ""

    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    def info(self, msg):
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg):
        self.last_error = str(msg)
        log.debug("yt-dlp error: %s", msg)


def impersonate_target():
    """A browser TLS fingerprint for yt-dlp when curl_cffi is installed (TikTok resets plain-Python connections)."""
    if not config.IMPERSONATE:
        return None
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str(config.IMPERSONATE)
    except Exception:  # noqa: BLE001  optional dependency
        return None


def ffmpeg_available() -> bool:
    return metadata.find_ffmpeg() is not None


def format_expression(has_ffmpeg: bool | None = None) -> str:
    if config.FORMAT_OVERRIDE:
        return config.FORMAT_OVERRIDE
    if has_ffmpeg is None:
        has_ffmpeg = ffmpeg_available()
    return FORMAT_WITH_FFMPEG if has_ffmpeg else FORMAT_NO_FFMPEG


def ydl_options(dest: Path, cookies: str = "", cookies_from_browser: str = "", quiet: bool = True) -> dict:
    opts = {
        "format": format_expression(),
        "outtmpl": str(dest / OUTTMPL),
        "retries": config.RETRIES,
        "fragment_retries": config.RETRIES,
        "socket_timeout": 60,
        "noplaylist": True,
        "quiet": quiet,
        "no_warnings": quiet,
        "noprogress": quiet,
        "restrictfilenames": True,
        "writethumbnail": False,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}] if ffmpeg_available() else [],
        "http_headers": {"User-Agent": config.USER_AGENT},
    }
    if quiet:
        opts["logger"] = _YdlLogger()
    target = impersonate_target()
    if target is not None:
        opts["impersonate"] = target
    ff = metadata.find_ffmpeg()
    if ff and shutil.which("ffmpeg") is None:
        opts["ffmpeg_location"] = ff
    cookies = cookies or config.COOKIES_FILE
    browser = cookies_from_browser or config.COOKIES_FROM_BROWSER
    if cookies:
        opts["cookiefile"] = cookies
    elif browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def _first_downloaded_path(info: dict) -> str:
    for d in info.get("requested_downloads") or []:
        if d.get("filepath"):
            return d["filepath"]
    return info.get("filepath") or info.get("_filename") or ""


def _first_video(info: dict) -> dict:
    """An Instagram post with several clips comes back as a playlist: take its first clip."""
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        return entries[0] if entries else info
    return info


def existing_file(dest: Path, extractor_key: str, video_id: str) -> Path | None:
    """The file a previous run saved for this video id, whatever link it was reached through."""
    if not video_id:
        return None
    for f in dest.glob(f"{extractor_key or '*'}-{video_id}.*"):
        if f.is_file() and f.suffix != ".part" and ".clean." not in f.name:
            return f
    return None


def ytdlp_downloader(cookies: str = "", cookies_from_browser: str = "", quiet: bool = True) -> Downloader:
    """The real thing. Imported lazily so the sheet / ClickUp commands never need yt-dlp.

    Two steps on purpose: resolve the link to a video id first (no download), look for that id in the folder, and only
    then download. yt-dlp's own download archive is not used: it answers an already-seen video with an empty result on
    some sites (Instagram), which is indistinguishable from a dead link."""
    import yt_dlp

    def _download(url: str, dest: Path) -> VideoResult:
        opts = ydl_options(dest, cookies, cookies_from_browser, quiet)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    return VideoResult(url=url, status="failed", error="no video found at this link")
                video = _first_video(info)
                vid, key = str(video.get("id") or ""), video.get("extractor_key") or ""
                have = existing_file(dest, key, vid)
                if have is not None:
                    return VideoResult(url=url, status="exists", file=have.name, video_id=vid, title=(video.get("title") or "")[:120],
                                       width=video.get("width"), height=video.get("height"))
                done = ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError as e:
            err = str(e).split("\n")[0][:300]
            return VideoResult(url=url, status="failed", error=TRANSIENT_TEXT if is_transient(err) else err)
        except Exception as e:  # noqa: BLE001  curl_cffi / transport errors surface as their own classes
            err = f"{type(e).__name__}: {str(e)[:200]}"
            return VideoResult(url=url, status="failed", error=TRANSIENT_TEXT if is_transient(err) else err)
        done = _first_video(done or {})
        path = _first_downloaded_path(done)
        if not path:
            have = existing_file(dest, done.get("extractor_key") or key, str(done.get("id") or vid))
            if have is None:
                return VideoResult(url=url, status="failed", video_id=vid, error="yt-dlp finished without writing a file")
            path = str(have)
        return VideoResult(url=url, status="downloaded", file=Path(path).name, video_id=str(done.get("id") or vid),
                           title=(done.get("title") or "")[:120], width=done.get("width"), height=done.get("height"),
                           format_note=done.get("format_note") or done.get("format") or "")

    return _download


# --------------------------------------------------------------------------- per product
def load_manifest(folder: Path) -> dict[str, VideoResult]:
    fp = folder / MANIFEST
    if not fp.exists():
        return {}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for row in data.get("videos", []):
        try:
            out[row["url"]] = VideoResult(**{k: row[k] for k in VideoResult.__dataclass_fields__ if k in row})
        except TypeError:
            continue
    return out


def save_manifest(folder: Path, product: Product, results: dict[str, VideoResult]) -> None:
    rows = [asdict(r) for r in results.values()]
    done = sum(1 for r in rows if r["status"] in ("downloaded", "exists"))
    payload = {"product": product.name, "slug": product.slug, "links": len(product.links), "downloaded": done,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "videos": rows}
    (folder / MANIFEST).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def download_product(product: Product, out_root: Path, downloader: Downloader, max_videos: int | None = None,
                     force: bool = False, dry_run: bool = False, pause_s: float | None = None,
                     progress: Callable[[str], None] | None = None) -> list[VideoResult]:
    """Fetch every link of one product into out_root/<slug>/. Links already downloaded (file present) are skipped
    unless `force`. Failures are recorded and never stop the product. Returns this run's results, one per link."""
    folder = out_root / product.slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LINKS).write_text("\n".join(product.links) + ("\n" if product.links else ""), encoding="utf-8")
    manifest = load_manifest(folder)
    pause = config.PAUSE_S if pause_s is None else pause_s
    say = progress or (lambda s: None)
    results: list[VideoResult] = []
    links = product.links[:max_videos] if max_videos else product.links
    for i, url in enumerate(links, start=1):
        prev = manifest.get(url)
        if prev and not force and prev.status in ("downloaded", "exists") and prev.file and (folder / prev.file).exists():
            r = VideoResult(**{**asdict(prev), "status": "exists"})
            results.append(r)
            say(f"  [{i}/{len(links)}] exists      {r.file}")
            continue
        if prev and not force and prev.status == "duplicate":
            r = VideoResult(**asdict(prev))
            results.append(r)
            say(f"  [{i}/{len(links)}] duplicate   of {prev.duplicate_of} (not fetched again)")
            continue
        if dry_run:
            r = VideoResult(url=url, status="dry-run")
            results.append(r)
            say(f"  [{i}/{len(links)}] would fetch {url}")
            continue
        r = downloader(url, folder)
        for wait in (config.THROTTLE_WAITS if r.status == "failed" and is_transient(r.error) else ()):
            say(f"  [{i}/{len(links)}] rate limited, waiting {wait}s then retrying {url}")
            time.sleep(wait)
            r = downloader(url, folder)
            if not (r.status == "failed" and is_transient(r.error)):
                break
        r.at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        results.append(r)
        manifest[url] = r
        save_manifest(folder, product, manifest)
        if r.status == "failed":
            say(f"  [{i}/{len(links)}] FAILED      {url}  ({r.error})")
        else:
            dims = f" {r.width}x{r.height}" if r.width and r.height else ""
            say(f"  [{i}/{len(links)}] {r.status:<11} {r.file}{dims}")
        if pause and i < len(links):
            time.sleep(random.uniform(pause, pause * 2))
    if not dry_run:
        save_manifest(folder, product, manifest)
    return results


def summarise(results: list[VideoResult]) -> dict[str, int]:
    out = {"downloaded": 0, "exists": 0, "failed": 0, "dry-run": 0, "duplicate": 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def strip_product(product: Product, out_root: Path, force: bool = False, progress: Callable[[str], None] | None = None,
                  stripper: Callable[[Path], tuple[bool, str]] | None = None) -> dict[str, int]:
    """Remove the metadata of every video in the product folder that is not yet marked clean in the manifest.
    Files not in the manifest (copied in by hand) are stripped too. Returns counts: cleaned / skipped / failed."""
    folder = out_root / product.slug
    say = progress or (lambda s: None)
    counts = {"cleaned": 0, "skipped": 0, "failed": 0}
    if not folder.exists():
        return counts
    strip = stripper or metadata.strip_file
    manifest = load_manifest(folder)
    by_file = {r.file: r for r in manifest.values() if r.file}
    for f in metadata.video_files(folder):
        entry = by_file.get(f.name)
        if entry and entry.clean and not force:
            counts["skipped"] += 1
            continue
        ok, err = strip(f)
        if ok:
            counts["cleaned"] += 1
            if entry:
                entry.clean = True
            say(f"  cleaned     {f.name}")
        else:
            counts["failed"] += 1
            say(f"  NOT cleaned {f.name}  ({err})")
    if manifest:
        save_manifest(folder, product, manifest)
    return counts
