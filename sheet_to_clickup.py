#!/usr/bin/env python3
"""One-off reconciliation: everything the Google Sheet export holds that ClickUp does not, added to ClickUp.

    python sheet_to_clickup.py            # dry run: prints every change it would make, touches nothing
    python sheet_to_clickup.py --apply    # makes the changes, then re-reads each task to confirm they landed

Reads the three tabs from sheet_export/ (File > Download > CSV of each tab) and the ClickUp lists through the API
with VIDDL_CLICKUP_TOKEN from .env. Additive only: links, post ids and research notes that are missing are appended
to task descriptions under a dated heading; custom fields are filled only when empty in ClickUp; products that have
no task are created; a product the sheet marks "Ready for launch" moves from "researching" to "ready to launch".
Nothing already in ClickUp is overwritten; every difference that would need an overwrite is listed as a NOTE.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from viddownloader import clickup, config
from viddownloader.config import slugify

ROOT = Path(__file__).resolve().parent
EXPORT = ROOT / "sheet_export"
API = clickup.API
PRODUCTS_LIST = "901222590753"
ACCOUNTS_LIST = "901222611415"
TODAY = datetime.now(timezone.utc).strftime("%d %b %Y")
URL_RE = re.compile(r"https?://[^\s\"'<>|]+")
POST_RE = re.compile(r"#[A-Za-z0-9+/=]{20,}")

# sheet value -> ClickUp status
STATUS_MAP = {"ready for launch": "ready to launch", "running": "testing", "lesson": "lesson", "winner": "winner", "scaling": "scaling"}
FOUND_BY_MAP = {"i": "J", "j": "J", "o": "O", "c": "C", "claude": "C"}
# Competition Checklist product names -> ClickUp task names (the migration expanded them)
CHECKLIST_ALIASES = {"purse": "Purse (Vanity Chain Pouch)", "starbucks mug": "Starbucks Mug (Glass Bearista)",
                     "apple x issey miyake": "Apple x Issey Miyake iPhone Pocket", "hello kitty stanley": "Hello Kitty Stanley (Sweet Hearts 40 oz)"}


# --------------------------------------------------------------------------- sheet
def read_csv(name: str) -> list[list[str]]:
    matches = list(EXPORT.glob(f"*{name}*.csv"))
    if not matches:
        raise SystemExit(f"no CSV for tab '{name}' in {EXPORT} (File > Download > CSV of that tab, put it there)")
    return list(csv.reader(open(matches[0], encoding="utf-8-sig")))


def links_in(text: str) -> list[str]:
    seen, out = set(), []
    for u in URL_RE.findall(text or ""):
        u = u.rstrip(".,;)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def sheet_products() -> list[dict]:
    rows = read_csv("Main TikTok Prods")
    hdr = [h.strip().lower() for h in rows[0]]
    col = {name: hdr.index(name) for name in hdr}
    out = []
    for r in rows[1:]:
        r = r + [""] * (len(hdr) - len(r))
        g = lambda k: r[col[k]].strip() if k in col else ""  # noqa: E731
        if not g("product name"):
            continue
        out.append({"name": g("product name"), "links": links_in(g("adspy")), "competition": links_in(g("competition")),
                    "first": g("first appearance date"), "offer": g("offer"), "method": g("method"), "lp": g("lp status"),
                    "tt": g("tt status"), "post_ids": POST_RE.findall(g("tt post ids")), "final": g("final url"),
                    "launch": g("launch date"), "found": g("found by"), "feedback": g("feedback \nloop") or g("feedback loop")})
    return out


def sheet_accounts() -> list[dict]:
    rows = read_csv("Ad Accounts")
    hdr = [h.strip().lower() for h in rows[0]]
    i = {k: n for n, k in enumerate(hdr)}
    out = []
    for r in rows[1:]:
        r = r + [""] * (len(hdr) - len(r))
        name = r[i["ad account name"]].strip()
        if not name:
            continue
        out.append({"name": name, "code": r[i["ad account code"]].strip(), "emails": r[i["email address added"]].strip(),
                    "dedicated": r[i["dedicated for:"]].strip(), "products": [p.strip() for p in r[i["products running"]].split(",") if p.strip()],
                    "status": r[i["status"]].strip(), "birch": r[i["birch automation"]].strip(),
                    "requested": r[i["date requested"]].strip(), "created": r[i["date created"]].strip()})
    return out


def sheet_checklist() -> list[dict]:
    """Blocks of the Competition Checklist tab: product name, its pipiads / competitor links, the first-appearance
    note, the checks done (method, done?, result) and the Conclusion text under the block."""
    rows = read_csv("Competition Checklist")
    blocks, cur, in_conclusion = [], None, False
    for r in rows:
        r = [c.strip() for c in r] + [""] * (7 - len(r))
        head = r[0]
        if head == "Product Name":
            cur, in_conclusion = None, False
            continue
        if head == "Conclusion":
            in_conclusion = True
            continue
        if in_conclusion:
            if head and cur is not None:
                cur["conclusion"] = (cur["conclusion"] + " " + head).strip()
            continue
        if head and cur is None:
            cur = {"name": head, "links": [], "first_appearance": "", "checks": [], "conclusion": ""}
            blocks.append(cur)
        if cur is None:
            continue
        if r[3]:
            cur["first_appearance"] = (cur["first_appearance"] + " " + r[3]).strip()
        if r[4]:
            cur["checks"].append((r[4], r[5].upper() == "TRUE", r[6]))
        for cell in (r[1], r[2], r[6]):
            for u in links_in(cell):
                if u not in cur["links"]:
                    cur["links"].append(u)
    return blocks


def checklist_markdown(b: dict) -> str:
    out = [f"**Competition Checklist (from the sheet, added {TODAY})**"]
    if b["first_appearance"]:
        out.append(f"First appearance: {b['first_appearance']}")
    for method, done, result in b["checks"]:
        line = f"- [{'x' if done else ' '}] {method.strip()}"
        if result:
            line += " — " + " / ".join(p.strip() for p in result.splitlines() if p.strip())
        out.append(line)
    if b["conclusion"]:
        out.append(f"Conclusion: {b['conclusion']}")
    links, seen = [], set()
    for u in b["links"]:
        if "docs.google" in u or norm_url(u) in seen:
            continue
        seen.add(norm_url(u))
        links.append(u)
    if links:
        out.append("Links:")
        out.append(md_links(links))
    return "\n".join(out)


# --------------------------------------------------------------------------- dates / values
def parse_date(text: str) -> str | None:
    """Sheet dates -> YYYY-MM-DD. '9/26/2026', '26/09/2026', '16-03' (day-month, this year), '18th September ***'."""
    t = re.sub(r"[*]+", "", text or "").strip()
    if not t:
        return None
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", t)
    if m:
        return f"2026-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)", t)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} 2026", "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def to_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def offer_parts(text: str) -> tuple[str | None, str | None, str | None]:
    """'Sale Price: $39.90 / Comparison Price: $79.80 / Offer: Bogo' -> ('39.90', '79.80', 'BOGO')."""
    sale = re.search(r"Sale Price:\s*\$?([\d.,]+)", text or "")
    comp = re.search(r"Comparison Price:\s*\$?([\d.,]+)", text or "")
    offer = re.search(r"Offer:\s*(\w+)", text or "")
    return (sale.group(1).replace(",", "") if sale else None, comp.group(1).replace(",", "") if comp else None,
            offer.group(1).upper() if offer else None)


def norm_url(u: str) -> str:
    return re.sub(r"[?#].*$", "", u.replace("\\_", "_").replace("\\&", "&")).rstrip("/").lower()


def video_key(u: str) -> str:
    m = re.search(r"(?:vm\.tiktok\.com/|vt\.tiktok\.com/|/reel/|/reels/|/p/|/video/)([^/?#]+)", u)
    return m.group(1) if m else norm_url(u)


# --------------------------------------------------------------------------- ClickUp
class CU:
    def __init__(self, token: str, apply: bool):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": token, "Content-Type": "application/json", "User-Agent": config.USER_AGENT})
        self.apply = apply
        self.writes = 0

    def get(self, path: str, **params):
        for attempt in range(4):
            r = self.s.get(f"{API}{path}", params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(30)
                continue
            if r.status_code != 200:
                raise SystemExit(f"GET {path}: HTTP {r.status_code} {r.text[:200]}")
            return r.json()
        raise SystemExit(f"GET {path}: rate limited four times")

    def write(self, method: str, path: str, body: dict, what: str):
        if not self.apply:
            return None
        self.writes += 1
        time.sleep(0.7)                                  # 100 requests / minute is the ClickUp limit
        for attempt in range(4):
            r = self.s.request(method, f"{API}{path}", data=json.dumps(body), timeout=60)
            if r.status_code == 429:
                time.sleep(30)
                continue
            if r.status_code >= 300:
                print(f"    !! {what}: HTTP {r.status_code} {r.text[:200]}")
                return None
            return r.json() if r.text else {}
        print(f"    !! {what}: rate limited four times")
        return None

    def fields(self, list_id: str) -> dict[str, dict]:
        return {f["name"]: f for f in self.get(f"/list/{list_id}/field")["fields"]}

    def tasks(self, list_id: str) -> list[dict]:
        out, page = [], 0
        while True:
            data = self.get(f"/list/{list_id}/task", page=page, include_closed="true", subtasks="false", include_markdown_description="true")
            out += data.get("tasks") or []
            if data.get("last_page", True) or not data.get("tasks"):
                return out
            page += 1

    def task(self, task_id: str) -> dict:
        return self.get(f"/task/{task_id}", include_markdown_description="true")

    def set_field(self, task: dict, field: dict, value, what: str):
        print(f"    set   {field['name']} = {str(value)[:80]}")
        body = {"value": value}
        if field["type"] == "date":
            body["value_options"] = {"time": False}
        self.write("POST", f"/task/{task['id']}/field/{field['id']}", body, f"{task['name']}: {what}")

    def append_description(self, task: dict, addition: str, what: str):
        current = task.get("markdown_description") or task.get("description") or ""
        new = (current.rstrip() + "\n\n" + addition.strip() + "\n") if current.strip() else addition.strip() + "\n"
        print(f"    append {what} ({addition.count(chr(10)) + 1} lines)")
        self.write("PUT", f"/task/{task['id']}", {"markdown_content": new}, f"{task['name']}: {what}")
        task["markdown_description"] = new                        # so later appends stack

    def set_status(self, task: dict, status: str):
        print(f"    status {task['status']['status']} -> {status}")
        self.write("PUT", f"/task/{task['id']}", {"status": status}, f"{task['name']}: status")
        task["status"]["status"] = status

    def create_task(self, list_id: str, name: str, description: str, status: str, custom_fields: list[dict]) -> dict | None:
        print(f"    create task '{name}' ({status}, {len(custom_fields)} fields)")
        body = {"name": name, "markdown_content": description, "status": status, "custom_fields": custom_fields}
        return self.write("POST", f"/list/{list_id}/task", body, f"create {name}")


def field_value(task: dict, name: str):
    for f in task.get("custom_fields") or []:
        if f.get("name") == name:
            v = f.get("value")
            if v in (None, "", []):
                return None
            return v
    return None


def option_id(field: dict, label: str) -> str | None:
    for o in field.get("type_config", {}).get("options", []):
        if o["name"].lower() == label.lower():
            return o["id"]
    return None


def md_links(urls: list[str]) -> str:
    """One markdown link per line; very long tracking URLs show without their query string."""
    return "\n".join(f"[{re.sub(r'[?#].*$', '', u) if len(u) > 120 else u}]({u})" for u in urls)


# --------------------------------------------------------------------------- reconciliation
def match_product(sheet_row: dict, tasks: list[dict]) -> dict | None:
    """By name first; failing that by competition / PDP URL (a renamed row on the sheet)."""
    slug = slugify(sheet_row["name"])
    for t in tasks:
        if slugify(t["name"]) == slug:
            return t
    keys = {norm_url(u) for u in sheet_row["competition"]} | ({norm_url(sheet_row["final"])} if sheet_row["final"] else set())
    for t in tasks:
        for fname in ("Main Competitor", "Competition URL's", "PDP URL"):
            v = field_value(t, fname)
            if v and norm_url(str(v)) in keys:
                return t
    return None


def product_fields(row: dict, F: dict) -> list[dict]:
    """Custom field values a sheet row provides, as {id, value} pairs (only fields that exist on the list)."""
    out = []
    sale, comp, offer = offer_parts(row["offer"])
    def add(name, value):
        if name in F and value not in (None, ""):
            out.append({"id": F[name]["id"], "value": value, "_name": name})
    comp_field = "Main Competitor" if "Main Competitor" in F else "Competition URL's"
    if row["competition"]:
        add(comp_field, row["competition"][0])
    add("PDP URL", row["final"])
    add("Sale Price", sale)
    add("Sale price (compare)", comp)
    if offer and "Offer Type" in F:
        add("Offer Type", option_id(F["Offer Type"], offer))
    if row["method"] and "Product Research Method" in F:
        add("Product Research Method", option_id(F["Product Research Method"], row["method"]))
    fb = FOUND_BY_MAP.get(row["found"].lower())
    if fb and "Found by" in F:
        add("Found by", option_id(F["Found by"], fb))
    if parse_date(row["launch"]):
        add("Launch Date", to_ms(parse_date(row["launch"])))
    if parse_date(row["first"]):
        add("First Appearance Date", to_ms(parse_date(row["first"])))
    if row["post_ids"] and "TT Post IDs" in F:
        ids = row["post_ids"]
        add("TT Post IDs", f"{len(ids)} IDs – full list in description. First 10: " + " ".join(ids[:10]))
    return out


def new_task_description(row: dict) -> str:
    parts = [f"Added from Google Sheet \"Product Research TT 2.0 [2026]\" → Main TikTok Prods V2 on {TODAY} (row was not in ClickUp)."]
    if row["first"]:
        parts.append(f"**First appearance:** {row['first']}")
    if row["competition"]:
        parts.append("**Competition:**\n" + md_links(row["competition"]))
    if row["offer"]:
        parts.append("**Offer:** " + row["offer"].replace("\n", " · "))
    if row["lp"] or row["tt"]:
        parts.append(f"**Sheet status:** LP {row['lp'] or '-'} · TT {row['tt'] or '-'}")
    if row["feedback"]:
        parts.append("**Feedback loop:** " + row["feedback"])
    parts.append(f"**Adspy ({len(row['links'])} links)**\n" + (md_links(row["links"]) if row["links"] else "none on the sheet"))
    if row["post_ids"]:
        parts.append(f"**TT Post IDs ({len(row['post_ids'])})**\n" + "\n".join(row["post_ids"]))
    return "\n\n".join(parts)


def reconcile_products(cu: CU, rows: list[dict], tasks: list[dict], F: dict, notes: list[str]) -> None:
    print("\n== Product Research ==")
    for row in rows:
        t = match_product(row, tasks)
        if t is None:
            print(f"\n{row['name']}: NOT IN CLICKUP")
            status = STATUS_MAP.get(row["lp"].lower()) or STATUS_MAP.get(row["tt"].lower()) or "researching"
            fields = [{"id": f["id"], "value": f["value"]} for f in product_fields(row, F)]
            created = cu.create_task(PRODUCTS_LIST, row["name"], new_task_description(row), status, fields)
            if created:
                tasks.append(created)
            continue
        print(f"\n{row['name']}  ->  {t['name']} [{t['status']['status']}]")
        if slugify(t["name"]) != slugify(row["name"]):
            notes.append(f"{t['name']}: the sheet now calls it '{row['name']}' (matched by URL). Rename by hand if you want the folder to follow.")
        desc = t.get("markdown_description") or t.get("description") or ""
        have = {video_key(u) for u in clickup.extract_video_links(desc)} | {norm_url(u) for u in links_in(desc)}
        missing = [u for u in row["links"] if video_key(u) not in have and norm_url(u) not in have]
        if missing:
            cu.append_description(t, f"**Adspy – added from the sheet {TODAY} ({len(missing)} links)**\n" + md_links(missing), f"{len(missing)} links")
        missing_ids = [p for p in row["post_ids"] if p not in desc]
        if missing_ids:
            cu.append_description(t, f"**TT Post IDs – added from the sheet {TODAY} ({len(missing_ids)})**\n" + "\n".join(missing_ids), f"{len(missing_ids)} post ids")
        for f in product_fields(row, F):
            current = field_value(t, f["_name"])
            if current is None:
                cu.set_field(t, F[f["_name"]], f["value"], f["_name"])
            elif f["_name"] in ("PDP URL", "Main Competitor", "Competition URL's") and norm_url(str(current)) != norm_url(str(f["value"])):
                notes.append(f"{t['name']}: {f['_name']} is '{current}' in ClickUp, sheet says '{f['value']}' (left as is)")
        want = STATUS_MAP.get(row["lp"].lower())
        if want == "ready to launch" and t["status"]["status"] == "researching":
            cu.set_status(t, "ready to launch")
        elif want and want != t["status"]["status"] and t["status"]["status"] not in ("scaling", "winner", "ready to build", "testing"):
            notes.append(f"{t['name']}: sheet status '{row['lp']}' vs ClickUp '{t['status']['status']}' (left as is)")
        if row["feedback"] and row["feedback"] not in desc:
            cu.append_description(t, f"**Feedback loop (sheet, {TODAY}):** {row['feedback']}", "feedback note")


def reconcile_checklist(cu: CU, blocks: list[dict], tasks: list[dict]) -> None:
    print("\n== Competition Checklist ==")
    for b in blocks:
        name = CHECKLIST_ALIASES.get(b["name"].lower(), b["name"])
        t = next((x for x in tasks if slugify(x["name"]) == slugify(name)), None)
        if t is None:
            print(f"\n{b['name']}: no task (skipped)")
            continue
        desc = t.get("markdown_description") or t.get("description") or ""
        if "Competition Checklist (from the sheet" in desc:
            print(f"\n{b['name']}  ->  {t['name']}: already carried over")
            continue
        have = {norm_url(u) for u in links_in(desc)}
        missing = [u for u in b["links"] if norm_url(u) not in have and "docs.google" not in u]
        print(f"\n{b['name']}  ->  {t['name']}: {len(b['checks'])} checks, {len(missing)} new links, conclusion {'yes' if b['conclusion'] else 'no'}")
        cu.append_description(t, checklist_markdown(b), "competition checklist")


def reconcile_accounts(cu: CU, rows: list[dict], accounts: list[dict], products: list[dict], AF: dict, PF: dict, notes: list[str]) -> None:
    print("\n== Ad Accounts ==")
    by_name = {slugify(t["name"]): t for t in accounts}
    for row in rows:
        t = by_name.get(slugify(row["name"]))
        if t is None:
            print(f"\n{row['name']}: NOT IN CLICKUP")
            fields = [{"id": AF["Ad Account Code"]["id"], "value": row["code"]}] if row["code"] and "Ad Account Code" in AF else []
            if row["emails"] and "Emails Added" in AF:
                fields.append({"id": AF["Emails Added"]["id"], "value": row["emails"]})
            cu.create_task(ACCOUNTS_LIST, row["name"], f"Added from the sheet's TT Ad Accounts tab on {TODAY}. Sheet status: {row['status'] or '-'}.", "ready", fields)
            continue
        print(f"\n{row['name']}  [{t['status']['status']}]")
        if row["dedicated"].lower() in ("testing", "scaling") and "Used For" in AF and field_value(t, "Used For") is None:
            cu.set_field(t, AF["Used For"], option_id(AF["Used For"], row["dedicated"]), "Used For")
        if row["birch"].lower() == "yes" and "BIRCH Automation" in AF and field_value(t, "BIRCH Automation") is None:
            cu.set_field(t, AF["BIRCH Automation"], True, "BIRCH Automation")
        for col, fname in (("requested", "Date Requested"), ("created", "Date Created")):
            day = parse_date(row[col])
            if day and fname in AF and field_value(t, fname) is None:
                cu.set_field(t, AF[fname], to_ms(day), fname)
        if row["code"] and "Ad Account Code" in AF and field_value(t, "Ad Account Code") is None:
            cu.set_field(t, AF["Ad Account Code"], row["code"], "Ad Account Code")
        # products running -> relationship on each product task (the inverse shows on the account)
        if row["products"] and "Ad Accounts" in PF:
            linked_here = {x["id"] for x in (field_value(t, "Products Running") or [])}
            for pname in row["products"]:
                p = next((x for x in products if slugify(x["name"]) == slugify(pname)), None)
                if p is None:
                    notes.append(f"{row['name']}: product '{pname}' on the sheet has no ClickUp task")
                    continue
                if p["id"] in linked_here:
                    continue
                print(f"    link  {p['name']} <-> {row['name']}")
                cu.write("POST", f"/task/{p['id']}/field/{PF['Ad Accounts']['id']}", {"value": {"add": [t["id"]], "rem": []}}, f"link {p['name']}")


def verify(cu: CU, task_ids: list[str], expect_marker: str) -> None:
    ok = 0
    for tid in task_ids:
        t = cu.task(tid)
        if expect_marker in (t.get("markdown_description") or t.get("description") or ""):
            ok += 1
        else:
            print(f"  !! {t['name']}: the appended section is not in the description after the update")
    print(f"verified {ok}/{len(task_ids)} descriptions carry today's additions")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="make the changes (default: print them only)")
    ap.add_argument("--token", default="", help="ClickUp personal token (default: VIDDL_CLICKUP_TOKEN in .env)")
    args = ap.parse_args(argv)
    token = args.token or config.CLICKUP_TOKEN
    if not token:
        raise SystemExit("no ClickUp token: VIDDL_CLICKUP_TOKEN in .env or --token")
    cu = CU(token, args.apply)
    print("mode  :", "APPLY" if args.apply else "dry run (nothing is written)")
    rows, accounts_rows, blocks = sheet_products(), sheet_accounts(), sheet_checklist()
    print(f"sheet : {len(rows)} products, {sum(len(r['links']) for r in rows)} links · {len(accounts_rows)} ad accounts · {len(blocks)} checklist blocks")
    PF, AF = cu.fields(PRODUCTS_LIST), cu.fields(ACCOUNTS_LIST)
    products, accounts = cu.tasks(PRODUCTS_LIST), cu.tasks(ACCOUNTS_LIST)
    print(f"clickup: {len(products)} product tasks, {len(accounts)} ad account tasks")
    notes: list[str] = []
    touched_before = {t["id"] for t in products}
    reconcile_products(cu, rows, products, PF, notes)
    reconcile_checklist(cu, blocks, products)
    reconcile_accounts(cu, accounts_rows, accounts, products, AF, PF, notes)
    if notes:
        print("\n== NOTES (nothing changed for these, decide by hand) ==")
        for n in notes:
            print("  -", n)
    print(f"\n{cu.writes} API writes" if args.apply else "\nDry run finished. Run again with --apply to make these changes.")
    if args.apply:
        appended = [t["id"] for t in products if t["id"] in touched_before and TODAY in (t.get("markdown_description") or "")]
        verify(cu, appended, TODAY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
