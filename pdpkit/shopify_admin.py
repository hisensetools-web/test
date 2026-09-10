"""Upload the generated images to Shopify via the Admin GraphQL API.

Needs a custom app with the `write_products` and `write_files` scopes. Since January 2026 custom apps
are created in the Shopify Dev Dashboard (dev.shopify.com), which gives a Client ID + Client secret
instead of a token; the Admin API token is minted here with the client credentials grant
(POST /admin/oauth/access_token, grant_type=client_credentials) and lasts 24 hours, so it is cached
and refreshed automatically. Put in .env:
    SHOPIFY_STORE=my-brand.myshopify.com
    SHOPIFY_CLIENT_ID=...
    SHOPIFY_CLIENT_SECRET=...
Legacy admin-created apps (token shpat_... shown once) still work: set SHOPIFY_ADMIN_TOKEN instead.

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

WHOAMI = """
query whoami {
  shop { name myshopifyDomain }
  currentAppInstallation { accessScopes { handle } }
}"""

PRODUCT_BY_HANDLE = """
query productByHandle($handle: String!) {
  productByHandle(handle: $handle) { id title status }
}"""


def mint_token(store: str, client_id: str, client_secret: str, *, scheme: str = "https", cache: Path | None = None) -> str:
    """Client credentials grant for a Dev Dashboard app. Returns a cached token while it has
    more than 10 minutes left; otherwise requests a new 24-hour one and caches it."""
    cache = cache or config.SHOPIFY_TOKEN_CACHE
    now = time.time()
    if cache.exists():
        try:
            saved = json.loads(cache.read_text())
            if saved.get("store") == store and saved.get("client_id") == client_id and saved.get("expires_at", 0) - now > 600:
                return saved["access_token"]
        except (ValueError, KeyError):
            pass
    r = requests.post(f"{scheme}://{store}/admin/oauth/access_token",
                      data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=config.REQUEST_TIMEOUT)
    if r.status_code != 200:
        hint = ""
        if r.status_code in (400, 401, 404) or "application_cannot_be_found" in r.text:
            hint = (" Check SHOPIFY_STORE is the *.myshopify.com domain, the Client ID / secret are from the app's "
                    "Settings page in the Dev Dashboard, the app is installed on this store, and the store and the app "
                    "are in the same Dev Dashboard organization (client credentials only work inside one organization).")
        raise SystemExit(f"Shopify token request failed: HTTP {r.status_code} {r.text[:300]}.{hint}")
    body = r.json()
    token = body["access_token"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"store": store, "client_id": client_id, "access_token": token,
                                 "expires_at": now + int(body.get("expires_in", 86399)), "scope": body.get("scope", "")}))
    try:
        cache.chmod(0o600)
    except OSError:
        pass
    return token


class ShopifyAdmin:
    def __init__(self, store: str | None = None, token: str | None = None, version: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None, scheme: str = "https"):
        self.store = (store or config.SHOPIFY_STORE).replace("https://", "").replace("http://", "").strip("/")
        self.version = version or config.SHOPIFY_API_VERSION
        client_id = client_id if client_id is not None else config.SHOPIFY_CLIENT_ID
        client_secret = client_secret if client_secret is not None else config.SHOPIFY_CLIENT_SECRET
        if not self.store:
            raise SystemExit("SHOPIFY_STORE (your-store.myshopify.com) must be set in .env")
        self.token = token or config.SHOPIFY_ADMIN_TOKEN
        if not self.token and client_id and client_secret:
            self.token = mint_token(self.store, client_id, client_secret, scheme=scheme)
        if not self.token:
            raise SystemExit("set SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (Dev Dashboard app) or SHOPIFY_ADMIN_TOKEN (legacy app) in .env")
        self.endpoint = f"{scheme}://{self.store}/admin/api/{self.version}/graphql.json"
        self.session = requests.Session()
        self.session.headers.update({"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"})

    def whoami(self) -> dict:
        """Shop name + the scopes this token actually has; flags the ones `upload` needs."""
        data = self.gql(WHOAMI)
        scopes = sorted(s["handle"] for s in (data.get("currentAppInstallation") or {}).get("accessScopes", []))
        missing = [s for s in config.SHOPIFY_REQUIRED_SCOPES if s not in scopes]
        return {"shop": data["shop"], "scopes": scopes, "missing": missing}

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
