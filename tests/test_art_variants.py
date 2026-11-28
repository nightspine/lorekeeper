"""Resized picture copies, immutable caching, the art manifest's ETag and gzip on JSON only (web/art.py, web/app.py)."""

import hashlib
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("PIL")
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from lorekeeper.web import app as web_app  # noqa: E402
from lorekeeper.web import art as store_mod  # noqa: E402

H = "a" * 64


def jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.effect_noise((w, h), 60).convert("RGB").save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    store = store_mod.ArtStore(data)
    big, small = jpeg(1376, 768), jpeg(200, 100)
    ids = {}
    for name, blob in (("big", big), ("small", small)):
        pid = "p" + hashlib.sha256(blob).hexdigest()[:8]
        store.put(H, "ink", {"id": pid, "file": f"{pid}.jpg", "kind": "portrait"}, blob)
        ids[name] = pid
    app = web_app.create_app(data, [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        yield c, store, ids, data, big


def size_of(content: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(content)).size


def test_each_width_is_a_webp_never_wider_than_the_original(env):
    c, store, ids, data, big = env
    for w in (160, 320, 640, 1280):
        r = c.get(f"/api/art/{H}/ink/{ids['big']}", params={"w": w})
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
        assert size_of(r.content) == (w, round(768 * w / 1376))
        assert (data / "art-cache" / H / "ink" / f"{ids['big']}.w{w}.webp").is_file()
    r = c.get(f"/api/art/{H}/ink/{ids['small']}", params={"w": 640})
    assert size_of(r.content) == (200, 100)          # never upscaled


def test_the_original_is_served_unchanged_and_thumb_is_320(env):
    c, store, ids, data, big = env
    assert c.get(f"/api/art/{H}/ink/{ids['big']}").content == big
    r = c.get(f"/api/art/{H}/ink/{ids['big']}", params={"thumb": 1})
    assert size_of(r.content)[0] == 320
    assert c.get(f"/api/art/{H}/ink/{ids['big']}", params={"w": 100}).status_code == 422


def test_headers_and_reuse(env):
    c, store, ids, data, big = env
    for params in ({}, {"w": 160}):
        r = c.get(f"/api/art/{H}/ink/{ids['big']}", params=params)
        assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert r.headers["x-content-type-options"] == "nosniff"
    cached = data / "art-cache" / H / "ink" / f"{ids['big']}.w160.webp"
    m = cached.stat().st_mtime_ns
    c.get(f"/api/art/{H}/ink/{ids['big']}", params={"w": 160})
    assert cached.stat().st_mtime_ns == m            # made once


def test_an_unlisted_id_is_404_even_with_a_cache_file(env):
    c, store, ids, data, big = env
    d = data / "art-cache" / H / "ink"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pdeadbeef.w160.webp").write_bytes(b"planted")
    assert c.get(f"/api/art/{H}/ink/pdeadbeef", params={"w": 160}).status_code == 404
    assert c.get(f"/api/art/{H}/ink/pdeadbeef").status_code == 404


def test_a_picture_that_will_not_decode_serves_the_original(env):
    c, store, ids, data, big = env
    junk = b"not an image at all"
    pid = "p" + hashlib.sha256(junk).hexdigest()[:8]
    store.put(H, "ink", {"id": pid, "file": f"{pid}.jpg"}, junk)
    r = c.get(f"/api/art/{H}/ink/{pid}", params={"w": 320})
    assert r.status_code == 200 and r.content == junk
    # not what ?w= names, so never kept for good: the browser asks again once the server can resize
    assert r.headers["cache-control"] == "no-cache"
    assert c.get(f"/api/art/{H}/ink/{pid}").headers["cache-control"] == "private, max-age=31536000, immutable"


def test_removing_a_picture_or_a_set_clears_its_copies(env):
    c, store, ids, data, big = env
    c.get(f"/api/art/{H}/ink/{ids['big']}", params={"w": 160})
    c.get(f"/api/art/{H}/ink/{ids['small']}", params={"w": 160})
    store.remove(H, "ink", ids["big"])
    d = data / "art-cache" / H / "ink"
    assert not (d / f"{ids['big']}.w160.webp").exists() and (d / f"{ids['small']}.w160.webp").exists()
    assert c.delete(f"/api/art/{H}").status_code == 200
    assert not (data / "art-cache" / H).exists()


def test_the_cache_is_not_counted_as_art(env):
    c, store, ids, data, big = env
    before = store.size(H)
    c.get(f"/api/art/{H}/ink/{ids['big']}", params={"w": 640})
    assert store.size(H) == before


def test_the_manifest_memo_follows_the_file(env):
    c, store, ids, data, big = env
    m = store.manifest(H, "ink")
    m["pictures"] = []
    store.save(H, "ink", m)
    assert store.manifest(H, "ink")["pictures"] == []
    store.manifest(H, "ink")["pictures"].append(1)       # a caller's edit never reaches the memo
    assert store.manifest(H, "ink")["pictures"] == []


def test_gzip_is_for_big_json_only(tmp_path):
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)

    @app.get("/api/_big")
    def big():
        return {"x": "y" * 5000}

    @app.get("/api/_small")
    def small():
        return {"x": 1}
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        r = c.get("/api/_big", headers={"Accept-Encoding": "gzip"})
        assert r.headers["content-encoding"] == "gzip" and r.json() == {"x": "y" * 5000}
        assert "content-encoding" not in c.get("/api/_small", headers={"Accept-Encoding": "gzip"}).headers
        assert "content-encoding" not in c.get("/api/_big", headers={"Accept-Encoding": "identity"}).headers
        assert "content-encoding" not in c.get("/api/_big", headers={"Accept-Encoding": "gzip", "Accept": "text/event-stream"}).headers
