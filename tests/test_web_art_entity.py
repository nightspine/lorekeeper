"""One entry's portraits through the book, up to the reader's position (the picture sheet's "Through the book so far"):
the art config's `entities`, `until` and `finished`, the estimate, the run route's refusals and the run itself.

The web side hands the pipeline `portraits=frozenset(ids)`, `until=(book index, anchor ordinal)` and `finished`; the tests
that need the pipeline to honour them skip until `art.Illustrator` takes `until`."""

import inspect
import json

import pytest

pytest.importorskip("fastapi")

from lorekeeper import art  # noqa: E402
from lorekeeper.web import jobs as jobs_mod  # noqa: E402

from .test_web_art import KEY, env, series_env, short_world, wait_for  # noqa: E402,F401

PIPELINE = "until" in inspect.signature(art.Illustrator.__init__).parameters
needs_pipeline = pytest.mark.skipif(not PIPELINE, reason="art.Illustrator does not take `until` yet (pipeline-engineer)")

BASE = {"style": "ink", "level": "none", "cap": 10, "portraits": False}


@pytest.fixture
def seen(monkeypatch):
    """Every Illustrator the routes and runs make, with what the web side handed it. Before the pipeline takes `until`,
    the new arguments are recorded and left out (an entry set as "all"), so the web's wiring is tested either way."""
    got = []
    real = art.Illustrator.__init__

    def spy(self, books, *a, **kw):
        got.append({"portraits": kw.get("portraits"), "until": kw.get("until"), "finished": kw.get("finished", "absent"),
                    "books": [(b.doc["id"], b.draw, b.epub is not None) for b in books]})
        if not PIPELINE:
            kw.pop("until", None)
            kw.pop("finished", None)
            if isinstance(kw.get("portraits"), frozenset):
                kw["portraits"] = "all"
        real(self, books, *a, **kw)
    monkeypatch.setattr(art.Illustrator, "__init__", spy)
    return got


def scarred(e):
    """Mira gets a lasting look change late in chapter two (after Gabriel arrives), so a position before it must not draw it."""
    doc = json.loads(e.index.read_text())
    mira = next(x for x in doc["entities"] if x["id"] == "mira")
    mira["facets"].append({"id": "mira.f9", "from": "a12", "basis": "stated", "evidence": ["a12"],
                           "text": "Mira is scarred across one cheek and keeps her hair cut short."})
    e.index.write_text(json.dumps(doc))
    return doc


def anchor_ord(doc, aid):
    return [a["id"] for a in doc["anchors"]].index(aid)


# ---- the config


def test_the_art_config_takes_entities_a_position_and_finished_books():
    from lorekeeper.web.jobs import art_config
    got = art_config({**BASE, "entities": ["mira", "mira", "oren"], "until": {"p": "a10"}, "finished": ["one", "one"]})
    assert got["entities"] == ["mira", "oren"] and got["until"] == {"p": "a10", "book": None} and got["finished"] == ["one"]
    assert art_config(got) == got                                              # a resume reads it back
    assert art_config({**BASE, "entities": ["mira"]})["portraits"] is False     # entries alone are something to draw
    plain = art_config({"style": "ink", "cap": 10})
    assert plain["entities"] is None and plain["until"] is None and plain["finished"] is None
    assert art_config({**BASE, "entities": ["mira"], "until": {"p": 0.5}})["until"]["p"] == 0.5
    for bad in ([], "mira", ["Mira"], ["../x"], [1], ["x"] * 13):
        with pytest.raises(ValueError, match="entities"):
            art_config({**BASE, "entities": bad})
    for bad in ("a10", {"p": True}, {"p": 2}, {"p": ""}, {"p": "a1", "book": 3}, {"p": "a1", "x": 1}, {}):
        with pytest.raises(ValueError, match="until"):
            art_config({**BASE, "entities": ["mira"], "until": bad})
    with pytest.raises(ValueError, match="portraits only"):                    # scenes up to a position: not offered
        art_config({**BASE, "level": "main", "entities": ["mira"], "until": {"p": "a10"}})
    for bad in ("one", [1], [""]):
        with pytest.raises(ValueError, match="finished"):
            art_config({**BASE, "entities": ["mira"], "finished": bad})
    with pytest.raises(ValueError, match="Choose chapters or portraits"):
        art_config({**BASE, "entities": None})


def test_run_limits_turn_the_position_into_an_ordinal_and_refuse_one_no_longer_there():
    doc = {"anchors": [{"id": "a0"}, {"id": "a1"}, {"id": "a2"}]}
    cfg = {"entities": ["mira"], "until": {"p": "a1", "book": None}, "finished": None}
    assert jobs_mod.run_limits(cfg, doc, 2) == {"portraits": frozenset({"mira"}), "until": (2, 1), "finished": None}
    assert jobs_mod.run_limits({**cfg, "finished": ["one"]}, doc, 0)["finished"] == frozenset({"one"})
    assert jobs_mod.run_limits({"entities": None, "until": None}, doc, 0) == {}      # an ordinary run: nothing new
    with pytest.raises(ValueError, match="no longer"):
        jobs_mod.run_limits({**cfg, "until": {"p": "a9", "book": None}}, doc, 0)


# ---- the run route's refusals


def post_run(e, **cfg):
    return e.c.post(f"/api/library/{KEY}/art/run", json={"art": {**BASE, **cfg}})


def test_the_run_refuses_an_entry_past_the_position_unknown_or_not_a_person(env):
    env.key()
    # Gabriel first appears at a10: not reached at a9, and an id the index never had gets the same answer
    for eid in ("gabriel", "nobody"):
        r = post_run(env, entities=[eid], until={"p": "a9"})
        assert r.status_code == 422 and r.json()["code"] == "not-visible", r.text
    r = post_run(env, entities=["mill"], until={"p": "a13"})
    assert r.status_code == 422 and r.json()["code"] == "bad-entity"
    for until in ({"p": "nope"}, {"p": "a10", "book": "two"}):
        r = post_run(env, entities=["mira"], until=until)
        assert r.status_code == 422 and r.json()["code"] == "bad-position", r.text
    r = post_run(env, entities=["mira"], until={"p": 0.0})          # nothing read yet: nothing to draw
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    r = post_run(env, entities=["mira"], until={"p": "a10"}, finished=["one"])     # a standalone book has no earlier books
    assert r.status_code == 422 and r.json()["code"] == "bad-position"
    assert env.c.get("/api/jobs").json() == [] or all(j["kind"] != "art" for j in env.c.get("/api/jobs").json())


def test_the_run_stores_the_position_as_an_anchor_and_hands_the_pipeline_its_limits(env, seen):
    env.key()
    doc = json.loads(env.index.read_text())
    r = post_run(env, entities=["mira"], until={"p": 1.0})
    assert r.status_code == 200, r.text
    job = wait_for(env.c, r.json()["id"])
    assert job["status"] == "done", (job.get("error"), env.jobs.events[job["id"]][-5:])
    cfg = env.jobs.jobs[job["id"]].config["art"]
    assert cfg["until"] == {"p": doc["anchors"][-1]["id"], "book": None} and cfg["entities"] == ["mira"]
    ran = seen[-1]
    assert ran["portraits"] == frozenset({"mira"}) and ran["until"] == (0, len(doc["anchors"]) - 1)
    assert ran["finished"] is None


def test_an_ordinary_run_hands_the_pipeline_nothing_new(env, seen):
    env.key()
    wait_for(env.c, env.c.post(f"/api/library/{KEY}/art/run", json={"art": {"style": "ink", "cap": 10}}).json()["id"])
    assert seen[-1]["until"] is None and seen[-1]["finished"] == "absent" and seen[-1]["portraits"] == "main"


def test_a_run_whose_position_left_the_index_draws_nothing(env, seen):
    env.key()
    # as if the index were rebuilt between queueing and running: the stored anchor is no longer there
    cfg = {**BASE, "entities": ["mira"], "until": {"p": "gone", "book": None}}
    job = env.jobs.submit_art(KEY, cfg, index_path=str(env.index), book_id="", title="Millbrook", text_hash=env.hash)
    done = wait_for(env.c, job.id)
    assert done["status"] == "failed" and "no longer" in done["error"]
    assert not env.image.calls


# ---- the estimate


def test_the_estimate_checks_an_entity_run_as_the_run_route_does(env, seen):
    q = {"key": KEY, "style": "ink", "level": "none", "portraits": "none"}
    ok = env.c.get("/api/art/estimate", params={**q, "entities": "mira", "until": "a10"})
    assert ok.status_code == 200, ok.text
    got = ok.json()
    assert got["entities"] == ["mira"] and got["until"] == {"p": "a10", "book": None} and got["plates"] == 0
    assert got["portraitsMax"] >= got["portraits"] >= 1 and got["priceMax"] >= got["price"] > 0
    assert env.c.get("/api/art/estimate", params={**q, "entities": "gabriel", "until": "a9"}).json()["code"] == "not-visible"
    assert env.c.get("/api/art/estimate", params={**q, "entities": "mill", "until": "a9"}).json()["code"] == "bad-entity"
    assert env.c.get("/api/art/estimate", params={**q, "entities": "Mira!"}).json()["code"] == "bad-config"
    assert env.c.get("/api/art/estimate", params={**q, "entities": "mira", "until": "0.5"}).status_code == 200
    # an ordinary estimate keeps its shape, with the upper bound equal to the count when every look index is known
    plain = env.c.get("/api/art/estimate", params={"key": KEY, "style": "ink"}).json()
    assert "entities" not in plain and plain["portraitsMax"] >= plain["portraits"]


@needs_pipeline
def test_the_estimate_counts_only_the_entrys_portraits_up_to_the_position(env):
    scarred(env)
    q = {"key": KEY, "style": "ink", "level": "none", "portraits": "none", "entities": "mira"}
    early = env.c.get("/api/art/estimate", params={**q, "until": "a10"}).json()
    late = env.c.get("/api/art/estimate", params={**q, "until": "a13"}).json()
    assert early["portraitsMax"] <= late["portraitsMax"]
    assert early["portraits"] >= 1


# ---- the run, end to end


def portraits_of(e, eid):
    m = json.loads((e.data / "art" / e.hash / "ink" / "art.json").read_text())
    return [r for r in m["pictures"] if r["kind"] == "portrait" and r["target"] == eid]


@needs_pipeline
def test_a_run_up_to_a_position_draws_only_that_entry_and_never_past_it(env):
    env.key()
    doc = scarred(env)
    at = "a10"
    done = wait_for(env.c, post_run(env, entities=["mira"], until={"p": at}).json()["id"])
    assert done["status"] == "done", done.get("error")
    m = json.loads((env.data / "art" / env.hash / "ink" / "art.json").read_text())
    assert m["pictures"] and {r["target"] for r in m["pictures"]} == {"mira"}
    assert all(r["kind"] == "portrait" for r in m["pictures"])
    limit = anchor_ord(doc, at)
    assert all(anchor_ord(doc, r["from"]) <= limit and all(anchor_ord(doc, s) <= limit for s in r.get("sources", []))
               for r in m["pictures"])
    first = len(m["pictures"])
    # the estimate now knows the look index: nothing left to draw up to a10, the change still to draw by a13
    q = {"key": KEY, "style": "ink", "level": "none", "portraits": "none", "entities": "mira"}
    assert env.c.get("/api/art/estimate", params={**q, "until": at}).json()["portraits"] == 0
    assert env.c.get("/api/art/estimate", params={**q, "until": "a13"}).json()["portraits"] == 1
    # read on to the end: the look change is drawn now, and what was drawn is not drawn again
    done = wait_for(env.c, post_run(env, entities=["mira"], until={"p": "a13"}).json()["id"])
    assert done["status"] == "done"
    after = portraits_of(env, "mira")
    assert len(after) > first and len({r["from"] for r in after}) == len(after)
    n = len(env.image.calls)
    wait_for(env.c, post_run(env, entities=["mira"], until={"p": "a13"}).json()["id"])
    assert len(env.image.calls) == n


@needs_pipeline
def test_undo_takes_out_the_runs_portraits(env):
    env.key()
    done = wait_for(env.c, post_run(env, entities=["mira"], until={"p": "a13"}).json()["id"])
    entry = env.jobs.jobs[done["id"]].progress.get("artUndo")
    assert entry and portraits_of(env, "mira")
    assert env.c.post(f"/api/library/{KEY}/undo/{entry}").status_code == 200
    assert portraits_of(env, "mira") == []


# ---- a series


def test_a_series_run_draws_into_the_earlier_books_the_reader_finished(series_env, seen, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    ask = {**BASE, "entities": ["mira"], "until": {"p": "a10"}}
    r = c.post(f"/api/library/{KEY}/art/run", json={"art": {**ask, "finished": ["one"]}})
    assert r.status_code == 200, r.text
    job = c.app.state.jobs.jobs[r.json()["id"]]
    assert job.config["text_hashes"] == [c.hash_one] and job.config["art"]["until"]["book"] == "two"
    done = wait_for(c, job.id)
    assert done["status"] == "done", (done.get("error"), c.app.state.jobs.events[job.id][-5:])
    ran = seen[-1]
    assert ran["books"] == [("one", True, True), ("mill", True, True)] and ran["finished"] == frozenset({"one"})
    assert ran["until"] == (1, 10)
    # book one not finished: it is only read, and the pipeline is told so (its pictures marked partial)
    r = c.post(f"/api/library/{KEY}/art/run", json={"art": {**ask, "finished": []}})
    assert r.status_code == 200, r.text
    job = c.app.state.jobs.jobs[r.json()["id"]]
    assert "text_hashes" not in job.config
    wait_for(c, job.id)
    assert seen[-1]["books"] == [("one", False, False), ("mill", True, True)] and seen[-1]["finished"] == frozenset()
    r = c.post(f"/api/library/{KEY}/art/run", json={"art": {**ask, "finished": ["two"]}})
    assert r.status_code == 422 and r.json()["code"] == "bad-position"


def test_an_entry_only_an_unfinished_earlier_book_has_is_not_visible(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    # book one's own ids are b1...: its Mira is b1mira, which book two does not list
    r = c.post(f"/api/library/{KEY}/art/run", json={"art": {**BASE, "entities": ["b1mira"], "until": {"p": "a13"},
                                                               "finished": []}})
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    r = c.post(f"/api/library/{KEY}/art/run", json={"art": {**BASE, "entities": ["b1mira"], "until": {"p": "a13"},
                                                               "finished": ["one"]}})
    assert r.status_code == 200, r.text


def test_a_run_drawing_an_earlier_book_blocks_its_editions_routes(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    mgr = c.app.state.jobs
    job = jobs_mod.Job(id="x1", book_id="", book_title="t", output="o", kind="art",
                       config={"art": {}, "text_hash": "sha256:" + "0" * 64, "text_hashes": [c.hash_one]})
    mgr.jobs["x1"] = job
    try:
        assert mgr.drawing(c.hash_one)
    finally:
        job.status = "done"


# ---- the models (picture-model-choices): a through-the-book run is an ordinary pictures run for the models it uses

def test_an_entry_run_writes_with_the_portrait_writer_and_draws_with_the_portrait_image_model(env, monkeypatch):
    from .test_web_art import GEMINI, WRITERS, is_portrait, models_env
    made, images_made, kw = models_env(env, monkeypatch)
    r = post_run(env, entities=["mira"], until={"p": "a13"}, provider="gemini", model=GEMINI,
                 portraitWriter={"provider": "gemini", "model": "gemini-3.1-pro-preview"},
                 portraitProvider="gemini", portraitModel="gemini-3-pro-image")
    assert r.status_code == 200, r.text
    done = wait_for(env.c, r.json()["id"])
    assert done["status"] == "done", done.get("error")
    assert kw["portraits"] == frozenset({"mira"}) and kw["until"] is not None
    port = made[("gemini", "gemini-3.1-pro-preview")]
    assert kw["portrait_text"] is port
    wrote = [p for p, s in zip(port.prompts, port.systems) if s in WRITERS]
    assert wrote and all(is_portrait(p) for p in wrote)
    assert ("gemini", "gemini-3-pro-image") in images_made
    m = json.loads((env.data / "art" / env.hash / "ink" / "art.json").read_text())
    assert m["pictures"] and all(r["kind"] == "portrait" and r["target"] == "mira" for r in m["pictures"])


def test_the_entry_estimate_prices_with_the_portrait_image_model(env):
    env.key()
    q = {"key": KEY, "style": "ink", "level": "none", "portraits": "none", "entities": "mira", "until": "a13",
         "provider": "gemini"}
    got = env.c.get("/api/art/estimate", params={**q, "portraitProvider": "gemini", "portraitModel": "gemini-3-pro-image"}).json()
    assert got["portraitModel"] == "gemini-3-pro-image" and got["entities"] == ["mira"] and got["portraits"] >= 1
    assert env.c.get("/api/art/estimate", params={**q, "portraitModel": "gpt-image-2"}).status_code == 422
