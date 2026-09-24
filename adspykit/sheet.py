"""Read one tab of the Google Sheet and turn it into (product, links) pairs.

The tab is fetched as CSV through the sheet's export endpoint (needs the sheet shared as "anyone with the link
can view"; a private sheet returns the Google sign-in page, which is detected and explained). The same parser
reads a CSV downloaded by hand (File > Download > CSV of that tab) for the `--csv` option.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

from . import config

log = logging.getLogger("adspy.sheet")

URL_RE = re.compile(r"https?://[^\s\"'<>|]+", re.IGNORECASE)
_TRAILING = ".,;:)]}\\"


class SheetAccessError(RuntimeError):
    """The tab could not be read as CSV (private sheet, wrong id/gid, network)."""


@dataclass
class Product:
    name: str
    links: list[str] = field(default_factory=list)
    row: int = 0            # 1-based row of the product name in the tab (for messages)

    @property
    def slug(self) -> str:
        return config.slugify(self.name)


# --------------------------------------------------------------------------- fetch
def export_url(sheet_id: str, gid: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def gviz_url(sheet_id: str, tab: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(tab, safe='')}"


def _looks_like_html(text: str, content_type: str) -> bool:
    head = text.lstrip()[:200].lower()
    return "text/html" in content_type.lower() or head.startswith("<!doctype") or head.startswith("<html")


def _get(session: requests.Session, url: str) -> str | None:
    """CSV text, or None when this URL does not serve the tab (login page, 4xx). Retries transport errors."""
    delay = 2.0
    for attempt in range(config.RETRIES + 1):
        try:
            r = session.get(url, timeout=config.REQUEST_TIMEOUT, headers={"User-Agent": config.USER_AGENT}, allow_redirects=True)
        except requests.RequestException as e:
            if attempt == config.RETRIES:
                raise SheetAccessError(f"could not reach Google Sheets: {e}") from e
            log.warning("sheet fetch failed (%s), retrying in %.0fs", e, delay)
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code in (401, 403, 404):
            log.debug("%s -> HTTP %s", url, r.status_code)
            return None
        if r.status_code >= 500 and attempt < config.RETRIES:
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code != 200:
            return None
        text = r.content.decode("utf-8-sig", errors="replace")
        if _looks_like_html(text, r.headers.get("Content-Type", "")):
            return None
        return text
    return None


def fetch_tab_csv(sheet_id: str = config.SHEET_ID, gid: str = config.SHEET_GID, tab: str = config.SHEET_TAB,
                  session: requests.Session | None = None) -> str:
    """The tab as CSV text: first by gid (exact tab), then by name; a clear error when neither works."""
    session = session or requests.Session()
    for url in (export_url(sheet_id, gid) if gid else None, gviz_url(sheet_id, tab) if tab else None):
        if not url:
            continue
        text = _get(session, url)
        if text is not None:
            return text
    raise SheetAccessError(
        f"Google returned the sign-in page (or nothing) for sheet {sheet_id}, tab '{tab}' (gid {gid}).\n"
        "Either share the sheet as 'Anyone with the link: Viewer' (Share > General access), or download the tab by hand\n"
        "(File > Download > Comma Separated Values while that tab is open) and run again with --csv <that file>.")


# --------------------------------------------------------------------------- parse
def extract_links(cell: str) -> list[str]:
    """Every http(s) URL in a cell, in order, trailing punctuation dropped, duplicates removed."""
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(cell or ""):
        url = m.group(0).rstrip(_TRAILING)
        # a cell rendered through markdown may carry backslash escapes before _ and &
        url = url.replace("\\_", "_").replace("\\&", "&")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _find_header(rows: list[list[str]], product_col: str, link_col: str) -> tuple[int, int, int] | None:
    want_p, want_l = product_col.strip().lower(), link_col.strip().lower()
    for i, row in enumerate(rows):
        cells = [c.strip().lower() for c in row]
        if want_p in cells and want_l in cells:
            return i, cells.index(want_p), cells.index(want_l)
    return None


def parse_products(csv_text: str, product_col: str = config.PRODUCT_COLUMN, link_col: str = config.LINK_COLUMN) -> list[Product]:
    """(product, links) per product row. A row with links but no name continues the product above it;
    a repeated header row starts a new block; rows without links are kept (so `links` can list them) only when named."""
    rows = list(csv.reader(io.StringIO(csv_text)))
    hdr = _find_header(rows, product_col, link_col)
    if hdr is None:
        raise SheetAccessError(f"no header row with both '{product_col}' and '{link_col}' columns in the CSV")
    h, pi, li = hdr
    products: list[Product] = []
    current: Product | None = None
    for n, row in enumerate(rows[h + 1:], start=h + 2):
        cells = [c.strip().lower() for c in row]
        if product_col.lower() in cells and link_col.lower() in cells:       # another header block further down
            current = None
            continue
        name = row[pi].strip() if pi < len(row) else ""
        links = extract_links(row[li] if li < len(row) else "")
        if name:
            current = Product(name=name, row=n)
            products.append(current)
            current.links.extend(links)
        elif links and current is not None:
            current.links.extend(u for u in links if u not in current.links)
    merged: dict[str, Product] = {}                     # a name repeated on two rows: merge into the first
    for p in products:
        key = p.slug
        if key in merged:
            merged[key].links.extend(u for u in p.links if u not in merged[key].links)
        else:
            merged[key] = p
    return list(merged.values())


def select(products: list[Product], only: list[str] | None) -> list[Product]:
    """Products whose name or slug contains any of the `only` terms (case-insensitive); all when `only` is empty."""
    if not only:
        return products
    terms = [t.strip().lower() for t in only if t.strip()]
    return [p for p in products if any(t in p.name.lower() or t in p.slug for t in terms)]
