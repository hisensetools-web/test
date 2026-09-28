"""Settings for vidDownloader.py (all optional, from .env next to vidDownloader.py). Self-contained: the vidDownloader.py + viddownloader/
pair can be copied to any folder and run there without the rest of this repository."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # the folder that holds vidDownloader.py


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, no interpolation. Existing environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def env(name: str, default: str = "") -> str:
    """VIDDL_<name>, or the tool's previous ADSPY_<name> spelling so an older .env keeps working."""
    return os.environ.get("VIDDL_" + name, os.environ.get("ADSPY_" + name, default))


USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)


def slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe folder name from a product name."""
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return (text or "product")[:max_len].rstrip("-")

# ClickUp, the only source: the Product Research list, one task per product, links in the description.
CLICKUP_TOKEN = env("CLICKUP_TOKEN", "").strip()          # personal token, pk_...
CLICKUP_LIST_ID = env("CLICKUP_LIST_ID", "901222590753").strip()
CLICKUP_SKIP_STATUSES = tuple(s.strip() for s in env("CLICKUP_SKIP_STATUSES", "cancelled,lesson").split(","))

# One folder per product under here: vidDownloader_output/<product-slug>/
OUTPUT_ROOT = Path(env("OUTPUT_DIR", ROOT / "vidDownloader_output"))
if not OUTPUT_ROOT.exists() and (ROOT / "adspy_output").exists() and not env("OUTPUT_DIR"):
    OUTPUT_ROOT = ROOT / "adspy_output"          # folder from the tool's previous name: keep using it rather than refetch

# Instagram (and sometimes TikTok) refuse anonymous requests after a while; either a Netscape cookies.txt
# exported from the browser, or the browser name so yt-dlp reads its cookie store directly (chrome, edge, firefox).
COOKIES_FILE = env("COOKIES", "").strip()
COOKIES_FROM_BROWSER = env("COOKIES_FROM_BROWSER", "").strip()

PAUSE_S = float(env("PAUSE_S", "3"))          # polite pause between downloads (randomised up to 2x)
# When TikTok / Instagram close the connection or time out (rate limiting), wait this long and retry the same link,
# one wait per attempt; the link is recorded as failed only after the last one.
THROTTLE_WAITS = tuple(int(x) for x in env("THROTTLE_WAITS", "30,60,120,300").split(",") if x.strip())
IMPERSONATE = env("IMPERSONATE", "chrome").strip()   # browser TLS fingerprint for yt-dlp (needs curl_cffi); empty = off
RETRIES = int(env("RETRIES", "3"))
FORMAT_OVERRIDE = env("FORMAT", "").strip()    # a raw yt-dlp -f expression, if you ever need one
FFMPEG = env("FFMPEG", "").strip()              # full path to ffmpeg.exe when it is not on PATH
DEDUP = env("DEDUP", "1").strip().lower() not in ("0", "false", "no")
DEDUP_ACROSS = env("DEDUP_ACROSS", "1").strip().lower() not in ("0", "false", "no")   # one copy across all products, not per product
STRIP_METADATA = env("STRIP_METADATA", "1").strip().lower() not in ("0", "false", "no")

REQUEST_TIMEOUT = (10, 60)
