"""Command-line interface. Build step 1: init-db, add-store, run (products only), status."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import config, db, shopify
from .watchlist import append_to_watchlist, read_watchlist

console = Console()
log = logging.getLogger("earlyscale")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_date(s: str | None) -> str:
    if not s:
        return date.today().isoformat()
    return datetime.strptime(s, "%Y-%m-%d").date().isoformat()


# ---------------------------------------------------------------- commands

def cmd_init_db(args) -> int:
    conn = db.connect(args.db)
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    console.print(f"[green]ok[/] {args.db or config.DB_PATH} tables: {', '.join(tables)}")
    return 0


def cmd_add_store(args) -> int:
    domain = args.domain
    meta_page = args.meta_page
    if not meta_page and not args.no_discover:
        console.print(f"looking for a Facebook page link on {domain} ...")
        meta_page = shopify.discover_meta_page(domain)
        console.print(f"  meta_page_name: [bold]{meta_page or '(not found; fill in watchlist.csv)'}[/]")
    entry = {"store_domain": domain, "meta_page_name": meta_page or "", "meta_page_id": args.meta_page_id or "",
             "notes": args.notes or ""}
    added = append_to_watchlist(entry, Path(args.watchlist) if args.watchlist else None)
    conn = db.connect(args.db)
    db.upsert_store(conn, entry["store_domain"] if added else domain.lower(), meta_page, args.meta_page_id, args.notes)
    conn.commit()
    console.print(f"[green]{'added' if added else 'already listed'}[/] {domain}")
    return 0


def run_products_pass(conn, stores: list[dict], snapshot_date: str, only: set[str] | None = None) -> tuple[int, int]:
    """Fetch and snapshot every store. Returns (ok, failed). A failing store never aborts the run."""
    if only:
        stores = [s for s in stores if s["store_domain"] in only]
    run_id = db.start_run(conn, snapshot_date, len(stores))
    session = shopify.make_session()
    ok = failed = 0
    for s in stores:
        domain = s["store_domain"]
        store_id = db.upsert_store(conn, domain, s.get("meta_page_name"), s.get("meta_page_id"), s.get("notes"))
        conn.commit()
        t0 = time.monotonic()
        try:
            raw, positions, pages = shopify.fetch_store(domain, session)
            products = shopify.normalise_products(raw, positions)
            n = db.write_product_snapshot(conn, store_id, snapshot_date, products)
            dur = time.monotonic() - t0
            db.record_store_run(conn, run_id, store_id, snapshot_date, "ok", None, n, pages, dur)
            sold_out = sum(p["sold_out_variants"] for p in products)
            log.info("%-28s ok  products=%-5d variants=%-5d sold_out_variants=%-4d pages=%d  %.1fs",
                     domain, n, sum(p["variant_count"] for p in products), sold_out, pages, dur)
            ok += 1
        except Exception as e:  # noqa: BLE001 - by design: log and continue
            dur = time.monotonic() - t0
            db.record_store_run(conn, run_id, store_id, snapshot_date, "error", str(e)[:500], 0, 0, dur)
            log.error("%-28s FAILED: %s", domain, e)
            failed += 1
    db.finish_run(conn, run_id, ok, failed)
    return ok, failed


def cmd_run(args) -> int:
    snapshot_date = _parse_date(args.date)
    stores = read_watchlist(Path(args.watchlist) if args.watchlist else None)
    if not stores:
        console.print("[red]watchlist is empty[/] - add stores with `python tracker.py add-store <domain>`")
        return 2
    conn = db.connect(args.db)
    only = set(args.only) if args.only else None
    console.print(f"run: snapshot_date={snapshot_date} stores={len(only or stores)} db={args.db or config.DB_PATH}")
    t0 = time.monotonic()
    ok, failed = run_products_pass(conn, stores, snapshot_date, only)
    console.print(f"done in {time.monotonic() - t0:.1f}s: [green]{ok} ok[/], [red]{failed} failed[/]")
    return 1 if ok == 0 and failed else 0


def cmd_status(args) -> int:
    conn = db.connect(args.db)
    runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    t = Table(title="recent runs")
    for c in ("id", "snapshot_date", "started_at", "finished_at", "total", "ok", "failed"):
        t.add_column(c)
    for r in runs:
        t.add_row(str(r["id"]), r["snapshot_date"], r["started_at"], r["finished_at"] or "-",
                  str(r["stores_total"]), str(r["stores_ok"]), str(r["stores_failed"]))
    console.print(t)

    rows = conn.execute(
        """SELECT s.store_domain, s.meta_page_name,
                  COUNT(DISTINCT p.snapshot_date) AS days,
                  MIN(p.snapshot_date) AS first_day, MAX(p.snapshot_date) AS last_day,
                  (SELECT COUNT(*) FROM products_daily p2 WHERE p2.store_id = s.id
                     AND p2.snapshot_date = MAX(p.snapshot_date)) AS products_last,
                  (SELECT SUM(sold_out_variants) FROM products_daily p3 WHERE p3.store_id = s.id
                     AND p3.snapshot_date = MAX(p.snapshot_date)) AS sold_out_last,
                  (SELECT sr.status || COALESCE(': ' || sr.error, '') FROM store_runs sr
                     WHERE sr.store_id = s.id ORDER BY sr.run_id DESC LIMIT 1) AS last_status
           FROM stores s LEFT JOIN products_daily p ON p.store_id = s.id
           GROUP BY s.id ORDER BY s.store_domain"""
    ).fetchall()
    t = Table(title="stores")
    for c in ("store", "meta page", "days", "first", "last", "products", "sold-out variants", "last status"):
        t.add_column(c)
    for r in rows:
        status = r["last_status"] or "-"
        t.add_row(r["store_domain"], r["meta_page_name"] or "-", str(r["days"]), r["first_day"] or "-",
                  r["last_day"] or "-", str(r["products_last"] or 0), str(r["sold_out_last"] or 0),
                  f"[green]{status}[/]" if status == "ok" else f"[red]{status[:60]}[/]")
    console.print(t)
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tracker.py", description="Shopify early-scaling tracker")
    p.add_argument("--db", help=f"SQLite path (default {config.DB_PATH})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-db", help="create data/tracker.db and tables")
    s.set_defaults(fn=cmd_init_db)

    s = sub.add_parser("add-store", help="add a store to watchlist.csv (tries to discover its Facebook page)")
    s.add_argument("domain")
    s.add_argument("--meta-page", help="Meta page name (skips discovery)")
    s.add_argument("--meta-page-id")
    s.add_argument("--notes")
    s.add_argument("--no-discover", action="store_true")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.set_defaults(fn=cmd_add_store)

    s = sub.add_parser("run", help="one full pass over the watchlist (step 1: products.json snapshot)")
    s.add_argument("--date", help="snapshot date YYYY-MM-DD (default today); re-running a date replaces it")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.add_argument("--only", nargs="+", metavar="DOMAIN", help="limit to these store domains")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="what is in the database")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)
