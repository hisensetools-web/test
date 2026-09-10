"""Command-line interface. Build step 1: init-db, add-store, run (products only), status."""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import ad_detail, ad_metrics, ad_rank, config, db, deltas, fb_posts, inventory, meta_ads, platforms, radar, scaling, sheets, shopify, store_age
from .watchlist import append_to_watchlist, read_watchlist, remove_from_watchlist, update_watchlist_entry

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
            cat = platforms.fetch_catalogue(domain, session, conn, store_id, snapshot_date)
            products, pages = cat.products, cat.pages
            n = db.write_product_snapshot(conn, store_id, snapshot_date, products)
            dur = time.monotonic() - t0
            db.record_store_run(conn, run_id, store_id, snapshot_date, "ok", None, n, pages, dur)
            sold_out = sum(p["sold_out_variants"] for p in products)
            log.info("%-28s ok  %-16s products=%-5d variants=%-5d sold_out_variants=%-4d pages=%d  %.1fs%s",
                     domain, cat.platform, n, sum(p["variant_count"] for p in products), sold_out, pages, dur,
                     f"  ({cat.note})" if cat.note and cat.platform != "shopify" else "")
            ok += 1
            if cat.platform in ("shopify", "shopify_headless"):
                try:   # shop id, once per store, quietly: Radar's store-age calibration uses it
                    store_age.ensure_identity(conn, store_id, cat.extra.get("myshopify") or domain, session)
                except Exception as e:  # noqa: BLE001
                    log.debug("%s shop id lookup failed: %s", domain, e)
            try:   # a platform that publishes stock counts gives the inventory pass its readings for free
                got = inventory.record_platform_stock(conn, store_id, snapshot_date, products)
                if got:
                    log.info("%-28s stock from the platform for %d hero variant(s)", domain, got)
            except Exception as e:  # noqa: BLE001
                log.debug("%s platform stock failed: %s", domain, e)
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
    awake = meta_ads.KeepAwake().__enter__()
    ok, failed = run_products_pass(conn, stores, snapshot_date, only)
    console.print(f"done in {time.monotonic() - t0:.1f}s: [green]{ok} ok[/], [red]{failed} failed[/]")
    rc = 1 if ok == 0 and failed else 0
    if not args.no_ads and conn.execute("SELECT 1 FROM fb_posts LIMIT 1").fetchone():
        console.print("feed-post engagement pass (captured permalinks, logged-out) ...")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **meta_ads.launch_kwargs())
                try:
                    c = fb_posts.refresh_engagement(conn, browser, snapshot_date)
                finally:
                    browser.close()
            console.print(f"posts: fetched {c['fetched']}, ok={c['ok']} gated={c['gated']} removed={c['removed']}")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]post engagement pass failed:[/] {e}")
    inv_stores = inventory_targets(stores, only, args.inventory, conn=conn)
    if inv_stores and not args.no_inventory:
        console.print(f"inventory probe pass ({len(inv_stores)} store(s), waits={config.INVENTORY_WAIT_MIN:.0f}-{config.INVENTORY_WAIT_MAX:.0f}s) ...")
        try:
            run_inventory_pass(conn, inv_stores, snapshot_date)
        except Exception as e:  # noqa: BLE001 - never let the probe break the daily run
            console.print(f"[red]inventory pass failed:[/] {e}")
    if (config.META_ADS_ENABLED or args.ads) and not args.no_ads:
        planned = plan_meta_stores(conn, stores, only)
        console.print(f"Meta Ad Library pass (META_ADS=1): {len(planned)} store(s)"
                      + (f" from META_STORES" if config.META_STORES else " (all; set META_STORES=a.com,b.com to narrow)")
                      + f", budget {config.META_MAX_MINUTES:.0f} min, least recently scraped first ...")
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
    awake.__exit__(None, None, None)
    return rc


def _do_sheets_sync(conn, tabs=sheets.TAB_ORDER, as_of=None, dry_run=False, verify=True) -> int:
    try:
        summaries = sheets.sync(conn, config.SHEETS_WEBHOOK_URL, tabs=tabs, as_of=as_of, dry_run=dry_run)
    except sheets.SheetsSyncError as e:
        console.print(f"[red]sheets sync failed:[/] {e}")
        return 3
    rc = _print_sync_summary(summaries, dry_run)
    if verify and not dry_run:
        rc = _verify_sheets(conn, as_of) or rc
    return rc


def _verify_sheets(conn, as_of=None) -> int:
    """Read row counts back from the live sheet and compare with the DB. Returns 3 on a mismatch."""
    try:
        v = sheets.verify(conn, config.SHEETS_WEBHOOK_URL, as_of)
    except sheets.SheetsSyncError as e:
        console.print(f"[yellow]could not verify the sheet:[/] {e}")
        return 0
    t = Table(title="sheet vs database")
    for c in ("tab", "mode", "rows in DB", "rows in sheet", "ok"):
        t.add_column(c, justify="right" if "rows" in c else "left")
    for x in v["tabs"]:
        t.add_row(x["tab"], x["mode"], str(x["expected"]), "?" if x["sheet"] is None else str(x["sheet"]), "yes" if x["ok"] else "[red]NO[/]")
    console.print(t)
    bad = [p for p in v["products"] if not p["ok"]]
    if bad:
        t = Table(title="Products rows per store that differ")
        for c in ("store", "rows in DB", "rows in sheet"):
            t.add_column(c, justify="left" if c == "store" else "right")
        for p in bad[:60]:
            t.add_row(_short(p["store"]), str(p["expected"]), str(p["sheet"]))
        console.print(t)
    if v["problems"]:
        console.print(f"[red]{len(v['problems'])} mismatch(es)[/] - the sheet does not hold what the DB holds (see above)")
        return 3
    console.print("[green]sheet matches the database[/]" + (f" ({len(v['products'])} stores' catalogues complete)" if v["products"] else ""))
    return 0


def _print_sync_summary(summaries, dry_run) -> int:
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
    if args.watchlist:
        sheets.set_watchlist_path(Path(args.watchlist))
    tabs = tuple(t.strip().lower() for t in args.tabs.split(",")) if args.tabs else sheets.TAB_ORDER
    bad = [t for t in tabs if t not in sheets.TAB_ORDER]
    if bad:
        console.print(f"[red]unknown tab(s):[/] {', '.join(bad)} (choose from {', '.join(sheets.TAB_ORDER)})")
        return 2
    as_of = _parse_date(args.date) if args.date else None
    if args.verify_only:
        return _verify_sheets(conn, as_of)
    return _do_sheets_sync(conn, tabs=tabs, as_of=as_of, dry_run=args.dry_run, verify=not args.no_verify)


def cmd_remove_store(args) -> int:
    removed, missing = remove_from_watchlist(args.domains, Path(args.watchlist) if args.watchlist else None)
    for d in removed:
        console.print(f"[green]removed[/] {d}")
    for d in missing:
        console.print(f"[yellow]not in watchlist[/] {d}")
    console.print(f"{len(removed)} removed, {len(missing)} not found. Snapshot history in the DB is kept.")
    return 0 if removed or not missing else 1


def dead_stores(conn, stores: list[dict]) -> list[dict]:
    """Watchlist stores that never returned a Shopify catalogue (no successful run) and never had an ad recorded:
    non-Shopify domains, typos, dead sites. Each entry: store_domain, runs, last_error."""
    out = []
    for s in stores:
        row = conn.execute("SELECT id FROM stores WHERE store_domain = ?", (s["store_domain"],)).fetchone()
        if row is None:
            continue
        sid = row["id"]
        ok = conn.execute("SELECT COUNT(*) FROM store_runs WHERE store_id = ? AND status = 'ok'", (sid,)).fetchone()[0]
        products = conn.execute("SELECT COUNT(*) FROM products_daily WHERE store_id = ?", (sid,)).fetchone()[0]
        ads = conn.execute("SELECT COUNT(*) FROM meta_ads WHERE store_id = ?", (sid,)).fetchone()[0]
        if ok or products or ads:
            continue
        runs = conn.execute("SELECT COUNT(*) FROM store_runs WHERE store_id = ?", (sid,)).fetchone()[0]
        last = conn.execute("SELECT error FROM store_runs WHERE store_id = ? ORDER BY run_id DESC LIMIT 1", (sid,)).fetchone()
        out.append({"store_domain": s["store_domain"], "runs": runs, "last_error": (last["error"] if last else "") or ""})
    return out


def cmd_db_check(args) -> int:
    """Is data/tracker.db free? Tries a 2-second write lock, lists the database files and the programs that could be
    holding it (any python, a database viewer, OneDrive syncing the folder)."""
    import os
    import sqlite3
    path = Path(args.db) if args.db else config.DB_PATH
    console.print(f"database: {path.resolve()}")
    for f in sorted(path.parent.glob(path.name + "*")):
        st = f.stat()
        console.print(f"  {f.name:<22} {st.st_size / 1e6:8.1f} MB   modified {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M:%S}")
    if not path.exists():
        console.print("[yellow]no database yet (the first run creates it)[/]")
        return 0
    raw = sqlite3.connect(str(path), timeout=2)
    try:
        ver = raw.execute("PRAGMA user_version").fetchone()[0]
        mode = raw.execute("PRAGMA journal_mode").fetchone()[0]
        console.print(f"  journal_mode={mode}  schema stamp {'current' if ver == db.schema_stamp() else 'needs the one-time update (a write)'}")
        try:
            raw.execute("BEGIN IMMEDIATE")
            raw.rollback()
            console.print("[green]write lock: free[/] (no other program is writing to it right now)")
            locked = False
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
            console.print("other processes that could hold it (python / OneDrive / SQLite viewers / WSL):"
                          + ("\n" + out if out else " none running"))
            if "OneDrive.exe" in out and "desktop" in str(path.resolve()).lower():
                console.print("[yellow]OneDrive is running and the project sits on the Desktop, which OneDrive often syncs: it can hold tracker.db "
                              "while uploading. Move the project out of synced folders (e.g. C:\\tracker) or exclude the folder in OneDrive.[/]")
            if "wsl.exe" in out:
                console.print("[yellow]WSL is running: a python started inside WSL (e.g. by a Claude Code session there) is invisible to "
                              "Get-Process but still locks the file. `wsl --shutdown` releases it.[/]")
        except Exception as e:  # noqa: BLE001
            console.print(f"(could not list processes: {e})")
    if locked:
        console.print("close any program that has the database open (a SQLite viewer, a VS Code SQLite tab), stop leftover python "
                      "processes (Stop-Process -Id <id>), then run db-check again.")
    return 1 if locked else 0


def cmd_prune_dead(args) -> int:
    conn = db.connect(args.db)
    stores = read_watchlist(Path(args.watchlist) if args.watchlist else None)
    dead = dead_stores(conn, stores)
    if not dead:
        console.print("no dead stores: every watchlist domain has returned a catalogue or an ad at least once")
        return 0
    t = Table(title=f"{len(dead)} watchlist domain(s) that never returned Shopify or Meta data")
    for c in ("store", "runs tried", "last error"):
        t.add_column(c, justify="right" if c == "runs tried" else "left")
    for d in dead:
        t.add_row(d["store_domain"], str(d["runs"]), d["last_error"][:90])
    console.print(t)
    if args.apply:
        removed, _ = remove_from_watchlist([d["store_domain"] for d in dead], Path(args.watchlist) if args.watchlist else None)
        console.print(f"[green]removed {len(removed)} store(s) from the watchlist[/] (their rows in the database are kept)")
    else:
        console.print("dry run: add --apply to remove them from the watchlist (or remove-store <domain> for a subset)")
    return 0


def cmd_restore_stores(args) -> int:
    """Put stores that are in the database but no longer in watchlist.csv back on it (undo of prune-dead / remove-store)."""
    conn = db.connect(args.db)
    wl_path = Path(args.watchlist) if args.watchlist else None
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


def brand_query(domain: str) -> str:
    """'tryhappyharvest.com' -> 'happyharvest': the label without shop/try/get prefixes, for an Ad Library search."""
    label = re.sub(r"^https?://", "", domain).split("/")[0].lower()
    label = re.sub(r"^(www|shop|store)\.", "", label).split(".")[0]
    for pre in ("try", "get", "shop", "buy", "the", "my"):
        if label.startswith(pre) and len(label) > len(pre) + 3:
            label = label[len(pre):]
            break
    return label.replace("-", " ").strip()


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
        dom = radar.normalise_landing_domain(a.get("landing_url") or a.get("landing_domain")) or ""
        if dom:
            p["domains"][dom] += 1
        if ad_metrics.same_store(dom, re.sub(r"^https?://", "", store_domain)):
            p["on_store"] += 1
    out = list(by.values())
    for p in out:
        p["top_domain"] = p["domains"].most_common(1)[0][0] if p["domains"] else ""
    out.sort(key=lambda p: (-p["on_store"], -p["ads"]))
    return out


def cmd_find_page(args) -> int:
    """Find the Facebook page behind a store: footer link first, then an Ad Library search for the brand name."""
    from playwright.sync_api import sync_playwright
    domain = args.domain
    console.print(f"looking for a Facebook page link on {domain} ...")
    footer = shopify.discover_meta_page(domain)
    console.print(f"  footer link: [bold]{footer or '(none)'}[/]")
    query = args.query or brand_query(domain)
    console.print(f"searching the Ad Library for [bold]{query}[/] (active ads, all countries) ...")
    with sync_playwright() as pw:
        handle = meta_ads.BrowserHandle(pw, headless=not args.headed)
        try:
            url = meta_ads.build_search_url(query=query, country="ALL", search_type="keyword_unordered")
            res = meta_ads.scrape_page(url, max_ads=args.max_ads, browser=handle.get())
        finally:
            handle.close()
    if res.blocked:
        console.print(f"[red]Ad Library blocked the search:[/] {res.note}")
        return 3
    cands = page_candidates(res.ads, domain)
    if not cands:
        console.print("no pages found. Try --query with the brand as written on the site (e.g. --query \"Happy Harvest\"), "
                      "or set it by hand: python tracker.py set-page <domain> --name \"Page Name\"")
        return 1
    t = Table(title=f"pages advertising for '{query}' (pages whose ads land on {domain} first)")
    for c in ("#", "page name", "page_id", "ads", "land on store", "top landing domain"):
        t.add_column(c, justify="right" if c in ("#", "ads", "land on store") else "left")
    for i, p in enumerate(cands[:15], start=1):
        t.add_row(str(i), p["page_name"][:40], p["page_id"], str(p["ads"]), str(p["on_store"]), p["top_domain"][:40])
    console.print(t)
    pick = args.set
    if pick is None and cands[0]["on_store"] > 0 and args.auto:
        pick = 1
    if pick:
        p = cands[pick - 1]
        _set_page(domain, p["page_name"], p["page_id"], args)
    else:
        console.print("pick one with:  python tracker.py find-page <domain> --set N   (or set-page <domain> --name \"...\" --page-id ...)")
    return 0


def _set_page(domain: str, name: str, page_id: str | None, args) -> None:
    ok = update_watchlist_entry(domain, Path(args.watchlist) if args.watchlist else None, meta_page_name=name, meta_page_id=page_id or "")
    conn = db.connect(args.db)
    if page_id:
        conn.execute("UPDATE stores SET meta_page_name = ?, meta_page_id = ? WHERE store_domain = ?", (name, page_id, domain))
    else:
        conn.execute("UPDATE stores SET meta_page_name = ? WHERE store_domain = ?", (name, domain))
    conn.commit()
    if ok:
        console.print(f"[green]set[/] {domain}: meta_page_name={name!r} meta_page_id={page_id or ''!r} (watchlist.csv + database). "
                      f"Next: python tracker.py ads --only {domain}")
    else:
        console.print(f"[yellow]{domain} is not in watchlist.csv[/]; the database row was updated. Add the store first (add-store).")


def cmd_set_page(args) -> int:
    _set_page(args.domain, args.name, args.page_id, args)
    return 0


def plan_meta_stores(conn, stores: list[dict], only: set[str] | None = None, scope: list[str] | None = None) -> list[dict]:
    """Stores for today's Meta pass: --only, else META_STORES if set, else all; least recently
    scraped first (never scraped first) so a watchlist bigger than the daily budget rotates."""
    if only:
        stores = [s for s in stores if s["store_domain"] in only]
    else:
        scope = config.META_STORES if scope is None else scope
        if scope:
            want = {x.lower() for x in scope}
            stores = [s for s in stores if s["store_domain"].lower() in want
                      or re.sub(r"^https?://", "", s["store_domain"].lower()) in want]
    last: dict[str, str] = {}
    for r in conn.execute("""SELECT s.store_domain, MAX(r.snapshot_date) AS d FROM meta_page_runs r JOIN stores s ON s.id = r.store_id
                             WHERE r.status = 'ok' GROUP BY s.store_domain"""):
        last[r["store_domain"]] = r["d"]
    # stores with a product catalogue first: without one, ads cannot attach to products and the
    # pass only feeds noise (the 404 domains had hundreds of ads and dozens of alerts, 0 resolved)
    has_products = {r[0] for r in conn.execute("SELECT DISTINCT s.store_domain FROM products_daily p JOIN stores s ON s.id = p.store_id")}
    return sorted(stores, key=lambda s: (0 if s["store_domain"] in has_products else 1, last.get(s["store_domain"]) or "", s["store_domain"]))


def run_ads_pass(conn, stores: list[dict], snapshot_date: str, only: set[str] | None = None,
                 headless: bool = True, max_scrolls: int | None = None, detail: bool = True,
                 detail_cap: int | None = None, max_minutes: float | None = None) -> tuple[int, int]:
    """Scrape the Ad Library for the planned stores (one browser, sequential) within a time budget.
    Returns (ok, failed). A blocked or failing page is logged in meta_page_runs and never aborts the pass."""
    from playwright.sync_api import sync_playwright
    stores = plan_meta_stores(conn, stores, only)
    budget = (config.META_MAX_MINUTES if max_minutes is None else max_minutes) * 60
    deadline = time.monotonic() + budget
    ok = failed = 0
    session = shopify.make_session()   # for landing-page fetches
    with sync_playwright() as p, meta_ads.KeepAwake():
        handle = meta_ads.BrowserHandle(p, headless=headless)
        try:
            for i, s in enumerate(stores):
                if time.monotonic() > deadline:
                    left = [x["store_domain"] for x in stores[i:]]
                    log.warning("Meta pass budget of %.0f min spent: %d store(s) deferred to the next run (%s%s)",
                                budget / 60, len(left), ", ".join(left[:5]), ", ..." if len(left) > 5 else "")
                    break
                browser = handle.get()   # relaunched if the previous store killed it
                domain = s["store_domain"]
                store_id = db.upsert_store(conn, domain, s.get("meta_page_name"), s.get("meta_page_id"), s.get("notes"))
                conn.commit()
                query = s.get("meta_page_name") or domain.split("//")[-1]
                url = meta_ads.build_search_url(query=None if s.get("meta_page_id") else query,
                                                page_id=s.get("meta_page_id") or None)
                t0 = time.monotonic()
                try:
                    res = meta_ads.scrape_page(url, headless=headless, max_scrolls=max_scrolls, browser=browser)
                    counts = meta_ads.record_scrape(conn, store_id, snapshot_date, res.ads, query)
                    meta_ads.record_page_run(conn, store_id, snapshot_date, query, "ok", res.note, len(res.ads),
                                             res.scrolls, time.monotonic() - t0)
                    log.info("%-28s ads=%-4d new=%-4d disappeared=%-3d scrolls=%d responses=%d %.0fs  %s",
                             domain, counts["total"], counts["new"], counts["disappeared"], res.scrolls,
                             res.responses, time.monotonic() - t0, res.note)
                    ok += 1
                    try:
                        ad_detail.record_list_readings(conn, store_id, snapshot_date, res.ads)
                    except Exception as e:  # noqa: BLE001
                        log.warning("%-28s list readings failed: %s", domain, e)
                    if config.META_RANK and _rank_due(conn, store_id, snapshot_date):
                        try:
                            rk = ad_rank.scrape_ranks(conn, handle.get(), s, store_id, snapshot_date, [a["ad_id"] for a in res.ads])
                            log.info("%-28s impression rank: %s", domain, rk["summary"])
                        except meta_ads.MetaBlocked as e:
                            log.error("%-28s impression-rank search blocked: %s", domain, e)
                        except Exception as e:  # noqa: BLE001
                            log.warning("%-28s impression-rank search failed: %s", domain, e)
                    _post_process_store(conn, store_id, domain, snapshot_date, session)
                    if detail:
                        _detail_pass_store(conn, handle, store_id, domain, snapshot_date, detail_cap)
                    try:
                        pe = meta_ads.fetch_boosted_engagement(conn, handle.get(), store_id, snapshot_date)
                        if pe["candidates"]:
                            log.info("%-28s boosted posts: %d candidates, fetched %d, counts found %d",
                                     domain, pe["candidates"], pe["fetched"], pe["with_counts"])
                            for a in conn.execute("SELECT DISTINCT ad_id FROM meta_ads_daily WHERE store_id = ? AND snapshot_date = ?",
                                                  (store_id, snapshot_date)):
                                ad_metrics.compute_reach_metrics(conn, a["ad_id"], snapshot_date)
                            conn.commit()
                    except Exception as e:  # noqa: BLE001
                        log.warning("%-28s boosted post fetch failed: %s", domain, e)
                except meta_ads.MetaBlocked as e:
                    meta_ads.record_page_run(conn, store_id, snapshot_date, query, "blocked", str(e), 0, 0,
                                             time.monotonic() - t0)
                    log.error("%-28s BLOCKED by Meta: %s (stopping this pass; try again later)", domain, e)
                    failed += 1
                    break
                except Exception as e:  # noqa: BLE001 - by design
                    meta_ads.record_page_run(conn, store_id, snapshot_date, query, "error", str(e), 0, 0,
                                             time.monotonic() - t0)
                    log.error("%-28s FAILED: %s", domain, e)
                    failed += 1
                took = time.monotonic() - t0
                if took > 25 * 60:
                    log.warning("%-28s took %.0f min for one store; the machine probably slept part of the way (the budget clock kept running)",
                                domain, took / 60)
                if i < len(stores) - 1:
                    meta_ads._wait()
        finally:
            handle.close()
    return ok, failed


def _rank_due(conn, store_id: int, today: str) -> bool:
    """Daily while the sort is informative or unknown; weekly re-check for a store where it was not. A store judged
    before the per-view comparison existed (no note on its last rank_checks row) is checked again right away."""
    r = conn.execute("SELECT sort_informative, rank_checked_at FROM stores WHERE id = ?", (store_id,)).fetchone()
    if not r or r["sort_informative"] is None or r["sort_informative"]:
        return True
    chk = conn.execute("SELECT note FROM rank_checks WHERE store_id = ? ORDER BY snapshot_date DESC LIMIT 1", (store_id,)).fetchone()
    if chk is None or not chk["note"]:
        return True
    try:
        return (date.fromisoformat(today) - date.fromisoformat(r["rank_checked_at"])).days >= 7
    except (TypeError, ValueError):
        return True


def _post_process_store(conn, store_id: int, domain: str, snapshot_date: str, session, fetch_landings: bool = True) -> dict:
    """Landing join, concepts, lineage, per-day metrics and alerts for one store. Never raises."""
    try:
        m = ad_metrics.process_store(conn, store_id, domain, snapshot_date, session, fetch_landings)
        alerts = ad_metrics.run_alerts(conn, store_id, domain, snapshot_date)
        alerts += scaling.run_alerts(conn, store_id, domain, snapshot_date)
        alerts += ad_rank.run_alerts(conn, store_id, domain, snapshot_date)
        log.info("%-28s metrics: resolved=%s/%s unlisted_products=%s concepts=%s lineage=%s pages_fetched=%s alerts=%d",
                 domain, m.get("resolved", 0), m.get("ads", 0), m.get("unlisted", 0), m.get("concepts", 0),
                 m.get("lineage", 0), m.get("pages_fetched", 0), len(alerts))
        return m
    except Exception as e:  # noqa: BLE001
        log.error("%-28s metrics FAILED: %s", domain, e)
        return {}


def inventory_targets(stores: list[dict], only: set[str] | None, force_all: bool = False, conn=None) -> list[dict]:
    """Stores to probe: INVENTORY_STORES from .env (or every store when INVENTORY=1 / --inventory), plus any store
    whose latest products.json snapshot shows a variant with inventory_management='shopify' (stock is tracked,
    so the rung-1 cart probe can read it: straight into the pool)."""
    wanted = None if (force_all or config.INVENTORY_ALL) else set(config.INVENTORY_STORES)
    managed = set()
    if conn is not None and wanted is not None:
        managed = {r[0] for r in conn.execute("""SELECT DISTINCT s.store_domain FROM variants_daily v JOIN stores s ON s.id = v.store_id
                                                  WHERE v.inventory_management = 'shopify'
                                                  AND v.snapshot_date = (SELECT MAX(snapshot_date) FROM variants_daily v2 WHERE v2.store_id = v.store_id)""")}
    out = []
    for s in stores:
        d = s["store_domain"]
        if only and d not in only:
            continue
        bare = re.sub(r"^www\.", "", d.split("//")[-1].lower())
        if wanted is None or d.lower() in wanted or bare in wanted or f"www.{bare}" in wanted or d in managed:
            out.append(s)
    return out


def run_inventory_pass(conn, stores: list[dict], snapshot_date: str) -> list[dict]:
    """Probe hero variants of each store, compute sales and alerts. A failing store never aborts the pass."""
    results = []
    for s in stores:
        domain = s["store_domain"]
        store_id = db.upsert_store(conn, domain, s.get("meta_page_name"), s.get("meta_page_id"), s.get("notes"))
        conn.commit()
        t0 = time.monotonic()
        plat = (conn.execute("SELECT platform FROM stores WHERE id = ?", (store_id,)).fetchone() or [None])[0]
        if plat and plat not in ("shopify", "shopify_headless"):
            log.info("%-28s %s store: no cart probe (stock comes from the platform's own JSON when it publishes any)", domain, plat)
            results.append({"store": domain, "ok": True, "cart_probe": 0, "theme_inventory": 0, "ads_only": 0, "blocked": 0,
                            "components": 0, "skipped": 0, "alerts": len(inventory.run_alerts(conn, store_id, domain, snapshot_date))})
            continue
        try:
            counts = inventory.probe_store(conn, store_id, domain, snapshot_date)
            alerts = inventory.run_alerts(conn, store_id, domain, snapshot_date)
            log.info("%-28s heroes: cart_probe=%d theme=%d ads_only=%d blocked=%d components=%d skipped=%d alerts=%d %.0fs",
                     domain, counts["cart_probe"], counts["theme_inventory"], counts["ads_only"], counts["blocked"],
                     counts["components"], counts["skipped"], len(alerts), time.monotonic() - t0)
            results.append({"store": domain, "ok": True, **counts, "alerts": len(alerts)})
        except Exception as e:  # noqa: BLE001 - by design
            log.error("%-28s inventory FAILED: %s", domain, e)
            results.append({"store": domain, "ok": False, "error": str(e)})
    md = ad_metrics.write_alerts_markdown(conn, snapshot_date)
    if md:
        log.info("alerts written to %s", md)
    return results


def cmd_inventory(args) -> int:
    snapshot_date = _parse_date(args.date)
    stores = read_watchlist(Path(args.watchlist) if args.watchlist else None)
    if not stores:
        console.print("[red]watchlist is empty[/]")
        return 2
    conn = db.connect(args.db)
    only = set(args.only) if args.only else None
    targets = inventory_targets(stores, only, force_all=bool(only) or args.all, conn=conn)
    if not targets:
        console.print("[red]no stores selected.[/] Set INVENTORY_STORES=a.com,b.com in .env, or pass --only a.com b.com")
        return 2
    missing = [s["store_domain"] for s in targets if inventory.latest_products(conn, db.upsert_store(conn, s["store_domain"]))[0] is None]
    if missing:
        console.print(f"[yellow]no products snapshot yet for:[/] {', '.join(missing)} - run `python tracker.py run --only ...` first")
    console.print(f"inventory: snapshot_date={snapshot_date} stores={len(targets)} waits={config.INVENTORY_WAIT_MIN:.0f}-{config.INVENTORY_WAIT_MAX:.0f}s "
                  f"max_variants={config.INVENTORY_MAX_VARIANTS}/store")
    t0 = time.monotonic()
    with meta_ads.KeepAwake():
        results = run_inventory_pass(conn, targets, snapshot_date)
    t = Table(title="hero variants by fallback rung")
    for c in ("store", "cart_probe", "theme_inventory", "ads_only", "blocked", "components", "skipped", "alerts"):
        t.add_column(c, justify="left" if c == "store" else "right")
    for r in results:
        if r["ok"]:
            t.add_row(r["store"], *[str(r[c]) for c in ("cart_probe", "theme_inventory", "ads_only", "blocked", "components", "skipped", "alerts")])
        else:
            t.add_row(r["store"], f"[red]FAILED: {r['error'][:60]}[/]", "", "", "", "", "", "")
    console.print(t)
    console.print(f"done in {time.monotonic() - t0:.0f}s. Readings: `python tracker.py inventory-report`")
    return 0 if any(r["ok"] for r in results) else 1


def _short(domain: str) -> str:
    return re.sub(r"^https?://", "", domain or "")


def cmd_inventory_probe(args) -> int:
    """One live probe with everything printed: the thing to paste when readings come back blocked."""
    import requests as _rq
    conn = db.connect(args.db)
    domain = args.store
    base = shopify.base_url(domain)
    try:
        base = shopify.resolve_base_url(shopify.make_session(), domain)
    except shopify.StoreFetchError as e:
        console.print(f"[yellow]host resolution failed:[/] {e}")
    console.print(f"base url: {base}")
    vid, handle = args.variant, args.handle
    if not vid:
        sid = db.upsert_store(conn, domain)
        row = conn.execute("""SELECT variant_id, handle FROM hero_variants WHERE store_id = ? ORDER BY role = 'rank', handle LIMIT 1""",
                           (sid,)).fetchone()
        if row is None:
            _, products = inventory.latest_products(conn, sid)
            heroes = inventory.select_heroes(products, date.today())
            if not heroes:
                console.print("[red]no hero variant known for this store[/] - run `python tracker.py run --only <store>` first, or pass --variant ID")
                return 2
            vid, handle = heroes[0]["variant_id"], heroes[0]["handle"]
        else:
            vid, handle = row["variant_id"], row["handle"]
    if not handle:
        r = conn.execute("SELECT p.handle FROM variants_daily v JOIN products_daily p ON p.store_id = v.store_id AND p.snapshot_date = v.snapshot_date "
                         "AND p.product_id = v.product_id WHERE v.variant_id = ? ORDER BY v.snapshot_date DESC LIMIT 1", (vid,)).fetchone()
        handle = r["handle"] if r else None
    console.print(f"variant: {vid}  handle: {handle}")
    sess = inventory.fresh_session(base)
    if handle:
        try:
            r0 = sess.get(f"{base}/products/{handle}", timeout=config.REQUEST_TIMEOUT,
                          headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "X-Requested-With": None})
            console.print(f"GET /products/{handle} -> HTTP {r0.status_code}, {len(r0.text)} bytes, cookies now: {sorted(sess.cookies.keys())[:8]}")
            q = inventory.theme_inventory_from_html(r0.text, vid)
            console.print(f"theme inventory_quantity for {vid}: {q}")
        except _rq.RequestException as e:
            console.print(f"[red]product page failed:[/] {e}")
    payload = {"items": [{"id": int(vid), "quantity": config.INVENTORY_PROBE_QTY}]}
    try:
        r = sess.post(f"{base}/cart/add.js", json=payload, timeout=config.REQUEST_TIMEOUT, allow_redirects=False)
    except _rq.RequestException as e:
        console.print(f"[red]POST /cart/add.js failed:[/] {type(e).__name__}: {e}")
        return 1
    console.print(f"POST /cart/add.js -> HTTP {r.status_code}")
    for k in ("server", "content-type", "location", "cf-mitigated", "cf-ray", "x-shopify-stage", "x-sorting-hat-shopid",
              "x-request-id", "retry-after", "set-cookie"):
        if r.headers.get(k):
            console.print(f"  {k}: {r.headers[k][:160]}")
    body = r.text or ""
    console.print(f"body ({len(body)} bytes): {inventory._squash(body)[:600]}")
    console.print(f"classified as: {inventory.probe_variant(base, vid, handle, session=sess, warm_up=False)['status']}")
    if r.status_code == 200:
        try:
            sess.post(f"{base}/cart/clear.js", timeout=config.REQUEST_TIMEOUT)
        except _rq.RequestException:
            pass
    return 0


def cmd_inventory_report(args) -> int:
    conn = db.connect(args.db)
    as_of = _parse_date(args.date) if args.date else conn.execute("SELECT MAX(snapshot_date) FROM inventory_daily").fetchone()[0]
    if not as_of:
        console.print("no inventory readings yet - run `python tracker.py inventory`")
        return 1
    days = args.days
    where, params = "", []
    if args.store:
        where, params = " AND s.store_domain = ?", [args.store]
    # 1. per-store rung counts (latest state of each hero variant)
    t = Table(title=f"fallback chain per store (hero variants, as of {as_of})")
    for c in ("store", "heroes", "cart_probe", "theme_inventory", "ads_only", "blocked/never", "stock=0", "tracked %"):
        t.add_column(c, justify="left" if c == "store" else "right")
    rows = conn.execute(f"""
        SELECT s.store_domain, COUNT(*) AS n,
               SUM(h.signal_source = 'cart_probe') AS cp, SUM(h.signal_source = 'theme_inventory') AS th,
               SUM(h.signal_source = 'ads_only') AS ao, SUM(h.signal_source IS NULL) AS nv,
               SUM(h.inventory_tracked = 1) AS tracked,
               SUM((SELECT stock_level FROM inventory_daily i WHERE i.store_id = h.store_id AND i.variant_id = h.variant_id
                    AND i.stock_level IS NOT NULL ORDER BY i.snapshot_date DESC LIMIT 1) = 0) AS zero
        FROM hero_variants h JOIN stores s ON s.id = h.store_id WHERE 1=1 {where}
        GROUP BY s.store_domain ORDER BY s.store_domain""", params).fetchall()
    for r in rows:
        pct = f"{100 * (r['tracked'] or 0) / r['n']:.0f}%" if r["n"] else ""
        t.add_row(_short(r["store_domain"]), str(r["n"]), str(r["cp"] or 0), str(r["th"] or 0), str(r["ao"] or 0),
                  str(r["nv"] or 0), str(r["zero"] or 0), pct)
    console.print(t)
    # 2. raw readings: one row per hero variant, one column per day
    dates = [r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM inventory_daily WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT ?",
                                        (as_of, days))][::-1]
    t = Table(title=f"raw stock_level readings, last {len(dates)} day(s)  ('-' = no reading, 'ads' = not tracked, 'blk' = blocked)")
    for c in ("store", "handle", "variant", "role", "price", "source"):
        t.add_column(c)
    for d in dates:
        t.add_column(d[5:], justify="right")
    for c in ("sold 1d", "u/day 7d", "wow"):
        t.add_column(c, justify="right")
    heroes = conn.execute(f"""
        SELECT s.store_domain, h.* FROM hero_variants h JOIN stores s ON s.id = h.store_id WHERE 1=1 {where}
        ORDER BY s.store_domain, h.role = 'rank', h.handle, h.variant_id""", params).fetchall()
    shown = 0
    for h in heroes:
        if args.limit and shown >= args.limit:
            break
        readings = {r["snapshot_date"]: r for r in conn.execute(
            "SELECT * FROM inventory_daily WHERE store_id = ? AND variant_id = ? AND snapshot_date <= ?",
            (h["store_id"], h["variant_id"], as_of))}
        if not readings and not args.all:
            continue
        cells = []
        for d in dates:
            r = readings.get(d)
            if r is None:
                cells.append("-")
            elif r["stock_level"] is not None:
                cells.append(str(r["stock_level"]))
            else:
                cells.append({"ads_only": "ads", "blocked": "blk"}.get(r["signal_source"], "?"))
        last = readings.get(dates[-1]) if dates else None
        def f(v, fmt="{}"):
            return "" if v is None else fmt.format(v)
        vt = h["variant_title"] or ""
        t.add_row(_short(h["store_domain"]), h["handle"], "" if vt == "Default Title" else vt, h["role"] or "",
                  f(h["price"], "{:.2f}"), h["signal_source"] or "", *cells,
                  f(last["units_sold_1d"]) if last else "", f(last["units_per_day_7d"], "{:.1f}") if last else "",
                  f(last["units_per_day_wow"], "x{:.2f}") if last else "")
        shown += 1
    console.print(t)
    if args.raw:
        t = Table(title="raw probe messages (latest reading per variant)")
        for c in ("store", "handle", "variant_id", "source", "message"):
            t.add_column(c)
        for h in heroes[: args.limit or None]:
            r = conn.execute("SELECT signal_source, raw_message FROM inventory_daily WHERE store_id = ? AND variant_id = ? ORDER BY snapshot_date DESC LIMIT 1",
                             (h["store_id"], h["variant_id"])).fetchone()
            if r:
                t.add_row(_short(h["store_domain"]), h["handle"], str(h["variant_id"]), r["signal_source"] or "", (r["raw_message"] or "")[:100])
        console.print(t)
    al = conn.execute(f"""SELECT a.snapshot_date, a.rule, s.store_domain, a.product_handle, a.detail FROM alerts a JOIN stores s ON s.id = a.store_id
                          WHERE a.rule IN (9, 10, 11) {where} ORDER BY a.snapshot_date DESC, a.rule LIMIT 40""", params).fetchall()
    if al:
        t = Table(title="inventory alerts (rules 9-11)")
        for c in ("date", "rule", "store", "handle", "detail"):
            t.add_column(c)
        for r in al:
            t.add_row(r["snapshot_date"], f"{r['rule']} {inventory.RULES[r['rule']]}", r["store_domain"], r["product_handle"] or "", r["detail"])
        console.print(t)
    return 0


def _detail_pass_store(conn, browser, store_id: int, domain: str, snapshot_date: str, cap: int | None = None) -> dict:
    """Single-ad pages for the selected ads, then delivery / page likes / creative lineage / rule 12. Never raises."""
    t0 = time.monotonic()
    try:
        c = ad_detail.fetch_store_details(conn, browser, store_id, snapshot_date, cap)
        f = ad_detail.finalize_store(conn, store_id, snapshot_date)
        log.info("%-28s detail: fetched %d/%d ok=%d removed=%d no_record=%d errors=%d login_wall=%d creatives_hashed=%d relaunches=%d | delivering=%d off=%d pages=%d "
                 "creative_lineage=%d alerts=%d %.0fs", domain, c["ok"] + c["removed"] + c["no_record"] + c["errors"], c["selected"], c["ok"],
                 c["removed"], c["no_record"], c["errors"], c["login_wall"], c["hashed"], c.get("relaunches", 0), f["delivering"], f["off"], f["pages"],
                 f["creative_lineage"], f["alerts"], time.monotonic() - t0)
        return {**c, **f}
    except Exception as e:  # noqa: BLE001
        log.error("%-28s detail pass FAILED: %s", domain, e)
        return {}


def cmd_ads_detail(args) -> int:
    """Fetch single-ad pages (end_date, page likes, creatives) for stores with scraped ads."""
    from playwright.sync_api import sync_playwright
    snapshot_date = _parse_date(args.date)
    conn = db.connect(args.db)
    stores = read_watchlist(Path(args.watchlist) if args.watchlist else None)
    only = set(args.only) if args.only else None
    targets = [s for s in stores if not only or s["store_domain"] in only]
    with sync_playwright() as pw, meta_ads.KeepAwake():
        handle = meta_ads.BrowserHandle(pw, headless=not args.headed)
        browser = handle
        try:
            if args.ads:
                sid = db.upsert_store(conn, targets[0]["store_domain"]) if targets else None
                for aid in args.ads:
                    row = conn.execute("SELECT store_id FROM meta_ads WHERE ad_id = ?", (aid,)).fetchone()
                    store_id = row["store_id"] if row else sid
                    d, status = ad_detail.fetch_ad_detail(browser, aid)
                    console.print(f"{aid}: {status} {json.dumps({k: v for k, v in (d or {}).items() if k not in ('images', 'videos')}, default=str)[:400]}")
                    if d:
                        console.print(f"  images={len(d['images'])} videos={len(d['videos'])}")
                    if store_id:
                        ad_detail.record_detail(conn, store_id, aid, snapshot_date, d, status)
                        if d:
                            ad_detail.fingerprint_creatives(conn, aid, d)
                        ad_detail.finalize_store(conn, store_id, snapshot_date)
                    conn.commit()
                return 0
            t = Table(title=f"single-ad page pass ({snapshot_date})")
            for c in ("store", "selected", "ok", "removed", "no_record", "errors", "login_wall", "hashed", "delivering", "off", "pages", "creative_lineage", "alerts"):
                t.add_column(c, justify="left" if c == "store" else "right")
            for st in targets:
                store_id = db.upsert_store(conn, st["store_domain"])
                conn.commit()
                if not conn.execute("SELECT 1 FROM meta_ads WHERE store_id = ? LIMIT 1", (store_id,)).fetchone():
                    continue
                r = _detail_pass_store(conn, handle, store_id, st["store_domain"], snapshot_date, args.max)
                t.add_row(_short(st["store_domain"]), *[str(r.get(c, "")) for c in ("selected", "ok", "removed", "no_record", "errors", "login_wall", "hashed", "delivering", "off", "pages", "creative_lineage", "alerts")])
            console.print(t)
        finally:
            handle.close()
    md = ad_metrics.write_alerts_markdown(conn, snapshot_date)
    if md:
        console.print(f"alerts written to {md}")
    return 0


def cmd_ads_detail_report(args) -> int:
    """end_date / page_like_count per day for flagged ads (or --ads), plus page likes per page."""
    conn = db.connect(args.db)
    as_of = _parse_date(args.date) if args.date else conn.execute("SELECT MAX(snapshot_date) FROM meta_ad_detail_daily").fetchone()[0]
    if not as_of:
        console.print("no detail readings yet - run `python tracker.py ads-detail`")
        return 1
    dates = [r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM meta_ad_detail_daily WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT ?",
                                        (as_of, args.days))][::-1]
    where, params = "", []
    if args.store:
        sid = conn.execute("SELECT id FROM stores WHERE store_domain = ?", (args.store,)).fetchone()
        if not sid:
            console.print(f"[red]unknown store[/] {args.store}")
            return 2
        where, params = " AND a.store_id = ?", [sid["id"]]
    if args.ads:
        ids = set(args.ads)
    else:
        ids = set()
        for r in conn.execute(f"SELECT DISTINCT a.store_id FROM meta_ads a WHERE 1=1 {where}", params):
            ids |= ad_detail.flagged_ad_ids(conn, r[0])
        if not ids:
            console.print("no flagged ads (no rule 5-8 alerts) for this selection; showing the ads with detail readings")
            ids = {r[0] for r in conn.execute(f"""SELECT d.ad_id FROM meta_ad_detail_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
                                                 WHERE d.source = 'detail' {where} ORDER BY d.snapshot_date DESC LIMIT ?""", params + [args.limit])}
    rows = [dict(r) for r in conn.execute(
        f"""SELECT a.ad_id, s.store_domain, a.page_name, a.ad_start_date, a.delivery_status, a.last_delivered, a.switched_off_date,
                   a.product_handle, a.lineage_of, a.lineage_via, a.creative_hash, a.page_categories
            FROM meta_ads a JOIN stores s ON s.id = a.store_id WHERE a.ad_id IN ({','.join('?' * len(ids))}) {where}
            ORDER BY s.store_domain, a.page_name, a.ad_start_date""", list(ids) + params)] if ids else []
    t = Table(title=f"end_date per day for {len(rows)} ads (as of {as_of}; '-' = no reading, L = from list payload)")
    for c in ("ad", "store", "page", "start", "product"):
        t.add_column(c)
    for d in dates:
        t.add_column(d[5:], justify="right")
    for c in ("status", "last delivered", "off since", "lineage"):
        t.add_column(c)
    for r in rows[: args.limit]:
        readings = {x["snapshot_date"]: x for x in conn.execute(
            "SELECT snapshot_date, end_date, source, status FROM meta_ad_detail_daily WHERE ad_id = ? AND snapshot_date <= ?", (r["ad_id"], as_of))}
        cells = []
        for d in dates:
            x = readings.get(d)
            if not x:
                cells.append("-")
            elif x["end_date"]:
                cells.append(x["end_date"][5:] + ("" if x["source"] == "detail" else " L"))
            else:
                cells.append(x["status"] or "?")
        lin = f"{r['lineage_of']} ({r['lineage_via'] or 'text'})" if r["lineage_of"] else ""
        t.add_row(r["ad_id"], _short(r["store_domain"]), (r["page_name"] or "")[:24], (r["ad_start_date"] or "")[5:],
                  (r["product_handle"] or "")[:24], *cells, r["delivery_status"] or "", (r["last_delivered"] or "")[5:],
                  (r["switched_off_date"] or "")[5:], lin)
    console.print(t)
    t = Table(title="page_like_count per day (page-level spend proxy)")
    for c in ("store", "page", "page_id", "categories"):
        t.add_column(c)
    for d in dates:
        t.add_column(d[5:], justify="right")
    for c in ("delta 1d", "likes/day 7d", "prev 7d"):
        t.add_column(c, justify="right")
    pages = conn.execute(f"""SELECT DISTINCT p.page_id, p.page_name, s.store_domain, p.store_id FROM meta_page_likes_daily p JOIN stores s ON s.id = p.store_id
                             WHERE 1=1 {where.replace('a.store_id', 'p.store_id')} ORDER BY s.store_domain, p.page_name""", params).fetchall()
    for pg in pages[: args.limit]:
        hist = {x["snapshot_date"]: x for x in conn.execute("SELECT * FROM meta_page_likes_daily WHERE page_id = ? AND snapshot_date <= ?", (pg["page_id"], as_of))}
        last = hist.get(dates[-1]) if dates else None
        cats = ""
        if last and last["page_categories"]:
            try:
                cats = ", ".join(json.loads(last["page_categories"]))[:30]
            except ValueError:
                cats = last["page_categories"][:30]
        t.add_row(_short(pg["store_domain"]), (pg["page_name"] or "")[:24], pg["page_id"], cats,
                  *[str(hist[d]["page_like_count"]) if d in hist and hist[d]["page_like_count"] is not None else "-" for d in dates],
                  "" if not last or last["likes_delta_1d"] is None else str(last["likes_delta_1d"]),
                  "" if not last or last["likes_slope_7d"] is None else f"{last['likes_slope_7d']:.1f}",
                  "" if not last or last["likes_slope_prev_7d"] is None else f"{last['likes_slope_prev_7d']:.1f}")
    console.print(t)
    nr = conn.execute(f"""SELECT s.store_domain, d.status, COUNT(*) AS n FROM meta_ad_detail_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
                          JOIN stores s ON s.id = a.store_id WHERE d.snapshot_date = ? AND d.source = 'detail' AND d.status != 'ok' {where}
                          GROUP BY s.store_domain, d.status ORDER BY s.store_domain, n DESC""", [as_of] + params).fetchall()
    if nr:
        t = Table(title=f"single-ad pages without a record on {as_of} (removed = the library says the ad is gone)")
        for c in ("store", "status", "pages"):
            t.add_column(c, justify="right" if c == "pages" else "left")
        for r in nr:
            t.add_row(_short(r["store_domain"]), r["status"], str(r["n"]))
        console.print(t)
    cov = conn.execute(f"""SELECT s.store_domain, COUNT(DISTINCT a.ad_id) AS ads, SUM(a.delivery_status = 'on') AS on_, SUM(a.delivery_status = 'off') AS off,
                             SUM(a.detail_fetched_date IS NOT NULL) AS fetched, SUM(a.creative_hash IS NOT NULL) AS hashed,
                             SUM(a.lineage_via = 'creative') AS lin_creative
                           FROM meta_ads a JOIN stores s ON s.id = a.store_id WHERE 1=1 {where} GROUP BY s.store_domain""", params).fetchall()
    t = Table(title="coverage")
    for c in ("store", "ads", "delivering", "off", "single-ad pages read", "creatives hashed", "lineage by creative"):
        t.add_column(c, justify="left" if c == "store" else "right")
    for r in cov:
        t.add_row(_short(r["store_domain"]), *[str(r[k] or 0) for k in ("ads", "on_", "off", "fetched", "hashed", "lin_creative")])
    console.print(t)
    return 0


def _read_clipboard() -> str:
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"could not read the clipboard ({e}); use --file or pass the URL") from e


def cmd_fb_capture(args) -> int:
    """Record Sponsored posts you saw yourself: a permalink, the bookmarklet JSON (--paste), or a file."""
    conn = db.connect(args.db)
    today = _parse_date(args.date)
    if args.paste:
        text = _read_clipboard()
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.url:
        text = json.dumps({"permalink": args.url, "page_name": args.page, "primary_text": args.text, "landing_url": args.landing,
                           "reactions": args.reactions, "comments": args.comments, "shares": args.shares})
    else:
        console.print("[red]give a URL, --paste (bookmarklet JSON on the clipboard) or --file[/]")
        return 2
    try:
        caps = fb_posts.parse_captures_text(text)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return 2
    if not caps:
        console.print("[yellow]nothing to capture[/]")
        return 1
    store_id = None
    if args.store:
        store_id = db.upsert_store(conn, args.store)
    new = 0
    for c in caps:
        if c.get("image_url") and not c.get("image_hash"):
            c["image_hash"] = fb_posts.hash_image(c["image_url"])
        if fb_posts.record_capture(conn, c, today, source="paste" if args.paste else ("file" if args.file else "manual"), store_id=store_id):
            new += 1
        console.print(f"post {c['post_id']}  page={c.get('page_name') or c.get('page_id') or '?'}  counts={c.get('counts') or {}}")
    matched = fb_posts.match_posts(conn)
    fb_posts.propagate_to_ads(conn, today)
    console.print(f"{len(caps)} capture(s), {new} new, {matched} newly matched to Ad Library ads. Daily counts: `python tracker.py fb-engagement`")
    return 0


def cmd_fb_engagement(args) -> int:
    """Logged-out re-fetch of every captured permalink; deltas; propagate to matched ads."""
    from playwright.sync_api import sync_playwright
    conn = db.connect(args.db)
    today = _parse_date(args.date)
    n = conn.execute("SELECT COUNT(*) FROM fb_posts").fetchone()[0]
    if not n:
        console.print("no captured posts yet - see `python tracker.py fb-capture --help`")
        return 1
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, **meta_ads.launch_kwargs())
        try:
            c = fb_posts.refresh_engagement(conn, browser, today, args.max)
        finally:
            browser.close()
    console.print(f"permalinks: {c['candidates']} due, fetched {c['fetched']}: ok={c['ok']} gated={c['gated']} removed={c['removed']} "
                  f"no_counts={c['no_counts']} errors={c['errors']}")
    return _fb_report(conn, today, args.limit)


def _fb_report(conn, as_of: str, limit: int = 60) -> int:
    dates = [r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM fb_posts_daily WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 7", (as_of,))][::-1]
    t = Table(title=f"captured Sponsored posts and their public counts (reactions/comments/shares; as of {as_of})")
    for c in ("post", "page", "store", "ad (match)", "status"):
        t.add_column(c)
    for d in dates:
        t.add_column(d[5:], justify="right")
    for c in ("comments 1d", "eng/day 7d"):
        t.add_column(c, justify="right")
    rows = conn.execute("""SELECT p.*, s.store_domain FROM fb_posts p LEFT JOIN stores s ON s.id = p.store_id ORDER BY p.captured_at DESC LIMIT ?""",
                        (limit,)).fetchall()
    for p in rows:
        hist = {x["snapshot_date"]: x for x in conn.execute("SELECT * FROM fb_posts_daily WHERE post_id = ?", (p["post_id"],))}
        last = hist.get(dates[-1]) if dates else None
        cells = []
        for d in dates:
            x = hist.get(d)
            if not x:
                cells.append("-")
            elif x["reactions"] is None and x["comments"] is None:
                cells.append(x["status"] or "?")
            else:
                cells.append(f"{x['reactions'] or 0}/{x['comments'] or 0}/{x['shares'] or 0}")
        match = f"{p['ad_id']} ({p['match_via']} {p['match_score']})" if p["ad_id"] else ""
        t.add_row(p["post_id"][:20], (p["page_name"] or p["page_id"] or "")[:22], _short(p["store_domain"] or ""), match, p["status"] or "",
                  *cells, "" if not last or last["comment_delta_1d"] is None else str(last["comment_delta_1d"]),
                  "" if not last or last["engagement_per_day_7d"] is None else f"{last['engagement_per_day_7d']:.1f}")
    console.print(t)
    total = conn.execute("SELECT COUNT(*), SUM(ad_id IS NOT NULL), SUM(status = 'gated'), SUM(status = 'removed') FROM fb_posts").fetchone()
    console.print(f"{total[0]} posts captured, {total[1] or 0} matched to Ad Library ads, {total[2] or 0} gated, {total[3] or 0} removed")
    return 0


def cmd_fb_report(args) -> int:
    conn = db.connect(args.db)
    as_of = _parse_date(args.date) if args.date else (conn.execute("SELECT MAX(snapshot_date) FROM fb_posts_daily").fetchone()[0] or date.today().isoformat())
    return _fb_report(conn, as_of, args.limit)


def cmd_fb_listen(args) -> int:
    """Receive captures from the browser observer extension (tools/fb_observer) on 127.0.0.1:8765."""
    from http.server import HTTPServer
    conn = db.connect(args.db)
    stats = {"received": 0, "new": 0, "matched": 0}

    def on_capture(n, new, matched):
        stats["received"] += n
        stats["new"] += new
        stats["matched"] += matched
        log.info("captures: +%d (%d new, %d matched) | total received %d, new %d", n, new, matched, stats["received"], stats["new"])
    srv = HTTPServer(("127.0.0.1", args.port), fb_posts.make_listener(conn, lambda: date.today().isoformat(), on_capture))
    console.print(f"listening on http://127.0.0.1:{args.port}/capture for the Sponsored post observer (Ctrl+C to stop). "
                  f"Browse Facebook normally in the browser where the extension is installed.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    console.print(f"received {stats['received']} capture(s), {stats['new']} new, {stats['matched']} matched to ads. Counts: `python tracker.py fb-engagement`")
    return 0


def cmd_fb_bait(args) -> int:
    """Open each watchlist store's hero product page in YOUR default browser so you can add to cart by hand
    (that is what makes the brands' ads appear in your feed). Nothing is automated on the store or on Facebook."""
    import webbrowser
    conn = db.connect(args.db)
    stores = read_watchlist(Path(args.watchlist) if args.watchlist else None)
    only = set(args.only) if args.only else None
    urls = []
    for s in stores:
        d = s["store_domain"]
        if only and d not in only:
            continue
        sid = db.upsert_store(conn, d)
        _, products = inventory.latest_products(conn, sid)
        heroes = inventory.select_heroes(products, date.today(), max_variants=5)
        base = shopify.base_url(d)
        handles = []
        for h in heroes:
            if h["handle"] not in handles:
                handles.append(h["handle"])
        for h in handles[: args.per_store]:
            urls.append(f"{base}/products/{h}")
        if not handles:
            urls.append(base)
    console.print(f"{len(urls)} product page(s) across {len([u for u in urls])} tab(s):")
    for u in urls:
        console.print(f"  {u}")
    if args.print_only:
        return 0
    for i, u in enumerate(urls):
        webbrowser.open_new_tab(u)
        if i < len(urls) - 1:
            time.sleep(1.5)
    console.print("Opened in your browser. On each page: look around for half a minute, add the product to cart, move on. "
                  "Do not check out. Their ads usually reach your feed within 1-2 days.")
    return 0


def _radar_summary(conn, out: dict) -> None:
    c = radar.summary_counts(conn)
    if out.get("imports") and out["imports"]["files"]:
        console.print(f"imports: {out['imports']['files']} file(s): {len(out['imports']['added'])} added, {len(out['imports']['existing'])} already listed, {len(out['imports']['bad'])} unreadable")
    for key, label in (("sweep", "hook sweep"), ("copycat", "copycat")):
        sw = out.get(key)
        if sw:
            console.print(f"{label}: {sw['queries']} queries, {sw['ads']} ads, {len(sw['domains'])} landing domains"
                          + (f"; {sw['skipped']} skipped (searched in the last {config.RADAR_RESWEEP_DAYS} days)" if sw.get("skipped") else "")
                          + (f"; [yellow]{sw['deferred']} deferred to the next sweep (budget)[/]" if sw.get("deferred") else ""))
    if out.get("web"):
        console.print(f"web search: {out['web']['queries']} queries, {out['web']['domains']} domains")
    console.print(f"new landing domains: {out['found']} found -> storefronts {out['shopify']} (promoted {out['promoted']}, parked "
                  f"{out['parked'] - out['funnels']}), funnels parked {out['funnels']}, discarded {out['discarded']} "
                  f"(no storefront and < {config.RADAR_FUNNEL_MIN_ADS} ads)"
                  + (f"; [yellow]{out['deferred_triage']} not checked yet (budget)[/]" if out.get("deferred_triage") else ""))
    console.print(f"re-checked {out['retriaged']} candidate(s) from stored facts, refreshed {out['refreshed']} catalogue(s); "
                  f"Ad Library searches run {out['searched']}"
                  + (f", [yellow]{out['deferred_search']} waiting for the next run[/]" if out.get("deferred_search") else "")
                  + f"; promoted later {out['promoted_later']}")
    console.print(f"radar totals: {c['domains']} domains seen; candidates {c['candidate']} (storefronts {c['shopify']}, funnels {c['funnel']}, "
                  f"{c['unsearched']} not yet searched), promoted {c['promoted']}, manual {c['watchlist']}, discarded {c['discarded']}")


def cmd_radar(args) -> int:
    from playwright.sync_api import sync_playwright
    conn = db.connect(args.db)
    today = _parse_date(args.date)
    do_sweep = True if args.sweep else (False if args.no_sweep else None)
    console.print(f"radar: {'sweep + ' if do_sweep or (do_sweep is None and date.fromisoformat(today).weekday() == config.RADAR_SWEEP_WEEKDAY) else ''}triage, "
                  f"hooks={len(radar.load_hooks())}, country={config.RADAR_COUNTRY}, up to {config.RADAR_MAX_ADS_PER_QUERY} ads/query")
    with sync_playwright() as pw, meta_ads.KeepAwake():
        handle = meta_ads.BrowserHandle(pw, headless=not args.headed)
        try:
            out = radar.run_radar(conn, handle, today, do_sweep=do_sweep, watchlist_path=Path(args.watchlist) if args.watchlist else None,
                                  max_minutes=args.max_minutes)
        finally:
            handle.close()
    _radar_summary(conn, out)
    if out.get("sweep"):
        _hook_table(conn)
    return _radar_table(conn, args.limit)


def _radar_table(conn, limit: int = 40) -> int:
    rows = radar.candidates_rows(conn)
    h = radar.CANDIDATES_HEADERS
    show = ["domain", "type", "status", "store_age_days", "store_first_created", "products", "active_ads", "ads_in_sweeps", "searched_at",
            "pages", "top page", "hot new product", "source", "lander_domain"]
    idx = [h.index(c) for c in show]
    t = Table(title="Candidates (what the Candidates tab shows; set promote=Y in the sheet to force one; "
                    "active_ads before searched_at is set = ads seen in sweeps only)")
    for c in show:
        t.add_column(c.replace("store_", "").replace("_", " "), justify="right" if c in ("store_age_days", "products", "active_ads", "ads_in_sweeps", "pages") else "left")
    for r in rows[:limit]:
        t.add_row(*[(str(r[i])[:28] if c not in ("top page", "source") else str(r[i])[:22]) for i, c in zip(idx, show)])
    console.print(t)
    return 0


def cmd_radar_add(args) -> int:
    conn = db.connect(args.db)
    r = radar.manual_add(conn, args.items, _parse_date(None), watchlist_path=Path(args.watchlist) if args.watchlist else None)
    for d in r["added"]:
        console.print(f"[green]added[/] {d}")
    for d in r["existing"]:
        console.print(f"already listed: {d}")
    for d in r["bad"]:
        console.print(f"[red]not a domain or URL:[/] {d}")
    console.print(f"{len(r['added'])} added to the watchlist (source=manual). They get their first snapshot on the next run.")
    return 0 if r["added"] or not r["bad"] else 1


def _hook_table(conn) -> None:
    rows = radar.hook_yield(conn)
    if not rows or not any(r["sweeps"] for r in rows):
        return
    t = Table(title="hook phrases by yield (delete a phrase with 0 promotable stores over 2 sweeps; add siblings to the top ones)")
    for c in ("hook", "sweeps", "ads", "domains", "promoted", "store cands", "funnels", "discarded", "verdict"):
        t.add_column(c, justify="left" if c in ("hook", "verdict") else "right")
    for r in rows:
        t.add_row(r["hook"], str(r["sweeps"]), str(r["ads"]), str(r["domains"]), str(r["promoted"]), str(r["candidates"]), str(r["funnels"]),
                  str(r["discarded"]), r["verdict"])
    console.print(t)


def _local(iso: str | None) -> str:
    """UTC timestamp from the database -> local wall clock, minute precision."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def cmd_radar_report(args) -> int:
    conn = db.connect(args.db)
    _hook_table(conn)
    c = radar.summary_counts(conn)
    console.print(f"radar totals: {c['domains']} domains seen; candidates {c['candidate']} (storefronts {c['shopify']}, funnels {c['funnel']}, "
                  f"{c['unsearched']} not yet searched), promoted {c['promoted']}, manual {c['watchlist']}, discarded {c['discarded']}")
    runs = conn.execute("SELECT kind, query, started_at, ads_found, domains_found, note FROM radar_runs ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    if runs:
        t = Table(title="recent radar runs")
        for col in ("kind", "query", "started", "ads", "domains", "note"):
            t.add_column(col)
        for r in runs:
            t.add_row(r["kind"], (r["query"] or "")[:40], _local(r["started_at"]), str(r["ads_found"] or ""), str(r["domains_found"] or ""), (r["note"] or "")[:40])
        console.print(t)
    return _radar_table(conn, args.limit)


def cmd_ads(args) -> int:
    snapshot_date = _parse_date(args.date)
    stores = read_watchlist(Path(args.watchlist) if args.watchlist else None)
    if not stores:
        console.print("[red]watchlist is empty[/]")
        return 2
    conn = db.connect(args.db)
    only = set(args.only) if args.only else None
    n = len([s for s in stores if not only or s["store_domain"] in only])
    console.print(f"ads: snapshot_date={snapshot_date} pages={n} waits={config.META_WAIT_MIN}-{config.META_WAIT_MAX}s "
                  f"headless={not args.headed}")
    t0 = time.monotonic()
    planned = plan_meta_stores(conn, stores, only)
    console.print(f"plan: {len(planned)} store(s), least recently scraped first, budget {args.max_minutes or config.META_MAX_MINUTES:.0f} min "
                  f"(about {config.META_MAX_SCROLLS * 5.5 / 60 + 1 + config.META_DETAIL_MAX * 4.5 / 60:.0f} min per store at current settings)")
    ok, failed = run_ads_pass(conn, stores, snapshot_date, only, headless=not args.headed, max_scrolls=args.max_scrolls,
                              detail=not args.no_detail, detail_cap=args.detail_max, max_minutes=args.max_minutes)
    md = ad_metrics.write_alerts_markdown(conn, snapshot_date)
    console.print(f"done in {time.monotonic() - t0:.0f}s: [green]{ok} ok[/], [red]{failed} failed[/]"
                  + (f"; alerts written to {md}" if md else ""))
    return 1 if ok == 0 and failed else 0


def cmd_rank_check(args) -> int:
    """Confirm the impressions sort is informative: for the given stores run the newest-first and the impressions-sorted
    searches (few scrolls each) and compare the orders; records ranks and sort_informative as the night pass would."""
    from playwright.sync_api import sync_playwright
    conn = db.connect(args.db)
    today = _parse_date(args.date)
    stores = read_watchlist(Path(args.watchlist) if args.watchlist else None)
    if args.only:
        stores = [s for s in stores if s["store_domain"] in set(args.only)]
    stores = stores[: args.limit]
    if not stores:
        console.print("[red]no stores selected[/]")
        return 2
    t = Table(title="impressions sort vs newest first")
    for c in ("store", "query", "newest", "sorted", "compared", "same position", "sort_informative", "country", "top 3 by impressions", "views tried"):
        t.add_column(c, justify="right" if c in ("newest", "sorted", "compared", "same position") else "left")
    with sync_playwright() as pw, meta_ads.KeepAwake():
        handle = meta_ads.BrowserHandle(pw, headless=not args.headed)
        try:
            for s in stores:
                sid = db.upsert_store(conn, s["store_domain"], s.get("meta_page_name"), s.get("meta_page_id"), s.get("notes"))
                conn.commit()
                query = s.get("meta_page_name") or s["store_domain"].split("//")[-1]
                url = meta_ads.build_search_url(query=None if s.get("meta_page_id") else query, page_id=s.get("meta_page_id") or None)
                try:
                    base = meta_ads.scrape_page(url, max_scrolls=args.scrolls, browser=handle.get())
                    if base.blocked:
                        raise meta_ads.MetaBlocked(base.note)
                    if base.ads:
                        meta_ads.record_scrape(conn, sid, today, base.ads, query)
                    rk = ad_rank.scrape_ranks(conn, handle.get(), s, sid, today, [a["ad_id"] for a in base.ads], max_scrolls=args.scrolls)
                except meta_ads.MetaBlocked as e:
                    console.print(f"[red]{s['store_domain']}: blocked ({e}); stopping[/]")
                    break
                except Exception as e:  # noqa: BLE001
                    t.add_row(_short(s["store_domain"]), query, "", "", "", "", f"error: {str(e)[:40]}", "", "", "")
                    continue
                top = [r[0] for r in conn.execute("SELECT ad_id FROM meta_ads_daily WHERE store_id = ? AND snapshot_date = ? AND impression_rank IS NOT NULL ORDER BY impression_rank LIMIT 3", (sid, today))]
                t.add_row(_short(s["store_domain"]), query[:24], str(len(base.ads)), str(rk["ranked"]), str(rk["n"]), str(rk["same_position"]),
                          "[green]true[/]" if rk["informative"] else "[red]false[/]" if rk["informative"] is not None else "?", rk.get("country") or "",
                          ", ".join(top), rk.get("summary", ""))
                _post_process_store(conn, sid, s["store_domain"], today, shopify.make_session(), fetch_landings=False)
        finally:
            handle.close()
    console.print(t)
    console.print("false = every country view tried (META_RANK_COUNTRIES) returned its ads in the same order as that view's newest-first "
                  "list: the sort carries no information for that store, and it is re-checked weekly instead of daily. "
                  "country = the view whose ranks were kept. The 'note' column of rank_checks records what each view returned.")
    return 0


def cmd_ads_fields(args) -> int:
    """Which keys the stored ad payloads carry that match --grep (to confirm what Meta calls a badge)."""
    conn = db.connect(args.db)
    sid = None
    if args.store:
        row = conn.execute("SELECT id FROM stores WHERE store_domain = ?", (args.store,)).fetchone()
        if not row:
            console.print(f"[red]unknown store[/] {args.store}")
            return 2
        sid = row["id"]
    fields = meta_ads.payload_fields(conn, args.grep, sid, args.limit)
    if not fields:
        console.print(f"no key or label matching /{args.grep}/ in the last {args.limit} stored payloads")
        return 1
    t = Table(title=f"payload keys matching /{args.grep}/ (last {args.limit} ads{' of ' + args.store if args.store else ''})")
    for c in ("key path", "ads", "example value"):
        t.add_column(c, justify="right" if c == "ads" else "left")
    for k, (n, ex) in list(fields.items())[:40]:
        t.add_row(k, str(n), str(ex)[:70])
    console.print(t)
    return 0


def cmd_delivering_report(args) -> int:
    """Before/after for the delivering metric: active ads (old ranking) vs ads without the low-impression badge."""
    conn = db.connect(args.db)
    bf = meta_ads.backfill_low_impressions(conn)
    console.print(f"badge backfill from stored payloads: {bf['updated']} daily rows filled, {bf['flagged']} flagged low, "
                  f"{bf['no_field']} payloads without a badge field" + (f"; keys: {bf['keys']}" if bf["keys"] else ""))
    like = [f"%{h}%" for h in args.handle] if args.handle else ["%"]
    rows = []
    for pat in like:
        for r in conn.execute("""SELECT DISTINCT s.id AS store_id, s.store_domain, a.product_handle FROM meta_ads a JOIN stores s ON s.id = a.store_id
                                 WHERE a.product_handle LIKE ? AND (? IS NULL OR s.store_domain LIKE ?) ORDER BY s.store_domain, a.product_handle""",
                              (pat, args.store, f"%{args.store}%" if args.store else None)):
            rows.append(dict(r))
    if not rows:
        console.print("no products match; the handle must be one an ad resolved to (see the Signals tab)")
        return 1
    seen = set()
    for r in rows:
        key = (r["store_id"], r["product_handle"])
        if key in seen:
            continue
        seen.add(key)
        snap = ad_metrics._snapshot_on_or_before(conn, r["store_id"], _parse_date(args.date) if args.date else "9999")
        if not snap:
            continue
        ad_metrics.write_concept_rows(conn, r["store_id"], snap)          # survival recomputed on delivering ads
        conn.commit()
        dm = ad_metrics.delivering_metrics(conn, r["store_id"], snap).get(r["product_handle"], {})
        cs = conn.execute("""SELECT COUNT(*) n, SUM(ads_active > 0) alive_search, SUM(COALESCE(ads_delivering, ads_active) > 0) alive_deliv
                             FROM meta_concepts_daily WHERE store_id = ? AND snapshot_date = ? AND product_handle = ?""",
                          (r["store_id"], snap, r["product_handle"])).fetchone()
        t = Table(title=f"{_short(r['store_domain'])} / {r['product_handle']}  (ads as of {snap}; week-ago snapshot {dm.get('prev_as_of') or 'none'})")
        for c in ("metric", "before (active ads)", "after (delivering)"):
            t.add_column(c)
        t.add_row("ads", str(dm.get("ads_active", 0)), str(dm.get("ads_delivering", 0)))
        t.add_row("of which low-impression badge", "-", str(dm.get("ads_low_impressions", 0)))
        t.add_row("of which badge unknown (no field in payload)", "-", str(dm.get("ads_badge_unknown", 0)))
        t.add_row("7 days ago", "-", str(dm.get("ads_delivering_7d_ago", "")))
        t.add_row("week over week", "-", str(dm.get("delivering_velocity_wow", "")))
        t.add_row("concepts alive / total", f"{cs['alive_search'] or 0}/{cs['n'] or 0}", f"{cs['alive_deliv'] or 0}/{cs['n'] or 0}")
        console.print(t)
        ads = conn.execute("""SELECT a.ad_id, a.ad_start_date, a.delivery_status, d.is_active, d.low_impressions, a.low_impressions_key, a.concept_id
                              FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id AND d.snapshot_date = ?
                              WHERE a.store_id = ? AND a.product_handle = ? AND d.is_active = 1
                              ORDER BY d.low_impressions IS NULL, d.low_impressions, a.ad_start_date DESC LIMIT ?""",
                           (snap, r["store_id"], r["product_handle"], args.limit)).fetchall()
        t2 = Table(title=f"active ads for {r['product_handle']} (first {args.limit})")
        for c in ("ad_id", "started", "badge", "badge key", "delivery (end_date)", "concept", "delivering"):
            t2.add_column(c)
        for a in ads:
            badge = {1: "LOW", 0: "no"}.get(a["low_impressions"], "?")
            t2.add_row(a["ad_id"], a["ad_start_date"] or "", badge, (a["low_impressions_key"] or "")[:28], a["delivery_status"] or "",
                       (a["concept_id"] or "")[:10], "yes" if ad_metrics.delivering(dict(a)) else "no")
        console.print(t2)
        rm = ad_rank.ad_rank_metrics(conn, r["store_id"], snap)
        pm = rm["products"].get(r["product_handle"])
        chk = conn.execute("SELECT informative, n_compared, same_position FROM rank_checks WHERE store_id = ? ORDER BY snapshot_date DESC LIMIT 1", (r["store_id"],)).fetchone()
        if pm:
            t3 = Table(title=f"top {ad_rank.TOP_N} ads by impression rank for {r['product_handle']} (ranks as of {rm['as_of']}; "
                             f"best rank {pm['best_rank']}, 7d ago {pm['best_rank_7d_ago'] if pm['best_rank_7d_ago'] is not None else '-'}, "
                             f"ads in top {ad_rank.TOP_N}: {pm['ads_in_top5']})")
            for c in ("rank", "ad_id", "started", "days running", "rank 7d ago", "delta 7d", "top5 days", "badge"):
                t3.add_column(c, justify="right" if c not in ("ad_id", "started", "badge") else "left")
            for a in pm["top5_ads"]:
                t3.add_row(str(a["rank"]), a["ad_id"], "", "" if a["days_running"] is None else str(a["days_running"]),
                           "" if a["rank_7d_ago"] is None else str(a["rank_7d_ago"]), "" if a["rank_delta_7d"] is None else str(a["rank_delta_7d"]),
                           str(a["top5_days"]), {1: "LOW", 0: "no"}.get(a["low_impressions"], "?"))
            console.print(t3)
        elif rm["as_of"] is None:
            console.print(f"  no impression-rank data for {_short(r['store_domain'])} yet: run `python tracker.py rank-check --only {r['store_domain']}` "
                          "or wait for the next Meta pass")
        else:
            console.print(f"  {r['product_handle']}: none of its ads appear in the impressions-sorted result of {rm['as_of']}")
        if chk is not None:
            console.print(f"  sort_informative={'true' if chk['informative'] else 'false' if chk['informative'] is not None else '?'} "
                          f"({chk['same_position']}/{chk['n_compared']} of the first ids in the same position as newest-first)")
    console.print("Signals and Early rank on ads_in_top5 / best-rank trend, then delivering trend; run `python tracker.py sync-sheets` to push.")
    return 0


def cmd_ads_metrics(args) -> int:
    """Recompute the landing join / concepts / lineage / alerts from stored ads (no scraping)."""
    snapshot_date = _parse_date(args.date) if args.date else None
    conn = db.connect(args.db)
    if snapshot_date is None:
        snapshot_date = conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily").fetchone()[0]
    if not snapshot_date:
        console.print("[red]no ad snapshots yet[/]")
        return 2
    session = shopify.make_session()
    stores = conn.execute(
        """SELECT DISTINCT s.id, s.store_domain FROM meta_ads_daily d JOIN stores s ON s.id = d.store_id
           WHERE d.snapshot_date = ? ORDER BY s.store_domain""", (snapshot_date,)).fetchall()
    if args.only:
        stores = [s for s in stores if s["store_domain"] in set(args.only)]
    console.print(f"ads-metrics: snapshot_date={snapshot_date} stores={len(stores)} fetch_landings={not args.no_fetch} "
                  f"posts={args.posts}")
    bf = meta_ads.backfill_low_impressions(conn)
    if bf["updated"]:
        console.print(f"low-impression badge filled from stored payloads for {bf['updated']} ad-day(s) ({bf['flagged']} flagged)")
    browser = pw = None
    if args.posts:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True, **meta_ads.launch_kwargs())
    try:
        for s in stores:
            _post_process_store(conn, s["id"], s["store_domain"], snapshot_date, session, fetch_landings=not args.no_fetch)
            if browser is not None:
                pe = meta_ads.fetch_boosted_engagement(conn, browser, s["id"], snapshot_date)
                log.info("%-28s boosted posts: %d candidates, fetched %d, counts found %d", s["store_domain"],
                         pe["candidates"], pe["fetched"], pe["with_counts"])
                for a in conn.execute("SELECT DISTINCT ad_id FROM meta_ads_daily WHERE store_id = ? AND snapshot_date = ?",
                                      (s["id"], snapshot_date)):
                    ad_metrics.compute_reach_metrics(conn, a["ad_id"], snapshot_date)
                conn.commit()
    finally:
        if browser is not None:
            browser.close()
            pw.stop()
    md = ad_metrics.write_alerts_markdown(conn, snapshot_date)
    if md:
        console.print(f"alerts written to {md}")
    return 0


def cmd_ads_report(args) -> int:
    conn = db.connect(args.db)
    as_of = _parse_date(args.date) if args.date else (
        conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily").fetchone()[0])
    if not as_of:
        console.print("[red]no ad snapshots yet[/] - run `python tracker.py ads --only <store>` first")
        return 2
    where, params = "", []
    if args.store:
        where, params = "AND s.store_domain = ?", [args.store]
    t = Table(title=f"Ad Library pages (as of {as_of})")
    for c in ("store", "query", "status", "ads", "active", "new today", "inactive today", "scrolls", "note"):
        t.add_column(c)
    for r in conn.execute(f"""
        SELECT s.store_domain, r.query, r.status, r.ads_found, r.scrolls, r.detail,
               (SELECT COUNT(*) FROM meta_ads_daily d WHERE d.store_id = s.id AND d.snapshot_date = ? AND d.is_active = 1) active,
               (SELECT COUNT(*) FROM meta_ads a WHERE a.store_id = s.id AND a.first_seen_date = ?) new_today,
               (SELECT COUNT(*) FROM meta_ads_daily d WHERE d.store_id = s.id AND d.snapshot_date = ? AND d.is_active = 0) gone
        FROM meta_page_runs r JOIN stores s ON s.id = r.store_id
        WHERE r.snapshot_date = ? {where} AND r.id = (SELECT MAX(id) FROM meta_page_runs r2 WHERE r2.store_id = r.store_id AND r2.snapshot_date = r.snapshot_date)
        ORDER BY s.store_domain""", [as_of, as_of, as_of, as_of] + params):
        colour = {"ok": "green", "blocked": "red", "error": "red"}.get(r["status"], "yellow")
        t.add_row(r["store_domain"], r["query"] or "", f"[{colour}]{r['status']}[/]", str(r["ads_found"]),
                  str(r["active"]), str(r["new_today"]), str(r["gone"]), str(r["scrolls"]), (r["detail"] or "")[:50])
    console.print(t)

    t = Table(title=f"where each store's active ads land (as of {as_of}): only 'product' rows reach the Signals tab")
    for c in ("store", "active ads", "-> products", "distinct products", "unlisted-product", "advertorial", "homepage", "collection",
              "other path", "external", "no-url", "page-ignored"):
        t.add_column(c, justify="left" if c == "store" else "right")
    for st in conn.execute(f"SELECT id, store_domain FROM stores {'WHERE store_domain = ?' if args.store else ''} ORDER BY store_domain",
                           ([args.store] if args.store else [])).fetchall():
        b = ad_metrics.landing_breakdown(conn, st["id"], st["store_domain"], as_of)
        if not b["active"]:
            continue
        t.add_row(_short(st["store_domain"]), str(b["active"]), str(b["to_products"]), str(b["products"]),
                  *[str(b[k]) for k in ("unlisted-product", "advertorial", "homepage", "collection", "other-store-path", "external", "no-url", "page-ignored")])
    console.print(t)
    t = Table(title=f"products by active ads pointing at them (as of {as_of})")
    for c in ("store", "product handle", "active ads", "pages", "oldest ad (days)", "newest ad (days)", "concepts", "landing kinds"):
        t.add_column(c)
    for r in conn.execute(f"""
        SELECT s.store_domain, a.product_handle, COUNT(*) n, COUNT(DISTINCT a.page_name) pages,
               MAX(d.days_running) oldest, MIN(d.days_running) newest, COUNT(DISTINCT a.concept_id) concepts,
               SUM(a.landing_resolved_via = 'url') direct, SUM(a.landing_resolved_via = 'page-fetch') via_page
        FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id JOIN stores s ON s.id = d.store_id
        WHERE d.snapshot_date = ? AND d.is_active = 1 AND a.product_handle IS NOT NULL {where}
        GROUP BY s.store_domain, a.product_handle ORDER BY n DESC LIMIT ?""", [as_of] + params + [args.limit]):
        t.add_row(r["store_domain"], r["product_handle"], str(r["n"]), str(r["pages"]), str(r["oldest"] or ""),
                  str(r["newest"] or ""), str(r["concepts"]), f"direct {r['direct'] or 0}, via page {r['via_page'] or 0}")
    console.print(t)
    ignored = conn.execute(f"""
        SELECT p.page_name, p.ads FROM meta_pages_daily p JOIN stores s ON s.id = p.store_id
        WHERE p.snapshot_date = ? AND p.ignored = 1 {where} ORDER BY p.ads DESC""", [as_of] + params).fetchall()
    if ignored:
        console.print("[dim]ignored pages (none of their ads land on the store): " +
                      ", ".join(f"{r['page_name']} x{r['ads']}" for r in ignored) + "[/]")

    t = Table(title="advertised handles (raw /products/<handle> in ad URLs) -> resolved product")
    for c in ("store", "advertised handle", "active ads", "in products.json", "resolved to"):
        t.add_column(c)
    for r in conn.execute(f"""
        SELECT s.store_domain, a.landing_handle, COUNT(*) n, a.product_handle,
               (SELECT CASE WHEN COALESCE(p.unlisted, 0) = 1 THEN 'unlisted' ELSE 'yes' END FROM products_daily p
                 WHERE p.store_id = s.id AND p.handle = a.landing_handle
                   AND p.snapshot_date = (SELECT MAX(snapshot_date) FROM products_daily WHERE store_id = s.id)) listed
        FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id JOIN stores s ON s.id = d.store_id
        WHERE d.snapshot_date = ? AND d.is_active = 1 AND a.landing_handle IS NOT NULL AND COALESCE(a.page_ignored, 0) = 0 {where}
        GROUP BY s.store_domain, a.landing_handle ORDER BY n DESC LIMIT ?""", [as_of] + params + [args.limit]):
        listed = {"yes": "[green]yes[/]", "unlisted": "[cyan]unlisted (live page, fetched)[/]"}.get(r["listed"], "[yellow]no[/]")
        t.add_row(r["store_domain"], r["landing_handle"], str(r["n"]), listed, r["product_handle"] or "[red]-[/]")
    console.print(t)

    t = Table(title="landing pages fetched (advertorials / unmatched URLs), unresolved first")
    for c in ("landing url", "active ads", "http", "resolved to", "handles found on page"):
        t.add_column(c, overflow="fold")
    known_products = {r[0] for r in conn.execute(
        "SELECT DISTINCT handle FROM products_daily WHERE snapshot_date >= date(?, '-7 days')", (as_of,))}
    for r in conn.execute(f"""
        SELECT lp.url, lp.status, lp.product_handle, lp.candidates, COUNT(*) n
        FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id JOIN stores s ON s.id = a.store_id
        JOIN landing_pages lp ON lp.url = substr(a.landing_url, 1, CASE WHEN instr(a.landing_url, '?') > 0
                                                       THEN instr(a.landing_url, '?') - 1 ELSE length(a.landing_url) END)
        WHERE d.snapshot_date = ? AND d.is_active = 1 AND COALESCE(a.page_ignored, 0) = 0 {where}
        GROUP BY lp.url ORDER BY (lp.product_handle IS NULL) DESC, n DESC LIMIT 20""", [as_of] + params):
        if r["url"].endswith(".json") or ad_metrics.handle_from_url(r["url"])[0] in known_products:
            continue   # product pages we already know are not diagnostics
        t.add_row(r["url"], str(r["n"]), "" if r["status"] is None else str(r["status"]),
                  r["product_handle"] or "[red]-[/]", (r["candidates"] or "")[:90])
    console.print(t)

    t = Table(title=f"concepts (page + landing + launch window), by days running")
    for c in ("store", "page", "launched", "days", "ads", "active", "survival", "product / page handle", "landing"):
        t.add_column(c, overflow="fold")
    for r in conn.execute(f"""
        SELECT s.store_domain, c.page_name, c.launch_date, c.days_running, c.ads_ever, c.ads_active, c.survival,
               c.product_handle, c.page_handle, c.landing_url
        FROM meta_concepts_daily c JOIN stores s ON s.id = c.store_id WHERE c.snapshot_date = ? {where}
        ORDER BY c.ads_ever DESC, c.days_running DESC LIMIT ?""", [as_of] + params + [args.limit]):
        surv = "" if r["survival"] is None else f"{r['survival']:.0%}"
        from urllib.parse import urlparse as _up
        t.add_row(r["store_domain"], r["page_name"] or "", r["launch_date"] or "", str(r["days_running"] or ""),
                  str(r["ads_ever"]), str(r["ads_active"]), surv,
                  r["product_handle"] or (f"/pages/{r['page_handle']}" if r["page_handle"] else ""),
                  (_up(r["landing_url"]).path if r["landing_url"] else "")[:60])
    console.print(t)

    lin = conn.execute(f"""
        SELECT s.store_domain, a.page_name, a.lineage_of, o.ad_start_date AS parent_start, COUNT(*) n,
               AVG(a.lineage_similarity) sim, MAX(a.product_handle) product_handle, substr(o.headline, 1, 40) headline
        FROM meta_ads a JOIN meta_ads o ON o.ad_id = a.lineage_of JOIN stores s ON s.id = a.store_id
        WHERE a.lineage_of IS NOT NULL AND COALESCE(a.page_ignored, 0) = 0 {where}
        GROUP BY s.store_domain, a.page_name, a.lineage_of ORDER BY n DESC LIMIT ?""", params + [args.limit]).fetchall()
    if lin:
        t = Table(title="lineage: older ads whose copy new ads re-use (>70% similar, parent 14+ days)")
        for c in ("store", "page", "parent ad", "parent start", "parent headline", "new ads", "avg sim", "product"):
            t.add_column(c)
        for r in lin:
            t.add_row(r["store_domain"], r["page_name"] or "", r["lineage_of"], r["parent_start"] or "",
                      r["headline"] or "", str(r["n"]), f"{r['sim']:.0%}", r["product_handle"] or "")
        console.print(t)

    al = conn.execute(f"""SELECT a.rule, a.product_handle, a.detail, s.store_domain FROM alerts a JOIN stores s ON s.id = a.store_id
                          WHERE a.snapshot_date = ? AND a.rule >= 5 {where} ORDER BY a.rule""", [as_of] + params).fetchall()
    if al:
        t = Table(title=f"alerts {as_of}")
        for c in ("rule", "store", "product", "detail"):
            t.add_column(c, overflow="fold")
        for r in al:
            t.add_row(f"{r['rule']} {ad_metrics.RULES.get(r['rule'], '')}", r["store_domain"], r["product_handle"] or "", r["detail"])
        console.print(t)
    if not args.raw:
        console.print("[dim]add --raw for the per-ad rows[/]")
        return 0

    t = Table(title=f"raw ads (as of {as_of}, newest start first, limit {args.limit})")
    for c in ("store", "ad id", "page", "start", "days", "act", "type", "headline", "primary text", "landing", "fp", "eu",
              "reach/day", "type", "comments"):
        t.add_column(c, overflow="fold")
    rows = conn.execute(f"""
        SELECT s.store_domain, a.ad_id, a.page_name, a.ad_start_date, a.first_seen_date, d.is_active, a.creative_type,
               a.headline, a.primary_text, a.landing_url, a.fingerprint, d.eu_total_reach, d.uk_reach, d.reach_slope_7d,
               a.engagement_type, d.comments
        FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id JOIN stores s ON s.id = d.store_id
        WHERE d.snapshot_date = ? {where}
        ORDER BY a.ad_start_date DESC, a.ad_id LIMIT ?""", [as_of] + params + [args.limit]).fetchall()
    for r in rows:
        dr = meta_ads.days_running(r["ad_start_date"], r["first_seen_date"], as_of)
        t.add_row(r["store_domain"], r["ad_id"], r["page_name"] or "", r["ad_start_date"] or "?",
                  "" if dr is None else str(dr), "[green]Y[/]" if r["is_active"] else "[red]N[/]",
                  r["creative_type"] or "", (r["headline"] or "")[:40], (r["primary_text"] or "")[:70],
                  (r["landing_url"] or "")[:60], r["fingerprint"] or "",
                  "" if r["eu_total_reach"] is None else str(r["eu_total_reach"]),
                  "" if r["reach_slope_7d"] is None else str(r["reach_slope_7d"]), r["engagement_type"] or "",
                  "" if r["comments"] is None else str(r["comments"]))
    console.print(t)
    return 0


def cmd_ads_coverage(args) -> int:
    conn = db.connect(args.db)
    as_of = _parse_date(args.date) if args.date else conn.execute("SELECT MAX(snapshot_date) FROM meta_ads_daily").fetchone()[0]
    if not as_of:
        console.print("[red]no ad snapshots yet[/]")
        return 2
    rows = ad_metrics.coverage(conn, as_of)
    t = Table(title=f"Meta measurability per store (as of {as_of}; ignored pages excluded)",
              caption="EU exact = exact EU/EEA reach number; UK exact = GB reach from the country breakdown; "
                      "range = only a lower/upper bound; boosted = ad resolves to a Page/Instagram post; "
                      "comments = boosted posts whose counts were read today; dark = no underlying post")
    for c, j in (("store", "left"), ("ads", "right"), ("EU exact", "right"), ("UK exact", "right"), ("range only", "right"),
                 ("no reach", "right"), ("boosted", "right"), ("comments", "right"), ("dark", "right")):
        t.add_column(c, justify=j)

    def pct(n, d):
        return f"{n} ({n / d:.0%})" if d else "0"
    for r in rows:
        style = "bold" if r["store"] == "TOTAL" else ""
        t.add_row(f"[{style}]{r['store']}[/]" if style else r["store"], str(r["ads"]),
                  pct(r["eu_exact"], r["ads"]), pct(r["uk_exact"], r["ads"]), pct(r["range_only"], r["ads"]),
                  pct(r["no_reach"], r["ads"]), pct(r["boosted"], r["ads"]), pct(r["with_comments"], r["boosted"]),
                  pct(r["dark"], r["ads"]))
    console.print(t)
    keys = rows[-1]["keys"] if rows else {}
    if keys:
        console.print("[dim]reach-related keys seen in the raw payloads (ads carrying each): " +
                      ", ".join(f"{k} x{v}" for k, v in sorted(keys.items(), key=lambda kv: -kv[1])[:12]) + "[/]")
    ps = conn.execute("""SELECT COALESCE(d.post_status, 'not fetched') st, COUNT(*) n FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id
                         WHERE d.snapshot_date = ? AND a.engagement_type = 'boosted' GROUP BY st""", (as_of,)).fetchall()
    if ps:
        console.print("[dim]boosted post fetch status: " + ", ".join(f"{r['st']} x{r['n']}" for r in ps) + "[/]")
    if args.keys:
        shape = ad_metrics.payload_shape(conn, as_of)
        t = Table(title="fields present in the raw ad payloads (depth <= 2)", caption="null = present but empty")
        for c in ("field", "ads", "null"):
            t.add_column(c, justify="right" if c != "field" else "left")
        for k, c, z in shape[:80]:
            t.add_row(k, str(c), str(z))
        console.print(t)
    return 0


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
    s.add_argument("--ads", action="store_true", help="also scrape the Meta Ad Library (same as META_ADS=1 in .env)")
    s.add_argument("--no-ads", action="store_true", help="skip the Meta pass even if META_ADS=1")
    s.add_argument("--inventory", action="store_true", help="probe stock on every store (same as INVENTORY=1; default: INVENTORY_STORES only)")
    s.add_argument("--no-inventory", action="store_true", help="skip the stock probe even if INVENTORY_STORES is set")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("inventory", help="probe hero-variant stock via /cart/add.js (fallback: theme inventory_quantity) and compute units sold")
    s.add_argument("--date", help="snapshot date YYYY-MM-DD (default today)")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.add_argument("--only", nargs="+", metavar="DOMAIN", help="probe these store domains (overrides INVENTORY_STORES)")
    s.add_argument("--all", action="store_true", help="probe every watchlist store")
    s.set_defaults(fn=cmd_inventory)

    s = sub.add_parser("inventory-probe", help="one live /cart/add.js probe with status, headers and body printed (diagnostics)")
    s.add_argument("store", help="store domain")
    s.add_argument("--variant", type=int, help="variant id (default: first hero variant of the store)")
    s.add_argument("--handle", help="product handle for the Referer / product page")
    s.set_defaults(fn=cmd_inventory_probe)

    s = sub.add_parser("inventory-report", help="raw stock_level readings per hero variant and per-store fallback-rung counts")
    s.add_argument("--date", help="as of this snapshot date (default: latest)")
    s.add_argument("--store", help="one store domain")
    s.add_argument("--days", type=int, default=14, help="how many daily columns to show")
    s.add_argument("--limit", type=int, default=200, help="max variant rows")
    s.add_argument("--all", action="store_true", help="include hero variants with no reading yet")
    s.add_argument("--raw", action="store_true", help="also print the raw probe messages")
    s.set_defaults(fn=cmd_inventory_report)

    s = sub.add_parser("ads", help="scrape the Meta Ad Library for watchlist stores (Part B)")
    s.add_argument("--date", help="snapshot date YYYY-MM-DD (default today)")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.add_argument("--only", nargs="+", metavar="DOMAIN", help="limit to these store domains")
    s.add_argument("--headed", action="store_true", help="show the browser window (debugging)")
    s.add_argument("--max-scrolls", type=int, help=f"scroll cap per page (default {config.META_MAX_SCROLLS})")
    s.add_argument("--max-minutes", type=float, help=f"wall-clock budget for this pass (default META_MAX_MINUTES={config.META_MAX_MINUTES:.0f})")
    s.add_argument("--no-detail", action="store_true", help="skip the single-ad page pass")
    s.add_argument("--detail-max", type=int, help=f"single-ad pages per store (default {config.META_DETAIL_MAX})")
    s.set_defaults(fn=cmd_ads)

    s = sub.add_parser("ads-detail", help="single-ad Ad Library pages: end_date (delivery), page likes, creative fingerprints")
    s.add_argument("--date", help="snapshot date (default today)")
    s.add_argument("--watchlist")
    s.add_argument("--only", nargs="+", metavar="DOMAIN")
    s.add_argument("--ads", nargs="+", metavar="AD_ID", help="fetch just these ad ids and print what was parsed")
    s.add_argument("--max", type=int, help=f"pages per store (default {config.META_DETAIL_MAX})")
    s.add_argument("--headed", action="store_true")
    s.set_defaults(fn=cmd_ads_detail)

    s = sub.add_parser("ads-detail-report", help="end_date and page_like_count per day for flagged ads; page likes; coverage")
    s.add_argument("--date", help="as of this date (default latest)")
    s.add_argument("--store", help="one store domain")
    s.add_argument("--ads", nargs="+", metavar="AD_ID")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--limit", type=int, default=60)
    s.set_defaults(fn=cmd_ads_detail_report)

    s = sub.add_parser("fb-capture", help="record a Sponsored post you saw yourself (URL, bookmarklet JSON via --paste, or a file)")
    s.add_argument("url", nargs="?", help="post permalink")
    s.add_argument("--paste", action="store_true", help="read the bookmarklet's JSON from the clipboard")
    s.add_argument("--file", help="file with JSON / JSON lines / one URL per line")
    s.add_argument("--page", help="page name")
    s.add_argument("--text", help="primary text (for matching to the Ad Library ad)")
    s.add_argument("--landing", help="landing URL")
    s.add_argument("--reactions"); s.add_argument("--comments"); s.add_argument("--shares")
    s.add_argument("--store", help="store domain this post advertises")
    s.add_argument("--date", help="snapshot date for the counts (default today)")
    s.set_defaults(fn=cmd_fb_capture)

    s = sub.add_parser("fb-engagement", help="logged-out re-fetch of every captured post's public counts; deltas; join to ads")
    s.add_argument("--date", help="snapshot date (default today)")
    s.add_argument("--max", type=int, help=f"posts per run (default {config.FB_POSTS_MAX})")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--limit", type=int, default=60)
    s.set_defaults(fn=cmd_fb_engagement)

    s = sub.add_parser("fb-listen", help="receive captures from the browser observer extension (tools/fb_observer) on 127.0.0.1:8765")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_fb_listen)

    s = sub.add_parser("fb-bait", help="open watchlist stores' hero product pages in your browser so you can add to cart by hand (seeds retargeting)")
    s.add_argument("--watchlist"); s.add_argument("--only", nargs="+", metavar="DOMAIN")
    s.add_argument("--per-store", type=int, default=1, help="product pages per store (default 1)")
    s.add_argument("--print-only", action="store_true", help="list the URLs instead of opening them")
    s.set_defaults(fn=cmd_fb_bait)

    s = sub.add_parser("fb-report", help="captured posts, matches and count history")
    s.add_argument("--date"); s.add_argument("--limit", type=int, default=60)
    s.set_defaults(fn=cmd_fb_report)

    s = sub.add_parser("rank-check", help="confirm the impressions sort is informative for a few stores (records ranks + sort_informative)")
    s.add_argument("--only", nargs="+", metavar="DOMAIN")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--scrolls", type=int, default=None)
    s.add_argument("--date"); s.add_argument("--watchlist"); s.add_argument("--headed", action="store_true")
    s.set_defaults(fn=cmd_rank_check)

    s = sub.add_parser("ads-fields", help="which keys the stored ad payloads carry that match --grep (find what Meta calls a badge)")
    s.add_argument("--grep", default="impression")
    s.add_argument("--store")
    s.add_argument("--limit", type=int, default=400, help="most recently seen ads to scan")
    s.set_defaults(fn=cmd_ads_fields)

    s = sub.add_parser("delivering-report", help="before/after: active ads vs ads delivering (no low-impression badge) per product")
    s.add_argument("--handle", action="append", help="product handle (substring); repeatable")
    s.add_argument("--store")
    s.add_argument("--date")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_delivering_report)

    s = sub.add_parser("ads-report", help="per-page status, products by ads, concepts, lineage, alerts (--raw for ad rows)")
    s.add_argument("--date", help="snapshot date (default: latest)")
    s.add_argument("--store", help="one store domain")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--raw", action="store_true", help="also print the per-ad rows")
    s.set_defaults(fn=cmd_ads_report)

    s = sub.add_parser("ads-metrics", help="recompute landing join / concepts / lineage / alerts from stored ads")
    s.add_argument("--date", help="snapshot date (default: latest)")
    s.add_argument("--only", nargs="+", metavar="DOMAIN")
    s.add_argument("--no-fetch", action="store_true", help="do not fetch advertorial landing pages")
    s.add_argument("--posts", action="store_true", help="also open boosted posts in a browser to read comment counts")
    s.set_defaults(fn=cmd_ads_metrics)

    s = sub.add_parser("ads-coverage", help="how measurable the scraped ads are: EU/UK reach, boosted vs dark, per store")
    s.add_argument("--date", help="snapshot date (default: latest)")
    s.add_argument("--keys", action="store_true", help="also list which fields the raw payloads contain")
    s.set_defaults(fn=cmd_ads_coverage)

    s = sub.add_parser("sync-sheets", help="push latest snapshot + deltas to Google Sheets via Apps Script")
    s.add_argument("--date", help="sync the snapshot as of this date (default: latest)")
    s.add_argument("--tabs", help="comma list from signals,families,categories,stores,pages,candidates,products,alerts (default all)")
    s.add_argument("--dry-run", action="store_true", help="build and size the chunks but send nothing")
    s.add_argument("--watchlist", help="alternate watchlist.csv (only its stores are synced)")
    s.add_argument("--verify-only", action="store_true", help="send nothing; compare the live sheet's row counts with the DB")
    s.add_argument("--no-verify", action="store_true", help="skip the read-back comparison after syncing")
    s.set_defaults(fn=cmd_sync_sheets)

    s = sub.add_parser("db-check", help="is data/tracker.db free to write? lists its files and the programs that could be holding it")
    s.add_argument("--db")
    s.set_defaults(fn=cmd_db_check)

    s = sub.add_parser("prune-dead", help="list (and with --apply remove) watchlist domains that never returned a catalogue or an ad")
    s.add_argument("--apply", action="store_true", help="remove them from watchlist.csv (history in the DB is kept)")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_prune_dead)

    s = sub.add_parser("restore-stores", help="put stores that are in the database but not on the watchlist back (undo prune-dead / remove-store)")
    s.add_argument("domains", nargs="*", help="only these (default: every store missing from the watchlist)")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_restore_stores)

    s = sub.add_parser("find-page", help="find a store's Facebook page: footer link, then an Ad Library search for the brand; --set N saves it")
    s.add_argument("domain")
    s.add_argument("--query", help="search this instead of the brand name derived from the domain")
    s.add_argument("--set", type=int, metavar="N", help="save candidate N to watchlist.csv and the database")
    s.add_argument("--auto", action="store_true", help="save the top candidate when its ads land on the store")
    s.add_argument("--max-ads", type=int, default=150)
    s.add_argument("--headed", action="store_true")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_find_page)

    s = sub.add_parser("set-page", help="set a store's Meta page name (and id) by hand")
    s.add_argument("domain")
    s.add_argument("--name", required=True)
    s.add_argument("--page-id")
    s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_set_page)

    s = sub.add_parser("remove-store", help="remove one or more domains from watchlist.csv")
    s.add_argument("domains", nargs="+")
    s.add_argument("--watchlist", help="alternate watchlist.csv path")
    s.set_defaults(fn=cmd_remove_store)

    s = sub.add_parser("radar", help="store discovery: hook + copycat sweeps (Sundays, or --sweep), then triage new landing domains")
    s.add_argument("--date"); s.add_argument("--watchlist")
    s.add_argument("--sweep", action="store_true", help="run the weekly sweeps now")
    s.add_argument("--no-sweep", action="store_true", help="triage only, even on a Sunday")
    s.add_argument("--max-minutes", type=float, help=f"budget for this run (default RADAR_MAX_MINUTES={config.RADAR_MAX_MINUTES:.0f})")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_radar)

    s = sub.add_parser("radar-add", help="add domains/URLs straight to the watchlist (source=manual), no triage")
    s.add_argument("items", nargs="+"); s.add_argument("--watchlist")
    s.set_defaults(fn=cmd_radar_add)

    s = sub.add_parser("radar-report", help="radar totals, recent runs, the Candidates table")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_radar_report)

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
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/] - everything recorded so far is in the database; the next run continues from there")
        import os
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)      # skip interpreter shutdown: worker threads mid-request and Playwright's loop would otherwise hang or spew
