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


class UploadError(RuntimeError):
    pass


def _put_exact(url: str, data: bytes, headers: dict) -> requests.Response:
    """PUT to a pre-signed URL without letting requests re-quote the query string:
    the signature covers the URL byte for byte."""
    session = requests.Session()
    prep = requests.Request("PUT", "https://placeholder.invalid/", data=data, headers=headers).prepare()
    prep.url = url
    return session.send(prep, timeout=(10, 180))


def upload_bytes(client, data: bytes, content_type: str) -> str:
    """Ask Higgsfield for a pre-signed upload URL, then PUT the bytes to it.
    Tries the header shapes buckets are commonly signed for; raises UploadError with the
    bucket's full answer (it names the signed headers) when every shape is refused."""
    public_url, upload_url = client._get_upload_url(content_type)
    log.debug("upload url host=%s query=%s", upload_url.split("/")[2], upload_url.split("?", 1)[-1][:300])
    attempts = (
        ("Content-Type " + content_type, {"Content-Type": content_type}),
        ("no Content-Type", {}),
        ("Content-Type application/octet-stream", {"Content-Type": "application/octet-stream"}),
    )
    failures = []
    for label, headers in attempts:
        r = _put_exact(upload_url, data, headers)
        if 200 <= r.status_code < 300:
            log.debug("upload accepted with %s", label)
            return public_url
        failures.append(f"[{label}] HTTP {r.status_code}: {r.text[:1500]}")
        if r.status_code not in (400, 403):
            break
    raise UploadError("pre-signed upload refused by the storage bucket:\n" + "\n".join(failures))


def upload_references(client, refs: list[Path]) -> list[str]:
    urls = []
    for p in refs:
        import mimetypes
        ctype = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        url = upload_bytes(client, p.read_bytes(), ctype)
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

    try:
        ref_urls = upload_references(client, refs)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(explain_error(e, model)) from e
    for i, prompt in enumerate(prompts, 1):
        args = build_arguments(prompt, ref_urls, num_images=num_images)
        log.info("prompt %d/%d -> %s", i, len(prompts), model)
        started = time.time()
        try:
            result = client.subscribe(model, arguments=args)
        except Exception as e:  # noqa: BLE001 - one bad prompt must not lose the others
            log.error("generation failed for prompt %d: %s", i, explain_error(e, model))
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


def check_credentials(sample: Path | None = None) -> str:
    """Upload one small image to Higgsfield and return its public URL. Proves HF_KEY works
    and that the network path is open, without spending generation credits."""
    import io

    import higgsfield_client

    client = higgsfield_client.SyncClient(timeout=60.0)
    if sample and sample.exists():
        import mimetypes
        return upload_bytes(client, sample.read_bytes(), mimetypes.guess_type(sample.name)[0] or "image/jpeg")
    # 1x1 PNG so the check needs no file on disk
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    return upload_bytes(client, io.BytesIO(png).getvalue(), "image/png")


def _status_code(e: Exception) -> int | None:
    """HTTP status behind a HiggsfieldClientError (chained from httpx.HTTPStatusError)."""
    cause = e.__cause__
    resp = getattr(cause, "response", None)
    return getattr(resp, "status_code", None)


def explain_error(e: Exception, model: str) -> str:
    """Turn the SDK's errors into the next thing to try."""
    import httpx

    text = str(e)
    if isinstance(e, UploadError):
        return (f"{text}\nHiggsfield accepted the key (it issued the upload URL); the bucket then refused the upload. "
                "Run again with `python pdp.py -v hf-check` and send me the output, it names the headers the URL was signed for.")
    if isinstance(e, (httpx.TransportError, ConnectionError, OSError)):
        return (f"could not reach platform.higgsfield.ai ({type(e).__name__}: {text[:120]}). This is the network, not the key: "
                "check VPN / proxy / firewall, then retry.")
    if isinstance(e, KeyError):
        return f"unexpected response shape from Higgsfield (missing {text}); run with -v and share the log."
    code = _status_code(e)
    low = text.lower()
    if code in (401, 403) or "unauthorized" in low or "invalid api key" in low:
        return (f"Higgsfield rejected the credentials (HTTP {code}: {text[:200]}). HF_KEY must be the API key *id* and *secret* "
                "from https://cloud.higgsfield.ai (Settings > API keys), joined with a colon, not a key from the higgsfield.ai "
                "consumer app. If the key is from cloud.higgsfield.ai, check the account has credits and the key was not revoked. "
                "Alternative: the CLI route (`higgsfield auth login`, then `python pdp.py hf-check --backend cli`).")
    if code == 404 or "not found" in low:
        return (f"model id '{model}' not found on platform.higgsfield.ai. Open the model's page on cloud.higgsfield.ai, copy the id from its "
                "API example, and set HIGGSFIELD_MODEL in .env (and HIGGSFIELD_IMAGE_ARG if its reference field is not 'image_urls').")
    if code in (400, 422) or "validation" in low:
        return (f"Higgsfield rejected the arguments for '{model}': {text[:300]}. Compare with the model's API example on cloud.higgsfield.ai; "
                "adjust HIGGSFIELD_IMAGE_ARG / HIGGSFIELD_ASPECT / HIGGSFIELD_RESOLUTION or drop fields with HIGGSFIELD_EXTRA_ARGS.")
    if code == 402 or "credit" in low or "balance" in low:
        return "Higgsfield reports no credits left on this key."
    return text
