"""Paths, .env loading and settings for the PDP cloning pipeline (pdp.py).

Reuses the tracker's minimal .env loader so one .env file serves both tools.
Every setting is optional except where a command says otherwise; a missing key
only disables the command that needs it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from earlyscale.config import ROOT, load_dotenv  # noqa: F401  (loads .env on import)

load_dotenv()

# Where every product gets its own folder: pdp_output/<product-slug>/
OUTPUT_ROOT = Path(os.environ.get("PDP_OUTPUT_DIR", ROOT / "pdp_output"))

REQUEST_TIMEOUT = (10, 45)
REQUEST_RETRIES = 3
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36",
)
MIN_IMAGE_BYTES = int(os.environ.get("PDP_MIN_IMAGE_BYTES", "8000"))   # skip icons / badges / pixels
MAX_IMAGES = int(os.environ.get("PDP_MAX_IMAGES", "80"))
CHROMIUM_PATH = os.environ.get("META_CHROMIUM_PATH", "").strip()   # shared with the tracker; empty = Playwright's own Chromium

# --- Claude (product_summary polish + guide text) ---------------------------
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
ANTHROPIC_ENABLED = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

# --- Higgsfield Cloud API ---------------------------------------------------
# Credentials: HF_KEY="key:secret" (or HF_API_KEY + HF_API_SECRET), from https://cloud.higgsfield.ai
HIGGSFIELD_MODEL = os.environ.get("HIGGSFIELD_MODEL", "bytedance/seedream/v4/edit")
HIGGSFIELD_IMAGE_ARG = os.environ.get("HIGGSFIELD_IMAGE_ARG", "image_urls")   # arg that carries reference URLs
HIGGSFIELD_MAX_REFS = int(os.environ.get("HIGGSFIELD_MAX_REFS", "6"))
HIGGSFIELD_ASPECT = os.environ.get("HIGGSFIELD_ASPECT", "1:1")
HIGGSFIELD_RESOLUTION = os.environ.get("HIGGSFIELD_RESOLUTION", "2K")
HIGGSFIELD_NUM_IMAGES = int(os.environ.get("HIGGSFIELD_NUM_IMAGES", "4"))
HIGGSFIELD_EXTRA_ARGS = os.environ.get("HIGGSFIELD_EXTRA_ARGS", "")   # JSON object merged into every request

# --- Shopify Admin API ------------------------------------------------------
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "").strip()             # e.g. my-brand.myshopify.com
SHOPIFY_ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()  # shpat_... from a custom app
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-07")

# --- Guide ------------------------------------------------------------------
PDP_TEMPLATE = os.environ.get("PDP_TEMPLATE", "").strip()   # default reference PDP template path


def slugify(text: str, max_len: int = 60) -> str:
    """Filesystem-safe folder name from a product title or handle."""
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return (text or "product")[:max_len].rstrip("-")


def product_dir(slug: str) -> Path:
    return OUTPUT_ROOT / slug


def generated_dir_name(product_name: str) -> str:
    """The user-specified folder name for Higgsfield output: '[product name]_shopify_PDP_imgs'."""
    return f"{slugify(product_name)}_shopify_PDP_imgs"
