"""Web API end to end with a scripted model: upload, run, events, stop/resume, library, editions."""

import json
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import llm  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402

from .test_generate import FakeLLM, make_epub  # noqa: E402


class SlowFake(FakeLLM):
    """Scripted model that can be held mid-run, to test stopping between chunks."""
    gate = None

    def complete(self, system, prompt, schema, prefix=None):
        if self.gate is not None:
            self.gate()
        return super().complete(system, prompt, schema, prefix)


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake = SlowFake()
    monkeypatch.setattr(llm, "Ollama", lambda **kw: fake)
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9",
                             allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        c.fake = fake
        yield c


def wait_for(client, job_id, statuses, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {job['status']}")


def upload(client, tmp_path):
    path = make_epub(tmp_path / "lighthouse.epub")
    r = client.post("/api/books", files={"file": ("lighthouse.epub", path.read_bytes(), "application/epub+zip")})
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_rejects_non_epub(client):
    r = client.post("/api/books", files={"file": ("x.epub", b"not a zip", "application/epub+zip")})
    assert r.status_code == 400


def test_upload_describes_book(client, tmp_path):
    book = upload(client, tmp_path)
    assert book["title"] == "The Lighthouse"
    assert [c["title"] for c in book["chapters"]] == ["Chapter One", "Chapter Two"]
    assert client.get("/api/books").json()[0]["id"] == book["id"]


def test_ollama_offline_is_reported_not_raised(client):
    status = client.get("/api/ollama").json()
    assert status["ok"] is False and "gemma4:12b-it-qat" in status["throughput"]


def test_full_run_events_library_and_edition(client, tmp_path):
    book = upload(client, tmp_path)
    job = client.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    done = wait_for(client, job["id"], {"done", "failed"})
    assert done["status"] == "done", done["error"]
    assert done["report"]["validation_errors"] == []

    with client.stream("GET", f"/api/jobs/{job['id']}/events") as r:
        events = []
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1]["type"] == "done":
                    break
    types = [e["type"] for e in events]
    assert types[:3] == ["queued", "running", "start"]
    assert {"chunk", "phase", "chunk_done", "recap", "log"} <= set(types)
    added = [a for e in events if e["type"] == "chunk_done" for a in e["added"]]
    assert any(a["kind"] == "entity" and a["name"] == "Alice Marlow" for a in added)

    lib = client.get("/api/library").json()
    entry = next(i for i in lib["indexes"] if i["source"] == "generated")
    assert entry["hasEdition"] is True
    doc = client.get(f"/api/library/{entry['key']}").json()
    edition = client.get(f"/api/editions/{doc['editions'][0]['textHash']}").json()
    assert len(edition["blockWords"]) == 9
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 200


def test_stop_between_chunks_then_resume(client, tmp_path):
    import threading
    book = upload(client, tmp_path)
    release = threading.Event()
    calls = {"n": 0}

    def gate():
        calls["n"] += 1
        if calls["n"] > 3:          # let chunk 1 (extract, rephrase, verify) through, hold chunk 2
            release.wait(5)
    client.fake.gate = gate
    job = client.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    deadline = time.time() + 5
    while calls["n"] <= 3 and time.time() < deadline:
        time.sleep(0.02)
    assert client.post(f"/api/jobs/{job['id']}/stop").json()["status"] == "stopping"
    release.set()
    stopped = wait_for(client, job["id"], {"stopped", "done", "failed"})
    # The chapter in flight finishes (checkpointed); the next chunk is never started.
    assert stopped["status"] in ("stopped", "done")
    if stopped["status"] == "stopped":
        client.fake.gate = None
        client.post(f"/api/jobs/{job['id']}/resume")
        assert wait_for(client, job["id"], {"done", "failed"})["status"] == "done"


def test_paths_never_come_from_requests(client):
    assert client.get("/api/library/..--..--etc--passwd").status_code == 404
    assert client.delete("/api/books/../../x").status_code in (404, 405)


def test_mutations_need_the_csrf_header(client, tmp_path):
    path = make_epub(tmp_path / "x.epub")
    r = client.post("/api/books", files={"file": ("x.epub", path.read_bytes())}, headers={"X-Lorekeeper": ""})
    assert r.status_code == 403
    assert client.get("/api/books").status_code == 200          # reads are unaffected


def test_foreign_host_is_rejected(client):
    assert client.get("/api/books", headers={"host": "evil.example"}).status_code == 403


class StubModels:
    good = "sk-ant-api03-" + "a" * 40

    def __init__(self, key):
        self.key = key

    def list(self, limit=1):
        import anthropic
        if self.key != self.good:
            raise anthropic.AuthenticationError("bad key", response=__import__("httpx").Response(
                401, request=__import__("httpx").Request("GET", "https://api.anthropic.com")), body=None)
        return []


@pytest.fixture
def stub_anthropic(monkeypatch):
    anthropic = pytest.importorskip("anthropic")

    class Stub:
        def __init__(self, api_key=None, **kw):
            self.models = StubModels(api_key)
    monkeypatch.setattr(anthropic, "Anthropic", Stub)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return StubModels.good


def test_anthropic_key_flow(client, tmp_path, stub_anthropic):
    import os, stat
    assert client.get("/api/claude?fresh=true").json()["source"] is None
    r = client.put("/api/providers/anthropic", json={"api_key": "not-a-key"})
    assert r.status_code == 400
    r = client.put("/api/providers/anthropic", json={"api_key": "sk-ant-api03-" + "b" * 40})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    r = client.put("/api/providers/anthropic", json={"api_key": stub_anthropic})
    assert r.status_code == 200
    status = r.json()
    assert status["ok"] and status["source"] == "settings"
    assert stub_anthropic not in json.dumps(status)              # never echoed back in full
    assert status["masked"].startswith("sk-ant-") and status["masked"].endswith("aaaa")
    secret_file = tmp_path / "data" / "secrets.json"
    assert stat.S_IMODE(os.stat(secret_file).st_mode) == 0o600
    assert client.delete("/api/providers/anthropic").json()["source"] is None


def test_claude_jobs_get_the_saved_key(client, tmp_path, stub_anthropic, monkeypatch):
    seen = {}
    real_make = llm.make
    monkeypatch.setattr(llm, "make", lambda provider, model=None, api_key=None, **kw:
                        seen.update(provider=provider, key=api_key) or client.fake)
    client.put("/api/providers/anthropic", json={"api_key": stub_anthropic})
    book = upload(client, tmp_path)
    job = client.post("/api/jobs", json={"book_id": book["id"],
                                         "config": {"provider": "claude", "chunk_words": 5000}}).json()
    wait_for(client, job["id"], {"done", "failed"})
    assert seen == {"provider": "claude", "key": stub_anthropic}
    assert stub_anthropic not in json.dumps(client.get(f"/api/jobs/{job['id']}").json())
    monkeypatch.setattr(llm, "make", real_make)


def test_runs_cut_off_by_a_restart_resume_automatically(tmp_path):
    from lorekeeper.web.jobs import BookStore, Job, JobManager
    from dataclasses import asdict
    root = tmp_path / "jobs"
    root.mkdir()
    for jid, status in [("aaa", "running"), ("bbb", "stopping"), ("ccc", "done")]:
        (root / f"{jid}.json").write_text(json.dumps(asdict(Job(id=jid, book_id="x", book_title="t", config={},
                                                                 output=str(tmp_path / f"{jid}.json"), status=status))))
    mgr = JobManager.__new__(JobManager)
    mgr.root, mgr.jobs, mgr.events, mgr._resume_on_start = root, {}, {}, []
    import threading
    mgr.lock = threading.Lock()
    mgr._load()
    assert mgr.jobs["aaa"].status == "queued" and mgr.jobs["aaa"].resume is True
    assert mgr._resume_on_start == ["aaa"]
    assert mgr.jobs["bbb"].status == "stopped"
    assert mgr.jobs["ccc"].status == "done"


def test_uploaded_books_show_their_epub_cover(client, tmp_path):
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    with_cover = make_epub(tmp_path / "c.epub", title="Covered", ch1=["A different first paragraph, long enough to count as text here.",
                                                                       "And a second paragraph so this book differs from the others."], cover=jpeg)
    b = client.post("/api/books", files={"file": ("c.epub", with_cover.read_bytes())}).json()
    plain = upload(client, tmp_path)
    books = {x["key"]: x for x in client.get("/api/catalog").json()["books"]}
    assert books[f"up:{b['id']}"]["cover"] == f"/api/catalog/cover/up:{b['id']}"
    assert books[f"up:{plain['id']}"]["cover"] is None
    r = client.get(f"/api/catalog/cover/up:{b['id']}")
    assert r.status_code == 200 and r.content == jpeg and r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/catalog/cover/up:{plain['id']}").status_code == 404
    assert client.get("/api/catalog/cover/up:..%2F..%2Fsecrets").status_code == 404


def test_duplicates_merge_undo_and_distinct(client, tmp_path):
    from .test_merge import _ent, _index
    ix = _index([_ent("dior", ["Dior"], "a1", facets=[("a1", "A thief.")]),
                 _ent("dior-lachance", ["Dior LaChance"], "a3", facets=[("a3", "Gives a full name.")]),
                 _ent("chloe", ["Chloe"]), _ent("chloe-sauvage", ["Chloe Sauvage"], "a2")])
    path = tmp_path / "data" / "indexes" / "b.lore.json"
    path.write_text(json.dumps(ix))
    key = "generated--b.lore"
    pairs = client.get(f"/api/library/{key}/duplicates").json()["pairs"]
    assert {(p["keep"], p["drop"]) for p in pairs} == {("dior", "dior-lachance"), ("chloe", "chloe-sauvage")}

    r = client.post(f"/api/library/{key}/merge", json={"pairs": [{"keep": "dior", "drop": "dior-lachance"}]}).json()
    assert r["merged"] == 1 and r["undo"]
    assert [e["id"] for e in json.loads(path.read_text())["entities"]] == ["dior", "chloe", "chloe-sauvage"]
    dup = client.get(f"/api/library/{key}/duplicates").json()
    assert dup["undo"]["id"] == r["undo"] and len(dup["pairs"]) == 1

    assert client.post(f"/api/library/{key}/merge/{r['undo']}/undo").status_code == 200
    assert len(json.loads(path.read_text())["entities"]) == 4

    assert client.post(f"/api/library/{key}/distinct", json={"a": "chloe", "b": "chloe-sauvage"}).json()["ok"]
    assert {(p["keep"], p["drop"]) for p in client.get(f"/api/library/{key}/duplicates").json()["pairs"]} == {("dior", "dior-lachance")}
    assert client.post(f"/api/library/{key}/merge", json={"pairs": []}).status_code == 400


def test_bulk_merge_follows_chains(client, tmp_path):
    from .test_merge import _ent, _index
    ix = _index([_ent("nico", ["Nicolette"], "a1"), _ent("nico-c", ["Nicolette Chastain"], "a2"), _ent("nico-v", ["Viscontessa Nicolette"], "a3")])
    path = tmp_path / "data" / "indexes" / "n.lore.json"
    path.write_text(json.dumps(ix))
    pairs = [{"keep": "nico", "drop": "nico-c"}, {"keep": "nico-c", "drop": "nico-v"}]
    assert client.post("/api/library/generated--n.lore/merge", json={"pairs": pairs}).json()["merged"] == 2
    assert [e["id"] for e in json.loads(path.read_text())["entities"]] == ["nico"]


def test_tidy_names_fixes_old_merges(client, tmp_path):
    from .test_merge import _ent, _index
    e = _ent("gabriel", ["Gabriel de León", "Killer (narrator)"], "a1", facets=[("a1", "Gabriel rides north.")])
    e["names"][1]["kind"] = "primary"            # what an older merge left behind
    path = tmp_path / "data" / "indexes" / "g.lore.json"
    path.write_text(json.dumps(_index([e])))
    r = client.post("/api/library/generated--g.lore/tidy-names").json()
    assert r["fixed"] == 1 and r["names"] == ["Gabriel de León"]
    names = json.loads(path.read_text())["entities"][0]["names"]
    assert [n["value"] for n in names if n["kind"] == "primary"] == ["Gabriel de León"]
    assert client.post("/api/library/generated--g.lore/tidy-names").json()["fixed"] == 0


def test_obvious_duplicates_merge_automatically(client, tmp_path, monkeypatch):
    """Sure by name and judged the same: merged without asking. A shared alias (maybe a hidden
    identity) or a pair the model calls different is left for review."""
    from lorekeeper.web.secrets import SecretStore

    from .test_merge import _ent, _index
    ix = _index([_ent("dior", ["Dior"], "a1"), _ent("dior-lachance", ["Dior LaChance"], "a3"),
                 _ent("chloe", ["Chloe"]), _ent("chloe-sauvage", ["Chloe Sauvage"], "a2"),
                 _ent("gabriel", ["Gabriel", "The Stranger"]), _ent("stranger", ["The Stranger"], "a2")])
    path = tmp_path / "data" / "indexes" / "b.lore.json"
    path.write_text(json.dumps(ix))
    SecretStore(tmp_path / "data" / "secrets.json").set("anthropic_api_key", "sk-ant-test-000000000000")

    class Judge:
        name = "judge"
        def complete(self, system, prompt, schema, prefix=None):
            blocks = prompt.split("\nP")[1:]
            return {"verdicts": [{"id": f"P{b.split(':')[0]}", "verdict": "different" if "Sauvage" in b else "same"} for b in blocks]}
    monkeypatch.setattr(llm, "make", lambda *a, **kw: Judge())
    key = "generated--b.lore"
    client.get(f"/api/library/{key}/duplicates")              # starts the check in the background
    for _ in range(100):
        dup = client.get(f"/api/library/{key}/duplicates").json()
        if not dup["checking"] and dup["undo"]:
            break
        time.sleep(0.05)
    ids = [e["id"] for e in json.loads(path.read_text())["entities"]]
    assert "dior-lachance" not in ids                          # fuller name + judged same: merged
    assert {"chloe-sauvage", "stranger"} <= set(ids)           # judged different / shared alias: left
    assert dup["undo"]["auto"] and dup["undo"]["keepNames"] == ["Dior"]
    assert {(p["keep"], p["drop"]) for p in dup["pairs"]} >= {("gabriel", "stranger")}


def test_checker_policy_proper_names_proposals_and_different_pairs(client, tmp_path, monkeypatch):
    """A shared proper-name alias merges on "same"; a shared descriptor does not. Pairs a model proposes
    from descriptions are judged too: merged when every name is proper, listed otherwise, and gone when
    judged different. Pairs judged different are not listed at all, and the merge records why."""
    from lorekeeper.web.secrets import SecretStore

    from .test_merge import _ent, _index
    f = lambda *t: [("a1", x) for x in t]   # noqa: E731
    ix = _index([
        _ent("edmund", ["Edmund Hale", "Ned"], "a1", facets=f("Heir to the Hale estate.")),
        _ent("nedley", ["Nedley Brook", "Ned"], "a2", facets=f("Heir to the Hale estate, called Ned.")),
        _ent("gabriel", ["Gabriel", "The Stranger"], "a1", facets=f("A knight.")),
        _ent("stranger", ["The Stranger"], "a2", facets=f("A knight in grey.")),
        _ent("aldous", ["Aldous Venn"], "a1", facets=f("Keeper of the lighthouse.")),
        _ent("aldo", ["Uncle Aldo"], "a2", facets=f("Keeper of the lighthouse, Mina's uncle.")),
        _ent("mara", ["Mara Quill"], "a1", facets=f("A weaver.")),
        _ent("weaver", ["the Weaver"], "a2", facets=f("A weaver.")),
        _ent("orrin", ["Orrin Pike"], "a1", facets=f("A smith.")),
        _ent("nell", ["Old Nell"], "a2", facets=f("A smith's wife.")),
        _ent("wren", ["Wren Ashby"], "a1", facets=f("A duke's daughter.")),
        _ent("worm", ["Worm"], "a2", facets=f("A scullery maid.")),
    ])
    path = tmp_path / "data" / "indexes" / "c.lore.json"
    path.write_text(json.dumps(ix))
    SecretStore(tmp_path / "data" / "secrets.json").set("anthropic_api_key", "sk-ant-test-000000000000")
    prompts = []

    class Judge:
        name = "judge"

        def complete(self, system, prompt, schema, prefix=None):
            if "pairs" in schema["properties"]:   # propose: the pairs the names can't show
                rows = [ln for ln in prompt.splitlines() if ln.startswith("E") and ":" in ln]
                num = lambda who: next(ln.split(":")[0] for ln in rows if who in ln)   # noqa: E731
                return {"pairs": [{"a": num(a), "b": num(b)} for a, b in
                                  (("Aldous Venn", "Uncle Aldo"), ("Mara Quill", "the Weaver"), ("Orrin Pike", "Old Nell"), ("Wren Ashby", "Worm"))]}
            prompts.append(prompt)
            out = []
            for block in prompt.split("\nP")[1:]:
                out.append({"id": f"P{block.split(':')[0]}", "because": "the entries agree",
                            "verdict": "different" if "Nell" in block else "same", "hidden": "Worm" in block})
            return {"verdicts": out}
    monkeypatch.setattr(llm, "make", lambda *a, **kw: Judge())
    key = "generated--c.lore"
    client.get(f"/api/library/{key}/duplicates")
    for _ in range(100):
        dup = client.get(f"/api/library/{key}/duplicates").json()
        if not dup["checking"] and dup["undo"]:
            break
        time.sleep(0.05)
    ids = {e["id"] for e in json.loads(path.read_text())["entities"]}
    assert "nedley" not in ids and "aldo" not in ids                  # proper-name alias, proper-name proposal: merged
    assert {"stranger", "weaver", "nell"} <= ids                      # descriptors stay; "different" stays apart
    left = {(p["keep"], p["drop"]) for p in dup["pairs"]}
    assert ("gabriel", "stranger") in left and ("mara", "weaver") in left   # review
    assert ("wren", "worm") in left and "worm" in ids                 # same, but the book hides it: review
    assert ("orrin", "nell") not in left                              # judged different: not listed
    assert len(prompts) >= 1 and "Heir to the Hale estate" in prompts[0]
    entry = json.loads(next((tmp_path / "data" / "merges").glob("*/entry.json")).read_text())
    assert entry["auto"] and set(entry["because"]) == {"edmund|nedley", "aldous|aldo"}


def test_a_name_that_fits_two_people_is_not_merged_even_if_each_pair_is_judged_the_same(client, tmp_path, monkeypatch):
    from lorekeeper.web.secrets import SecretStore

    from .test_merge import _ent, _index
    ix = _index([_ent("chloe", ["Chloe"], "a1", facets=[("a1", "A novice.")]),
                 _ent("sauvage", ["Chloe Sauvage"], "a2", facets=[("a2", "A novice.")]),
                 _ent("brun", ["Chloe Brun"], "a2", facets=[("a2", "A novice.")])])
    path = tmp_path / "data" / "indexes" / "d.lore.json"
    path.write_text(json.dumps(ix))
    SecretStore(tmp_path / "data" / "secrets.json").set("anthropic_api_key", "sk-ant-test-000000000000")

    class Judge:
        name = "judge"

        def complete(self, system, prompt, schema, prefix=None):
            if "pairs" in schema["properties"]:
                return {"pairs": []}
            return {"verdicts": [{"id": f"P{b.split(':')[0]}", "verdict": "same", "because": "x", "hidden": False}
                                 for b in prompt.split("\nP")[1:]]}
    monkeypatch.setattr(llm, "make", lambda *a, **kw: Judge())
    key = "generated--d.lore"
    client.get(f"/api/library/{key}/duplicates")
    for _ in range(100):
        dup = client.get(f"/api/library/{key}/duplicates").json()
        if not dup["checking"] and not dup["unjudged"]:
            break
        time.sleep(0.05)
    assert {e["id"] for e in json.loads(path.read_text())["entities"]} == {"chloe", "sauvage", "brun"}
    assert dup["undo"] is None and len(dup["pairs"]) == 2


def test_titled_single_word_forms_do_not_block_an_approved_merge(client, tmp_path, monkeypatch):
    """"Mr. Holmwood" and "Lord Godalming" are one-word forms once the title is stripped, so they never
    count as two different full names; judged the same (not hidden) through the shared "Arthur" they merge."""
    from lorekeeper.web.secrets import SecretStore

    from .test_merge import _ent, _index
    ix = _index([_ent("holmwood", ["Mr. Holmwood", "Arthur"], "a1", facets=[("a1", "Lucy's fiance.")]),
                 _ent("godalming", ["Lord Godalming", "Arthur", "Godalming"], "a2", facets=[("a2", "Lucy's fiance.")])])
    path = tmp_path / "data" / "indexes" / "e.lore.json"
    path.write_text(json.dumps(ix))
    SecretStore(tmp_path / "data" / "secrets.json").set("anthropic_api_key", "sk-ant-test-000000000000")

    class Judge:
        name = "judge"

        def complete(self, system, prompt, schema, prefix=None):
            if "pairs" in schema["properties"]:
                return {"pairs": []}
            return {"verdicts": [{"id": f"P{b.split(':')[0]}", "verdict": "same", "because": "x", "hidden": False}
                                 for b in prompt.split("\nP")[1:]]}
    monkeypatch.setattr(llm, "make", lambda *a, **kw: Judge())
    key = "generated--e.lore"
    client.get(f"/api/library/{key}/duplicates")
    for _ in range(100):
        dup = client.get(f"/api/library/{key}/duplicates").json()
        if not dup["checking"] and dup["undo"]:
            break
        time.sleep(0.05)
    assert [e["id"] for e in json.loads(path.read_text())["entities"]] == ["holmwood"]


def test_finished_runs_write_descriptions_when_a_claude_key_is_set(tmp_path, monkeypatch):
    from lorekeeper.web.secrets import SecretStore
    fake, asked = SlowFake(), []
    monkeypatch.setattr(llm, "Ollama", lambda **kw: fake)

    class Describer:
        usage = llm.Usage()
        cancel = None

        def __init__(self, **kw):
            asked.append(kw["name"])

        def complete(self, system, prompt, schema, prefix=None):
            return {"text": "A lighthouse keeper who tends the lamp."}
    monkeypatch.setattr(llm, "Claude", Describer)
    (tmp_path / "data").mkdir()
    SecretStore(tmp_path / "data" / "secrets.json").set("anthropic_api_key", "sk-test")
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",), auth=False)
    with TestClient(app, headers={"X-Lorekeeper": "1"}) as c:
        c.fake = fake
        book = upload(c, tmp_path)
        job = c.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
        done = wait_for(c, job["id"], {"done", "failed"})
    assert done["status"] == "done", done["error"]
    assert "portraits" in done["report"] and "failed" not in done["report"]["portraits"]
    assert asked == ["claude-haiku-5-5"]


def test_without_a_cloud_key_descriptions_are_skipped(client, tmp_path):
    book = upload(client, tmp_path)
    job = client.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    done = wait_for(client, job["id"], {"done", "failed"})
    assert done["report"]["portraits"] == {"skipped": "no cloud key"}


# ---- bring your own key: any provider --------------------------------------------------------

@pytest.fixture
def cloud(client, monkeypatch):
    """No provider keys in the environment, and /models answered by a stub: the good key is 'good-key-123'."""
    for p in llm.PROVIDERS.values():
        monkeypatch.delenv(p["key_env"], raising=False)
    for v in ("CUSTOM_BASE_URL", "CUSTOM_MODEL"):
        monkeypatch.delenv(v, raising=False)
    calls = []

    def fake_list(provider, key, base_url=None, timeout=8):
        calls.append((provider, base_url))
        if provider != "custom" and key != "good-key-123":
            raise llm.ProviderError(f"{llm.PROVIDERS[provider]['label']} rejected this API key.", 401)
        return ["live-model-1", llm.PROVIDERS[provider]["default"] or "x"]
    monkeypatch.setattr(llm, "list_models", fake_list)
    client.calls = calls
    return client


def by_id(client, fresh=False):
    r = client.get("/api/providers" + ("?fresh=true" if fresh else ""))
    assert r.status_code == 200
    return {p["id"]: p for p in r.json()["providers"]}


def test_providers_listing_shows_every_provider_and_env_keys_masked(cloud, monkeypatch):
    ps = by_id(cloud)
    assert list(ps) == list(llm.PROVIDERS)
    assert not any(p["configured"] for p in ps.values())
    assert ps["gemini"]["key_env"] == "GEMINI_API_KEY" and ps["gemini"]["key_url"]
    monkeypatch.setenv("GEMINI_API_KEY", "good-key-123")
    g = by_id(cloud, fresh=True)["gemini"]
    assert g["configured"] and g["source"] == "environment" and g["ok"]
    assert "good-key-123" not in json.dumps(g) and g["masked"]
    assert [m["id"] for m in g["liveModels"]] == ["live-model-1"]       # presets are not repeated
    assert g["default"] == "gemini-2.5-flash" and g["cheap"] and g["strong"]


def test_put_refuses_a_key_that_fails_the_test_and_never_saves_it(cloud, tmp_path):
    r = cloud.put("/api/providers/openai", json={"key": "wrong-key-12345"})
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    assert not by_id(cloud)["openai"]["configured"]
    r = cloud.put("/api/providers/openai", json={"key": "good-key-123"})
    assert r.status_code == 200
    st = r.json()
    assert st["configured"] and st["source"] == "settings" and st["ok"] and "good-key-123" not in json.dumps(st)
    assert json.loads((tmp_path / "data" / "secrets.json").read_text())["openai_api_key"] == "good-key-123"
    assert not by_id(cloud, fresh=True)["openai"]["error"]
    after = cloud.delete("/api/providers/openai").json()
    assert not after["configured"]


def test_custom_server_needs_a_url_and_a_model_but_no_key(cloud):
    assert cloud.put("/api/providers/custom", json={"model": "m"}).status_code == 400
    assert cloud.put("/api/providers/custom", json={"base_url": "http://localhost:1234/v1"}).status_code == 400
    r = cloud.put("/api/providers/custom", json={"base_url": "http://localhost:1234/v1/", "model": "local-1"})
    assert r.status_code == 200
    c = r.json()
    assert c["configured"] and c["base_url"] == "http://localhost:1234/v1" and c["model"] == "local-1"
    assert c["default"] == "local-1" and c["masked"] is None
    assert cloud.calls[-1] == ("custom", "http://localhost:1234/v1")


def test_anthropic_alias_is_the_claude_provider(client, tmp_path, stub_anthropic):
    r = client.put("/api/providers/anthropic", json={"api_key": stub_anthropic})
    assert r.status_code == 200 and r.json()["source"] == "settings"
    claude = {p["id"]: p for p in client.get("/api/providers").json()["providers"]}["claude"]
    assert claude["configured"] and claude["ok"] and claude["source"] == "settings"
    assert json.loads((tmp_path / "data" / "secrets.json").read_text())["anthropic_api_key"] == stub_anthropic
    assert client.put("/api/providers/claude", json={"key": "sk-ant-api03-" + "b" * 40}).status_code == 400
    assert client.put("/api/providers/nope", json={"key": "x" * 20}).status_code == 404


def test_job_with_provider_gemini_builds_an_openai_compatible_model(cloud, tmp_path, monkeypatch):
    seen = {}
    real_make = llm.make

    def spy(provider, model=None, api_key=None, base_url=None, **kw):
        m = real_make(provider, model, api_key=api_key, base_url=base_url, **kw)
        seen.update(provider=provider, model=model, key=api_key, kind=type(m).__name__)
        return cloud.fake
    monkeypatch.setattr(llm, "make", spy)
    book = upload(cloud, tmp_path)
    # no key yet: the run fails with a clear message instead of calling the API
    job = cloud.post("/api/jobs", json={"book_id": book["id"], "config": {"provider": "gemini", "chunk_words": 5000}}).json()
    failed = wait_for(cloud, job["id"], {"done", "failed"})
    assert failed["status"] == "failed" and "Add one in Settings" in failed["error"]
    assert cloud.post("/api/jobs", json={"book_id": book["id"], "config": {"provider": "nope"}}).status_code == 400

    cloud.put("/api/providers/gemini", json={"key": "good-key-123"})
    job = cloud.post("/api/jobs", json={"book_id": book["id"], "config": {
        "provider": "gemini", "model": "gemini-2.5-flash-lite", "chunk_words": 5000}}).json()
    done = wait_for(cloud, job["id"], {"done", "failed"})
    assert done["status"] == "done", done["error"]
    assert seen["kind"] == "OpenAICompatible" and seen["key"] == "good-key-123"
    events = (tmp_path / "data" / "jobs" / f"{job['id']}.events.jsonl").read_text()
    assert events and "good-key-123" not in json.dumps(cloud.get(f"/api/jobs/{job['id']}").json()) + events


def test_descriptions_use_the_cheap_model_of_a_configured_provider(cloud, tmp_path, monkeypatch):
    picked = []
    real_make = llm.make
    monkeypatch.setattr(llm, "make", lambda provider, model=None, **kw: picked.append((provider, model)) or cloud.fake)
    # no cloud provider: nothing
    book = upload(cloud, tmp_path)
    j = cloud.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    assert wait_for(cloud, j["id"], {"done", "failed"})["report"]["portraits"] == {"skipped": "no cloud key"}
    # only OpenAI configured, run on the local model: OpenAI's cheap model writes them
    cloud.put("/api/providers/openai", json={"key": "good-key-123"})
    picked.clear()
    j = cloud.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    wait_for(cloud, j["id"], {"done", "failed"})
    assert ("openai", llm.PROVIDERS["openai"]["cheap"]) in picked
    # a run on Gemini writes them with Gemini even though OpenAI comes first
    cloud.put("/api/providers/gemini", json={"key": "good-key-123"})
    picked.clear()
    j = cloud.post("/api/jobs", json={"book_id": book["id"], "config": {"provider": "gemini", "chunk_words": 5000}}).json()
    wait_for(cloud, j["id"], {"done", "failed"})
    assert picked[-1] == ("gemini", llm.PROVIDERS["gemini"]["cheap"])
    monkeypatch.setattr(llm, "make", real_make)


def test_checker_prefers_claude_then_the_first_configured_provider(tmp_path, monkeypatch):
    from lorekeeper.web.jobs import JobManager
    for p in llm.PROVIDERS.values():
        monkeypatch.delenv(p["key_env"], raising=False)
    have = {"openai": "k1", "gemini": "k2"}
    made = []
    monkeypatch.setattr(llm, "make", lambda provider, model=None, **kw: made.append((provider, model)) or object())
    from lorekeeper.web.jobs import BookStore
    mgr = JobManager(tmp_path / "jobs", BookStore(tmp_path / "b"), tmp_path / "i", "http://127.0.0.1:9", keys=have.get)
    assert mgr.configured() == ["openai", "gemini"]
    assert mgr.cheap_model("gemini") is not None and made[-1] == ("gemini", "gemini-2.5-flash")
    have["claude"] = "k0"
    mgr.cheap_model(None)
    assert made[-1] == ("claude", "claude-haiku-5-5")


def test_a_run_never_remerges_pairs_a_person_separated(client, tmp_path, monkeypatch):
    """Options.distinct is filled from the catalog's "not the same" pairs for the run's series."""
    from lorekeeper import generate
    seen = {}
    real_run = generate.run

    def spy(book, model, opts, *a, **kw):
        seen["distinct"] = set(opts.distinct)
        return real_run(book, model, opts, *a, **kw)
    monkeypatch.setattr(generate, "run", spy)
    state = tmp_path / "data" / "library.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"distinct": {"series:the-keepers": [["a", "b"]]}, "defaults": {}, "series": {}, "links": {}}))
    book = upload(client, tmp_path)
    job = client.post("/api/jobs", json={"book_id": book["id"], "config": {
        "chunk_words": 5000, "series_id": "the-keepers", "book_key": "one"}}).json()
    wait_for(client, job["id"], {"done", "failed"})
    assert seen["distinct"] == {frozenset(("a", "b"))}
    # a build outside any series reads the group of its own index file
    job = client.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    wait_for(client, job["id"], {"done", "failed"})
    assert seen["distinct"] == set()
