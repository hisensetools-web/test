"""ClickUp as the product source: one task per product in the Product Research list, the Adspy links in the
task description.

Links are taken from the description wherever they are (any heading), filtered to the video hosts we download
from (TikTok, Instagram) so competition / PDP / PipiAds links are never treated as videos. A task that stores
TikToks as bare numeric video ids with a "prefix each with https://www.tiktok.com/@creator/video/" note gets
its links rebuilt from that prefix. Tasks whose status is in SKIP_STATUSES (cancelled, lesson) are left out; STATUSES (or --status) keeps only the
statuses named, for example only "ready to launch".

API: ClickUp v2, personal token (ClickUp > Settings > Apps > API Token). Read-only: one GET per page of tasks.
"""
from __future__ import annotations

import logging
import re
import time

import requests

from . import config
from .products import Product

log = logging.getLogger("viddownloader.clickup")

API = "https://api.clickup.com/api/v2"
VIDEO_HOSTS = ("tiktok.com", "instagram.com", "facebook.com/reel", "fb.watch", "youtube.com/shorts", "youtu.be")
# scheme optional: ClickUp's plain-text description drops it ("www.tiktok.com/x"); the markdown one keeps it
LINK_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)*(?:tiktok\.com|instagram\.com|facebook\.com|fb\.watch|youtube\.com|youtu\.be)/[^\s\"'<>)\]|]*", re.IGNORECASE)
TIKTOK_PREFIX_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/@[\w.\-]+/video/?", re.IGNORECASE)
BARE_ID_RE = re.compile(r"(?<![\w/])\d{15,20}(?![\w/])")
_TRAILING = ".,;:)]}\\*_"


class ClickUpError(RuntimeError):
    """Token missing/invalid, list not found, or the API unreachable."""


# --------------------------------------------------------------------------- parse
def _is_video(url: str) -> bool:
    """A link to one video, not a profile, hashtag or shop page."""
    u = url.lower()
    if "tiktok.com" in u:
        return "/video/" in u or "vm.tiktok.com/" in u or "vt.tiktok.com/" in u or "tiktok.com/t/" in u
    if "instagram.com" in u or "facebook.com" in u:
        return "/reel/" in u or "/reels/" in u or "/p/" in u or "/videos/" in u
    return "fb.watch/" in u or "youtube.com/shorts/" in u or "youtu.be/" in u


def extract_video_links(text: str) -> list[str]:
    """Every TikTok / Instagram video link in the text, in order, https:// added when missing, duplicates removed.
    Bare TikTok video ids are turned into links when the text names a `tiktok.com/@creator/video/` prefix."""
    seen: set[str] = set()
    out: list[str] = []
    text = text or ""
    for m in LINK_RE.finditer(text):
        url = m.group(0).rstrip(_TRAILING).replace("\\_", "_").replace("\\&", "&")
        if url.lower().startswith("http://"):
            url = "https://" + url[7:]
        elif not url.lower().startswith("https://"):
            url = "https://" + url
        if _is_video(url) and url not in seen:
            seen.add(url)
            out.append(url)
    prefix = TIKTOK_PREFIX_RE.search(text)
    if prefix:
        base = prefix.group(0).rstrip("/") + "/"
        out = [u for u in out if u.rstrip("/") + "/" != base]        # the prefix itself is not a video
        for vid in BARE_ID_RE.findall(text):
            url = base + vid
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def task_to_product(task: dict) -> Product:
    text = task.get("markdown_description") or task.get("description") or task.get("text_content") or ""
    return Product(name=(task.get("name") or "").strip(), links=extract_video_links(text), status=task_status(task))


def task_status(task: dict) -> str:
    s = task.get("status")
    if isinstance(s, dict):
        return (s.get("status") or "").strip().lower()
    return (s or "").strip().lower()


def normalise_statuses(statuses) -> tuple[str, ...]:
    """'Ready To Launch, testing' or ['Ready To Launch'] -> ('ready to launch', 'testing')."""
    if not statuses:
        return ()
    if isinstance(statuses, str):
        statuses = statuses.split(",")
    return tuple(s.strip().lower() for s in statuses if s and s.strip())


def products_from_tasks(tasks: list[dict], skip_statuses: tuple[str, ...] | None = None, statuses=None) -> list[Product]:
    """One product per task. `statuses` (or VIDDL_CLICKUP_STATUSES) keeps only tasks in those statuses; empty means every
    status except the skipped ones (cancelled, lesson)."""
    skip = normalise_statuses(config.CLICKUP_SKIP_STATUSES if skip_statuses is None else skip_statuses)
    keep = normalise_statuses(config.CLICKUP_STATUSES if statuses is None else statuses)
    out: dict[str, Product] = {}
    for t in tasks:
        status = task_status(t)
        if status in skip or (keep and status not in keep) or not (t.get("name") or "").strip():
            continue
        p = task_to_product(t)
        if p.slug in out:                                   # two tasks with the same name: merge the links
            out[p.slug].links.extend(u for u in p.links if u not in out[p.slug].links)
        else:
            out[p.slug] = p
    return list(out.values())


# --------------------------------------------------------------------------- fetch
def fetch_tasks(list_id: str = "", token: str = "", session: requests.Session | None = None, include_closed: bool = True) -> list[dict]:
    """Every task of the list (all pages), with the markdown description."""
    list_id = list_id or config.CLICKUP_LIST_ID
    token = token or config.CLICKUP_TOKEN
    if not token:
        raise ClickUpError("no ClickUp token: put VIDDL_CLICKUP_TOKEN=pk_... in .env (ClickUp > Settings > Apps > API Token)")
    if not list_id:
        raise ClickUpError("no ClickUp list id: put VIDDL_CLICKUP_LIST_ID=<id> in .env (the number in the list's URL)")
    session = session or requests.Session()
    headers = {"Authorization": token, "Accept": "application/json", "User-Agent": config.USER_AGENT}
    tasks: list[dict] = []
    page = 0
    while True:
        params = {"page": page, "include_closed": str(include_closed).lower(), "subtasks": "false",
                  "include_markdown_description": "true", "order_by": "created", "reverse": "true"}
        data = _get(session, f"{API}/list/{list_id}/task", headers, params)
        batch = data.get("tasks") or []
        tasks.extend(batch)
        if data.get("last_page", True) or not batch:
            break
        page += 1
        time.sleep(0.3)
    return tasks


def _get(session: requests.Session, url: str, headers: dict, params: dict) -> dict:
    delay = 2.0
    for attempt in range(config.RETRIES + 1):
        try:
            r = session.get(url, headers=headers, params=params, timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt == config.RETRIES:
                raise ClickUpError(f"could not reach ClickUp: {e}") from e
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 401:
            raise ClickUpError("ClickUp rejected the token (401): check VIDDL_CLICKUP_TOKEN in .env")
        if r.status_code == 404:
            raise ClickUpError("ClickUp list not found (404): check VIDDL_CLICKUP_LIST_ID (the number in the list's URL)")
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == config.RETRIES:
                raise ClickUpError(f"ClickUp returned HTTP {r.status_code} after {config.RETRIES} retries")
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code != 200:
            raise ClickUpError(f"ClickUp returned HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise ClickUpError("ClickUp returned something that is not JSON") from e
    raise ClickUpError("ClickUp: gave up")


def fetch_products(list_id: str = "", token: str = "", session: requests.Session | None = None, statuses=None) -> list[Product]:
    return products_from_tasks(fetch_tasks(list_id, token, session), statuses=statuses)
