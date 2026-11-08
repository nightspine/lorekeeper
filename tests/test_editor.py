"""Editing indexes by hand through the web API: a blank index written from scratch, every
operation, the content rules, anchors chosen from paragraphs, undo, and edits that survive a rebuild."""

import json

import pytest

pytest.importorskip("fastapi")

from lorekeeper import validate  # noqa: E402
from lorekeeper.web import editor as editor_mod  # noqa: E402

from .test_merge import _ent, _index  # noqa: E402
from .test_web import client, upload, wait_for  # noqa: E402, F401

# The Lighthouse (tests/test_generate.py make_epub): seq 0 "Chapter One" heading, 1 Alice arrives,
# 2 her brother Tom, 3 the keeper Ezra Pike, 4 "Yes." (too short to anchor), 5 the letter,
# 6 "Chapter Two" heading, 7 Pike admits, 8 Tom left by boat.


def edit(client, key, **op):
    r = client.post(f"/api/library/{key}/edits", json=op)
    assert r.status_code == 200, r.text
    return r.json()


def refuse(client, key, status, **op):
    r = client.post(f"/api/library/{key}/edits", json=op)
    assert r.status_code == status, r.text
    return r.json()


def doc_of(client, key):
    return client.get(f"/api/library/{key}").json()


def handmade(client, tmp_path):
    book = upload(client, tmp_path)
    r = client.post(f"/api/catalog/books/up:{book['id']}/handmade", json={"license": "CC-BY-4.0"})
    assert r.status_code == 200, r.text
    return book, r.json()["key"]


def test_an_index_written_by_hand_is_valid_against_the_book(client, tmp_path):
    book, key = handmade(client, tmp_path)
    d = doc_of(client, key)
    assert d["provenance"]["contributors"] == [{"name": "admin", "role": "author"}] and "generator" not in d["provenance"]
    assert d["coverage"] == {"complete": False, "through": "toc01.para01"} and d["anchors"][0]["seq"] == 1
    assert client.get(f"/api/catalog/books/up:{book['id']}").json()["versions"][0]["model"] == "Hand-written"

    alice = edit(client, key, op="add-entity", name="Alice Marlow", type="person", seq=1,
                 desc="A young woman who arrives at a lighthouse one March morning.")
    assert alice["id"] == "alice-marlow" and alice["what"] == "Added Alice Marlow" and alice["anchor"] == "toc01.para01"
    tom = edit(client, key, op="add-entity", name="Tom Marlow", type="person", seq=2)
    pike = edit(client, key, op="add-entity", name="Ezra Pike", type="person", seq=3, desc="The keeper of the lighthouse.")
    edit(client, key, op="add-name", entity="ezra-pike", value="the keeper", kind="descriptive", seq=3)
    detail = edit(client, key, op="add-facet", entity="alice-marlow", text="Finds a letter hidden under the floor.", seq=5)
    rel = edit(client, key, op="add-relation", src="alice-marlow", dst="tom-marlow", label="searching for", seq=2, basis="stated")
    ev = edit(client, key, op="add-event", summary="Alice finds a hidden letter.", entities=["alice-marlow"], seq=5)
    story = edit(client, key, op="add-recap", text="Alice has come to the lighthouse to find her missing brother.", seq=5)
    chap = edit(client, key, op="add-recap", scope="chapter", title="Chapter One", seq=5,
                text="Alice arrives, the keeper denies knowing Tom, and she finds a letter.")
    edit(client, key, op="edit-facet", id=detail["id"], text="Finds a letter under the floorboards.", basis="inferred")
    edit(client, key, op="edit-name", entity="tom-marlow", index=0, value="Thomas Marlow")
    edit(client, key, op="set-type", entity="ezra-pike", type="creature")
    edit(client, key, op="edit-event", id=ev["id"], entities=["alice-marlow", "tom-marlow"])
    edit(client, key, op="edit-relation", id=rel["id"], label="looking for")
    edit(client, key, op="edit-recap", id=chap["id"], title="One")

    d = doc_of(client, key)
    ents = {e["id"]: e for e in d["entities"]}
    assert ents["tom-marlow"]["names"][0]["value"] == "Thomas Marlow" and ents["ezra-pike"]["type"] == "creature"
    assert [n["value"] for n in ents["ezra-pike"]["names"]] == ["Ezra Pike", "the keeper"]
    assert ents["alice-marlow"]["facets"][0]["kind"] == "profile"
    assert ents["alice-marlow"]["facets"][1] == {"id": detail["id"], "from": "toc01.para05", "basis": "inferred",
                                                 "evidence": ["toc01.para05"], "text": "Finds a letter under the floorboards."}
    assert d["relations"][0]["label"] == "looking for" and d["events"][0]["entities"] == ["alice-marlow", "tom-marlow"]
    assert [r.get("scope", "story") for r in d["recaps"]] == ["story", "chapter"] and d["recaps"][1]["title"] == "One"
    assert d["coverage"]["through"] == "toc01.para05" and [a["seq"] for a in d["anchors"]] == [1, 2, 3, 5]
    assert d["provenance"]["contributors"] == [{"name": "admin", "role": "author"}]
    assert tom["id"] == "tom-marlow" and pike["id"] == "ezra-pike" and story["id"].startswith("the-lighthouse.recap")

    from lorekeeper import epub
    blocks = epub.blocks(tmp_path / "lighthouse.epub")
    assert [str(i) for i in validate.check_index(d) + validate.check_against_book(d, blocks) if i.level == "error"] == []

    assert edit(client, key, op="set-complete", complete=True)["what"] == "Marked the index complete"
    d = doc_of(client, key)
    assert d["coverage"] == {"complete": True, "through": "toc01.para05"}
    assert not [i for i in validate.check_index(d) if i.level == "error"]


def test_anchors_snap_forward_and_are_shared(client, tmp_path):
    _, key = handmade(client, tmp_path)
    a = edit(client, key, op="add-entity", name="Alice", type="person", seq=4)     # "Yes." can't carry an anchor
    assert a["anchor"] == "toc01.para05"
    b = edit(client, key, op="add-entity", name="Letter", type="object", seq=5)
    assert b["anchor"] == "toc01.para05"
    c = edit(client, key, op="add-entity", name="Pike", type="person", seq=7)
    assert c["anchor"] == "toc02.para01"
    d = doc_of(client, key)
    assert [a["seq"] for a in d["anchors"]] == [1, 5, 7] and d["coverage"]["through"] == "toc02.para01"
    refuse(client, key, 400, op="add-entity", name="Nobody", type="person", seq=99)
    refuse(client, key, 400, op="add-facet", entity="pike", text="Seen earlier.", seq=1)   # before Pike is revealed


def test_content_rules_are_refused_with_a_reason(client, tmp_path):
    _, key = handmade(client, tmp_path)
    edit(client, key, op="add-entity", name="Alice Marlow", type="person", seq=1)
    edit(client, key, op="add-entity", name="Tom Marlow", type="person", seq=2)
    w = lambda **op: refuse(client, key, 422, **op)  # noqa: E731
    assert w(op="add-facet", entity="alice-marlow", text="It turns out she is the keeper's daughter.", seq=3)["code"] == "wording"
    assert "hindsight" in w(op="add-facet", entity="alice-marlow", text="Seemingly a harmless visitor.", seq=1)["detail"]
    assert "“now”" in w(op="add-facet", entity="alice-marlow", kind="profile", text="She is now at the lighthouse.", seq=1)["detail"]
    assert "fiancée" in w(op="add-facet", entity="alice-marlow", text="Tom's fiancée.", seq=1)["detail"]
    assert "Quotes" in w(op="add-facet", entity="alice-marlow",
                         text="She had come to find her brother, Tom Marlow, who had vanished long ago.", seq=2)["detail"]
    assert "280" in w(op="add-facet", entity="alice-marlow", text="x" * 281, seq=1)["detail"]
    assert "80" in w(op="add-relation", src="alice-marlow", dst="tom-marlow", label="x" * 81, seq=2)["detail"]
    # The passage states the tie, so the same words pass; a detail (not a description) may say "now".
    edit(client, key, op="add-facet", entity="tom-marlow", text="Alice's brother, missing for a year.", seq=2)
    edit(client, key, op="add-facet", entity="alice-marlow", text="Now looking for Tom.", seq=2)
    refuse(client, key, 400, op="add-facet", entity="alice-marlow", text="Fine.", seq=1, basis="guessed")
    refuse(client, key, 400, op="fly", entity="alice-marlow")
    refuse(client, key, 400, op="add-relation", src="alice-marlow", dst="alice-marlow", label="herself", seq=2)


def test_delete_cleans_references_and_undo_restores(client, tmp_path):
    _, key = handmade(client, tmp_path)
    edit(client, key, op="add-entity", name="Alice Marlow", type="person", seq=1, desc="A visitor to the lighthouse.")
    edit(client, key, op="add-entity", name="Tom Marlow", type="person", seq=2)
    edit(client, key, op="add-relation", src="alice-marlow", dst="tom-marlow", label="looking for", seq=2)
    ev = edit(client, key, op="add-event", summary="Alice arrives.", entities=["alice-marlow", "tom-marlow"], seq=2)
    refuse(client, key, 400, op="delete-name", entity="alice-marlow", index=0)   # the last name stays
    gone = edit(client, key, op="delete-entity", entity="alice-marlow")
    assert gone["what"] == "Deleted Alice Marlow"
    d = doc_of(client, key)
    assert [e["id"] for e in d["entities"]] == ["tom-marlow"] and d["relations"] == []
    assert d["events"][0]["entities"] == ["tom-marlow"]
    dup = client.get(f"/api/library/{key}/duplicates").json()
    assert dup["undo"]["kind"] == "edit" and dup["undo"]["what"] == "Deleted Alice Marlow" and dup["undo"]["id"] == gone["undo"]

    assert client.post(f"/api/library/{key}/undo/{gone['undo']}").json()["what"] == "Deleted Alice Marlow"
    d = doc_of(client, key)
    assert [e["id"] for e in d["entities"]] == ["alice-marlow", "tom-marlow"] and len(d["relations"]) == 1
    assert client.post(f"/api/library/{key}/undo/{gone['undo']}").status_code == 409   # already undone
    assert edit(client, key, op="delete-event", id=ev["id"])["what"] == "Deleted an event"
    assert doc_of(client, key)["events"] == []
    # The undo removed the deletion from the recorded edits too.
    sidecar = json.loads(next((tmp_path / "data" / "edits").glob("*.json")).read_text())["edits"]
    assert [e["op"]["op"] for e in sidecar][-2:] == ["add-event", "delete-event"]


def test_entity_wide_changes_reach_every_book_of_the_series(client, tmp_path):
    one = {**_index([_ent("dior", ["Dior"], "a1", facets=[("a1", "A thief.")]), _ent("laure", ["Laure"], "a2")],
                    relations=[{"id": "b1.rel.1", "src": "dior", "dst": "laure", "label": "knows", "from": "a2",
                                "basis": "stated", "evidence": ["a2"]}]),
           "id": "s.one", "series": {"id": "s", "book": "one"}}
    two = {**_index([_ent("dior", ["Dior"], "a1"), _ent("mina", ["Mina"], "a3")],
                    events=[{"id": "b2.ev.1", "from": "a3", "summary": "Dior meets Mina.", "entities": ["dior", "mina"], "evidence": ["a3"]}]),
           "id": "s.two", "series": {"id": "s", "book": "two"}}
    for name, d in (("one", one), ("two", two)):
        d["entities"][0]["facets"] = [{**f, "id": f"{name}.{f['id']}"} for f in d["entities"][0]["facets"]]
        (tmp_path / "data" / "indexes" / f"{name}.lore.json").write_text(json.dumps(d))
    (tmp_path / "data" / "indexes" / "s.series.json").write_text(json.dumps(
        {"lorekeeper": "0.1", "kind": "series", "id": "s", "title": "S", "books": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
         "orders": {"publication": ["one", "two"]}, "defaultOrder": "publication"}))
    key = "generated--one.lore"
    assert edit(client, key, op="set-type", entity="dior", type="creature")["what"] == "Changed Dior to a creature"
    assert all(doc_of(client, f"generated--{b}.lore")["entities"][0]["type"] == "creature" for b in ("one", "two"))
    # No book file on disk: paragraphs can't be chosen, but deleting needs none.
    assert "book file" in refuse(client, key, 400, op="add-facet", entity="dior", text="A thief.", seq=1)["detail"]
    edit(client, key, op="delete-entity", entity="dior")
    assert [e["id"] for e in doc_of(client, "generated--one.lore")["entities"]] == ["laure"]
    t = doc_of(client, "generated--two.lore")
    assert [e["id"] for e in t["entities"]] == ["mina"] and t["events"][0]["entities"] == ["mina"]
    assert doc_of(client, key)["relations"] == []
    assert refuse(client, "generated--two.lore", 400, op="edit-facet", id="one.dior.0", text="x")["detail"]


def test_edits_are_applied_again_after_a_rebuild(client, tmp_path):
    book = upload(client, tmp_path)
    job = client.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    assert wait_for(client, job["id"], {"done", "failed"})["status"] == "done"
    key = next(i["key"] for i in client.get("/api/library").json()["indexes"] if i["source"] == "generated")
    d = doc_of(client, key)
    pike = next(e for e in d["entities"] if e["id"] == "ezra-pike")
    denial = next(f for f in pike["facets"] if "never heard" in f["text"])
    edit(client, key, op="edit-name", entity="ezra-pike", index=0, value="Ezra Pike the keeper")
    edit(client, key, op="delete-facet", id=denial["id"])
    edit(client, key, op="add-facet", entity="alice-marlow", text="Travels alone.", seq=1)
    edit(client, key, op="set-type", entity="lighthouse", type="object")
    edit(client, key, op="delete-entity", entity="tom-marlow")
    assert d["provenance"]["contributors"][0]["role"] == "generator"
    assert doc_of(client, key)["provenance"]["contributors"][-1] == {"name": "admin", "role": "editor"}

    again = client.post("/api/jobs", json={"book_id": book["id"], "config": {"chunk_words": 5000}}).json()
    done = wait_for(client, again["id"], {"done", "failed"})
    assert done["status"] == "done" and done["report"]["edits"] == {"applied": 5, "skipped": 0}
    key2 = next(i["key"] for i in client.get("/api/library").json()["indexes"] if i["source"] == "generated" and i["key"] != key)
    d2 = doc_of(client, key2)
    ents = {e["id"]: e for e in d2["entities"]}
    assert "tom-marlow" not in ents and ents["lighthouse"]["type"] == "object"
    assert ents["ezra-pike"]["names"][0]["value"] == "Ezra Pike the keeper"
    assert not any("never heard" in f["text"] for f in ents["ezra-pike"]["facets"])
    assert any(f["text"] == "Travels alone." for f in ents["alice-marlow"]["facets"])
    assert not [i for i in validate.check_index(d2) if i.level == "error"]
    lines = []
    with client.stream("GET", f"/api/jobs/{again['id']}/events") as r:   # SSE: read until the run's final event
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev["type"] == "log":
                    lines.append(ev["line"])
                if ev["type"] in ("done", "failed"):
                    break
    assert "your edits: 5 applied again" in lines


def test_events_can_be_weighed_by_hand(client, tmp_path):
    _, key = handmade(client, tmp_path)
    edit(client, key, op="add-entity", name="Alice Marlow", type="person", seq=1)
    ev = edit(client, key, op="add-event", summary="Alice finds the letter.", entities=["alice-marlow"], seq=5, kind="turn")
    plain = edit(client, key, op="add-event", summary="Alice has supper.", entities=[], seq=5, kind="")
    d = doc_of(client, key)
    kinds = {e["id"]: e.get("kind") for e in d["events"]}
    assert kinds[ev["id"]] == "turn" and kinds[plain["id"]] is None
    edit(client, key, op="edit-event", id=ev["id"], kind="minor")
    edit(client, key, op="edit-event", id=plain["id"], summary="Alice eats.")       # kind untouched when not sent
    d = doc_of(client, key)
    kinds = {e["id"]: e.get("kind") for e in d["events"]}
    assert kinds[ev["id"]] == "minor" and kinds[plain["id"]] is None
    edit(client, key, op="edit-event", id=ev["id"], kind="")
    assert "kind" not in next(e for e in doc_of(client, key)["events"] if e["id"] == ev["id"])
    refuse(client, key, 400, op="add-event", summary="x", entities=[], seq=5, kind="huge")


def test_passage_for_the_anchor_picker(client, tmp_path):
    _, key = handmade(client, tmp_path)
    th = doc_of(client, key)["editions"][0]["textHash"]
    r = client.get(f"/api/editions/{th}/blocks", params={"seq": 4, "before": 1, "after": 2, "i": key}).json()
    assert r["seq"] == 4 and r["total"] == 9 and r["chapter"] == {"n": 1, "title": "Chapter One", "start": 0}
    assert [(b["seq"], b["eligible"], b["anchor"]) for b in r["blocks"]] == [(3, True, None), (4, False, None), (5, True, None), (6, False, None)]
    assert r["blocks"][1]["text"] == "Yes." and r["blocks"][0]["words"] == 17
    assert client.get(f"/api/editions/{th}/blocks", params={"seq": 1, "before": 0, "after": 0, "i": key}).json()["blocks"][0]["anchor"] == "toc01.para01"
    assert client.get("/api/editions/nope/blocks").status_code == 404


def test_wording_rules_match_the_generator():
    ok = editor_mod.check_wording
    ok("detail", "A quiet man who keeps the light.")
    with pytest.raises(editor_mod.WordingError, match="hindsight"):
        ok("event", "Unbeknownst to Alice, Tom is upstairs.")
    with pytest.raises(editor_mod.WordingError, match="moment"):
        ok("profile", "Is about to leave the lighthouse.")
    ok("detail", "Is about to leave the lighthouse.")   # a detail may describe the moment
    with pytest.raises(editor_mod.WordingError, match="passage never does"):
        ok("label", "husband of", cited="They were married last spring.")   # "married" is not "husband"
    ok("label", "husband of", cited="Her husband, the keeper.")
    with pytest.raises(editor_mod.WordingError, match="1200"):
        ok("story", "x" * 1201)
    ok("name", "Seemingly Fine")   # names carry no claims
