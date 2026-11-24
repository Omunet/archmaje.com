#!/usr/bin/env python3
"""Download the latest Instagram posts for the ArchMAJE static pages.

Graph host defaults to graph.facebook.com (Instagram API with Facebook Login /
Unified Login, long-lived system-user tokens). Override without code changes:

  IG_GRAPH_BASE     default https://graph.facebook.com
                    use https://graph.instagram.com for Instagram Login tokens
  IG_GRAPH_VERSION  default v27.0

Tokens are read from the environment and never written to logs.
One account failing leaves its previous files in place; the others still update.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH_BASE_DEFAULT = "https://graph.facebook.com"
GRAPH_VERSION_DEFAULT = "v27.0"
MEDIA_FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"
CHILD_FIELDS = "id,media_type,media_url,thumbnail_url"
POST_LIMIT = 3
CAPTION_LIMIT = 140
IMAGE_MAX_BYTES = 20 * 1024 * 1024
JSON_MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "archmaje-instagram-sync"

ACCOUNTS = (
    {
        "slug": "archmaje",
        "ig_user_id": "17841406891841663",
        "token_env": "IG_TOKEN_ARCHMAJE",
    },
    {
        "slug": "maudespinasse",
        "ig_user_id": "17841431336866794",
        "token_env": "IG_TOKEN_MAUDESPINASSE",
    },
    {
        "slug": "m4je",
        "ig_user_id": "17841462694542754",
        "token_env": "IG_TOKEN_M4JE",
    },
)

IMAGE_NAME = re.compile(r"^\d+\.(?:jpg|jpeg|png|webp|gif)$")
MEDIA_ID = re.compile(r"^\d{5,30}$")
PARTIAL_SUFFIX = ".partial"
EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


class AccountError(Exception):
    """A single account could not be refreshed. Previous files stay."""


class Response:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body


def redact(text: str, secrets: list[str]) -> str:
    redacted = re.sub(r"(access_token=)[^&\s\"']+", r"\1[REDACTED]", text)
    redacted = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", redacted)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def short_caption(text: str, limit: int = CAPTION_LIMIT) -> str:
    cleaned = re.sub(r"[\u0000-\u001f\u007f]+", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[: limit - 1].rsplit(" ", 1)[0].rstrip(" .,;:-–—")
    if not cut or len(cut) < limit // 2:
        cut = cleaned[: limit - 1].rstrip()
    return cut + "…"


def valid_permalink(url: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme != "https" or parsed.netloc not in {"www.instagram.com", "instagram.com"}:
        return False
    return parsed.path.startswith(("/p/", "/reel/", "/tv/"))


def assert_public_https(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        raise AccountError("image URL must be https")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        raise AccountError("refusing local image host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise AccountError("refusing non-public image host")


def urllib_get(url: str, headers: dict, timeout: int = 30) -> Response:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return Response(resp.status, dict(resp.headers.items()), resp.read(IMAGE_MAX_BYTES + 1))
    except urllib.error.HTTPError as exc:
        body = exc.read(IMAGE_MAX_BYTES + 1) if exc.fp else b""
        hdrs = dict(exc.headers.items()) if exc.headers else {}
        return Response(exc.code, hdrs, body)
    except urllib.error.URLError as exc:
        raise AccountError("network error: " + redact(str(exc.reason), [])) from None


def graph_settings(env: dict) -> tuple[str, str]:
    base = (env.get("IG_GRAPH_BASE") or GRAPH_BASE_DEFAULT).rstrip("/")
    version = (env.get("IG_GRAPH_VERSION") or GRAPH_VERSION_DEFAULT).strip("/")
    return base, version


def auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def graph_get(http_get, base: str, version: str, path: str, query: dict, token: str, secrets: list[str]) -> dict:
    qs = urllib.parse.urlencode(query)
    url = f"{base}/{version}/{path.lstrip('/')}?{qs}"
    if token and token in url:
        raise AccountError("refusing to put the token in the request URL")
    try:
        response = http_get(url, auth_headers(token))
    except AccountError:
        raise
    except Exception as exc:
        raise AccountError("request failed: " + redact(str(exc), secrets)) from None
    body_text = response.body[:JSON_MAX_BYTES].decode("utf-8", "replace")
    if response.status != 200:
        detail = redact(body_text[:300], secrets).replace("\n", " ")
        raise AccountError(f"HTTP {response.status} {detail}")
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise AccountError("response was not JSON") from exc
    if not isinstance(payload, dict):
        raise AccountError("unexpected JSON response")
    return payload


def select_image_url(media: dict, fetch_children) -> str | None:
    media_type = media.get("media_type")
    if media_type == "VIDEO":
        return media.get("thumbnail_url") or None
    if media_type == "IMAGE":
        return media.get("media_url") or None
    if media_type == "CAROUSEL_ALBUM":
        cover = media.get("media_url")
        if cover:
            return cover
        for child in fetch_children(str(media.get("id") or "")):
            if not isinstance(child, dict):
                continue
            child_type = child.get("media_type")
            if child_type == "VIDEO" and child.get("thumbnail_url"):
                return child["thumbnail_url"]
            if child.get("media_url") and child_type != "VIDEO":
                return child["media_url"]
            if child.get("thumbnail_url"):
                return child["thumbnail_url"]
        return None
    return None


def extension_for(content_type: str, url: str) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime in EXT_BY_TYPE:
        return EXT_BY_TYPE[mime]
    path = urllib.parse.urlparse(url).path.lower()
    for ext in ("jpeg", "jpg", "png", "webp", "gif"):
        if path.endswith("." + ext):
            return "jpg" if ext == "jpeg" else ext
    if mime.startswith("image/"):
        return "jpg"
    raise AccountError("download was not an image")


def download_image(http_get, url: str, secrets: list[str]) -> tuple[bytes, str]:
    if any(secret and secret in url for secret in secrets):
        raise AccountError("refusing image URL that contains a token")
    assert_public_https(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*"}
    try:
        response = http_get(url, headers)
    except AccountError:
        raise
    except Exception as exc:
        raise AccountError("image download failed: " + redact(str(exc), secrets)) from None
    if response.status != 200:
        raise AccountError(f"image HTTP {response.status}")
    if len(response.body) > IMAGE_MAX_BYTES:
        raise AccountError("image is too large")
    if not response.body:
        raise AccountError("image was empty")
    ext = extension_for(response.headers.get("content-type", ""), url)
    return response.body, ext


def existing_image(img_dir: Path, media_id: str) -> Path | None:
    if not img_dir.is_dir():
        return None
    matches = [
        path
        for path in img_dir.iterdir()
        if path.is_file() and not path.is_symlink() and path.name.startswith(media_id + ".") and IMAGE_NAME.match(path.name)
    ]
    return sorted(matches)[0] if matches else None


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + PARTIAL_SUFFIX)
    tmp.write_bytes(data)
    tmp.replace(path)


def render_json(posts: list[dict]) -> str:
    return json.dumps(posts, ensure_ascii=False, indent=2) + "\n"


def publish(root: Path, slug: str, staged: list[dict]) -> str:
    """Write JSON and images. staged items have permalink, caption, timestamp, filename, and optional bytes."""
    img_dir = root / "assets" / "instagram" / slug
    json_path = root / "assets" / "instagram" / f"{slug}.json"
    img_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    keep = set()
    for item in staged:
        dest = img_dir / item["filename"]
        if item.get("bytes") is not None:
            atomic_write(dest, item["bytes"])
        elif not (dest.is_file() and not dest.is_symlink()):
            raise AccountError("expected image was not saved")
        keep.add(item["filename"])
        entries.append(
            {
                "permalink": item["permalink"],
                "image": f"/assets/instagram/{slug}/{item['filename']}",
                "caption": item["caption"],
                "timestamp": item["timestamp"],
            }
        )
    new_json = render_json(entries)
    previous = json_path.read_text(encoding="utf-8") if json_path.is_file() else None
    if previous != new_json:
        atomic_write(json_path, new_json.encode("utf-8"))
    removed = 0
    for path in list(img_dir.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.endswith(PARTIAL_SUFFIX) or (IMAGE_NAME.match(path.name) and path.name not in keep):
            path.unlink()
            removed += 1
    if previous == new_json and removed == 0 and all(item.get("bytes") is None for item in staged):
        return "unchanged"
    return "updated"


def collect_posts(http_get, base: str, version: str, ig_user_id: str, token: str, secrets: list[str], img_dir: Path) -> tuple[list[dict], bool]:
    """Return (staged posts, api_was_empty)."""
    payload = graph_get(
        http_get,
        base,
        version,
        f"{ig_user_id}/media",
        {"fields": MEDIA_FIELDS, "limit": str(POST_LIMIT)},
        token,
        secrets,
    )
    media = payload.get("data")
    if not isinstance(media, list):
        raise AccountError("media response has no data list")
    if not media:
        return [], True

    def fetch_children(media_id: str) -> list:
        if not MEDIA_ID.fullmatch(media_id):
            return []
        try:
            child_payload = graph_get(
                http_get,
                base,
                version,
                f"{media_id}/children",
                {"fields": CHILD_FIELDS},
                token,
                secrets,
            )
        except AccountError as exc:
            message = str(exc)
            if message.startswith(("HTTP 401", "HTTP 403")) or message.startswith(("request failed", "network error")):
                raise
            return []
        children = child_payload.get("data")
        return children if isinstance(children, list) else []

    staged = []
    for item in media[:POST_LIMIT]:
        if not isinstance(item, dict):
            continue
        media_id = str(item.get("id") or "")
        permalink = item.get("permalink") or ""
        if not MEDIA_ID.fullmatch(media_id) or not valid_permalink(permalink):
            continue
        image_url = select_image_url(item, fetch_children)
        if not image_url:
            continue
        current = existing_image(img_dir, media_id)
        if current is not None:
            filename = current.name
            blob = None
        else:
            blob, ext = download_image(http_get, image_url, secrets)
            filename = f"{media_id}.{ext}"
        timestamp = item.get("timestamp") if isinstance(item.get("timestamp"), str) else ""
        staged.append(
            {
                "permalink": permalink,
                "caption": short_caption(item.get("caption") or ""),
                "timestamp": timestamp,
                "filename": filename,
                "bytes": blob,
            }
        )
    return staged, False


def update_account(root: Path, account: dict, env: dict, http_get, log) -> str:
    slug = account["slug"]
    token = env.get(account["token_env"]) or ""
    secrets = [token] if token else []
    if not token:
        log(f"::warning::{slug}: kept previous data (secret {account['token_env']} is not set)")
        return "kept"
    base, version = graph_settings(env)
    img_dir = root / "assets" / "instagram" / slug
    try:
        staged, api_empty = collect_posts(http_get, base, version, account["ig_user_id"], token, secrets, img_dir)
        if not staged and not api_empty:
            log(f"::warning::{slug}: kept previous data (no downloadable image in the latest {POST_LIMIT})")
            return "kept"
        outcome = publish(root, slug, staged)
        log(f"{slug}: {outcome} ({len(staged)})")
        return outcome
    except AccountError as exc:
        log(f"::warning::{slug}: kept previous data ({redact(str(exc), secrets)})")
        return "kept"


def update_all(root: Path, env: dict | None = None, http_get=urllib_get, log=print) -> int:
    env = os.environ if env is None else env
    for account in ACCOUNTS:
        update_account(root, account, env, http_get, log)
    return 0


def main() -> int:
    tokens = [os.environ.get(account["token_env"]) or "" for account in ACCOUNTS]
    try:
        return update_all(Path(__file__).resolve().parents[1])
    except Exception as exc:
        print("Instagram sync failed: " + redact(str(exc), tokens), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
