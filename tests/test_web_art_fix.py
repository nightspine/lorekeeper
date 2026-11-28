"""Drawing faults in the web app: the check and fix routes, `check` on the sheet's draw, `art.check` in a run's config and
in the estimate. Scripted text and image models (tests/test_art.py); no network."""

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import art, llm  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402
from lorekeeper.web import jobs as jobs_mod  # noqa: E402

from .test_art import HEAD, Seeing  # noqa: E402
from .test_web_art import GEMINI, KEY, env, pictures, run_set, series_env, short_world, undo  # noqa: E402,F401




@pytest.fixture
def eye(env, monkeypatch):
    """The picker with eyes: every picture route that checks uses it. `eye.chosen` has each (choice, tier) asked for."""
    e = Seeing()
    e.name, e.provider = "claude-sonnet-5-5", "claude"
    seen = []

    def vision_model(self, choice=None, tier="cheap"):
        seen.append((choice, tier))
        return e
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", vision_model)
    e.chosen = seen
    return e


MAJOR = {**HEAD, "severity": "major", "kind": "drawing"}


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
    assert r.json() == {"faults": [MAJOR], "cost": pytest.approx(0.001),
                        "model": {"provider": "claude", "model": "claude-sonnet-5-5"}}
    assert eye.chosen[-1] == (None, "strong")                     # the strong tier judges, not the cheap picker
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
    assert eye.chosen[-1][0] == {"provider": "claude", "model": "claude-haiku-5-5"}
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={"picker": {"provider": "claude", "model": "nope"}})
    assert r.status_code == 422 and r.json()["code"] == "bad-picker"


def test_without_a_model_that_sees_pictures_the_check_is_409_no_vision(env, monkeypatch):
    old = one_picture(env)
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None, tier="cheap": None)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={})
    assert r.status_code == 409 and r.json()["code"] == "no-vision"
    blind = Seeing(blind=True)
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None, tier="cheap": blind)
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
    assert new["fixes"][-1]["faults"] == [{**HEAD, "severity": "major"}]       # sent without one: fixed in full
    assert new["fixes"][-1]["note"] == "keep the lantern"
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
    assert rec["check"] == {"faults": [HEAD], "fixed": True, "verified": rec["fixes"][-1]["verified"]} and len(rec["fixes"]) == 1
    assert rec["fixes"][-1]["verified"]["fixed"] == 1
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
    monkeypatch.setattr(jobs_mod.JobManager, "text_choice", lambda self, tier="cheap", jp=None: ("claude", "claude-haiku-5-5"))
    got = env.c.get("/api/art/estimate", params={**q, "check": "true"}).json()
    one = art.check_price("claude", "claude-haiku-5-5")
    n = plain["plates"] + plain["portraits"]
    scene = llm.image_price("gemini", GEMINI, "standard", "landscape")
    port = llm.image_price("gemini", GEMINI, "standard", "portrait")
    extra = n * (one + art.FIX_RATE * got["check"]["verify"]) + art.FIX_RATE * (plain["plates"] * scene + plain["portraits"] * port)
    assert got["check"]["each"] == pytest.approx(one) and got["check"]["fixRate"] == art.FIX_RATE
    assert got["check"]["provider"] == "claude" and got["check"]["model"] == "claude-haiku-5-5"
    assert got["price"] == pytest.approx(plain["price"] + extra, abs=1e-4)
    assert got["check"]["price"] == pytest.approx(extra, abs=1e-4)
    assert got["pictures"] == plain["pictures"]


def test_the_estimate_adds_no_check_the_run_would_not_make(env, monkeypatch):
    env.key()
    q = {"key": KEY, "style": "ink", "level": "main", "check": "true"}
    plain = env.c.get("/api/art/estimate", params={**q, "check": "false"}).json()
    groq = llm.PROVIDERS["groq"]["cheap"]
    monkeypatch.setattr(jobs_mod.JobManager, "text_choice", lambda self, tier="cheap", jp=None: ("groq", groq))
    got = env.c.get("/api/art/estimate", params=q).json()
    assert "check" not in got and got["price"] == plain["price"]          # a picker that cannot see: the run is unchecked
    monkeypatch.setattr(jobs_mod.JobManager, "text_choice", lambda self, tier="cheap", jp=None: None)
    got = env.c.get("/api/art/estimate", params=q).json()
    assert "check" not in got and got["price"] == plain["price"]          # no picker at all
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: ["groq"])
    env.jobs.text_models = lambda p: [groq] if p == "groq" else []
    got = env.c.get("/api/art/estimate", params={**q, "pickerProvider": "groq", "pickerModel": groq}).json()
    assert "check" not in got                                             # the chosen picker, as the run would use it
    r = env.c.get("/api/art/estimate", params={**q, "pickerProvider": "groq"})
    assert r.status_code == 422 and r.json()["code"] == "bad-picker"


def test_the_sheets_check_uses_the_seeing_model_for_the_check_only(env, eye, monkeypatch):
    env.key()
    made = []
    real = art.Illustrator.__init__

    def spy(self, *a, **kw):
        made.append(kw)
        real(self, *a, **kw)
    monkeypatch.setattr(art.Illustrator, "__init__", spy)
    body = {"kind": "event", "target": "e5", "style": "ink", "prompt": "A woman waits at a mill gate at dusk.", "check": True}
    assert env.c.post(f"/api/library/{KEY}/art/draw", json=body).status_code == 200
    assert made[-1]["checker"] is eye and made[-1]["cheap_text"] is env.text and made[-1]["check"] is True
    assert len(eye.looked) == 1 and eye.prompts == []                    # the seeing model made the check, nothing else


def test_a_picture_with_no_inherits_in_a_later_book_waits_for_every_earlier_one(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None, tier="cheap": Seeing())
    r = c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e6", "style": "ink", "prompt": "A gate."})
    assert r.status_code == 200, r.text
    pid, h = r.json()["id"], r.json()["url"].split("/")[3]
    path = c.data_dir / "art" / h / "ink" / "art.json"

    def set_inherits(value):
        m = json.loads(path.read_text())
        for p in m["pictures"]:
            if p["id"] == pid:
                p.pop("inherits", None)
                if value is not None:
                    p["inherits"] = value
        path.write_text(json.dumps(m))
    set_inherits(None)                                  # drawn before `inherits` existed
    for route, body in (("check", {}), ("fix", {"faults": [HEAD]})):
        r = c.post(f"/api/library/{KEY}/art/{pid}/{route}", json={**body, "finished": []})
        assert r.status_code == 422 and r.json()["code"] == "not-visible", route
    assert c.post(f"/api/library/{KEY}/art/{pid}/check", json={"finished": ["one"]}).status_code == 200
    assert c.post(f"/api/library/{KEY}/art/{pid}/check", json={}).status_code == 200      # no `finished`: every book read
    set_inherits([])                                    # carries nothing from book one: open to every reader
    assert c.post(f"/api/library/{KEY}/art/{pid}/check", json={"finished": []}).status_code == 200


def test_the_new_routes_need_the_csrf_header(env):
    bare = TestClient(env.c.app)
    for route in ("check", "fix"):
        assert bare.post(f"/api/library/{KEY}/art/p00000000/{route}").status_code == 403, route


# ---- a critical check, a fix that is verified before it is called fixed, the picture it replaced


def test_the_vision_model_takes_the_strong_tier_when_asked_and_falls_back_to_cheap(env, monkeypatch):
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: ["groq", "gemini"])
    monkeypatch.setattr(jobs_mod.JobManager, "cloud_model", lambda self, p, m=None: (p, m))
    assert env.jobs.vision_model(tier="strong") == ("gemini", llm.PROVIDERS["gemini"]["strong"])
    assert env.jobs.vision_model() == ("gemini", llm.PROVIDERS["gemini"]["cheap"])
    real = llm.vision
    monkeypatch.setattr(llm, "vision", lambda p, m: False if m == llm.PROVIDERS["gemini"]["strong"] else real(p, m))
    assert env.jobs.vision_model(tier="strong") == ("gemini", llm.PROVIDERS["gemini"]["cheap"])   # no strong one sees
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: ["groq"])
    assert env.jobs.vision_model(tier="strong") is None


def test_the_fix_and_the_check_judge_with_the_strong_tier_unless_a_picker_is_sent(env, eye, monkeypatch):
    old = one_picture(env)
    given = []
    real = art.Illustrator.fix

    def fix(self, rec, faults, note="", bi=None, verify=True, checker=None):
        given.append((verify, checker))
        return real(self, rec, faults, note, bi=bi, verify=verify, checker=checker)
    monkeypatch.setattr(art.Illustrator, "fix", fix)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]})
    assert r.status_code == 200, r.text
    assert eye.chosen[-1] == (None, "strong") and given[-1] == (True, eye)
    monkeypatch.setattr(jobs_mod.JobManager, "configured", lambda self: ["claude"])
    env.jobs.text_models = lambda p: ["claude-haiku-5-5"] if p == "claude" else []
    new = r.json()
    r = env.c.post(f"/api/library/{KEY}/art/{new['id']}/fix",
                   json={"faults": [HEAD], "picker": {"provider": "claude", "model": "claude-haiku-5-5"}})
    assert r.status_code == 200, r.text
    assert eye.chosen[-1][0] == {"provider": "claude", "model": "claude-haiku-5-5"}


def test_without_a_model_that_sees_pictures_the_fix_is_filed_unverified(env, monkeypatch):
    old = one_picture(env)
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None, tier="cheap": None)
    given = []
    real = art.Illustrator.fix

    def fix(self, rec, faults, note="", bi=None, verify=True, checker=None):
        given.append(verify)
        return real(self, rec, faults, note, bi=bi, verify=verify, checker=checker)
    monkeypatch.setattr(art.Illustrator, "fix", fix)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]})
    assert r.status_code == 200, r.text
    assert given == [False] and r.json()["verified"] is None


@pytest.mark.parametrize("sev", ["minor", "major", None])
def test_a_fault_may_carry_its_severity(env, sev, monkeypatch):
    old = one_picture(env)
    seen = []
    real = art.Illustrator.fix

    def fix(self, rec, faults, note="", bi=None, verify=True, checker=None):
        seen.append(faults)
        return real(self, rec, faults, note, bi=bi, verify=verify, checker=checker)
    monkeypatch.setattr(art.Illustrator, "fix", fix)
    f = {**HEAD, **({"severity": sev} if sev else {})}
    assert env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [f]}).status_code == 200
    assert seen[-1] == [{**HEAD, "severity": sev or "major"}]          # missing: fixed in full, as a major one


@pytest.mark.parametrize("sev", ["huge", "", 1, ["minor"]])
def test_an_unknown_severity_is_a_bad_fix(env, sev):
    old = one_picture(env)
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [{**HEAD, "severity": sev}]})
    assert r.status_code == 422 and r.json()["code"] == "bad-fix" and env.image.calls == []


def test_the_check_answers_each_faults_severity(env, eye):
    old = one_picture(env)
    eye.faults = [{**HEAD, "severity": "minor"}, {"what": "a sword passes through the hand", "where": "the man"}]
    got = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={}).json()["faults"]
    assert [f["severity"] for f in got] == ["minor", "major"]
    assert all(f["severity"] in ("major", "minor") for f in got) and got[1]["severity"] == "major"


def journal(env):
    return sorted(p.parent.name for p in (env.data / "merges").glob("*/entry.json"))


def test_a_fix_that_changed_nothing_is_422_unchanged_and_files_nothing(env, eye, monkeypatch):
    old = one_picture(env)
    before, entries = pictures(env), journal(env)
    verdict = art.Verdict(results=[{"n": 1, "fixed": "no"}], changed=False, new=[], checked=True, cost=0.002)

    def fix(self, rec, faults, note="", bi=None, verify=True, checker=None):
        raise art.FixUnchanged(verdict, 0.042)
    monkeypatch.setattr(art.Illustrator, "fix", fix)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD], "note": "the hand"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "unchanged" and body["detail"].startswith("The model sent the picture back without the fix")
    assert body["verified"] == {"fixed": 0, "partly": 0, "of": 1, "results": [{"n": 1, "fixed": "no"}], "new": []}
    assert body["fixCost"] == pytest.approx(0.042)
    assert pictures(env) == before and journal(env) == entries


def test_an_image_model_that_echoes_the_picture_is_422_unchanged(env, eye):
    old = one_picture(env)
    before, entries = pictures(env), journal(env)
    calls = []

    def echo(prompt, *, shape="landscape", quality="standard", references=(), **kw):
        calls.append(prompt)
        data, mime = references[0]
        return art.images.Drawn(data, mime, "fake-image", 0.03)
    env.image.draw = echo
    n = len(eye.looked)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]})
    assert r.status_code == 422 and r.json()["code"] == "unchanged", r.text
    assert calls and len(eye.looked) == n                         # identical bytes: judged by code, no call
    assert r.json()["verified"]["fixed"] == 0 and r.json()["fixCost"] == pytest.approx(0.03)
    assert pictures(env) == before and journal(env) == entries


def scripted_verdict(monkeypatch, verified):
    """`Illustrator.fix` as the pipeline files a verified fix: the real edit, then `verified` on its fix entry."""
    real = art.Illustrator.fix

    def fix(self, rec, faults, note="", bi=None, verify=True, checker=None):
        new = real(self, rec, faults, note, bi=bi, verify=False, checker=None)
        new["fixes"][-1]["verified"] = verified
        m = self.man[len(self.books) - 1]
        for r_ in m["pictures"]:
            if r_["id"] == new["id"]:
                r_["fixes"] = new["fixes"]
        self._save(len(self.books) - 1)
        return new
    monkeypatch.setattr(art.Illustrator, "fix", fix)


def test_a_partial_fix_is_filed_with_its_verdict_and_the_picture_it_replaced(env, eye, monkeypatch):
    old = one_picture(env)
    old_bytes = env.c.get(old["url"]).content
    v = {"fixed": 1, "partly": 1, "of": 3, "results": [{"n": 1, "fixed": "yes"}, {"n": 2, "fixed": "partly"},
                                                       {"n": 3, "fixed": "no"}],
         "new": [{"what": "a third hand", "where": "the man"}]}
    scripted_verdict(monkeypatch, v)
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD] * 3})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["verified"] == {**v, "new": [{"what": "a third hand", "where": "the man", "severity": "major", "kind": "drawing"}]}
    assert new["beforeUrl"] == f"/api/library/{KEY}/art/{new['id']}/before"
    got = env.c.get(new["beforeUrl"])
    assert got.status_code == 200 and got.content == old_bytes and got.headers["cache-control"] == "no-cache"
    assert got.headers["content-type"] == "image/png"
    assert env.c.get(new["url"]).content != old_bytes
    assert next(p for p in pictures(env) if p["id"] == new["id"])["fixes"][-1]["verified"] == v


def test_the_picture_before_a_fix_is_gated_like_the_picture_and_gone_after_undo(env, eye):
    old = one_picture(env)
    assert env.c.get(f"/api/library/{KEY}/art/{old['id']}/before").json()["code"] == "no-before"   # never fixed
    new = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]}).json()
    url = new["beforeUrl"]
    first, last = env.doc["anchors"][0]["id"], env.doc["anchors"][-1]["id"]
    r = env.c.get(url, params={"p": first})
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    assert env.c.get(url, params={"p": last}).status_code == 200
    assert env.c.get(url, params={"p": "1"}).status_code == 200                  # the fraction of the book read
    r = env.c.get(url, params={"finished": "nope"})
    assert r.status_code == 422 and r.json()["code"] == "bad-position"
    assert env.c.get(f"/api/library/{KEY}/art/p00000000/before").status_code == 404
    undo(env, new["undo"])
    r = env.c.get(url)
    assert r.status_code == 404                                                   # the fix is undone: no such picture
    again = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]}).json()
    assert env.c.get(again["beforeUrl"]).status_code == 200
    # a redraw files its picture the same way, so its earlier picture is there too
    fixed = env.c.get(again["url"]).content
    red = env.c.post(f"/api/library/{KEY}/art/{again['id']}/redraw", json={}).json()
    assert env.c.get(f"/api/library/{KEY}/art/{red['id']}/before").content == fixed


def test_the_picture_before_waits_for_the_earlier_books_its_picture_waits_for(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(jobs_mod.JobManager, "vision_model", lambda self, choice=None, tier="cheap": None)
    r = c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e6", "style": "ink", "prompt": "A gate."})
    pid = r.json()["id"]
    h = r.json()["url"].split("/")[3]
    path = c.data_dir / "art" / h / "ink" / "art.json"
    m = json.loads(path.read_text())
    for p in m["pictures"]:
        if p["id"] == pid:
            p["inherits"] = ["one"]
    path.write_text(json.dumps(m))
    new = c.post(f"/api/library/{KEY}/art/{pid}/fix", json={"faults": [HEAD], "finished": ["one"]}).json()
    assert c.get(new["beforeUrl"], params={"finished": "one"}).status_code == 200
    r = c.get(new["beforeUrl"], params={"finished": ""})
    assert r.status_code == 422 and r.json()["code"] == "not-visible"


def test_the_estimate_prices_the_verification_of_each_fix(env, monkeypatch):
    env.key()
    q = {"key": KEY, "style": "ink", "level": "main"}
    plain = env.c.get("/api/art/estimate", params=q).json()
    monkeypatch.setattr(jobs_mod.JobManager, "text_choice", lambda self, tier="cheap", jp=None: ("claude", "claude-haiku-5-5"))
    got = env.c.get("/api/art/estimate", params={**q, "check": "true"}).json()
    price = llm.model_price("claude", "claude-haiku-5-5")
    verify = (art.VERIFY_TOKENS[0] * price[0] + art.VERIFY_TOKENS[1] * price[1]) / 1e6
    assert got["check"]["verify"] == pytest.approx(verify, abs=1e-6)
    n = plain["plates"] + plain["portraits"]
    no_verify = n * got["check"]["each"] + art.FIX_RATE * (
        plain["plates"] * llm.image_price("gemini", GEMINI, "standard", "landscape")
        + plain["portraits"] * llm.image_price("gemini", GEMINI, "standard", "portrait"))
    assert got["check"]["price"] == pytest.approx(no_verify + n * art.FIX_RATE * verify, abs=1e-4)


def test_the_before_route_is_behind_sign_in(tmp_path):
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",))
    with TestClient(app) as c:
        assert c.get(f"/api/library/{KEY}/art/p00000000/before").status_code == 401


def test_the_verdict_numbers_the_faults_from_one_end_to_end(env, eye):
    """`results[].n` is the fault's place in the list sent, counted from 1 (the Reader's `cleanVerified` drops 0): the real
    verification, through the route, as filed and as answered."""
    old = one_picture(env)
    eye.verdict = {"results": [{"n": 2, "fixed": "yes"}, {"n": 1, "fixed": "partly"}], "changed": True, "new": []}
    two = [HEAD, {"what": "a sword passes through the hand", "where": "the man", "severity": "minor"}]
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": two})
    assert r.status_code == 200, r.text
    v = r.json()["verified"]
    assert v["of"] == 2 and v["fixed"] == 1 and v["partly"] == 1
    assert v["results"] == [{"n": 1, "fixed": "partly"}, {"n": 2, "fixed": "yes"}]
    assert next(p for p in pictures(env) if p["id"] == r.json()["id"])["fixes"][-1]["verified"]["results"] == v["results"]
    eye.verdict = {"results": [{"n": 1, "fixed": "no"}, {"n": 2, "fixed": "no"}], "changed": True, "new": []}
    r = env.c.post(f"/api/library/{KEY}/art/{r.json()['id']}/fix", json={"faults": two})
    assert r.status_code == 422 and r.json()["code"] == "unchanged"
    assert r.json()["verified"]["results"] == [{"n": 1, "fixed": "no"}, {"n": 2, "fixed": "no"}]


def test_a_position_that_is_an_anchor_id_is_read_as_that_anchor_before_a_fraction(env, eye):
    """An anchor id may look like a number ("1"): `?p=1` at that anchor is that anchor, never the end of the book."""
    env.index.write_text(env.index.read_text().replace('"a1"', '"1"'))
    assert any(a["id"] == "1" for a in json.loads(env.index.read_text())["anchors"])
    old = one_picture(env)
    new = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]}).json()
    r = env.c.get(new["beforeUrl"], params={"p": "1"})
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    assert env.c.get(new["beforeUrl"], params={"p": "1.0"}).status_code == 200      # not an id: the whole book read


def test_the_picture_before_is_only_ever_a_picture_file_inside_its_snapshot(env, eye, tmp_path):
    old = one_picture(env)
    new = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [HEAD]}).json()
    snap = env.data / "merges" / new["undo"]
    path = snap / "entry.json"
    entry = json.loads(path.read_text())
    good = entry["art"]["removed"][0]["file"]
    assert env.c.get(new["beforeUrl"]).status_code == 200

    def named(name):
        e = json.loads(path.read_text())
        e["art"]["removed"][0]["file"] = name
        path.write_text(json.dumps(e))
        return env.c.get(new["beforeUrl"])
    other = "p" + "0" * 8 + ".png"
    (snap / other).write_bytes(b"another picture")
    for bad in ("entry.json", "../entry.json", f"../{new['undo']}/{good}", other, good.rsplit(".", 1)[0] + ".svg", ""):
        r = named(bad)
        assert r.status_code == 404 and r.json()["code"] == "no-before", bad
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"outside the snapshot")
    (snap / good).unlink()
    (snap / good).symlink_to(outside)
    r = named(good)
    assert r.status_code == 404 and r.json()["code"] == "no-before"                # a link out of the snapshot


@pytest.mark.parametrize("kind", ["text", "style", "drawing", None])
def test_a_fault_may_carry_its_kind_and_it_is_passed_on_as_sent(env, kind, monkeypatch):
    old = one_picture(env)
    seen = []
    real = art.Illustrator.fix

    def fix(self, rec, faults, note="", bi=None, verify=True, checker=None):
        seen.append(faults)
        return real(self, rec, faults, note, bi=bi, verify=verify, checker=checker)
    monkeypatch.setattr(art.Illustrator, "fix", fix)
    f = {**HEAD, **({"kind": kind} if kind else {})}
    assert env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [f]}).status_code == 200
    assert seen[-1] == [{**HEAD, "severity": "major", **({"kind": kind} if kind else {})}]


@pytest.mark.parametrize("kind", ["photo", "", 1, ["text"]])
def test_an_unknown_kind_is_a_bad_fix(env, kind):
    old = one_picture(env)
    env.image.calls.clear()
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/fix", json={"faults": [{**HEAD, "kind": kind}]})
    assert r.status_code == 422 and r.json()["code"] == "bad-fix" and env.image.calls == []


def test_the_check_answers_each_faults_kind(env, eye, monkeypatch):
    old = one_picture(env)
    faults = [{**HEAD, "kind": "text"}, {**HEAD, "kind": "style", "severity": "major"}, {**HEAD, "kind": "bogus"}, HEAD]
    # what the pipeline's check returns is its own business (tests/test_art.py); the route answers what it is given
    monkeypatch.setattr(art.Illustrator, "check_picture", lambda self, *a, **k: art.Check(faults, True, 0.001, ""))
    got = env.c.post(f"/api/library/{KEY}/art/{old['id']}/check", json={}).json()["faults"]
    assert [f["kind"] for f in got] == ["text", "style", "drawing", "drawing"]


def test_the_check_sends_the_sets_style_sentence_inside_style_and_none_without_one(env, eye, monkeypatch):
    old = one_picture(env)
    eye.faults = []
    url = f"/api/library/{KEY}/art/{old['id']}/check"
    assert env.c.post(url, json={}).status_code == 200
    prompt = eye.looked[-1][0]
    assert f"<style>{art.style_sentence('ink')}</style>" in prompt          # the ink set's own sentence
    monkeypatch.setattr(art.Illustrator, "_style", lambda self, bi: "")      # a set with no style sentence
    assert env.c.post(url, json={}).status_code == 200
    assert "<style>" not in eye.looked[-1][0]
