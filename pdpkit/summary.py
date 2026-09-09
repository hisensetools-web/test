"""product_summary: what is on the competitor page, as Markdown + JSON.

Two layers:
  1. deterministic summary built from PageData (always produced, no API key needed)
  2. an optional Claude pass that rewrites it into a tighter marketing brief
     (angle, hooks, benefits, objections, page structure) when ANTHROPIC_API_KEY is set
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import config
from .scrape import PageData

log = logging.getLogger("pdpkit.summary")

SUMMARY_SYSTEM = """You write product-page briefs for an e-commerce team that tests many dropshipping products.
You are given the raw facts scraped from a competitor's product detail page (PDP): title, prices, variants,
description, headings in page order, bullet points, FAQs, trust lines, reviews, and image alt text.

Write a product_summary in Markdown with exactly these sections:
# <Product name>
## One-line pitch
## Who it is for / core problem
## Main angle and hooks (the promises the page leads with)
## Benefits (customer language, ordered as on the page)
## Features and specs
## Offer (price, compare-at, bundles, discounts, shipping, guarantee)
## Social proof (rating, review count, notable review themes, quotes)
## Objections the page handles (FAQ + reassurance)
## Page structure (section-by-section order of the competitor page, one line each, with the image types used)
## Gaps and opportunities (what the page does badly or misses)

Rules: use only what is in the source; where a fact is absent write "not on page". Keep quotes short.
No preamble, no closing remarks. Markdown only."""


def deterministic_markdown(pd: PageData, manifest: list[dict] | None = None) -> str:
    lines = [f"# {pd.title or pd.handle}", ""]
    lines.append(f"- Source: {pd.url}")
    lines.append(f"- Platform: {pd.platform}" + (f" | Vendor: {pd.vendor}" if pd.vendor else ""))
    price = pd.price and f"{pd.currency} {pd.price}".strip()
    if pd.compare_at_price:
        price += f" (compare at {pd.compare_at_price})"
    lines.append(f"- Price: {price or 'not on page'}")
    if pd.rating or pd.review_count:
        lines.append(f"- Rating: {pd.rating or '?'} / 5 from {pd.review_count or '?'} reviews")
    if pd.options:
        lines.append("- Options: " + "; ".join(f"{o['name']}: {', '.join(map(str, o['values']))}" for o in pd.options))
    if pd.variants:
        lines.append(f"- Variants: {len(pd.variants)} ({sum(1 for v in pd.variants if v.get('available') is False)} sold out)")
    if manifest is not None:
        g = sum(1 for m in manifest if m["kind"] == "gallery")
        lines.append(f"- Images saved: {len(manifest)} ({g} gallery, {len(manifest) - g} page)")
    lines += ["", "## Meta description", pd.meta_description or "not on page", ""]
    lines += ["## Description", pd.description_text or "not on page", ""]
    if pd.headings:
        lines += ["## Headings (page order)"] + [f"- {h}" for h in pd.headings] + [""]
    if pd.bullets:
        lines += ["## Bullet points"] + [f"- {b}" for b in pd.bullets] + [""]
    if pd.trust_lines:
        lines += ["## Trust / offer lines"] + [f"- {t}" for t in pd.trust_lines] + [""]
    if pd.cta_texts:
        lines += ["## Calls to action"] + [f"- {t}" for t in pd.cta_texts] + [""]
    if pd.faqs:
        lines += ["## FAQ"] + [f"- **{f['q']}** {f['a']}" for f in pd.faqs] + [""]
    if pd.review_snippets:
        lines += ["## Review snippets"] + [f"- {r}" for r in pd.review_snippets] + [""]
    if pd.variants:
        lines += ["## Variants", "| title | price | compare at | available |", "|---|---|---|---|"]
        lines += [f"| {v.get('title')} | {v.get('price')} | {v.get('compare_at_price') or ''} | {v.get('available')} |" for v in pd.variants]
        lines.append("")
    alts = [i.alt for i in pd.images if i.alt]
    if alts:
        lines += ["## Image alt texts"] + [f"- {a}" for a in dict.fromkeys(alts)] + [""]
    return "\n".join(lines)


def source_pack(pd: PageData, manifest: list[dict] | None) -> str:
    """Compact JSON of the facts handed to Claude (body_text trimmed)."""
    d = pd.to_dict()
    d["body_text"] = d["body_text"][:12000]
    d["images"] = [{"kind": i["kind"], "alt": i["alt"]} for i in d["images"] if i["alt"]][:40]
    if manifest is not None:
        d["images_saved"] = len(manifest)
    return json.dumps(d, ensure_ascii=False, indent=1)


def claude_markdown(pd: PageData, manifest: list[dict] | None) -> str | None:
    """Rewrite the facts into a marketing brief. Returns None when Claude is not configured or fails."""
    if not config.ANTHROPIC_ENABLED:
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; using the deterministic summary")
        return None
    client = anthropic.Anthropic()
    try:
        with client.messages.stream(
            model=config.ANTHROPIC_MODEL,
            max_tokens=16000,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": "Scraped PDP facts (JSON):\n\n" + source_pack(pd, manifest)}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIError as e:
        log.warning("Claude summary failed (%s); using the deterministic summary", e)
        return None
    if msg.stop_reason == "refusal":
        log.warning("Claude declined the summary; using the deterministic summary")
        return None
    return "".join(b.text for b in msg.content if b.type == "text").strip() or None


def write_summary(pd: PageData, out_dir: Path, manifest: list[dict] | None = None, use_claude: bool = True) -> Path:
    """Writes product_summary.md (+ product_summary.json with the raw facts). Returns the .md path."""
    base = deterministic_markdown(pd, manifest)
    polished = claude_markdown(pd, manifest) if use_claude else None
    md = polished + "\n\n---\n\n<details><summary>Raw scrape</summary>\n\n" + base + "\n</details>\n" if polished else base
    md_path = out_dir / "product_summary.md"
    md_path.write_text(md, encoding="utf-8")
    (out_dir / "product_summary.json").write_text(json.dumps(pd.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return md_path
