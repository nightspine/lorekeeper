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

from .test_art import CH1, CH2, WRITERS, Image, Text, base_doc, book, is_portrait, png  # noqa: E402
from .test_generate import make_epub  # noqa: E402

KEY = "examples--mill"
GEMINI = "gemini-nano-banana-2.1"


class GeminiFake(Image):
    provider, name = "gemini", GEMINI


@pytest.fixture(autouse=True)
def short_world(monkeypatch):
    monkeypatch.setattr(art, "WORLD_MIN_WORDS", 1, raising=False)   # an older pipeline's era note: nothing is held back
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
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: e.text)
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
    assert got["slug"] == "ink" and got["styles"][0]["slug"] == "ink"
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
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: t)   # the portrait writer
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


def test_the_sheet_writes_with_the_strong_tier_and_runs_with_the_cheap_one(env, monkeypatch):
    env.key()
    strong = Text()
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: strong)
    monkeypatch.setattr(jobs_mod.JobManager, "text_choice",
                        lambda self, tier="cheap", provider=None: ("gemini", f"gemini-{tier}"))
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink"}).json()
    assert strong.prompts and not env.text.prompts
    assert got["writer"] == {"provider": "gemini", "model": "gemini-strong"} and got["model"] == GEMINI
    n = len(strong.prompts)                                 # a draw on a cache miss writes as the sheet does
    ask = {"kind": "event", "target": "e6", "style": "ink", "prompt": "A gate."}
    assert env.c.post(f"/api/library/{KEY}/art/draw", json=ask).status_code == 200
    assert len(strong.prompts) > n and not env.text.prompts
    run_set(env, portraits=False)                           # a run keeps the cheap tier
    assert env.text.prompts and len(strong.prompts) > n


def test_without_a_text_model_the_prompt_route_says_so(env, monkeypatch):
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: None)
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: None)
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


def test_a_draw_from_the_sheet_can_replace_a_picture_and_undo_brings_it_back(env):
    """Redraw with a new prompt: the sheet's draw names the picture it takes the place of; the old one leaves the set as a
    redraw does it, in one `redraw` journal entry."""
    env.key()
    run_set(env, portraits=False)
    pics = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    old = pics[0]
    ask = {"kind": old["kind"], "target": old["target"], "style": "ink", "prompt": "A mill wheel at dusk.", "p": 1.0,
           "replaces": old["id"]}
    r = env.c.post(f"/api/library/{KEY}/art/draw", json=ask)
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["replaces"] == old["id"] and new["prompt"] == "A mill wheel at dusk." and new["target"] == old["target"]
    now = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert old["id"] not in {p["id"] for p in now} and next(p for p in now if p["id"] == new["id"])["replaces"] == old["id"]
    assert len(now) == len(pics) and env.c.get(old["url"]).status_code == 404
    entry = json.loads((env.data / "merges" / new["undo"] / "entry.json").read_text())
    assert entry["kind"] == "art" and entry["art"]["op"] == "redraw" and entry["art"]["added"] == [new["id"]]
    assert env.c.post(f"/api/library/{KEY}/undo/{new['undo']}").status_code == 200
    back = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert old["id"] in {p["id"] for p in back} and new["id"] not in {p["id"] for p in back}
    assert env.c.get(old["url"]).status_code == 200


def test_a_draw_without_replaces_adds_a_version_beside_the_old_one(env):
    env.key()
    run_set(env, portraits=False)
    pics = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    old = pics[0]
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": old["kind"], "target": old["target"], "style": "ink",
                                                          "prompt": "A mill wheel at dusk."})
    assert r.status_code == 200 and "replaces" not in r.json()
    now = {p["id"] for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]}
    assert old["id"] in now and r.json()["id"] in now


def test_replaces_must_name_a_visible_picture_of_the_same_target_in_the_set(env):
    env.key()
    run_set(env, portraits=False)
    pics = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    old = pics[0]
    other = next(p for p in pics if (p["kind"], p["target"]) != (old["kind"], old["target"]))
    ask = {"kind": old["kind"], "target": old["target"], "style": "ink", "prompt": "A mill wheel at dusk.", "p": 1.0}
    calls = len(env.image.calls)
    for bad, code in ((other["id"], "bad-replaces"), ("p00000000", "bad-replaces"), (7, "bad-replaces")):
        r = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "replaces": bad})
        assert r.status_code == 422 and r.json()["code"] == code, (bad, r.text)
    # a picture of another style's set is not this set's
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "watercolour", "replaces": old["id"]})
    assert r.status_code == 422 and r.json()["code"] == "bad-replaces"
    assert len(env.image.calls) == calls                          # refused before anything is drawn
    assert {p["id"] for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]} == {p["id"] for p in pics}


def test_replaces_never_names_a_picture_past_the_position(env):
    """A picture whose anchor is past the reader's position is refused like the target, even when the target is reached."""
    env.key()
    run_set(env, portraits=False)
    path = env.data / "art" / env.hash / "ink" / "art.json"
    man = json.loads(path.read_text())
    old = next(p for p in man["pictures"] if p["target"] == "Chapter One")
    ask = {"kind": old["kind"], "target": old["target"], "style": "ink", "prompt": "A gate.", "p": 0.9, "replaces": old["id"]}
    old["from"] = env.doc["anchors"][-1]["id"]                     # past the position asked with
    path.write_text(json.dumps(man))
    calls = len(env.image.calls)
    r = env.c.post(f"/api/library/{KEY}/art/draw", json=ask)
    assert r.status_code == 422 and r.json()["code"] == "not-visible" and "picture" in r.json()["detail"], r.text
    assert len(env.image.calls) == calls


def fake_soften(monkeypatch, seen):
    """`Illustrator.soften` as the pipeline shapes it (job, text, reason, finished) -> Prompt: here make_prompt with a
    quieter body, so the route is tested without the pipeline's own rewrite."""
    import dataclasses

    def soften(self, job, text_, reason, finished=None):
        seen.append({"job": job, "text": text_, "reason": reason, "finished": finished})
        pr = self.make_prompt(job)
        quiet = "A figure carries a covered shape along a muddy road at dusk."
        return dataclasses.replace(pr, body=quiet, scene=quiet, text=" ".join([quiet, pr.style, pr.no_text]).strip())
    monkeypatch.setattr(art.Illustrator, "soften", soften, raising=False)


def test_soften_rewrites_a_refused_prompt_in_the_prompt_routes_shape(env, monkeypatch):
    env.key()
    seen = []
    fake_soften(monkeypatch, seen)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e6", "style": "ink"}).json()
    ask = {"kind": "event", "target": "e6", "style": "ink", "text": got["prompt"], "reason": "IMAGE_SAFETY", "p": 1.0}
    r = env.c.post(f"/api/library/{KEY}/art/soften", json=ask)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(got) == set(out) and out["body"].startswith("A figure carries a covered shape")
    assert out["style"] == got["style"] and out["kind"] == "event" and out["target"] == "e6" and env.image.calls == []
    assert seen[0]["text"] == " ".join(got["prompt"].split()) and seen[0]["reason"] == "IMAGE_SAFETY"
    assert seen[0]["finished"] is None and seen[0]["job"].target == "e6"
    # never cached as the job's prompt: the prompt route's own stays what a draw reuses
    n = len(env.text.prompts)
    d = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e6", "style": "ink",
                                                          "prompt": out["prompt"]})
    assert d.status_code == 200 and d.json()["prompt"] == " ".join(out["prompt"].split()) and len(env.text.prompts) == n


def test_soften_keeps_the_prompt_routes_gates(env, monkeypatch):
    env.key()
    seen = []
    fake_soften(monkeypatch, seen)
    base = {"kind": "event", "target": "e5", "style": "ink", "text": "A hanging.", "reason": "IMAGE_SAFETY"}
    r = env.c.post(f"/api/library/{KEY}/art/soften", json={**base, "p": "a2"})
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    assert env.c.post(f"/api/library/{KEY}/art/soften", json={**base, "text": ""}).json()["code"] == "bad-prompt"
    assert env.c.post(f"/api/library/{KEY}/art/soften", json={**base, "reason": "x" * 301}).json()["code"] == "bad-reason"
    assert env.c.post(f"/api/library/{KEY}/art/soften", json={**base, "finished": ["nope"]}).json()["code"] == "bad-position"
    assert env.c.post(f"/api/library/{KEY}/art/soften", json={**base, "target": "e99"}).status_code == 404
    assert seen == []                                              # refused before any writer call
    r = env.c.post(f"/api/library/{KEY}/art/soften", json=base, headers={"X-Lorekeeper": ""})
    assert r.status_code == 403


def test_the_store_gives_the_other_style_sets_as_read_only_likeness_sinks(tmp_path):
    s = store_mod.ArtStore(tmp_path)
    h = "ef" * 32
    for style, pid in (("ink", "p0000aaaa"), ("watercolour", "p0000bbbb")):
        s.put(h, style, {"id": pid, "file": f"{pid}.png", "kind": "portrait"}, png(2, 2, b"x"))
    got = s.likeness(h, "Ink")                       # a style by name: its own set is never a likeness of itself
    assert list(got) == ["watercolour"] and got["watercolour"].manifest()["style"] == "watercolour"
    with pytest.raises(PermissionError):
        got["watercolour"].save({})
    with pytest.raises(PermissionError):
        got["watercolour"].put("y.png", b"y")
    assert s.likeness(h, "comic").keys() == {"ink", "watercolour"} and s.likeness("00" * 32, "ink") == {}


@pytest.fixture
def books_seen(monkeypatch):
    """Every pipeline `Book` the app builds."""
    seen = []
    real = art.Book

    def make(*a, **k):
        b = real(*a, **k)
        seen.append(b)
        return b
    monkeypatch.setattr(store_mod.art, "Book", make)
    return seen


def test_routes_and_runs_hand_the_pipeline_the_other_sets_as_likeness(env, books_seen):
    env.key()
    run_set(env, portraits="all")                                  # ink pictures, portraits among them
    assert books_seen[-1].likeness == {}                           # the first set: no other style yet
    books_seen.clear()
    ask = {"kind": "event", "target": "e6", "style": "Watercolour"}
    assert env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).status_code == 200
    assert list(books_seen[-1].likeness) == ["ink"]
    books_seen.clear()
    assert env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": "A gate."}).status_code == 200
    assert list(books_seen[-1].likeness) == ["ink"]
    books_seen.clear()
    run_set(env, style="watercolour", portraits=False)             # the run stage
    assert set(books_seen[-1].likeness) == {"ink"}
    books_seen.clear()
    env.c.post(f"/api/library/{KEY}/art/soften", json={**ask, "text": "A gate.", "reason": "IMAGE_SAFETY"})
    assert books_seen and set(books_seen[-1].likeness) == {"ink"}
    ink = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert ink == env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]   # never written through


def test_a_scene_in_another_style_takes_the_figures_likeness_from_a_portrait(env, monkeypatch):
    """Comic has no portraits, ink has: the comic scene sends the ink portraits of the figures it shows, read from the
    ink set and recorded as `{set, id}`, with the likeness-only words; the ink set is never written."""
    env.key()
    run_set(env, portraits="all")
    portraits = {p["target"]: p["id"] for p in pictures(env) if p["kind"] == "portrait"}
    assert portraits
    real = art.Illustrator.make_prompt

    def with_shows(self, job, finished=None):
        pr = real(self, job, finished)
        pr.shows = list(portraits)
        return pr
    monkeypatch.setattr(art.Illustrator, "make_prompt", with_shows)
    ink_before = (env.data / "art" / env.hash / "ink" / "art.json").read_bytes()
    env.image.calls.clear()
    ask = {"kind": "event", "target": "e6", "style": "comic"}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    assert got["references"] and all(x["set"] == "ink" and "/ink/" in x["url"] for x in got["references"])
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["references"] and all(x["set"] == "ink" and x["id"] in portraits.values() for x in rec["references"])
    call = env.image.calls[-1]
    assert call["references"] and len(call["references"]) == len(rec["references"])
    # which reference is which labelled figure, then the likeness words, numbered the same (one note)
    labels = [f["label"] for f in rec["figures"] if f["ref"]]
    assert call["note"] == art.Illustrator.reference_note("event", [(0, {"set": "ink", "kind": "portrait"})] * len(rec["references"]),
                                                          labels) != ""
    assert call["note"].endswith(art.CHEST_UP)
    assert "likeness" in call["note"] and call["note"] not in call["prompt"]
    assert (env.data / "art" / env.hash / "ink" / "art.json").read_bytes() == ink_before
    assert rec["prompt"] == got["prompt"]                          # the likeness words are not in the person's text


def test_a_redraw_finds_a_likeness_reference_in_its_own_set(env, monkeypatch):
    """A record holding a `{set, id}` reference is redrawn with that picture read from the other set (and a plain id from
    its own), never failing on the dict."""
    env.key()
    run_set(env, portraits="all")
    portraits = {p["target"]: p["id"] for p in pictures(env) if p["kind"] == "portrait"}
    real = art.Illustrator.make_prompt

    def with_shows(self, job, finished=None):
        pr = real(self, job, finished)
        pr.shows = list(portraits)
        return pr
    monkeypatch.setattr(art.Illustrator, "make_prompt", with_shows)
    ask = {"kind": "event", "target": "e6", "style": "comic"}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    first = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]}).json()
    assert first["references"] and isinstance(first["references"][0], dict)
    # a mixed record: a likeness, a gone one and a malformed one are all read without failing
    path = env.data / "art" / env.hash / "comic" / "art.json"
    man = json.loads(path.read_text())
    rec = next(p for p in man["pictures"] if p["id"] == first["id"])
    rec["references"] = rec["references"] + [{"set": "ink", "id": "p00000000"}, {"set": 3}, 7]
    path.write_text(json.dumps(man))
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{first['id']}/redraw", json={"note": "Closer"})
    assert r.status_code == 200, r.text
    assert r.json()["references"] == first["references"]
    assert len(env.image.calls[-1]["references"]) == len(first["references"])


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
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: t)   # the portrait writer
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


def test_the_prompt_route_writes_no_world_note_and_holds_nothing_back(env):
    env.key()
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e1", "style": "ink", "p": "a2"})
    assert r.status_code == 200, r.text                     # no era note, so nothing is held to its anchor
    assert not any("visual world note" in p for p in env.text.prompts)
    assert world_of(env) is None


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
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: t)
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
        c.text, c.data_dir, c.hash_one = t, tmp_path / "data", store_mod.text_hash(d1)
        yield c, seen


def earlier_portrait(c, eid="mira") -> str:
    """Book one's ink set gets a current portrait of `eid`, an entity book two shows: a reference only for a reader who
    finished book one."""
    folder = c.data_dir / "art" / c.hash_one / "ink"
    folder.mkdir(parents=True, exist_ok=True)
    data = png(64, 64, b"book one")
    rec = {"id": "p0000b001", "kind": "portrait", "target": eid, "chapter": "One", "from": "b1a3", "file": "p0000b001.png",
           "mime": "image/png", "width": 64, "height": 64, "prompt": "A slight young woman.", "references": [],
           "sources": ["b1a3"], "inherits": [], "notes": art.NOTE_VERSION}
    (folder / rec["file"]).write_bytes(data)
    m = art.blank(c.hash_one, "ink")
    m["pictures"] = [rec]
    (folder / "art.json").write_text(json.dumps(m))
    return rec["id"]


def test_a_prompt_cached_for_every_book_finished_is_not_drawn_for_a_reader_with_none(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    ask = {"kind": "event", "target": "e6", "style": "ink"}
    assert c.post(f"/api/library/{KEY}/art/prompt", json=ask).status_code == 200       # no `finished`: every book read
    n = len(c.text.prompts)
    r = c.post(f"/api/library/{KEY}/art/draw", json={**ask, "finished": [], "prompt": "A gate."})
    assert r.status_code == 200, r.text
    assert len(c.text.prompts) > n                          # [] is not None: the cache missed and wrote for this reader
    assert r.json()["partial"] is True
    n = len(c.text.prompts)
    assert c.post(f"/api/library/{KEY}/art/draw", json={**ask, "finished": [], "prompt": "A gate."}).status_code == 200
    assert len(c.text.prompts) == n                         # and [] is its own key, used again


def test_a_scene_drawn_in_another_style_never_takes_an_unfinished_books_picture(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    pid = earlier_portrait(c)
    ask = {"kind": "event", "target": "e6"}
    # every earlier book read: book one's portrait is a reference in ink (the setup holds)
    got = c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "style": "watercolour"}).json()
    rec = c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "ink", "prompt": got["prompt"]}).json()
    assert pid in rec["references"] and "one" in rec["inherits"]
    # book one not finished: the prompt written in watercolour, drawn in ink, never takes it
    got = c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "style": "watercolour", "finished": []}).json()
    rec = c.post(f"/api/library/{KEY}/art/draw",
                 json={**ask, "style": "ink", "finished": [], "prompt": got["prompt"]}).json()
    assert pid not in rec["references"] and "one" not in rec["inherits"] and rec["partial"] is True


def test_a_draw_on_a_miss_of_other_text_is_hidden_until_every_earlier_book_is_finished(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    ask = {"kind": "event", "target": "e5", "style": "ink", "finished": []}
    # text from somewhere else (a prompt written for a reader with book one finished, say): every earlier book
    rec = c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": "A gate at dusk."}).json()
    assert rec["inherits"] == ["one"] and rec["partial"] is True
    # the prompt just written for this reader, drawn as written: only what it carries
    got = c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "target": "e6", "style": "anime"}).json()
    remember, recall, cache = c.app.state.art_prompts
    cache.clear()                                           # a miss again, and the text is the fresh prompt's own
    rec = c.post(f"/api/library/{KEY}/art/draw", json={**ask, "target": "e6", "style": "anime", "prompt": got["prompt"]}).json()
    assert rec["inherits"] == got["inherits"] == []


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


def shown_world(e, style="ink"):
    """The world note the Reader gets for a style: the edition's background note, set or no set."""
    return e.c.get(f"/api/library/{KEY}/art", params={"style": style}).json()["world"]


def world_of(e, style="ink"):
    p = e.data / "art" / e.hash / style / "art.json"
    return json.loads(p.read_text()).get("world") if p.exists() else None


def test_a_draw_on_a_cache_miss_writes_the_prompt_once_and_no_world_note(env):
    env.key()
    ask = {"kind": "event", "target": "e1", "style": "ink", "p": "a2", "prompt": "A gate at dawn."}
    assert env.c.post(f"/api/library/{KEY}/art/draw", json=ask).status_code == 200
    n = len(env.text.prompts)
    assert n and not any("visual world note" in p for p in env.text.prompts) and world_of(env) is None
    assert env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "anime"}).status_code == 200
    assert len(env.text.prompts) == n                       # the miss was remembered: the next draw asks nothing


@pytest.mark.parametrize("route", ["draw", "prompt"])
def test_a_dropped_prompt_writes_no_world_note(env, route):
    env.key()
    env.text.scene = lambda prompt: SCENE_WITH_NAME                        # the writer names someone the book never does: dropped
    r = env.c.post(f"/api/library/{KEY}/art/{route}", json={"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate."})
    assert r.status_code == 422 and r.json()["code"] == "dropped", r.text
    # the check is named for the person, but never the name itself: the book has not used it (it could be a spoiler)
    assert r.json()["why"] == "a name the book has not used by this point" and "Valdemar" not in r.text
    assert not any("visual world note" in p for p in env.text.prompts) and world_of(env) is None
    assert env.image.calls == []


def test_a_world_note_already_there_is_never_taken_back(env):
    env.key()
    env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A grey valley of mills."})
    env.text.scene = lambda prompt: SCENE_WITH_NAME
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate."})
    assert r.status_code == 422 and shown_world(env)["text"] == "A grey valley of mills."


# a name the book never uses (the writer's memory): dropped after the rewrite, since only a known name can be replaced by a role
SCENE_WITH_NAME = ("At a wooden gate beside a grey stone mill, Valdemar waits with a basket on his arm, while a miller dusted with "
                   "flour watches from the doorway. Soft morning light falls across the packed earth of the yard and a path leads "
                   "off toward low green hills in the distance under a pale sky.")


# ---- style and options never write a new prompt


def test_the_prompt_comes_in_parts_with_every_style_sentence(env):
    env.key()
    custom = {"kind": "event", "target": "e5", "style": "Moody blue gouache", "prompt": "A gate."}
    assert env.c.post(f"/api/library/{KEY}/art/draw", json=custom).status_code == 200
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink"}).json()
    assert got["prompt"] == " ".join([got["body"], got["style"], got["noText"]])
    assert got["style"] == art.STYLES["ink"] and got["noText"] == art.NO_TEXT
    assert art.STYLES["ink"] not in got["body"] and art.NO_TEXT not in got["body"]
    # every listed style, and the edition's own custom set by its stored text, so the sheet swaps a sentence with no call
    assert {k: got["styles"][k] for k in art.STYLES} == art.STYLES
    assert got["styles"]["moody-blue-gouache"] == "Moody blue gouache"
    for old in ("prompt", "anchor", "references", "price", "slug", "inherits", "shape", "kind", "target"):
        assert old in got


def test_drawing_in_another_style_or_shape_reuses_the_prompt_with_no_call(env):
    env.key()
    ask = {"kind": "event", "target": "e5", "style": "ink"}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    n = len(env.text.prompts)
    swapped = " ".join([got["body"], got["styles"]["watercolour"], got["noText"]])
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "watercolour", "shape": "square", "prompt": swapped})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert len(env.text.prompts) == n                       # neither the style nor the shape asks the writer again
    assert rec["prompt"] == swapped and rec["from"] == got["anchor"] and rec["shape"] == "square"
    assert env.image.calls[-1]["shape"] == "square" and "/watercolour/" in rec["url"]
    assert pictures(env, "watercolour")[0]["id"] == rec["id"] and pictures(env) == []
    custom = {**ask, "style": "Moody blue gouache", "prompt": got["body"] + " Moody blue gouache. " + got["noText"]}
    r = env.c.post(f"/api/library/{KEY}/art/draw", json=custom)
    assert r.status_code == 200 and "/moody-blue-gouache/" in r.json()["url"] and len(env.text.prompts) == n
    # the sheet files and shows a custom-style picture under the set's slug, which only the server knows
    assert r.json()["slug"] == "moody-blue-gouache"


def test_a_prompt_drawn_into_another_set_takes_its_references_from_that_set(env):
    env.key()
    run_set(env, portraits=True)                            # ink has portraits; watercolour has none
    ask = {"kind": "event", "target": "e6", "style": "ink"}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    assert got["references"]
    env.image.calls.clear()
    ink_before = pictures(env)
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "watercolour", "prompt": got["prompt"]}).json()
    # watercolour has no portraits of its own: the ink ones go as likenesses, named by their set, never as watercolour's
    assert rec["references"] == [{"set": "ink", "id": x["id"]} for x in got["references"]]
    assert len(env.image.calls[0]["references"]) == len(got["references"]) and pictures(env) == ink_before
    # and back in ink, the ink portraits again, as this set's own
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]}).json()
    assert rec["references"] == [x["id"] for x in got["references"]]


def test_a_prompt_made_in_an_empty_set_takes_the_shown_figures_references_in_another(env, monkeypatch):
    env.key()
    run_set(env, portraits=True)
    portraits = {p["target"]: p["id"] for p in pictures(env) if p["kind"] == "portrait"}
    real = art.Illustrator.make_prompt

    def with_shows(self, job, finished=None):            # the pipeline's Prompt.shows: the entities the picture shows
        pr = real(self, job, finished)
        pr.shows = list(portraits)
        return pr
    monkeypatch.setattr(art.Illustrator, "make_prompt", with_shows)
    ask = {"kind": "event", "target": "e6", "style": "watercolour"}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    # watercolour has no pictures: the shown figures' ink portraits are its likenesses, each served from the ink set
    assert got["references"] and all(x["set"] == "ink" and f"/ink/{x['id']}" in x["url"] for x in got["references"])
    assert all(env.c.get(x["url"]).status_code == 200 for x in got["references"])
    n = len(env.text.prompts)
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "ink", "prompt": got["prompt"]}).json()
    assert len(env.text.prompts) == n and rec["references"]
    assert set(rec["references"]) <= set(portraits.values())        # in ink they are its own: plain ids


def test_the_background_note_is_the_books_in_every_style(env):
    env.key()
    run_set(env, portraits=False)
    ask = {"kind": "event", "target": "e5"}
    env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "watercolour", "prompt": "A gate."})
    r = env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A grey northern valley of mills."})
    assert r.status_code == 200
    for st in ("ink", "watercolour"):
        assert world_of(env, st)["text"] == "A grey northern valley of mills." and world_of(env, st)["edited"] is True
    # a style with no set yet reads it
    assert env.c.get(f"/api/library/{KEY}/art?style=anime").json()["world"]["text"].startswith("A grey northern")
    undo(env, r.json()["undo"])                            # one entry: every set it wrote goes back
    for st in ("ink", "watercolour"):
        assert world_of(env, st) is None or not world_of(env, st).get("edited")
    # and a set made after the note starts with it
    env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A grey northern valley of mills."})
    assert env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "anime", "prompt": "A gate."}).status_code == 200
    assert world_of(env, "anime")["text"].startswith("A grey northern")
    m = json.loads((env.data / "art" / env.hash / "anime" / "art.json").read_text())
    ink = json.loads((env.data / "art" / env.hash / "ink" / "art.json").read_text())
    assert m["style"] == "anime" and m["textHash"] == ink["textHash"]


def old_set(env, style, world, picture=False):
    """A set as an older server left it: its own edited world note."""
    folder = env.data / "art" / env.hash / style
    folder.mkdir(parents=True, exist_ok=True)
    m = art.blank(env.hash, style)
    m["world"] = {"text": world, "edited": True, "from": env.doc["anchors"][0]["id"]}
    (folder / "art.json").write_text(json.dumps(m))
    return folder


def test_the_background_note_is_one_file_per_edition_and_a_draw_never_changes_it(env):
    env.key()
    old_set(env, "ink", "An older note, written in ink.")
    time.sleep(0.02)
    old_set(env, "watercolour", "The newest note, written in watercolour.")
    # first read: the newest edited note becomes the edition's file
    assert shown_world(env, "anime")["text"] == "The newest note, written in watercolour."
    bg = json.loads((env.data / "art" / env.hash / "background.json").read_text())
    assert bg["text"] == "The newest note, written in watercolour." and bg["edited"] is True
    # drawing rewrites ink's art.json (now the newest file): the edition's note stays, in every style and for the writer
    assert env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink",
                                                            "prompt": "A gate."}).status_code == 200
    for st in ("ink", "watercolour", "anime"):
        assert shown_world(env, st)["text"] == "The newest note, written in watercolour."
    assert env.c.app.state.jobs.art_store.sink(env.hash, "ink").manifest()["world"]["text"].startswith("The newest")
    # a note written now replaces it in the file and every set; undo puts both back
    r = env.c.put(f"/api/library/{KEY}/art/world", json={"style": "anime", "text": "A grey valley of mills."})
    assert json.loads((env.data / "art" / env.hash / "background.json").read_text())["text"] == "A grey valley of mills."
    assert world_of(env, "ink")["text"] == world_of(env, "watercolour")["text"] == "A grey valley of mills."
    assert not (env.data / "art" / env.hash / "anime").exists()     # the note needs no set of its own
    undo(env, r.json()["undo"])
    assert shown_world(env, "anime")["text"] == "The newest note, written in watercolour."
    assert world_of(env, "ink")["text"] == "The newest note, written in watercolour."   # as the draw had written it


def test_a_world_note_that_fails_halfway_puts_back_what_it_wrote(env, monkeypatch):
    old_set(env, "ink", "Ink's note.")
    old_set(env, "watercolour", "Watercolour's note.")
    assert shown_world(env)["text"]                         # the file exists before the write
    before = (env.data / "art" / env.hash / "background.json").read_text()
    real, n = store_mod.ArtStore.save, []

    def flaky(self, h, style, m):
        n.append(style)
        if len(n) == 2:
            raise OSError("disk full")
        return real(self, h, style, m)
    monkeypatch.setattr(store_mod.ArtStore, "save", flaky)
    with pytest.raises(OSError):
        env.c.put(f"/api/library/{KEY}/art/world", json={"style": "ink", "text": "A grey valley of mills."})
    monkeypatch.setattr(store_mod.ArtStore, "save", real)
    assert world_of(env, "ink")["text"] == "Ink's note." and world_of(env, "watercolour")["text"] == "Watercolour's note."
    assert (env.data / "art" / env.hash / "background.json").read_text() == before
    journal = [json.loads(p.read_text()) for p in (env.data / "merges").glob("*/entry.json")] \
        if (env.data / "merges").is_dir() else []
    assert not any(e.get("what") == "changed the world note" for e in journal)


def test_an_older_art_set_still_loads_and_draws(env):
    env.key()
    folder = env.data / "art" / env.hash / "ink"
    folder.mkdir(parents=True)
    old = {"lorekeeperArt": "0.1", "textHash": env.hash, "style": "ink", "pictures": [], "screened": {}, "looks": {},
           "skipped": [], "cost": 0.0, "world": {"text": "A valley of mills, early iron age.", "from": env.doc["anchors"][0]["id"]},
           "appearance": {"mira": [{"v": 1, "from": env.doc["anchors"][0]["id"], "text": "A young woman: dark hair."}]},
           "groups": {"mira": {"key": "x", "picks": []}}}
    (folder / "art.json").write_text(json.dumps(old))
    got = env.c.get(f"/api/library/{KEY}/art?style=ink").json()
    assert got["world"]["text"].startswith("A valley") and got["pictures"] == []
    ask = {"kind": "event", "target": "e5", "style": "ink"}
    pr = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask)
    assert pr.status_code == 200, pr.text
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": pr.json()["prompt"]})
    assert r.status_code == 200, r.text
    m = json.loads((folder / "art.json").read_text())
    assert [p["id"] for p in m["pictures"]] == [r.json()["id"]] and m["appearance"]["mira"][0]["v"] == 1


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


# ---- which chapters get pictures, and portraits only


def test_the_art_config_takes_level_none_and_a_chapter_choice():
    from lorekeeper.web.jobs import art_config
    base = {"style": "ink", "cap": 10}
    got = art_config(base)
    assert got["level"] == "main" and got["chapters"] is None              # an older config: every chapter at main
    assert art_config({**base, "level": "none"})["level"] == "none"          # portraits only (main cast by default)
    assert art_config({**base, "chapters": [5, 2, 2]})["chapters"] == [2, 5]
    assert art_config({**base, "chapters": []})["chapters"] == []
    for bad in ("1,2", [True], ["1"], [0], [-1], [1.5], {"n": 1}):
        with pytest.raises(ValueError, match="chapters"):
            art_config({**base, "chapters": bad})
    for nothing in ({"level": "none", "portraits": False}, {"chapters": [], "portraits": False}):
        with pytest.raises(ValueError, match="Choose chapters or portraits"):
            art_config({**base, **nothing})
    stored = art_config({**base, "level": "all", "chapters": [3]})
    assert art_config(stored) == stored                                        # a resume reads it back


def chapter_ns(env) -> dict[str, int]:
    got = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink"}).json()
    return {c["title"]: c["n"] for c in got["chapters"]}


def test_level_none_draws_only_portraits_and_never_screens(env):
    env.key()
    done = run_set(env, level="none", portraits="main")
    assert done["status"] == "done", done["error"]
    got = env.c.get(f"/api/library/{KEY}/art?style=ink").json()
    assert got["pictures"] and {p["kind"] for p in got["pictures"]} == {"portrait"}
    assert not got["screened"] and not any("Which of them are a visible moment" in p for p in env.text.prompts)
    assert done["config"]["art"]["level"] == "none"


def test_a_chapter_choice_limits_scenes_but_never_portraits(env):
    env.key()
    ns = chapter_ns(env)
    done = run_set(env, chapters=[ns["Chapter Two"]])
    assert done["status"] == "done", done["error"]
    pics = env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert [p["target"] for p in pics if p["kind"] == "plate"] == ["Chapter Two"]
    est = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink", "portraits": "all"}).json()
    assert len([p for p in pics if p["kind"] == "portrait"]) == est["planned"]["portraits"] > 0   # not cut at a chapter
    assert est["portraits"] == 0                                               # all drawn: nothing left to price


def test_a_run_refuses_an_unknown_chapter_or_nothing_to_draw(env):
    env.key()
    for cfg in ({"chapters": [999]}, {"level": "none", "portraits": False}, {"chapters": [], "portraits": False},
                {"chapters": "all"}):
        r = env.c.post(f"/api/library/{KEY}/art/run", json={"art": {"style": "ink", "cap": 10, **cfg}})
        assert r.status_code == 422 and r.json()["code"] == "bad-config", cfg
    assert env.jobs.jobs == {}
    r = env.c.post(f"/api/library/{KEY}/art/run", json={"art": {"style": "ink", "cap": 10, "chapters": [], "portraits": "main"}})
    assert r.status_code == 200                                                # portraits only, by an empty choice
    wait_for(env.c, r.json()["id"])


def test_the_estimate_follows_the_level_and_the_chapter_choice(env):
    env.key()
    q = {"key": KEY, "style": "ink", "portraits": "false"}
    est = lambda **kw: env.c.get("/api/art/estimate", params={**q, **kw})     # noqa: E731
    full = est().json()
    assert full["level"] == "main" and full["plates"] == 2 and env.text.prompts == []
    rows = full["chapters"]
    assert [r["title"] for r in rows] == ["Chapter One", "Chapter Two"]
    assert all({"n", "title", "drawable", "screened", "events", "pictures", "drawn", "chosen"} <= r.keys() for r in rows)
    assert all(r["pictures"] == 1 and r["drawn"] == 0 and r["chosen"] for r in rows)
    one = est(chapters=str(rows[0]["n"])).json()
    assert one["plates"] == one["pictures"] == 1 and one["price"] < full["price"]
    assert [r["chosen"] for r in one["chapters"]] == [True, False] and one["chapters"][1]["pictures"] == 1
    none = est(chapters="").json()
    assert none["plates"] == 0 and none["price"] == 0 and [r["pictures"] for r in none["chapters"]] == [1, 1]
    lvl = est(level="none", portraits="main").json()
    assert lvl["plates"] == 0 and lvl["portraits"] == lvl["pictures"] > 0
    assert all(r["pictures"] == 0 and not r["chosen"] for r in lvl["chapters"])
    every = est(level="all", chapters=f"{rows[0]['n']},{rows[1]['n']}").json()
    assert every["plates"] == sum(r["pictures"] for r in every["chapters"]) >= 2
    for bad in ("x", "1;2", "999", "-1", "²", "１", "٢", "1,²"):
        r = est(chapters=bad)
        assert r.status_code == 422 and r.json()["code"] == "bad-config", bad
    assert est(level="some").json()["code"] == "bad-config"
    run_set(env, portraits=False)
    after = est().json()
    assert all(r["drawn"] == r["pictures"] == 1 for r in after["chapters"])   # "already drawn"
    assert after["plates"] == after["pictures"] == 0 and after["price"] == 0  # a rerun would draw nothing new
    assert after["planned"] == {"pictures": 2, "plates": 2, "portraits": 0}


def test_the_estimate_prices_only_what_a_run_would_still_draw(env):
    env.key()
    q = {"key": KEY, "style": "ink", "portraits": "all"}
    before = env.c.get("/api/art/estimate", params=q).json()
    assert before["planned"] == {k: before[k] for k in ("pictures", "plates", "portraits")}   # nothing drawn yet
    ns = chapter_ns(env)
    run_set(env, chapters=[ns["Chapter One"]], portraits="all")
    after = env.c.get("/api/art/estimate", params=q).json()
    assert after["planned"] == before["planned"] and after["plates"] == 1 and after["portraits"] == 0
    each = llm.image_price("gemini", GEMINI, "standard", "landscape")
    assert after["pictures"] == 1 and after["price"] == pytest.approx(each)


def test_the_estimate_leaves_out_front_matter_with_nothing_to_draw(env, monkeypatch):
    real = art.Illustrator.counts

    def with_contents(self, bi=-1, level="main"):
        got = real(self, bi, level)
        got["chapters"].insert(0, {"n": 99, "title": "Contents", "drawable": None, "events": 0, "pictures": 0, "drawn": 0,
                                   "chosen": False})
        return got
    monkeypatch.setattr(art.Illustrator, "counts", with_contents)
    rows = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink"}).json()["chapters"]
    assert [r["title"] for r in rows] == ["Chapter One", "Chapter Two"]


def test_the_estimate_gives_a_range_while_looks_are_unchecked(env):
    env.key()
    q = {"key": KEY, "style": "ink", "portraits": "all"}
    got = env.c.get("/api/art/estimate", params=q).json()
    assert got["portraits"] > 0 and env.text.prompts == []                    # no look check: every entity unchecked
    extra = got["portraitsMax"] - got["portraits"]
    assert 0 < extra and got["portraitsMax"] <= got["portraits"] * art.MAX_VERSIONS
    assert got["picturesMax"] == got["pictures"] + extra and got["plates"] + got["portraitsMax"] == got["picturesMax"]
    each = llm.image_price("gemini", GEMINI, "standard", "portrait")
    assert got["priceMax"] == pytest.approx(got["price"] + extra * each)
    run_set(env, portraits="all")                                              # the run checks every entity's looks
    after = env.c.get("/api/art/estimate", params=q).json()
    assert (after["picturesMax"], after["portraitsMax"], after["priceMax"]) == \
        (after["pictures"], after["portraits"], after["price"])


def test_the_range_is_measured_from_the_portraits_still_to_draw(env, monkeypatch):
    # `portraits_max` is what `new_portraits` could grow to: with 3 of 5 planned portraits drawn and up to 6 still to draw,
    # the top is 6, not 2 + (6 - 5)
    env.key()
    real = art.Illustrator.counts

    def drawn(self, bi=-1, level="main"):
        got = real(self, bi, level)
        got.update(portraits=5, new_portraits=2, portraits_max=6)
        return got
    monkeypatch.setattr(art.Illustrator, "counts", drawn)
    got = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink", "portraits": "all", "level": "none"}).json()
    assert (got["portraits"], got["portraitsMax"], got["picturesMax"]) == (2, 6, got["plates"] + 6)


def test_the_estimate_has_no_range_without_a_portraits_max_or_a_price(env, monkeypatch):
    env.key()
    q = {"key": KEY, "style": "ink", "portraits": "all"}
    real = art.Illustrator.counts

    def older(self, bi=-1, level="main"):                                      # a pipeline from before the range
        got = real(self, bi, level)
        got.pop("portraits_max")
        return got
    monkeypatch.setattr(art.Illustrator, "counts", older)
    plain = env.c.get("/api/art/estimate", params=q).json()
    assert plain["portraits"] > 0
    assert (plain["picturesMax"], plain["portraitsMax"], plain["priceMax"]) == \
        (plain["pictures"], plain["portraits"], plain["price"])

    def below(self, bi=-1, level="main"):                                      # never a top under the plain number
        got = real(self, bi, level)
        got["portraits_max"] = 0
        return got
    monkeypatch.setattr(art.Illustrator, "counts", below)
    assert env.c.get("/api/art/estimate", params=q).json()["portraitsMax"] == plain["portraits"]
    monkeypatch.setattr(art.Illustrator, "counts", real)
    monkeypatch.setattr(llm, "image_price", lambda *a, **kw: None)             # a price unknown: both unknown
    unknown = env.c.get("/api/art/estimate", params=q).json()
    assert unknown["price"] is None and unknown["priceMax"] is None and unknown["picturesMax"] > unknown["pictures"]


def test_a_builds_pictures_stage_honours_level_none(tmp_path, monkeypatch):
    from .test_web import SlowFake, upload
    monkeypatch.setattr(llm, "Ollama", lambda **kw: SlowFake())
    t, im = Text(), GeminiFake()
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: t)
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: t)   # the portrait writer
    monkeypatch.setattr(jobs_mod.JobManager, "_portraits", lambda self, *a: {"skipped": "test"})
    monkeypatch.setattr(llm, "make_image", lambda *a, **kw: im)
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    seen = []
    real = art.Illustrator.__init__

    def spy(self, *a, **kw):
        seen.append(kw.get("scene_chapters"))
        real(self, *a, **kw)
    monkeypatch.setattr(art.Illustrator, "__init__", spy)
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        meta = upload(c, tmp_path)
        bad = c.post("/api/jobs", json={"book_id": meta["id"], "config": {"chunk_words": 5000, "art": {
            "style": "ink", "cap": 10, "level": "none", "portraits": False}}})
        assert bad.status_code == 422
        job = c.post("/api/jobs", json={"book_id": meta["id"], "config": {"chunk_words": 5000, "art": {
            "style": "ink", "cap": 10, "level": "none", "chapters": [1]}}}).json()
        done = wait_for(c, job["id"])
        key = "generated--" + Path(done["output"]).with_suffix("").name
        got = c.get(f"/api/library/{key}/art?style=ink").json()
    assert done["status"] == "done", done["error"]
    assert "drawn" in done["report"]["art"] and seen[-1] == [1]
    assert all(p["kind"] == "portrait" for p in got["pictures"]) and not got["screened"]


# ---- a scene's labelled figures (figure A, figure B...) and their references


def scene_asked(env, style="ink"):
    env.key()
    run_set(env, portraits="all")
    return {"kind": "event", "target": "e6", "style": style}


def test_the_prompt_route_lists_each_labelled_figure_with_its_reference(env):
    ask = scene_asked(env)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    assert got["figures"], got
    assert [f["label"] for f in got["figures"]] == sorted(f["label"] for f in got["figures"])
    portraits = {p["target"]: p["id"] for p in pictures(env) if p["kind"] == "portrait"}
    for f in got["figures"]:
        assert f"igure {f['label']}" in got["prompt"] and f["entity"] in portraits
        assert f["ref"] == f"/api/art/{env.hash}/ink/{portraits[f['entity']]}" and env.c.get(f["ref"]).status_code == 200
    # the references are those, in the same order
    assert [x["url"] for x in got["references"]] == [f["ref"] for f in got["figures"]]
    # a portrait has no labels at all
    p = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "portrait", "target": next(iter(portraits)), "style": "ink"}).json()
    assert p["figures"] is None


def test_a_draw_into_another_style_keeps_the_labels_and_takes_their_pictures_from_that_set(env):
    ask = scene_asked(env)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    env.image.calls.clear()
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "style": "watercolour", "prompt": got["prompt"]}).json()
    assert [(f["label"], f["entity"]) for f in rec["figures"]] == [(f["label"], f["entity"]) for f in got["figures"]]
    # watercolour has no portraits: the ink ones are its likenesses, named by their set, each URL served from that set
    assert all(isinstance(f["ref"], str) and "/ink/" in f["ref"] for f in rec["figures"])
    call = env.image.calls[0]
    assert len(call["references"]) == len(got["figures"])
    for n, f in enumerate(got["figures"], 1):
        assert f"Reference picture {n} is figure {f['label']}." in call["note"]
    assert "figure" not in call["note"].split("Reference picture 1")[0]


def test_a_label_the_person_takes_out_sends_no_reference(env):
    ask = scene_asked(env)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    if len(got["figures"]) < 2:
        pytest.skip("the scripted scene shows one figure")
    gone = got["figures"][-1]["label"]
    edited = re.sub(rf"\b[Ff]igure {gone}\b", "the stranger", got["prompt"])
    env.image.calls.clear()
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": edited}).json()
    call = env.image.calls[0]
    assert gone not in [f["label"] for f in rec["figures"] if f["ref"]]
    assert f"figure {gone}" not in call["note"] and len(call["references"]) == len(got["figures"]) - 1


def test_a_same_prompt_redraw_rebuilds_the_figure_note_from_the_record(env):
    ask = scene_asked(env)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    first = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]}).json()
    assert first["figures"]
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{first['id']}/redraw", json={})
    assert r.status_code == 200, r.text
    new, call = r.json(), env.image.calls[-1]
    assert [(f["label"], f["entity"], f["ref"]) for f in new["figures"]] == [(f["label"], f["entity"], f["ref"]) for f in first["figures"]]
    assert new["references"] == first["references"]
    for n, f in enumerate(first["figures"], 1):
        assert f"Reference picture {n} is figure {f['label']}." in call["note"]
    assert call["note"] not in call["prompt"]
    # a note put first leaves the labels in place
    r = env.c.post(f"/api/library/{KEY}/art/{new['id']}/redraw", json={"note": "Closer"})
    assert r.status_code == 200 and [f["label"] for f in r.json()["figures"]] == [f["label"] for f in first["figures"]]


def test_the_art_route_serves_each_pictures_figures_with_urls(env):
    ask = scene_asked(env)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    first = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]}).json()
    m = env.c.get(f"/api/library/{KEY}/art?style=ink").json()
    rec = next(p for p in m["pictures"] if p["id"] == first["id"])
    assert rec["figures"] == first["figures"] and all(f["ref"] is None or env.c.get(f["ref"]).status_code == 200 for f in rec["figures"])
    # nothing but checked ids ever becomes a URL
    path = env.data / "art" / env.hash / "ink" / "art.json"
    man = json.loads(path.read_text())
    next(p for p in man["pictures"] if p["id"] == first["id"])["figures"] = [
        {"label": "A", "entity": "x", "ref": "../../etc"}, {"label": "B", "entity": "y", "ref": {"set": "../x", "id": "p00000000"}}, "junk",
        {"label": "C", "entity": "z", "ref": "p00000000"}]      # well formed, but no edition holds it: null, not a 404 URL
    path.write_text(json.dumps(man))
    rec = next(p for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"] if p["id"] == first["id"])
    assert [f["ref"] for f in rec["figures"]] == [None, None, None]


def test_an_older_record_without_figures_is_served_and_redrawn_as_before(env):
    ask = scene_asked(env)
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    first = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]}).json()
    path = env.data / "art" / env.hash / "ink" / "art.json"
    man = json.loads(path.read_text())
    next(p for p in man["pictures"] if p["id"] == first["id"]).pop("figures")
    path.write_text(json.dumps(man))
    rec = next(p for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"] if p["id"] == first["id"])
    assert "figures" not in rec
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{first['id']}/redraw", json={})
    assert r.status_code == 200 and "figures" not in r.json()
    assert len(env.image.calls[-1]["references"]) == len(first["references"])
    assert "Reference picture 1 is figure" not in env.image.calls[-1]["note"]


def test_a_reference_from_an_earlier_book_is_served_from_that_books_edition(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    pid = earlier_portrait(c)
    ask = {"kind": "event", "target": "e6", "style": "ink"}
    got = c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    fig = [f for f in got["figures"] if f["entity"] == "mira"]
    assert fig and fig[0]["ref"] == f"/api/art/{c.hash_one}/ink/{pid}" and c.get(fig[0]["ref"]).status_code == 200
    rec = c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]}).json()
    ref = next(f["ref"] for f in rec["figures"] if f["entity"] == "mira")
    assert ref == fig[0]["ref"]
    served = c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]
    assert next(f["ref"] for f in next(p for p in served if p["id"] == rec["id"])["figures"] if f["entity"] == "mira") == ref
    redo = c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={})
    assert redo.status_code == 200 and next(f["ref"] for f in redo.json()["figures"] if f["entity"] == "mira") == ref


def test_the_sheets_writer_gets_the_cheap_model_for_the_look_sentence_pick(env, monkeypatch):
    env.key()
    cheap, strong = Text(), Text()
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: cheap)
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: strong)
    seen = {}
    real = art.Illustrator.__init__

    def spy(self, *a, **kw):
        real(self, *a, **kw)
        seen.update(text=self.text, cheap=self.cheap)
    monkeypatch.setattr(art.Illustrator, "__init__", spy)
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e6", "style": "ink"})
    assert r.status_code == 200, r.text
    assert seen["text"] is strong and seen["cheap"] is cheap and strong.prompts


# ---- a model for each responsibility (picture-model-choices)


def test_the_art_config_takes_a_model_for_each_responsibility():
    from lorekeeper.web.jobs import art_config
    base = {"style": "ink", "cap": 1}
    got = art_config(base)                      # an older config: every choice absent, the defaults apply
    assert all(got[k] is None for k in ("writer", "portraitWriter", "picker", "portraitProvider", "portraitModel"))
    full = {**base, "writer": {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            "portraitWriter": {"provider": "anthropic", "model": "claude-sonnet-5-5"},
            "picker": {"provider": "gemini", "model": "gemini-2.5-flash"},
            "portraitProvider": "gemini", "portraitModel": "gemini-3-pro-image"}
    got = art_config(full)
    assert got["portraitWriter"] == {"provider": "claude", "model": "claude-sonnet-5-5"}
    assert got["portraitModel"] == "gemini-3-pro-image" and art_config(got) == got       # a resume reads it back
    for bad in ({"writer": {"provider": "ollama", "model": "gemma4:12b-it-qat"}},      # Ollama never writes picture prompts
                {"picker": {"provider": "nobody", "model": "x"}}, {"writer": "claude"},
                {"portraitWriter": {"provider": "claude"}},
                {"portraitProvider": "claude"},                                        # Claude cannot draw
                {"portraitProvider": "gemini", "portraitModel": "gpt-image-2"},
                {"provider": "openai", "portraitModel": "gemini-3-pro-image"}):
        with pytest.raises(ValueError):
            art_config({**base, **bad})


class Named(Text):
    def __init__(self, label):
        super().__init__()
        self.label = label
        if ":" in label:                 # named like a real backend, so its records and log lines name it
            self.provider, self.name = label.split(":", 1)


def models_env(env, monkeypatch, live=()):
    """Gemini set up for text and pictures (its live models `live`), every cloud model a scripted one named by its id; the
    keywords and text model the run hands the pipeline are kept in `seen`."""
    env.key()
    made = {}
    monkeypatch.setattr(jobs_mod.JobManager, "cloud_model",
                        lambda self, p, m=None: made.setdefault((p, m), Named(f"{p}:{m}")))
    monkeypatch.setattr(llm, "live_models", lambda *a, **kw: list(live))
    images_made = []
    monkeypatch.setattr(llm, "make_image", lambda provider, model=None, api_key=None, base_url=None:
                        images_made.append((provider, model)) or env.image)
    seen = {}
    real_init = art.Illustrator.__init__

    def spy_init(self, books, text_, image, *a, **kw):
        seen.update(kw, text=text_, image=image)
        real_init(self, books, text_, image, *a, **kw)
    monkeypatch.setattr(art.Illustrator, "__init__", spy_init)
    return made, images_made, seen


def test_a_run_hands_each_responsibility_its_chosen_model(env, monkeypatch):
    made, images_made, seen = models_env(env, monkeypatch, live=["gemini-live-x"])
    job = run_set(env, portraits="all", writer={"provider": "gemini", "model": "gemini-2.5-flash-lite"},
                  portraitWriter={"provider": "gemini", "model": "gemini-3.1-pro-preview"},
                  picker={"provider": "gemini", "model": "gemini-live-x"},          # a live model counts as listed
                  provider="gemini", model=GEMINI, portraitModel="gemini-3-pro-image")
    assert job["status"] == "done", job
    assert seen["text"].label == "gemini:gemini-2.5-flash-lite"
    assert seen["portrait_text"].label == "gemini:gemini-3.1-pro-preview"
    assert seen["cheap_text"].label == "gemini:gemini-live-x"
    assert ("gemini", GEMINI) in images_made and ("gemini", "gemini-3-pro-image") in images_made


def test_a_run_without_choices_writes_portraits_with_the_strong_tier(env, monkeypatch):
    _, images_made, seen = models_env(env, monkeypatch)
    cheap, strong = Named("cheap"), Named("strong")
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: cheap)
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: strong)
    assert run_set(env)["status"] == "done"
    assert seen["text"] is cheap and seen["cheap_text"] is cheap and seen["portrait_text"] is strong
    assert seen["portrait_image"] is seen["image"] and len(images_made) == 1   # portraits draw with the scenes' model


def test_a_choice_of_a_provider_not_set_up_or_a_model_it_does_not_list_is_refused(env, monkeypatch):
    models_env(env, monkeypatch, live=["gemini-live-x"])
    base = {"style": "ink", "cap": 1}
    for bad in ({"writer": {"provider": "claude", "model": "claude-haiku-5-5"}},       # no Claude key
                {"picker": {"provider": "gemini", "model": "gemini-9-ultra"}},          # neither a preset nor live
                {"portraitWriter": {"provider": "ollama", "model": "gemma4:12b-it-qat"}},
                {"portraitProvider": "openai"},                                         # no OpenAI key
                {"portraitModel": "gpt-image-2"}):                                      # not a model of Gemini
        r = env.c.post(f"/api/library/{KEY}/art/run", json={"art": {**base, **bad}})
        assert r.status_code == 422 and r.json()["code"] == "bad-config", (bad, r.text)
    assert env.c.get("/api/jobs").json() == [] or all(j["kind"] != "art" for j in env.c.get("/api/jobs").json())


def test_a_live_model_that_cannot_write_text_is_never_a_writer(env, monkeypatch):
    models_env(env, monkeypatch, live=[{"id": "gemini-live-x", "text": True}, {"id": "lyria-3.5", "text": False}])
    g = next(p for p in env.c.get("/api/providers").json()["providers"] if p["id"] == "gemini")
    assert [m["id"] for m in g["liveModels"]] == ["gemini-live-x"]
    assert [m["id"] for m in g["otherModels"]] == ["lyria-3.5"] and g["otherModels"][0]["text"] is False
    bad = {"provider": "gemini", "model": "lyria-3.5"}
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink", "writer": bad})
    assert r.status_code == 422 and r.json()["code"] == "bad-writer", r.text
    r = env.c.post(f"/api/library/{KEY}/art/run", json={"art": {"style": "ink", "cap": 1, "picker": bad}})
    assert r.status_code == 422 and r.json()["code"] == "bad-config", r.text


def test_a_build_config_with_a_model_choice_is_checked_too(env, monkeypatch):
    models_env(env, monkeypatch)
    r = env.c.post("/api/jobs", json={"book_id": "nope", "config": {"provider": "gemini", "art": {
        "style": "ink", "cap": 1, "writer": {"provider": "claude", "model": "claude-haiku-5-5"}}}})
    assert r.status_code == 422 and "Claude" in r.text


def test_the_portrait_writer_writes_the_portraits_and_the_scene_writer_never_does(env, monkeypatch):
    made, _, _ = models_env(env, monkeypatch)
    run_set(env, level="none", portraits="all", writer={"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            portraitWriter={"provider": "gemini", "model": "gemini-3.1-pro-preview"},
            picker={"provider": "gemini", "model": "gemini-2.5-flash"})
    scene, port = made.get(("gemini", "gemini-2.5-flash-lite")), made[("gemini", "gemini-3.1-pro-preview")]
    wrote = [p for p, s in zip(port.prompts, port.systems) if s in WRITERS]
    assert wrote and all(is_portrait(p) or "place" in p or "thing" in p for p in wrote)
    assert scene is None or not any(s in WRITERS and is_portrait(p) for p, s in zip(scene.prompts, scene.systems))


def test_the_estimate_prices_portraits_with_their_own_image_model(env):
    q = f"/api/art/estimate?key={KEY}&style=ink&portraits=all&provider=gemini&model={GEMINI}"
    same = env.c.get(q).json()
    assert same["portraitProvider"] == "gemini" and same["portraitModel"] == GEMINI
    pro = env.c.get(q + "&portraitModel=gemini-3-pro-image").json()
    assert pro["portraitModel"] == "gemini-3-pro-image" and pro["portraits"] == same["portraits"] > 0
    assert pro["price"] == pytest.approx(same["plates"] * 0.0336 + same["portraits"] * 0.134, abs=1e-4)
    other = env.c.get(q + "&portraitProvider=openai").json()      # another provider: its default model
    assert other["portraitModel"] == llm.IMAGE_PROVIDERS["openai"]["default"]
    assert env.c.get(q + "&portraitModel=gpt-image-2").status_code == 422
    assert env.c.get(q + "&portraitProvider=claude").status_code == 422


def test_the_sheet_writes_with_the_chosen_writer_and_draws_portraits_with_the_portrait_model(env, monkeypatch):
    made, images_made, _ = models_env(env, monkeypatch)
    strong = Named("strong")
    monkeypatch.setattr(jobs_mod.JobManager, "strong_model", lambda self, provider=None: strong)
    writer = {"provider": "gemini", "model": "gemini-2.5-flash"}
    ask = {"kind": "portrait", "target": "mira", "style": "ink", "provider": "gemini", "model": GEMINI,
           "portraitModel": "gemini-3-pro-image", "writer": writer}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask)
    assert got.status_code == 200, got.text
    got = got.json()
    assert got["writer"] == writer and made[("gemini", "gemini-2.5-flash")].prompts and not strong.prompts
    assert got["model"] == "gemini-3-pro-image" and got["price"] == 0.134
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": got["prompt"]})
    assert rec.status_code == 200, rec.text
    assert images_made[-1] == ("gemini", "gemini-3-pro-image")
    scene = env.c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "kind": "event", "target": "e5"}).json()
    assert scene["model"] == GEMINI                                # a scene keeps the scene model
    assert images_made[-1] == ("gemini", GEMINI)
    plain = env.c.post(f"/api/library/{KEY}/art/prompt", json={k: v for k, v in ask.items() if k != "writer"}).json()
    assert strong.prompts and plain["writer"]["model"] != "gemini-2.5-flash"   # no choice: the strong tier
    for bad in ({"provider": "claude", "model": "claude-haiku-5-5"}, {"provider": "gemini", "model": "nope"},
                {"provider": "ollama", "model": "gemma4:12b-it-qat"}, "gemini"):
        r = env.c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "writer": bad})
        assert r.status_code == 422 and r.json()["code"] == "bad-writer", (bad, r.text)


def test_a_same_prompt_redraw_keeps_who_wrote_the_prompt(env):
    env.key()
    ask = {"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate."}
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json=ask).json()
    slug, man = "ink", env.jobs.art_store.manifest(env.hash, "ink")
    for r in man["pictures"]:
        if r["id"] == rec["id"]:
            r["writer"] = {"provider": "claude", "model": "claude-sonnet-5-5"}
    env.jobs.art_store.save(env.hash, slug, man)
    new = env.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={}).json()
    assert new["writer"] == {"provider": "claude", "model": "claude-sonnet-5-5"}


def test_a_portrait_model_without_the_runs_quality_draws_at_its_nearest_level(env, monkeypatch):
    lite = "gemini-3.1-flash-lite-image"                    # draft and standard only
    assert llm.nearest_quality("gemini", lite, "best") == "standard"
    assert llm.nearest_quality("gemini", lite, "draft") == "draft"
    assert llm.nearest_quality("gemini", GEMINI, "best") == "best"
    monkeypatch.setitem(llm.IMAGE_PROVIDERS["gemini"]["models"][0], "levels", ("standard", "best"))
    assert llm.nearest_quality("gemini", GEMINI, "draft") == "standard"     # a tie goes up
    monkeypatch.setitem(llm.IMAGE_PROVIDERS["gemini"]["models"][0], "levels", ("draft", "standard", "best"))
    assert llm.nearest_quality("nobody", "x", "best") == "best"            # not in the table: as asked
    q = f"/api/art/estimate?key={KEY}&style=ink&level=none&portraits=all&provider=gemini&model={GEMINI}&quality=best"
    got = env.c.get(q + f"&portraitModel={lite}").json()
    assert got["quality"] == "best" and got["portraitQuality"] == "standard"
    assert got["price"] == pytest.approx(got["portraits"] * 0.0336, abs=1e-4)
    models_env(env, monkeypatch)
    made = {}

    def make(provider, model=None, api_key=None, base_url=None):
        im = made.setdefault(model, GeminiFake())
        im.name = model or GEMINI
        return im
    monkeypatch.setattr(llm, "make_image", make)
    job = run_set(env, level="none", portraits="all", quality="best", provider="gemini", portraitModel=lite)
    assert job["status"] == "done", job
    recs = pictures(env)
    assert recs and all(r["quality"] == "standard" for r in recs)
    assert made[lite].calls and {c["quality"] for c in made[lite].calls} == {"standard"}


def test_the_run_report_splits_the_text_cost_by_responsibility(env, monkeypatch):
    models_env(env, monkeypatch)
    job = run_set(env)
    assert isinstance(job["report"]["art"]["text_costs"], dict)


def test_a_chosen_model_that_lost_its_key_is_replaced_with_a_log_line(env, monkeypatch):
    made, images_made, seen = models_env(env, monkeypatch)
    # accepted when it was asked for, then the Claude and OpenAI keys were removed before the pictures stage
    monkeypatch.setattr(jobs_mod.JobManager, "check_art_models", lambda self, cfg: None)
    job = run_set(env, writer={"provider": "gemini", "model": "gemini-2.5-flash-lite"},
                  portraitWriter={"provider": "claude", "model": "claude-sonnet-5-5"},
                  picker={"provider": "claude", "model": "claude-haiku-5-5"},
                  portraitProvider="openai", portraitModel="gpt-image-2")
    assert job["status"] == "done", job
    assert seen["portrait_text"] is seen["text"] is seen["cheap_text"] and seen["portrait_image"] is seen["image"]
    lines = [x for x in (ev.get("line", "") for ev in env.jobs.events[job["id"]]) if "no longer set up" in x]
    assert len(lines) == 3
    assert any("Claude claude-sonnet-5-5 for the portrait prompts" in x and "Gemini gemini-2.5-flash-lite" in x for x in lines)
    assert any("Claude claude-haiku-5-5 for the choosing calls" in x for x in lines)
    assert any("OpenAI gpt-image-2 for the portraits" in x for x in lines)


def test_an_early_scene_takes_a_later_portrait_only_once_the_reader_has_reached_it(env):
    env.key()
    mira = {"kind": "portrait", "target": "mira", "style": "ink"}       # her portrait shows from her profile, after e1
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=mira).json()
    assert env.c.post(f"/api/library/{KEY}/art/draw", json={**mira, "prompt": got["prompt"]}).status_code == 200
    e1 = next(e for e in env.doc["events"] if e["id"] == "e1")
    ask = {"kind": "event", "target": "e1", "style": "ink"}
    far = env.c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "p": 1.0}).json()
    assert [f["entity"] for f in far["figures"]] == ["mira"] and far["figures"][0]["ref"]
    near = env.c.post(f"/api/library/{KEY}/art/prompt", json={**ask, "p": e1["from"]}).json()
    assert near["figures"] == [] and near["references"] == []
    # a draw at the early position never reuses the prompt written further on (the frontier is part of the cache key)
    drawn = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "p": e1["from"], "prompt": far["prompt"]}).json()
    assert drawn["references"] == []
    drawn = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "p": 1.0, "prompt": far["prompt"]}).json()
    assert drawn["references"] == [x["id"] for x in far["references"]] and len(drawn["references"]) == 1
    assert drawn["figures"][0]["entity"] == "mira" and drawn["figures"][0]["ref"]


def test_a_same_prompt_redraw_sends_a_later_reference_only_when_the_reader_has_reached_it(env):
    env.key()
    mira = {"kind": "portrait", "target": "mira", "style": "ink"}
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json=mira).json()
    port = env.c.post(f"/api/library/{KEY}/art/draw", json={**mira, "prompt": got["prompt"]}).json()
    e1 = next(e for e in env.doc["events"] if e["id"] == "e1")
    ask = {"kind": "event", "target": "e1", "style": "ink", "p": 1.0}
    far = env.c.post(f"/api/library/{KEY}/art/prompt", json=ask).json()
    rec = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": far["prompt"]}).json()
    assert rec["references"] == [port["id"]]
    early = env.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={"note": "", "p": e1["from"]})
    assert early.status_code == 200, early.text
    assert early.json()["references"] == [] and early.json()["figures"][0]["ref"] is None
    assert env.image.calls[-1]["references"] == []
    late = env.c.post(f"/api/library/{KEY}/art/{early.json()['id']}/redraw", json={"note": "", "p": 1.0}).json()
    assert late["references"] == []           # the redraw before had none: a same prompt sends what the record holds
    again = env.c.post(f"/api/library/{KEY}/art/draw", json={**ask, "prompt": far["prompt"]}).json()
    plain = env.c.post(f"/api/library/{KEY}/art/{again['id']}/redraw", json={"note": ""}).json()   # no p: as before
    assert plain["references"] == [port["id"]] and plain["figures"][0]["ref"]
    assert env.c.post(f"/api/library/{KEY}/art/{plain['id']}/redraw", json={"p": "nowhere"}).json()["code"] == "bad-position"


# ---- portraits are cropped at mid-chest (portrait-chest-up)


def test_every_portrait_prompt_is_framed_at_mid_chest(env):
    env.key()
    run_set(env, portraits="all")
    sent = [c["prompt"] for c in env.image.calls if c["prompt"].startswith("Close portrait")]
    assert sent and all(p.startswith(art.PORTRAIT_FRAME + " ") and f" {art.PORTRAIT_CROP} " in p for p in sent)
    portraits = [p for p in pictures(env) if p["kind"] == "portrait"]
    assert portraits and all(p["prompt"].startswith(art.PORTRAIT_FRAME) for p in portraits)
    # the sheet's prompt: the body carries the frame and the crop line, the style sentence after them
    got = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "portrait", "target": portraits[0]["target"],
                                                             "style": "ink"}).json()
    assert got["body"].startswith(art.PORTRAIT_FRAME + " ") and got["body"].endswith(" " + art.PORTRAIT_CROP)
    assert got["prompt"] == f"{got['body']} {got['style']} {got['noText']}"


def test_a_same_prompt_redraw_of_an_older_portrait_takes_the_mid_chest_frame(env):
    import json
    env.key()
    run_set(env, portraits="all")
    path = env.data / "art" / env.hash / "ink" / "art.json"
    m = json.loads(path.read_text())
    rec = next(p for p in m["pictures"] if p["kind"] == "portrait")
    rest = rec["prompt"][len(art.PORTRAIT_FRAME):].replace(" " + art.PORTRAIT_CROP, "")
    rec["prompt"] = art.PORTRAIT_FRAME_OLD + rest           # drawn before the crop was fixed
    path.write_text(json.dumps(m))
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={"note": "Make it night"})
    assert r.status_code == 200, r.text
    sent = env.image.calls[-1]["prompt"]
    assert sent == "Make it night. " + art.PORTRAIT_FRAME + rest and art.PORTRAIT_FRAME_OLD not in sent
    # a portrait whose prompt is the person's own words is drawn as given
    m = json.loads(path.read_text())
    rec = next(p for p in m["pictures"] if p["kind"] == "portrait")
    rec["prompt"] = "A woman by a window, drawn in ink."
    path.write_text(json.dumps(m))
    r = env.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={})
    assert r.status_code == 200, r.text
    assert env.image.calls[-1]["prompt"] == "A woman by a window, drawn in ink."
