"""POST /api/library/{key}/ask and GET /api/ask: questions answered from the gated view, never the book."""

import json
import re
import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import gating, llm  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402

from .conftest import EXAMPLES  # noqa: E402

SCARLET = "examples--sherlock-holmes--study-in-scarlet"
SIGN = "examples--sherlock-holmes--sign-of-four"
H = {"X-Lorekeeper": "1"}


class Model:
    """Records every prompt; answers with the first entry tag it was shown."""
    name = "fake"

    def __init__(self):
        self.calls, self.gate, self.fail = [], None, None

    def complete(self, system, prompt, schema, prefix=None):
        self.calls.append((system, prompt))
        if self.gate:
            self.gate.wait(5)
        if self.fail:
            raise self.fail
        tag = re.search(r"\[(e:[^\]]+)\]", prompt)
        return {"answer": "The notes describe this entry.", "found": True, "cites": [tag.group(1)] if tag else []}


@pytest.fixture
def make(tmp_path, monkeypatch):
    for p in llm.PROVIDERS.values():
        monkeypatch.delenv(p["key_env"], raising=False)
    model = Model()
    monkeypatch.setattr(llm, "make", lambda *a, **kw: model)

    def build(auth=False, **kw):
        app = web_app.create_app(tmp_path / "data", [("examples", EXAMPLES.parent)], [],
                                 ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=auth, **kw)
        return TestClient(app, headers=H)
    return build, model, monkeypatch


@pytest.fixture
def ask(make):
    build, model, mp = make
    mp.setenv("ANTHROPIC_API_KEY", "test-key-123")
    with build() as c:
        c.model, c.mp = model, mp
        yield c


def doc(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


def strings(x, out=None):
    out = set() if out is None else out
    if isinstance(x, str):
        out.add(x)
    elif isinstance(x, dict):
        for v in x.values():
            strings(v, out)
    elif isinstance(x, list):
        for v in x:
            strings(v, out)
    return out


def test_status_names_the_cheap_model_of_the_first_cloud_provider(ask):
    r = ask.get("/api/ask").json()
    assert r["available"] and r["provider"] == "claude" and r["label"] == "Claude" and not r["local"]
    assert r["model"] == llm.PROVIDERS["claude"]["cheap"] and r["reason"] is None


def test_status_without_a_model(make):
    build, _, _ = make
    with build() as c:
        r = c.get("/api/ask").json()
    assert r == {"available": False, "provider": None, "label": None, "model": None, "modelLabel": None,
                 "local": False, "reason": "no-model"}


def test_answer_at_an_early_anchor_and_the_prompt_holds_nothing_from_later(ask):
    sign = doc("sign-of-four")
    early = sign["anchors"][3]["id"]
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Sherlock Holmes?", "anchor": early})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["found"] and body["cites"] and body["provider"] == "claude" and not body["local"]
    assert set(body) == {"answer", "found", "cites", "note", "provider", "model", "local", "usage"}
    series = json.loads((EXAMPLES / "series.json").read_text())
    ix = [doc("study-in-scarlet"), sign]
    late = gating.view(ix, gating.Position("sign-of-four", sign["anchors"][-1]["id"]), series)
    soon = gating.view(ix, gating.Position("sign-of-four", early), series)
    only_late = {s for s in strings(late) - strings(soon) if len(s) >= 26}
    assert only_late
    prompt = "\n".join(p for _, p in ask.model.calls)
    assert prompt and not [s for s in only_late if s in prompt]


def test_finished_and_order_are_honoured(ask):
    sign = doc("sign-of-four")
    a = sign["anchors"][3]["id"]
    # with the first book not finished, nothing of it is in the notes
    ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Sherlock Holmes?", "anchor": a, "finished": []})
    unfinished = ask.model.calls[-1][1]
    ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Sherlock Holmes?", "anchor": a,
                                                 "finished": ["study-in-scarlet"], "order": "publication"})
    finished = ask.model.calls[-1][1]
    assert len(finished) > len(unfinished)


def test_history_is_cut_to_six_turns_and_fifteen_hundred_characters(ask):
    seen = {}
    import lorekeeper.ask as askmod
    real = askmod.answer

    def spy(view, question, llm_, history=(), *a, **kw):
        seen["h"] = list(history)
        return real(view, question, llm_, history, *a, **kw)
    ask.mp.setattr(askmod, "answer", spy)
    hist = [{"question": f"q{i}", "answer": "x" * 3000, "cites": ["e:E1"]} for i in range(9)]
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?", "anchor": None, "history": hist})
    assert r.status_code == 200
    assert [t.question for t in seen["h"]] == [f"q{i}" for i in range(3, 9)]
    assert all(len(t.answer) == 1500 for t in seen["h"])


def test_roster_is_for_a_cloud_model_only(ask):
    import lorekeeper.ask as askmod
    seen = {}
    real = askmod.answer

    def spy(view, question, llm_, history=(), budget=0, books=None, roster_chars=-1):
        seen["r"] = roster_chars
        return real(view, question, llm_, history, budget, books, roster_chars)
    ask.mp.setattr(askmod, "answer", spy)
    assert ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"}).status_code == 200
    assert seen["r"] == askmod.ROSTER_CHARS == 120_000


@pytest.mark.parametrize("body,status,code", [
    ({"question": ""}, 422, "bad-question"),
    ({"question": "   "}, 422, "bad-question"),
    ({"question": "x" * 501}, 422, "bad-question"),
    ({"question": "Who?", "anchor": "nope"}, 422, "bad-position"),
    ({"question": "Who?", "anchor": 5}, 422, "bad-position"),
    ({"question": "Who?", "order": "nope"}, 422, "bad-position"),
    ({"question": "Who?", "finished": "all"}, 422, "bad-position"),
])
def test_bad_requests(ask, body, status, code):
    r = ask.post(f"/api/library/{SIGN}/ask", json=body)
    assert r.status_code == status and r.json()["code"] == code and r.json()["detail"]
    assert not ask.model.calls


def test_unknown_key_is_404(ask):
    r = ask.post("/api/library/nothing/ask", json={"question": "Who?"})
    assert r.status_code == 404 and r.json()["code"] == "not-found"


def test_a_series_manifest_is_not_an_index(ask):
    r = ask.post("/api/library/examples--sherlock-holmes--series/ask", json={"question": "Who?"})
    assert r.status_code == 404


def test_csrf_header_is_required(ask):
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who?"}, headers={"X-Lorekeeper": "0"})
    assert r.status_code == 403


def test_no_model(make):
    build, model, _ = make
    with build() as c:
        r = c.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
    assert r.status_code == 409 and r.json()["code"] == "no-model" and not model.calls


def test_ollama_busy(make):
    build, model, mp = make

    class Tags:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"models": [{"name": llm.DEFAULT_MODEL}]}).encode()
    mp.setattr(web_app.urllib.request, "urlopen", lambda *a, **kw: Tags())
    with build() as c:
        assert c.get("/api/ask").json()["local"] is True
        ok = c.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
        assert ok.status_code == 200 and ok.json()["local"] and ok.json()["provider"] == "ollama"
        import lorekeeper.ask as askmod
        seen = {}
        real = askmod.answer
        mp.setattr(askmod, "answer", lambda *a, **kw: (seen.update(r=a[6] if len(a) > 6 else kw.get("roster_chars")), real(*a, **kw))[1])
        assert c.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"}).status_code == 200
        assert seen["r"] == 0                                  # Ollama's context cannot hold a roster
        mp.setattr(askmod, "answer", real)
        job = type("J", (), {"status": "running", "config": {"provider": "ollama"}})()
        c.app.state.jobs.jobs["busy"] = job
        r = c.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
        assert r.status_code == 409 and r.json()["code"] == "ollama-busy"
        assert c.get("/api/ask").json()["reason"] == "ollama-busy"
        del c.app.state.jobs.jobs["busy"]


def test_provider_failure_is_502_without_a_raw_body(ask):
    ask.model.fail = llm.ProviderError("rate limited", 429)
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
    assert r.status_code == 502 and r.json() == {"detail": "rate limited", "code": "model-failed"}
    ask.model.fail = RuntimeError("secret test-key-123 in a body")
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
    assert r.status_code == 502 and "test-key-123" not in r.text


def test_a_third_question_in_flight_is_refused(ask):
    ask.model.gate = threading.Event()
    out = []

    def go():
        out.append(ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"}).status_code)
    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for _ in range(100):
        if len(ask.model.calls) >= 2:
            break
        threading.Event().wait(0.05)
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
    ask.model.gate.set()
    for t in ts:
        t.join()
    assert r.status_code == 429 and r.json()["code"] == "busy"
    assert out == [200, 200]


def test_nothing_is_stored_or_logged(ask, tmp_path, caplog):
    ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes? zebra-marker"})
    assert "zebra-marker" not in caplog.text
    for p in (tmp_path / "data").rglob("*"):
        if p.is_file():
            assert b"zebra-marker" not in p.read_bytes()


def test_sign_in_is_required(make):
    build, model, mp = make
    mp.setenv("ANTHROPIC_API_KEY", "test-key-123")
    with build(auth=True, initial_password="correct horse battery") as c:
        assert c.get("/api/ask").status_code == 401
        r = c.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
        assert r.status_code == 401 and not model.calls


def test_book_must_be_the_one_the_index_belongs_to(ask):
    a = doc("sign-of-four")["anchors"][3]["id"]
    ok = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?", "anchor": a, "book": "sign-of-four"})
    assert ok.status_code == 200
    ok = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?", "anchor": a, "book": None})
    assert ok.status_code == 200
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?", "anchor": a, "book": "study-in-scarlet"})
    assert r.status_code == 422 and r.json()["code"] == "bad-position"
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?", "book": 3})
    assert r.status_code == 422


def test_the_backend_gets_a_two_minute_timeout(ask):
    ask.model.timeout = 1800
    ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
    assert ask.model.timeout == 120


def test_a_client_that_goes_away_does_not_free_the_slot_early(ask):
    import asyncio

    ask.model.gate = threading.Event()
    route = next(r for r in ask.app.routes if getattr(r, "path", "") == "/api/library/{key}/ask")

    class Req:
        async def json(self):
            return {"question": "Who is Holmes?"}

    async def cancelled():
        t = asyncio.ensure_future(route.endpoint(SIGN, Req()))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if ask.model.calls:
                break
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    loop = asyncio.new_event_loop()   # not asyncio.run: that would wait for the threads on exit
    for _ in range(2):
        loop.run_until_complete(cancelled())
        ask.model.calls.clear()
    # two cancelled asks still run in their threads: a third is refused until they end
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"})
    ask.model.gate.set()
    assert r.status_code == 429
    for _ in range(100):   # once the threads end, the slots come back
        if ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"}).status_code == 200:
            break
        threading.Event().wait(0.05)
    else:
        raise AssertionError("slots were never freed")
    loop.close()
