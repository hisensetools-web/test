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

from . import config, db, deltas, sheets, shopify
from .watchlist import append_to_watchlist, read_watchlist, remove_from_watchlist

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
    rc = 1 if ok == 0 and failed else 0
    if config.SHEETS_WEBHOOK_URL and not args.no_sync:
        console.print("syncing to Google Sheets (SHEETS_WEBHOOK_URL is set) ...")
        if _do_sheets_sync(conn, as_of=snapshot_date) != 0:
            rc = rc or 3
    return rc


def _do_sheets_sync(conn, tabs=sheets.TAB_ORDER, as_of=None, dry_run=False) -> int:
    try:
        summaries = sheets.sync(conn, config.SHEETS_WEBHOOK_URL, tabs=tabs, as_of=as_of, dry_run=dry_run)
    except sheets.SheetsSyncError as e:
        console.print(f"[red]sheets sync failed:[/] {e}")
        return 3
    t = Table(title="Google Sheets sync" + (" (dry run, nothing sent)" if dry_run else ""))
    for c in ("tab", "rows", "chunks", "written", "skipped"):
        t.add_column(c, justify="right" if c != "tab" else "left")
    for x in summaries:
        t.add_row(x["tab"], str(x["rows"]), str(x["chunks"]),
                  "-" if dry_run else str(x["written"]), "-" if dry_run else str(x["skipped"]))
    console.print(t)
    return 0


def cmd_sync_sheets(args) -> int:
    if not config.SHEETS_WEBHOOK_URL and not args.dry_run:
        console.print("[red]SHEETS_WEBHOOK_URL is not set.[/] Put your Apps Script /exec URL in .env "
                      "(see README > Google Sheets sync), or use --dry-run to preview.")
        return 2
    conn = db.connect(args.db)
    tabs = tuple(t.strip().lower() for t in args.tabs.split(",")) if args.tabs else sheets.TAB_ORDER
    bad = [t for t in tabs if t not in sheets.TAB_ORDER]
    if bad:
        console.print(f"[red]unknown tab(s):[/] {', '.join(bad)} (choose from {', '.join(sheets.TAB_ORDER)})")
        return 2
    as_of = _parse_date(args.date) if args.date else None
    return _do_sheets_sync(conn, tabs=tabs, as_of=as_of, dry_run=args.dry_run)


def cmd_remove_store(args) -> int:
    removed, missing = remove_from_watchlist(args.domains, Path(args.watchlist) if args.watchlist else None)
    for d in removed:
        console.print(f"[green]removed[/] {d}")
    for d in missing:
        console.print(f"[yellow]not in watchlist[/] {d}")
    console.print(f"{len(removed)} removed, {len(missing)} not found. Snapshot history in the DB is kept.")
    return 0 if removed or not missing else 1


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


def _fmt_delta(v: int | None, signed: bool = True) -> str:
    if v is None:
        return "[dim]n/a[/]"
    if v == 0:
        return "0"
    if signed:
        return f"[red]+{v}[/]" if v > 0 else f"[green]{v}[/]"
    return f"[yellow]{v}[/]"


def cmd_report(args) -> int:
    conn = db.connect(args.db)
    as_of = _parse_date(args.date) if args.date else None
    rows = deltas.all_store_deltas(conn, as_of)
    if not rows:
        console.print("[red]no snapshots yet[/] - run `python tracker.py run` first")
        return 2
    no_hist = [d for d in rows if not d.has_history]
    t = Table(title=f"stores by change score (as of {rows[0].snapshot_date})", caption=
              "7d columns need only today's snapshot; delta columns compare against the previous snapshot day. "
              "n/a = insufficient history (1 day).")
    for c, j in (("store", "left"), ("snap", "left"), ("vs", "left"), ("products", "right"), ("new 7d", "right"),
                 ("upd 7d", "right"), ("sold-out", "right"), ("Δ sold-out", "right"), ("price Δ", "right"),
                 ("+handles", "right"), ("-handles", "right"), ("score", "right")):
        t.add_column(c, justify=j)
    for d in rows[:args.top]:
        t.add_row(d.store_domain, d.snapshot_date, d.prev_date or "[dim]1 day[/]", str(d.products),
                  str(d.new_products_7d), str(d.updated_products_7d), str(d.sold_out_variants),
                  _fmt_delta(d.sold_out_variants_delta), _fmt_delta(d.price_changes, signed=False),
                  _fmt_delta(d.new_handles, signed=False), _fmt_delta(d.removed_handles, signed=False),
                  f"[bold]{d.change_score}[/]")
    console.print(t)
    if no_hist:
        console.print(f"[dim]{len(no_hist)} store(s) have a single snapshot; deltas appear after the next run.[/]")

    changes = [(d, c) for d in rows for c in d.changes]
    if changes:
        t = Table(title=f"product changes (top {args.changes})")
        for c in ("store", "handle", "change", "detail"):
            t.add_column(c)
        colour = {"new_handle": "cyan", "sold_out": "red", "price": "yellow", "restocked": "green", "removed": "dim"}
        for d, c in changes[:args.changes]:
            t.add_row(d.store_domain, c.handle, f"[{colour[c.kind]}]{c.kind}[/]", c.detail)
        console.print(t)
    elif any(d.has_history for d in rows):
        console.print("[dim]no product-level changes vs the previous snapshot.[/]")
    return 0


def cmd_product(args) -> int:
    conn = db.connect(args.db)
    rows = deltas.product_history(conn, args.handle, args.store)
    if not rows:
        console.print(f"[red]no snapshots for handle[/] {args.handle}")
        return 2
    t = Table(title=f"{args.handle}  ({rows[0]['title'] or ''})", caption=f"published {rows[0]['published_at']}")
    for c, j in (("date", "left"), ("store", "left"), ("variants", "right"), ("sold-out", "right"),
                 ("min", "right"), ("max", "right"), ("coll. pos", "right"), ("updated_at", "left")):
        t.add_column(c, justify=j)
    prev = None
    for r in rows:
        so = str(r["sold_out_variants"])
        if prev and prev["store_domain"] == r["store_domain"] and r["sold_out_variants"] != prev["sold_out_variants"]:
            so = f"[red]{so}[/]" if r["sold_out_variants"] > prev["sold_out_variants"] else f"[green]{so}[/]"
        price = f"{r['min_price']:.2f}" if r["min_price"] is not None else "-"
        if prev and prev["store_domain"] == r["store_domain"] and r["min_price"] != prev["min_price"]:
            price = f"[yellow]{price}[/]"
        t.add_row(r["snapshot_date"], r["store_domain"], str(r["variant_count"]), so, price,
                  f"{r['max_price']:.2f}" if r["max_price"] is not None else "-",
                  str(r["collection_position"]) if r["collection_position"] is not None else "-",
                  (r["updated_at"] or "")[:19])
        prev = r
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
    s.add_argument("--no-sync", action="store_true", help="skip the Google Sheets sync even if SHEETS_WEBHOOK_URL is set")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("sync-sheets", help="push latest snapshot + deltas to Google Sheets via Apps Script")
    s.add_argument("--date", help="sync the snapshot as of this date (default: latest)")
    s.add_argument("--tabs", help="comma list from stores,products,alerts (default all)")
    s.add_argument("--dry-run", action="store_true", help="build and size the chunks but send nothing")
    s.set_defaults(fn=cmd_sync_sheets)

    s = sub.add_parser("remove-store", help="remove one or more domains from watchlist.csv")
    s.add_argument("domains", nargs="+")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.set_defaults(fn=cmd_remove_store)

    s = sub.add_parser("report", help="stores sorted by how much changed, with product-level changes")
    s.add_argument("--date", help="report as of this snapshot date (default: latest)")
    s.add_argument("--top", type=int, default=50, help="max stores to show")
    s.add_argument("--changes", type=int, default=40, help="max product changes to show")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("product", help="time series for one product handle")
    s.add_argument("handle")
    s.add_argument("--store", help="restrict to one store domain")
    s.set_defaults(fn=cmd_product)

    s = sub.add_parser("status", help="what is in the database")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.fn(args)
