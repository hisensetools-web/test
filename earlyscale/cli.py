"""Command-line interface: run (catalogues), ads (Meta Ad Library), sync-sheets, and the watchlist / diagnostics helpers. `python tracker.py --help` lists everything."""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.table import Table

from . import config, db, meta_ads, platforms, sheets, shopify, store_age, winners
from .watchlist import append_to_watchlist, normalise_domain, read_watchlist, remove_from_watchlist, update_watchlist_entry

console = Console()
log = logging.getLogger("earlyscale")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_date(s: str | None) -> str:
    if not s:
        return date.today().isoformat()
    return datetime.strptime(s, "%Y-%m-%d").date().isoformat()


def _short(domain: str) -> str:
    return re.sub(r"^https?://", "", domain or "")


def _wl(args) -> Path | None:
    return Path(args.watchlist) if getattr(args, "watchlist", None) else None


# ---------------------------------------------------------------- catalogues

def cmd_init_db(args) -> int:
    conn = db.connect(args.db)
    tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    console.print(f"[green]ok[/] {args.db or config.DB_PATH} tables: {', '.join(tables)}")
    return 0


def run_products_pass(conn, stores: list[dict], snapshot_date: str, only: set[str] | None = None) -> tuple[int, int]:
    """Fetch and snapshot every store's catalogue (id, handle, title, dates) and, once, its shop id.
    Returns (ok, failed). A failing store never aborts the run."""
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
            cat = platforms.fetch_catalogue(domain, session, conn, store_id, snapshot_date)
            n = db.write_product_snapshot(conn, store_id, snapshot_date, cat.products)
            dur = time.monotonic() - t0
            db.record_store_run(conn, run_id, store_id, snapshot_date, "ok", None, n, cat.pages, dur)
            log.info("%-28s ok  %-16s products=%-5d pages=%d  %.1fs%s", domain, cat.platform, n, cat.pages, dur,
                     f"  ({cat.note})" if cat.note and cat.platform != "shopify" else "")
            ok += 1
            if cat.platform in ("shopify", "shopify_headless"):
                try:   # shop id + myshopify handle, once per store (they never change)
                    store_age.ensure_identity(conn, store_id, cat.extra.get("myshopify") or domain, session)
                except Exception as e:  # noqa: BLE001
                    log.debug("%s shop id lookup failed: %s", domain, e)
        except Exception as e:  # noqa: BLE001 - by design: log and continue
            dur = time.monotonic() - t0
            db.record_store_run(conn, run_id, store_id, snapshot_date, "error", str(e)[:500], 0, 0, dur)
            log.error("%-28s FAILED: %s", domain, e)
            failed += 1
    db.finish_run(conn, run_id, ok, failed)
    try:
        r = store_age.refresh_estimates(conn)
        log.info("store age: %d of %d stores estimated from %d calibration point(s)", r["estimated"], r["stores"], r["calibration_rows"])
    except Exception as e:  # noqa: BLE001
        log.warning("store age estimates not refreshed: %s", e)
    return ok, failed


def cmd_run(args) -> int:
    snapshot_date = _parse_date(args.date)
    stores = read_watchlist(_wl(args))
    if not stores:
        console.print("[red]watchlist is empty[/] - add stores with `python tracker.py add-store <domain>`")
        return 2
    conn = db.connect(args.db)
    only = set(args.only) if args.only else None
    console.print(f"run: snapshot_date={snapshot_date} stores={len(only or stores)} db={args.db or config.DB_PATH}")
    t0 = time.monotonic()
    with meta_ads.KeepAwake():
        ok, failed = run_products_pass(conn, stores, snapshot_date, only)
        console.print(f"done in {time.monotonic() - t0:.1f}s: [green]{ok} ok[/], [red]{failed} failed[/]")
        rc = 1 if ok == 0 and failed else 0
        if (config.META_ADS_ENABLED or args.ads) and not args.no_ads:
            planned = plan_meta_stores(conn, stores, only)
            console.print(f"Meta Ad Library pass: {len(planned)} store(s), budget {config.META_MAX_MINUTES:.0f} min, least recently scraped first ...")
            try:
                a_ok, a_failed = run_ads_pass(conn, stores, snapshot_date, only)
                console.print(f"ads: [green]{a_ok} ok[/], [red]{a_failed} failed[/]")
            except Exception as e:  # noqa: BLE001 - never let the ad pass break the daily run
                console.print(f"[red]ads pass failed:[/] {e}")
        if config.SHEETS_WEBHOOK_URL and not args.no_sync:
            if args.watchlist:
                sheets.set_watchlist_path(Path(args.watchlist))
            console.print("syncing to Google Sheets (SHEETS_WEBHOOK_URL is set) ...")
            if _do_sheets_sync(conn, as_of=snapshot_date) != 0:
                rc = rc or 3
    return rc


# ---------------------------------------------------------------- Meta Ad Library

def _stop_at_clock(hhmm: str, now: datetime | None = None) -> datetime | None:
    """META_STOP_AT ('08:30') as the next occurrence of that local time (today if still ahead, else tomorrow)."""
    if not hhmm:
        return None
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        return None
    now = now or datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def plan_meta_stores(conn, stores: list[dict], only: set[str] | None = None, scope: list[str] | None = None) -> list[dict]:
    """Stores for today's Meta pass: --only, else META_STORES if set, else all; least recently scraped first
    (never scraped first) so a watchlist bigger than the daily budget rotates."""
    if only:
        stores = [s for s in stores if s["store_domain"] in only]
    else:
        scope = config.META_STORES if scope is None else scope
        if scope:
            want = {x.lower() for x in scope}
            stores = [s for s in stores if s["store_domain"].lower() in want or re.sub(r"^https?://", "", s["store_domain"].lower()) in want]
    last: dict[str, str] = {}
    for r in conn.execute("""SELECT s.store_domain, MAX(r.snapshot_date) AS d FROM meta_page_runs r JOIN stores s ON s.id = r.store_id
                             WHERE r.status = 'ok' GROUP BY s.store_domain"""):
        last[r["store_domain"]] = r["d"]
    return sorted(stores, key=lambda s: (last.get(s["store_domain"]) or "", s["store_domain"]))


def run_ads_pass(conn, stores: list[dict], snapshot_date: str, only: set[str] | None = None,
                 headless: bool = True, max_scrolls: int | None = None, max_minutes: float | None = None) -> tuple[int, int]:
    """Scrape the Ad Library for the planned stores (one browser, sequential) within a time budget.
    Returns (ok, failed). A blocked or failing page is logged in meta_page_runs and never aborts the pass."""
    from playwright.sync_api import sync_playwright
    stores = plan_meta_stores(conn, stores, only)
    budget = (config.META_MAX_MINUTES if max_minutes is None else max_minutes) * 60
    deadline = time.monotonic() + budget
    stop_at = _stop_at_clock(config.META_STOP_AT)
    ok = failed = 0
    with sync_playwright() as p, meta_ads.KeepAwake():
        handle = meta_ads.BrowserHandle(p, headless=headless)
        try:
            for i, s in enumerate(stores):
                if time.monotonic() > deadline or (stop_at and datetime.now() >= stop_at):
                    left = [x["store_domain"] for x in stores[i:]]
                    log.warning("Meta pass %s: %d store(s) deferred to the next run (%s%s)",
                                f"stop time {config.META_STOP_AT} reached" if stop_at and datetime.now() >= stop_at else f"budget of {budget / 60:.0f} min spent",
                                len(left), ", ".join(left[:5]), ", ..." if len(left) > 5 else "")
                    break
                browser = handle.get()   # relaunched if the previous store killed it
                domain = s["store_domain"]
                store_id = db.upsert_store(conn, domain, s.get("meta_page_name"), s.get("meta_page_id"), s.get("notes"))
                conn.commit()
                query = meta_ads.store_query(s)
                url = meta_ads.build_search_url(query=None if s.get("meta_page_id") else query, page_id=s.get("meta_page_id") or None)
                t0 = time.monotonic()
                try:
                    res = meta_ads.scrape_page(url, headless=headless, max_scrolls=max_scrolls, browser=browser)
                    counts = meta_ads.record_scrape(conn, store_id, snapshot_date, res.ads, query)
                    meta_ads.record_page_run(conn, store_id, snapshot_date, query, "ok", res.note, len(res.ads), res.scrolls, time.monotonic() - t0)
                    log.info("%-28s ads=%-4d new=%-4d disappeared=%-3d badge_known=%-4d urls=%-3d scrolls=%d %.0fs  %s",
                             domain, counts["total"], counts["new"], counts["disappeared"], counts["badge_known"], counts["urls"],
                             res.scrolls, time.monotonic() - t0, res.note)
                    ok += 1
                except meta_ads.MetaBlocked as e:
                    meta_ads.record_page_run(conn, store_id, snapshot_date, query, "blocked", str(e), 0, 0, time.monotonic() - t0)
                    log.error("%-28s BLOCKED by Meta: %s (stopping this pass; try again later)", domain, e)
                    failed += 1
                    break
                except Exception as e:  # noqa: BLE001 - by design
                    meta_ads.record_page_run(conn, store_id, snapshot_date, query, "error", str(e), 0, 0, time.monotonic() - t0)
                    log.error("%-28s FAILED: %s", domain, e)
                    failed += 1
                    if meta_ads.browser_closed_error(e):
                        log.warning("%-28s browser crashed; relaunching for the next store", domain)
                        handle.close()
                took = time.monotonic() - t0
                if took > 25 * 60:
                    # the machine slept: give the lost time back to the budget (the stop time, if set, still holds)
                    deadline += took - 25 * 60
                    log.warning("%-28s took %.0f min for one store; the machine probably slept part of the way (%.0f min given back to the budget)",
                                domain, took / 60, (took - 25 * 60) / 60)
                if i < len(stores) - 1:
                    meta_ads._wait()
        finally:
            handle.close()
    return ok, failed


def cmd_ads(args) -> int:
    snapshot_date = _parse_date(args.date)
    stores = read_watchlist(_wl(args))
    if not stores:
        console.print("[red]watchlist is empty[/]")
        return 2
    conn = db.connect(args.db)
    only = set(args.only) if args.only else None
    planned = plan_meta_stores(conn, stores, only)
    console.print(f"ads: snapshot_date={snapshot_date} stores={len(planned)} (least recently scraped first) "
                  f"budget {args.max_minutes or config.META_MAX_MINUTES:.0f} min, about {config.META_MAX_SCROLLS * 5.5 / 60 + 1:.0f} min per store, "
                  f"waits={config.META_WAIT_MIN:.0f}-{config.META_WAIT_MAX:.0f}s headless={not args.headed}")
    t0 = time.monotonic()
    ok, failed = run_ads_pass(conn, stores, snapshot_date, only, headless=not args.headed, max_scrolls=args.max_scrolls, max_minutes=args.max_minutes)
    console.print(f"done in {time.monotonic() - t0:.0f}s: [green]{ok} ok[/], [red]{failed} failed[/]. "
                  "`python tracker.py report` shows the Winners; `sync-sheets` pushes them.")
    return 1 if ok == 0 and failed else 0


def cmd_rebuild(args) -> int:
    """Recompute url_daily (delivering, wow, pages, families) for every day from the stored ads (no scraping)."""
    conn = db.connect(args.db)
    t0 = time.monotonic()
    n = winners.rebuild_url_daily(conn)
    console.print(f"url_daily rebuilt: {n} URL-day rows in {time.monotonic() - t0:.0f}s")
    return 0


# ---------------------------------------------------------------- Google Sheets

def _do_sheets_sync(conn, tabs=sheets.TAB_ORDER, as_of=None, dry_run=False, verify=True) -> int:
    try:
        summaries = sheets.sync(conn, config.SHEETS_WEBHOOK_URL, tabs=tabs, as_of=as_of, dry_run=dry_run)
    except sheets.SheetsSyncError as e:
        console.print(f"[red]sheets sync failed:[/] {e}")
        return 3
    rc = _print_sync_summary(summaries, dry_run)
    if verify and not dry_run:
        rc = _verify_sheets(conn, as_of) or rc
        if rc == 3 and not getattr(_do_sheets_sync, "_repairing", False):
            # a tab holding the wrong number of rows (a lost chunk, a concurrent writer): push it again once
            bad = [t for t in tabs if sheets.TAB_NAMES[t] in _last_mismatched_tabs]
            if bad:
                console.print(f"[yellow]re-pushing {', '.join(sheets.TAB_NAMES[t] for t in bad)} once to repair the mismatch[/]")
                _do_sheets_sync._repairing = True
                try:
                    rc = _do_sheets_sync(conn, tabs=bad, as_of=as_of, dry_run=False, verify=True)
                finally:
                    _do_sheets_sync._repairing = False
    return rc


_last_mismatched_tabs: set[str] = set()


def _verify_sheets(conn, as_of=None) -> int:
    """Read row counts back from the live sheet and compare with the DB. Returns 3 on a mismatch."""
    try:
        v = sheets.verify(conn, config.SHEETS_WEBHOOK_URL, as_of)
    except sheets.SheetsSyncError as e:
        console.print(f"[yellow]could not verify the sheet:[/] {e}")
        return 0
    _last_mismatched_tabs.clear()
    _last_mismatched_tabs.update(x["tab"] for x in v["tabs"] if not x["ok"])
    t = Table(title="sheet vs database")
    for c in ("tab", "rows in DB", "rows in sheet", "ok"):
        t.add_column(c, justify="right" if "rows" in c else "left")
    for x in v["tabs"]:
        t.add_row(x["tab"], str(x["expected"]), "?" if x["sheet"] is None else str(x["sheet"]), "yes" if x["ok"] else "[red]NO[/]")
    console.print(t)
    if v["problems"]:
        console.print(f"[red]{len(v['problems'])} mismatch(es)[/] - the sheet does not hold what the DB holds (see above)")
        return 3
    console.print("[green]sheet matches the database[/]")
    return 0


def _print_sync_summary(summaries, dry_run) -> int:
    t = Table(title="Google Sheets sync" + (" (dry run, nothing sent)" if dry_run else ""))
    for c in ("tab", "rows", "chunks", "written"):
        t.add_column(c, justify="right" if c != "tab" else "left")
    for x in summaries:
        t.add_row(x["tab"], str(x["rows"]), str(x["chunks"]), "-" if dry_run else str(x["written"]))
    console.print(t)
    return 0


def cmd_sync_sheets(args) -> int:
    if not config.SHEETS_WEBHOOK_URL and not args.dry_run:
        console.print("[red]SHEETS_WEBHOOK_URL is not set.[/] Put your Apps Script /exec URL in .env "
                      "(see README > Google Sheets sync), or use --dry-run to preview.")
        return 2
    conn = db.connect(args.db)
    if args.watchlist:
        sheets.set_watchlist_path(Path(args.watchlist))
    tabs = tuple(t.strip().lower() for t in args.tabs.split(",")) if args.tabs else sheets.TAB_ORDER
    bad = [t for t in tabs if t not in sheets.TAB_ORDER]
    if bad:
        console.print(f"[red]unknown tab(s):[/] {', '.join(bad)} (choose from {', '.join(sheets.TAB_ORDER)})")
        return 2
    as_of = _parse_date(args.date) if args.date else None
    try:
        store_age.refresh_estimates(conn)
    except Exception as e:  # noqa: BLE001
        log.warning("store age estimates not refreshed: %s", e)
    if args.verify_only:
        return _verify_sheets(conn, as_of)
    return _do_sheets_sync(conn, tabs=tabs, as_of=as_of, dry_run=args.dry_run, verify=not args.no_verify)


# ---------------------------------------------------------------- watchlist

def cmd_add_store(args) -> int:
    """Add one or more stores to watchlist.csv (and the database). No Facebook page is needed: the Meta pass searches
    the Ad Library for the domain, which returns every page advertising it."""
    conn = db.connect(args.db)
    domains = list(args.domains)
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().split(",")[0].strip()
            if line and not line.startswith("#") and "." in line:
                domains.append(line)
    if not domains:
        console.print("[red]no domains given[/] (list them on the command line, or --file domains.txt with one per line)")
        return 2
    added = 0
    for raw in domains:
        domain = normalise_domain(raw)
        entry = {"store_domain": domain, "meta_page_name": "", "meta_page_id": (args.page_id or "") if len(domains) == 1 else "",
                 "notes": args.notes or ""}
        ok = append_to_watchlist(entry, _wl(args))
        db.upsert_store(conn, domain, None, entry["meta_page_id"] or None, args.notes)
        conn.commit()
        added += 1 if ok else 0
        console.print(f"[green]{'added' if ok else 'already listed'}[/] {domain}")
    console.print(f"{added} of {len(domains)} added. Catalogue on the next `run`, ads on the next `ads` pass "
                  "(python tracker.py ads --only <domain> to do one now).")
    return 0


def cmd_remove_store(args) -> int:
    removed, missing = remove_from_watchlist(args.domains, _wl(args))
    for d in removed:
        console.print(f"[green]removed[/] {d}")
    for d in missing:
        console.print(f"[yellow]not in watchlist[/] {d}")
    console.print(f"{len(removed)} removed, {len(missing)} not found. Snapshot history in the DB is kept.")
    return 0 if removed or not missing else 1


def dead_stores(conn, stores: list[dict]) -> list[dict]:
    """Watchlist stores that never returned a catalogue (no successful run) and never had an ad recorded:
    non-store domains, typos, dead sites. Each entry: store_domain, runs, last_error."""
    out = []
    for s in stores:
        row = conn.execute("SELECT id FROM stores WHERE store_domain = ?", (s["store_domain"],)).fetchone()
        if row is None:
            continue
        sid = row["id"]
        ok = conn.execute("SELECT COUNT(*) FROM store_runs WHERE store_id = ? AND status = 'ok'", (sid,)).fetchone()[0]
        products = conn.execute("SELECT COUNT(*) FROM products_daily WHERE store_id = ?", (sid,)).fetchone()[0]
        ads = conn.execute("SELECT COUNT(*) FROM ads WHERE store_id = ?", (sid,)).fetchone()[0]
        if ok or products or ads:
            continue
        runs = conn.execute("SELECT COUNT(*) FROM store_runs WHERE store_id = ?", (sid,)).fetchone()[0]
        last = conn.execute("SELECT error FROM store_runs WHERE store_id = ? ORDER BY run_id DESC LIMIT 1", (sid,)).fetchone()
        out.append({"store_domain": s["store_domain"], "runs": runs, "last_error": (last["error"] if last else "") or ""})
    return out


def cmd_prune_dead(args) -> int:
    conn = db.connect(args.db)
    stores = read_watchlist(_wl(args))
    dead = dead_stores(conn, stores)
    if not dead:
        console.print("no dead stores: every watchlist domain has returned a catalogue or an ad at least once")
        return 0
    t = Table(title=f"{len(dead)} watchlist domain(s) that never returned a catalogue or an ad")
    for c in ("store", "runs tried", "last error"):
        t.add_column(c, justify="right" if c == "runs tried" else "left")
    for d in dead:
        t.add_row(d["store_domain"], str(d["runs"]), d["last_error"][:90])
    console.print(t)
    if args.apply:
        removed, _ = remove_from_watchlist([d["store_domain"] for d in dead], _wl(args))
        console.print(f"[green]removed {len(removed)} store(s) from the watchlist[/] (their rows in the database are kept)")
    else:
        console.print("dry run: add --apply to remove them from the watchlist (or remove-store <domain> for a subset)")
    return 0


def cmd_restore_stores(args) -> int:
    """Put stores that are in the database but no longer in watchlist.csv back on it (undo of prune-dead / remove-store)."""
    conn = db.connect(args.db)
    wl_path = _wl(args)
    listed = {s["store_domain"] for s in read_watchlist(wl_path)}
    rows = [dict(r) for r in conn.execute("SELECT store_domain, meta_page_name, meta_page_id, notes, platform FROM stores ORDER BY store_domain")
            if r["store_domain"] not in listed]
    if args.domains:
        want = {d.strip().lower() for d in args.domains}
        rows = [r for r in rows if r["store_domain"] in want or r["store_domain"].replace("www.", "") in want]
    if not rows:
        console.print("nothing to restore: every store in the database is on the watchlist")
        return 0
    t = Table(title=f"{len(rows)} store(s) in the database but not on the watchlist")
    for c in ("store", "meta page", "platform", "notes"):
        t.add_column(c)
    for r in rows:
        t.add_row(r["store_domain"], r["meta_page_name"] or "", r["platform"] or "", (r["notes"] or "")[:40])
    console.print(t)
    if not args.apply:
        console.print("dry run: add --apply to put them back (or name domains: restore-stores --apply x.com y.com)")
        return 0
    n = 0
    for r in rows:
        if append_to_watchlist({"store_domain": r["store_domain"], "meta_page_name": r["meta_page_name"] or "",
                                "meta_page_id": r["meta_page_id"] or "", "notes": r["notes"] or ""}, wl_path):
            n += 1
    conn.execute("UPDATE stores SET platform = NULL, platform_checked_at = NULL WHERE store_domain IN (%s)" % ",".join("?" * len(rows)),
                 [r["store_domain"] for r in rows])
    conn.commit()
    console.print(f"[green]restored {n} store(s)[/]; their platform is detected afresh on the next run "
                  "(python tracker.py run --only <domains> to do it now)")
    return 0


def _same_store(landing_domain: str | None, store_domain: str) -> bool:
    a = re.sub(r"^(https?://)?(www\.)?", "", (landing_domain or "").lower()).split("/")[0]
    b = re.sub(r"^(https?://)?(www\.)?", "", store_domain.lower()).split("/")[0]
    return bool(a) and (a == b or a.endswith("." + b))


def _landing_domain(url: str | None) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    if "://" not in u:
        u = "https://" + u
    host = urlparse(u).netloc.split("@")[-1]
    return re.sub(r"^www\.", "", host)


def page_candidates(ads: list[dict], store_domain: str) -> list[dict]:
    """Group Ad Library results by page: ads, how many land on the store, example landing domain. Store-landing pages first."""
    from collections import Counter
    by: dict[str, dict] = {}
    for a in ads:
        key = str(a.get("page_id") or a.get("page_name") or "")
        if not key:
            continue
        p = by.setdefault(key, {"page_id": a.get("page_id") or "", "page_name": a.get("page_name") or "", "ads": 0, "on_store": 0, "domains": Counter()})
        p["ads"] += 1
        dom = _landing_domain(a.get("landing_url") or a.get("landing_domain"))
        if dom:
            p["domains"][dom] += 1
        if _same_store(dom, store_domain):
            p["on_store"] += 1
    out = list(by.values())
    for p in out:
        p["top_domain"] = p["domains"].most_common(1)[0][0] if p["domains"] else ""
    out.sort(key=lambda p: (-p["on_store"], -p["ads"]))
    return out


def cmd_pages(args) -> int:
    """Which Facebook pages advertise a domain: an Ad Library keyword search for the domain (what the night pass does),
    grouped by page, pages whose ads land on the store first. --set N pins the pass to that one page (rarely wanted)."""
    from playwright.sync_api import sync_playwright
    domain = args.domain
    query = args.query or meta_ads.store_query({"store_domain": domain})
    console.print(f"searching the Ad Library for [bold]{query}[/] (active ads, all countries) ...")
    with sync_playwright() as pw:
        handle = meta_ads.BrowserHandle(pw, headless=not args.headed)
        try:
            url = meta_ads.build_search_url(query=query, country="ALL", search_type="keyword_unordered")
            res = meta_ads.scrape_page(url, max_ads=args.max_ads, browser=handle.get())
        except meta_ads.MetaBlocked as e:
            console.print(f"[red]Ad Library blocked the search:[/] {e}")
            return 3
        finally:
            handle.close()
    cands = page_candidates(res.ads, domain)
    if not cands:
        console.print("no ads found for this search. Try --query with the brand as written in the ads, or the lander's domain if the "
                      "ads go through one.")
        return 1
    t = Table(title=f"pages advertising '{query}' (pages whose ads land on {domain} first; {len(res.ads)} ads seen)")
    for c in ("#", "page name", "page_id", "ads", "land on store", "top landing domain"):
        t.add_column(c, justify="right" if c in ("#", "ads", "land on store") else "left")
    for i, p in enumerate(cands[:25], start=1):
        t.add_row(str(i), p["page_name"][:40], p["page_id"], str(p["ads"]), str(p["on_store"]), p["top_domain"][:40])
    console.print(t)
    if args.set:
        p = cands[args.set - 1]
        _set_page(domain, p["page_name"], p["page_id"], args)
    else:
        console.print("the night pass already covers every page here (it searches the domain). To pin the store to one page only: "
                      "python tracker.py pages <domain> --set N")
    return 0


def _set_page(domain: str, name: str, page_id: str | None, args) -> None:
    ok = update_watchlist_entry(domain, _wl(args), meta_page_name=name, meta_page_id=page_id or "")
    conn = db.connect(args.db)
    if page_id:
        conn.execute("UPDATE stores SET meta_page_name = ?, meta_page_id = ? WHERE store_domain = ?", (name, page_id, domain))
    else:
        conn.execute("UPDATE stores SET meta_page_name = ? WHERE store_domain = ?", (name, domain))
    conn.commit()
    if ok:
        console.print(f"[green]set[/] {domain}: meta_page_name={name!r} meta_page_id={page_id or ''!r} (watchlist.csv + database). "
                      + (f"The Meta pass now searches only that page. Next: python tracker.py ads --only {domain}" if page_id
                         else "Without a page id the Meta pass still searches the domain; the name is a label."))
    else:
        console.print(f"[yellow]{domain} is not in watchlist.csv[/]; the database row was updated. Add the store first (add-store).")


def cmd_set_page(args) -> int:
    _set_page(args.domain, args.name, args.page_id, args)
    return 0


# ---------------------------------------------------------------- reports

def cmd_report(args) -> int:
    """The Winners tab in the terminal: one row per landing URL with >= 3 delivering ads, delivering_wow desc."""
    conn = db.connect(args.db)
    as_of = _parse_date(args.date) if args.date else None
    stores = [dict(r) for r in conn.execute("SELECT id, store_domain, shop_id, store_created_est FROM stores ORDER BY store_domain")]
    if args.store:
        stores = [s for s in stores if _same_store(args.store, s["store_domain"]) or _same_store(s["store_domain"], args.store)]
    rows = winners.winners_rows(conn, stores, as_of)
    if not rows:
        console.print("no landing URL has 3 or more delivering ads yet - run `python tracker.py ads` first (the badge is read per card)")
        return 2
    t = Table(title=f"Winners: landing URLs with >= {winners.MIN_DELIVERING} delivering ads (delivering_wow desc, then delivering)")
    for c in winners.WINNERS_HEADERS:
        t.add_column(c, justify="left" if c in ("store", "landing_url", "top_page", "ads_as_of") else "right")
    for r in rows[: args.top]:
        t.add_row(_short(r[0]), r[1][:70], *[str(x) for x in r[2:]])
    console.print(t)
    console.print("delivering = active ads on the URL whose card shows no 'Low impression count' badge; 'new' = 0 delivering a week ago; "
                  "blank wow = no scrape 6-8 days earlier. family/store age only where known.")
    return 0


def cmd_url(args) -> int:
    """Time series for one landing URL (host/path, query string ignored): delivering per day + the ads behind it."""
    conn = db.connect(args.db)
    path = winners.normalise_landing_path(args.url) or args.url.lower()
    rows = conn.execute("""SELECT u.*, s.store_domain FROM url_daily u JOIN stores s ON s.id = u.store_id
                           WHERE u.landing_path = ? ORDER BY u.snapshot_date, s.store_domain""", (path,)).fetchall()
    if not rows:
        like = conn.execute("SELECT DISTINCT landing_path FROM url_daily WHERE landing_path LIKE ? ORDER BY 1 LIMIT 10", (f"%{path.split('/')[-1]}%",)).fetchall()
        console.print(f"[red]no delivering ads recorded for[/] {path}" + (": similar URLs: " + ", ".join(r[0] for r in like) if like else ""))
        return 2
    t = Table(title=f"{path}  (per scrape day)")
    for c in ("date", "store", "delivering", "7d ago", "wow", "proven_days", "pages", "pages_new_7d", "top_page", "family", "family_created"):
        t.add_column(c, justify="left" if c in ("date", "store", "top_page", "family", "family_created") else "right")
    for r in rows:
        t.add_row(r["snapshot_date"], _short(r["store_domain"]), str(r["delivering"]), "" if r["delivering_7d_ago"] is None else str(r["delivering_7d_ago"]),
                  str(winners.wow_cell(r["delivering"], r["delivering_7d_ago"])), "" if r["proven_days"] is None else str(r["proven_days"]),
                  str(r["pages"]), str(r["pages_new_7d"]), r["top_page"] or "", r["family_key"] or "", r["family_created"] or "")
    console.print(t)
    last = rows[-1]
    ads = conn.execute("""SELECT a.ad_id, a.page_name, a.first_seen, d.low_impressions, a.landing_url, a.primary_text
                          FROM ads_daily d JOIN ads a ON a.ad_id = d.ad_id AND a.store_id = d.store_id
                          WHERE d.store_id = ? AND d.snapshot_date = ? AND d.still_active = 1 AND a.landing_path = ?
                          ORDER BY d.low_impressions IS NULL, d.low_impressions, a.first_seen""", (last["store_id"], last["snapshot_date"], path)).fetchall()
    t = Table(title=f"active ads on it, {last['snapshot_date']} (badge: LOW = low impression count, no = delivering, unread = card not rendered)")
    for c in ("ad_id", "page", "first_seen", "badge", "landing_url", "primary text"):
        t.add_column(c, overflow="fold")
    for a in ads[: args.limit]:
        t.add_row(a["ad_id"], (a["page_name"] or "")[:24], a["first_seen"] or "", {1: "LOW", 0: "no"}.get(a["low_impressions"], "unread"),
                  (a["landing_url"] or "")[:60], " ".join((a["primary_text"] or "").split())[:80])
    console.print(t)
    return 0


def cmd_status(args) -> int:
    conn = db.connect(args.db)
    runs = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    t = Table(title="recent runs")
    for c in ("id", "snapshot_date", "started_at", "finished_at", "total", "ok", "failed"):
        t.add_column(c)
    for r in runs:
        t.add_row(str(r["id"]), r["snapshot_date"], r["started_at"], r["finished_at"] or "-", str(r["stores_total"]), str(r["stores_ok"]), str(r["stores_failed"]))
    console.print(t)
    t = Table(title="stores")
    for c in ("store", "ad search", "shop_id", "products", "catalogue days", "last catalogue", "ads as of", "active", "delivering", "winners", "last status"):
        t.add_column(c, justify="right" if c in ("shop_id", "products", "catalogue days", "active", "delivering", "winners") else "left")
    for s in conn.execute("SELECT id, store_domain, meta_page_id, shop_id FROM stores ORDER BY store_domain"):
        p = conn.execute("""SELECT COUNT(DISTINCT snapshot_date), MAX(snapshot_date),
                                   (SELECT COUNT(*) FROM products_daily p2 WHERE p2.store_id = p.store_id AND p2.snapshot_date = MAX(p.snapshot_date))
                            FROM products_daily p WHERE store_id = ?""", (s["id"],)).fetchone()
        snap = winners.latest_scrape(conn, s["id"])
        a = conn.execute("SELECT COUNT(*), SUM(low_impressions = 0) FROM ads_daily WHERE store_id = ? AND snapshot_date = ? AND still_active = 1",
                         (s["id"], snap)).fetchone() if snap else (0, 0)
        w = conn.execute("SELECT COUNT(*) FROM url_daily WHERE store_id = ? AND snapshot_date = ? AND delivering >= ?",
                         (s["id"], snap, winners.MIN_DELIVERING)).fetchone()[0] if snap else 0
        last = conn.execute("SELECT status, error FROM store_runs WHERE store_id = ? ORDER BY run_id DESC LIMIT 1", (s["id"],)).fetchone()
        status = "-" if last is None else (last["status"] + (": " + (last["error"] or "")[:50] if last["status"] != "ok" else ""))
        t.add_row(_short(s["store_domain"]), f"page {s['meta_page_id']}" if s["meta_page_id"] else "domain", str(s["shop_id"] or ""), str(p[2] or 0), str(p[0] or 0), p[1] or "-",
                  snap or "-", str(a[0] or 0), str(a[1] or 0), str(w), f"[green]{status}[/]" if status == "ok" else f"[red]{status}[/]")
    console.print(t)
    return 0


def cmd_diag(args) -> int:
    """Collect the diagnostics report (read-only) and write it to the Google Doc "EarlyScale Diag" via Apps Script,
    plus logs/diag.txt. The scheduled tasks run this when they finish, so the doc always shows the latest state."""
    from . import ops
    conn = db.connect(args.db)
    text = ops.collect_diag(conn, [n for n in (args.note or [])])
    out = Path(config.ROOT) / "logs" / "diag.txt"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError:
        pass
    if args.print:
        console.print(text, markup=False, highlight=False)
    if args.no_push or not config.SHEETS_WEBHOOK_URL:
        console.print(f"diag written to {out}" + ("" if config.SHEETS_WEBHOOK_URL else " (SHEETS_WEBHOOK_URL not set: not pushed)"))
        return 0
    try:
        r = ops.push_diag(shopify.make_session(), config.SHEETS_WEBHOOK_URL, text)
        console.print(f"diag pushed ({r.get('chars')} chars) -> Google Doc '{ops.DIAG_DOC}' {r.get('doc') or ''}")
    except sheets.SheetsSyncError as e:
        console.print(f"[red]diag not pushed:[/] {e} (the deployed Code.gs must know mode=diag: re-paste sheets/Code.gs, deploy a new version)")
        return 3
    return 0


def cmd_db_check(args) -> int:
    """Is data/tracker.db free? Tries a 2-second write lock, lists the database files and the programs that could be
    holding it (any python, a database viewer, OneDrive syncing the folder)."""
    path = Path(args.db) if args.db else config.DB_PATH
    console.print(f"database: {path.resolve()}")
    for f in sorted(path.parent.glob(path.name + "*")):
        st = f.stat()
        console.print(f"  {f.name:<22} {st.st_size / 1e6:8.1f} MB   modified {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}")
    if not path.exists():
        console.print("[yellow]no database yet (the first run creates it)[/]")
        return 0
    raw = sqlite3.connect(str(path), timeout=2)
    locked = False
    try:
        ver = raw.execute("PRAGMA user_version").fetchone()[0]
        mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
        console.print(f"  journal_mode={mode}  schema stamp {'current' if ver == db.schema_stamp() else 'needs the one-time update (a write; the first command after git pull does it)'}")
        try:
            raw.execute("BEGIN IMMEDIATE")
            raw.rollback()
            console.print("[green]write lock: free[/] (no other program is writing to it right now)")
        except sqlite3.OperationalError as e:
            console.print(f"[red]write lock: NOT available[/] ({e}) - another program has the database open for writing")
            locked = True
        try:
            n = raw.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
            console.print(f"read: ok ({n} stores)")
        except sqlite3.OperationalError as e:
            console.print(f"[red]read: failed ({e})[/]")
    finally:
        raw.close()
    if os.name == "nt":
        import subprocess
        try:
            me = os.getpid()
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "Get-CimInstance Win32_Process -Filter \"name='python.exe' or name='pythonw.exe' or name='py.exe' or "
                                  "name='OneDrive.exe' or name='DB Browser for SQLite.exe' or name='sqlitebrowser.exe' or name='wsl.exe'\" "
                                  f"| Where-Object {{ $_.ProcessId -ne {me} }} "
                                  "| Select-Object ProcessId, @{n='started';e={$_.CreationDate.ToString('HH:mm:ss')}}, CommandLine "
                                  "| Format-Table -AutoSize -Wrap | Out-String -Width 200"],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
            console.print("other processes that could hold it (python / OneDrive / SQLite viewers / WSL):" + ("\n" + out if out else " none running"))
            if "OneDrive.exe" in out and "desktop" in str(path.resolve()).lower():
                console.print("[yellow]OneDrive is running and the project sits on the Desktop, which OneDrive often syncs: it can hold tracker.db "
                              "while uploading. Move the project out of synced folders (e.g. C:\\tracker) or exclude the folder in OneDrive.[/]")
            if "wsl.exe" in out:
                console.print("[yellow]WSL is running: a python started inside WSL is invisible to Get-Process but still locks the file. "
                              "`wsl --shutdown` releases it.[/]")
        except Exception as e:  # noqa: BLE001
            console.print(f"(could not list processes: {e})")
    if locked:
        console.print("close any program that has the database open (a SQLite viewer, a VS Code SQLite tab), stop leftover python "
                      "processes (Stop-Process -Id <id>), then run db-check again.")
    return 1 if locked else 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tracker.py", description="Shopify early-scaling tracker: which landing URLs are gaining delivering ads")
    p.add_argument("--db", help=f"SQLite path (default {config.DB_PATH})")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-db", help="create data/tracker.db and tables")
    s.set_defaults(fn=cmd_init_db)

    s = sub.add_parser("run", help="catalogue snapshot of every watchlist store (products.json: id, handle, title, dates) + shop id")
    s.add_argument("--date", help="snapshot date YYYY-MM-DD (default today); re-running a date replaces it")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.add_argument("--only", nargs="+", metavar="DOMAIN", help="limit to these store domains")
    s.add_argument("--no-sync", action="store_true", help="skip the Google Sheets sync even if SHEETS_WEBHOOK_URL is set")
    s.add_argument("--ads", action="store_true", help="also scrape the Meta Ad Library (same as META_ADS=1 in .env)")
    s.add_argument("--no-ads", action="store_true", help="skip the Meta pass even if META_ADS=1")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("ads", help="Meta Ad Library pass: every ad of each store (page, landing URL, start date, low-impression badge)")
    s.add_argument("--date", help="snapshot date YYYY-MM-DD (default today)")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.add_argument("--only", nargs="+", metavar="DOMAIN", help="limit to these store domains")
    s.add_argument("--headed", action="store_true", help="show the browser window (debugging)")
    s.add_argument("--max-scrolls", type=int, help=f"scroll cap per page (default {config.META_MAX_SCROLLS})")
    s.add_argument("--max-minutes", type=float, help=f"wall-clock budget for this pass (default META_MAX_MINUTES={config.META_MAX_MINUTES:.0f})")
    s.set_defaults(fn=cmd_ads)

    s = sub.add_parser("rebuild", help="recompute the per-URL daily numbers (delivering, wow, pages, families) from the stored ads; no scraping")
    s.set_defaults(fn=cmd_rebuild)

    s = sub.add_parser("sync-sheets", help="rewrite the Winners and Stores tabs from the database, then verify the row counts")
    s.add_argument("--date", help="as of this date (default: latest)")
    s.add_argument("--tabs", help="comma list from winners,stores (default both)")
    s.add_argument("--dry-run", action="store_true", help="build and size the chunks but send nothing")
    s.add_argument("--watchlist", help="alternate watchlist.csv (only its stores are synced)")
    s.add_argument("--verify-only", action="store_true", help="send nothing; compare the live sheet's row counts with the DB")
    s.add_argument("--no-verify", action="store_true", help="skip the read-back comparison after syncing")
    s.set_defaults(fn=cmd_sync_sheets)

    s = sub.add_parser("report", help="the Winners tab in the terminal")
    s.add_argument("--date", help="as of this date (default: latest)")
    s.add_argument("--store", help="one store domain")
    s.add_argument("--top", type=int, default=60)
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("url", help="time series for one landing URL and the ads behind it")
    s.add_argument("url")
    s.add_argument("--limit", type=int, default=40, help="max ad rows")
    s.set_defaults(fn=cmd_url)

    s = sub.add_parser("status", help="what is in the database, per store")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("diag", help="write the diagnostics report to the Google Doc 'EarlyScale Diag' (and logs/diag.txt); read-only")
    s.add_argument("--no-push", action="store_true", help="write logs/diag.txt only")
    s.add_argument("--print", action="store_true")
    s.add_argument("--note", action="append", help="a line to include at the top (e.g. what just ran)")
    s.set_defaults(fn=cmd_diag)

    s = sub.add_parser("db-check", help="is data/tracker.db free to write? lists its files and the programs that could be holding it")
    s.set_defaults(fn=cmd_db_check)

    s = sub.add_parser("add-store", help="add one or more stores to watchlist.csv (no Facebook page needed: the Meta pass searches the domain)")
    s.add_argument("domains", nargs="*", metavar="DOMAIN")
    s.add_argument("--file", help="a text file with one domain per line (a CSV's first column works too)")
    s.add_argument("--page-id", help="single domain only: pin the Meta pass to this one Facebook page instead of the domain search")
    s.add_argument("--notes")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.set_defaults(fn=cmd_add_store)

    s = sub.add_parser("remove-store", help="remove one or more domains from watchlist.csv")
    s.add_argument("domains", nargs="+")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.set_defaults(fn=cmd_remove_store)

    s = sub.add_parser("prune-dead", help="list (and with --apply remove) watchlist domains that never returned a catalogue or an ad")
    s.add_argument("--apply", action="store_true", help="remove them from watchlist.csv (history in the DB is kept)")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_prune_dead)

    s = sub.add_parser("restore-stores", help="put stores that are in the database but not on the watchlist back (undo prune-dead / remove-store)")
    s.add_argument("domains", nargs="*", help="only these (default: every store missing from the watchlist)")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_restore_stores)

    s = sub.add_parser("pages", aliases=["find-page"], help="which Facebook pages advertise a domain (the Ad Library search the night pass runs)")
    s.add_argument("domain")
    s.add_argument("--query", help="search this instead of the domain")
    s.add_argument("--set", type=int, metavar="N", help="pin the store to page N (the Meta pass then searches only that page)")
    s.add_argument("--max-ads", type=int, default=300)
    s.add_argument("--headed", action="store_true")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_pages)

    s = sub.add_parser("set-page", help="pin a store to one Facebook page by hand (--page-id); without an id the name is only a label")
    s.add_argument("domain")
    s.add_argument("--name", required=True)
    s.add_argument("--page-id")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_set_page)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/] - everything recorded so far is in the database; the next run continues from there")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)      # skip interpreter shutdown: worker threads mid-request and Playwright's loop would otherwise hang or spew
