"""Build step 2: day-over-day and 7-day delta calculations.

All arithmetic lives in pure functions over plain dicts so it can be tested with
fixture JSON. The DB-facing helpers at the bottom only load rows and call them.

With a single day of history the 7-day metrics (which only need today's
published_at/updated_at) are real; the comparison metrics are None and the
report shows "n/a (1 day)".
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone


# ---------------------------------------------------------------- helpers

def parse_ts(value: str | None) -> datetime | None:
    """Shopify timestamps look like 2026-08-30T10:00:00-04:00 (or ...Z). Returns aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def within_days(value: str | None, as_of: date, days: int) -> bool:
    """True if `value` falls in the `days`-day window ending at the end of `as_of` (UTC)."""
    dt = parse_ts(value)
    if dt is None:
        return False
    end = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    start = end - timedelta(days=days)
    return start <= dt < end


# ---------------------------------------------------------------- results

@dataclass
class ProductChange:
    handle: str
    title: str | None
    kind: str          # new_handle | removed | sold_out | restocked | price
    detail: str


@dataclass
class StoreDelta:
    store_id: int
    store_domain: str
    snapshot_date: str
    prev_date: str | None            # snapshot compared against (None = insufficient history)
    products: int
    variants: int
    new_products_7d: int
    updated_products_7d: int
    sold_out_variants: int
    sold_out_variants_delta: int | None
    price_changes: int | None
    new_handles: int | None
    removed_handles: int | None
    changes: list[ProductChange] = field(default_factory=list)

    @property
    def has_history(self) -> bool:
        return self.prev_date is not None

    @property
    def change_score(self) -> int:
        """Crude 'how much moved' number used to sort the report. updated_products_7d is
        deliberately left out: inventory apps bump updated_at on every product daily, so it
        equals the catalogue size on most stores and would swamp the real signals."""
        s = self.new_products_7d
        if self.has_history:
            s += abs(self.sold_out_variants_delta or 0) + (self.price_changes or 0) \
                 + (self.new_handles or 0) + (self.removed_handles or 0)
        return s

    def to_dict(self) -> dict:
        d = asdict(self)
        d["change_score"] = self.change_score
        return d


# ---------------------------------------------------------------- pure calculations

def compute_store_delta(store_id: int, store_domain: str, snapshot_date: str,
                        today_products: list[dict], today_variants: list[dict],
                        prev_date: str | None = None,
                        prev_products: list[dict] | None = None,
                        prev_variants: list[dict] | None = None,
                        max_changes: int = 50) -> StoreDelta:
    """`*_products` rows need: product_id, handle, title, published_at, updated_at, sold_out_variants,
    variant_count. `*_variants` rows need: variant_id, product_id, price, available."""
    as_of = date.fromisoformat(snapshot_date)
    sold_out_today = sum(p["sold_out_variants"] for p in today_products)
    d = StoreDelta(
        store_id=store_id, store_domain=store_domain, snapshot_date=snapshot_date, prev_date=prev_date,
        products=len(today_products), variants=sum(p["variant_count"] for p in today_products),
        new_products_7d=sum(1 for p in today_products if within_days(p["published_at"], as_of, 7)),
        updated_products_7d=sum(1 for p in today_products if within_days(p["updated_at"], as_of, 7)),
        sold_out_variants=sold_out_today,
        sold_out_variants_delta=None, price_changes=None, new_handles=None, removed_handles=None,
    )
    if prev_date is None or prev_products is None:
        return d

    prev_by_id = {p["product_id"]: p for p in prev_products}
    today_by_id = {p["product_id"]: p for p in today_products}
    title_of = {p["product_id"]: p.get("title") for p in today_products + prev_products}
    handle_of = {p["product_id"]: p["handle"] for p in today_products + prev_products}

    d.sold_out_variants_delta = sold_out_today - sum(p["sold_out_variants"] for p in prev_products)
    new_ids = [pid for pid in today_by_id if pid not in prev_by_id]
    removed_ids = [pid for pid in prev_by_id if pid not in today_by_id]
    d.new_handles = len(new_ids)
    d.removed_handles = len(removed_ids)

    changes: list[ProductChange] = []
    for pid in new_ids:
        p = today_by_id[pid]
        changes.append(ProductChange(p["handle"], p.get("title"), "new_handle",
                                     f"published {str(p.get('published_at') or '?')[:10]}"))
    for pid in removed_ids:
        changes.append(ProductChange(handle_of[pid], title_of[pid], "removed", "gone from products.json"))

    prev_v = {v["variant_id"]: v for v in (prev_variants or [])}
    price_changes = 0
    price_by_product: dict[int, list[tuple[float, float]]] = {}
    for v in today_variants:
        pv = prev_v.get(v["variant_id"])
        if pv is None:
            continue
        if v["price"] is not None and pv["price"] is not None and v["price"] != pv["price"]:
            price_changes += 1
            price_by_product.setdefault(v["product_id"], []).append((pv["price"], v["price"]))
    d.price_changes = price_changes
    for pid, pairs in price_by_product.items():
        old, new = pairs[0]
        pct = (new - old) / old * 100 if old else 0
        changes.append(ProductChange(handle_of.get(pid, "?"), title_of.get(pid), "price",
                                     f"{old:.2f} -> {new:.2f} ({pct:+.0f}%, {len(pairs)} variant(s))"))

    for pid, p in today_by_id.items():
        pv = prev_by_id.get(pid)
        if pv is None:
            continue
        if p["sold_out_variants"] > pv["sold_out_variants"]:
            fully = p["sold_out_variants"] == p["variant_count"] and p["variant_count"] > 0
            changes.append(ProductChange(p["handle"], p.get("title"), "sold_out",
                                         f"sold-out variants {pv['sold_out_variants']} -> {p['sold_out_variants']}"
                                         + (" (ALL)" if fully else "")))
        elif p["sold_out_variants"] < pv["sold_out_variants"]:
            changes.append(ProductChange(p["handle"], p.get("title"), "restocked",
                                         f"sold-out variants {pv['sold_out_variants']} -> {p['sold_out_variants']}"))
    order = {"new_handle": 0, "sold_out": 1, "price": 2, "restocked": 3, "removed": 4}
    changes.sort(key=lambda c: (order[c.kind], c.handle))
    d.changes = changes[:max_changes]
    return d


# ---------------------------------------------------------------- DB loaders

def snapshot_dates(conn: sqlite3.Connection, store_id: int, up_to: str | None = None) -> list[str]:
    """Distinct snapshot dates for a store, newest first."""
    sql = "SELECT DISTINCT snapshot_date FROM products_daily WHERE store_id = ?"
    args: list = [store_id]
    if up_to:
        sql += " AND snapshot_date <= ?"
        args.append(up_to)
    return [r[0] for r in conn.execute(sql + " ORDER BY snapshot_date DESC", args)]


def load_products(conn: sqlite3.Connection, store_id: int, snapshot_date: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT product_id, handle, title, published_at, updated_at, sold_out_variants, variant_count,
                  min_price, max_price, collection_position
           FROM products_daily WHERE store_id = ? AND snapshot_date = ?""", (store_id, snapshot_date))]


def load_variants(conn: sqlite3.Connection, store_id: int, snapshot_date: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT variant_id, product_id, price, available FROM variants_daily WHERE store_id = ? AND snapshot_date = ?",
        (store_id, snapshot_date))]


def store_delta_from_db(conn: sqlite3.Connection, store_id: int, store_domain: str,
                        as_of: str | None = None) -> StoreDelta | None:
    """Delta for the latest snapshot on/before `as_of` vs the snapshot before it.
    Returns None if the store has no snapshots."""
    dates = snapshot_dates(conn, store_id, as_of)
    if not dates:
        return None
    today = dates[0]
    prev = dates[1] if len(dates) > 1 else None
    return compute_store_delta(
        store_id, store_domain, today,
        load_products(conn, store_id, today), load_variants(conn, store_id, today),
        prev,
        load_products(conn, store_id, prev) if prev else None,
        load_variants(conn, store_id, prev) if prev else None,
    )


def all_store_deltas(conn: sqlite3.Connection, as_of: str | None = None) -> list[StoreDelta]:
    out = []
    for r in conn.execute("SELECT id, store_domain FROM stores ORDER BY store_domain"):
        d = store_delta_from_db(conn, r["id"], r["store_domain"], as_of)
        if d is not None:
            out.append(d)
    out.sort(key=lambda d: (-d.change_score, d.store_domain))
    return out


def product_history(conn: sqlite3.Connection, handle: str, store_domain: str | None = None) -> list[dict]:
    sql = """SELECT p.snapshot_date, s.store_domain, p.title, p.published_at, p.updated_at, p.variant_count,
                    p.sold_out_variants, p.min_price, p.max_price, p.collection_position
             FROM products_daily p JOIN stores s ON s.id = p.store_id WHERE p.handle = ?"""
    args: list = [handle]
    if store_domain:
        sql += " AND s.store_domain = ?"
        args.append(store_domain)
    return [dict(r) for r in conn.execute(sql + " ORDER BY s.store_domain, p.snapshot_date", args)]
