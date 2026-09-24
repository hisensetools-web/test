"""Strip every tag from the downloaded files once the downloads are done.

ffmpeg rewrites the file with stream copy (no re-encode, pixels and audio untouched) and drops global tags
(title, comment, description, artist, encoder, creation time, location...), per-stream tags, chapters and the
muxer's own encoder marker. ffprobe is not needed: `ffmpeg -i` lists what is left and `tags()` reads it back
for verification. The two tags the mp4 muxer always writes, `handler_name` (VideoHandler / SoundHandler) and
`vendor_id` (zeros), are generic and identify nothing.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger("adspy.metadata")

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
GENERIC_TAGS = {"handler_name", "vendor_id", "major_brand", "minor_version", "compatible_brands"}
STRIP_ARGS = ["-map", "0", "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1", "-c", "copy",
              "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]


def find_ffmpeg() -> str | None:
    """ADSPY_FFMPEG, then PATH, then the imageio-ffmpeg wheel if someone installed it."""
    if config.FFMPEG and Path(config.FFMPEG).exists():
        return config.FFMPEG
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001  optional
        return None


def tags(path: Path, ffmpeg: str | None = None) -> dict[str, str]:
    """Every metadata tag ffmpeg reports for the file (global and per stream), generic muxer tags left out."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace", timeout=120)
    out: dict[str, str] = {}
    in_meta = False
    for line in r.stderr.splitlines():
        if re.match(r"\s*Metadata:\s*$", line):
            in_meta = True
            continue
        m = re.match(r"\s{4,}([A-Za-z_][\w.\-]*)\s*:\s?(.*)$", line)
        if in_meta and m and m.group(1) not in GENERIC_TAGS:
            out[m.group(1)] = m.group(2).strip()
        elif not m:
            in_meta = False
    return out


def strip_file(path: Path, ffmpeg: str | None = None) -> tuple[bool, str]:
    """Rewrite `path` without metadata, in place (via a temp file, the original is replaced only on success)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not found (winget install Gyan.FFmpeg, then reopen the terminal)"
    tmp = path.with_name(path.stem + ".clean" + path.suffix)
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), *STRIP_ARGS]
    if path.suffix.lower() in (".mp4", ".m4v", ".mov"):
        args += ["-movflags", "+faststart"]
    args.append(str(tmp))
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        tmp.unlink(missing_ok=True)
        return False, f"ffmpeg failed to start or timed out: {e}"
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return False, (r.stderr.strip().splitlines() or ["ffmpeg returned %s" % r.returncode])[-1][:300]
    os.replace(tmp, path)
    return True, ""


def video_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS and ".clean" not in p.name)
