"""watchlist.csv read/write."""
from __future__ import annotations

import csv
from pathlib import Path

from . import config

COLUMNS = ["store_domain", "meta_page_name", "meta_page_id", "notes"]


def normalise_domain(domain: str) -> str:
    """Lower-case, strip trailing slash and the default https:// scheme.
    An explicit http:// (local mock stores) is preserved."""
    d = domain.strip().lower().rstrip("/")
    if d.startswith("https://"):
        d = d[len("https://"):]
    return d


def read_watchlist(path: Path | None = None) -> list[dict]:
    path = path or config.WATCHLIST_PATH
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = []
        for row in csv.DictReader(f):
            domain = (row.get("store_domain") or "").strip()
            if not domain or domain.startswith("#"):
                continue
            rows.append({c: (row.get(c) or "").strip() for c in COLUMNS} | {"store_domain": normalise_domain(domain)})
        return rows


def append_to_watchlist(entry: dict, path: Path | None = None) -> bool:
    """Append a store; returns False if the domain is already listed."""
    path = path or config.WATCHLIST_PATH
    existing = {r["store_domain"] for r in read_watchlist(path)}
    entry = {c: entry.get(c, "") or "" for c in COLUMNS}
    entry["store_domain"] = normalise_domain(entry["store_domain"])
    if entry["store_domain"] in existing:
        return False
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(entry)
    return True
