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


def test_a_client_that_leaves_before_the_job_starts_never_holds_a_slot(ask):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    route = next(r for r in ask.app.routes if getattr(r, "path", "") == "/api/library/{key}/ask")

    class Req:
        async def json(self):
            return {"question": "Who is Holmes?"}

    async def leave_while_queued():
        pool = ThreadPoolExecutor(1)           # one thread, kept busy: the ask job waits in the queue
        release = threading.Event()
        pool.submit(release.wait, 5)
        asyncio.get_running_loop().set_default_executor(pool)
        t = asyncio.ensure_future(route.endpoint(SIGN, Req()))
        await asyncio.sleep(0.2)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        release.set()
        pool.shutdown(wait=True)
    for _ in range(3):                          # more than the two slots
        asyncio.run(leave_while_queued())
    assert not ask.model.calls
    for _ in range(3):
        assert ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?"}).status_code == 200


# ---- source: the book's own text before the position (Index and book, Book only) --------------------------------

from types import SimpleNamespace  # noqa: E402

import lorekeeper.ask as askmod  # noqa: E402
from lorekeeper import epub  # noqa: E402

from .test_editor import edit  # noqa: E402
from .test_generate import make_epub  # noqa: E402

REAL_TEXT = hasattr(askmod, "BookText")   # the pipeline side of this feature; the end-to-end tests skip without it

# The frontier block holds FRONTIER, a later block LATER: neither may ever reach the model at that position.
FRONTIER, LATER, EARLY = "zanzibarfrontier", "quixoticlater", "marmaladeearly"
CH1 = [f"Alice Marlow arrived at the old lighthouse one grey morning and saw a {EARLY} lantern on the stair.",
       "She had come to find her brother, Tom Marlow, who had vanished from the village a year before.",
       "The keeper, a quiet man named Ezra Pike, said he had never heard of anyone called Tom at all."]
CH2 = [f"In the morning Alice Marlow found a {FRONTIER} key under the loose boards of the lamp room floor.",
       f"Pike admitted that Tom had stayed all winter and had left by boat with the {LATER} chart in hand."]


@pytest.fixture
def text_ask(make, tmp_path):
    """A hand-made index of a book whose EPUB is here, with Alice visible from the start; a cloud key."""
    build, model, mp = make
    mp.setenv("ANTHROPIC_API_KEY", "test-key-123")
    built = record_built(mp)
    with build() as c:
        path = make_epub(tmp_path / "light.epub", ch1=CH1, ch2=CH2)
        up = c.post("/api/books", files={"file": ("light.epub", path.read_bytes(), "application/epub+zip")}).json()
        key = c.post(f"/api/catalog/books/up:{up['id']}/handmade", json={"license": "CC-BY-4.0"}).json()["key"]
        edit(c, key, op="add-entity", name="Alice Marlow", type="person", seq=1, desc="A young woman who comes to a lighthouse.")
        c.model, c.mp, c.key, c.book, c.built = model, mp, key, epub.read(path), built
        yield c


def record_built(mp):
    """Every `ask.TextBook` the route builds, as the route passed it (before `BookText` cuts anything again), so the
    cut is proved in the web layer itself. One list of `SimpleNamespace`s per `BookText`, the latest last."""
    built, pending = [], []
    real_tb, real_bt = askmod.TextBook, askmod.BookText

    def text_book(**kw):
        pending.append(SimpleNamespace(**{**kw, "blocks": list(kw["blocks"]), "chapters": list(kw["chapters"])}))
        return real_tb(**kw)

    def book_text(books):
        built.append(pending[:])
        pending.clear()
        return real_bt(books)
    mp.setattr(askmod, "TextBook", text_book)
    mp.setattr(askmod, "BookText", book_text)
    return built


def frac_before(book, seq):
    """The Reader's `p` whose frontier block (finished mode) is `seq`."""
    total = sum(len(b.words) for b in book.blocks)
    return sum(len(b.words) for b in book.blocks[:seq]) / total


def seq_of(book, word):
    return next(b.seq for b in book.blocks if word in b.raw)


def spy_answer(mp):
    """Records what the route hands `ask.answer`, and answers without a model."""
    seen = []

    def spy(view, question, llm_, history=(), budget=0, books=None, roster_chars=0, **kw):
        seen.append({"history": list(history), **kw})
        return askmod.Answer("ok", True, [], None, 0, {}, source=kw.get("source", "index"))
    mp.setattr(askmod, "answer", spy)
    return seen


def assert_cut(tb, frontier):
    """The route itself cut the book being read at the frontier: `before` is it, and no block or chapter at or
    after it was passed at all."""
    assert tb.before == frontier
    assert [b.seq for b in tb.blocks] == list(range(frontier))
    assert all(c.start < frontier for c in tb.chapters)
    assert tb.numbers is None or len(tb.numbers) == len(tb.chapters)


def test_index_only_is_todays_call(text_ask):
    seen = spy_answer(text_ask.mp)
    for body in ({}, {"source": "index"}, {"source": "index", "p": 0.5}):
        r = text_ask.post(f"/api/library/{text_ask.key}/ask", json={"question": "Who is Alice Marlow?", **body})
        assert r.status_code == 200, r.text
        assert "source" not in r.json()
    assert all(s == {"history": []} for s in seen)       # no text, no source: the call is exactly today's


@pytest.mark.parametrize("source", ["both", "book"])
def test_only_blocks_before_the_frontier_are_passed(text_ask, source):
    seen = spy_answer(text_ask.mp)
    book, F = text_ask.book, seq_of(text_ask.book, FRONTIER)
    r = text_ask.post(f"/api/library/{text_ask.key}/ask",
                      json={"question": "What did Alice Marlow find?", "source": source, "p": frac_before(book, F)})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == source
    assert seen[-1]["source"] == source and seen[-1]["text"].books   # the BookText the route built
    [tb] = text_ask.built[-1]
    assert F > 0 and tb.book is None and tb.title is None
    assert_cut(tb, F)
    assert not [b for b in tb.blocks if FRONTIER in b.raw or LATER in b.raw]


def test_the_block_being_read_never_counts_and_the_whole_book_only_at_the_end(text_ask):
    seen = spy_answer(text_ask.mp)
    book, F = text_ask.book, seq_of(text_ask.book, FRONTIER)
    total = sum(len(b.words) for b in book.blocks)
    # one word into the frontier block: still not read
    for p, before in ((frac_before(book, F) + 1 / total, F), (0.0, 0), (0.9999, len(book.blocks) - 1),
                      (1, len(book.blocks)), (1.0, len(book.blocks))):
        r = text_ask.post(f"/api/library/{text_ask.key}/ask",
                          json={"question": "Who is Alice Marlow?", "source": "both", "p": p})
        assert r.status_code == 200 and r.json()["source"] == "both"
        [tb] = text_ask.built[-1]
        assert_cut(tb, before)


def test_passage_cites_in_history_are_never_handed_on(text_ask):
    seen = spy_answer(text_ask.mp)
    hist = [{"question": "What did she find?", "answer": "A chart.", "cites": ["p::7", "p:lights-one:3", "e:alice-marlow"]}]
    text_ask.post(f"/api/library/{text_ask.key}/ask",
                  json={"question": "Who is Alice Marlow?", "source": "both", "p": 0.2, "history": hist})
    assert seen[-1]["history"][0].cites == ["e:alice-marlow"]


@pytest.mark.parametrize("body,code", [
    ({"source": "all"}, "bad-source"),
    ({"source": None}, "bad-source"),
    ({"source": 1}, "bad-source"),
    ({"source": "both"}, "bad-position"),
    ({"source": "book", "p": None}, "bad-position"),
    ({"source": "book", "p": 1.5}, "bad-position"),
    ({"source": "book", "p": -0.1}, "bad-position"),
    ({"source": "book", "p": "0.5"}, "bad-position"),
    ({"source": "book", "p": True}, "bad-position"),
    ({"source": "index", "p": 2}, "bad-position"),
])
def test_bad_source_or_position(text_ask, body, code):
    r = text_ask.post(f"/api/library/{text_ask.key}/ask", json={"question": "Who is Alice?", **body})
    assert r.status_code == 422 and r.json()["code"] == code and r.json()["detail"]
    assert not text_ask.model.calls


def test_no_book_file_is_409_before_any_call(ask):
    # the bundled examples have no EPUB on disk here
    r = ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?", "source": "both", "p": 0.5})
    assert r.status_code == 409 and r.json() == {"code": "no-book-file", "detail": "The book file for this index is not here."}
    assert not ask.model.calls
    assert ask.post(f"/api/library/{SIGN}/ask", json={"question": "Who is Holmes?", "p": 0.5}).status_code == 200


def test_the_text_is_not_stored_or_logged(text_ask, tmp_path, caplog):
    before = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*") if p.is_file()}
    text_ask.post(f"/api/library/{text_ask.key}/ask", json={"question": "What lantern did Alice Marlow see?",
                                                           "source": "both", "p": 0.9})
    assert EARLY not in caplog.text
    after = {p: p.read_bytes() for p in (tmp_path / "data").rglob("*") if p.is_file()}
    assert not [p for p in after if p not in before or after[p] != before[p]]


# ---- a series: an earlier book's text only when finished and earlier in the order used ---------------------------

@pytest.fixture
def series_ask(make, tmp_path):
    build, model, mp = make
    mp.setenv("ANTHROPIC_API_KEY", "test-key-123")
    seen = spy_answer(mp)
    built = record_built(mp)
    with build() as c:
        keys, epubs = [], []
        for i, title in enumerate(["Lights One", "Lights Two", "Lights Three"], 1):
            meta = f'<meta name="calibre:series" content="Lights"/><meta name="calibre:series_index" content="{i}"/>'
            words = [f"Book {i} marker{i}word: Alice Marlow kept the harbour light alone for twenty winters and more."] * 3
            path = make_epub(tmp_path / f"b{i}.epub", title=title, series_meta=meta, ch1=words)
            epubs.append(epub.read(path))
            up = c.post("/api/books", files={"file": (f"b{i}.epub", path.read_bytes())}).json()
            r = c.post(f"/api/catalog/books/up:{up['id']}/handmade", json={"license": "CC-BY-4.0"})
            assert r.status_code == 200, r.text
            keys.append(r.json()["key"])
        docs = [c.get(f"/api/library/{k}").json() for k in keys]
        c.keys, c.books, c.seen, c.built, c.epubs = keys, [d["series"]["book"] for d in docs], seen, built, epubs
        yield c


def text_books(c, key, **body):
    r = c.post(f"/api/library/{key}/ask", json={"question": "Who is Alice Marlow?", "source": "both", **body})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "both"
    return {tb.book: tb for tb in c.built[-1]}


def frontier_at(book, p):
    """The first block not read at `p` (finished mode), worked out here rather than taken from the route."""
    total, done = sum(len(b.words) for b in book.blocks), 0
    for b in book.blocks:
        if done + len(b.words) > p * total:
            return b.seq
        done += len(b.words)
    return len(book.blocks)


def test_an_earlier_book_is_read_only_when_finished(series_ask):
    c = series_ask
    one, two, three = c.books
    mid = text_books(c, c.keys[1], p=0.5, finished=[one], book=two)
    assert list(mid) == [one, two]
    assert mid[one].before == len(mid[one].blocks) and mid[one].title == "Lights One"
    assert any("marker1word" in b.raw for b in mid[one].blocks)
    assert [b.seq for b in mid[one].blocks] == list(range(len(c.epubs[0].blocks)))   # the finished one is whole
    F = frontier_at(c.epubs[1], 0.5)
    assert 0 < F < len(c.epubs[1].blocks)
    assert_cut(mid[two], F)                                              # this book is cut at the frontier
    assert not [b for t in mid.values() for b in t.blocks if "marker3word" in b.raw]
    # not finished, finished = null, or a later book listed as finished: never sent
    assert list(text_books(c, c.keys[1], p=0.5, finished=[])) == [two]
    assert list(text_books(c, c.keys[1], p=0.5)) == [two]
    assert list(text_books(c, c.keys[1], p=0.5, finished=[one, three])) == [one, two]
    got = text_books(c, c.keys[2], p=0.5, finished=[one, two])
    assert list(got) == [one, two, three]
    assert_cut(got[three], frontier_at(c.epubs[2], 0.5))
    for k, i in ((one, 0), (two, 1)):
        assert got[k].before == len(c.epubs[i].blocks) == len(got[k].blocks)


def test_a_missing_earlier_book_file_is_skipped(series_ask, tmp_path):
    c = series_ask
    one, two, _ = c.books
    from lorekeeper import text as textmod
    want = c.get(f"/api/library/{c.keys[0]}").json()["editions"][0]["textHash"]
    [path] = [p for p in (tmp_path / "data").rglob("*.epub")
              if textmod.text_hash(b.words for b in epub.blocks(p)) == want]
    path.rename(path.with_suffix(".gone"))
    assert list(text_books(c, c.keys[1], p=0.5, finished=[one])) == [two]


# ---- end to end with the real ask.BookText: what reaches any prompt (pick calls included) ------------------------

def prompts(c):
    return "\n".join(s + "\n" + p for s, p in c.model.calls)


@pytest.mark.skipif(not REAL_TEXT, reason="needs ask.BookText (the pipeline side of add-ask-book-text)")
@pytest.mark.parametrize("source", ["both", "book"])
def test_no_text_at_or_after_the_frontier_reaches_a_prompt(text_ask, source):
    c, book = text_ask, text_ask.book
    F = seq_of(book, FRONTIER)
    url = f"/api/library/{c.key}/ask"
    # the first question asks for exactly what lies past the position
    for q in ("What did Alice Marlow find under the boards?", "What chart did Tom take?", "What happened next?"):
        r = c.post(url, json={"question": q, "source": source, "p": frac_before(book, F)})
        assert r.status_code == 200, r.text
    assert c.model.calls and EARLY in prompts(c)          # the text before the position does reach the model
    assert FRONTIER not in prompts(c) and LATER not in prompts(c)
    # a follow-up whose history cites the frontier and a later passage
    hist = [{"question": "What did she find?", "answer": f"A {FRONTIER} key and the {LATER} chart.",
             "cites": [f"p::{F}", f"p::{F + 1}", "p::999"]}]
    c.model.calls.clear()
    r = c.post(url, json={"question": "And then what did Alice Marlow do?", "source": source,
                          "p": frac_before(book, F), "history": hist})
    assert r.status_code == 200
    assert FRONTIER not in prompts(c) and LATER not in prompts(c)
    # moved back to the block holding EARLY: now that is out of reach too
    c.model.calls.clear()
    E = seq_of(book, EARLY)
    r = c.post(url, json={"question": "What lantern did Alice Marlow see on the stair?", "source": source,
                          "p": frac_before(book, E), "history": hist})
    assert r.status_code == 200
    assert not [w for w in (EARLY, FRONTIER, LATER) if w in prompts(c)]
    for cite in r.json()["cites"]:
        if cite.get("kind") == "passage":
            assert cite["end"] < E


@pytest.mark.skipif(not REAL_TEXT, reason="needs ask.BookText (the pipeline side of add-ask-book-text)")
def test_passage_cites_come_back_with_the_source(text_ask, monkeypatch):
    c, book = text_ask, text_ask.book

    def cite_first_passage(system, prompt, schema, prefix=None):
        c.model.calls.append((system, prompt))
        tag = re.search(r"\[(p:[^\]]+)\]", prompt)
        return {"answer": "The book says she saw a lantern on the stair.", "found": True,
                "cites": [tag.group(1)] if tag else []}
    monkeypatch.setattr(c.model, "complete", cite_first_passage)
    r = c.post(f"/api/library/{c.key}/ask", json={"question": "What did Alice Marlow see on the stair?",
                                                  "source": "book", "p": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "book"
    passages = [x for x in body["cites"] if x.get("kind") == "passage"]
    for x in passages:
        assert x["id"] == f"p::{x['seq']}" and x["book"] is None and x["seq"] <= x["end"] < len(book.blocks)
