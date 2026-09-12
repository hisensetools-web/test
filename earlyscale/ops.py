"""Diagnostics without pasting: `python tracker.py diag` collects a report (code version, scheduled tasks, database
counts, per-store status, the top Winners rows, log errors) and writes it to the Google Doc "EarlyScale Diag"
through the Apps Script web app, where it can be read directly. The day / night tasks push a fresh report when
they finish. Read-only: it runs nothing and changes nothing on the machine.
"""
from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
from datetime import datetime, timezone

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
    from . import db as _db, winners
    max_chars = max_chars or config.OPS_DIAG_CHARS
    L: list[str] = []
    add = L.append
    add(f"== EarlyScale diag {_utcnow()} ==")
    add(f"host: {platform.node()} {platform.system()} {platform.release()}; python {platform.python_version()}; cwd {ROOT}")
    add(f"code: {git_head()}")
    add(f"config: META_MAX_SCROLLS={config.META_MAX_SCROLLS} META_NIGHT_MINUTES={os.environ.get('META_NIGHT_MINUTES', '600')} "
        f"META_STOP_AT={config.META_STOP_AT or '-'} RADAR_MAX_MINUTES={config.RADAR_MAX_MINUTES:.0f} "
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
    ad = _q1(conn, "SELECT MAX(snapshot_date) FROM ads_daily")
    add(f"stores {_q1(conn, 'SELECT COUNT(*) FROM stores')} (shop_id known {_q1(conn, 'SELECT COUNT(*) FROM stores WHERE shop_id IS NOT NULL')}); "
        f"products_daily latest {pd} ({_q1(conn, 'SELECT COUNT(*) FROM products_daily WHERE snapshot_date = ?', pd)} rows); "
        f"ads {_q1(conn, 'SELECT COUNT(*) FROM ads')}; ads_daily latest {ad} "
        f"({_q1(conn, 'SELECT COUNT(*) FROM ads_daily WHERE snapshot_date = ?', ad)} rows, "
        f"active {_q1(conn, 'SELECT COUNT(*) FROM ads_daily WHERE snapshot_date = ? AND still_active = 1', ad)}, "
        f"badge known {_q1(conn, 'SELECT COUNT(*) FROM ads_daily WHERE snapshot_date = ? AND still_active = 1 AND low_impressions IS NOT NULL', ad)}); "
        f"url_daily latest {_q1(conn, 'SELECT MAX(snapshot_date) FROM url_daily')} ({_q1(conn, 'SELECT COUNT(*) FROM url_daily WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM url_daily)')} URLs)")
    add("")
    add("== stores ==")
    add("domain | platform | shop_id | products: status date | ads: date active delivering badge_unknown urls>=3 | last error")
    for s in conn.execute("SELECT id, store_domain, platform, shop_id FROM stores ORDER BY store_domain"):
        sr = conn.execute("SELECT status, snapshot_date, error FROM store_runs WHERE store_id = ? ORDER BY snapshot_date DESC, run_id DESC LIMIT 1",
                          (s[0],)).fetchone()
        snap = winners.latest_scrape(conn, s[0])
        if snap:
            a = conn.execute("""SELECT COUNT(*), SUM(low_impressions = 0), SUM(low_impressions IS NULL) FROM ads_daily
                                WHERE store_id = ? AND snapshot_date = ? AND still_active = 1""", (s[0], snap)).fetchone()
            urls = _q1(conn, "SELECT COUNT(*) FROM url_daily WHERE store_id = ? AND snapshot_date = ? AND delivering >= ?", s[0], snap, winners.MIN_DELIVERING)
            ads_txt = f"{snap} {a[0]} {a[1] or 0} {a[2] or 0} {urls}"
        else:
            ads_txt = "-"
        add(f"{s[1]} | {s[2] or '?'} | {s[3] or '-'} | {(sr[0] + ' ' + sr[1]) if sr else '-'} | {ads_txt} | {((sr[2] or '') if sr else '')[:70]}")
    add("")
    add("== winners (top 40 by delivering_wow, then delivering) ==")
    add(" | ".join(winners.WINNERS_HEADERS))
    stores = [dict(r) for r in conn.execute("SELECT id, store_domain, shop_id, store_created_est FROM stores ORDER BY store_domain")]
    try:
        for r in winners.winners_rows(conn, stores)[:40]:
            add(" | ".join(str(x) for x in r))
    except Exception as e:  # noqa: BLE001
        add(f"(winners section failed: {e})")
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
