"""Drawing faults in the web app: the check and fix routes, `check` on the sheet's draw, `art.check` in a run's config and
in the estimate. Scripted text and image models (tests/test_art.py); no network."""

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import art, llm  # noqa: E402
from lorekeeper.web import jobs as jobs_mod  # noqa: E402

from .test_art import HEAD, Seeing  # noqa: E402
from .test_web_art import GEMINI, KEY, env, pictures, run_set, short_world, undo  # noqa: E402,F401


@pytest.fixture
def eye(env, monkeypatch):
    """The picker with eyes: every picture route that checks uses it."""
    e = Seeing()
    seen = []

    def vision_model(self, choice=None):
        seen.append(choice)
        return e
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", vision_model)
    e.chosen = seen
    return e


def one_picture(env):
    env.key()
    run_set(env, portraits=False)
    return pictures(env)[0]


def test_the_check_answers_the_faults_and_their_cost_and_writes_nothing(env, eye):
    old = one_picture(env)
    eye.faults = [HEAD]
    before = pictures(env)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"faults": [HEAD], "cost": pytest.approx(0.001)}
    prompt, data = eye.looked[-1]
    assert old["prompt"][:120] in prompt and data == [env.c.get(old["url"]).content]    # the stored prompt goes as context
    assert pictures(env) == before
    eye.faults = []
    assert env.c.post(f"/api/library/{KEY}/art/{old['id']}/check").json()["faults"] == []


def test_the_check_takes_the_requests_picker(env, eye, monkeypatch):
    old = one_picture(env)
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: ["claude"])
    env.jobs.text_models = lambda p: ["claude-haiku-5-5"] if p == "claude" else []
    body = {"picker": {"provider": "claude", "model": "claude-haiku-5-5"}}
    assert env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json=body).status_code == 200
    assert eye.chosen[-1] == {"provider": "claude", "model": "claude-haiku-5-5"}
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={"picker": {"provider": "claude", "model": "nope"}})
    assert r.status_code == 422 and r.json()["code"] == "bad-picker"


def test_without_a_model_that_sees_pictures_the_check_is_409_no_vision(env, monkeypatch):
    old = one_picture(env)
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None: None)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={})
    assert r.status_code == 409 and r.json()["code"] == "no-vision"
    blind = Seeing(blind=True)
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None: blind)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={})
    assert r.status_code == 409 and r.json()["code"] == "no-vision"


def test_a_failed_check_is_an_error_not_a_clean_picture(env, eye):
    old = one_picture(env)
    eye.fail = True
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={})
    assert r.status_code == 502 and r.json()["code"] == "model-failed"


def test_the_vision_model_skips_presets_that_cannot_see(env, monkeypatch):
    made = []
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: ["groq", "gemini"])
    monkeypatch.setattr(jobs_mod.JobManager, "cloud_model", lambda self, p, m=None: made.append((p, m)) or (p, m))
    got = env.jobs.vision_model()
    assert got == ("gemini", llm.PROVIDERS["gemini"]["cheap"])
    groq = llm.PROVIDERS["groq"]["cheap"]
    assert llm.vision("groq", groq) is False and env.jobs.vision_model({"provider": "groq", "model": groq}) is None
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: ["groq"])
    assert env.jobs.vision_model() is None


def test_the_check_and_fix_keep_the_redraw_gates(env, eye):
    old = one_picture(env)
    first = env.doc["anchors"][0]["id"]
    for route, body in (("check", {}), ("fix", {"faults": [HEAD]})):
        r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/{route}", json={**body, "p": first})
        assert r.status_code == 422 and r.json()["code"] == "not-visible", route
        r = env.c.post(f"/api/library/{KEY}/art/p00000000/{route}", json=body)
        assert r.status_code == 404, route
        r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/{route}", json={**body, "finished": ["nope"]})
        assert r.status_code == 422 and r.json()["code"] == "bad-position", route
    env.jobs.drawing = lambda h: True
    for route, body in (("check", {}), ("fix", {"faults": [HEAD]})):
        r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/{route}", json=body)
        assert r.status_code == 409 and r.json()["code"] == "busy", route


def test_fix_edits_the_picture_in_its_place_and_undo_brings_it_back(env):
    old = one_picture(env)
    old_bytes = env.c.get(old["url"]).content
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix",
                   json={"faults": [HEAD], "note": "keep the lantern", "p": env.doc["anchors"][-1]["id"]})
    assert r.status_code == 200, r.text
    new = r.json()
    call = env.image.calls[-1]
    assert call["prompt"] == art.fix_prompt([HEAD], "keep the lantern")
    assert len(call["references"]) == 1 and call["references"][0] == old_bytes
    assert new["replaces"] == old["id"] and new["prompt"] == old["prompt"] and new["from"] == old["from"]
    assert new["provider"] == old["provider"] and new["quality"] == old["quality"] and new["shape"] == old["shape"]
    assert new["fixes"][-1]["faults"] == [HEAD] and new["fixes"][-1]["note"] == "keep the lantern"
    assert new["fixCost"] == new["fixes"][-1]["cost"] and new["undo"] and new["slug"] == "ink"
    assert new["cost"] == pytest.approx((old["cost"] or 0) + new["fixCost"])
    ids = {p["id"] for p in pictures(env)}
    assert old["id"] not in ids and new["id"] in ids
    entry = json.loads(next((env.data / "merges").glob(f"{new['undo']}/entry.json")).read_text())
    assert entry["art"]["op"] == "redraw" and entry["what"].startswith("fixed a picture")
    undo(env, new["undo"])
    ids = {p["id"] for p in pictures(env)}
    assert old["id"] in ids and new["id"] not in ids and env.c.get(old["url"]).status_code == 200


def test_a_note_alone_is_something_to_fix(env):
    old = one_picture(env)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"note": "the left hand has six fingers"})
    assert r.status_code == 200, r.text
    assert r.json()["fixes"][-1] == {**r.json()["fixes"][-1], "faults": [], "note": "the left hand has six fingers"}


@pytest.mark.parametrize("body", [
    {}, {"faults": []}, {"faults": [], "note": "  "}, {"faults": "the head"}, {"faults": [{"where": "left"}]},
    {"faults": [{"what": ""}]}, {"faults": [{"what": "x" * 201}]}, {"faults": [{"what": "x", "where": "y" * 121}]},
    {"faults": [{"what": "x", "where": 3}]}, {"faults": [HEAD] * 9}, {"note": "n" * 301}, {"faults": [HEAD], "note": 5},
])
def test_a_bad_fix_is_refused_before_anything_is_drawn(env, body):
    old = one_picture(env)
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json=body)
    assert r.status_code == 422 and r.json()["code"] == "bad-fix", body
    assert env.image.calls == []


def test_a_refused_fix_keeps_the_picture(env):
    old = one_picture(env)
    before = pictures(env)
    env.image.refuse = [lambda prompt, n: True, lambda prompt, n: True]
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]})
    assert r.status_code == 422 and r.json()["code"] == "refused" and "safety" in r.json()["detail"]
    assert pictures(env) == before


def test_fix_without_the_records_image_key_is_409(env, monkeypatch):
    old = one_picture(env)
    monkeypatch.delenv("GEMINI_API_KEY")
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]})
    assert r.status_code == 409 and r.json()["code"] == "no-image-key" and "Gemini" in r.json()["detail"]


def test_the_sheets_draw_can_check_and_fix_once(env, eye):
    env.key()
    eye.faults = [HEAD]
    body = {"kind": "event", "target": "e5", "style": "ink", "prompt": "A woman waits at a mill gate at dusk."}
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={**body, "check": True})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["check"] == {"faults": [HEAD], "fixed": True} and len(rec["fixes"]) == 1
    assert rec["prompt"] == body["prompt"]               # the person's words are not rewritten
    assert len(eye.looked) == 1                           # never checked again
    n = len(eye.looked)
    plain = env.c.post(f"/api/library/{KEY}/art/draw", json={**body, "target": "e6"}).json()
    assert "check" not in plain and len(eye.looked) == n  # off unless asked
    r = env.c.post(f"/api/library/{KEY}/art/draw", json={**body, "check": "yes"})
    assert r.status_code == 422 and r.json()["code"] == "bad-config"


def test_the_job_config_carries_check(env):
    assert jobs_mod.art_config({"style": "ink", "cap": 10})["check"] is False
    got = jobs_mod.art_config({"style": "ink", "cap": 10, "check": True})
    assert got["check"] is True and jobs_mod.art_config(got) == got
    with pytest.raises(ValueError):
        jobs_mod.art_config({"style": "ink", "cap": 10, "check": 1})


def test_a_run_with_check_checks_every_picture_and_reports_it(env, monkeypatch):
    eye = Seeing(faults=[HEAD])
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: eye)
    env.key()
    done = run_set(env, portraits=False, check=True)
    assert done["status"] == "done", done["error"]
    rep = done["report"]["art"]
    assert rep["checked"] == rep["drawn"] > 0 and rep["fixed"] == rep["drawn"] and rep["fix_refused"] == 0
    assert all(p["check"]["fixed"] for p in pictures(env))
    assert "pictures: checked:" in (env.data / "jobs" / f"{done['id']}.events.jsonl").read_text()


def test_a_run_whose_picker_cannot_see_pictures_is_drawn_unchecked_with_a_line(env, monkeypatch):
    env.key()
    blind = Seeing(faults=[HEAD])
    blind.name, blind.provider = llm.PROVIDERS["groq"]["cheap"], "groq"
    monkeypatch.setattr(jobs_mod.JobManager, "cheap_model", lambda self, provider=None: blind)
    done = run_set(env, portraits=False, check=True)
    assert done["status"] == "done" and blind.looked == []
    assert done["report"]["art"]["checked"] == 0 and not any("check" in p for p in pictures(env))


def test_the_estimate_adds_the_check_when_asked(env, monkeypatch):
    env.key()
    q = {"key": KEY, "style": "ink", "level": "main"}
    plain = env.c.get("/api/art/estimate", params=q).json()
    assert "check" not in plain
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None: None)
    got = env.c.get("/api/art/estimate", params={**q, "check": "true"}).json()
    n = plain["plates"] + plain["portraits"]
    scene = llm.image_price("gemini", GEMINI, "standard", "landscape")
    port = llm.image_price("gemini", GEMINI, "standard", "portrait")
    extra = n * art.CHECK_FALLBACK + art.FIX_RATE * (plain["plates"] * scene + plain["portraits"] * port)
    assert got["check"]["each"] == art.CHECK_FALLBACK and got["check"]["fixRate"] == art.FIX_RATE
    assert got["price"] == pytest.approx(plain["price"] + extra, abs=1e-4)
    assert got["check"]["price"] == pytest.approx(extra, abs=1e-4)
    assert got["pictures"] == plain["pictures"]


def test_the_new_routes_need_the_csrf_header(env):
    bare = TestClient(env.c.app)
    for route in ("check", "fix"):
        assert bare.post(f"/api/library/{KEY}/art/p00000000/{route}").status_code == 403, route
