"""Download every link of a product into its folder with yt-dlp: no watermark, highest quality, resumable.

Watermarks: TikTok serves two kinds of file. The "download" file carries the moving TikTok logo + handle;
the "play" streams (what the app itself plays) are clean. yt-dlp labels the former `watermarked`, so the
format expression below rejects any format whose note contains that word and takes the best of the rest
(resolution, then bitrate). Instagram reels are served clean. Nothing is re-encoded.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import config
from .sheet import Product

log = logging.getLogger("adspy.download")

MANIFEST = "manifest.json"
LINKS = "links.txt"
ARCHIVE = ".downloaded.txt"        # yt-dlp's own archive: one "extractor id" per finished video (dedups reposts across share links)
OUTTMPL = "%(extractor_key)s-%(id)s.%(ext)s"

NO_WATERMARK = "[format_note!*=atermark]"     # case-insensitive enough: 'watermarked' / 'Watermarked'
FORMAT_WITH_FFMPEG = f"bv*{NO_WATERMARK}+ba/b{NO_WATERMARK}/bv*+ba/b"
FORMAT_NO_FFMPEG = f"b{NO_WATERMARK}/b"       # a single muxed file; nothing to merge


@dataclass
class VideoResult:
    url: str
    status: str                  # downloaded | exists | failed | dry-run
    file: str = ""
    video_id: str = ""
    title: str = ""
    width: int | None = None
    height: int | None = None
    format_note: str = ""
    error: str = ""
    at: str = ""


class Downloader(Protocol):
    def __call__(self, url: str, dest: Path) -> VideoResult: ...


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


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
        "download_archive": str(dest / ARCHIVE),
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


def ytdlp_downloader(cookies: str = "", cookies_from_browser: str = "", quiet: bool = True) -> Downloader:
    """The real thing. Imported lazily so the sheet commands never need yt-dlp."""
    import yt_dlp

    def _download(url: str, dest: Path) -> VideoResult:
        opts = ydl_options(dest, cookies, cookies_from_browser, quiet)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            return VideoResult(url=url, status="failed", error=str(e).split("\n")[0][:300])
        if info is None:
            return VideoResult(url=url, status="failed", error="no video found at this link")
        if info.get("_type") == "playlist":      # an Instagram post with several clips: take the entries
            info = (info.get("entries") or [info])[0] or info
        path = _first_downloaded_path(info)
        status = "downloaded"
        if not path:                                        # in the archive already -> nothing written this time
            status = "exists"
            for f in dest.glob(f"{info.get('extractor_key', '*')}-{info.get('id', '*')}.*"):
                if f.name != ARCHIVE and f.suffix != ".part":
                    path = str(f)
                    break
            else:
                return VideoResult(url=url, status="failed", video_id=str(info.get("id") or ""),
                                   error=f"video {info.get('id')} is listed in {ARCHIVE} but its file is gone; delete that line (or the file) to refetch")
        return VideoResult(url=url, status=status, file=Path(path).name if path else "", video_id=str(info.get("id") or ""),
                           title=(info.get("title") or "")[:120], width=info.get("width"), height=info.get("height"),
                           format_note=info.get("format_note") or info.get("format") or "")

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
            out[row["url"]] = VideoResult(**{k: row.get(k, "") for k in VideoResult.__dataclass_fields__})
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
        if dry_run:
            r = VideoResult(url=url, status="dry-run")
            results.append(r)
            say(f"  [{i}/{len(links)}] would fetch {url}")
            continue
        r = downloader(url, folder)
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
            time.sleep(pause)
    if not dry_run:
        save_manifest(folder, product, manifest)
    return results


def summarise(results: list[VideoResult]) -> dict[str, int]:
    out = {"downloaded": 0, "exists": 0, "failed": 0, "dry-run": 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out
