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

REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds
REQUEST_RETRIES = 3
REQUEST_DELAY_S = float(os.environ.get("REQUEST_DELAY_S", "1.0"))
PAGE_LIMIT = 250
MAX_PAGES = 60  # 60 * 250 = 15k products; safety cap against infinite pagination
# Present as a desktop browser (some storefront WAFs answer 406 to bot-looking User-Agents, seen on olavita.co)
# but ask for JSON explicitly: an HTML-first Accept makes Shopify serve the storefront HTML for /products.json.
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
OPS_DIAG_CHARS = int(os.environ.get("OPS_DIAG_CHARS", "250000"))   # size cap of the "EarlyScale Diag" Google Doc written by `tracker.py diag`

# Meta Ad Library scraper. META_ADS=1 in .env lets `run` call it after the Shopify pass.
META_ADS_ENABLED = os.environ.get("META_ADS", "").strip() in ("1", "true", "yes")
META_WAIT_MIN = float(os.environ.get("META_WAIT_MIN", "3"))
META_WAIT_MAX = float(os.environ.get("META_WAIT_MAX", "8"))
META_MAX_SCROLLS = int(os.environ.get("META_MAX_SCROLLS", "20"))   # ~350 ads, ~2 min; the newest ads come first
# Which stores the Meta pass covers and how long it may run. META_STORES empty = every watchlist store;
# META_MAX_MINUTES caps the whole pass per day, and stores are taken least-recently-scraped first.
META_STORES = [d.strip().lower() for d in os.environ.get("META_STORES", "").split(",") if d.strip()]
META_MAX_MINUTES = float(os.environ.get("META_MAX_MINUTES", "90"))
META_MAX_ADS = int(os.environ.get("META_MAX_ADS", "3000"))
META_NAV_TIMEOUT_MS = int(os.environ.get("META_NAV_TIMEOUT_MS", "45000"))
META_CHROMIUM_PATH = os.environ.get("META_CHROMIUM_PATH", "").strip()  # optional explicit browser binary
# Override only for offline testing against tests/fake_ad_library.py
META_AD_LIBRARY_BASE = os.environ.get("META_AD_LIBRARY_BASE", "https://www.facebook.com/ads/library/")
META_STOP_AT = os.environ.get("META_STOP_AT", "").strip()             # e.g. 08:30 - the pass stops at this local time (set by run_daily.bat meta)

# Radar (store discovery)
RADAR_MAX_ADS_PER_QUERY = int(os.environ.get("RADAR_MAX_ADS_PER_QUERY", "500"))
RADAR_COUNTRY = os.environ.get("RADAR_COUNTRY", "US")
RADAR_WEB_SEARCH = os.environ.get("RADAR_WEB_SEARCH", "1").strip() not in ("0", "false", "no")
RADAR_MAX_WEB_QUERIES = int(os.environ.get("RADAR_MAX_WEB_QUERIES", "40"))
RADAR_MAX_TRIAGE = int(os.environ.get("RADAR_MAX_TRIAGE", "40"))            # new domains checked per run
RADAR_MAX_AGE_DAYS = int(os.environ.get("RADAR_MAX_AGE_DAYS", "180"))
RADAR_MIN_ACTIVE_ADS = int(os.environ.get("RADAR_MIN_ACTIVE_ADS", "10"))
RADAR_SWEEP_WEEKDAY = int(os.environ.get("RADAR_SWEEP_WEEKDAY", "6"))        # 6 = Sunday
RADAR_MAX_MINUTES = float(os.environ.get("RADAR_MAX_MINUTES", "240"))       # budget for one radar run (sweeps + triage)
RADAR_MAX_COPYCAT_QUERIES = int(os.environ.get("RADAR_MAX_COPYCAT_QUERIES", "60"))   # per sweep, least recently searched first
RADAR_FUNNEL_MIN_ADS = int(os.environ.get("RADAR_FUNNEL_MIN_ADS", "3"))     # non-Shopify lander needs this many sweep ads to be tracked as a funnel
RADAR_CHECK_WORKERS = int(os.environ.get("RADAR_CHECK_WORKERS", "6"))       # concurrent Shopify checks in triage stage 1
RADAR_RESEARCH_DAYS = int(os.environ.get("RADAR_RESEARCH_DAYS", "7"))       # do not re-run a candidate's Ad Library search sooner than this
RADAR_RESWEEP_DAYS = int(os.environ.get("RADAR_RESWEEP_DAYS", "6"))         # a phrase searched this recently is skipped (sweep resume)
RADAR_REFRESH_DAYS = int(os.environ.get("RADAR_REFRESH_DAYS", "7"))         # re-read a Shopify candidate's catalogue this often (new products)
RADAR_TRIAGE_MINUTES = float(os.environ.get("RADAR_TRIAGE_MINUTES", "60"))  # part of RADAR_MAX_MINUTES kept back from the sweeps for triage
RADAR_SEARCH_MAX_ADS = int(os.environ.get("RADAR_SEARCH_MAX_ADS", "200"))   # ads collected per candidate page/domain search
# Catalogue adapters for non-Shopify platforms (earlyscale/platforms.py)
CATALOGUE_MAX_PAGES = int(os.environ.get("CATALOGUE_MAX_PAGES", "60"))        # product pages the generic adapter reads per store per run
CATALOGUE_MAX_SITEMAPS = int(os.environ.get("CATALOGUE_MAX_SITEMAPS", "15"))
CATALOGUE_MAX_URLS = int(os.environ.get("CATALOGUE_MAX_URLS", "3000"))
CATALOGUE_REFRESH_DAYS = int(os.environ.get("CATALOGUE_REFRESH_DAYS", "3"))    # re-read a product page this often
CATALOGUE_REDETECT_DAYS = int(os.environ.get("CATALOGUE_REDETECT_DAYS", "14")) # trust the detected platform this long
