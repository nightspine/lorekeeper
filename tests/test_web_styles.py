"""Picture styles in the web app: the listed styles and the saved ones (styles.json), their previews, and a saved style
used by its slug in a draw and a run. Scripted text and image models; no network."""

import json
import struct

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import art, llm  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402
from lorekeeper.web import auth  # noqa: E402
from lorekeeper.web import styles as styles_mod  # noqa: E402

from .test_web_art import GEMINI, KEY, env, pictures, run_set, short_world  # noqa: E402,F401

NOIR = "Moody film noir, deep shadows, rain-slick streets, one hard light."


@pytest.fixture(autouse=True)
def no_saved_styles():
    yield
    art.SAVED_STYLES.clear()      # the store fills a module table; never leak it into another test
    art.SAVED_STYLE_REFS = {}


def make(e, name="Moody noir", text=NOIR):
    r = e.c.post("/api/styles", json={"name": name, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def listed(e):
    return {s["slug"]: s for s in e.c.get("/api/styles").json()["styles"]}


def test_the_listed_styles_come_first_with_their_static_previews(env):
    make(env)
    got = env.c.get("/api/styles").json()["styles"]
    assert [s["slug"] for s in got[:len(art.STYLES)]] == list(art.STYLES)
    assert all(s["builtin"] and s["text"] == art.STYLES[s["slug"]] for s in got[:len(art.STYLES)])
    for s in got[:len(art.STYLES)]:
        assert s["preview"] in (None, f"/static/styles/{s['slug']}.webp")
        if s["preview"]:
            assert env.c.get(s["preview"]).status_code == 200
    assert got[-1] == {"slug": "moody-noir", "name": "Moody noir", "text": NOIR, "builtin": False, "preview": None,
                       "stale": False, "created": got[-1]["created"], "reference": None, "useReference": False}


def test_create_checks_the_name_and_the_words_and_refuses_a_clash(env):
    for body in ({"name": "", "text": NOIR}, {"name": "x" * 41, "text": NOIR}, {"name": "Noir", "text": ""},
                 {"name": "Noir", "text": "y" * 301}, {"name": "Noir", "text": 5}):
        r = env.c.post("/api/styles", json=body)
        assert r.status_code == 422 and r.json()["code"] == "bad-style", body
    assert env.c.post("/api/styles", json={"name": "INK", "text": NOIR}).status_code == 409      # a listed slug
    make(env, "Moody  noir", "  Moody   film noir. ")
    assert listed(env)["moody-noir"]["text"] == "Moody film noir." and listed(env)["moody-noir"]["name"] == "Moody noir"
    assert env.c.post("/api/styles", json={"name": "moody noir!", "text": NOIR}).status_code == 409
    saved = json.loads((env.data / "styles.json").read_text())
    assert [s["slug"] for s in saved["styles"]] == ["moody-noir"]


def test_a_preview_is_the_shared_scene_in_the_style_and_replaces_the_old_one(env):
    env.key()
    make(env)
    assert env.c.get("/api/styles/moody-noir/preview").status_code == 404
    r = env.c.post("/api/styles/moody-noir/preview", json={"quality": "draft"})
    assert r.status_code == 200, r.text
    got = r.json()
    call = env.image.calls[-1]
    assert call["prompt"] == art.preview_prompt(NOIR) == styles_mod.preview_prompt(NOIR) and NOIR in call["prompt"]
    assert call["prompt"].endswith(art.NO_TEXT) and call["shape"] == art.PREVIEW_SHAPE == "square" and call["references"] == []
    q = llm.nearest_quality("gemini", GEMINI, "draft")
    assert got["quality"] == q and got["price"] == llm.image_price("gemini", GEMINI, q, call["shape"])
    assert got["preview"].startswith("/api/styles/moody-noir/preview?v=") and not got["stale"]
    f = env.c.get(got["preview"])
    assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.headers["cache-control"] == "no-cache"
    first = f.content
    again = env.c.post("/api/styles/moody-noir/preview", json={}).json()
    assert env.c.get(again["preview"]).content != first and len(list((env.data / "styles").iterdir())) == 1


def test_a_preview_draws_the_stored_words_even_when_they_read_like_a_listed_slug(env):
    env.key()
    make(env, "Plain", "ink")
    assert env.c.post("/api/styles/plain/preview", json={}).status_code == 200
    prompt = env.image.calls[-1]["prompt"]
    assert art.STYLES["ink"] not in prompt and prompt == " ".join((art.PREVIEW_SCENE, "ink", art.NO_TEXT))


def test_changing_the_words_marks_the_preview_stale_and_a_rename_keeps_the_slug(env):
    env.key()
    make(env)
    env.c.post("/api/styles/moody-noir/preview", json={})
    r = env.c.put("/api/styles/moody-noir", json={"name": "Night city"})
    assert r.status_code == 200 and r.json()["slug"] == "moody-noir" and r.json()["name"] == "Night city"
    assert not r.json()["stale"]
    r = env.c.put("/api/styles/moody-noir", json={"text": "Neon noir, wet streets."})
    assert r.json()["stale"] and r.json()["preview"] and art.style_sentence("moody-noir") == "Neon noir, wet streets."
    assert not env.c.post("/api/styles/moody-noir/preview", json={}).json()["stale"]
    assert env.c.put("/api/styles/moody-noir", json={"text": ""}).status_code == 422
    assert env.c.put("/api/styles/ink", json={"name": "Mine"}).status_code == 409
    assert env.c.put("/api/styles/nope", json={"name": "Mine"}).status_code == 404


def test_a_refused_preview_says_why_and_keeps_the_old_one(env):
    env.key()
    make(env)
    old = env.c.post("/api/styles/moody-noir/preview", json={}).json()["preview"]
    env.image.refuse = [lambda prompt, n: True]
    r = env.c.post("/api/styles/moody-noir/preview", json={})
    assert r.status_code == 422 and r.json()["code"] == "refused" and "safety" in r.json()["detail"]
    assert listed(env)["moody-noir"]["preview"] == old and env.c.get(old).status_code == 200


def test_a_preview_needs_an_image_key_a_saved_style_and_a_known_model(env):
    make(env)
    env.image = None
    assert env.c.post("/api/styles/moody-noir/preview", json={}).json()["code"] == "no-image-key"
    assert env.c.post("/api/styles/ink/preview", json={}).status_code == 409
    assert env.c.post("/api/styles/nope/preview", json={}).status_code == 404
    assert env.c.post("/api/styles/moody-noir/preview", json={"provider": "claude"}).json()["code"] == "bad-provider"
    assert env.c.post("/api/styles/moody-noir/preview", json={"provider": "gemini", "model": "x"}).json()["code"] == "bad-model"
    assert env.c.post("/api/styles/moody-noir/preview", json={"quality": "huge"}).json()["code"] == "bad-config"


def test_delete_removes_the_style_and_its_preview_but_keeps_its_art_sets_drawing_in_its_words(env):
    env.key()
    make(env)
    env.c.post("/api/styles/moody-noir/preview", json={})
    run_set(env, style="moody-noir", portraits=False)
    n = len(pictures(env, "moody-noir"))
    assert n and env.c.delete("/api/styles/moody-noir").status_code == 200
    assert "moody-noir" not in listed(env) and not list((env.data / "styles").iterdir())
    assert env.c.get("/api/styles/moody-noir/preview").status_code == 404
    assert len(pictures(env, "moody-noir")) == n
    assert art.style_sentence("moody-noir") == NOIR          # a new picture in that set still says the same
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "moody-noir"}).json()
    assert NOIR in got["prompt"]
    assert env.c.delete("/api/styles/moody-noir").status_code == 404
    assert env.c.delete("/api/styles/ink").status_code == 409
    make(env)                                                 # the name is free again


def test_a_saved_style_draws_by_its_slug_in_the_sheet(env):
    env.key()
    make(env)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "moody-noir"}).json()
    assert got["slug"] == "moody-noir" and got["style"] == NOIR and NOIR in got["prompt"]
    assert got["styles"]["moody-noir"] == NOIR and got["styles"]["ink"] == art.STYLES["ink"]
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "moody-noir",
                                                         "prompt": got["prompt"]})
    assert r.status_code == 200, r.text
    m = json.loads((env.data / "art" / env.hash / "moody-noir" / "art.json").read_text())
    assert m["style"] == "moody-noir" and [p["id"] for p in m["pictures"]] == [r.json()["id"]]
    # the words changed: the next prompt in the same set says the new ones, and the set is never started over
    env.c.put("/api/styles/moody-noir", json={"text": "Neon noir, wet streets."})
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e6", "style": "Moody noir"}).json()
    assert got["slug"] == "moody-noir" and "Neon noir, wet streets." in got["prompt"] and NOIR not in got["prompt"]


def test_a_saved_style_draws_by_its_slug_in_a_run_and_the_estimate(env):
    env.key()
    make(env)
    ask = {"key": KEY, "style": "moody-noir", "level": "main", "portraits": "false"}
    assert env.c.get("/api/art/estimate", params=ask).json()["plates"] == 2
    job = run_set(env, style="moody-noir", portraits=False)
    assert job["status"] == "done" and job["report"]["art"]["style"] == "moody-noir"
    plates = pictures(env, "moody-noir")
    assert plates and all(p["prompt"].endswith(NOIR + " " + art.NO_TEXT) for p in plates)
    assert not (env.data / "art" / env.hash / "ink").exists()
    assert env.c.get("/api/art/estimate", params=ask).json()["plates"] == 0      # the same set: all drawn


def test_the_store_survives_a_broken_file_and_ignores_rows_naming_a_listed_style(tmp_path):
    (tmp_path / "styles.json").write_text(json.dumps({"styles": [{"slug": "ink", "text": "x"}, {"slug": "../x", "text": "y"},
                                                                 {"slug": "fine", "name": "Fine", "text": "Fine lines."}]}))
    store = styles_mod.StyleStore(tmp_path)
    assert [s["slug"] for s in store.all() if not s["builtin"]] == ["fine"] and art.SAVED_STYLES == {"fine": "Fine lines."}
    assert store.preview_path("../x") is None
    (tmp_path / "styles.json").write_text("{not json")
    assert [s for s in styles_mod.StyleStore(tmp_path).all() if not s["builtin"]] == []


def test_mutating_style_routes_need_the_csrf_header(env):
    bare = TestClient(env.c.app)
    for method, url in (("post", "/api/styles"), ("put", "/api/styles/x"), ("delete", "/api/styles/x"),
                        ("post", "/api/styles/x/preview")):
        assert getattr(bare, method)(url).status_code == 403, url


def test_the_style_routes_need_sign_in(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 10)
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",),
                             log=lambda *_: None)
    c = TestClient(app, headers={"X-Lorekeeper": "1"})
    for method, url in (("get", "/api/styles"), ("post", "/api/styles"), ("put", "/api/styles/x"),
                        ("delete", "/api/styles/x"), ("get", "/api/styles/x/preview"), ("post", "/api/styles/x/preview")):
        assert getattr(c, method)(url).status_code == 401, url


# ---- round 2


def test_the_saved_words_are_swapped_in_one_assignment_never_emptied(env):
    make(env)
    before = art.SAVED_STYLES
    make(env, "Night city", "Neon noir, wet streets.")
    assert before == {"moody-noir": NOIR}                 # a reader holding the old table never saw it cleared
    assert art.SAVED_STYLES == {"moody-noir": NOIR, "night-city": "Neon noir, wet streets."}
    assert art.style_sentence("night-city") == "Neon noir, wet streets."


def test_a_model_sent_without_a_provider_is_checked_against_the_drawing_provider(env):
    env.key()
    make(env)
    r = env.c.post("/api/styles/moody-noir/preview", json={"model": "nope"})
    assert r.status_code == 422 and r.json()["code"] == "bad-model" and env.image.calls == []
    assert env.c.post("/api/styles/moody-noir/preview", json={"model": GEMINI}).status_code == 200


@pytest.mark.parametrize("mime", ["image/webp", "image/png"])     # a redraw in another format, then in the same one
def test_a_failed_list_write_keeps_the_old_preview(env, monkeypatch, mime):
    env.key()
    make(env)
    old = env.c.post("/api/styles/moody-noir/preview", json={}).json()["preview"]
    first = env.c.get(old).content
    draw = env.image.draw

    def drawn(prompt, **kw):
        got = draw(prompt, **kw)
        got.mime = mime
        return got
    monkeypatch.setattr(env.image, "draw", drawn)

    def broken(self, data):
        raise OSError("disk full")
    monkeypatch.setattr(styles_mod.StyleStore, "_write", broken)
    with pytest.raises(OSError):
        env.c.post("/api/styles/moody-noir/preview", json={})
    assert listed(env)["moody-noir"]["preview"] == old and env.c.get(old).content == first
    assert sorted(f.name for f in (env.data / "styles").iterdir()) == ["moody-noir.png"]


def test_a_name_whose_slug_the_sheet_keeps_for_itself_is_refused(env):
    for name in ("Custom", "custom!", "!!!"):         # "!!!" slugs to "custom" too
        r = env.c.post("/api/styles", json={"name": name, "text": NOIR})
        assert r.status_code == 409 and r.json()["code"] == "exists", name
    assert "custom" not in listed(env) and "custom" not in art.SAVED_STYLES
    (env.data / "styles.json").write_text(json.dumps({"styles": [{"slug": "custom", "name": "Custom", "text": NOIR}]}))
    assert "custom" not in listed(env)                  # an older file's row is never listed


# ---- reference pictures


def png_of(w=512, h=384, salt=b"a"):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + salt * 64


def jpeg_of(w=640, h=480):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + b"\xff\xff" + sof + b"\xff\xda\x00\x02" + b"\x00" * 64 + b"\xff\xd9"


def webp_of(w=300, h=900, kind="VP8L"):
    if kind == "VP8L":
        bits = (w - 1) | ((h - 1) << 14)
        body = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 32
    elif kind == "VP8X":
        body = b"\x00" * 4 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little") + b"\x00" * 32
    else:
        body = b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", w, h) + b"\x00" * 32
    chunk = kind.encode() + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def upload(e, slug, data, name="ref.png", ctype="image/png"):
    return e.c.put(f"/api/styles/{slug}/reference", files={"file": (name, data, ctype)})


def test_the_header_reader_takes_png_jpeg_and_webp_by_their_bytes():
    assert styles_mod.sniff(png_of(512, 384)) == ("image/png", 512, 384)
    assert styles_mod.sniff(jpeg_of(640, 480)) == ("image/jpeg", 640, 480)
    for kind in ("VP8L", "VP8X", "VP8 "):
        assert styles_mod.sniff(webp_of(300, 900, kind)) == ("image/webp", 300, 900), kind
    for data, says in ((b"", "empty"), (b"GIF89a" + b"\x00" * 40, "PNG, JPEG or WebP"), (b"<svg></svg>", "PNG, JPEG or WebP"),
                       (png_of(255, 400), "255 by 400"), (png_of(400, 8193), "400 by 8193"),
                       (b"\x89PNG\r\n\x1a\n\x00\x00", "damaged"), (b"\xff\xd8\xff\xd9" + b"\x00" * 8, "damaged"),
                       (b"RIFF\x00\x00\x00\x00WEBPVP8Z" + b"\x00" * 30, "damaged")):
        with pytest.raises(styles_mod.StyleError) as got:
            styles_mod.sniff(data)
        assert got.value.status == 422 and got.value.code == "bad-image" and says in got.value.detail, data[:12]


def test_a_reference_picture_is_filed_listed_served_and_sent_by_default(env):
    make(env)
    r = upload(env, "moody-noir", png_of(), name="anything.webp", ctype="image/webp")   # the bytes decide, not the name
    assert r.status_code == 200, r.text
    got = r.json()
    row = json.loads((env.data / "styles.json").read_text())["styles"][0]
    ref = row["reference"]
    assert ref["ext"] == "png" and ref["width"] == 512 and ref["height"] == 384 and ref["bytes"] == len(png_of())
    assert len(ref["sha"]) == 64 and isinstance(ref["created"], int) and row["useReference"] is True
    assert got["reference"] == f"/api/styles/moody-noir/reference?v={ref['sha'][:12]}" and got["useReference"] is True
    assert listed(env)["moody-noir"]["reference"] == got["reference"]
    f = env.c.get(got["reference"])
    assert f.status_code == 200 and f.content == png_of() and f.headers["content-type"] == "image/png"
    assert f.headers["cache-control"] == "no-cache"
    assert (env.data / "styles" / "moody-noir.ref.png").is_file()
    sent = art.SAVED_STYLE_REFS["moody-noir"]
    assert sent["mime"] == "image/png" and sent["sha"] == ref["sha"] and sent["path"].read_bytes() == png_of()
    assert art.SAVED_STYLES["moody-noir"] == NOIR      # the words stay what the prompt carries


def test_a_replaced_picture_keeps_the_choice_and_drops_the_old_file(env):
    make(env)
    upload(env, "moody-noir", png_of())
    r = env.c.put("/api/styles/moody-noir", json={"useReference": False})
    assert r.status_code == 200 and r.json()["useReference"] is False and "moody-noir" not in art.SAVED_STYLE_REFS
    assert r.json()["reference"]                           # still kept, only not sent
    before = art.SAVED_STYLE_REFS
    got = upload(env, "moody-noir", webp_of()).json()
    assert got["useReference"] is False and got["reference"].endswith(
        json.loads((env.data / "styles.json").read_text())["styles"][0]["reference"]["sha"][:12])
    assert sorted(f.name for f in (env.data / "styles").iterdir()) == ["moody-noir.ref.webp"]
    assert env.c.put("/api/styles/moody-noir", json={"useReference": True}).json()["useReference"] is True
    assert before == {} and art.SAVED_STYLE_REFS["moody-noir"]["mime"] == "image/webp"   # swapped, never edited
    assert env.c.put("/api/styles/moody-noir", json={"useReference": "yes"}).json()["code"] == "bad-style"


def test_a_picture_is_refused_by_its_bytes_size_and_sides(env, monkeypatch):
    make(env)
    for data in (b"GIF89a" + b"\x00" * 64, png_of(200, 900), png_of(9000, 900), b"not a picture"):
        r = upload(env, "moody-noir", data)
        assert r.status_code == 422 and r.json()["code"] == "bad-image", r.text
    monkeypatch.setattr(styles_mod, "MAX_REF_BYTES", 1000)
    r = upload(env, "moody-noir", png_of(salt=b"x" * 20))
    assert r.status_code == 413 and r.json()["code"] == "too-large"
    assert listed(env)["moody-noir"]["reference"] is None and not (env.data / "styles").exists()
    assert upload(env, "ink", png_of()).json()["code"] == "builtin"
    assert upload(env, "nope", png_of()).json()["code"] == "no-style"
    assert env.c.put("/api/styles/moody-noir", json={"useReference": True}).status_code == 422   # nothing to send


def test_removing_the_picture_stops_sending_it(env):
    make(env)
    upload(env, "moody-noir", jpeg_of())
    r = env.c.delete("/api/styles/moody-noir/reference")
    assert r.status_code == 200 and r.json()["reference"] is None and r.json()["useReference"] is False
    assert env.c.get("/api/styles/moody-noir/reference").status_code == 404 and "moody-noir" not in art.SAVED_STYLE_REFS
    assert not (env.data / "styles").exists() or not list((env.data / "styles").iterdir())
    assert env.c.delete("/api/styles/moody-noir/reference").status_code == 404
    assert env.c.delete("/api/styles/ink/reference").status_code == 409
    assert env.c.get("/api/styles/ink/reference").status_code == 404


def test_a_deleted_style_keeps_sending_its_picture_and_a_new_one_of_the_name_takes_it_back(env):
    make(env)
    upload(env, "moody-noir", png_of())
    sha = json.loads((env.data / "styles.json").read_text())["styles"][0]["reference"]["sha"]
    assert env.c.delete("/api/styles/moody-noir").status_code == 200
    saved = json.loads((env.data / "styles.json").read_text())
    assert saved["retired_refs"]["moody-noir"]["reference"]["sha"] == sha
    assert saved["retired_refs"]["moody-noir"]["useReference"] is True
    assert (env.data / "styles" / "moody-noir.ref.png").is_file()          # its sets keep drawing as they did
    assert art.SAVED_STYLE_REFS["moody-noir"]["sha"] == sha
    assert env.c.get("/api/styles/moody-noir/reference").status_code == 404   # but it is no longer listed or served
    again = make(env)
    assert again["reference"] and again["useReference"] is True
    assert "moody-noir" not in json.loads((env.data / "styles.json").read_text())["retired_refs"]
    assert env.c.get(again["reference"]).content == png_of()


def test_a_preview_sends_the_picture_last_with_the_style_note_and_goes_stale_when_it_changes(env):
    env.key()
    make(env)
    upload(env, "moody-noir", png_of())
    got = env.c.post("/api/styles/moody-noir/preview", json={}).json()
    call = env.image.calls[-1]
    assert call["references"] == [png_of()] and call["prompt"] == art.preview_prompt(NOIR)
    assert call["note"] == getattr(art, "STYLE_REF", styles_mod.STYLE_REF_NOTE) and "drawing style" in call["note"]
    assert not got["stale"] and (env.data / "styles" / "moody-noir.ref.png").is_file()   # the preview never removes it
    assert env.c.put("/api/styles/moody-noir", json={"useReference": False}).json()["stale"]
    env.c.post("/api/styles/moody-noir/preview", json={})
    assert env.image.calls[-1]["references"] == [] and env.image.calls[-1]["note"] == ""
    assert not listed(env)["moody-noir"]["stale"]
    env.c.put("/api/styles/moody-noir", json={"useReference": True})
    assert listed(env)["moody-noir"]["stale"]
    env.c.post("/api/styles/moody-noir/preview", json={})
    upload(env, "moody-noir", webp_of())
    assert listed(env)["moody-noir"]["stale"]               # another picture
    names = sorted(f.name for f in (env.data / "styles").iterdir())
    assert names == ["moody-noir.png", "moody-noir.ref.webp"]
    env.c.delete("/api/styles/moody-noir")
    assert sorted(f.name for f in (env.data / "styles").iterdir()) == ["moody-noir.ref.webp"]


class Seeing:
    provider, name = "claude", "claude-haiku-5-5"

    def __init__(self):
        self.usage = llm.Usage()


def test_describe_writes_the_sentence_from_the_picture_and_stores_nothing(env, monkeypatch):
    model = Seeing()
    seen = []

    def describe(m, data, mime):
        seen.append((m, data, mime))
        m.usage.cost_usd += 0.001
        return "Loose ink line over flat washes of muted colour."
    monkeypatch.setattr(art, "describe_style", describe, raising=False)
    monkeypatch.setattr(env.jobs.__class__, "describe_model", lambda self: model)
    r = env.c.post("/api/styles/describe", files={"file": ("x.bin", jpeg_of(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "Loose ink line over flat washes of muted colour.", "provider": "claude",
                        "model": "claude-haiku-5-5", "cost": 0.001}
    assert seen == [(model, jpeg_of(), "image/jpeg")]
    assert not (env.data / "styles").exists() and listed(env).keys() == set(art.STYLES)
    r = env.c.post("/api/styles/describe", files={"file": ("x.png", b"GIF89a" + b"\x00" * 40, "image/png")})
    assert r.status_code == 422 and r.json()["code"] == "bad-image" and len(seen) == 1


def test_describe_says_when_no_model_can_see_or_the_model_fails(env, monkeypatch):
    pic = {"file": ("x.png", png_of(), "image/png")}
    monkeypatch.setattr(env.jobs.__class__, "describe_model", lambda self: None)
    r = env.c.post("/api/styles/describe", files=pic)
    assert r.status_code == 409 and r.json()["code"] == "no-vision-model" and "Settings" in r.json()["detail"]
    monkeypatch.setattr(env.jobs.__class__, "describe_model", lambda self: Seeing())
    for error, status, code in ((llm.NoVision("blind"), 409, "no-vision-model"),
                                (llm.ProviderError("HTTP 500"), 502, "model-failed"),
                                (ValueError("could not describe"), 502, "model-failed"),
                                (RuntimeError("Claude returned no JSON"), 502, "model-failed"),
                                (KeyError("secret body"), 502, "model-failed")):
        def describe(m, data, mime, error=error):
            raise error
        monkeypatch.setattr(art, "describe_style", describe, raising=False)
        r = env.c.post("/api/styles/describe", files=pic)
        assert r.status_code == status and r.json()["code"] == code, error


def test_the_reference_routes_need_the_csrf_header_and_sign_in(env, tmp_path, monkeypatch):
    bare = TestClient(env.c.app)
    for method, url in (("put", "/api/styles/x/reference"), ("delete", "/api/styles/x/reference"),
                        ("post", "/api/styles/describe")):
        assert getattr(bare, method)(url).status_code == 403, url
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 10)
    app = web_app.create_app(tmp_path / "signed", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",),
                             log=lambda *_: None)
    c = TestClient(app, headers={"X-Lorekeeper": "1"})
    for method, url in (("put", "/api/styles/x/reference"), ("delete", "/api/styles/x/reference"),
                        ("get", "/api/styles/x/reference"), ("post", "/api/styles/describe")):
        assert getattr(c, method)(url).status_code == 401, url


def test_a_broken_reference_row_is_dropped_when_read(tmp_path):
    (tmp_path / "styles.json").write_text(json.dumps({
        "styles": [{"slug": "fine", "name": "Fine", "text": "Fine lines.", "useReference": True,
                    "reference": {"ext": "exe", "sha": "x"}}],
        "retired_refs": {"gone": {"reference": {"ext": "png", "sha": "../../x"}, "useReference": True}}}))
    store = styles_mod.StyleStore(tmp_path)
    row = [s for s in store.all() if s["slug"] == "fine"][0]
    assert row["reference"] is None and row["useReference"] is False and art.SAVED_STYLE_REFS == {}


@pytest.mark.skipif(not hasattr(art, "STYLE_REF"), reason="the pipeline's style reference is not on this branch yet")
def test_a_draw_in_a_style_with_a_picture_sends_it_last_and_the_art_route_serves_its_sha(env):
    env.key()
    make(env)
    upload(env, "moody-noir", png_of())
    sha = json.loads((env.data / "styles.json").read_text())["styles"][0]["reference"]["sha"]
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "moody-noir"}).json()
    assert NOIR in got["prompt"] and "moody-noir" in got["styles"]
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "moody-noir",
                                                         "prompt": got["prompt"]})
    assert r.status_code == 200, r.text
    call = env.image.calls[-1]
    assert call["references"][-1] == png_of() and art.STYLE_REF in call["note"]
    assert r.json()["styleRef"] == sha
    pics = env.c.get(f"/api/library/{KEY}/art", params={"style": "moody-noir"}).json()["pictures"]
    assert [p.get("styleRef") for p in pics] == [sha]


def test_a_preview_from_before_reference_pictures_goes_stale_once_a_picture_is_sent(env):
    env.key()
    make(env)
    env.c.post("/api/styles/moody-noir/preview", json={})
    saved = json.loads((env.data / "styles.json").read_text())
    saved["styles"][0]["preview"].pop("reference")           # as an older server wrote it
    (env.data / "styles.json").write_text(json.dumps(saved))
    assert not listed(env)["moody-noir"]["stale"]             # no picture then, none now
    upload(env, "moody-noir", png_of())
    assert listed(env)["moody-noir"]["stale"]
    env.c.put("/api/styles/moody-noir", json={"useReference": False})
    assert not listed(env)["moody-noir"]["stale"]


def test_describe_uses_the_strong_tier_that_sees_pictures_else_the_cheap_one(env, monkeypatch):
    jm = env.jobs.__class__
    made = []
    monkeypatch.setattr(jm, "cloud_model", lambda self, p, m=None: made.append((p, m)) or (p, m))
    monkeypatch.setattr(jm, "configured", lambda self: ["groq", "claude"])
    assert env.jobs.describe_model() == ("claude", llm.PROVIDERS["claude"]["strong"])     # groq's strong one is blind
    seeing = {("claude", llm.PROVIDERS["claude"]["cheap"])}
    monkeypatch.setattr(llm, "vision", lambda p, m: (p, m) in seeing)
    assert env.jobs.describe_model() == ("claude", llm.PROVIDERS["claude"]["cheap"])      # no strong one sees: cheap
    seeing.clear()
    assert env.jobs.describe_model() is None
