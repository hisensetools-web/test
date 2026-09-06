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
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; EarlyScaleTracker/0.1; read-only public products.json monitor)",
)
