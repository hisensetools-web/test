"""Build the PDF instructions guide for Fudge (the Shopify page-builder agent).

Inputs: our reference PDP template (any of .md / .txt / .docx / .pdf / .html) and the product
summary. Claude merges the two into a section-by-section build spec; without an API key the
guide is assembled mechanically (template text followed by the summary) so the pipeline
still produces a usable document.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from . import config

log = logging.getLogger("pdpkit.guide")

GUIDE_SYSTEM = """You write build instructions for "Fudge", an AI agent that builds Shopify product pages.
You receive (1) our reference PDP template, which defines the sections, order, tone and rules every one of our
product pages follows, and (2) a product_summary scraped from a competitor's page for the product we are launching.

Produce a complete, self-contained instruction guide that Fudge can follow without seeing either source.
Structure it exactly as:
# PDP build guide: <product name>
## 0. Product snapshot (name, price, compare-at, variants, one-line pitch)
## 1. Page settings (title, URL handle, SEO title + meta description, tags, product type)
## 2. Sections in order
For every section of the template: heading, purpose, the exact copy to use (headline, subhead, body, bullets, CTA text),
which image to place (refer to files as gen_01.jpg, gen_02.jpg ... from the generated set; say what the image should show),
and layout notes. Write the actual copy, not placeholders. Adapt the competitor's angle into our voice; never copy
their sentences verbatim.
## 3. FAQ (questions + answers)
## 4. Reviews to seed (5-8 short, varied, realistic review texts with names and star ratings)
## 5. Compliance / claims check (claims that need softening, anything to avoid)
## 6. Checklist before publish

Rules: follow the template's section order and rules exactly where they exist; use the summary for facts and angle;
mark anything you had to invent with [ASSUMED]. Markdown only, no preamble."""


# --------------------------------------------------------------------------- template reading
def read_template(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt", ".markdown"):
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in (".html", ".htm"):
        from bs4 import BeautifulSoup
        return BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser").get_text("\n", strip=True)
    if suffix == ".docx":
        try:
            import docx  # python-docx, optional
        except ImportError as e:
            raise SystemExit("pip install python-docx to read .docx templates") from e
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # optional
        except ImportError as e:
            raise SystemExit("pip install pypdf to read .pdf templates") from e
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    raise SystemExit(f"unsupported template type: {path.suffix}")


# --------------------------------------------------------------------------- content
def claude_guide(template_text: str, summary_md: str, generated_files: list[str]) -> str | None:
    if not config.ANTHROPIC_ENABLED:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic()
    user = (
        "## Reference PDP template\n\n" + template_text.strip() +
        "\n\n## product_summary\n\n" + summary_md.strip() +
        "\n\n## Generated image files available\n\n" + ("\n".join(generated_files) if generated_files else "(none yet)")
    )
    try:
        with client.messages.stream(
            model=config.ANTHROPIC_MODEL,
            max_tokens=32000,
            system=GUIDE_SYSTEM,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIError as e:
        log.warning("Claude guide failed (%s); assembling a mechanical guide", e)
        return None
    if msg.stop_reason == "refusal":
        return None
    return "".join(b.text for b in msg.content if b.type == "text").strip() or None


def mechanical_guide(product_name: str, template_text: str, summary_md: str, generated_files: list[str]) -> str:
    return "\n".join([
        f"# PDP build guide: {product_name}",
        "",
        "> Assembled without Claude (no ANTHROPIC_API_KEY). Fudge: follow the template below, using the product summary for facts.",
        "",
        "## Reference PDP template",
        "",
        template_text.strip(),
        "",
        "## Generated images",
        "",
        *([f"- {f}" for f in generated_files] or ["- (none yet)"]),
        "",
        "## product_summary",
        "",
        summary_md.strip(),
    ])


# --------------------------------------------------------------------------- pdf rendering
def _inline(text: str) -> str:
    """Minimal Markdown inline -> ReportLab mini-HTML."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", text)
    return text


def markdown_to_pdf(md: str, pdf_path: Path, title: str) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib import colors

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=4)
    h = {1: ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceBefore=10, spaceAfter=8),
         2: ParagraphStyle("h2", parent=styles["Heading2"], fontSize=14, spaceBefore=10, spaceAfter=6),
         3: ParagraphStyle("h3", parent=styles["Heading3"], fontSize=11.5, spaceBefore=8, spaceAfter=4)}
    quote = ParagraphStyle("quote", parent=body, leftIndent=12, textColor=colors.HexColor("#444444"))

    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm, title=title)
    story = []
    bullets: list[str] = []
    table_rows: list[list[str]] = []

    def flush_bullets():
        nonlocal bullets
        if bullets:
            story.append(ListFlowable([ListItem(Paragraph(_inline(b), body), leftIndent=10) for b in bullets], bulletType="bullet", leftIndent=14))
            bullets = []

    def flush_table():
        nonlocal table_rows
        if table_rows:
            width = max(len(r) for r in table_rows)
            rows = [[Paragraph(_inline(c), body) for c in r + [""] * (width - len(r))] for r in table_rows]
            t = Table(rows, repeatRows=1, hAlign="LEFT")
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story.append(t)
            story.append(Spacer(1, 6))
            table_rows = []

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            flush_bullets()
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            table_rows.append(cells)
            continue
        flush_table()
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush_bullets()
            level = min(len(m.group(1)), 3)
            if level == 1 and story:
                story.append(Spacer(1, 6))
            story.append(Paragraph(_inline(m.group(2)), h[level]))
            continue
        m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)", line)
        if m:
            bullets.append(m.group(1))
            continue
        flush_bullets()
        if line.strip() == "---":
            story.append(PageBreak())
        elif line.startswith(">"):
            story.append(Paragraph(_inline(line.lstrip("> ")), quote))
        elif line.strip():
            story.append(Paragraph(_inline(line), body))
        else:
            story.append(Spacer(1, 4))
    flush_bullets()
    flush_table()
    doc.build(story)
    return pdf_path


def build_guide(product_dir: Path, product_name: str, template_path: Path, *, use_claude: bool = True) -> Path:
    summary_path = product_dir / "product_summary.md"
    if not summary_path.exists():
        raise SystemExit(f"{summary_path} missing; run `grab` first")
    summary_md = summary_path.read_text(encoding="utf-8")
    template_text = read_template(template_path)
    gen_dir = product_dir / config.generated_dir_name(product_name)
    generated = sorted(p.name for p in gen_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")) if gen_dir.exists() else []
    md = (claude_guide(template_text, summary_md, generated) if use_claude else None) or mechanical_guide(product_name, template_text, summary_md, generated)
    slug = config.slugify(product_name)
    (product_dir / f"{slug}_fudge_guide.md").write_text(md, encoding="utf-8")
    return markdown_to_pdf(md, product_dir / f"{slug}_fudge_guide.pdf", f"PDP build guide: {product_name}")
