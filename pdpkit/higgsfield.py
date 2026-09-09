"""Generate new PDP images with Higgsfield Cloud, using the competitor images as references.

Uses the official `higgsfield-client` SDK (https://cloud.higgsfield.ai). Credentials in .env:
    HF_KEY=<api-key>:<api-secret>        (or HF_API_KEY + HF_API_SECRET)

The model and the argument that carries reference URLs are configurable because Higgsfield
exposes many models (Seedream edit, Soul, Nano Banana, ...) with slightly different argument
names. Defaults: HIGGSFIELD_MODEL=bytedance/seedream/v4/edit, HIGGSFIELD_IMAGE_ARG=image_urls.
Check the model card on cloud.higgsfield.ai and change .env if yours differs.

Output goes to <product dir>/<product name>_shopify_PDP_imgs/ with a generation log.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger("pdpkit.higgsfield")

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def pick_references(competitor_dir: Path, max_refs: int | None = None, prefer: str = "gallery") -> list[Path]:
    """Reference images in a stable order: gallery photos first, then page images."""
    max_refs = max_refs or config.HIGGSFIELD_MAX_REFS
    files = sorted(p for p in competitor_dir.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    gallery = [p for p in files if p.name.startswith("gallery_")]
    page = [p for p in files if not p.name.startswith("gallery_")]
    ordered = gallery + page if prefer == "gallery" else page + gallery
    return ordered[:max_refs]


def upload_references(client, refs: list[Path]) -> list[str]:
    urls = []
    for p in refs:
        url = client.upload_file(str(p))
        log.info("uploaded %s -> %s", p.name, url)
        urls.append(url)
    return urls


def build_arguments(prompt: str, ref_urls: list[str], *, num_images: int | None = None) -> dict:
    args = {
        "prompt": prompt,
        config.HIGGSFIELD_IMAGE_ARG: ref_urls,
        "aspect_ratio": config.HIGGSFIELD_ASPECT,
        "resolution": config.HIGGSFIELD_RESOLUTION,
        "num_images": num_images or config.HIGGSFIELD_NUM_IMAGES,
    }
    if config.HIGGSFIELD_EXTRA_ARGS:
        try:
            args.update(json.loads(config.HIGGSFIELD_EXTRA_ARGS))
        except ValueError:
            log.warning("HIGGSFIELD_EXTRA_ARGS is not valid JSON; ignored")
    return args


def result_image_urls(result: dict) -> list[str]:
    """Higgsfield results carry images under 'images' (list of {url}) or a single 'image'."""
    out = []
    for key in ("images", "output", "results"):
        items = result.get(key)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("url"):
                    out.append(it["url"])
                elif isinstance(it, str) and it.startswith("http"):
                    out.append(it)
    single = result.get("image")
    if isinstance(single, dict) and single.get("url"):
        out.append(single["url"])
    elif isinstance(single, str):
        out.append(single)
    return list(dict.fromkeys(out))


def download(url: str, dest: Path) -> Path:
    r = requests.get(url, timeout=config.REQUEST_TIMEOUT)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()
    ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
    if dest.suffix.lower() not in IMAGE_SUFFIXES:
        dest = dest.with_suffix(ext)
    dest.write_bytes(r.content)
    return dest


def generate(product_dir: Path, product_name: str, prompts: list[str], *, refs: list[Path] | None = None,
             num_images: int | None = None, model: str | None = None, dry_run: bool = False) -> Path:
    """Run every prompt against the same references; save all images to the output folder.
    Returns the output folder. With dry_run the request is printed and nothing is sent."""
    import higgsfield_client  # lazy: only needed for this command

    out = product_dir / config.generated_dir_name(product_name)
    out.mkdir(parents=True, exist_ok=True)
    model = model or config.HIGGSFIELD_MODEL
    refs = refs or pick_references(product_dir / "competitor_imgs")
    if not refs:
        raise SystemExit(f"no reference images in {product_dir / 'competitor_imgs'}; run `grab` first")

    client = higgsfield_client.SyncClient(timeout=180.0)
    log_path = out / "generation_log.json"
    entries = json.loads(log_path.read_text()) if log_path.exists() else []
    existing = sum(1 for p in out.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)

    if dry_run:
        print(json.dumps({"model": model, "references": [p.name for p in refs],
                          "arguments": build_arguments(prompts[0], ["<uploaded-url>"] * len(refs), num_images=num_images)}, indent=2))
        return out

    ref_urls = upload_references(client, refs)
    for i, prompt in enumerate(prompts, 1):
        args = build_arguments(prompt, ref_urls, num_images=num_images)
        log.info("prompt %d/%d -> %s", i, len(prompts), model)
        started = time.time()
        try:
            result = client.subscribe(model, arguments=args)
        except Exception as e:  # noqa: BLE001 - one bad prompt must not lose the others
            log.error("generation failed for prompt %d: %s", i, e)
            entries.append({"prompt": prompt, "model": model, "error": str(e), "at": datetime.now(timezone.utc).isoformat()})
            log_path.write_text(json.dumps(entries, indent=2))
            continue
        urls = result_image_urls(result)
        if not urls:
            log.warning("no image URLs in result for prompt %d: %s", i, json.dumps(result)[:500])
        saved = []
        for url in urls:
            existing += 1
            path = download(url, out / f"gen_{existing:02d}.jpg")
            saved.append(path.name)
            log.info("saved %s", path.name)
        entries.append({"prompt": prompt, "model": model, "references": [p.name for p in refs], "arguments": {k: v for k, v in args.items() if k != config.HIGGSFIELD_IMAGE_ARG},
                        "result_urls": urls, "files": saved, "seconds": round(time.time() - started, 1),
                        "at": datetime.now(timezone.utc).isoformat()})
        log_path.write_text(json.dumps(entries, indent=2))
    return out
