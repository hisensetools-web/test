"""Catalogue adapters for every D2C storefront, not only Shopify.

Every adapter returns the same normalised product dicts that shopify.normalise_product produces
(product_id, handle, title, created_at, published_at, updated_at, url_path), so the snapshot and Radar
code do not care what the store runs on. The adapters read variants only to tell a product page that
was read from a placeholder (variant_count); nothing about variants is stored.

  detect(session, domain)         one homepage GET (+ the Shopify probe): {platform, base, myshopify, evidence}
  fetch_catalogue(domain, ...)    detect (cached per store), then the adapter:
    shopify                       /products.json                                           shopify.fetch_store
    shopify_headless              <shop>.myshopify.com/products.json behind a custom front (Hydrogen, Next.js)
    woocommerce                   /wp-json/wc/store/v1/products (public Store API), popularity order,
                                  dates from /wp-json/wp/v2/product when the site exposes it
    squarespace                   /shop?format=json: addedOn, variants with price and qtyInStock
    magento                       /graphql products query: created_at, stock_status, only_x_left_in_stock
    bigcommerce, wix, generic     sitemap product URLs (+lastmod) and each product page's JSON-LD / OpenGraph,
                                  a budget of CATALOGUE_MAX_PAGES pages per store per day, cached in product_pages;
                                  BigCommerce adds pubDate from its new-products RSS

Dates a platform does not give are filled from product_first_seen (the day this tracker first saw the
URL). Products already present on the store's first snapshot get created_at NULL (age unknown) so a new
store does not look like it launched its whole catalogue today.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests

from . import config, shopify

log = logging.getLogger("earlyscale.platforms")

PLATFORMS = ("shopify", "shopify_headless", "woocommerce", "squarespace", "magento", "bigcommerce", "wix", "generic")
CATALOGUE_PLATFORMS = PLATFORMS   # every one of them yields a catalogue (generic may be partial at first)

PRODUCT_PATH_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:products?|shop/p|p|item|items|store/p|producto|produkt|artikel)/([^/?#]+)/?$", re.I)
COLLECTION_HINT_RE = re.compile(r"/(collections?|category|categories|product-category|tag|tags|page|blog|news|pages|cart|checkout|account|search)(/|$)", re.I)
LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
META_RE = re.compile(r'<meta\s+(?:property|name)=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']', re.I)
META_RE2 = re.compile(r'<meta\s+content=["\']([^"\']*)["\']\s+(?:property|name)=["\']([^"\']+)["\']', re.I)
MYSHOPIFY_RE = re.compile(r"([a-z0-9][a-z0-9\-]*)\.myshopify\.com", re.I)


class PlatformError(Exception):
    pass


@dataclass
class Catalogue:
    platform: str
    base: str
    products: list[dict]
    pages: int = 0
    note: str = ""
    evidence: str = ""
    partial: bool = False          # generic: not every product page has been read yet
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- helpers (pure)

def stable_id(*parts: str) -> int:
    """A 48-bit integer id from a URL path (or path + sku): stable across days, fits SQLite INTEGER."""
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16)


def slug_of(path: str) -> str:
    segs = [s for s in path.split("?")[0].split("#")[0].split("/") if s]
    if not segs:
        return ""
    s = segs[-1].lower()
    for ext in (".html", ".htm", ".php"):
        if s.endswith(ext):
            s = s[: -len(ext)]
    return s


def norm_path(url_or_path: str) -> str:
    p = urlparse(url_or_path).path if "://" in url_or_path else url_or_path
    p = p.split("?")[0].split("#")[0]
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def _iso(ts) -> str | None:
    """Epoch ms/s, ISO strings and 'YYYY-MM-DD HH:MM:SS' -> ISO 8601 (UTC) or None."""
    if ts in (None, "", 0):
        return None
    try:
        if isinstance(ts, (int, float)):
            v = float(ts)
            if v > 1e11:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc).replace(microsecond=0).isoformat()
        s = str(ts).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return s + "T00:00:00+00:00"
        s = s.replace(" ", "T", 1) if re.match(r"^\d{4}-\d{2}-\d{2} \d", s) else s
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.replace(microsecond=0).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    try:  # RFC 2822 (RSS pubDate)
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(str(ts)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def make_product(path: str, title: str | None, variants: list[dict], *, created_at=None, published_at=None, updated_at=None,
                 vendor: str | None = None, product_type: str | None = None, tags: list[str] | None = None,
                 position: int | None = None, product_id: int | None = None) -> dict:
    """The normalised product every adapter returns (same keys as shopify.normalise_product + url_path)."""
    path = norm_path(path)
    vs = []
    for i, v in enumerate(variants or []):
        price = _num(v.get("price"))
        vs.append({"variant_id": v.get("variant_id") or stable_id(path, str(v.get("sku") or v.get("title") or i)),
                   "title": v.get("title"), "sku": v.get("sku") or None, "price": price,
                   "compare_at_price": _num(v.get("compare_at_price")), "available": bool(v.get("available", True)),
                   "inventory_management": v.get("inventory_management"), "inventory_policy": v.get("inventory_policy"),
                   "stock": v.get("stock")})
    prices = [v["price"] for v in vs if v["price"] is not None]
    return {"product_id": product_id or stable_id(path), "handle": slug_of(path), "url_path": path, "title": title,
            "vendor": vendor, "product_type": product_type or None, "tags": list(tags or []),
            "created_at": _iso(created_at), "published_at": _iso(published_at), "updated_at": _iso(updated_at),
            "variant_count": len(vs), "sold_out_variants": sum(1 for v in vs if not v["available"]),
            "min_price": min(prices) if prices else None, "max_price": max(prices) if prices else None,
            "collection_position": position, "variants": vs}


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _get(session: requests.Session, url: str, accept: str = "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8", **kw):
    return session.get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True, headers={"Accept": accept}, **kw)


# ---------------------------------------------------------------- detection

MARKERS = [
    ("woocommerce", re.compile(r"wp-content/plugins/woocommerce|class=\"[^\"]*woocommerce|wc-block|woocommerce-", re.I)),
    ("squarespace", re.compile(r"static1?\.squarespace\.com|Static\.SQUARESPACE_CONTEXT|squarespace-cdn", re.I)),
    ("bigcommerce", re.compile(r"cdn\d*\.bigcommerce\.com|bigcommerce\.com/|stencil-utils|data-stencil", re.I)),
    ("magento", re.compile(r"Magento_|/static/version\d+/|mage/requirejs|magento", re.I)),
    ("wix", re.compile(r"static\.wixstatic\.com|wix\.com/|X-Wix-|wixsite", re.I)),
]


def detect(session: requests.Session, domain: str, quiet: bool = False) -> dict:
    """{platform, base, myshopify, evidence}. Tries the Shopify JSON first (cheapest), then one homepage GET."""
    base = shopify.base_url(domain)
    # 1. a Shopify storefront answers /products.json with products (resolve_base_url tries www. and shop.)
    try:
        resolved = shopify.resolve_base_url(session, domain, quiet=quiet)
        r = _get(session, f"{resolved}/products.json?limit=1", accept=config.JSON_ACCEPT)
        if r.status_code == 406:   # a WAF that rejects an explicit JSON Accept (olavita.co): same retry as shopify.fetch
            r = _get(session, f"{resolved}/products.json?limit=1", accept=config.FALLBACK_ACCEPT)
        if r.status_code == 200 and r.text.lstrip().startswith("{") and "products" in r.text[:200]:
            return {"platform": "shopify", "base": resolved, "myshopify": None, "evidence": "products.json"}
        base = resolved
    except (shopify.StoreFetchError, requests.RequestException):
        pass
    # 2. the homepage
    try:
        r = _get(session, base + "/")
        html = r.text if r.status_code < 500 else ""
        final = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}" if r.url else base
        headers = {k.lower(): v for k, v in r.headers.items()}
    except requests.RequestException as e:
        raise PlatformError(f"unreachable: {e}") from e
    if not html:
        raise PlatformError(f"homepage answered HTTP {r.status_code}")
    if "x-shopify-stage" in headers or "shopify" in headers.get("powered-by", "").lower():
        return {"platform": "shopify", "base": final, "myshopify": None, "evidence": "x-shopify header"}
    m = MYSHOPIFY_RE.search(html)
    if m or "cdn.shopify.com" in html or "shopify-checkout" in html.lower() or "window.Shopify" in html:
        shop = f"{m.group(1)}.myshopify.com" if m else None
        return {"platform": "shopify_headless", "base": final, "myshopify": shop,
                "evidence": f"shopify assets, shop={shop or '?'}"}
    if "x-wix-request-id" in headers:
        return {"platform": "wix", "base": final, "myshopify": None, "evidence": "x-wix-request-id"}
    for name, rx in MARKERS:
        if rx.search(html):
            return {"platform": name, "base": final, "myshopify": None, "evidence": f"html marker {name}"}
    return {"platform": "generic", "base": final, "myshopify": None, "evidence": "no known platform markers"}


# ---------------------------------------------------------------- JSON-LD / OpenGraph (pure)

def _ld_blocks(html: str):
    for m in LD_RE.finditer(html or ""):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except ValueError:
                continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                yield x
                for k in ("@graph", "mainEntity", "itemListElement", "hasVariant", "offers"):
                    if k in x and isinstance(x[k], (list, dict)):
                        stack.append(x[k])


def _is_type(obj: dict, name: str) -> bool:
    t = obj.get("@type")
    if isinstance(t, list):
        return any(str(x).lower() == name.lower() for x in t)
    return str(t or "").lower() == name.lower()


def _avail(v) -> bool | None:
    if v is None:
        return None
    s = str(v).lower()
    if "instock" in s or "in_stock" in s or "limitedavailability" in s or "onlineonly" in s or "preorder" in s or "backorder" in s:
        return True
    if "outofstock" in s or "out_of_stock" in s or "soldout" in s or "discontinued" in s:
        return False
    return None


def meta_tags(html: str) -> dict[str, str]:
    out = {}
    for m in META_RE.finditer(html or ""):
        out.setdefault(m.group(1).lower(), unescape(m.group(2)))
    for m in META_RE2.finditer(html or ""):
        out.setdefault(m.group(2).lower(), unescape(m.group(1)))
    return out


def parse_product_page(html: str, path: str) -> dict | None:
    """Product facts from JSON-LD (schema.org Product) or OpenGraph product tags. None when the page is not a product."""
    products = [o for o in _ld_blocks(html) if _is_type(o, "Product") or _is_type(o, "ProductGroup")]
    tags = meta_tags(html)
    if not products and tags.get("og:type", "").lower() not in ("product", "og:product", "product.item"):
        if not any(k.startswith("product:price") for k in tags):
            return None
    variants: list[dict] = []
    title = brand = sku = created = None
    for o in products:
        title = title or o.get("name")
        b = o.get("brand")
        brand = brand or (b.get("name") if isinstance(b, dict) else b if isinstance(b, str) else None)
        sku = sku or o.get("sku")
        created = created or o.get("releaseDate") or o.get("datePublished") or o.get("dateCreated")
        offers = o.get("offers")
        if isinstance(offers, dict):
            offers = [offers]
        for off in offers or []:
            if not isinstance(off, dict):
                continue
            if _is_type(off, "AggregateOffer"):
                lo = off.get("lowPrice") or off.get("price")
                variants.append({"title": "Default", "sku": sku, "price": lo, "available": _avail(off.get("availability")) is not False})
                continue
            item = off.get("itemOffered") if isinstance(off.get("itemOffered"), dict) else {}
            stock = off.get("inventoryLevel")
            if isinstance(stock, dict):
                stock = stock.get("value")
            variants.append({"title": item.get("name") or off.get("name") or "Default", "sku": off.get("sku") or item.get("sku") or sku,
                             "price": off.get("price") or (off.get("priceSpecification") or {}).get("price") if isinstance(off.get("priceSpecification"), dict) else off.get("price"),
                             "available": _avail(off.get("availability")) is not False, "stock": _num(stock)})
        for hv in o.get("hasVariant") or []:
            if isinstance(hv, dict):
                offs = hv.get("offers")
                offs = [offs] if isinstance(offs, dict) else (offs or [])
                for off in offs:
                    if isinstance(off, dict):
                        variants.append({"title": hv.get("name"), "sku": hv.get("sku") or off.get("sku"), "price": off.get("price"),
                                         "available": _avail(off.get("availability")) is not False})
    if not variants:
        price = tags.get("product:price:amount") or tags.get("og:price:amount")
        if price or title or tags.get("og:title"):
            variants.append({"title": "Default", "sku": sku, "price": price,
                             "available": _avail(tags.get("product:availability") or tags.get("og:availability")) is not False})
    title = title or tags.get("og:title") or tags.get("twitter:title")
    if not title and not variants:
        return None
    # dedupe variants on (sku, price, title)
    seen, uniq = set(), []
    for v in variants:
        k = (v.get("sku"), _num(v.get("price")), v.get("title"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(v)
    return {"path": norm_path(path), "title": unescape(str(title)) if title else slug_of(path).replace("-", " ").title(),
            "brand": brand, "variants": uniq, "created_at": created, "published_at": tags.get("article:published_time"),
            "updated_at": tags.get("article:modified_time") or tags.get("og:updated_time")}


# ---------------------------------------------------------------- sitemaps (pure parse + fetch)

def parse_sitemap(xml_text: str) -> tuple[list[str], list[tuple[str, str | None]]]:
    """(child sitemap urls, [(url, lastmod)]) from a sitemap or sitemap index."""
    try:
        root = ET.fromstring(xml_text.strip().encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        # some sites serve sitemaps with a BOM or leading whitespace/junk; fall back to a regex scan
        subs = re.findall(r"<sitemap>\s*<loc>\s*([^<\s]+)\s*</loc>", xml_text or "", re.I)
        urls = re.findall(r"<url>\s*<loc>\s*([^<\s]+)\s*</loc>(?:\s*<lastmod>\s*([^<\s]+)\s*</lastmod>)?", xml_text or "", re.I | re.S)
        return subs, [(u, lm or None) for u, lm in urls]
    ns = {"s": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
    p = (lambda t: f"s:{t}") if ns else (lambda t: t)
    subs = [el.text.strip() for el in root.findall(f".//{p('sitemap')}/{p('loc')}", ns) if el.text]
    urls = []
    for u in root.findall(f".//{p('url')}", ns):
        loc = u.find(p("loc"), ns)
        lm = u.find(p("lastmod"), ns)
        if loc is not None and loc.text:
            urls.append((loc.text.strip(), lm.text.strip() if lm is not None and lm.text else None))
    return subs, urls


def is_product_url(url: str, sitemap_name: str = "") -> bool:
    path = norm_path(url)
    if COLLECTION_HINT_RE.search(path):
        return False
    if PRODUCT_PATH_RE.match(path):
        return True
    return "product" in sitemap_name.lower() and path.count("/") >= 1 and path != "/"


def discover_product_urls(session: requests.Session, base: str, max_sitemaps: int | None = None, max_urls: int | None = None) -> tuple[list[tuple[str, str | None]], int]:
    """Product page URLs (+lastmod) from robots.txt sitemaps / the usual sitemap paths. Returns (urls, fetches)."""
    max_sitemaps = max_sitemaps or config.CATALOGUE_MAX_SITEMAPS
    max_urls = max_urls or config.CATALOGUE_MAX_URLS
    starts: list[str] = []
    fetches = 0
    try:
        r = _get(session, base + "/robots.txt", accept="text/plain,*/*")
        fetches += 1
        if r.status_code == 200:
            starts += [ln.split(":", 1)[1].strip() for ln in r.text.splitlines() if ln.lower().startswith("sitemap:")]
    except requests.RequestException:
        pass
    for cand in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/product-sitemap.xml", "/xmlsitemap.php", "/sitemap.php"):
        if base + cand not in starts:
            starts.append(base + cand)
    seen_maps: set[str] = set()
    out: dict[str, str | None] = {}
    queue = list(starts)
    while queue and len(seen_maps) < max_sitemaps and len(out) < max_urls:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            r = _get(session, sm, accept="application/xml,text/xml,*/*")
            fetches += 1
        except requests.RequestException:
            continue
        if r.status_code != 200 or "<" not in r.text[:200]:
            continue
        subs, urls = parse_sitemap(r.text)
        # product sitemaps first, the rest after (only if we still need more)
        subs.sort(key=lambda u: 0 if "product" in u.lower() else 1)
        queue = subs + queue if "product" in sm.lower() or not out else queue + subs
        for u, lm in urls:
            if is_product_url(u, sm):
                out.setdefault(u, lm)
        if out and all("product" not in s.lower() for s in queue):
            # a product sitemap was found (or the flat sitemap had products): the remaining generic ones rarely add any
            if any("product" in s.lower() for s in seen_maps):
                break
    return list(out.items())[:max_urls], fetches


# ---------------------------------------------------------------- adapters

def catalogue_shopify(session: requests.Session, domain: str, base: str | None = None) -> Catalogue:
    raw, pages = shopify.fetch_store(base or domain, session)
    return Catalogue("shopify", base or shopify.base_url(domain), shopify.normalise_products(raw), pages)


def catalogue_shopify_headless(session: requests.Session, domain: str, info: dict) -> Catalogue:
    """The myshopify.com origin usually still serves products.json for a headless front end."""
    shop = info.get("myshopify")
    if not shop:
        raise PlatformError("headless Shopify front without a visible myshopify.com shop name")
    raw, pages = shopify.fetch_store(f"https://{shop}", session)
    products = shopify.normalise_products(raw)
    c = Catalogue("shopify_headless", info["base"], products, pages, note=f"catalogue via {shop}")
    c.extra["myshopify"] = shop
    return c


def catalogue_woocommerce(session: requests.Session, base: str) -> Catalogue:
    """WooCommerce Blocks Store API (public): products in popularity order; dates from wp/v2 when exposed."""
    items: list[dict] = []
    pages = 0
    page = 1
    while page <= config.MAX_PAGES:
        r = _get(session, f"{base}/wp-json/wc/store/v1/products", accept="application/json",
                 params={"per_page": 100, "page": page, "orderby": "popularity"})
        pages += 1
        if r.status_code != 200 or not r.text.lstrip().startswith("["):
            if page == 1:
                raise PlatformError(f"Store API not available (HTTP {r.status_code})")
            break
        batch = r.json()
        if not batch:
            break
        items.extend(batch)
        total = r.headers.get("X-WP-TotalPages")
        if total and page >= int(total):
            break
        if len(batch) < 100:
            break
        page += 1
        shopify._polite_pause()
    dates: dict[str, dict] = {}
    try:  # optional: the WordPress REST route for the product post type carries date / modified
        for pg in range(1, 6):
            r = _get(session, f"{base}/wp-json/wp/v2/product", accept="application/json",
                     params={"per_page": 100, "page": pg, "_fields": "id,slug,date_gmt,modified_gmt"})
            pages += 1
            if r.status_code != 200 or not r.text.lstrip().startswith("["):
                break
            for x in r.json():
                dates[str(x.get("id"))] = x
            if len(r.json()) < 100:
                break
    except (requests.RequestException, ValueError):
        pass
    products = []
    for i, it in enumerate(items):
        path = norm_path(it.get("permalink") or f"/product/{it.get('slug')}")
        pr = it.get("prices") or {}
        unit = 10 ** int(pr.get("currency_minor_unit") or 2)
        price = _num(pr.get("price"))
        regular = _num(pr.get("regular_price"))
        price = price / unit if price is not None else None
        regular = regular / unit if regular is not None else None
        stock = it.get("low_stock_remaining")
        base_v = {"price": price, "compare_at_price": regular if regular and price and regular > price else None,
                  "available": bool(it.get("is_in_stock", True)), "stock": _num(stock),
                  "inventory_management": "shopify" if stock is not None else None}
        variations = it.get("variations") or []
        if variations:
            vs = [dict(base_v, variant_id=int(v["id"]), title=", ".join(a.get("value", "") for a in (v.get("attributes") or [])) or "variation",
                       sku=None) for v in variations if isinstance(v, dict) and v.get("id")]
        else:
            vs = [dict(base_v, variant_id=int(it["id"]), title=it.get("name"), sku=it.get("sku") or None)]
        d = dates.get(str(it.get("id")), {})
        products.append(make_product(path, unescape(str(it.get("name") or "")), vs, product_id=int(it["id"]),
                                     created_at=(d.get("date_gmt") + "+00:00") if d.get("date_gmt") else None,
                                     published_at=(d.get("date_gmt") + "+00:00") if d.get("date_gmt") else None,
                                     updated_at=(d.get("modified_gmt") + "+00:00") if d.get("modified_gmt") else None,
                                     product_type=", ".join(c.get("name", "") for c in (it.get("categories") or [])[:3]) or None,
                                     tags=[t.get("name", "") for t in (it.get("tags") or [])], position=i))
    return Catalogue("woocommerce", base, products, pages, note="Store API, popularity order" + (", wp/v2 dates" if dates else ""))


def _squarespace_items(session: requests.Session, url: str, pages_budget: int = 30) -> tuple[list[dict], int]:
    items: list[dict] = []
    fetches = 0
    offset = None
    while fetches < pages_budget:
        params = {"format": "json"}
        if offset:
            params["offset"] = offset
        r = _get(session, url, accept="application/json", params=params)
        fetches += 1
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            break
        data = r.json()
        batch = data.get("items") or []
        items.extend(batch)
        pag = data.get("pagination") or {}
        if not (pag.get("nextPage") and pag.get("nextPageOffset")):
            break
        offset = pag["nextPageOffset"]
        shopify._polite_pause()
    return items, fetches


def catalogue_squarespace(session: requests.Session, base: str) -> Catalogue:
    """Squarespace: any commerce collection answers ?format=json with items, addedOn and variants[].stock.qtyInStock."""
    pages = 0
    items: list[dict] = []
    tried = []
    for path in ("/shop", "/store", "/products", "/shop-all", "/all-products", "/collections"):
        tried.append(path)
        got, n = _squarespace_items(session, base + path)
        pages += n
        if got and any((x.get("variants") or x.get("structuredContent", {}).get("variants")) for x in got):
            items = got
            break
    if not items:
        # find a commerce collection through the sitemap (product pages look like /<collection>/p/<slug>)
        urls, n = discover_product_urls(session, base, max_sitemaps=5, max_urls=500)
        pages += n
        cols = {}
        for u, _ in urls:
            p = norm_path(u)
            if "/p/" in p:
                cols[p.split("/p/")[0]] = cols.get(p.split("/p/")[0], 0) + 1
        for col in sorted(cols, key=lambda c: -cols[c])[:3]:
            got, n = _squarespace_items(session, base + col)
            pages += n
            if got:
                items = got
                break
    if not items:
        raise PlatformError(f"no commerce collection answered ?format=json (tried {', '.join(tried)})")
    products = []
    for i, it in enumerate(items):
        sc = it.get("structuredContent") or {}
        vs = []
        for v in it.get("variants") or sc.get("variants") or []:
            pm = v.get("priceMoney") or {}
            sm = v.get("salePriceMoney") or {}
            price = _num(pm.get("value")) if pm else (_num(v.get("price")) / 100 if v.get("price") is not None else None)
            sale = _num(sm.get("value")) if sm else (_num(v.get("salePrice")) / 100 if v.get("salePrice") else None)
            on_sale = bool(v.get("onSale")) and sale
            st = v.get("stock") or {}
            qty = st.get("qtyInStock")
            unlimited = bool(st.get("unlimited"))
            attrs = v.get("attributes") or {}
            vs.append({"variant_id": stable_id(str(it.get("id")), str(v.get("id") or v.get("sku") or len(vs))),
                       "title": ", ".join(str(x) for x in attrs.values()) or "Default", "sku": v.get("sku"),
                       "price": sale if on_sale else price, "compare_at_price": price if on_sale else None,
                       "available": unlimited or (qty is None) or qty > 0,
                       "stock": None if unlimited else _num(qty), "inventory_management": None if unlimited else "shopify"})
        path = norm_path(it.get("fullUrl") or f"/shop/p/{it.get('urlId')}")
        products.append(make_product(path, unescape(str(it.get("title") or "")), vs,
                                     created_at=it.get("addedOn"), published_at=it.get("publishOn") or it.get("addedOn"),
                                     updated_at=it.get("updatedOn"), tags=list(it.get("tags") or []),
                                     product_type=", ".join(it.get("categories") or [])[:80] or None, position=i))
    return Catalogue("squarespace", base, products, pages, note="?format=json with stock")


MAGENTO_QUERY = """query ($page: Int!) { products(filter: { price: { from: "0" } }, pageSize: 100, currentPage: $page,
  sort: { position: ASC }) { total_count items { sku name url_key url_suffix created_at updated_at stock_status only_x_left_in_stock
  price_range { minimum_price { final_price { value } regular_price { value } } maximum_price { final_price { value } } }
  ... on ConfigurableProduct { variants { product { sku name stock_status only_x_left_in_stock
    price_range { minimum_price { final_price { value } } } } } } } } }"""


def catalogue_magento(session: requests.Session, base: str) -> Catalogue:
    items: list[dict] = []
    pages = 0
    page = 1
    while page <= 20:
        r = session.post(f"{base}/graphql", json={"query": MAGENTO_QUERY, "variables": {"page": page}}, timeout=config.REQUEST_TIMEOUT,
                         headers={"Accept": "application/json", "Content-Type": "application/json"})
        pages += 1
        if r.status_code != 200 or not r.text.lstrip().startswith("{"):
            raise PlatformError(f"GraphQL not available (HTTP {r.status_code})")
        data = r.json()
        prod = ((data.get("data") or {}).get("products") or {})
        batch = prod.get("items") or []
        if not batch:
            if page == 1 and data.get("errors"):
                raise PlatformError(f"GraphQL error: {data['errors'][0].get('message', '')[:120]}")
            break
        items.extend(batch)
        if len(items) >= int(prod.get("total_count") or 0) or len(batch) < 100:
            break
        page += 1
        shopify._polite_pause()
    products = []
    for i, it in enumerate(items):
        suffix = it.get("url_suffix") or ".html"
        path = f"/{it.get('url_key')}{suffix}"
        mn = ((it.get("price_range") or {}).get("minimum_price") or {})
        price = _num((mn.get("final_price") or {}).get("value"))
        regular = _num((mn.get("regular_price") or {}).get("value"))
        vs = []
        for v in it.get("variants") or []:
            pv = v.get("product") or {}
            pmn = ((pv.get("price_range") or {}).get("minimum_price") or {})
            vs.append({"title": pv.get("name"), "sku": pv.get("sku"), "price": _num((pmn.get("final_price") or {}).get("value")) or price,
                       "available": (pv.get("stock_status") or "IN_STOCK") == "IN_STOCK", "stock": _num(pv.get("only_x_left_in_stock")),
                       "inventory_management": "shopify" if pv.get("only_x_left_in_stock") is not None else None})
        if not vs:
            vs = [{"title": "Default", "sku": it.get("sku"), "price": price, "compare_at_price": regular if regular and price and regular > price else None,
                   "available": (it.get("stock_status") or "IN_STOCK") == "IN_STOCK", "stock": _num(it.get("only_x_left_in_stock")),
                   "inventory_management": "shopify" if it.get("only_x_left_in_stock") is not None else None}]
        products.append(make_product(path, it.get("name"), vs, created_at=it.get("created_at"), published_at=it.get("created_at"),
                                     updated_at=it.get("updated_at"), position=i))
    return Catalogue("magento", base, products, pages, note="GraphQL")


def bigcommerce_new_products(session: requests.Session, base: str) -> dict[str, str]:
    """{url_path: pubDate iso} from BigCommerce's new-products RSS (the newest ~50 products with a real date)."""
    try:
        r = _get(session, f"{base}/rss.php", accept="application/rss+xml,application/xml,*/*", params={"type": "rss", "action": "newproducts"})
    except requests.RequestException:
        return {}
    if r.status_code != 200 or "<item" not in r.text:
        return {}
    out = {}
    for m in re.finditer(r"<item>(.*?)</item>", r.text, re.S | re.I):
        block = m.group(1)
        link = re.search(r"<link>\s*([^<\s]+)\s*</link>", block, re.I)
        pub = re.search(r"<pubDate>\s*([^<]+?)\s*</pubDate>", block, re.I)
        if link:
            out[norm_path(link.group(1))] = _iso(pub.group(1)) if pub else None
    return out


def catalogue_generic(session: requests.Session, base: str, platform: str, conn: sqlite3.Connection | None, store_id: int | None,
                      today: str, budget: int | None = None) -> Catalogue:
    """Sitemap product URLs + each product page's structured data, CATALOGUE_MAX_PAGES pages per run, cached in product_pages."""
    budget = config.CATALOGUE_MAX_PAGES if budget is None else budget
    urls, fetches = discover_product_urls(session, base)
    if not urls:
        raise PlatformError("no product URLs in the site's sitemaps")
    cache: dict[str, dict] = {}
    if conn is not None and store_id is not None:
        for r in conn.execute("SELECT * FROM product_pages WHERE store_id = ?", (store_id,)):
            cache[r["url_path"]] = dict(r)
    rss_dates = bigcommerce_new_products(session, base) if platform == "bigcommerce" else {}
    fetches += 1 if platform == "bigcommerce" else 0
    stale_before = (date.fromisoformat(today) - __import__("datetime").timedelta(days=config.CATALOGUE_REFRESH_DAYS)).isoformat()
    order = sorted(urls, key=lambda x: (0 if norm_path(x[0]) not in cache else (1 if (cache[norm_path(x[0])].get("last_fetched") or "") < stale_before else 2)))
    fetched = 0
    for u, lastmod in order:
        path = norm_path(u)
        row = cache.get(path)
        if row and (row.get("last_fetched") or "") >= stale_before:
            continue
        if fetched >= budget:
            break
        fetched += 1
        rec = {"url_path": path, "last_fetched": today, "status": None, "json": None, "lastmod": lastmod}
        try:
            r = _get(session, u)
            rec["status"] = r.status_code
            facts = parse_product_page(r.text, path) if r.status_code == 200 else None
            rec["json"] = json.dumps(facts) if facts else None
        except requests.RequestException as e:
            rec["status"] = -1
            rec["json"] = None
            log.debug("%s: %s", u, e)
        cache[path] = rec
        if conn is not None and store_id is not None:
            conn.execute("""INSERT INTO product_pages (store_id, url_path, first_seen, last_fetched, status, lastmod, json)
                            VALUES (?,?,?,?,?,?,?) ON CONFLICT(store_id, url_path) DO UPDATE SET last_fetched = excluded.last_fetched,
                            status = excluded.status, lastmod = COALESCE(excluded.lastmod, lastmod), json = COALESCE(excluded.json, json)""",
                         (store_id, path, today, today, rec["status"], lastmod, rec["json"]))
        shopify._polite_pause()
    if conn is not None:
        conn.commit()
    products = []
    known_paths = set()
    for i, (u, lastmod) in enumerate(urls):
        path = norm_path(u)
        known_paths.add(path)
        row = cache.get(path) or {}
        facts = json.loads(row["json"]) if row.get("json") else None
        if row.get("status") and row["status"] not in (200,) and not facts:
            continue                       # a dead or non-product URL
        if facts:
            products.append(make_product(path, facts.get("title"), facts.get("variants") or [], vendor=facts.get("brand"),
                                         created_at=facts.get("created_at") or rss_dates.get(path),
                                         published_at=facts.get("published_at") or rss_dates.get(path) or facts.get("created_at"),
                                         updated_at=facts.get("updated_at") or lastmod, position=None))
        else:
            # not read yet (budget): a placeholder row so ads can attach; facts arrive on a later run
            products.append(make_product(path, slug_of(path).replace("-", " ").title(), [], updated_at=lastmod,
                                         created_at=rss_dates.get(path), published_at=rss_dates.get(path)))
    unread = sum(1 for p in products if p["variant_count"] == 0)
    return Catalogue(platform, base, products, fetches + fetched, partial=unread > 0,
                     note=f"sitemap {len(urls)} product urls, {fetched} pages read this run" + (f", {unread} still unread" if unread else ""))


# ---------------------------------------------------------------- dispatch + dates

def fetch_catalogue(domain: str, session: requests.Session | None = None, conn: sqlite3.Connection | None = None,
                    store_id: int | None = None, today: str | None = None, platform_hint: dict | None = None,
                    page_budget: int | None = None, quiet: bool = False) -> Catalogue:
    """Detect the platform (cached on stores.platform for CATALOGUE_REDETECT_DAYS) and fetch the catalogue with its adapter.
    A platform whose adapter fails falls back to the generic sitemap adapter, and finally to re-detection."""
    session = session or shopify.make_session()
    today = today or date.today().isoformat()
    info = platform_hint or (_cached_platform(conn, store_id, today) if conn is not None and store_id is not None else None)
    if info is None:
        info = detect(session, domain, quiet=quiet)
    try:
        cat = _run_adapter(session, domain, info, conn, store_id, today, page_budget)
    except (PlatformError, shopify.StoreFetchError, requests.RequestException, ValueError) as e:
        if info["platform"] in ("generic", "bigcommerce", "wix"):
            raise
        (log.debug if quiet else log.warning)("%s: %s adapter failed (%s); trying the generic sitemap adapter", domain, info["platform"], e)
        cat = catalogue_generic(session, domain if not info.get("base") else info["base"], "generic", conn, store_id, today, page_budget)
        cat.note = f"{info['platform']} adapter failed: {str(e)[:80]}; " + cat.note
        cat.platform = info["platform"]           # keep what the site is, even if the catalogue came the generic way
    cat.evidence = info.get("evidence", "")
    if conn is not None and store_id is not None:
        conn.execute("UPDATE stores SET platform = ?, platform_base = ?, platform_checked_at = ?, platform_note = ? WHERE id = ?",
                     (cat.platform, cat.base, today, (cat.note or "")[:200], store_id))
        fill_dates(conn, store_id, cat.products, today)
        conn.commit()
    return cat


def _run_adapter(session, domain, info, conn, store_id, today, page_budget=None) -> Catalogue:
    p = info["platform"]
    if p == "shopify":
        return catalogue_shopify(session, domain, info.get("base"))
    if p == "shopify_headless":
        return catalogue_shopify_headless(session, domain, info)
    if p == "woocommerce":
        return catalogue_woocommerce(session, info["base"])
    if p == "squarespace":
        return catalogue_squarespace(session, info["base"])
    if p == "magento":
        return catalogue_magento(session, info["base"])
    return catalogue_generic(session, info["base"], p, conn, store_id, today, page_budget)


def _cached_platform(conn, store_id, today) -> dict | None:
    row = conn.execute("SELECT platform, platform_base, platform_checked_at FROM stores WHERE id = ?", (store_id,)).fetchone()
    if not row or not row["platform"] or not row["platform_checked_at"]:
        return None
    age = (date.fromisoformat(today) - date.fromisoformat(row["platform_checked_at"][:10])).days
    if age > config.CATALOGUE_REDETECT_DAYS:
        return None
    return {"platform": row["platform"], "base": row["platform_base"] or shopify.base_url(""), "myshopify": None, "evidence": "cached"}


def fill_dates(conn: sqlite3.Connection, store_id: int, products: list[dict], today: str) -> None:
    """created_at / published_at for products the platform did not date: the day this tracker first saw the URL.
    Products already there on the store's first snapshot get NULL (unknown age), not 'today'."""
    first_snapshot = conn.execute("SELECT MIN(snapshot_date) FROM products_daily WHERE store_id = ?", (store_id,)).fetchone()[0]
    seen = {r["url_path"]: r["first_seen"] for r in conn.execute("SELECT url_path, first_seen FROM product_first_seen WHERE store_id = ?", (store_id,))}
    for p in products:
        path = p.get("url_path") or f"/products/{p['handle']}"
        fs = seen.get(path)
        if fs is None:
            fs = today
            conn.execute("INSERT OR IGNORE INTO product_first_seen (store_id, url_path, first_seen) VALUES (?,?,?)", (store_id, path, today))
            seen[path] = fs
        if not p.get("created_at"):
            unknown = first_snapshot is None or fs <= first_snapshot
            p["created_at"] = None if unknown else fs + "T00:00:00+00:00"
        if not p.get("published_at"):
            p["published_at"] = p["created_at"]
        if not p.get("updated_at"):
            p["updated_at"] = p["published_at"]


def is_store(domain: str, session: requests.Session | None = None, page_budget: int = 5) -> tuple[str | None, list[dict]]:
    """(platform, products) when the domain sells products through any known platform, else (None, []).
    Used by Radar triage: the generic adapter reads only `page_budget` product pages here (enough to confirm a store)."""
    session = session or shopify.make_session()
    try:
        cat = fetch_catalogue(domain, session, today=date.today().isoformat(), page_budget=page_budget, quiet=True)
    except (PlatformError, shopify.StoreFetchError, requests.RequestException, ValueError) as e:
        log.debug("%s: not a store (%s)", domain, e)
        return None, []
    return (cat.platform, cat.products) if cat.products else (None, [])
