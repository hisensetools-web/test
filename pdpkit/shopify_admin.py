"""Upload the generated images to Shopify via the Admin GraphQL API.

Needs a custom app on your store (Settings > Apps and sales channels > Develop apps) with the
`write_products` and `write_files` scopes. Put in .env:
    SHOPIFY_STORE=my-brand.myshopify.com
    SHOPIFY_ADMIN_TOKEN=shpat_...

Flow: stagedUploadsCreate (one staged target per file) -> POST the bytes to the staged URL ->
productCreateMedia with the returned resourceUrl. If no product id is given, a DRAFT product is
created first with the competitor title, so nothing is ever published by this script.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import time
from pathlib import Path

import requests

from . import config

log = logging.getLogger("pdpkit.shopify")

STAGED_UPLOADS = """
mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""

PRODUCT_CREATE = """
mutation productCreate($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product { id handle title status }
    userErrors { field message }
  }
}"""

PRODUCT_CREATE_MEDIA = """
mutation productCreateMedia($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    media { alt status ... on MediaImage { id } }
    mediaUserErrors { field message }
  }
}"""

PRODUCT_BY_HANDLE = """
query productByHandle($handle: String!) {
  productByHandle(handle: $handle) { id title status }
}"""


class ShopifyAdmin:
    def __init__(self, store: str | None = None, token: str | None = None, version: str | None = None):
        self.store = (store or config.SHOPIFY_STORE).replace("https://", "").strip("/")
        self.token = token or config.SHOPIFY_ADMIN_TOKEN
        self.version = version or config.SHOPIFY_API_VERSION
        if not self.store or not self.token:
            raise SystemExit("SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN must be set in .env for `upload`")
        self.endpoint = f"https://{self.store}/admin/api/{self.version}/graphql.json"
        self.session = requests.Session()
        self.session.headers.update({"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"})

    def gql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(config.REQUEST_RETRIES):
            r = self.session.post(self.endpoint, json={"query": query, "variables": variables or {}}, timeout=config.REQUEST_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                log.warning("Shopify %s; retry in %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(body['errors'])[:800]}")
            return body["data"]
        raise RuntimeError("Shopify GraphQL: too many retries")

    # -- products -------------------------------------------------------------
    def find_product(self, handle: str) -> dict | None:
        return self.gql(PRODUCT_BY_HANDLE, {"handle": handle}).get("productByHandle")

    def create_draft_product(self, title: str, description_html: str = "", vendor: str = "", product_type: str = "", tags: list[str] | None = None) -> dict:
        product = {"title": title, "status": "DRAFT", "descriptionHtml": description_html}
        if vendor:
            product["vendor"] = vendor
        if product_type:
            product["productType"] = product_type
        if tags:
            product["tags"] = tags
        data = self.gql(PRODUCT_CREATE, {"product": product})["productCreate"]
        if data["userErrors"]:
            raise RuntimeError(f"productCreate: {data['userErrors']}")
        return data["product"]

    # -- media ----------------------------------------------------------------
    def staged_upload(self, path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        size = path.stat().st_size
        data = self.gql(STAGED_UPLOADS, {"input": [{
            "resource": "IMAGE", "filename": path.name, "mimeType": mime, "httpMethod": "POST", "fileSize": str(size),
        }]})["stagedUploadsCreate"]
        if data["userErrors"]:
            raise RuntimeError(f"stagedUploadsCreate: {data['userErrors']}")
        target = data["stagedTargets"][0]
        form = [(p["name"], (None, p["value"])) for p in target["parameters"]]
        form.append(("file", (path.name, path.read_bytes(), mime)))
        r = requests.post(target["url"], files=form, timeout=(10, 120))
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"staged upload of {path.name} failed: HTTP {r.status_code} {r.text[:300]}")
        return target["resourceUrl"]

    def attach_images(self, product_id: str, files: list[Path], alt_prefix: str = "") -> list[dict]:
        media = []
        for f in files:
            resource_url = self.staged_upload(f)
            media.append({"originalSource": resource_url, "mediaContentType": "IMAGE", "alt": f"{alt_prefix} {f.stem}".strip()})
            log.info("staged %s", f.name)
        data = self.gql(PRODUCT_CREATE_MEDIA, {"productId": product_id, "media": media})["productCreateMedia"]
        if data["mediaUserErrors"]:
            raise RuntimeError(f"productCreateMedia: {data['mediaUserErrors']}")
        return data["media"]


def upload_folder(folder: Path, *, title: str, handle: str | None = None, product_id: str | None = None,
                  description_html: str = "", vendor: str = "", product_type: str = "", tags: list[str] | None = None,
                  include_competitor: bool = False, dry_run: bool = False) -> dict:
    """Attach every image in `folder` to a product. Creates a draft product when neither
    product_id nor an existing handle matches."""
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not files:
        raise SystemExit(f"no images in {folder}")
    if dry_run:
        print(f"[dry-run] would upload {len(files)} files from {folder} to product {product_id or handle or '(new draft: ' + title + ')'}")
        return {"files": [f.name for f in files], "product": product_id}
    admin = ShopifyAdmin()
    product = None
    if product_id:
        product = {"id": product_id if product_id.startswith("gid://") else f"gid://shopify/Product/{product_id}", "title": title}
    elif handle:
        product = admin.find_product(handle)
    if not product:
        product = admin.create_draft_product(title, description_html, vendor, product_type, tags)
        log.info("created DRAFT product %s (%s)", product["title"], product["id"])
    media = admin.attach_images(product["id"], files, alt_prefix=title)
    result = {"product": product, "uploaded": [f.name for f in files], "media": media}
    (folder / "shopify_upload.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
