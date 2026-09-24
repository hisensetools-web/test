"""python adspy.py <command>. Run `python adspy.py --help`."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config
from .sheet import SheetAccessError, fetch_tab_csv, parse_products, select


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _products(args):
    if args.csv:
        text = Path(args.csv).read_text(encoding="utf-8-sig")
        source = args.csv
    else:
        text = fetch_tab_csv(args.sheet_id, args.gid, args.tab)
        source = f"sheet {args.sheet_id} / '{args.tab}'"
    products = select(parse_products(text, args.product_column, args.link_column), args.only)
    if not products:
        raise SystemExit(f"no products{' matching ' + ', '.join(args.only) if args.only else ''} in {source}")
    return products, source


# --------------------------------------------------------------------------- commands
def cmd_links(args) -> int:
    products, source = _products(args)
    print(f"{source}: {len(products)} products, {sum(len(p.links) for p in products)} links")
    for p in products:
        print(f"\n{p.name}  ->  {config.OUTPUT_ROOT / p.slug}  ({len(p.links)} links)")
        if args.urls:
            for u in p.links:
                print("   ", u)
    return 0


def cmd_download(args) -> int:
    from . import download as dl
    products, source = _products(args)
    out_root = Path(args.out) if args.out else config.OUTPUT_ROOT
    has_ffmpeg = dl.ffmpeg_available()
    print(f"{source}: {len(products)} products, {sum(len(p.links) for p in products)} links -> {out_root}")
    print(f"format: {dl.format_expression(has_ffmpeg)}" + ("" if has_ffmpeg else "   (ffmpeg not found: single-file formats only; install it for merged best video+audio)"))
    downloader = (lambda url, dest: dl.VideoResult(url=url, status="dry-run")) if args.dry_run else \
        dl.ytdlp_downloader(args.cookies, args.cookies_from_browser, quiet=not args.verbose)
    totals = {"downloaded": 0, "exists": 0, "failed": 0, "dry-run": 0}
    failed: list[tuple[str, dl.VideoResult]] = []
    for p in products:
        if not p.links:
            print(f"\n{p.name}: no links, skipped")
            continue
        print(f"\n{p.name}  ({len(p.links)} links) -> {out_root / p.slug}")
        try:
            results = dl.download_product(p, out_root, downloader, max_videos=args.max, force=args.force, dry_run=args.dry_run,
                                          pause_s=args.pause, progress=print)
        except KeyboardInterrupt:
            print("\nstopped; everything fetched so far is in the manifests, run again to continue")
            return 130
        except Exception as e:  # noqa: BLE001  a broken product must not abort the run
            print(f"  ERROR {p.name}: {e}")
            logging.getLogger("adspy").debug("product failed", exc_info=True)
            continue
        for k, v in dl.summarise(results).items():
            totals[k] = totals.get(k, 0) + v
        failed += [(p.name, r) for r in results if r.status == "failed"]
    print(f"\ndone: {totals['downloaded']} downloaded, {totals['exists']} already there, {totals['failed']} failed"
          + (f", {totals['dry-run']} would be fetched" if args.dry_run else ""))
    if not args.dry_run and not args.keep_metadata and config.STRIP_METADATA:
        _strip_all(products, out_root, force=False)
    if failed:
        print("failed links (run again to retry; Instagram usually needs --cookies-from-browser chrome):")
        for name, r in failed[:40]:
            print(f"  {name}: {r.url}  {r.error[:120]}")
    return 0 if not failed or totals["downloaded"] or totals["exists"] else 1


def _strip_all(products, out_root: Path, force: bool) -> dict[str, int]:
    from . import download as dl, metadata
    totals = {"cleaned": 0, "skipped": 0, "failed": 0}
    if not metadata.find_ffmpeg():
        print("\nmetadata NOT removed: ffmpeg not found (winget install Gyan.FFmpeg, reopen the terminal, then `python adspy.py clean`)")
        return totals
    print("\nremoving metadata (stream copy, quality untouched)...")
    for p in products:
        folder = out_root / p.slug
        if not folder.exists():
            continue
        counts = dl.strip_product(p, out_root, force=force, progress=print)
        for k, v in counts.items():
            totals[k] += v
    print(f"metadata: {totals['cleaned']} files cleaned, {totals['skipped']} already clean, {totals['failed']} failed")
    return totals


def cmd_clean(args) -> int:
    products, _ = _products(args)
    out_root = Path(args.out) if args.out else config.OUTPUT_ROOT
    totals = _strip_all(products, out_root, force=args.force)
    if args.verify:
        from . import metadata
        left = 0
        for p in products:
            folder = out_root / p.slug
            for f in (metadata.video_files(folder) if folder.exists() else []):
                t = metadata.tags(f)
                if t:
                    left += 1
                    print(f"  still tagged {p.slug}/{f.name}: {', '.join(f'{k}={v[:40]}' for k, v in t.items())}")
        print(f"verify: {left} files still carry tags" if left else "verify: no tags left on any file")
        return 1 if left else 0
    return 1 if totals["failed"] else 0


def cmd_check(args) -> int:
    from . import download as dl
    try:
        import yt_dlp
        print(f"yt-dlp : {yt_dlp.version.__version__}   (update: pip install -U yt-dlp; TikTok/Instagram change often)")
    except ImportError:
        print("yt-dlp : NOT INSTALLED  ->  pip install -r requirements.txt")
        return 1
    from . import metadata
    ff = metadata.find_ffmpeg()
    print(f"ffmpeg : {ff or 'NOT FOUND (winget install Gyan.FFmpeg, then reopen the terminal; needed for merging and for the metadata removal)'}")
    print(f"strip  : {'metadata removed after every download' if config.STRIP_METADATA else 'OFF (ADSPY_STRIP_METADATA=0)'}")
    print(f"format : {dl.format_expression()}")
    print(f"sheet  : {args.sheet_id}  tab '{args.tab}' (gid {args.gid})  columns '{args.product_column}' / '{args.link_column}'")
    print(f"output : {config.OUTPUT_ROOT}")
    print(f"cookies: {config.COOKIES_FILE or (config.COOKIES_FROM_BROWSER + ' (browser)' if config.COOKIES_FROM_BROWSER else 'none')}")
    try:
        products, _ = _products(args)
        print(f"reads  : OK, {len(products)} products, {sum(len(p.links) for p in products)} links")
    except (SheetAccessError, SystemExit) as e:
        print(f"reads  : FAILED\n{e}")
        return 1
    return 0


# --------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="adspy.py", description="Pull the Adspy links of one Google Sheet tab and download every video (clean, best quality) into a folder per product.")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging and yt-dlp's own output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--csv", help="read this CSV (File > Download > CSV of the tab) instead of fetching the sheet")
        p.add_argument("--sheet-id", default=config.SHEET_ID, help="Google Sheet id (ADSPY_SHEET_ID)")
        p.add_argument("--gid", default=config.SHEET_GID, help="tab gid from the URL (ADSPY_SHEET_GID)")
        p.add_argument("--tab", default=config.SHEET_TAB, help="tab name, used when the gid does not resolve (ADSPY_SHEET_TAB)")
        p.add_argument("--product-column", default=config.PRODUCT_COLUMN)
        p.add_argument("--link-column", default=config.LINK_COLUMN)
        p.add_argument("--only", action="append", metavar="NAME", help="only products whose name contains this (repeatable)")

    p = sub.add_parser("links", help="list the products and how many links each has (--urls prints them)")
    common(p)
    p.add_argument("--urls", action="store_true")
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("download", help="download every link into <out>/<product>/")
    common(p)
    p.add_argument("--out", help=f"output root (default {config.OUTPUT_ROOT})")
    p.add_argument("--max", type=int, metavar="N", help="at most N videos per product (for a quick test)")
    p.add_argument("--force", action="store_true", help="re-download links that already have a file")
    p.add_argument("--dry-run", action="store_true", help="show what would be fetched, download nothing")
    p.add_argument("--pause", type=float, default=None, help=f"seconds between downloads (default {config.PAUSE_S})")
    p.add_argument("--cookies", default="", help="Netscape cookies.txt (ADSPY_COOKIES); Instagram needs a logged-in session")
    p.add_argument("--cookies-from-browser", default="", metavar="BROWSER", help="chrome | edge | firefox ... (ADSPY_COOKIES_FROM_BROWSER)")
    p.add_argument("--keep-metadata", action="store_true", help="skip the metadata removal that runs after the downloads")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("clean", help="remove metadata from every downloaded video (runs automatically after `download`)")
    common(p)
    p.add_argument("--out", help=f"output root (default {config.OUTPUT_ROOT})")
    p.add_argument("--force", action="store_true", help="re-clean files already marked clean")
    p.add_argument("--verify", action="store_true", help="afterwards, list any file that still carries a tag")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("check", help="yt-dlp / ffmpeg present, sheet readable")
    common(p)
    p.set_defaults(func=cmd_check)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except SheetAccessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
