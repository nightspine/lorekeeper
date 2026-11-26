"""Picture styles in the web app: the listed styles and the saved ones (styles.json), their previews, and a saved style
used by its slug in a draw and a run. Scripted text and image models; no network."""

import json

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
                       "stale": False, "created": got[-1]["created"]}


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
