"""The store list: stores.txt, one domain per line. That file is the whole watchlist; edit it whenever you like and
every command reads it fresh.

    # comments and blank lines are fine
    elivorahealth.com
    gutbiowellness.com   page=123456789012345   # optional: pin the Meta pass to this one Facebook page
    somestore.com        # a trailing comment is kept as the store's note

A .csv path (the previous watchlist.csv layout, columns store_domain, meta_page_name, meta_page_id, notes) is still
read and written the old way, so the offline tests keep their fixtures. The first time stores.txt is missing and
watchlist.csv exists next to it, stores.txt is created from it.
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from . import config

log = logging.getLogger("earlyscale.watchlist")

COLUMNS = ["store_domain", "meta_page_name", "meta_page_id", "notes"]
PAGE_RE = re.compile(r"\bpage[=:]\s*(\d{5,})", re.I)


def normalise_domain(domain: str) -> str:
    """'https://www.Example.com/products/x?y=1' -> 'example.com': lower-case, no scheme, no www., no path.
    An explicit http:// origin (local mock stores, usually with a port) is preserved as 'http://host:port'."""
    d = domain.strip().lower()
    scheme = ""
    if d.startswith("http://"):
        scheme, d = "http://", d[len("http://"):]
    elif d.startswith("https://"):
        d = d[len("https://"):]
    d = d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if d.startswith("www."):
        d = d[len("www."):]
    return scheme + d if scheme and ":" in d else d


def _is_csv(path: Path) -> bool:
    return path.suffix.lower() == ".csv"


def _default_path() -> Path:
    """stores.txt; created once from watchlist.csv when only the old file exists."""
    path = config.WATCHLIST_PATH
    if not path.exists() and not _is_csv(path):
        old = path.parent / "watchlist.csv"
        if old.exists():
            rows = _read_csv(old)
            _write_txt(path, rows, header=True)
            log.info("stores.txt created from watchlist.csv (%d stores); edit stores.txt from now on", len(rows))
    return path


# ---------------------------------------------------------------- reading

def _parse_txt_line(raw: str) -> dict | None:
    """One stores.txt line -> a watchlist row, or None for blank / comment lines."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    body, _, comment = line.partition("#")
    parts = body.split()
    if not parts:
        return None
    domain = parts[0].strip(",;")
    if "." not in domain:
        return None
    page_id = ""
    m = PAGE_RE.search(body)
    if m:
        page_id = m.group(1)
    return {"store_domain": normalise_domain(domain), "meta_page_name": "", "meta_page_id": page_id, "notes": comment.strip()}


def _read_txt(path: Path) -> list[dict]:
    rows, seen = [], set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        r = _parse_txt_line(raw)
        if r and r["store_domain"] not in seen:
            seen.add(r["store_domain"])
            rows.append(r)
    return rows


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = []
        for row in csv.DictReader(f):
            domain = (row.get("store_domain") or "").strip()
            if not domain or domain.startswith("#"):
                continue
            rows.append({c: (row.get(c) or "").strip() for c in COLUMNS} | {"store_domain": normalise_domain(domain)})
        return rows


def read_watchlist(path: Path | None = None) -> list[dict]:
    path = Path(path) if path else _default_path()
    if not path.exists():
        return []
    return _read_csv(path) if _is_csv(path) else _read_txt(path)


# ---------------------------------------------------------------- writing

def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def _txt_line(r: dict) -> str:
    line = r["store_domain"]
    if r.get("meta_page_id"):
        line += f"   page={r['meta_page_id']}"
    if r.get("notes"):
        line += f"   # {r['notes']}"
    return line


def _write_txt(path: Path, rows: list[dict], header: bool = False) -> None:
    """Rewrite stores.txt keeping comment lines and the order; stores no longer in `rows` are dropped."""
    keep = {r["store_domain"]: r for r in rows}
    out: list[str] = []
    written: set[str] = set()
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            r = _parse_txt_line(raw)
            if r is None:
                out.append(raw.rstrip())
                continue
            d = r["store_domain"]
            if d in keep and d not in written:
                cur = keep[d]
                out.append(raw.rstrip() if (r["meta_page_id"] == cur.get("meta_page_id", "") and r["notes"] == cur.get("notes", "")) else _txt_line(cur))
                written.add(d)
    elif header:
        out.append("# One store per line: the domain. Optional on the same line: page=<facebook page id> to pin the Meta pass to one page,")
        out.append("# and a # comment as a note. Every command reads this file fresh; add or remove lines whenever you like.")
    for r in rows:
        if r["store_domain"] not in written:
            out.append(_txt_line(r))
            written.add(r["store_domain"])
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def _write(path: Path, rows: list[dict]) -> None:
    (_write_csv if _is_csv(path) else _write_txt)(path, rows)


def append_to_watchlist(entry: dict, path: Path | None = None) -> bool:
    """Append a store; returns False if the domain is already listed."""
    path = Path(path) if path else _default_path()
    rows = read_watchlist(path)
    entry = {c: entry.get(c, "") or "" for c in COLUMNS}
    entry["store_domain"] = normalise_domain(entry["store_domain"])
    if entry["store_domain"] in {r["store_domain"] for r in rows}:
        return False
    if _is_csv(path):
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            if new_file:
                w.writeheader()
            w.writerow(entry)
    else:
        _write_txt(path, rows + [entry], header=not path.exists())
    return True


def remove_from_watchlist(domains: list[str], path: Path | None = None) -> tuple[list[str], list[str]]:
    """Drop the given domains. Returns (removed, not_found)."""
    path = Path(path) if path else _default_path()
    targets = {normalise_domain(d) for d in domains}
    rows = read_watchlist(path)
    keep = [r for r in rows if r["store_domain"] not in targets]
    present = {r["store_domain"] for r in rows}
    removed = sorted(t for t in targets if t in present)
    missing = sorted(t for t in targets if t not in present)
    if removed:
        _write(path, keep)
    return removed, missing


def update_watchlist_entry(domain: str, path: Path | None = None, **fields) -> bool:
    """Set meta_page_id / notes (/ meta_page_name in the csv layout) on one store. Returns False if not listed."""
    path = Path(path) if path else _default_path()
    target = normalise_domain(domain)
    rows = read_watchlist(path)
    hit = False
    for r in rows:
        if r["store_domain"] == target:
            for k, v in fields.items():
                if k in COLUMNS and v is not None:
                    r[k] = v
            hit = True
    if hit:
        _write(path, rows)
    return hit
