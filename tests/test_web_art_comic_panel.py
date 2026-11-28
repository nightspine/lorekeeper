"""Fixing one panel of a comic page in the web app: the panel route, its gates, undo, and the grid the art route serves.
The pipeline's `fix_panel` and `comic_grid` are used when they exist; until then they are scripted here (the paste-back
itself is tested with the pipeline). Scripted models (tests/test_art.py); no network."""

import json

import pytest

pytest.importorskip("fastapi")

from lorekeeper import art  # noqa: E402
from lorekeeper.web import jobs as jobs_mod  # noqa: E402

from .test_art import Seeing  # noqa: E402
from .test_web_art import KEY, env, series_env, short_world, undo  # noqa: E402,F401
from .test_web_art_comic import CH, anchor_of, ask  # noqa: E402

GRID = [[0.0, 0.0, 0.5, 0.33], [0.5, 0.0, 1.0, 0.33], [0.0, 0.33, 0.5, 0.66]]


@pytest.fixture
def pipe(monkeypatch):
    """The pipeline's panel fix when it is there, else a scripted one that files a plain fix with the panel's entry."""
    monkeypatch.setattr(art, "PANEL_TEXT_CHARS", getattr(art, "PANEL_TEXT_CHARS", 600), raising=False)
    calls = []
    if not hasattr(art.Illustrator, "fix_panel"):
        def fix_panel(self, rec, k, text="", note="", faults=(), bi=None, verify=True, checker=None):
            calls.append((k, text, note, list(faults)))
            new = art.Illustrator.fix(self, rec, list(faults), note or text or "panel", bi=bi, verify=False)
            new["fixes"][-1].update(panel=k, pasted=True, **({"text": text} if text else {}))
            m = self.man[len(self.books) - 1]
            for r in m["pictures"]:
                if r["id"] == new["id"]:
                    r["fixes"] = new["fixes"]
            self._save(len(self.books) - 1)
            return new
        monkeypatch.setattr(art.Illustrator, "fix_panel", fix_panel, raising=False)
    monkeypatch.setattr(art, "comic_grid", lambda data, n: GRID[:n] if n == 3 else None, raising=False)
    return calls


@pytest.fixture
def eye(env, monkeypatch):
    e = Seeing()
    e.name, e.provider = "claude-sonnet-5-5", "claude"
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None, tier="cheap": e)
    return e


def page(env):
    env.key()
    at = anchor_of(env, "e6")
    return ask(env, "draw", p=at, prompt=ask(env, "prompt", p=at).json()["prompt"]).json()


def ok(env, rec, **body):
    return env.c.post(f"/api/library/{KEY}/art/{rec['id']}/panel", json=body)


def pictures(env):
    return env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]


def test_a_panel_fix_takes_the_pages_place_and_undo_brings_it_back(env, pipe):
    old = page(env)
    r = ok(env, old, k=2, text="The miller waves.", note="keep the rain", p=1.0)
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["replaces"] == old["id"] and new["kind"] == "comic" and new["panels"] == old["panels"]
    assert new["fixes"][-1]["panel"] == 2 and new["undo"] and "fixCost" in new and "verified" in new
    ids = {p["id"] for p in pictures(env)}
    assert old["id"] not in ids and new["id"] in ids
    entry = json.loads(next((env.data / "merges").glob(f"{new['undo']}/entry.json")).read_text())
    assert entry["art"]["op"] == "redraw" and entry["what"].startswith("fixed panel 2 of a comic")
    undo(env, new["undo"])
    ids = {p["id"] for p in pictures(env)}
    assert old["id"] in ids and new["id"] not in ids and env.c.get(old["url"]).status_code == 200


@pytest.mark.parametrize("body", [
    {}, {"k": 0, "note": "x"}, {"k": 4, "note": "x"}, {"k": "2", "note": "x"}, {"k": True, "note": "x"},
    {"k": 1.5, "note": "x"}, {"k": None, "note": "x"},
])
def test_a_panel_outside_the_page_is_bad_panel(env, pipe, body):
    old = page(env)
    env.image.calls.clear()
    r = ok(env, old, **body)
    assert r.status_code == 422 and r.json()["code"] == "bad-panel" and env.image.calls == []


@pytest.mark.parametrize("body", [
    {"k": 1}, {"k": 1, "text": "  "}, {"k": 1, "text": "x" * 601}, {"k": 1, "text": 5}, {"k": 1, "note": "n" * 301},
    {"k": 1, "faults": [{"what": ""}]}, {"k": 1, "faults": "hand"}, {"k": 1, "faults": [{"what": "x", "severity": "huge"}]},
])
def test_a_bad_panel_fix_is_refused_before_anything_is_drawn(env, pipe, body):
    old = page(env)
    env.image.calls.clear()
    r = ok(env, old, **body)
    assert r.status_code == 422 and r.json()["code"] == "bad-fix", body
    assert env.image.calls == []


def test_only_a_comic_has_panels(env, pipe):
    env.key()
    ev = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink",
                                                          "prompt": "A gate."}).json()
    r = ok(env, ev, k=1, note="x")
    assert r.status_code == 422 and r.json()["code"] == "not-comic"


def test_the_panel_route_keeps_the_fix_gates(env, pipe):
    old = page(env)
    first = env.doc["anchors"][0]["id"]
    r = ok(env, old, k=1, note="x", p=first)
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    assert env.c.post(f"/api/library/{KEY}/art/p00000000/panel", json={"k": 1, "note": "x"}).status_code == 404
    r = ok(env, old, k=1, note="x", finished=["nope"])
    assert r.status_code == 422 and r.json()["code"] == "bad-position"
    env.jobs.drawing = lambda h: True
    r = ok(env, old, k=1, note="x")
    assert r.status_code == 409 and r.json()["code"] == "busy"


def test_a_panel_fix_without_the_image_key_is_409(env, pipe, monkeypatch):
    old = page(env)
    monkeypatch.delenv("GEMINI_API_KEY")
    r = ok(env, old, k=1, note="x")
    assert r.status_code == 409 and r.json()["code"] == "no-image-key"


def test_a_panel_fix_that_changed_nothing_is_422_unchanged_and_files_nothing(env, pipe, monkeypatch):
    old = page(env)
    before = pictures(env)
    verdict = art.Verdict(results=[{"n": 1, "fixed": "no"}], changed=False, new=[], checked=True, cost=0.002)

    def fix_panel(self, rec, k, text="", note="", faults=(), bi=None, verify=True, checker=None):
        raise art.FixUnchanged(verdict, 0.042)
    monkeypatch.setattr(art.Illustrator, "fix_panel", fix_panel, raising=False)
    r = ok(env, old, k=3, text="New words")
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "unchanged" and body["verified"]["of"] == 1 and body["fixCost"] == pytest.approx(0.042)
    assert pictures(env) == before


def test_a_refused_panel_fix_keeps_the_page(env, pipe, monkeypatch):
    old = page(env)
    before = pictures(env)

    def fix_panel(self, *a, **k):
        raise art.images.ImageRefused("IMAGE_SAFETY")
    monkeypatch.setattr(art.Illustrator, "fix_panel", fix_panel, raising=False)
    r = ok(env, old, k=1, note="more blood")
    assert r.status_code == 422 and r.json()["code"] == "refused"
    assert pictures(env) == before


def test_the_art_route_serves_each_comics_grid_without_writing_it(env, pipe, monkeypatch):
    old = page(env)
    manifest = env.data / "art"
    stored = sorted(manifest.rglob("art.json"))[0]
    text_before = stored.read_text()
    got = next(p for p in pictures(env) if p["id"] == old["id"])
    assert got["grid"] == GRID                                       # computed from the file, not on disk
    assert stored.read_text() == text_before
    # a stored grid is served as stored, and the file is not read again
    data = json.loads(text_before)
    for p in data["pictures"]:
        if p["id"] == old["id"]:
            p["grid"] = [[0, 0, 1, 1]] * 3
    stored.write_text(json.dumps(data))
    monkeypatch.setattr(art, "comic_grid", lambda *a: pytest.fail("read again"))
    got = next(p for p in pictures(env) if p["id"] == old["id"])
    assert got["grid"] == [[0, 0, 1, 1]] * 3
    # a page whose boxes are not found has no grid; a scene is served without the key
    ev = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink",
                                                          "prompt": "A gate."}).json()
    assert "grid" not in next(p for p in pictures(env) if p["id"] == ev["id"])


def test_the_art_route_serves_panel_texts_as_stored_else_cut_from_the_prompt(env, pipe, monkeypatch):
    old = page(env)
    got = next(p for p in pictures(env) if p["id"] == old["id"])
    if "panelTexts" not in old:
        seen = []
        monkeypatch.setattr(art, "panel_texts", lambda prompt, style="": seen.append(style) or ["a", "b", "c"], raising=False)
        got = next(p for p in pictures(env) if p["id"] == old["id"])
        assert got["panelTexts"] == ["a", "b", "c"] and seen and seen[-1] == art.style_sentence("ink")
        monkeypatch.setattr(art, "panel_texts", lambda prompt, style="": ["a"], raising=False)    # wrong length: omitted
        assert "panelTexts" not in next(p for p in pictures(env) if p["id"] == old["id"])
        monkeypatch.setattr(art, "panel_texts", lambda prompt, style="": None, raising=False)
        assert "panelTexts" not in next(p for p in pictures(env) if p["id"] == old["id"])
    else:
        assert len(got["panelTexts"]) == 3
