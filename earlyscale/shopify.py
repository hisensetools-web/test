"""Shopify storefront fetcher (public, unauthenticated) and product normaliser.

Network and parsing are separated so the parsing/normalising half can be tested
against fixture JSON without a network.
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger("earlyscale.shopify")


class StoreFetchError(Exception):
    """A store could not be fetched (network, non-JSON, password-protected, ...)."""


# ---------------------------------------------------------------- HTTP

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    return s


def base_url(store_domain: str) -> str:
    """Accepts 'example.com', 'www.example.com', 'https://example.com/', 'http://localhost:8001'."""
    d = store_domain.strip()
    if not re.match(r"^https?://", d):
        d = "https://" + d
    parsed = urlparse(d)
    return f"{parsed.scheme}://{parsed.netloc}"


def get_json(session: requests.Session, url: str, params: dict | None = None,
             retries: int = config.REQUEST_RETRIES) -> dict:
    """GET a JSON document with timeout and exponential backoff.
    Retries on connection errors, timeouts, 429/430 (Shopify rate limit) and 5xx."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
            if r.status_code in (429, 430) or r.status_code >= 500:
                raise StoreFetchError(f"HTTP {r.status_code}")
            if r.status_code in (401, 403):
                raise StoreFetchError(f"HTTP {r.status_code} (password-protected or blocked)")  # no retry
            if r.status_code == 404:
                raise StoreFetchError("HTTP 404 (not a Shopify storefront?)")  # no retry
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if "json" not in ctype:
                # Password-protected stores 302 to /password and return HTML with 200.
                raise StoreFetchError(f"non-JSON response ({ctype or 'no content-type'}) from {r.url}")  # no retry
            return r.json()
        except StoreFetchError as e:
            msg = str(e)
            retryable = msg.startswith("HTTP 429") or msg.startswith("HTTP 430") or msg.startswith("HTTP 5")
            if not retryable:
                raise
            last_err = e
        except (requests.ConnectionError, requests.Timeout, ValueError) as e:  # ValueError: bad JSON
            last_err = e
        if attempt < retries:
            wait = (2 ** attempt) * 1.5 + random.uniform(0, 1)
            log.warning("retry %d/%d for %s after %s (sleep %.1fs)", attempt + 1, retries, url, last_err, wait)
            time.sleep(wait)
    raise StoreFetchError(f"giving up on {url}: {last_err}")


def _polite_pause() -> None:
    if config.REQUEST_DELAY_S > 0:
        time.sleep(config.REQUEST_DELAY_S * random.uniform(0.7, 1.3))


def fetch_all_pages(session: requests.Session, url: str) -> tuple[list[dict], int]:
    """Paginate `url?limit=250&page=N` until an empty page. Returns (products, pages_fetched)."""
    products: list[dict] = []
    seen_ids: set[int] = set()
    pages = 0
    for page in range(1, config.MAX_PAGES + 1):
        data = get_json(session, url, params={"limit": config.PAGE_LIMIT, "page": page})
        pages += 1
        batch = data.get("products") or []
        if not batch:
            break
        new = [p for p in batch if p.get("id") not in seen_ids]
        if not new:  # some stores ignore `page` and return page 1 forever
            log.warning("page %d of %s repeated earlier ids; stopping pagination", page, url)
            break
        seen_ids.update(p["id"] for p in new)
        products.extend(new)
        if len(batch) < config.PAGE_LIMIT:
            break
        _polite_pause()
    else:
        log.warning("hit MAX_PAGES=%d for %s; catalog truncated", config.MAX_PAGES, url)
    return products, pages


def fetch_store(store_domain: str, session: requests.Session | None = None) -> tuple[list[dict], dict[int, int], int]:
    """Fetch /products.json and /collections/all/products.json for one store.
    Returns (raw_products, {product_id: collection_position}, pages_fetched).
    The collection fetch is best-effort: failure there still yields products."""
    session = session or make_session()
    base = base_url(store_domain)
    raw, pages = fetch_all_pages(session, f"{base}/products.json")
    positions: dict[int, int] = {}
    try:
        _polite_pause()
        coll, coll_pages = fetch_all_pages(session, f"{base}/collections/all/products.json")
        pages += coll_pages
        positions = {p["id"]: i for i, p in enumerate(coll) if "id" in p}
    except (StoreFetchError, requests.RequestException) as e:
        log.warning("%s: collections/all failed (%s); positions left NULL", store_domain, e)
    return raw, positions, pages


# ---------------------------------------------------------------- parsing (pure)

def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tags(v) -> list[str]:
    if isinstance(v, list):
        return [str(t).strip() for t in v if str(t).strip()]
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    return []


def normalise_product(p: dict, collection_position: int | None = None) -> dict:
    """Flatten one products.json record into the columns we store."""
    variants = []
    for v in p.get("variants") or []:
        variants.append({
            "variant_id": int(v["id"]),
            "title": v.get("title"),
            "sku": v.get("sku") or None,
            "price": _to_float(v.get("price")),
            "compare_at_price": _to_float(v.get("compare_at_price")),
            "available": bool(v.get("available", False)),
        })
    prices = [v["price"] for v in variants if v["price"] is not None]
    return {
        "product_id": int(p["id"]),
        "handle": p["handle"],
        "title": p.get("title"),
        "vendor": p.get("vendor"),
        "product_type": p.get("product_type") or None,
        "tags": _tags(p.get("tags")),
        "created_at": p.get("created_at"),
        "published_at": p.get("published_at"),
        "updated_at": p.get("updated_at"),
        "variant_count": len(variants),
        "sold_out_variants": sum(1 for v in variants if not v["available"]),
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
        "collection_position": collection_position,
        "variants": variants,
    }


def normalise_products(raw: list[dict], positions: dict[int, int] | None = None) -> list[dict]:
    positions = positions or {}
    out = []
    for p in raw:
        if "id" not in p or not p.get("handle"):
            log.warning("skipping malformed product record: %r", {k: p.get(k) for k in ("id", "handle", "title")})
            continue
        out.append(normalise_product(p, positions.get(p["id"])))
    return out


# ---------------------------------------------------------------- meta page discovery

_FB_RE = re.compile(r"https?://(?:www\.|m\.|business\.)?facebook\.com/([A-Za-z0-9_.\-]+)/?", re.I)
_FB_SKIP = {"sharer", "sharer.php", "share", "plugins", "dialog", "login", "pages", "profile.php",
            "tr", "policies", "help", "privacy", "groups", "events", "hashtag", "watch", "ads"}


def discover_meta_page(store_domain: str, session: requests.Session | None = None) -> str | None:
    """Best-effort: find a facebook.com/<page> link on the storefront home page."""
    session = session or make_session()
    try:
        r = session.get(base_url(store_domain) + "/", timeout=config.REQUEST_TIMEOUT,
                        headers={"Accept": "text/html"})
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("%s: could not fetch home page for meta discovery: %s", store_domain, e)
        return None
    return extract_meta_page(r.text)


def extract_meta_page(html: str) -> str | None:
    for m in _FB_RE.finditer(html):
        name = m.group(1)
        if name.lower() in _FB_SKIP:
            continue
        return name
    return None
