import json
import os
import re
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def university_domain(host):
    if not host or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
        return None
    parts = host.split(".")
    if parts[-1] == "edu":
        return ".".join(parts[-2:])
    return host


def brand_assets(payload):
    colors = [c for c in payload.get("colors", []) if isinstance(c, dict) and
              re.fullmatch(r"#[0-9a-fA-F]{6}", str(c.get("hex", "")))]
    accent = next((c["hex"] for c in colors if c.get("type") == "accent"), None)
    color = accent or next((c["hex"] for c in colors if c.get("type") == "brand"), None)
    logos = sorted((logo for logo in payload.get("logos", []) if isinstance(logo, dict)),
                   key=lambda logo: {"icon": 0, "symbol": 1, "logo": 2}.get(logo.get("type"), 3))
    image = None
    for logo in logos:
        for variant in logo.get("formats", []):
            src = variant.get("src", "")
            url = urlsplit(src)
            if url.scheme == "https" and url.netloc == "cdn.brandfetch.io":
                image = src
                break
        if image:
            break
    return {"logo": image, "color": color}


def lookup(directory, domain):
    cache = directory / f"brandfetch-{domain}.json"
    try:
        saved = json.loads(cache.read_text())
        if time.time() - saved["at"] < 30 * 86400:
            return saved["brand"]
    except (OSError, ValueError, KeyError):
        saved = {}
    try:
        key = os.environ.get("BRANDFETCH_API_KEY") or (directory / "brandfetch-key").read_text().strip()
        if not key:
            return saved.get("brand", {})
        request = Request(f"https://api.brandfetch.io/v2/brands/domain/{domain}",
                          headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
        with build_opener(NoRedirect()).open(request, timeout=4) as response:
            brand = brand_assets(json.load(response))
        cache.write_text(json.dumps({"at": time.time(), "brand": brand}))
        return brand
    except (OSError, ValueError, TypeError, AttributeError):
        return saved.get("brand", {})
