"""Paths, .env loading and tunables. No third-party deps."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, no interpolation.
    Existing environment variables win over the file."""
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

DB_PATH = Path(os.environ.get("TRACKER_DB", ROOT / "data" / "tracker.db"))
WATCHLIST_PATH = ROOT / "watchlist.csv"
ALERTS_DIR = ROOT / "alerts"

REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds
REQUEST_RETRIES = 3
REQUEST_DELAY_S = float(os.environ.get("REQUEST_DELAY_S", "1.0"))
PAGE_LIMIT = 250
MAX_PAGES = 60  # 60 * 250 = 15k products; safety cap against infinite pagination
# Present as a desktop browser (some storefront WAFs answer 406 to bot-looking
# User-Agents, seen on olavita.co) but ask for JSON explicitly: an HTML-first Accept
# makes Shopify serve the storefront HTML for /products.json (regression, 2026-09).
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36",
)
JSON_ACCEPT = "application/json"
FALLBACK_ACCEPT = "*/*"          # tried once if a store answers 406 to application/json
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": JSON_ACCEPT,
    "Accept-Language": "en-US,en;q=0.9",
}

# Google Sheets sync (Apps Script web app, see sheets/Code.gs). Empty = disabled.
SHEETS_WEBHOOK_URL = os.environ.get("SHEETS_WEBHOOK_URL", "").strip()
SHEETS_CHUNK_BYTES = int(os.environ.get("SHEETS_CHUNK_BYTES", "40000"))
SHEETS_TIMEOUT = (10, 90)  # Apps Script can take a while on big appends

# Meta Ad Library scraper (Part B). META_ADS=1 in .env lets `run` call it after the Shopify pass.
META_ADS_ENABLED = os.environ.get("META_ADS", "").strip() in ("1", "true", "yes")
META_WAIT_MIN = float(os.environ.get("META_WAIT_MIN", "3"))
META_WAIT_MAX = float(os.environ.get("META_WAIT_MAX", "8"))
META_MAX_SCROLLS = int(os.environ.get("META_MAX_SCROLLS", "40"))
META_MAX_ADS = int(os.environ.get("META_MAX_ADS", "600"))
META_NAV_TIMEOUT_MS = int(os.environ.get("META_NAV_TIMEOUT_MS", "45000"))
META_CHROMIUM_PATH = os.environ.get("META_CHROMIUM_PATH", "").strip()  # optional explicit browser binary
# Override only for offline testing against tests/fake_ad_library.py
META_AD_LIBRARY_BASE = os.environ.get("META_AD_LIBRARY_BASE", "https://www.facebook.com/ads/library/")
