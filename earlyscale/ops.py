"""Diagnostics without pasting: `python tracker.py diag` collects a report (code version, scheduled tasks, database
counts, per-store status, rank verdicts, log errors) and writes it to the Google Doc "EarlyScale Diag" through the
Apps Script web app, where it can be read directly. The day / night tasks push a fresh report when they finish.
Read-only: it runs nothing and changes nothing on the machine.
"""
from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config

ROOT = config.ROOT
DIAG_DOC = "EarlyScale Diag"


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sh(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a read-only helper (git log, schtasks /Query) and return (rc, output)."""
    try:
        r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, errors="replace")
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def git_head() -> str:
    rc, out = _sh(["git", "log", "-1", "--format=%h %cd %s", "--date=format:%Y-%m-%d %H:%M"])
    _, branch = _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _, dirty = _sh(["git", "status", "--short"])
    n = len([ln for ln in dirty.splitlines() if ln.strip()])
    return f"{out} (branch {branch}{', ' + str(n) + ' local change(s)' if n else ''})" if rc == 0 else f"git not available: {out}"


def _scheduled_tasks() -> list[str]:
    if os.name != "nt":
        return ["(not Windows: cron / launchd not inspected)"]
    out = []
    for name in ("ShopifyTracker Daily", "ShopifyTracker Meta"):
        rc, txt = _sh(["schtasks", "/Query", "/TN", name, "/V", "/FO", "LIST"])
        if rc != 0:
            out.append(f"{name}: not registered")
            continue
        want = {}
        for ln in txt.splitlines():
            for k in ("Last Run Time", "Last Result", "Next Run Time", "Status"):
                if ln.strip().startswith(k + ":"):
                    want[k] = ln.split(":", 1)[1].strip()
        out.append(f"{name}: " + ", ".join(f"{k} {v}" for k, v in want.items()))
    return out


def _q1(conn, sql, *p):
    r = conn.execute(sql, p).fetchone()
    return r[0] if r else None


def collect_diag(conn: sqlite3.Connection, notes: list[str] | None = None, max_chars: int | None = None) -> str:
    from . import db as _db
    max_chars = max_chars or config.OPS_DIAG_CHARS
    L: list[str] = []
    add = L.append
    add(f"== EarlyScale diag {_utcnow()} ==")
    add(f"host: {platform.node()} {platform.system()} {platform.release()}; python {platform.python_version()}; cwd {ROOT}")
    add(f"code: {git_head()}")
    add(f"config: META_RANK={int(config.META_RANK)} META_RANK_UI={int(config.META_RANK_UI)} META_MAX_SCROLLS={config.META_MAX_SCROLLS} "
        f"META_NIGHT_MINUTES={os.environ.get('META_NIGHT_MINUTES', '480')} SHEETS_ADS_PER_STORE={config.SHEETS_ADS_PER_STORE} "
        f"INVENTORY_STORES={len(config.INVENTORY_STORES)} LANDING_REFRESH_WORKERS={config.LANDING_REFRESH_WORKERS} "
        f"SHEETS_WEBHOOK_URL={'set' if config.SHEETS_WEBHOOK_URL else 'MISSING'}")
    for n in notes or []:
        add(f"note: {n}")
    add("")
    add("== scheduled tasks ==")
    L.extend(_scheduled_tasks())
    add("")
    add("== database ==")
    try:
        p = config.DB_PATH
        add(f"{p} {p.stat().st_size / 1e6:.0f} MB; schema stamp {'current' if _q1(conn, 'PRAGMA user_version') == _db.schema_stamp() else 'STALE'}; "
            f"migrations: {', '.join(r[0] for r in conn.execute('SELECT name FROM schema_migrations ORDER BY applied_at'))}")
    except Exception as e:  # noqa: BLE001
        add(f"db info failed: {e}")
    pd = _q1(conn, "SELECT MAX(snapshot_date) FROM products_daily")
    md = _q1(conn, "SELECT MAX(snapshot_date) FROM meta_ads_daily")
    add(f"stores {_q1(conn, 'SELECT COUNT(*) FROM stores')}; products_daily latest {pd} "
        f"({_q1(conn, 'SELECT COUNT(*) FROM products_daily WHERE snapshot_date = ?', pd)} rows); "
        f"meta_ads {_q1(conn, 'SELECT COUNT(*) FROM meta_ads')}; meta_ads_daily latest {md} "
        f"({_q1(conn, 'SELECT COUNT(*) FROM meta_ads_daily WHERE snapshot_date = ?', md)} rows, "
        f"badge known {_q1(conn, 'SELECT COUNT(*) FROM meta_ads_daily WHERE snapshot_date = ? AND low_impressions IS NOT NULL', md)}, "
        f"ranked {_q1(conn, 'SELECT COUNT(*) FROM meta_ads_daily WHERE snapshot_date = ? AND impression_rank IS NOT NULL', md)})")
    add("landing_pages by resolver: " + ", ".join(f"v{r[0] or 0}={r[1]}" for r in conn.execute("SELECT resolver, COUNT(*) FROM landing_pages GROUP BY 1")))
    add("")
    add("== rank verdicts (last 3 days) ==")
    for r in conn.execute("""SELECT s.store_domain, c.snapshot_date, c.country, c.informative, c.n_sorted, c.note
                             FROM rank_checks c JOIN stores s ON s.id = c.store_id
                             WHERE c.snapshot_date >= date((SELECT MAX(snapshot_date) FROM rank_checks), '-2 days')
                             ORDER BY c.snapshot_date DESC, s.store_domain LIMIT 150"""):
        add(f"{r[1]} {r[0]:<28} {r[2] or '-':<4} informative={r[3]} sorted={r[4]} {(r[5] or '')[:600]}")
    add("")
    add("== stores ==")
    add("domain | platform | products: status date | ads: date active badge_known low resolved | last error")
    for s in conn.execute("SELECT id, store_domain, platform FROM stores ORDER BY store_domain"):
        sr = conn.execute("SELECT status, snapshot_date, error FROM store_runs WHERE store_id = ? ORDER BY snapshot_date DESC, run_id DESC LIMIT 1",
                          (s[0],)).fetchone()
        ad = conn.execute("""SELECT d.snapshot_date, COUNT(*), SUM(d.low_impressions IS NOT NULL), SUM(d.low_impressions = 1),
                                    SUM(a.product_handle IS NOT NULL)
                             FROM meta_ads_daily d JOIN meta_ads a ON a.ad_id = d.ad_id WHERE d.store_id = ? AND d.is_active = 1
                             AND d.snapshot_date = (SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = ?)""", (s[0], s[0])).fetchone()
        ads_txt = f"{ad[0]} {ad[1]} {ad[2] or 0} {ad[3] or 0} {ad[4] or 0}" if ad and ad[0] else "-"
        add(f"{s[1]} | {s[2] or '?'} | {(sr[0] + ' ' + sr[1]) if sr else '-'} | {ads_txt} | {((sr[2] or '') if sr else '')[:70]}")
    add("")
    add("== landers -> product (latest snapshot; the attribution behind Signals, busiest first) ==")
    try:
        from urllib.parse import urlparse
        for r in conn.execute("""SELECT s.store_domain, a.landing_url, a.product_handle, COUNT(*) n
                                 FROM meta_ads a JOIN meta_ads_daily d ON d.ad_id = a.ad_id JOIN stores s ON s.id = a.store_id
                                 WHERE d.is_active = 1 AND a.landing_url IS NOT NULL AND a.landing_url NOT LIKE '%/products/%'
                                   AND d.snapshot_date = (SELECT MAX(snapshot_date) FROM meta_ads_daily WHERE store_id = a.store_id)
                                 GROUP BY s.store_domain, a.landing_url, a.product_handle ORDER BY n DESC LIMIT 80"""):
            u = urlparse(r[1])
            add(f"{r[0]:<26} {r[3]:>4} ads  {(u.netloc + u.path)[:70]:<70} -> {r[2] or '(unresolved)'}")
    except Exception as e:  # noqa: BLE001
        add(f"(lander section failed: {e})")
    add("")
    add("== logs ==")
    logs_dir = ROOT / "logs"
    logs = sorted(logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:4] if logs_dir.exists() else []
    for lg in logs:
        lines = lg.read_text(encoding="utf-8", errors="replace").splitlines()
        bad = [ln for ln in lines if ("ERROR" in ln or "Traceback" in ln or "WARNING" in ln)][-40:]
        add(f"-- {lg.name} ({lg.stat().st_size // 1000} KB, {len(lines)} lines, modified {datetime.fromtimestamp(lg.stat().st_mtime):%Y-%m-%d %H:%M})")
        for ln in bad:
            add("  ! " + ln[:220])
        for ln in lines[-30:]:
            add("    " + ln[:220])
    text = "\n".join(L)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... truncated at {max_chars} chars"
    return text


def push_diag(session, url: str, text: str) -> dict:
    from . import sheets
    return sheets.post_payload(session, url, {"tab": "Diag", "mode": "diag", "text": text})
