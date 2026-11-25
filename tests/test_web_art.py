"""Pictures in the web app: the art store, the standalone pictures run and the build's pictures stage, the prompt and draw
routes, delete, redraw and the world note with undo. Scripted text and image models (tests/test_art.py); no network."""

import json
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import art, epub, llm, text  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402
from lorekeeper.web import art as store_mod  # noqa: E402
from lorekeeper.web import jobs as jobs_mod  # noqa: E402

from .test_art import CH1, CH2, Image, Text, base_doc, book, png  # noqa: E402
from .test_generate import make_epub  # noqa: E402

KEY = "examples--mill"
GEMINI = "gemini-nano-banana-2.1"


class GeminiFake(Image):
    provider, name = "gemini", GEMINI


@pytest.fixture(autouse=True)
def short_world(monkeypatch):
    monkeypatch.setattr(art, "WORLD_MIN_WORDS", 1)    # the world note ends with chapter one: nothing is held back
    monkeypatch.setattr(art, "MIN_GAP", 2)
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def mill_index(epub_path: Path) -> dict:
    """tests/test_art.py's Millbrook index, its anchors moved onto the EPUB's blocks (each chapter opens with a heading)."""
    bk = epub.read(epub_path)
    d = base_doc(book(("One", CH1), ("Two", CH2)))
    seq = {i: i + 1 if i < len(CH1) else i + 2 for i in range(len(CH1) + len(CH2))}
    d = json.loads(re.sub(r'"a(\d+)"', lambda m: f'"a{seq[int(m.group(1))]}"', json.dumps(d)))
    d["anchors"] = [{"id": f"a{b.seq}", "seq": b.seq, "fp": b.fingerprint} for b in bk.blocks]
    d["coverage"]["through"] = d["anchors"][-1]["id"]
    d.update({"lorekeeper": "0.1", "kind": "index", "id": "mill",
              "editions": [{"textHash": text.text_hash(b.words for b in bk.blocks), "builtFrom": True}]})
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An app with the Millbrook index in an examples folder and its EPUB beside it; the text model and the image model
    are scripted. `env.text` and `env.image` are the fakes, `env.key()` gives the image provider a key."""
    epubs, idx = tmp_path / "epubs", tmp_path / "idx"
    epubs.mkdir()
    idx.mkdir()
    path = make_epub(epubs / "mill.epub", title="Millbrook", ch1=CH1, ch2=CH2)
    doc = mill_index(path)
    (idx / "mill.json").write_text(json.dumps(doc))

    class Env:
        pass
    e = Env()
    e.text, e.image, e.doc, e.index = Text(), GeminiFake(), doc, idx / "mill.json"
    e.hash = doc["editions"][0]["textHash"]
    e.key = lambda: monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: e.text)
    monkeypatch.setattr(llm, "make_image", lambda provider, model=None, api_key=None, base_url=None: e.image)
    app = web_app.create_app(tmp_path / "data", [("examples", idx)], [epubs], ollama_url="http://127.0.0.1:9",
                             allowed_hosts=("testserver",), auth=False)
    e.jobs = app.state.jobs
    e.data = tmp_path / "data"
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        e.c = c
        yield e


def wait_for(c, job_id, statuses=("done", "failed", "stopped"), timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {job['status']}")


def run_set(e, **cfg):
    r = e.c.post(f"/api/library/{KEY}/art/run", json={"art": {"style": "ink", "level": "main", "cap": 10, "portraits": "all", **cfg}})
    assert r.status_code == 200, r.text
    return wait_for(e.c, r.json()["id"])


# ---- the store


def test_the_store_files_lists_serves_and_removes_pictures(tmp_path):
    s = store_mod.ArtStore(tmp_path)
    h = "ab" * 32
    rec = {"id": "p0000aaaa", "file": "p0000aaaa.png", "kind": "plate", "target": "One", "from": "a1", "cost": 0.04}
    s.put(h, "ink", rec, png(4, 3, b"x"))
    assert s.manifest(h, "ink")["pictures"] == [rec] and s.manifest(h, "ink")["cost"] == 0.04
    assert [x["slug"] for x in s.styles(h)] == ["ink"] and s.find(h, "p0000aaaa")[0] == "ink"
    assert s.path(h, "ink", "p0000aaaa").read_bytes().startswith(b"\x89PNG") and s.size(h) > 0
    custom = "Linocut print, two colours"
    assert s.folder(h, custom).name == art.style_slug(custom) == "linocut-print-two-colours"
    for bad in [("../x", "ink", "p0000aaaa"), (h, "../ink", "p0000aaaa"), (h, "ink", "../art.json"), (h, "ink", "p0000aaab")]:
        try:
            assert s.path(*bad) is None
        except KeyError:
            pass
    (s.folder(h, "ink") / "p1111bbbb.png").write_bytes(b"left over")
    assert s.prune(h, "ink") == 1 and s.path(h, "ink", "p0000aaaa")
    assert s.remove(h, "ink", "p0000aaaa")["id"] == "p0000aaaa"
    assert s.manifest(h, "ink")["pictures"] == [] and not (s.folder(h, "ink") / "p0000aaaa.png").exists()


def test_a_record_naming_another_file_is_never_served(tmp_path):
    s = store_mod.ArtStore(tmp_path)
    h = "cd" * 32
    s.put(h, "ink", {"id": "p0000aaaa", "file": "p0000aaaa.png"}, b"one")
    m = s.manifest(h, "ink")
    m["pictures"][0]["file"] = "art.json"
    s.save(h, "ink", m)
    assert s.path(h, "ink", "p0000aaaa") is None


# ---- the standalone run


def test_a_pictures_run_on_an_existing_index_draws_the_set_and_never_writes_the_index(env):
    env.key()
    before = env.index.read_bytes()
    done = wait_for(env.c, env.c.post(f"/api/library/{KEY}/art/run", json={"art": {"style": "ink", "cap": 10}}).json()["id"])
    assert done["status"] == "done", done["error"] and done["kind"] == "art"
    rep = done["report"]["art"]
    assert rep["drawn"] == len(env.image.calls) > 0 and rep["style"] == "ink" and rep["textHash"] == env.hash
    assert env.index.read_bytes() == before
    got = env.c.get(f"/api/library/{KEY}/art?style=ink").json()
    assert got["slug"] == "ink" and got["styles"][0]["slug"] == "ink" and got["world"]["text"]
    kinds = {p["kind"] for p in got["pictures"]}
    assert {"plate", "portrait"} <= kinds and got["screened"]
    pic = got["pictures"][0]
    r = env.c.get(pic["url"])
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG") and r.headers["cache-control"] == "max-age=86400"
    assert r.headers["content-type"] == "image/png"
    events = [json.loads(x) for x in (env.data / "jobs" / f"{done['id']}.events.jsonl").read_text().splitlines()]
    assert {"type": "phase", "phase": "art"}.items() <= next(x for x in events if x.get("phase") == "art").items()
    ticks = [x for x in events if x["type"] == "art"]
    assert ticks and all({"done", "total", "cost"} <= x.keys() for x in ticks)
    assert done["progress"]["art"][0] == done["progress"]["art"][1]


def test_a_second_run_resumes_and_draws_nothing_new(env):
    env.key()
    first = run_set(env)["report"]["art"]["drawn"]
    calls = len(env.image.calls)
    again = run_set(env)
    assert first > 0 and again["report"]["art"]["drawn"] == 0 and len(env.image.calls) == calls


def test_without_an_image_key_the_run_says_so(env):
    done = run_set(env)
    assert done["status"] == "done" and done["report"]["art"] == {"skipped": "no image key"}
    assert any("pictures: skipped" in json.loads(x).get("line", "")
               for x in (env.data / "jobs" / f"{done['id']}.events.jsonl").read_text().splitlines())


def test_a_failure_keeps_the_pictures_drawn_so_far(env, monkeypatch):
    env.key()
    real = art.Illustrator.draw
    n = []

    def flaky(self, job, prompt=None, text=None):
        n.append(1)
        if len(n) == 2:
            raise RuntimeError("disk full")
        return real(self, job, prompt, text)
    monkeypatch.setattr(art.Illustrator, "draw", flaky)
    monkeypatch.setattr(art, "WORKERS", 1)
    done = run_set(env, portraits=False)
    assert done["status"] == "failed" and "disk full" in done["report"]["art"]["failed"]
    assert len(env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]) == 1


def test_the_cap_stops_the_run_and_keeps_what_was_drawn(env):
    env.key()
    done = run_set(env, cap=0.06)
    rep = done["report"]["art"]
    assert done["status"] == "done" and rep["drawn"] == 1 and "cap" in rep["stopped"]


def test_a_run_is_refused_while_a_build_writes_the_index(env):
    env.key()
    busy = jobs_mod.Job(id="busy1", book_id="", book_title="Millbrook", config={}, output=str(env.index), status="running")
    env.jobs.jobs["busy1"] = busy
    r = env.c.post(f"/api/library/{KEY}/art/run", json={"art": {"style": "ink", "cap": 10}})
    assert r.status_code == 409 and r.json()["code"] == "busy"
    busy.status = "done"


def test_a_bad_config_is_refused_before_anything_is_queued(env):
    for cfg in ({"style": "", "cap": 5}, {"style": "ink", "level": "every", "cap": 5}, {"style": "ink", "cap": -1},
                {"style": "ink", "provider": "claude", "cap": 5}, {"style": "ink", "quality": "huge", "cap": 5},
                {"style": "ink"}, {"style": "ink", "cap": None}, {"style": "ink", "cap": 0}, {"style": "ink", "cap": 501},
                {"style": "ink", "level": None, "cap": 5}):
        r = env.c.post(f"/api/library/{KEY}/art/run", json={"art": cfg})
        assert r.status_code == 422, cfg
    assert env.jobs.jobs == {}


def test_deleting_a_pictures_run_keeps_the_index_and_the_pictures(env):
    env.key()
    done = run_set(env)
    assert env.c.delete(f"/api/jobs/{done['id']}").status_code == 200
    assert env.index.exists() and env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]


def test_a_pictures_run_does_not_mark_the_book_as_building_or_block_edits(env):
    busy = jobs_mod.Job(id="art1", book_id="", book_title="Millbrook", config={"text_hash": env.hash}, output=str(env.index),
                        status="running", kind="art")
    env.jobs.jobs["art1"] = busy
    assert not env.jobs.writing(env.index) and env.jobs.drawing(env.hash)
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "plate", "target": "Chapter One", "style": "ink"})
    assert r.status_code == 409 and r.json()["code"] == "busy"
    busy.status = "done"


# ---- the build's pictures stage


def test_the_build_ends_with_the_pictures_stage_after_the_arcs(tmp_path, monkeypatch):
    from .test_web import SlowFake, upload
    monkeypatch.setattr(llm, "Ollama", lambda **kw: SlowFake())
    t, im = Text(), GeminiFake()
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: t)
    monkeypatch.setattr(jobs_mod.JobManager, "_portraits", lambda self, *a: {"skipped": "test"})
    monkeypatch.setattr(llm, "make_image", lambda *a, **kw: im)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        meta = upload(c, tmp_path)
        bad = c.post("/api/jobs", json={"book_id": meta["id"], "config": {"chunk_words": 5000, "art": {"style": "ink", "level": "x"}}})
        assert bad.status_code == 422
        job = c.post("/api/jobs", json={"book_id": meta["id"], "config": {"chunk_words": 5000, "art": {"style": "ink", "cap": 10}}}).json()
        assert job["config"]["art"]["level"] == "main" and job["config"]["art"]["cap"] == 10
        no_cap = c.post("/api/jobs", json={"book_id": meta["id"], "config": {"chunk_words": 5000, "art": {"style": "ink"}}})
        assert no_cap.status_code == 422
        done = wait_for(c, job["id"])
        events = (tmp_path / "data" / "jobs" / f"{job['id']}.events.jsonl").read_text()
        key = "generated--" + Path(done["output"]).with_suffix("").name
        got = c.get(f"/api/library/{key}/art?style=ink").json()
    assert done["status"] == "done", done["error"]
    rep = done["report"]["art"]
    assert "drawn" in rep and rep["textHash"] == meta["textHash"]
    assert events.index("book arcs: skipped") < events.index('"phase": "art"') < events.rindex('"type": "done"')
    assert len(got["pictures"]) == rep["drawn"]


def test_without_an_image_key_the_build_skips_the_pictures(tmp_path, monkeypatch):
    from .test_web import SlowFake, upload
    monkeypatch.setattr(llm, "Ollama", lambda **kw: SlowFake())
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        meta = upload(c, tmp_path)
        job = c.post("/api/jobs", json={"book_id": meta["id"], "config": {"chunk_words": 5000, "art": {"style": "ink", "cap": 10}}}).json()
        done = wait_for(c, job["id"])
    assert done["status"] == "done" and done["report"]["art"] == {"skipped": "no image key"}


# ---- on demand: prompt, draw, delete, redraw, world


def test_the_prompt_route_writes_a_prompt_without_drawing(env):
    env.key()
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink"})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["prompt"].endswith(art.NO_TEXT) and art.STYLES["ink"] in got["prompt"]
    assert got["anchor"] in {a["id"] for a in env.doc["anchors"]} and got["shape"] == "landscape"
    assert got["price"] == llm.image_price("gemini", GEMINI, "standard", "landscape") and env.image.calls == []


def test_a_target_past_the_position_is_not_visible(env):
    env.key()
    early = {"kind": "event", "target": "e5", "style": "ink"}
    for p in ("a2", 0.1):
        r = env.c.post(f"/api/library/{KEY}/art/prompt", json={**early, "p": p})
        assert r.status_code == 422 and r.json()["code"] == "not-visible", p
    assert env.c.post(f"/api/library/{KEY}/art/prompt", json={**early, "p": 1.0}).status_code == 200
    last = {"kind": "plate", "target": "Chapter Two", "style": "ink"}    # its anchor is the book's last block
    assert env.c.post(f"/api/library/{KEY}/art/prompt", json={**last, "p": 0.99}).json()["code"] == "not-visible"
    assert env.c.post(f"/api/library/{KEY}/art/prompt", json={**last, "p": 1}).status_code == 200
    assert env.c.post(f"/api/library/{KEY}/art/prompt", json={**early, "p": "nowhere"}).json()["code"] == "bad-position"
    assert env.c.post(f"/api/library/{KEY}/art/prompt", json={**early, "target": "e99"}).status_code == 404


def test_without_a_text_model_the_prompt_route_says_so(env, monkeypatch):
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: None)
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink"})
    assert r.status_code == 409 and r.json()["code"] == "no-text-model"


def test_draw_files_the_edited_prompt_with_the_pipelines_anchor_and_references(env):
    env.key()
    run_set(env, portraits=True)
    env.image.calls.clear()
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e6", "style": "ink"}).json()
    edited = "A young woman holds a silver coin up to a narrow window. " + got["prompt"]
    n = len(env.text.prompts)
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e6", "style": "ink", "prompt": edited})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["prompt"] == " ".join(edited.split()) and rec["from"] == got["anchor"] and rec["kind"] == "event"
    assert len(env.text.prompts) == n                       # the prompt route's references are reused: no second call
    assert rec["references"] == [x["id"] for x in got["references"]] and env.image.calls[0]["references"]
    assert env.c.get(rec["url"]).status_code == 200
    assert any(p["id"] == rec["id"] for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"])


def test_a_refused_drawing_answers_with_the_reason(env):
    env.key()
    env.image.refuse = [lambda prompt, n: True, lambda prompt, n: True]
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate."})
    assert r.status_code == 422 and r.json()["code"] == "refused" and "safety" in r.json()["detail"]


def test_draw_without_an_image_key_is_refused(env):
    env.image = None
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate."})
    assert r.status_code == 409 and r.json()["code"] == "no-image-key"


def test_delete_and_undo(env):
    env.key()
    run_set(env, portraits=False)
    pics = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    pid = pics[0]["id"]
    r = env.c.delete(f"/api/library/{KEY}/art/{pid}")
    assert r.status_code == 200
    after = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert pid not in {p["id"] for p in after} and env.c.get(pics[0]["url"]).status_code == 404
    u = env.c.post(f"/api/library/{KEY}/undo/{r.json()['undo']}")
    assert u.status_code == 200 and u.json()["kind"] == "art"
    back = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert sorted(p["id"] for p in back) == sorted(p["id"] for p in pics) and env.c.get(pics[0]["url"]).status_code == 200
    assert env.c.delete(f"/api/library/{KEY}/art/p00000000").status_code == 404


def test_redraw_replaces_the_picture_and_undo_brings_it_back(env):
    env.key()
    run_set(env, portraits=False)
    old = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"][0]
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/redraw", json={"note": "Make it night"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["replaces"] == old["id"] and new["from"] == old["from"] and new["target"] == old["target"]
    assert env.image.calls[0]["prompt"].startswith("Make it night. ") and old["prompt"] in env.image.calls[0]["prompt"]
    now = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert old["id"] not in {p["id"] for p in now} and next(p for p in now if p["id"] == new["id"])["replaces"] == old["id"]
    assert env.c.get(old["url"]).status_code == 404
    folder = env.data / "art" / env.hash / "ink"
    assert env.c.post(f"/api/library/{KEY}/undo/{new['undo']}").status_code == 200
    back = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert old["id"] in {p["id"] for p in back} and new["id"] not in {p["id"] for p in back}
    assert env.c.get(old["url"]).status_code == 200 and not (folder / new["file"]).exists()


def test_a_redraw_of_a_picture_from_an_older_note_stays_no_reference(env):
    # its prompt carries the old note, so the new record must not claim the current note version
    import json
    env.key()
    run_set(env, portraits=False)
    path = env.data / "art" / env.hash / "ink" / "art.json"
    m = json.loads(path.read_text())
    assert all(p.get("notes") for p in m["pictures"])
    m["pictures"][0].pop("notes")
    path.write_text(json.dumps(m))
    old = m["pictures"][0]
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/redraw", json={})
    assert r.status_code == 200, r.text
    new = next(p for p in json.loads(path.read_text())["pictures"] if p["id"] == r.json()["id"])
    assert "notes" not in new


def test_a_redraw_keeps_the_earlier_books_its_prompt_carries(env):
    """The redrawn picture is drawn from the old prompt, so it carries the same earlier-book content: its `inherits` (and
    `partial`) go to the new record, else the Reader would show a prompt it hides until those books are finished. A record
    from before `inherits` existed gets none either, so the Reader still treats it as unknown."""
    env.key()
    run_set(env, portraits=False)
    path = env.data / "art" / env.hash / "ink" / "art.json"
    man = json.loads(path.read_text())
    first, second = man["pictures"][0], man["pictures"][1]
    first.update({"inherits": ["book-one"], "partial": True})
    second.pop("inherits", None)
    path.write_text(json.dumps(man))

    new = env.c.post(f"/api/library/{KEY}/art/{first['id']}/redraw", json={"note": "Closer"}).json()
    assert new["inherits"] == ["book-one"] and new.get("partial") is True
    stored = {p["id"]: p for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]}
    assert stored[new["id"]]["inherits"] == ["book-one"] and stored[new["id"]].get("partial") is True

    legacy = env.c.post(f"/api/library/{KEY}/art/{second['id']}/redraw", json={}).json()
    assert "inherits" not in legacy
    stored = {p["id"]: p for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]}
    assert "inherits" not in stored[legacy["id"]]


def test_a_redraw_keeps_the_story_time_its_picture_shows(env, monkeypatch):
    """`scope` is stored, never derived: the redrawn record takes the old one's as it is, and a record without one (a
    single-scope figure, an older set) gets none, even when the pipeline would now label it."""
    env.key()
    run_set(env, portraits=False)
    record = art.Illustrator._record
    monkeypatch.setattr(art.Illustrator, "_record", lambda self, *a: {**record(self, *a), "scope": "s9.9"})
    path = env.data / "art" / env.hash / "ink" / "art.json"
    man = json.loads(path.read_text())
    first, second = man["pictures"][0], man["pictures"][1]
    first["scope"] = "s2.1"
    second.pop("scope", None)
    path.write_text(json.dumps(man))

    new = env.c.post(f"/api/library/{KEY}/art/{first['id']}/redraw", json={"note": "Younger"}).json()
    assert new["scope"] == "s2.1"
    stored = {p["id"]: p for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]}
    assert stored[new["id"]]["scope"] == "s2.1" and first["id"] not in stored

    legacy = env.c.post(f"/api/library/{KEY}/art/{second['id']}/redraw", json={}).json()
    assert "scope" not in legacy
    assert all("scope" not in p for p in json.loads(path.read_text())["pictures"] if p["id"] == legacy["id"])
    for entry in (legacy["undo"], new["undo"]):      # undone newest first
        assert env.c.post(f"/api/library/{KEY}/undo/{entry}").status_code == 200
    back = {p["id"]: p for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]}
    assert back[first["id"]]["scope"] == "s2.1"


def test_the_manifest_route_gives_the_browser_the_timeline_and_each_scope(env):
    """The Reader picks a figure's picture by the scope of the block being read, so the set comes with `timeline.runs`
    and every picture with its `scope`; the strands' labels and the cue quotes stay on the server."""
    env.key()
    run_set(env, portraits=False)
    path = env.data / "art" / env.hash / "ink" / "art.json"
    man = json.loads(path.read_text())
    timeline = {"strands": [{"id": "s1", "label": "the frame", "book": "mill", "from": "a1"},
                            {"id": "s2", "label": "the miller's youth", "book": "mill", "from": "a9"}],
                "chapters": {"One": {"key": "k1", "strand": "s1", "era": 1, "cue": {"seq": 1, "quote": "The mill"},
                                     "events": {}},
                             "Two": {"key": "k2", "strand": "s2", "era": 1, "cue": {"seq": 9, "quote": "Years before"},
                                     "events": {"e3": "other"}}},
                "runs": [{"seq": 1, "scope": None}, {"seq": 1, "scope": "s1.1"},
                         {"seq": 8, "scope": None}, {"seq": 9, "scope": "s2.1"}]}
    man["timeline"] = timeline
    man["pictures"][0]["scope"] = "s2.1"
    path.write_text(json.dumps(man))

    body = env.c.get(f"/api/library/{KEY}/art?style=ink").json()
    assert body["timeline"]["runs"] == timeline["runs"]
    assert [s["id"] for s in body["timeline"]["strands"]] == ["s1", "s2"]
    assert all("label" not in s for s in body["timeline"]["strands"])
    two = body["timeline"]["chapters"]["Two"]
    assert two["cue"] == {"seq": 9} and two["strand"] == "s2" and two["events"] == {"e3": "other"}
    pics = {p["id"]: p for p in body["pictures"]}
    assert pics[man["pictures"][0]["id"]]["scope"] == "s2.1"
    assert json.loads(path.read_text())["timeline"] == timeline     # the stored set keeps its working notes


def test_a_refused_redraw_leaves_the_set_as_it_was(env):
    env.key()
    run_set(env, portraits=False)
    before = env.c.get(f"/api/library/{KEY}/art?style=ink").json()
    env.image.refuse = [lambda prompt, n: True, lambda prompt, n: True]
    r = env.c.post(f"/api/library/{KEY}/art/{before['pictures'][0]['id']}/redraw", json={})
    assert r.status_code == 422 and r.json()["code"] == "refused"
    after = env.c.get(f"/api/library/{KEY}/art?style=ink").json()
    assert [p["id"] for p in after["pictures"]] == [p["id"] for p in before["pictures"]]
    assert [json.loads(p.read_text())["art"]["op"] for p in (env.data / "merges").glob("*/entry.json")] == ["add"]   # the run's


def test_the_world_note_can_be_written_by_hand_and_undone(env):
    env.key()
    run_set(env, portraits=False)
    was = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["world"]
    r = env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A grey northern valley of mills, early iron age."})
    assert r.status_code == 200 and r.json()["world"]["edited"] is True
    assert env.c.get(f"/api/library/{KEY}/art?style=ink").json()["world"]["text"].startswith("A grey northern")
    assert env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": ""}).status_code == 422
    assert env.c.post(f"/api/library/{KEY}/undo/{r.json()['undo']}").status_code == 200
    assert env.c.get(f"/api/library/{KEY}/art?style=ink").json()["world"] == was


def test_an_edited_world_note_is_kept_by_the_next_run(env):
    env.key()
    env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A grey northern valley of mills."})
    run_set(env, portraits=False)
    w = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["world"]
    assert w["text"] == "A grey northern valley of mills." and w["edited"] is True
    assert all("visual world note" not in p for p in env.text.prompts)


def test_the_estimate_counts_without_a_call(env):
    env.key()
    got = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink", "level": "main"}).json()
    assert env.text.prompts == [] and [c["title"] for c in got["chapters"]] == ["Chapter One", "Chapter Two"]
    assert got["plates"] == 2 and got["portraits"] >= 1 and got["pictures"] == got["plates"] + got["portraits"]
    each = llm.image_price("gemini", GEMINI, "standard", "landscape")
    assert got["price"] == pytest.approx(2 * each + got["portraits"] * llm.image_price("gemini", GEMINI, "standard", "portrait"))
    every = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink", "level": "all", "portraits": "false"}).json()
    assert every["portraits"] == 0 and every["plates"] >= got["plates"]
    assert env.c.get("/api/art/estimate", params={"key": "nope", "style": "ink"}).status_code == 404


def test_an_unknown_index_or_a_missing_book_file(env):
    assert env.c.get("/api/library/nope/art").status_code == 404
    assert env.c.get(f"/api/art/{env.hash}/ink/p00000000").status_code == 404
    assert env.c.get("/api/art/xyz/ink/p00000000").status_code == 404
    got = env.c.get(f"/api/library/{KEY}/art").json()
    assert got["pictures"] == [] and got["styles"] == [] and got["slug"] is None


def test_mutating_routes_need_the_csrf_header(env):
    bare = TestClient(env.c.app)
    for method, url in (("post", f"/api/library/{KEY}/art/run"), ("post", f"/api/library/{KEY}/art/prompt"),
                        ("post", f"/api/library/{KEY}/art/draw"), ("delete", f"/api/library/{KEY}/art/p00000000"),
                        ("put", f"/api/library/{KEY}/art/world"), ("post", f"/api/library/{KEY}/art/p00000000/redraw"),
                        ("delete", f"/api/art/{env.hash}")):
        assert getattr(bare, method)(url).status_code == 403, url


# ---- round 2


def pictures(e, style="ink"):
    return e.c.get(f"/api/library/{KEY}/art", params={"style": style}).json()["pictures"]


def undo(e, entry):
    r = e.c.post(f"/api/library/{KEY}/undo/{entry}")
    assert r.status_code == 200, r.text
    return r.json()


def latest_entry(e):
    entries = [json.loads(p.read_text()) for p in (e.data / "merges").glob("*/entry.json")]
    return max(entries, key=lambda x: x["at"])


def test_undo_touches_only_the_records_of_its_entry(env):
    env.key()
    run_set(env, portraits=False)
    a = pictures(env)[0]
    deleted = env.c.delete(f"/api/library/{KEY}/art/{a['id']}").json()["undo"]
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink"}).json()
    b = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink", "prompt": got["prompt"]}).json()
    run = run_set(env, portraits=True)
    more = {p["id"] for p in pictures(env)} - {b["id"]} - {p["id"] for p in pictures(env) if p["kind"] != "portrait"}
    assert len(more) >= 3 and run["progress"]["artUndo"] == latest_entry(env)["id"]
    undo(env, run["progress"]["artUndo"])                       # the run's portraits go, B stays
    assert b["id"] in {p["id"] for p in pictures(env)} and not more & {p["id"] for p in pictures(env)}
    undo(env, b["undo"])
    assert b["id"] not in {p["id"] for p in pictures(env)}
    undo(env, deleted)
    assert a["id"] in {p["id"] for p in pictures(env)} and env.c.get(a["url"]).status_code == 200


def test_undoing_a_delete_never_takes_out_a_picture_drawn_outside_the_journal(env):
    env.key()
    run_set(env, portraits=False)
    a = pictures(env)[0]
    deleted = env.c.delete(f"/api/library/{KEY}/art/{a['id']}").json()["undo"]
    store = store_mod.ArtStore(env.data)
    store.put(env.hash, "ink", {"id": "p0000cccc", "file": "p0000cccc.png", "kind": "plate", "target": "x", "from": "a1"},
              png(2, 2, b"late"))
    undo(env, deleted)
    ids = {p["id"] for p in pictures(env)}
    assert {a["id"], "p0000cccc"} <= ids and (env.data / "art" / env.hash / "ink" / "p0000cccc.png").exists()


def test_a_style_is_its_slug_and_never_starts_the_set_over(env):
    env.key()
    run_set(env, portraits=False)
    n = len(pictures(env))
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "Ink"})
    assert r.status_code == 200, r.text
    assert env.c.put(f"/api/library/{KEY}/art/world", json={"style": "INK", "text": "A valley of mills."}).status_code == 200
    m = json.loads((env.data / "art" / env.hash / "ink" / "art.json").read_text())
    assert len(m["pictures"]) == n and m["style"] == "ink" and len(pictures(env, "Ink")) == n
    assert run_set(env, style="Ink", portraits=False)["report"]["art"]["drawn"] == 0     # the same set: nothing new to draw


def test_pictures_entries_keep_the_duplicates_panel_working(env):
    env.key()
    run_set(env, portraits=False)
    env.c.delete(f"/api/library/{KEY}/art/{pictures(env)[0]['id']}")
    entry = latest_entry(env)
    assert entry["keepNames"] == [] and entry["dropNames"] == []
    assert env.c.get(f"/api/library/{KEY}/duplicates").status_code == 200


def test_a_pictures_run_names_its_providers_and_does_not_make_ollama_look_busy(env, monkeypatch):
    env.key()

    class Tags:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"models": [{"name": llm.DEFAULT_MODEL}]}).encode()
    monkeypatch.setattr(web_app.urllib.request, "urlopen", lambda *a, **kw: Tags())
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: [])
    monkeypatch.setattr(jobs_mod.JobManager, "text_provider", lambda self, p=None: "claude")
    job = env.jobs.submit_art(KEY, {"style": "ink", "cap": 5}, index_path="/nowhere", book_id="", title="M", text_hash="ef" * 32)
    assert job.config["provider"] == "claude" and job.config["image_provider"] == "gemini"
    job.status = "running"
    assert env.c.get("/api/ask").json()["reason"] != "ollama-busy"
    job.config["provider"] = None
    assert env.c.get("/api/ask").json()["reason"] != "ollama-busy"
    job.status = "stopped"


def test_an_earlier_book_without_an_edition_is_skipped_not_fatal(env):
    env.key()
    real = env.jobs.art_inputs

    def with_bad_prior(job):
        doc, bk, _ = real(job)
        return doc, bk, [{"work": {"title": "Before Millbrook"}, "editions": [{"textHash": "not-a-hash"}], "anchors": [], "entities": []}]
    env.jobs.art_inputs = with_bad_prior
    done = run_set(env, portraits=False)
    assert done["status"] == "done" and done["report"]["art"]["drawn"] > 0
    assert "Before Millbrook names no edition" in (env.data / "jobs" / f"{done['id']}.events.jsonl").read_text()


def test_the_estimate_refuses_an_unknown_provider(env):
    r = env.c.get("/api/art/estimate", params={"key": KEY, "provider": "foo"})
    assert r.status_code == 422 and r.json()["code"] == "bad-provider"
    assert env.c.get("/api/art/estimate", params={"key": KEY, "provider": "gemini", "model": "nope"}).json()["code"] == "bad-model"


def test_the_prompt_cache_is_safe_across_threads(env):
    from concurrent.futures import ThreadPoolExecutor
    remember, recall, cache = env.c.app.state.art_prompts

    def one(i):
        remember(("k", i), f"prompt {i}")
        return recall(("k", i))
    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(one, range(200)))
    assert len(cache) == 32 and all(g is None or g.startswith("prompt") for g in got) and recall(("k", 199)) == "prompt 199"


def test_routes_for_one_index_do_not_scan_the_library(env, monkeypatch):
    env.key()
    run_set(env, portraits=False)
    n = []
    real = web_app.Library.scan
    monkeypatch.setattr(web_app.Library, "scan", lambda self: n.append(1) or real(self))
    env.c.get(f"/api/library/{KEY}/art", params={"style": "ink"})
    env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A valley."})
    assert n == [1]           # the world note's journal entry finds the book's group; reading the set scans nothing


def test_starting_a_run_works_off_the_event_loop(env, monkeypatch):
    env.key()
    names = []
    real = web_app.Catalog.entries
    monkeypatch.setattr(web_app.Catalog, "entries", lambda self: names.append(threading.current_thread().name) or real(self))
    run_set(env, portraits=False)
    assert names and all(x.startswith("asyncio_") for x in names)    # the loop's default executor, not the loop


def test_a_stop_in_the_pictures_stage_leaves_the_build_resumable(tmp_path, monkeypatch):
    from .test_web import SlowFake, upload
    monkeypatch.setattr(llm, "Ollama", lambda **kw: SlowFake())
    t = Text()
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: t)
    monkeypatch.setattr(jobs_mod.JobManager, "_portraits", lambda self, *a: {"skipped": "test"})
    monkeypatch.setattr(art, "WORKERS", 1)
    builds = []
    real_run = jobs_mod.generate.run
    monkeypatch.setattr(jobs_mod.generate, "run", lambda *a, **kw: builds.append(1) or real_run(*a, **kw))
    box = {}

    class Stopper(GeminiFake):
        def draw(self, prompt, **kw):
            if not box.get("stopped"):
                box["stopped"] = True
                mgr = box["mgr"]
                mgr.stop(next(j.id for j in mgr.jobs.values() if j.status == "running"))
            return super().draw(prompt, **kw)
    im = Stopper()
    monkeypatch.setattr(llm, "make_image", lambda *a, **kw: im)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    box["mgr"] = app.state.jobs
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        meta = upload(c, tmp_path)
        job = c.post("/api/jobs", json={"book_id": meta["id"], "config": {"chunk_words": 5000, "art": {"style": "ink", "cap": 10, "portraits": "all"}}}).json()
        stopped = wait_for(c, job["id"])
        assert stopped["status"] == "stopped" and stopped["progress"]["resumeFrom"] == "art", stopped
        first = len(im.calls)
        assert c.post(f"/api/jobs/{job['id']}/resume").status_code == 200
        done = wait_for(c, job["id"], ("done", "failed"))
    assert done["status"] == "done", done["error"] and len(builds) == 1
    assert len(im.calls) > first and "resumeFrom" not in done["progress"] and "arcs" in done["report"]


def test_the_prompt_route_keeps_no_world_note_for_a_picture_it_then_holds_back(env):
    env.key()
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e1", "style": "ink", "p": "a2"})
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    assert any("visual world note" in p for p in env.text.prompts)          # it was written, then not kept
    assert env.c.get(f"/api/library/{KEY}/art", params={"style": "ink"}).json()["world"] is None


def test_a_given_prompt_is_never_softened(env):
    env.key()
    run_set(env, portraits=False)
    old = pictures(env)[0]
    env.image.calls.clear()
    env.image.refuse = [lambda prompt, n: True, lambda prompt, n: True]
    env.text.prompts.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/redraw", json={"note": "At night"})
    assert r.status_code == 422 and r.json()["code"] == "refused" and len(env.image.calls) == 1
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate at dusk."})
    assert r.status_code == 422 and r.json()["code"] == "refused" and len(env.image.calls) == 2
    assert not any("quieter composition" in p for p in env.text.prompts)
    assert json.loads((env.data / "art" / env.hash / "ink" / "art.json").read_text())["skipped"] == []


def test_settings_lists_and_removes_a_books_pictures_with_undo(env):
    env.key()
    run_set(env, portraits=False)
    listed = env.c.get("/api/art").json()
    assert listed == [{"key": KEY, "title": "Millbrook", "textHash": env.hash, "bytes": listed[0]["bytes"],
                       "styles": [{"style": "ink", "slug": "ink", "pictures": len(pictures(env)), "bytes": listed[0]["bytes"]}]}]
    n = len(pictures(env))
    busy = jobs_mod.Job(id="art2", book_id="", book_title="M", config={"text_hash": env.hash}, output="", status="running", kind="art")
    env.jobs.jobs["art2"] = busy
    assert env.c.delete(f"/api/art/{env.hash}").status_code == 409
    busy.status = "done"
    r = env.c.delete(f"/api/art/{env.hash}", params={"style": "ink"})
    assert r.status_code == 200 and r.json()["removed"] == ["ink"]
    assert env.c.get("/api/art").json() == [] and pictures(env) == []
    undo(env, r.json()["undo"])
    assert len(pictures(env)) == n and env.c.get(pictures(env)[0]["url"]).status_code == 200
    assert env.c.delete(f"/api/art/{'00' * 32}").status_code == 404


def test_a_pictures_job_names_its_index(env):
    env.key()
    done = run_set(env, portraits=False)
    assert done["index"] == KEY == done["config"]["index_key"]
    assert all("index" not in j or j["kind"] == "art" for j in env.c.get("/api/jobs").json())


@pytest.fixture
def series_env(tmp_path, monkeypatch):
    """Millbrook as book two of a series whose book one is a slightly longer telling (its own edition and entity ids)."""
    epubs, idx = tmp_path / "epubs", tmp_path / "idx"
    epubs.mkdir()
    idx.mkdir()
    one = make_epub(epubs / "one.epub", title="Millbrook One", ch1=CH1, ch2=CH2 + ["The mill stood quiet under the winter moon that night."])
    two = make_epub(epubs / "two.epub", title="Millbrook", ch1=CH1, ch2=CH2)
    # Its own ids throughout, as a real series has (shared anchor ids across books are the pipeline's round 3, item 1).
    d1 = json.loads(re.sub(r'"(mira|oren|mill|stranger|gabriel|bk\.|[aer]\d)', r'"b1\1', json.dumps(mill_index(one))))
    d1.update({"id": "one", "series": {"id": "mill", "book": "one"}})
    d2 = mill_index(two)
    d2["series"] = {"id": "mill", "book": "two"}
    (idx / "one.json").write_text(json.dumps(d1))
    (idx / "mill.json").write_text(json.dumps(d2))
    (idx / "mill.series.json").write_text(json.dumps({
        "lorekeeper": "0.1", "kind": "series", "id": "mill", "title": "Mill",
        "books": [{"key": "one", "title": "Millbrook One"}, {"key": "two", "title": "Millbrook"}],
        "orders": {"pub": ["one", "two"]}, "defaultOrder": "pub"}))
    t = Text()
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: t)
    monkeypatch.setattr(llm, "make_image", lambda *a, **kw: GeminiFake())
    seen = []

    real = art.Illustrator.make_prompt

    def spy(self, job, finished=None):
        pr = real(self, job, finished)
        seen.append({"books": [b.doc["id"] for b in self.books], "finished": job.finished, "inherits": pr.inherits})
        return pr
    monkeypatch.setattr(art.Illustrator, "make_prompt", spy)
    app = web_app.create_app(tmp_path / "data", [("examples", idx)], [epubs], ollama_url="http://127.0.0.1:9",
                             allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        yield c, seen


def test_the_prompt_route_reads_only_the_books_the_reader_finished(series_env):
    c, seen = series_env
    ask = {"kind": "event", "target": "e5", "style": "ink", "provider": "openai", "quality": "draft"}
    for fin, read in (([], False), (["one"], True), (None, True)):
        r = c.post(f"/api/library/{KEY}/art/prompt", json={**ask, **({"finished": fin} if fin is not None else {})})
        assert r.status_code == 200, r.text
        got, s = r.json(), seen[-1]
        # Every earlier book finished is the pipeline's None (a run's view); none finished is the empty set.
        assert s["books"] == ["one", "mill"] and s["finished"] == (None if read else frozenset())
        assert got["slug"] == "ink" and got["inherits"] == s["inherits"]
        assert got["inherits"] == [] if not read else set(got["inherits"]) <= {"one"}
        assert got["partial"] is (not read)
        assert got["price"] == llm.image_price("openai", llm.IMAGE_PROVIDERS["openai"]["default"], "draft", got["shape"])
    for bad in (["two"], ["nope"], "one"):
        r = c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "finished": bad})
        assert r.status_code == 422 and r.json()["code"] == "bad-position", bad


def test_a_text_model_failure_is_a_502_not_a_missing_target(env, monkeypatch):
    env.key()

    def fails(self, job, finished=None):
        raise art.TextFailed("the text model failed twice")
    monkeypatch.setattr(art.Illustrator, "make_prompt", fails)
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink"})
    assert r.status_code == 502 and r.json()["code"] == "model-failed"


def world_of(e):
    p = e.data / "art" / e.hash / "ink" / "art.json"
    return json.loads(p.read_text()).get("world") if p.exists() else None


def test_a_draw_on_a_cache_miss_keeps_no_world_note_for_a_picture_it_then_holds_back(env):
    env.key()
    r = env.c.post(f"/api/library/{KEY}/art/draw",
                   json={"kind": "event", "target": "e1", "style": "ink", "p": "a2", "prompt": "A gate at dawn."})
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    assert any("visual world note" in p for p in env.text.prompts)          # it was written, then not kept
    assert world_of(env) is None and env.image.calls == []


@pytest.mark.parametrize("route", ["draw", "prompt"])
def test_a_dropped_prompt_keeps_no_world_note_it_wrote(env, route):
    env.key()
    env.text.scene = lambda prompt: SCENE_WITH_NAME                        # the writer names someone the book never does: dropped
    r = env.c.post(f"/api/library/{KEY}/art/{route}", json={"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate."})
    assert r.status_code == 422 and r.json()["code"] == "dropped", r.text
    # the check is named for the person, but never the name itself: the book has not used it (it could be a spoiler)
    assert r.json()["why"] == "a name the book has not used by this point" and "Valdemar" not in r.text
    assert any("visual world note" in p for p in env.text.prompts) and world_of(env) is None
    assert env.image.calls == []


def test_a_world_note_already_there_is_never_taken_back(env):
    env.key()
    env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A grey valley of mills."})
    env.text.scene = lambda prompt: SCENE_WITH_NAME
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate."})
    assert r.status_code == 422 and world_of(env)["text"] == "A grey valley of mills."


# a name the book never uses (the writer's memory): dropped after the rewrite, since only a known name can be replaced by a role
SCENE_WITH_NAME = ("At a wooden gate beside a grey stone mill, Valdemar waits with a basket on his arm, while a miller dusted with "
                   "flour watches from the doorway. Soft morning light falls across the packed earth of the yard and a path leads "
                   "off toward low green hills in the distance under a pale sky.")


# ---- choosing the image model


def test_the_providers_list_carries_each_image_table(env):
    got = {p["id"]: p for p in env.c.get("/api/providers").json()["providers"]}
    assert got["claude"]["images"] is None and got["mistral"]["images"] is None
    gem = got["gemini"]["images"]
    assert gem["default"] == llm.IMAGE_PROVIDERS["gemini"]["default"]
    models = {m["id"]: m for m in gem["models"]}
    assert set(models) == {m["id"] for m in llm.IMAGE_PROVIDERS["gemini"]["models"]}
    assert models["gemini-3-pro-image"]["label"] == "Nano Banana Pro"
    assert models["gemini-3-pro-image"]["price"]["standard"] == llm.image_price("gemini", "gemini-3-pro-image", "standard")
    assert models["gemini-3.1-flash-lite-image"]["levels"] == ["draft", "standard"]
    assert models[GEMINI]["levels"] == ["draft", "standard", "best"]
    assert got["openai"]["images"]["models"]


def test_the_chosen_image_model_reaches_the_provider_and_the_price(env, monkeypatch):
    env.key()
    asked = []

    def make(provider, model=None, api_key=None, base_url=None):
        asked.append((provider, model))
        return env.image
    monkeypatch.setattr(llm, "make_image", make)
    pro = "gemini-3-pro-image"
    q = {"kind": "event", "target": "e5", "style": "ink", "provider": "gemini", "model": pro}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=q).json()
    assert got["model"] == pro and got["price"] == llm.image_price("gemini", pro, "standard", "landscape")
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={**q, "prompt": got["prompt"]})
    assert r.status_code == 200, r.text
    assert asked and all(a == ("gemini", pro) for a in asked)
    est = env.c.get("/api/art/estimate", params={"key": KEY, "provider": "gemini", "model": pro}).json()
    assert est["model"] == pro


# ---- which portraits a run draws


def test_the_art_config_takes_main_all_or_false_for_portraits():
    from lorekeeper.web.jobs import art_config
    base = {"style": "ink", "cap": 10}
    assert art_config(base)["portraits"] == "main"                           # missing: the main cast
    assert art_config({**base, "portraits": True})["portraits"] == "all"      # an older page or a resumed older job
    assert art_config({**base, "portraits": "all"})["portraits"] == "all"
    assert art_config({**base, "portraits": "main"})["portraits"] == "main"
    assert art_config({**base, "portraits": False})["portraits"] is False
    for bad in ("none", "everyone", 1, 0, None, []):
        with pytest.raises(ValueError):
            art_config({**base, "portraits": bad})
    stored = art_config({**base, "portraits": "all"})
    assert art_config(stored) == stored and art_config(art_config(base)) == art_config(base)    # a resume reads it back


def test_the_estimate_counts_portraits_of_the_main_cast_by_default(env):
    env.key()

    def ports(**q):
        r = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink", **q})
        assert r.status_code == 200, r.text
        return r.json()["portraits"]
    every = ports(portraits="all")
    assert ports() == ports(portraits="main") < every and ports(portraits="true") == every
    assert ports(portraits="none") == ports(portraits="false") == 0
    assert env.c.get("/api/art/estimate", params={"key": KEY, "portraits": "some"}).json()["code"] == "bad-config"
