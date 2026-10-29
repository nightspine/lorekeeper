"""The public /v0 protocol (SPEC.md §8): discovery, lookup, fetch with ETag, gated views, entity lookup."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

from lorekeeper import anchors as anchors_mod  # noqa: E402
from lorekeeper import epub, gating  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402

from .conftest import CORPUS, EXAMPLES, ROOT  # noqa: E402

SCHEMAS = ROOT / "schema"
SCARLET = "examples--sherlock-holmes--study-in-scarlet"
SIGN = "examples--sherlock-holmes--sign-of-four"
needs_locators = pytest.mark.skipif(
    not (ROOT / "src" / "lorekeeper" / "locators.py").exists(),
    reason="lorekeeper.locators has not landed yet; locator routes cannot be tested")


def _registry() -> Registry:
    reg = Registry()
    for p in SCHEMAS.glob("*.json"):
        doc = json.loads(p.read_text())
        reg = reg.with_resource(doc["$id"], Resource.from_contents(doc))
    return reg


def check_def(name: str, body) -> None:
    schema = {"$ref": f"https://lorekeeper.dev/schema/0.1/api.schema.json#/$defs/{name}"}
    Draft202012Validator(schema, registry=_registry()).validate(body)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("LOREKEEPER_API_TOKEN", raising=False)
    epubs = [CORPUS] if CORPUS.is_dir() else []
    app = web_app.create_app(tmp_path / "data", [("examples", EXAMPLES.parent)], epubs,
                             ollama_url="http://127.0.0.1:9", allowed_hosts=("testserver",))
    with TestClient(app) as c:
        yield c


def problem(r, status):
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    check_def("problem", body)
    assert body["status"] == status
    return body


def scarlet_doc():
    return json.loads((EXAMPLES / "study-in-scarlet.json").read_text())


def test_discovery(client):
    r = client.get("/.well-known/lorekeeper")
    assert r.status_code == 200
    body = r.json()
    check_def("discovery", body)
    assert body["api"] == "/v0" and body["auth"] == {"type": "none"}
    assert body["capabilities"] == ["lookup", "index", "series", "view", "view-cfi", "entities"]


def test_lookup_by_gutenberg_and_hashes(client):
    r = client.get("/v0/lookup", params={"gutenberg": "244"})
    assert r.status_code == 200
    body = r.json()
    check_def("lookup", body)
    hit = body["results"][0]
    assert hit["id"] == SCARLET and hit["match"] == "identifier" and hit["complete"] is False
    assert hit["series"] == {"id": "sherlock-holmes", "book": "study-in-scarlet"}
    assert hit["href"] == f"/v0/indexes/{SCARLET}"
    assert [x["id"] for x in body["results"]] == [SCARLET]

    ed = scarlet_doc()["editions"][0]
    by_file = client.get("/v0/lookup", params={"fileHash": ed["fileHash"], "gutenberg": "244"}).json()
    assert by_file["results"][0]["match"] == "file"
    by_text = client.get("/v0/lookup", params={"textHash": ed["textHash"]}).json()
    assert by_text["results"][0]["match"] == "text"
    assert client.get("/v0/lookup", params={"gutenberg": "999999"}).json() == {"results": []}


def test_lookup_needs_a_parameter(client):
    problem(client.get("/v0/lookup"), 400)


def test_fetch_index_with_etag(client):
    r = client.get(f"/v0/indexes/{SCARLET}")
    assert r.status_code == 200
    assert r.json() == scarlet_doc()
    etag = r.headers["etag"]
    assert etag.startswith('"sha256:') and etag.endswith('"')
    assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"
    digest = client.get("/v0/lookup", params={"gutenberg": "244"}).json()["results"][0]["digest"]
    assert etag == f'"{digest}"'
    again = client.get(f"/v0/indexes/{SCARLET}", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    stale = client.get(f"/v0/indexes/{SCARLET}", headers={"If-None-Match": '"sha256:0"'})
    assert stale.status_code == 200


def test_fetch_series_and_unknown(client):
    r = client.get("/v0/series/sherlock-holmes")
    assert r.status_code == 200 and r.json()["id"] == "sherlock-holmes"
    assert client.get("/v0/series/sherlock-holmes", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    problem(client.get("/v0/series/nope"), 404)
    problem(client.get("/v0/indexes/nope"), 404)
    problem(client.get("/v0/indexes/nope/view", params={"anchor": "x"}), 404)


def test_view_by_anchor_equals_gating(client):
    doc, four = scarlet_doc(), json.loads((EXAMPLES / "sign-of-four.json").read_text())
    series = json.loads((EXAMPLES / "series.json").read_text())
    anchor = "ch2.para14"
    r = client.get(f"/v0/indexes/{SCARLET}/view", params={"anchor": anchor})
    assert r.status_code == 200
    assert r.json() == gating.view([doc, four], gating.Position("study-in-scarlet", anchor), series)
    Draft202012Validator({"$ref": "https://lorekeeper.dev/schema/0.1/view.schema.json"},
                         registry=_registry()).validate(r.json())
    # Second book: the first is finished by default, and `finished=` overrides it.
    anchor4 = four["anchors"][3]["id"]
    for query, fin in (({}, None), ({"finished": ""}, set())):
        r = client.get(f"/v0/indexes/{SIGN}/view", params={"anchor": anchor4, **query})
        assert r.json() == gating.view([doc, four], gating.Position("sign-of-four", anchor4), series, None, fin)


def test_view_bad_requests(client):
    base = f"/v0/indexes/{SCARLET}/view"
    problem(client.get(base), 400)                                                   # no position
    problem(client.get(base, params={"anchor": "nope"}), 400)                        # unknown anchor
    problem(client.get(base, params={"anchor": "ch2.para14", "block": "5", "textHash": "x"}), 400)
    problem(client.get(base, params={"block": "5"}), 400)                            # needs an edition
    problem(client.get(base, params={"block": "five", "textHash": "x"}), 400)
    problem(client.get(base, params={"spine": "1", "textHash": "x"}), 400)           # word or progress
    problem(client.get(base, params={"spine": "1", "word": "1", "progress": "0.5", "textHash": "x"}), 400)
    problem(client.get(base, params={"anchor": "ch2.para14", "order": "nope"}), 400)
    problem(client.get(base, params={"anchor": "ch2.para14", "inclusive": "maybe"}), 400)


def test_unknown_edition_is_409(client):
    for p in ({"block": "5", "textHash": "0" * 64}, {"cfi": "epubcfi(/6/2!/4/2)", "fileHash": "f" * 64}):
        body = problem(client.get(f"/v0/indexes/{SCARLET}/view", params=p), 409)
        assert "not held" in body["title"].lower()
    problem(client.get(f"/v0/indexes/{SCARLET}/entities", params={"name": "x", "block": "5", "textHash": "0" * 64}), 409)


def corpus_book():
    path = CORPUS / "pg244" / "pg244.epub"
    if not path.exists():
        pytest.skip("corpus/pg244/pg244.epub not downloaded")
    return path


@needs_locators
def test_view_by_block_matches_frontier(client):
    path = corpus_book()
    doc = scarlet_doc()
    blocks = epub.blocks(path)
    resolved = anchors_mod.resolve(doc["anchors"], blocks)
    ed = doc["editions"][0]
    anchor = next(a for a in doc["anchors"] if a["id"] == "ch2.para14")
    seq = resolved[anchor["id"]]
    for q in (seq - 1, seq, seq + 3, 0):
        for inclusive in ("false", "true"):
            for how in ({"textHash": ed["textHash"]}, {"fileHash": ed["fileHash"]}):
                r = client.get(f"/v0/indexes/{SCARLET}/view",
                               params={"block": q, "inclusive": inclusive, **how})
                if r.status_code == 409 and "textHash" in how:
                    continue   # this example lists a text hash from an older normalization
                assert r.status_code == 200, r.text
                done = q if inclusive == "true" else q - 1
                want = anchors_mod.frontier(doc["anchors"], resolved, done)
                assert r.json()["position"]["anchor"] == want
                assert r.json()["locator"] == {"position": want, "seq": q, "basis": "block", "exact": True}
                Draft202012Validator({"$ref": "https://lorekeeper.dev/schema/0.1/view.schema.json"},
                                     registry=_registry()).validate(r.json())
    # The anchor's own block: not yet finished (anchor withheld) versus inclusive (revealed).
    fin = client.get(f"/v0/indexes/{SCARLET}/view", params={"block": seq, "fileHash": ed["fileHash"]}).json()
    inc = client.get(f"/v0/indexes/{SCARLET}/view",
                     params={"block": seq, "fileHash": ed["fileHash"], "inclusive": "true"}).json()
    assert fin["position"]["anchor"] != "ch2.para14" and inc["position"]["anchor"] == "ch2.para14"


@needs_locators
def test_view_by_word_progress_reports_approximate(client):
    corpus_book()
    ed = scarlet_doc()["editions"][0]
    r = client.get(f"/v0/indexes/{SCARLET}/view", params={"spine": "3", "progress": "0.5", "fileHash": ed["fileHash"]})
    assert r.status_code == 200, r.text
    assert r.json()["locator"]["basis"] == "progress" and r.json()["locator"]["exact"] is False
    r = client.get(f"/v0/indexes/{SCARLET}/view", params={"spine": "3", "word": "10", "fileHash": ed["fileHash"]})
    assert r.status_code == 200 and r.json()["locator"]["basis"] == "word" and r.json()["locator"]["exact"] is True


@needs_locators
def test_bad_locator_is_400(client):
    corpus_book()
    ed = scarlet_doc()["editions"][0]
    base = f"/v0/indexes/{SCARLET}/view"
    problem(client.get(base, params={"cfi": "epubcfi(/6/9999!/4/2)", "fileHash": ed["fileHash"]}), 400)
    problem(client.get(base, params={"spine": "9999", "word": "1", "fileHash": ed["fileHash"]}), 400)
    problem(client.get(base, params={"cfi": "garbage", "fileHash": ed["fileHash"]}), 400)


def entity_named(doc, value):
    return next(e for e in doc["entities"] if any(n["value"] == value for n in e["names"]))


def test_entities_by_name(client):
    doc = scarlet_doc()
    holmes = entity_named(doc, "Sherlock Holmes")
    ids = {a["id"]: i for i, a in enumerate(doc["anchors"])}
    late = max((e["from"] for e in doc["entities"]), key=ids.get)
    r = client.get(f"/v0/indexes/{SCARLET}/entities", params={"name": "sherlock holmes", "anchor": late})
    assert r.status_code == 200
    body = r.json()
    check_def("entitiesResponse", body)
    assert "locator" not in body and body["position"] == late
    assert [e["id"] for e in body["entities"]] == [holmes["id"]]
    series = json.loads((EXAMPLES / "series.json").read_text())
    four = json.loads((EXAMPLES / "sign-of-four.json").read_text())
    view = gating.view([doc, four], gating.Position("study-in-scarlet", late), series)
    ent = body["entities"][0]
    assert {k: ent[k] for k in ("id", "type", "name", "aliases", "profile", "facets", "history")} == \
        next({k: e[k] for k in ("id", "type", "name", "aliases", "profile", "facets", "history")}
             for e in view["entities"] if e["id"] == holmes["id"])
    assert ent["relations"] == [r for r in view["relations"] if holmes["id"] in (r["src"], r["dst"])]
    assert ent["events"] == [v for v in view["events"] if holmes["id"] in v["entities"]]
    assert ent["relations"] or ent["events"]
    # Surname lookup, when it is unique among the visible entities.
    sur = client.get(f"/v0/indexes/{SCARLET}/entities", params={"name": "Holmes", "anchor": late}).json()
    assert [e["id"] for e in sur["entities"]] == [holmes["id"]]
    assert client.get(f"/v0/indexes/{SCARLET}/entities", params={"name": "Nobody", "anchor": late}).json()["entities"] == []
    problem(client.get(f"/v0/indexes/{SCARLET}/entities", params={"anchor": late}), 400)


def test_entities_reveal_nothing_beyond_the_position(client):
    doc = scarlet_doc()
    ids = {a["id"]: i for i, a in enumerate(doc["anchors"])}
    series = json.loads((EXAMPLES / "series.json").read_text())
    four = json.loads((EXAMPLES / "sign-of-four.json").read_text())
    for pos in (a["id"] for a in doc["anchors"][::6]):
        view = gating.view([doc, four], gating.Position("study-in-scarlet", pos), series)
        seen_events = {e["id"] for e in view["events"]}
        seen_rels = {r["id"] for r in view["relations"]}
        for e in doc["entities"]:
            name = e["names"][0]["value"]
            body = client.get(f"/v0/indexes/{SCARLET}/entities", params={"name": name, "anchor": pos}).json()
            visible_ids = {x["id"] for x in view["entities"]}
            for m in body["entities"]:
                assert {v["id"] for v in m["events"]} <= seen_events
                assert {v["id"] for v in m["relations"]} <= seen_rels
                assert m["id"] in visible_ids


@needs_locators
def test_entities_by_block_is_inclusive_by_default(client):
    path = corpus_book()
    doc = scarlet_doc()
    resolved = anchors_mod.resolve(doc["anchors"], epub.blocks(path))
    seq = resolved["ch2.para14"]
    fh = doc["editions"][0]["fileHash"]
    name = "Sherlock Holmes"
    inc = client.get(f"/v0/indexes/{SCARLET}/entities", params={"name": name, "block": seq, "fileHash": fh}).json()
    exc = client.get(f"/v0/indexes/{SCARLET}/entities",
                     params={"name": name, "block": seq, "fileHash": fh, "inclusive": "false"}).json()
    check_def("entitiesResponse", inc)
    assert inc["position"] == "ch2.para14" and inc["locator"]["seq"] == seq
    assert exc["position"] == anchors_mod.frontier(doc["anchors"], resolved, seq - 1)


def test_bearer_token(client, monkeypatch):
    monkeypatch.setenv("LOREKEEPER_API_TOKEN", "s3cret")
    assert client.get("/.well-known/lorekeeper").json()["auth"] == {"type": "bearer"}
    problem(client.get("/v0/lookup", params={"gutenberg": "244"}), 401)
    problem(client.get("/v0/lookup", params={"gutenberg": "244"}, headers={"Authorization": "Bearer nope"}), 401)
    ok = client.get("/v0/lookup", params={"gutenberg": "244"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert client.get(f"/v0/indexes/{SCARLET}").status_code == 401
    # The UI's own API is untouched by the token.
    assert client.get("/api/library").status_code == 200
    # Preflight needs no token.
    assert client.options("/v0/lookup", headers={"Origin": "https://reader.example"}).status_code == 204


def test_cors(client):
    r = client.get(f"/v0/indexes/{SCARLET}", headers={"Origin": "https://reader.example"})
    assert r.headers["access-control-allow-origin"] == "*"
    assert "ETag" in r.headers["access-control-expose-headers"]
    assert client.get("/.well-known/lorekeeper").headers["access-control-allow-origin"] == "*"
    assert problem(client.get("/v0/lookup"), 400) and client.get("/v0/lookup").headers["access-control-allow-origin"] == "*"
    pre = client.options("/v0/indexes/x/view", headers={"Origin": "https://reader.example",
                                                         "Access-Control-Request-Method": "GET",
                                                         "Access-Control-Request-Headers": "authorization,if-none-match"})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "*"
    assert "Authorization" in pre.headers["access-control-allow-headers"]
    assert "access-control-allow-origin" not in client.get("/api/library").headers


def test_docs_list_protocol_tag(client):
    spec = client.get("/openapi.json").json()
    tagged = [p for p, ops in spec["paths"].items() if any("Protocol" in o.get("tags", []) for o in ops.values())]
    assert "/v0/indexes/{id}/view" in tagged and "/.well-known/lorekeeper" in tagged
