#!/usr/bin/env python3
"""Behaviour tests for the Instagram sync. No network, no real tokens."""

from __future__ import annotations

import importlib.util
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("update_instagram", ROOT / "scripts" / "update_instagram.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

TOKEN_A = "TESTTOKEN-archmaje-do-not-log"
TOKEN_B = "TESTTOKEN-maude-do-not-log"
TOKEN_C = "TESTTOKEN-m4je-do-not-log"
BASE = "https://graph.facebook.com"
VERSION = "v27.0"


def png_bytes() -> bytes:
    raw = b"\x00" + bytes((180, 40, 40))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG = png_bytes()


def media_url(ig_id: str) -> str:
    query = urlencode({"fields": mod.MEDIA_FIELDS, "limit": str(mod.POST_LIMIT)})
    return f"{BASE}/{VERSION}/{ig_id}/media?{query}"


def children_url(media_id: str) -> str:
    query = urlencode({"fields": mod.CHILD_FIELDS})
    return f"{BASE}/{VERSION}/{media_id}/children?{query}"


class FakeHTTP:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, url: str, status: int, body, content_type: str = "application/json"):
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode()
        elif isinstance(body, str):
            raw = body.encode()
        else:
            raw = body
        self.routes[url] = mod.Response(status, {"Content-Type": content_type}, raw)

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url not in self.routes:
            raise AssertionError("unexpected URL " + url)
        return self.routes[url]


def env_for(**tokens):
    env = {"IG_GRAPH_BASE": BASE, "IG_GRAPH_VERSION": VERSION}
    env.update(tokens)
    return env


def seed(root: Path, slug: str, media_id: str, caption: str):
    img = root / "assets" / "instagram" / slug
    img.mkdir(parents=True)
    (img / f"{media_id}.jpg").write_bytes(b"old-image")
    payload = [
        {
            "permalink": f"https://www.instagram.com/p/{media_id}/",
            "image": f"/assets/instagram/{slug}/{media_id}.jpg",
            "caption": caption,
            "timestamp": "2020-01-01T00:00:00+0000",
        }
    ]
    (root / "assets" / "instagram" / f"{slug}.json").write_text(mod.render_json(payload), encoding="utf-8")


class UpdateInstagramTest(unittest.TestCase):
    def test_caption_and_permalink(self):
        self.assertEqual(mod.short_caption("  line\none  "), "line one")
        long = "word " * 80
        short = mod.short_caption(long)
        self.assertLessEqual(len(short), mod.CAPTION_LIMIT)
        self.assertTrue(short.endswith("…"))
        self.assertTrue(mod.valid_permalink("https://www.instagram.com/p/ABC123/"))
        self.assertTrue(mod.valid_permalink("https://www.instagram.com/reel/ABC123/"))
        self.assertFalse(mod.valid_permalink("https://example.com/p/ABC123/"))
        self.assertFalse(mod.valid_permalink("javascript:alert(1)"))

    def test_redact_removes_token_forms(self):
        text = f"Bearer {TOKEN_A} access_token={TOKEN_A}&fields=id and raw {TOKEN_A}"
        cleaned = mod.redact(text, [TOKEN_A])
        self.assertNotIn(TOKEN_A, cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_updates_downloads_and_isolates_failures(self):
        arch = mod.ACCOUNTS[0]
        maude = mod.ACCOUNTS[1]
        m4je = mod.ACCOUNTS[2]
        http = FakeHTTP()
        http.add(
            media_url(arch["ig_user_id"]),
            200,
            {
                "data": [
                    {
                        "id": "1111111111",
                        "caption": "Night one\nat the club",
                        "media_type": "IMAGE",
                        "media_url": "https://cdn.example/111.jpg",
                        "permalink": "https://www.instagram.com/p/AAA111/",
                        "timestamp": "2026-11-01T10:00:00+0000",
                    },
                    {
                        "id": "2222222222",
                        "caption": "A clip",
                        "media_type": "VIDEO",
                        "media_url": "https://cdn.example/222.mp4",
                        "thumbnail_url": "https://cdn.example/222.jpg",
                        "permalink": "https://www.instagram.com/reel/BBB222/",
                        "timestamp": "2026-11-02T10:00:00+0000",
                    },
                    {
                        "id": "3333333333",
                        "caption": "Carousel cover",
                        "media_type": "CAROUSEL_ALBUM",
                        "media_url": "https://cdn.example/333.jpg",
                        "permalink": "https://www.instagram.com/p/CCC333/",
                        "timestamp": "2026-11-03T10:00:00+0000",
                    },
                ]
            },
        )
        http.add("https://cdn.example/111.jpg", 200, PNG, "image/png")
        http.add("https://cdn.example/222.jpg", 200, PNG, "image/jpeg")
        http.add("https://cdn.example/333.jpg", 200, PNG, "image/png")
        http.add(
            media_url(maude["ig_user_id"]),
            500,
            {"error": {"message": f"boom {TOKEN_B}"}},
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed(root, "archmaje", "9999999999", "previous")
            seed(root, "maudespinasse", "8888888888", "keep me")
            seed(root, "m4je", "7777777777", "keep me too")
            logs = []
            code = mod.update_all(
                root,
                env_for(IG_TOKEN_ARCHMAJE=TOKEN_A, IG_TOKEN_MAUDESPINASSE=TOKEN_B),
                http,
                logs.append,
            )
            self.assertEqual(code, 0)
            joined = "\n".join(logs)
            for secret in (TOKEN_A, TOKEN_B, TOKEN_C):
                self.assertNotIn(secret, joined)

            posts = json.loads((root / "assets/instagram/archmaje.json").read_text())
            self.assertEqual([p["permalink"] for p in posts], [
                "https://www.instagram.com/p/AAA111/",
                "https://www.instagram.com/reel/BBB222/",
                "https://www.instagram.com/p/CCC333/",
            ])
            self.assertEqual(posts[0]["caption"], "Night one at the club")
            self.assertEqual(posts[0]["image"], "/assets/instagram/archmaje/1111111111.png")
            self.assertEqual(posts[1]["image"], "/assets/instagram/archmaje/2222222222.jpg")
            self.assertFalse((root / "assets/instagram/archmaje/9999999999.jpg").exists())
            self.assertEqual((root / "assets/instagram/archmaje/2222222222.jpg").read_bytes(), PNG)
            video_calls = [url for url, _headers in http.calls if url.endswith("222.mp4")]
            self.assertEqual(video_calls, [])

            kept = json.loads((root / "assets/instagram/maudespinasse.json").read_text())
            self.assertEqual(kept[0]["caption"], "keep me")
            self.assertEqual((root / "assets/instagram/maudespinasse/8888888888.jpg").read_bytes(), b"old-image")
            untouched = json.loads((root / "assets/instagram/m4je.json").read_text())
            self.assertEqual(untouched[0]["caption"], "keep me too")
            self.assertTrue(any("maudespinasse: kept previous data" in line for line in logs))
            self.assertTrue(any("m4je: kept previous data" in line and "not set" in line for line in logs))
            self.assertTrue(any(line.startswith("archmaje: updated") for line in logs))

            for url, headers in http.calls:
                self.assertNotIn(TOKEN_A, url)
                self.assertNotIn(TOKEN_B, url)
                if "graph.facebook.com" in url:
                    self.assertEqual(headers.get("Authorization"), f"Bearer {TOKEN_A}" if arch["ig_user_id"] in url else f"Bearer {TOKEN_B}")
                else:
                    self.assertNotIn("Authorization", headers)

    def test_carousel_children_and_empty_account(self):
        arch = mod.ACCOUNTS[0]
        http = FakeHTTP()
        http.add(
            media_url(arch["ig_user_id"]),
            200,
            {
                "data": [
                    {
                        "id": "4444444444",
                        "caption": "Album",
                        "media_type": "CAROUSEL_ALBUM",
                        "permalink": "https://www.instagram.com/p/DDD444/",
                        "timestamp": "2026-11-04T10:00:00+0000",
                    }
                ]
            },
        )
        http.add(
            children_url("4444444444"),
            200,
            {"data": [{"id": "5555555555", "media_type": "IMAGE", "media_url": "https://cdn.example/child.jpg"}]},
        )
        http.add("https://cdn.example/child.jpg", 200, PNG, "image/png")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = []
            mod.update_account(root, arch, env_for(IG_TOKEN_ARCHMAJE=TOKEN_A), http, logs.append)
            posts = json.loads((root / "assets/instagram/archmaje.json").read_text())
            self.assertEqual(posts[0]["image"], "/assets/instagram/archmaje/4444444444.png")
            self.assertTrue(any(url == children_url("4444444444") for url, _h in http.calls))

        http_empty = FakeHTTP()
        http_empty.add(media_url(arch["ig_user_id"]), 200, {"data": []})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed(root, "archmaje", "9999999999", "gone")
            logs = []
            mod.update_account(root, arch, env_for(IG_TOKEN_ARCHMAJE=TOKEN_A), http_empty, logs.append)
            self.assertEqual(json.loads((root / "assets/instagram/archmaje.json").read_text()), [])
            self.assertFalse((root / "assets/instagram/archmaje/9999999999.jpg").exists())
            self.assertTrue(any("archmaje: updated (0)" in line for line in logs))

    def test_no_image_keeps_previous_and_second_account_still_updates(self):
        arch = mod.ACCOUNTS[0]
        m4je = mod.ACCOUNTS[2]
        http = FakeHTTP()
        http.add(
            media_url(arch["ig_user_id"]),
            200,
            {
                "data": [
                    {
                        "id": "6666666666",
                        "media_type": "VIDEO",
                        "media_url": "https://cdn.example/only.mp4",
                        "permalink": "https://www.instagram.com/reel/EEE666/",
                        "timestamp": "2026-11-05T10:00:00+0000",
                    }
                ]
            },
        )
        http.add(
            media_url(m4je["ig_user_id"]),
            200,
            {
                "data": [
                    {
                        "id": "1212121212",
                        "caption": "Drawing",
                        "media_type": "IMAGE",
                        "media_url": "https://cdn.example/art.png",
                        "permalink": "https://www.instagram.com/p/FFF121/",
                        "timestamp": "2026-11-06T10:00:00+0000",
                    }
                ]
            },
        )
        http.add("https://cdn.example/art.png", 200, PNG, "image/png")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed(root, "archmaje", "9999999999", "stay")
            logs = []
            mod.update_all(
                root,
                env_for(IG_TOKEN_ARCHMAJE=TOKEN_A, IG_TOKEN_M4JE=TOKEN_C),
                http,
                logs.append,
            )
            kept = json.loads((root / "assets/instagram/archmaje.json").read_text())
            self.assertEqual(kept[0]["caption"], "stay")
            self.assertTrue((root / "assets/instagram/archmaje/9999999999.jpg").exists())
            fresh = json.loads((root / "assets/instagram/m4je.json").read_text())
            self.assertEqual(fresh[0]["caption"], "Drawing")
            self.assertEqual(fresh[0]["image"], "/assets/instagram/m4je/1212121212.png")
            joined = "\n".join(logs)
            self.assertNotIn(TOKEN_A, joined)
            self.assertNotIn(TOKEN_C, joined)
            self.assertEqual([url for url, _h in http.calls if url.endswith(".mp4")], [])

    def test_repeat_run_reuses_files(self):
        arch = mod.ACCOUNTS[0]
        http = FakeHTTP()
        http.add(
            media_url(arch["ig_user_id"]),
            200,
            {
                "data": [
                    {
                        "id": "1313131313",
                        "caption": "Same",
                        "media_type": "IMAGE",
                        "media_url": "https://cdn.example/same.png",
                        "permalink": "https://www.instagram.com/p/GGG131/",
                        "timestamp": "2026-11-07T10:00:00+0000",
                    }
                ]
            },
        )
        http.add("https://cdn.example/same.png", 200, PNG, "image/png")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mod.update_account(root, arch, env_for(IG_TOKEN_ARCHMAJE=TOKEN_A), http, lambda _m: None)
            downloads = [url for url, _h in http.calls if url.startswith("https://cdn.example/")]
            self.assertEqual(len(downloads), 1)
            logs = []
            mod.update_account(root, arch, env_for(IG_TOKEN_ARCHMAJE=TOKEN_A), http, logs.append)
            downloads = [url for url, _h in http.calls if url.startswith("https://cdn.example/")]
            self.assertEqual(len(downloads), 1)
            self.assertTrue(any("unchanged" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
