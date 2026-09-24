"""Settings for adspy.py (all optional, from .env next to adspy.py). Self-contained: the adspy.py + adspykit/
pair can be copied to any folder and run there without the rest of this repository."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # the folder that holds adspy.py


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

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)


def slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe folder name from a product name."""
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return (text or "product")[:max_len].rstrip("-")

# The sheet: id + tab. The gid is what the browser URL shows after `#gid=`; the tab name is the fallback lookup.
SHEET_ID = os.environ.get("ADSPY_SHEET_ID", "1O35L85zhY_5WMnsG8gEKN0bKx7oziTUMlbn6r3TJ1J0").strip()
SHEET_GID = os.environ.get("ADSPY_SHEET_GID", "395636284").strip()
SHEET_TAB = os.environ.get("ADSPY_SHEET_TAB", "Main TikTok Prods V2").strip()
PRODUCT_COLUMN = os.environ.get("ADSPY_PRODUCT_COLUMN", "Product Name").strip()
LINK_COLUMN = os.environ.get("ADSPY_LINK_COLUMN", "Adspy").strip()

# One folder per product under here: adspy_output/<product-slug>/
OUTPUT_ROOT = Path(os.environ.get("ADSPY_OUTPUT_DIR", ROOT / "adspy_output"))

# Instagram (and sometimes TikTok) refuse anonymous requests after a while; either a Netscape cookies.txt
# exported from the browser, or the browser name so yt-dlp reads its cookie store directly (chrome, edge, firefox).
COOKIES_FILE = os.environ.get("ADSPY_COOKIES", "").strip()
COOKIES_FROM_BROWSER = os.environ.get("ADSPY_COOKIES_FROM_BROWSER", "").strip()

PAUSE_S = float(os.environ.get("ADSPY_PAUSE_S", "2"))          # polite pause between downloads
RETRIES = int(os.environ.get("ADSPY_RETRIES", "3"))
FORMAT_OVERRIDE = os.environ.get("ADSPY_FORMAT", "").strip()    # a raw yt-dlp -f expression, if you ever need one

REQUEST_TIMEOUT = (10, 60)
