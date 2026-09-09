"""python pdp.py <command>. Run `python pdp.py --help`."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _product_dir(slug_or_path: str) -> Path:
    p = Path(slug_or_path)
    if p.exists() and p.is_dir():
        return p
    d = config.product_dir(config.slugify(slug_or_path))
    if not d.exists():
        raise SystemExit(f"no product folder for '{slug_or_path}' (looked in {d}); run `grab <url>` first")
    return d


def _product_name(product_dir: Path, override: str | None = None) -> str:
    if override:
        return override
    facts = product_dir / "product_summary.json"
    if facts.exists():
        data = json.loads(facts.read_text(encoding="utf-8"))
        return data.get("title") or data.get("handle") or product_dir.name
    return product_dir.name


def _prompts(args) -> list[str]:
    prompts = list(args.prompt or [])
    if args.prompt_file:
        text = Path(args.prompt_file).read_text(encoding="utf-8")
        prompts += [p.strip() for p in text.split("\n\n") if p.strip()]   # blank-line separated prompts
    if not prompts:
        raise SystemExit("give at least one --prompt or a --prompt-file")
    return prompts


# --------------------------------------------------------------------------- commands
def cmd_grab(args) -> int:
    from . import scrape, summary
    data, out_dir, manifest = scrape.grab(args.url, use_browser=True if args.browser else None)
    md = summary.write_summary(data, out_dir, manifest, use_claude=not args.no_claude)
    print(f"product : {data.title}")
    print(f"folder  : {out_dir}")
    print(f"images  : {len(manifest)} saved to {out_dir / 'competitor_imgs'} ({sum(1 for m in manifest if m['kind'] == 'gallery')} gallery)")
    print(f"summary : {md}")
    if not config.ANTHROPIC_ENABLED and not args.no_claude:
        print("note    : ANTHROPIC_API_KEY not set, summary is the raw-facts version")
    return 0


def cmd_generate(args) -> int:
    from . import higgsfield
    pdir = _product_dir(args.product)
    name = _product_name(pdir, args.name)
    refs = [Path(r) for r in args.ref] if args.ref else None
    out = higgsfield.generate(pdir, name, _prompts(args), refs=refs, num_images=args.num, model=args.model, dry_run=args.dry_run)
    print(f"generated images folder: {out}")
    return 0


def cmd_upload(args) -> int:
    from . import shopify_admin
    pdir = _product_dir(args.product)
    name = _product_name(pdir, args.name)
    folder = Path(args.folder) if args.folder else pdir / config.generated_dir_name(name)
    if not folder.exists():
        raise SystemExit(f"{folder} does not exist; run `generate` first or pass --folder")
    facts = {}
    fp = pdir / "product_summary.json"
    if fp.exists():
        facts = json.loads(fp.read_text(encoding="utf-8"))
    result = shopify_admin.upload_folder(
        folder, title=args.title or name, handle=args.handle, product_id=args.product_id,
        description_html=facts.get("description_html", "") if args.with_description else "",
        vendor=facts.get("vendor", "") if args.with_description else "", product_type=facts.get("product_type", ""),
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        p = result["product"]
        print(f"uploaded {len(result['uploaded'])} images to product {p.get('title')} ({p['id']})")
    return 0


def cmd_guide(args) -> int:
    from . import guide
    pdir = _product_dir(args.product)
    name = _product_name(pdir, args.name)
    template = Path(args.template or config.PDP_TEMPLATE or "")
    if not template.is_file():
        raise SystemExit("pass --template <file> (md/txt/docx/pdf/html) or set PDP_TEMPLATE in .env")
    pdf = guide.build_guide(pdir, name, template, use_claude=not args.no_claude)
    print(f"guide: {pdf}")
    return 0


def cmd_run(args) -> int:
    """grab -> generate -> upload -> guide, stopping at the first missing prerequisite."""
    from . import scrape, summary
    data, out_dir, manifest = scrape.grab(args.url, use_browser=True if args.browser else None)
    summary.write_summary(data, out_dir, manifest, use_claude=not args.no_claude)
    print(f"[1/4] grabbed {len(manifest)} images -> {out_dir}")
    name = args.name or data.title or data.handle
    args.product = str(out_dir)
    if args.prompt or args.prompt_file:
        cmd_generate(args)
        print("[2/4] generated")
        if not args.skip_upload:
            args.folder = None
            args.title = None
            cmd_upload(args)
            print("[3/4] uploaded")
        else:
            print("[3/4] upload skipped")
    else:
        print("[2/4] no --prompt given, generation and upload skipped")
    if args.template or config.PDP_TEMPLATE:
        cmd_guide(args)
        print("[4/4] guide written")
    else:
        print("[4/4] no --template given, guide skipped")
    return 0


def cmd_list(args) -> int:
    root = config.OUTPUT_ROOT
    if not root.exists():
        print(f"nothing yet in {root}")
        return 0
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        comp = d / "competitor_imgs"
        n_comp = sum(1 for p in comp.iterdir() if p.suffix.lower() in (".jpg", ".png", ".webp", ".jpeg")) if comp.exists() else 0
        gens = [g for g in d.iterdir() if g.is_dir() and g.name.endswith("_shopify_PDP_imgs")]
        n_gen = sum(1 for g in gens for p in g.iterdir() if p.suffix.lower() in (".jpg", ".png", ".webp", ".jpeg"))
        guide = "guide" if list(d.glob("*_fudge_guide.pdf")) else "-"
        uploaded = "uploaded" if any((g / "shopify_upload.json").exists() for g in gens) else "-"
        print(f"{d.name:40s} competitor={n_comp:3d} generated={n_gen:3d} {uploaded:9s} {guide}")
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdp.py", description="Clone a competitor PDP: images, summary, Higgsfield renders, Shopify upload, Fudge guide.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def add_product(sp):
        sp.add_argument("product", help="product slug (folder under pdp_output/) or a folder path")
        sp.add_argument("--name", help="override the product name used for folder / Shopify title")

    def add_generate_opts(sp):
        sp.add_argument("--prompt", action="append", help="Higgsfield prompt (repeat for several)")
        sp.add_argument("--prompt-file", help="text file, prompts separated by blank lines")
        sp.add_argument("--ref", action="append", help="explicit reference image path (repeat); default: first gallery images")
        sp.add_argument("--num", type=int, help=f"images per prompt (default {config.HIGGSFIELD_NUM_IMAGES})")
        sp.add_argument("--model", help=f"Higgsfield model id (default {config.HIGGSFIELD_MODEL})")

    def add_upload_opts(sp):
        sp.add_argument("--handle", help="attach to the existing Shopify product with this handle")
        sp.add_argument("--product-id", help="attach to this Shopify product id (number or gid)")
        sp.add_argument("--title", help="title for a new draft product (default: competitor title)")
        sp.add_argument("--with-description", action="store_true", help="also copy the competitor description/vendor onto the new draft")

    s = sub.add_parser("grab", help="download every image on a competitor PDP + write product_summary")
    s.add_argument("url")
    s.add_argument("--browser", action="store_true", help="force a headless Chromium render (JS-heavy pages)")
    s.add_argument("--no-claude", action="store_true", help="skip the Claude rewrite of the summary")
    s.set_defaults(func=cmd_grab)

    s = sub.add_parser("generate", help="Higgsfield images using competitor_imgs as references")
    add_product(s)
    add_generate_opts(s)
    s.add_argument("--dry-run", action="store_true", help="print the request without calling Higgsfield")
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("upload", help="upload the generated images to Shopify (draft product)")
    add_product(s)
    add_upload_opts(s)
    s.add_argument("--folder", help="upload this folder instead of <name>_shopify_PDP_imgs")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("guide", help="PDF build instructions for Fudge from the template + product_summary")
    add_product(s)
    s.add_argument("--template", help="reference PDP template (md/txt/docx/pdf/html); default PDP_TEMPLATE in .env")
    s.add_argument("--no-claude", action="store_true")
    s.set_defaults(func=cmd_guide)

    s = sub.add_parser("run", help="grab -> generate -> upload -> guide in one go")
    s.add_argument("url")
    s.add_argument("--name")
    s.add_argument("--browser", action="store_true")
    s.add_argument("--no-claude", action="store_true")
    add_generate_opts(s)
    add_upload_opts(s)
    s.add_argument("--skip-upload", action="store_true")
    s.add_argument("--template")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("list", help="what has been grabbed / generated / uploaded")
    s.set_defaults(func=cmd_list)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
