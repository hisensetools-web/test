"""Remove duplicate videos after a run, so running `download` again never grows the folders.

Three kinds are handled, in this order:
1. leftovers: `.part`, `.ytdl`, `.clean.*` and `.temp.*` files from an interrupted run or strip;
2. the same video id saved twice with different extensions in one folder (keeps the largest);
3. byte-identical files (SHA-256, compared after the metadata strip so tags do not hide a match), within a product
   folder and, by default, across product folders: the first product on the sheet keeps the file, the others get a
   `duplicate` row in their manifest pointing at it, which `download` honours on the next run (no refetch).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable

from . import config
from .download import VideoResult, load_manifest, save_manifest
from .metadata import video_files
from .sheet import Product

log = logging.getLogger("adspy.dedup")

LEFTOVER_SUFFIXES = (".part", ".ytdl", ".part-Frag0", ".temp.mp4", ".temp.webm", ".temp.mkv")


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_leftover(p: Path) -> bool:
    n = p.name
    return n.endswith(LEFTOVER_SUFFIXES) or ".clean." in n or ".temp." in n or n.endswith(".part")


def _fmt_mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB"


def dedup(products: list[Product], out_root: Path, across_products: bool | None = None, dry_run: bool = False,
          progress: Callable[[str], None] | None = None) -> dict[str, int]:
    """Returns counts: leftovers, duplicates (files removed), bytes (freed)."""
    say = progress or (lambda s: None)
    across = config.DEDUP_ACROSS if across_products is None else across_products
    counts = {"leftovers": 0, "duplicates": 0, "bytes": 0}
    seen: dict[str, tuple[Product, Path]] = {}        # hash -> first file kept, in sheet order
    retired: set[Path] = set()                         # removed this pass (or would be, in a dry run)

    for product in products:
        folder = out_root / product.slug
        if not folder.exists():
            continue
        manifest = load_manifest(folder)
        changed = False

        # 1. leftovers of interrupted downloads / strips
        for p in sorted(folder.iterdir()):
            if p.is_file() and _is_leftover(p):
                counts["leftovers"] += 1
                counts["bytes"] += p.stat().st_size
                say(f"  leftover    {product.slug}/{p.name}  ({_fmt_mb(p.stat().st_size)})")
                if not dry_run:
                    p.unlink()

        def _retire(dup: Path, kept: Path, kept_product: Product) -> None:
            nonlocal changed
            same_folder = kept.parent == dup.parent
            size = dup.stat().st_size
            counts["duplicates"] += 1
            counts["bytes"] += size
            where = kept.name if same_folder else f"{kept_product.slug}/{kept.name}"
            retired.add(dup)
            say(f"  duplicate   {product.slug}/{dup.name} = {where}  ({_fmt_mb(size)} freed)")
            for r in manifest.values():
                if r.file == dup.name:
                    if same_folder:
                        r.file = kept.name
                    else:
                        r.status, r.file, r.duplicate_of = "duplicate", "", where
                    changed = True
            if not dry_run:
                dup.unlink()

        # 2. same id, two containers
        by_stem: dict[str, list[Path]] = {}
        for p in video_files(folder):
            by_stem.setdefault(p.stem, []).append(p)
        for stem, files in by_stem.items():
            if len(files) > 1:
                files.sort(key=lambda p: (p.suffix.lower() != ".mp4", -p.stat().st_size))
                for dup in files[1:]:
                    _retire(dup, files[0], product)

        # 3. identical content
        for p in sorted(video_files(folder)):
            if p in retired or not p.exists():
                continue
            digest = file_hash(p)
            prior = seen.get(digest)
            if prior is None:
                seen[digest] = (product, p)
            elif prior[1].parent == p.parent or across:
                _retire(p, prior[1], prior[0])
        if changed and not dry_run:
            save_manifest(folder, product, manifest)
    return counts
