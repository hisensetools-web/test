"""Second Higgsfield backend: the official `higgsfield` CLI on your normal Higgsfield account.

    curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/main/install.sh | sh
    higgsfield auth login          # one-time, opens the browser

Why a second backend: the CLI exposes the consumer catalogue (Nano Banana 2, Seedream 4.5,
GPT Image 2, ...) and two product-specific commands with a backend prompt enhancer:

    higgsfield product-photoshoot create --mode product_shot --prompt "..." --image ref.jpg --count 3
    higgsfield generate create nano_banana_2 --prompt "..." --image a.jpg --image b.jpg --wait --json

Reference images are passed as local paths; the CLI uploads them itself. Result URLs are read
from the JSON (or plain) output and downloaded into <product name>_shopify_PDP_imgs/.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .higgsfield import IMAGE_SUFFIXES, download, pick_references

log = logging.getLogger("pdpkit.higgsfield_cli")

PHOTOSHOOT_MODES = (
    "product_shot", "lifestyle_scene", "closeup_product_with_person", "moodboard_pin", "hero_banner",
    "social_carousel", "ad_creative_pack", "virtual_model_tryout", "conceptual_product", "restyle",
)
_URL = re.compile(r"https?://[^\s\"'<>\\)\]]+")
_AUTH_HINT = ("not authenticated", "session expired", "stored credentials are for")


def cli_path() -> str | None:
    return shutil.which(config.HIGGSFIELD_CLI)


def run(args: list[str], timeout_s: int = 1200) -> subprocess.CompletedProcess:
    exe = cli_path()
    if not exe:
        raise SystemExit(f"'{config.HIGGSFIELD_CLI}' not on PATH. Install: curl -fsSL https://raw.githubusercontent.com/higgsfield-ai/cli/main/install.sh | sh"
                         "  (then `higgsfield auth login`). Or use the API backend with HF_KEY.")
    cmd = [exe, *args]
    log.info("$ %s", " ".join(_quote(a) for a in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        low = out.lower()
        if any(h in low for h in _AUTH_HINT):
            raise SystemExit("Higgsfield CLI is not logged in: run `higgsfield auth login` once, then retry.")
        if "unknown params" in low or "invalid values" in low:
            raise SystemExit(f"Higgsfield rejected a flag: {out.strip()[:400]}\n"
                             f"Run `higgsfield model get {config.HIGGSFIELD_CLI_MODEL} --json` to see the accepted params, then adjust "
                             "HIGGSFIELD_CLI_MODEL / HIGGSFIELD_ASPECT / HIGGSFIELD_CLI_EXTRA in .env.")
        raise RuntimeError(f"higgsfield exited {proc.returncode}: {out.strip()[:600]}")
    return proc


def _quote(a: str) -> str:
    return f'"{a}"' if " " in a else a


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    elif isinstance(obj, str):
        yield obj


def extract_urls(text: str) -> list[str]:
    """Image URLs from `--json` output (any string field) or from plain stdout lines."""
    found: list[str] = []
    try:
        data = json.loads(text)
        found = [s for s in _walk(data) if s.startswith("http")]
    except ValueError:
        found = _URL.findall(text)
    keep = []
    for u in found:
        low = u.lower().split("?", 1)[0]
        if low.endswith(IMAGE_SUFFIXES) or "cdn.higgsfield" in low or "/result" in low or "/output" in low:
            keep.append(u)
    return list(dict.fromkeys(keep))


def build_command(prompt: str, refs: list[Path], *, num_images: int | None = None, model: str | None = None,
                  photoshoot_mode: str | None = None) -> list[str]:
    n = num_images or config.HIGGSFIELD_NUM_IMAGES
    extra = config.HIGGSFIELD_CLI_EXTRA.split() if config.HIGGSFIELD_CLI_EXTRA else []
    if photoshoot_mode:
        if photoshoot_mode not in PHOTOSHOOT_MODES:
            raise SystemExit(f"unknown photoshoot mode '{photoshoot_mode}'; one of {', '.join(PHOTOSHOOT_MODES)}")
        cmd = ["product-photoshoot", "create", "--mode", photoshoot_mode, "--prompt", prompt, "--count", str(n)]
        for r in refs:
            cmd += ["--image", str(r)]
        return cmd + extra
    cmd = ["generate", "create", model or config.HIGGSFIELD_CLI_MODEL, "--prompt", prompt]
    for r in refs:
        cmd += ["--image", str(r)]
    cmd += ["--aspect_ratio", config.HIGGSFIELD_ASPECT, "--resolution", config.HIGGSFIELD_RESOLUTION.lower()]
    # no --num_images here: generic models are one image per job and the CLI rejects unknown params,
    # so generate() repeats the job n times instead
    return cmd + extra + ["--wait", "--json"]


def generate(product_dir: Path, product_name: str, prompts: list[str], *, refs: list[Path] | None = None,
             num_images: int | None = None, model: str | None = None, photoshoot_mode: str | None = None,
             dry_run: bool = False) -> Path:
    out = product_dir / config.generated_dir_name(product_name)
    out.mkdir(parents=True, exist_ok=True)
    refs = refs or pick_references(product_dir / "competitor_imgs")
    if not refs:
        raise SystemExit(f"no reference images in {product_dir / 'competitor_imgs'}; run `grab` first")
    log_path = out / "generation_log.json"
    entries = json.loads(log_path.read_text()) if log_path.exists() else []
    existing = sum(1 for p in out.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)

    n = num_images or config.HIGGSFIELD_NUM_IMAGES
    jobs = [(p, 1) for p in prompts] if photoshoot_mode else [(p, k) for p in prompts for k in range(1, n + 1)]
    for i, (prompt, k) in enumerate(jobs, 1):
        cmd = build_command(prompt, refs, num_images=num_images, model=model, photoshoot_mode=photoshoot_mode)
        if dry_run:
            if k == 1:
                print(config.HIGGSFIELD_CLI, " ".join(_quote(a) for a in cmd), "" if photoshoot_mode else f"   (x{n})")
            continue
        started = time.time()
        try:
            proc = run(cmd)
        except RuntimeError as e:
            log.error("prompt %d failed: %s", i, e)
            entries.append({"backend": "cli", "prompt": prompt, "command": cmd, "error": str(e), "at": datetime.now(timezone.utc).isoformat()})
            log_path.write_text(json.dumps(entries, indent=2))
            continue
        urls = extract_urls(proc.stdout) or extract_urls(proc.stderr)
        if not urls:
            log.warning("no image URLs in CLI output for prompt %d:\n%s", i, proc.stdout[:800])
        saved = []
        for url in urls:
            existing += 1
            path = download(url, out / f"gen_{existing:02d}.jpg")
            saved.append(path.name)
            log.info("saved %s", path.name)
        entries.append({"backend": "cli", "prompt": prompt, "command": cmd, "references": [p.name for p in refs], "result_urls": urls,
                        "files": saved, "seconds": round(time.time() - started, 1), "at": datetime.now(timezone.utc).isoformat()})
        log_path.write_text(json.dumps(entries, indent=2))
    return out


def check() -> str:
    """`higgsfield account status`; returns its output or raises SystemExit with the fix."""
    proc = run(["account", "status"], timeout_s=60)
    return (proc.stdout or proc.stderr).strip()
