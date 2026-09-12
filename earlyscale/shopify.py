"""Shopify storefront fetcher (public, unauthenticated: /products.json) and product normaliser.

Network and parsing are separated so the parsing/normalising half can be tested
against fixture JSON without a network.
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urlparse, unquote

import requests

from . import config

log = logging.getLogger("earlyscale.shopify")


class StoreFetchError(Exception):
    """A store could not be fetched (network, non-JSON, password-protected, ...)."""


# ---------------------------------------------------------------- HTTP

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(config.BROWSER_HEADERS)
    return s


def base_url(store_domain: str) -> str:
    """Accepts 'example.com', 'www.example.com', 'https://example.com/', 'http://localhost:8001'."""
    d = store_domain.strip()
    if not re.match(r"^https?://", d):
        d = "https://" + d
    parsed = urlparse(d)
    return f"{parsed.scheme}://{parsed.netloc}"


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _www_variant(base: str) -> str | None:
    """https://example.com -> https://www.example.com (None if it already has www or is an IP/port)."""
    return _prefixed(base, "www.")


def _shop_variant(base: str) -> str | None:
    """https://example.com -> https://shop.example.com: marketing sites (Webflow, Wix) often keep the
    Shopify storefront on a shop. subdomain. None for hosts that already carry a subdomain, or IP/port."""
    return _prefixed(base, "shop.")


def _prefixed(base: str, prefix: str) -> str | None:
    u = urlparse(base)
    host = u.netloc
    if host.startswith(("www.", "shop.")) or ":" in host or host.replace(".", "").isdigit() or host.count(".") < 1:
        return None
    return f"{u.scheme}://{prefix}{host}"


def _clearly_not_storefront(r: requests.Response) -> bool:
    """A probe answered, but not with a products.json document (404, or an HTML page)."""
    if r.status_code >= 400:
        return True
    try:
        return _looks_like_html(r.text or "")
    except Exception:
        return False


def resolve_base_url(session: requests.Session, store_domain: str, quiet: bool = False) -> str:
    """Find the origin that actually serves the storefront.

    Many stores redirect apex -> www (or the reverse), and some apex hosts don't
    answer at all (seen on tryterrastrike.com). We probe once, follow redirects,
    and reuse the final origin for every later request so pagination never
    bounces through a redirect. If the apex host is unreachable, or answers with
    a 404 / HTML page (a marketing site in front of the shop), we try www. and
    then shop. (pipitea.com -> shop.pipitea.com). If no candidate serves JSON the
    first one that answered is returned so the caller reports the real error."""
    base = base_url(store_domain)
    candidates = [base]
    for alt in (_www_variant(base), _shop_variant(base)):
        if alt:
            candidates.append(alt)
    last_err: Exception | None = None
    first_answer: str | None = None
    say = log.debug if quiet else log.info
    warn = log.debug if quiet else log.warning
    for cand in candidates:
        try:
            r = session.get(f"{cand}/products.json", params={"limit": 1}, timeout=config.REQUEST_TIMEOUT,
                            allow_redirects=True, headers={"Accept": config.JSON_ACCEPT})
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            warn("%s unreachable (%s)%s", cand, type(e).__name__,
                 "; trying the next host" if cand is not candidates[-1] else "")
            continue
        final = _origin(r.url)
        if r.history:
            say("%s redirected to %s; using that host", cand, final)
        if not _clearly_not_storefront(r):
            return final
        first_answer = first_answer or final
        if cand is not candidates[-1]:
            say("%s/products.json is not a storefront (HTTP %s); trying the next host", final, r.status_code)
    if first_answer:
        return first_answer
    raise StoreFetchError(f"unreachable: {last_err}")


def _looks_like_html(text: str) -> bool:
    head = text.lstrip("\ufeff \t\r\n")[:512].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or head.startswith("<?xml") \
        or head.startswith("<head") or head.startswith("<body")


def parse_json_response(r: requests.Response) -> dict:
    """Decode a JSON body, raising a clear StoreFetchError (never a JSONDecodeError
    traceback) when the store sent HTML or otherwise non-JSON content."""
    ctype = r.headers.get("Content-Type", "")
    text = r.text
    if _looks_like_html(text) or ("html" in ctype and not text.lstrip().startswith(("{", "["))):
        raise StoreFetchError(
            f"got HTML, not JSON from {r.url} (HTTP {r.status_code}, content-type {ctype or 'none'}) "
            "- password page, bot challenge, or not a Shopify storefront")
    try:
        data = r.json()
    except ValueError:
        snippet = text.strip()[:80].replace("\n", " ")
        raise StoreFetchError(
            f"response from {r.url} is not valid JSON (HTTP {r.status_code}, content-type {ctype or 'none'}): "
            f"{snippet!r}") from None
    if not isinstance(data, dict):
        raise StoreFetchError(f"unexpected JSON shape from {r.url}: {type(data).__name__}")
    return data


def get_json(session: requests.Session, url: str, params: dict | None = None,
             retries: int = config.REQUEST_RETRIES) -> dict:
    """GET a JSON document with timeout and exponential backoff.

    Sends Accept: application/json. If the store answers 406 to that, the request is
    repeated once with Accept: */* (some WAFs reject explicit JSON). Retries with backoff
    on connection errors, timeouts, 429/430 (Shopify rate limit) and 5xx. HTML or
    unparsable bodies fail immediately with a descriptive error."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT, allow_redirects=True,
                            headers={"Accept": config.JSON_ACCEPT})
            if r.status_code == 406:
                log.info("%s answered 406 to Accept: %s; retrying once with %s", url, config.JSON_ACCEPT,
                         config.FALLBACK_ACCEPT)
                r = session.get(url, params=params, timeout=config.REQUEST_TIMEOUT, allow_redirects=True,
                                headers={"Accept": config.FALLBACK_ACCEPT})
            if r.status_code in (429, 430) or r.status_code >= 500:
                raise StoreFetchError(f"HTTP {r.status_code}")
            if r.status_code in (401, 403, 406):
                raise StoreFetchError(f"HTTP {r.status_code} (password-protected or blocked)")  # no retry
            if r.status_code == 404:
                raise StoreFetchError("HTTP 404 (not a Shopify storefront?)")  # no retry
            r.raise_for_status()
            return parse_json_response(r)  # raises non-retryable StoreFetchError on HTML/garbage
        except StoreFetchError as e:
            msg = str(e)
            retryable = msg.startswith("HTTP 429") or msg.startswith("HTTP 430") or msg.startswith("HTTP 5")
            if not retryable:
                raise
            last_err = e
        except (requests.ConnectionError, requests.Timeout) as e:
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


def fetch_store(store_domain: str, session: requests.Session | None = None) -> tuple[list[dict], int]:
    """Fetch /products.json for one store. Returns (raw_products, pages_fetched)."""
    session = session or make_session()
    base = resolve_base_url(session, store_domain)
    raw, pages = fetch_all_pages(session, f"{base}/products.json")
    return raw, pages + 1  # + the resolve probe


# ---------------------------------------------------------------- parsing (pure)

def _tags(v) -> list[str]:
    if isinstance(v, list):
        return [str(t).strip() for t in v if str(t).strip()]
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    return []


def normalise_product(p: dict) -> dict:
    """The columns kept per product: id, handle, title, the dates (+ vendor / type / tags, informational)."""
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
        "url_path": f"/products/{p['handle']}",
        "variant_count": len(p.get("variants") or []),
    }


def normalise_products(raw: list[dict]) -> list[dict]:
    out = []
    for p in raw:
        if "id" not in p or not p.get("handle"):
            log.warning("skipping malformed product record: %r", {k: p.get(k) for k in ("id", "handle", "title")})
            continue
        out.append(normalise_product(p))
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
                        headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("%s: could not fetch home page for meta discovery: %s", store_domain, e)
        return None
    return extract_meta_page(r.text)


_FB_PRETTY_RE = re.compile(r"https?://(?:www\.|m\.)?facebook\.com/(?:p|people)/([A-Za-z0-9_.\-%]+?)(?:-\d{5,}|/\d{5,})/?", re.I)


def extract_meta_page(html: str) -> str | None:
    """The page's name (or vanity slug) from the first facebook.com link on the page. Handles the newer
    facebook.com/p/<Page-Name>-<id>/ and /people/<Name>/<id>/ links (the name, hyphens -> spaces) and skips
    share / login / profile.php links and one- or two-character slugs."""
    for m in _FB_RE.finditer(html):
        name = m.group(1)
        if name.lower() in _FB_SKIP:
            continue
        if name.lower() in ("p", "people"):
            pm = _FB_PRETTY_RE.match(html, m.start())
            if pm:
                return unquote(pm.group(1)).replace("-", " ").strip() or None
            continue
        if len(name) < 3:
            continue
        return name
    return None
