"""Settings for adspy.py (all optional, read from .env through the shared loader in pdpkit.config)."""
from __future__ import annotations

import os
from pathlib import Path

from pdpkit.config import ROOT, USER_AGENT, load_dotenv, slugify  # noqa: F401  (one .env for every tool)

load_dotenv()

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
