"""Part A: product-level signals derived from products_daily.

Pure helpers (no DB) at the top so they can be unit-tested on plain dicts:
  - channel tag parsing from handle suffixes  (ceylon-cinnamon-tt -> "tiktok")
  - product families (handles that share a base name / normalised title)
  - categories derived from title keywords shared across stores (no hardcoded list)
DB-facing builders at the bottom produce rows for the Signals / Families / Categories tabs.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import deltas

# ---------------------------------------------------------------- channel tags

TAG_MAP = {
    "google": "google", "gg": "google", "gads": "google",
    "tt": "tiktok", "tiktok": "tiktok",
    "taboola": "taboola", "tb": "taboola", "outbrain": "outbrain",
    "fb": "fb", "facebook": "fb", "meta": "fb", "ig": "fb",
    "otp": "otp", "sub": "sub", "subscription": "sub", "subscribe": "sub",
    "cc": "coc", "coc": "coc",
    "vip": "vip",
    "prev": "retired", "old": "retired", "archive": "retired", "archived": "retired",
    "copy": "variant", "dup": "variant", "duplicate": "variant",
}
_NUMERIC_TOKEN = re.compile(r"^(v?\d{1,3}|copy\d*|[a-z]\d)$")


def split_handle(handle: str) -> tuple[str, list[str]]:
    """Return (base_handle, tags). Tags come from a leading 'copy-of-' and from trailing
    tokens that are channel words or version numbers; base is what is left."""
    tokens = [t for t in (handle or "").lower().split("-") if t]
    tags: list[str] = []
    if len(tokens) >= 3 and tokens[0] == "copy" and tokens[1] == "of":
        tags.append("variant")
        tokens = tokens[2:]
    trailing: list[str] = []
    while len(tokens) > 1:
        t = tokens[-1]
        if t in TAG_MAP:
            trailing.append(TAG_MAP[t])
            tokens.pop()
        elif _NUMERIC_TOKEN.match(t):
            trailing.append("variant")
            tokens.pop()
        else:
            break
    tags.extend(reversed(trailing))
    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return "-".join(tokens), seen


def channel_tag(handle: str) -> str:
    return "+".join(split_handle(handle)[1])


# ---------------------------------------------------------------- titles / families

_TITLE_NOISE = {
    # channel words that leak into titles, plus copy markers
    *TAG_MAP.keys(), "copy", "of", "test", "new", "v2", "v3",
}
_PUNCT = re.compile(r"[^a-z0-9]+")


def normalise_title(title: str | None) -> str:
    text = (title or "").lower().replace("'", "").replace("\u2019", "")   # lion's -> lions, not "lion s"
    words = [w for w in _PUNCT.sub(" ", text).split() if w]
    words = [w for w in words if w not in _TITLE_NOISE and not _NUMERIC_TOKEN.match(w)]
    return " ".join(words)


class _UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def assign_families(products: list[dict]) -> dict[int, str]:
    """products: dicts with product_id, handle, title (one store). Returns {product_id: family}.
    Two products share a family if their base handles match OR their normalised titles match."""
    uf = _UnionFind()
    base_of: dict[int, str] = {}
    for p in products:
        pid = p["product_id"]
        base, _ = split_handle(p["handle"])
        norm = normalise_title(p.get("title"))
        base_of[pid] = base or norm.replace(" ", "-") or p["handle"]
        uf.find(pid)
        if base:
            uf.union(("h", base), pid)
        if norm:
            uf.union(("t", norm), pid)
    groups: dict = defaultdict(list)
    for p in products:
        groups[uf.find(p["product_id"])].append(p["product_id"])
    out: dict[int, str] = {}
    for members in groups.values():
        # family name = most common base handle in the group, shortest on ties
        c = Counter(base_of[m] for m in members)
        name = sorted(c.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
        for m in members:
            out[m] = name
    return out


# ---------------------------------------------------------------- categories

_STOP = {
    # generic commerce / supplement words that never define a category on their own
    "the", "a", "an", "and", "or", "for", "with", "of", "in", "to", "by", "from", "plus", "pack", "packs",
    "bottle", "bottles", "bundle", "bundles", "set", "kit", "count", "ct", "mg", "mcg", "iu", "oz", "ml",
    "capsule", "capsules", "caps", "gummy", "gummies", "tablet", "tablets", "softgel", "softgels", "powder",
    "drops", "liquid", "tincture", "spray", "cream", "serum", "patch", "patches", "tea", "bag", "bags",
    "supplement", "supplements", "formula", "complex", "blend", "extract", "extracts", "support", "supports",
    "organic", "natural", "pure", "premium", "advanced", "ultra", "max", "pro", "original", "daily", "high",
    "strength", "potency", "extra", "double", "triple", "month", "months", "day", "days", "supply", "servings",
    "free", "shipping", "subscription", "subscribe", "save", "one", "time", "purchase", "buy", "get", "off",
    "special", "offer", "deal", "sale", "exclusive", "limited", "edition", "official", "new", "best", "seller",
    "men", "women", "mens", "womens", "adult", "adults", "kids", "vegan", "gluten", "non", "gmo", "usa", "made",
    "x", "s", "size", "small", "medium", "large", "black", "white", "blue", "red", "green", "pink",
}


def _title_tokens(title: str) -> list[str]:
    return [w for w in normalise_title(title).split() if w not in _STOP and len(w) > 2]


def _ngrams(tokens: list[str]) -> set[str]:
    grams = set(tokens)
    grams.update(f"{a} {b}" for a, b in zip(tokens, tokens[1:]))
    return grams


def derive_categories(families: list[dict], min_support: int = 2) -> dict[tuple[str, str], str]:
    """families: dicts with store, family, title. Returns {(store, family): category}.

    Candidate categories are unigrams/bigrams from titles. Support = number of distinct
    stores using the phrase (falls back to number of families when only one store is
    present). A family gets the best-supported phrase, bigrams preferred; families
    whose phrases are all unique get "".
    """
    grams_of: dict[tuple[str, str], set[str]] = {}
    store_support: dict[str, set[str]] = defaultdict(set)
    fam_support: Counter = Counter()
    for f in families:
        key = (f["store"], f["family"])
        g = _ngrams(_title_tokens(f.get("title") or f["family"].replace("-", " ")))
        grams_of[key] = g
        for gram in g:
            store_support[gram].add(f["store"])
            fam_support[gram] += 1
    multi_store = len({f["store"] for f in families}) > 1
    support = {g: (len(store_support[g]) if multi_store else fam_support[g]) for g in fam_support}
    out: dict[tuple[str, str], str] = {}
    for key, grams in grams_of.items():
        best, best_score = "", 0
        for g in grams:
            s = support[g]
            if s < min_support:
                continue
            score = s * (3 if " " in g else 1) + (len(g) / 100.0)
            if score > best_score:
                best, best_score = g, score
        out[key] = best
    return out


# ---------------------------------------------------------------- DB builders

def _days_between(iso_ts: str | None, as_of: date) -> int | None:
    dt = deltas.parse_ts(iso_ts)
    return None if dt is None else (as_of - dt.date()).days


def _snapshot_on_or_before(conn: sqlite3.Connection, store_id: int, target: str) -> str | None:
    r = conn.execute("SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = ? AND snapshot_date <= ?",
                     (store_id, target)).fetchone()
    return r[0] if r else None


def load_store_products(conn: sqlite3.Connection, store_id: int, snapshot_date: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT product_id, handle, title, published_at, updated_at, min_price, variant_count,
                  sold_out_variants, collection_position
           FROM products_daily WHERE store_id = ? AND snapshot_date = ?""", (store_id, snapshot_date))]


def store_signal_context(conn: sqlite3.Connection, store_id: int, store_domain: str,
                         as_of: str | None = None) -> dict | None:
    """Everything the Signals/Families builders need for one store, or None if no snapshot."""
    dates = deltas.snapshot_dates(conn, store_id, as_of)
    if not dates:
        return None
    today = dates[0]
    as_of_d = date.fromisoformat(today)
    products = load_store_products(conn, store_id, today)
    fam = assign_families(products)
    week_ago = _snapshot_on_or_before(conn, store_id, (as_of_d - timedelta(days=7)).isoformat())
    rank_7d: dict[int, int | None] = {}
    if week_ago:
        rank_7d = {p["product_id"]: p["collection_position"] for p in load_store_products(conn, store_id, week_ago)}
    from . import ad_metrics
    meta = ad_metrics.meta_for_signals(conn, store_id, today)
    return {"store_id": store_id, "store": store_domain, "date": today, "as_of": as_of_d,
            "products": products, "families": fam, "rank_7d": rank_7d, "rank_7d_date": week_ago, "meta": meta}


def _blank(v):
    return "" if v is None else v


def signals_rows_for_store(ctx: dict) -> list[list]:
    as_of, fam = ctx["as_of"], ctx["families"]
    fam_new_7d: Counter = Counter()
    for p in ctx["products"]:
        d = _days_between(p["published_at"], as_of)
        if d is not None and 0 <= d < 7:
            fam_new_7d[fam[p["product_id"]]] += 1
    rows = []
    meta = ctx.get("meta") or {}
    for p in ctx["products"]:
        m = meta.get(p["handle"], {})
        days = _days_between(p["published_at"], as_of)
        rank = None if p["collection_position"] is None else p["collection_position"] + 1
        old = ctx["rank_7d"].get(p["product_id"]) if ctx["rank_7d_date"] else None
        rank_delta = "" if (rank is None or old is None) else (old + 1) - rank   # positive = climbed
        sold_out = "Y" if p["variant_count"] and p["sold_out_variants"] >= p["variant_count"] else "N"
        rows.append([
            ctx["store"], fam[p["product_id"]], p["handle"], channel_tag(p["handle"]),
            "" if days is None else days, p["published_at"] or "",
            "" if p["min_price"] is None else p["min_price"], sold_out,
            "" if rank is None else rank, rank_delta, fam_new_7d[fam[p["product_id"]]],
            m.get("ads_pointing_here", ""), _blank(m.get("engagement_per_day")), _blank(m.get("days_running_max")),
            m.get("concept_status", ""),
        ])
    return rows


def families_rows_for_store(ctx: dict) -> list[list]:
    as_of, fam = ctx["as_of"], ctx["families"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in ctx["products"]:
        groups[fam[p["product_id"]]].append(p)
    rows = []
    for name, members in groups.items():
        pubs = [p["published_at"] for p in members if p["published_at"]]
        ages = [_days_between(p["published_at"], as_of) for p in members]
        ages = [a for a in ages if a is not None]
        ranks = [p["collection_position"] + 1 for p in members if p["collection_position"] is not None]
        title = Counter(normalise_title(p["title"]) for p in members).most_common(1)[0][0]
        rows.append([
            ctx["store"], name, title, len(members),
            max(pubs) if pubs else "", min(pubs) if pubs else "",
            sum(1 for a in ages if 0 <= a < 7), sum(1 for a in ages if 0 <= a < 14), sum(1 for a in ages if 0 <= a < 30),
            min(ranks) if ranks else "",
            ", ".join(sorted(p["handle"] for p in members))[:500],
        ])
    return rows


def categories_rows(family_rows: list[list]) -> list[list]:
    """family_rows are Families-tab rows (store, family, title, count, newest, oldest, 7d, 14d, 30d, ...)."""
    fams = [{"store": r[0], "family": r[1], "title": r[2], "newest": r[4], "n7": r[6]} for r in family_rows]
    cat = derive_categories(fams)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for f in fams:
        buckets[cat[(f["store"], f["family"])] or "(uncategorised)"].append(f)
    rows = []
    for name, members in buckets.items():
        stores = sorted({m["store"] for m in members})
        newest = max((m["newest"] for m in members if m["newest"]), default="")
        rows.append([name, len(stores), len(members), newest, sum(m["n7"] for m in members),
                     ", ".join(stores[:8]), ", ".join(sorted({m["family"] for m in members})[:8])])
    rows.sort(key=lambda r: (r[0] == "(uncategorised)", -r[1], -r[2], r[3] and -int(r[3][:4]), r[0]))
    return rows
