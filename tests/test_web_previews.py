"""A book's own style previews (web/previews.py): the store, the "previews" job, the first-build hook and the routes. The
pipeline's `opening_moments` and `preview_body` are scripted here (they are art.py's, tested in tests/test_art*.py), the
image model is the fake of tests/test_art.py; no network."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import art, llm  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402
from lorekeeper.web import jobs as jobs_mod  # noqa: E402
from lorekeeper.web import previews as pv  # noqa: E402

from .test_art import Text  # noqa: E402
from .test_web_art import GEMINI, KEY, GeminiFake, env, short_world, wait_for  # noqa: E402,F401

NOIR = "Moody film noir, deep shadows, rain-slick streets, one hard light."


@dataclass
class Moment:
    kind: str
    target: str
    chapter: str
    anchor: str
    summary: str


@pytest.fixture(autouse=True)
def pipeline(monkeypatch):
    """The pipeline's half, scripted: each chapter's main moment (its turning point, else its first event), then the other
    non-minor events, of the first `chapters` chapters; `extra` moments are added after them (a gate test's)."""
    box = {"bodies": [], "extra": [], "seen": []}

    def opening_moments(self, bi=-1, chapters=2, story=None):
        box["seen"].append({"chapters": chapters, "story": story})
        b = self.books[bi]
        chs = [c for c in b.chapters if story is None or story(c["title"], c["words"])][:chapters]
        plates, others = [], []
        for c in chs:
            evs = [e for e in c["events"] if e.get("kind") != "minor"]
            if not evs:
                continue
            main = next((e for e in evs if e.get("kind") == "turn"), evs[0])
            plates.append(Moment("plate", c["title"], c["title"], c["last"], main["summary"]))
            others += [Moment("event", e["id"], c["title"], e["from"], e["summary"]) for e in evs if e is not main]
        return plates + others + box["extra"]

    def preview_body(self, moment, finished=frozenset()):
        box["bodies"].append((moment, finished))
        if self.text is not None:
            self.text.prompts.append(f"preview of {moment.target}")
        body = f"A scene in the mill: {moment.summary}"
        return art.Prompt(text=body, scene=body, anchor=moment.anchor, shape="square", body=body,
                          written_by={"provider": "claude", "model": "fake-writer"})
    monkeypatch.setattr(art.Illustrator, "opening_moments", opening_moments, raising=False)
    monkeypatch.setattr(art.Illustrator, "preview_body", preview_body, raising=False)
    monkeypatch.setattr(jobs_mod.JobManager, "text_choice", lambda self, tier="cheap", jp=None: ("claude", "fake"))
    yield box
    art.SAVED_STYLES.clear()
    art.SAVED_STYLE_REFS = {}


def got(e):
    r = e.c.get(f"/api/library/{KEY}/previews")
    assert r.status_code == 200, r.text
    return r.json()


def draw(e, **body):
    r = e.c.post(f"/api/library/{KEY}/previews", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "previews"
    return wait_for(e.c, r.json()["id"])


# ---- the store

def test_the_store_files_serves_only_listed_files_and_a_new_scene_clears_the_old(tmp_path):
    s = pv.PreviewStore(tmp_path)
    h = "ab" * 32
    s.set_scene(h, {"target": "One", "body": "x"})
    item = s.put(h, "gemini", "ink", b"\x89PNGone", "image/png", {"text": "ink words", "reference": None})
    assert item["file"] == "gemini-ink.png" and s.path(h, "gemini", "ink").read_bytes() == b"\x89PNGone"
    assert s.get(h)["items"]["gemini"]["ink"]["text"] == "ink words"
    s.put(h, "gemini", "ink", b"jpeg", "image/jpeg", {"text": "ink words"})      # another kind of file: the old one goes
    assert not (tmp_path / "previews" / h / "gemini-ink.png").exists() and s.path(h, "gemini", "ink").suffix == ".jpg"
    for bad in [("gemini", "../ink"), ("../x", "ink"), ("gemini", "anime"), ("open-ai", "ink")]:
        assert s.path(h, *bad) is None
    (tmp_path / "previews" / h / "gemini-anime.png").write_bytes(b"not listed")
    assert s.path(h, "gemini", "anime") is None
    data = s.get(h)
    data["items"]["gemini"]["ink"]["file"] = "previews.json"
    s._write(h, data)
    assert s.path(h, "gemini", "ink") is None                 # a record naming another file is never served
    s.set_scene(h, {"target": "Two", "body": "y"})
    assert s.get(h)["items"] == {} and not (tmp_path / "previews" / h / "gemini-ink.jpg").exists()
    assert s.remove(h) and s.get(h) is None and not s.remove(h)
    assert s.get("../etc") is None


# ---- the job and the routes

def test_previews_draw_the_opening_scene_once_in_every_style_per_provider(env, pipeline):
    env.key()
    before = env.index.read_bytes()
    est = env.c.get(f"/api/library/{KEY}/previews/estimate").json()
    q = llm.nearest_quality("gemini", llm.IMAGE_PROVIDERS["gemini"]["default"], "draft")
    each = llm.image_price("gemini", llm.IMAGE_PROVIDERS["gemini"]["default"], q, "square")
    assert est["styles"] == list(art.STYLES) and est["pictures"] == len(art.STYLES)
    assert est["providers"] == [{"provider": "gemini", "label": llm.PROVIDERS["gemini"]["label"],
                                 "model": llm.IMAGE_PROVIDERS["gemini"]["default"], "quality": q, "price": each,
                                 "pictures": len(art.STYLES)}]
    assert est["price"] == round(len(art.STYLES) * each, 4)
    done = draw(env)
    assert done["status"] == "done", done["error"]
    assert done["config"]["name"] == "Style previews, Millbrook" and done["index"] == KEY
    assert len(pipeline["bodies"]) == 1 and pipeline["bodies"][0][1] == frozenset()    # one body, nothing earlier finished
    assert pipeline["seen"][0]["chapters"] == 2 and pipeline["seen"][0]["story"] is jobs_mod.is_story
    calls = env.image.calls
    assert len(calls) == len(art.STYLES) and all(c["shape"] == "square" and c["quality"] == q and c["references"] == []
                                                  for c in calls)
    body = "A scene in the mill: Oren the miller comes out of the mill an"
    assert all(c["prompt"].startswith(body[:40]) and c["prompt"].endswith(art.NO_TEXT) for c in calls)
    assert [art.STYLES[s] in c["prompt"] for s, c in zip(art.STYLES, calls)] == [True] * len(art.STYLES)
    g = got(env)
    assert g["scene"]["kind"] == "plate" and g["scene"]["target"] == "Chapter One" and g["scene"]["storyChapter"] == 1
    assert g["scene"]["chosen"] == "auto" and "body" not in g["scene"] and "anchor" not in g["scene"]
    assert set(g["providers"]) == {"gemini"} and set(g["providers"]["gemini"]) == set(art.STYLES)
    ink = g["providers"]["gemini"]["ink"]
    assert ink["url"].startswith(f"/api/previews/{env.hash}/gemini/ink?v=") and not ink["stale"]
    f = env.c.get(ink["url"])
    assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.headers["cache-control"] == "no-cache"
    assert [m["target"] for m in g["moments"]] == ["Chapter One", "Chapter Two", "e1", "e5", "e6"] and not g["drawing"]
    assert all(set(m) == {"kind", "target", "chapter", "storyChapter", "summary"} for m in g["moments"])
    assert env.index.read_bytes() == before and env.c.get(f"/api/library/{KEY}/art").json()["styles"] == []
    assert not (env.data / "art").exists() or not any((env.data / "art").rglob("art.json"))   # never an art set
    # nothing left to draw: same scene, every style fresh
    assert env.c.get(f"/api/library/{KEY}/previews/estimate").json()["pictures"] == 0
    assert env.c.get(f"/api/library/{KEY}/previews/estimate?redraw=true").json()["pictures"] == len(art.STYLES)
    again = draw(env)
    assert again["status"] == "done" and len(env.image.calls) == len(art.STYLES) and len(pipeline["bodies"]) == 1


def test_another_scene_writes_a_new_body_and_clears_the_old_previews(env, pipeline):
    env.key()
    draw(env)
    old = got(env)["providers"]["gemini"]["ink"]["url"]
    assert env.c.get(f"/api/library/{KEY}/previews/estimate?scene=e5").json()["pictures"] == len(art.STYLES)
    done = draw(env, scene="e5")
    assert done["status"] == "done" and len(pipeline["bodies"]) == 2 and pipeline["bodies"][1][0].target == "e5"
    g = got(env)
    assert g["scene"]["target"] == "e5" and g["scene"]["chosen"] == "person" and g["scene"]["storyChapter"] == 2
    assert "Gabriel rides in" in env.image.calls[-1]["prompt"] and len(env.image.calls) == 2 * len(art.STYLES)
    assert g["providers"]["gemini"]["ink"]["url"] != old or env.c.get(old).status_code == 200
    # with no scene asked for, the person's scene stays
    draw(env, redraw=True)
    assert got(env)["scene"]["target"] == "e5" and len(pipeline["bodies"]) == 2


def test_a_scene_past_the_opening_is_never_offered_or_served(env, pipeline, monkeypatch):
    env.key()
    monkeypatch.setattr(pv, "OPENING_CHAPTERS", 1)       # the opening is chapter one only
    pipeline["extra"].append(Moment("event", "e4", "Chapter Two", "a8", "A hooded stranger comes"))
    g = got(env)
    assert [m["target"] for m in g["moments"]] == ["Chapter One", "e1"]       # chapter two's, even asked for, are not
    for scene in ("e4", "Chapter Two", "e99"):
        r = env.c.post(f"/api/library/{KEY}/previews", json={"scene": scene})
        assert r.status_code == 422 and r.json()["code"] == "bad-scene", scene
        assert env.c.get(f"/api/library/{KEY}/previews/estimate?scene={scene}").json()["code"] == "bad-scene"
    monkeypatch.setattr(pv, "OPENING_CHAPTERS", 2)
    draw(env, scene="e5")
    assert got(env)["scene"]["target"] == "e5"
    monkeypatch.setattr(pv, "OPENING_CHAPTERS", 1)       # a stored scene outside the opening as it reads now
    g = got(env)
    assert g["scene"] is None and g["providers"] == {}


def test_route_errors(env, pipeline, monkeypatch):
    r = env.c.post(f"/api/library/{KEY}/previews", json={})
    assert r.status_code == 409 and r.json()["code"] == "no-image-key"
    env.key()
    for body in ({"scene": 5}, {"providers": ["claude"]}, {"styles": ["../x"]}, {"redraw": "yes"}, {"providers": []}):
        r = env.c.post(f"/api/library/{KEY}/previews", json=body)
        assert r.status_code == 422 and r.json()["code"] == "bad-config", body
    assert env.c.get("/api/library/nope/previews").status_code == 404
    busy = jobs_mod.Job(id="pv1", book_id="", book_title="Millbrook", config={"text_hash": env.hash}, output=str(env.index),
                        status="running", kind="previews")
    env.jobs.jobs["pv1"] = busy
    assert got(env)["drawing"]
    r = env.c.post(f"/api/library/{KEY}/previews", json={})
    assert r.status_code == 409 and r.json()["code"] == "busy"
    assert env.c.delete(f"/api/library/{KEY}/previews").json()["code"] == "busy"
    # a previews job never blocks pictures, edits or merges, and never reads as building the index
    assert not env.jobs.writing(env.index) and not env.jobs.drawing(env.hash)
    busy.status = "done"
    monkeypatch.setattr(pv, "CAP", 0.01)
    r = env.c.post(f"/api/library/{KEY}/previews", json={})
    assert r.status_code == 422 and r.json()["code"] == "bad-config"
    monkeypatch.setattr(pv, "CAP", 5.0)
    pipeline["extra"].clear()
    monkeypatch.setattr(art.Illustrator, "opening_moments", lambda self, *a, **kw: [], raising=False)
    r = env.c.post(f"/api/library/{KEY}/previews", json={})
    assert r.status_code == 422 and r.json()["code"] == "no-moments"
    monkeypatch.setattr(jobs_mod.JobManager, "text_choice", lambda self, tier="cheap", jp=None: None)
    monkeypatch.setattr(art.Illustrator, "opening_moments", lambda self, *a, **kw: [
        Moment("plate", "Chapter One", "Chapter One", "a6", "Mira at the mill")], raising=False)
    r = env.c.post(f"/api/library/{KEY}/previews", json={})
    assert r.status_code == 409 and r.json()["code"] == "no-text-model"


def test_delete_removes_them(env):
    env.key()
    draw(env)
    url = got(env)["providers"]["gemini"]["ink"]["url"]
    assert env.c.delete(f"/api/library/{KEY}/previews").json() == {"removed": True}
    assert got(env)["scene"] is None and env.c.get(url).status_code == 404
    assert env.c.delete(f"/api/library/{KEY}/previews").json() == {"removed": False}


def test_a_refusal_or_provider_error_skips_one_style(env):
    env.key()
    env.image.refuse = [lambda prompt, n: n == 2]
    real = env.image.draw

    def flaky(prompt, **kw):
        if len(env.image.calls) == 3:
            env.image.calls.append({})
            raise llm.ProviderError("server error 500")
        return real(prompt, **kw)
    env.image.draw = flaky
    done = draw(env)
    rep = done["report"]
    assert done["status"] == "done" and rep["refused"] == 1 and rep["failed"] == 1
    assert rep["drawn"] == len(art.STYLES) - 2 and len(got(env)["providers"]["gemini"]) == len(art.STYLES) - 2
    events = env.c.get(f"/api/jobs/{done['id']}").json()
    log = (env.data / "jobs" / f"{done['id']}.events.jsonl").read_text()
    assert "refused" in log and "failed" in log and events["progress"]["previews"] == [len(art.STYLES)] * 2
    # the two left out are drawn by the next job, nothing else
    n = len(env.image.calls)
    env.image.refuse = []
    env.image.draw = real
    draw(env)
    assert len(env.image.calls) - n == 2 and len(got(env)["providers"]["gemini"]) == len(art.STYLES)


def test_a_stop_keeps_what_was_drawn(env, monkeypatch):
    env.key()
    box = {}

    class Stopper(GeminiFake):
        def draw(self, prompt, **kw):
            if len(self.calls) == 3:
                env.jobs.stop(box["id"])
            return super().draw(prompt, **kw)
    env.image = Stopper()
    monkeypatch.setattr(llm, "make_image", lambda *a, **kw: env.image)
    r = env.c.post(f"/api/library/{KEY}/previews", json={})
    box["id"] = r.json()["id"]
    done = wait_for(env.c, box["id"])
    assert done["status"] == "stopped" and done["report"]["stopped"]
    kept = got(env)["providers"]["gemini"]
    assert len(kept) == 4 == done["report"]["drawn"] and all(env.c.get(v["url"]).status_code == 200 for v in kept.values())


def test_the_cap_is_reserved_before_each_picture(env, monkeypatch):
    env.key()
    q = llm.nearest_quality("gemini", llm.IMAGE_PROVIDERS["gemini"]["default"], "draft")
    each = llm.image_price("gemini", llm.IMAGE_PROVIDERS["gemini"]["default"], q, "square")
    monkeypatch.setattr(pv, "CAP", each * 3.5)        # accepted at queueing, then dearer pictures than priced
    env.image.cost = each * 2
    done = draw(env, styles=["ink", "anime", "realism"])
    assert done["status"] == "stopped" and "cap" in done["report"]["stopped"] and len(env.image.calls) == 2


def test_a_saved_style_is_drawn_from_its_words_and_goes_stale_when_they_change(env):
    env.key()
    assert env.c.post("/api/styles", json={"name": "Moody noir", "text": NOIR}).status_code == 200
    draw(env, styles=["moody-noir", "ink"])
    assert NOIR in env.image.calls[0]["prompt"] and art.STYLES["ink"] in env.image.calls[1]["prompt"]
    assert not got(env)["providers"]["gemini"]["moody-noir"]["stale"]
    env.c.put("/api/styles/moody-noir", json={"text": "Neon noir, wet streets."})
    g = got(env)["providers"]["gemini"]
    assert g["moody-noir"]["stale"] and not g["ink"]["stale"]
    est = env.c.get(f"/api/library/{KEY}/previews/estimate").json()
    assert est["pictures"] == len(art.STYLES) - 1 + 1   # every built-in but ink, plus the stale saved one
    env.c.delete("/api/styles/moody-noir")
    assert "moody-noir" not in got(env)["providers"]["gemini"]      # a deleted style's preview is not listed


def test_a_saved_style_with_a_reference_picture_sends_it(env):
    from .test_web_styles import png_of
    env.key()
    env.c.post("/api/styles", json={"name": "Moody noir", "text": NOIR})
    r = env.c.put("/api/styles/moody-noir/reference", files={"file": ("ref.png", png_of(), "image/png")})
    assert r.status_code == 200, r.text
    draw(env, styles=["moody-noir"])
    call = env.image.calls[-1]
    assert len(call["references"]) == 1 and call["note"] == art.STYLE_REF
    assert not got(env)["providers"]["gemini"]["moody-noir"]["stale"]
    env.c.put("/api/styles/moody-noir", json={"useReference": False})
    assert got(env)["providers"]["gemini"]["moody-noir"]["stale"]


def test_two_providers_each_draw_with_their_own_default_model(env, monkeypatch):
    made = []

    class OpenAIFake(GeminiFake):
        provider, name = "openai", llm.IMAGE_PROVIDERS["openai"]["default"]
    fakes = {"gemini": env.image, "openai": OpenAIFake()}
    monkeypatch.setattr(llm, "make_image", lambda provider, model=None, **kw: made.append((provider, model)) or fakes[provider])
    env.key()
    monkeypatch.setenv("OPENAI_API_KEY", "o-test")
    est = env.c.get(f"/api/library/{KEY}/previews/estimate").json()
    assert [p["provider"] for p in est["providers"]] == ["gemini", "openai"] and est["pictures"] == 2 * len(art.STYLES)
    done = draw(env)
    assert done["status"] == "done" and set(made) == {("gemini", None), ("openai", None)}
    oq = llm.nearest_quality("openai", llm.IMAGE_PROVIDERS["openai"]["default"], "draft")
    assert all(c["quality"] == oq for c in fakes["openai"].calls) and len(fakes["openai"].calls) == len(art.STYLES)
    assert set(got(env)["providers"]) == {"gemini", "openai"}
    assert env.image.calls[0]["prompt"] == fakes["openai"].calls[0]["prompt"]          # one body for every provider


# ---- after a first build

def build_app(tmp_path, monkeypatch):
    from .test_web import SlowFake
    monkeypatch.setattr(llm, "Ollama", lambda **kw: SlowFake())
    t, im = Text(), GeminiFake()
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: t)
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: t)
    monkeypatch.setattr(jobs_mod.JobManager, "_portraits", lambda self, *a: {"skipped": "test"})
    monkeypatch.setattr(llm, "make_image", lambda *a, **kw: im)
    return web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",),
                              auth=False), im


def previews_jobs(c):
    return [j for j in c.get("/api/jobs").json() if j["kind"] == "previews"]


def test_a_first_build_queues_the_previews_only_when_asked_and_only_once(tmp_path, monkeypatch):
    from .test_web import upload
    app, im = build_app(tmp_path, monkeypatch)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        meta = upload(c, tmp_path)
        cfg = {"chunk_words": 5000}
        # no image key: nothing queued, one line says why
        done = wait_for(c, c.post("/api/jobs", json={"book_id": meta["id"], "config": {**cfg, "stylePreviews": True}}).json()["id"])
        assert done["status"] == "done" and previews_jobs(c) == []
        log = (tmp_path / "data" / "jobs" / f"{done['id']}.events.jsonl").read_text()
        assert "style previews: not drawn" in log
        monkeypatch.setenv("GEMINI_API_KEY", "g-test")
        # not asked for (the CLI, an older page): nothing
        done = wait_for(c, c.post("/api/jobs", json={"book_id": meta["id"], "config": cfg}).json()["id"])
        assert done["status"] == "done" and previews_jobs(c) == []
        assert "style previews" not in (tmp_path / "data" / "jobs" / f"{done['id']}.events.jsonl").read_text()
        # asked for: queued after the build, said in the build's log
        done = wait_for(c, c.post("/api/jobs", json={"book_id": meta["id"], "config": {**cfg, "stylePreviews": True}}).json()["id"])
        log = (tmp_path / "data" / "jobs" / f"{done['id']}.events.jsonl").read_text()
        assert "style previews: queued" in log and log.index("style previews: queued") < log.rindex('"type": "done"')
        [pj] = previews_jobs(c)
        key = "generated--" + Path(done["output"]).with_suffix("").name
        assert pj["index"] == key and pj["config"]["text_hash"] == meta["textHash"]
        pj = wait_for(c, pj["id"])
        assert pj["status"] == "done", pj["error"]
        # a rebuild, the previews there: nothing more
        h = meta["textHash"]
        (tmp_path / "data" / "previews" / h).mkdir(parents=True, exist_ok=True)
        if not (tmp_path / "data" / "previews" / h / "previews.json").exists():   # the opening had no moments
            app.state.jobs.previews.set_scene(h, {"target": "x", "body": "y"})
        done = wait_for(c, c.post("/api/jobs", json={"book_id": meta["id"], "config": {**cfg, "stylePreviews": True}}).json()["id"])
        assert done["status"] == "done" and len(previews_jobs(c)) == 1


def test_a_stopped_build_queues_nothing(tmp_path, monkeypatch):
    app, _ = build_app(tmp_path, monkeypatch)
    jm = app.state.jobs
    job = jobs_mod.Job(id="b1", book_id="x", book_title="T", config={"stylePreviews": True}, output="/nowhere.lore.json",
                       status="stopped")
    calls = []
    jm.after_build = lambda j: calls.append(j) or "x"
    jm._built(job)
    assert calls == []
    job.status = "done"
    jm._built(job)
    assert calls == [job]


def test_previews_json_shape(env):
    env.key()
    draw(env)
    data = json.loads((env.data / "previews" / env.hash / "previews.json").read_text())
    assert set(data) == {"scene", "items"}
    assert set(data["scene"]) == {"kind", "target", "chapter", "storyChapter", "anchor", "summary", "body", "writer",
                                  "chosen", "created"}
    assert data["scene"]["writer"] == {"provider": "claude", "model": "fake-writer"}
    it = data["items"]["gemini"]["ink"]
    assert set(it) == {"file", "model", "quality", "cost", "created", "text", "reference"} and it["file"] == "gemini-ink.png"
