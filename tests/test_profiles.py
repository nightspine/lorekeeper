"""Entity profiles and the name guardrails: what an entity *is*, not what it did, and no junk entities."""

import json

import pytest

from lorekeeper import epub, generate, validate
from lorekeeper.gating import Position, view

from .test_generate import make_epub


@pytest.mark.parametrize("name,etype,expected", [
    ("Gabriel's straight razor", "object", ("drop", "description, not a name")),
    ("Gabriel's fear", "concept", ("drop", "description, not a name")),
    ("Gabriel's cell tower", "place", ("drop", "description, not a name")),
    ("Gabriel and Celene's execution fight", "concept", ("drop", "description, not a name")),
    ("Celene's brother Gabriel", "person", ("known", "gabriel")),
    ("Chevalier of the Golden Host (Gabriel)", "person", ("known", "gabriel")),
    ("Gabriel's sister Celene", "person", ("name", "Celene")),               # someone new, by name
    ("Gabriel's bedchamber, White Rabbit", "place", ("name", "White Rabbit")),
    ("Gabriel's daughter", "person", ("name", "Gabriel's daughter")),         # unnamed people stay
    ("Gabriel's grandfather, baron of León", "person", ("name", "Gabriel's grandfather, baron of León")),
    ("King's Landing", "place", ("name", "King's Landing")),                  # a real name
    ("Isla (thrall)", "person", ("name", "Isla")),
    ("Lashaame (Gabriel)", "person", ("name", "Lashaame")),                  # a name with a note stays itself
    ("Dior's Saoirse and Bellamy", "person", ("name", "Dior's Saoirse and Bellamy")),
    ("Dior's room at Aveléne", "place", ("drop", "description, not a name")),
])
def test_triage_name(name, etype, expected):
    known = {"gabriel": "gabriel"}
    assert generate.triage_name(name, etype, lambda n: known.get(n.casefold())) == expected


ALICE_1 = "A young woman from the village who has come to the lighthouse to look for her missing brother."
ALICE_2 = "A determined young woman searching for her missing brother Tom, who keeps a hidden letter that may be his."


class ProfileLLM:
    """Chapter one introduces Alice with a profile and offers junk names; chapter two updates her profile."""
    name = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, system, prompt, schema, prefix=None):
        self.calls.append((prefix or "") + prompt)
        props = schema.get("properties", {})
        if "new" in props:
            if "Alice Marlow arrived" in (prefix or ""):
                return {"new": [{"name": "Alice Marlow", "type": "person", "p": 2, "desc": "", "desc_p": 2},
                                {"name": "Alice's lantern", "type": "object", "p": 2, "desc": "", "desc_p": 2},
                                {"name": "Tom's sister Alice", "type": "person", "p": 3, "desc": "", "desc_p": 3}],
                        "updates": [{"id": "Tom's sister Alice", "p": 3, "desc": "She is Tom Marlow's younger sister."}],
                        "profiles": [{"id": "Alice Marlow", "p": 3, "text": ALICE_1}],
                        "names": [], "relations": [], "events": [], "summary": [], "recap": "Alice reaches the lighthouse."}
            return {"new": [], "updates": [], "names": [], "relations": [], "events": [], "summary": [],
                    "profiles": [{"id": "alice-marlow", "p": 2, "text": ALICE_2}],
                    "recap": "Pike admits that Tom stayed at the lighthouse."}
        return {"unsupported": [], "later": []}


def _run(path, out, opts=None):
    llm = ProfileLLM()
    generate.run(epub.read(path), llm, opts or generate.Options(), path, out, log=lambda m: None)
    return json.loads(out.read_text()), llm


def test_profiles_replace_each_other_and_junk_is_kept_out(tmp_path):
    path = make_epub(tmp_path / "b.epub")
    ix, llm = _run(path, tmp_path / "out.lore.json")
    names = {n["value"] for e in ix["entities"] for n in e["names"]}
    assert "Alice's lantern" not in names and "Tom's sister Alice" not in names
    alice = next(e for e in ix["entities"] if e["id"] == "alice-marlow")
    profiles = [f for f in alice["facets"] if f.get("kind") == "profile"]
    assert [f["text"] for f in profiles] == [ALICE_1, ALICE_2]
    assert profiles[1]["replaces"] == [profiles[0]["id"]]
    assert "She is Tom Marlow's younger sister." in [f["text"] for f in alice["facets"]]   # via the description
    check = next(c for c in llm.calls if "Pike admitted" in c and "Check each claim" in c)
    assert f"known: {ALICE_1}" in check      # a new profile is checked against what was already known
    assert not [e for e in validate.check_index(ix) if e.level == "error"]
    # The view shows the profile as of the position: the first one in chapter one, the second later.
    first = profiles[0]["from"]
    v1 = view([ix], Position(None, first))
    assert next(e for e in v1["entities"] if e["id"] == "alice-marlow")["profile"]["text"] == ALICE_1
    v2 = view([ix], Position(None, ix["coverage"]["through"]))
    a2 = next(e for e in v2["entities"] if e["id"] == "alice-marlow")
    assert a2["profile"]["text"] == ALICE_2 and ALICE_1 in [h["text"] for h in a2["history"]]


def test_series_book_continues_the_earlier_profile(tmp_path):
    path = make_epub(tmp_path / "b.epub")
    book1 = {
        "lorekeeper": "0.1", "kind": "index", "id": "s.one", "series": {"id": "s", "book": "one"},
        "anchors": [{"id": "a1", "fp": "0" * 16}],
        "entities": [{"id": "alice-marlow", "type": "person", "from": "a1",
                      "names": [{"value": "Alice Marlow", "kind": "primary", "from": "a1"}],
                      "facets": [{"id": "one.alice-marlow.p", "kind": "profile", "from": "a1", "basis": "stated",
                                  "evidence": ["a1"], "text": "A schoolteacher who lost her brother at sea."}]}],
        "relations": [], "events": [], "recaps": [],
    }
    ix, llm = _run(path, tmp_path / "two.lore.json", generate.Options(series_id="s", book_key="two", prior=[book1]))
    assert "profile: A schoolteacher who lost her brother at sea." in llm.calls[0]   # inherited, shown to the model
    alice = next(e for e in ix["entities"] if e["id"] == "alice-marlow")
    first = next(f for f in alice["facets"] if f.get("kind") == "profile")
    assert first["replaces"] == ["one.alice-marlow.p"]                              # overrides book one's
    assert not [e for e in validate.check_index(ix, [book1]) if e.level == "error"]


def _doc(key, entities, recaps=(), n=6):
    for e in entities:
        for f in e["facets"]:
            f["id"] = f"{key}.{f['id']}"
    return {"lorekeeper": "0.1", "kind": "index", "id": key, "series": {"id": "s", "book": key},
            "work": {"title": key, "authors": ["X"], "language": "en"},
            "editions": [{"textHash": "0" * 64, "fileHash": "0" * 64}], "coverage": {"complete": True, "through": f"a{n}"},
            "provenance": {"contributors": [], "review": [], "created": "2026-01-01T00:00:00Z", "license": "CC0-1.0"},
            "anchors": [{"id": f"a{i}", "fp": f"{i:016x}"} for i in range(1, n + 1)],
            "entities": entities, "relations": [], "events": [], "recaps": list(recaps)}


def _e(eid, names, facts, etype="person", frm="a1"):
    return {"id": eid, "type": etype, "from": frm,
            "names": [{"value": x, "kind": "primary" if i == 0 else "alias", "from": frm} for i, x in enumerate(names)],
            "facets": [{"id": f"{eid}.{i}", "from": a, "basis": "stated", "evidence": [a], "text": t} for i, (a, t) in enumerate(facts)]}


def test_tidy_removes_descriptions_and_folds_wrapped_names():
    from lorekeeper import profiles
    doc = _doc("one", [
        _e("gabriel-de-leon", ["Gabriel de León", "Gabriel"], [("a1", "A silversaint.")]),
        _e("gabriel", ["Gabriel"], [("a2", "Fights well.")]),                 # alias match: left to the duplicates review
        _e("celenes-brother-gabriel", ["Celene's brother Gabriel"], [("a3", "Mourns his sister.")]),
        _e("gabriels-razor", ["Gabriel's straight razor"], [("a2", "Kept very sharp.")], etype="object"),
        _e("gabriels-daughter", ["Gabriel's daughter"], [("a2", "Gabriel's only child.")]),       # a person: kept
    ])
    doc["relations"] = [{"id": "r1", "src": "gabriel-de-leon", "dst": "gabriels-razor", "label": "shaves with",
                         "from": "a2", "basis": "stated", "evidence": ["a2"]}]
    t = profiles.tidy([doc])
    ids = [e["id"] for e in doc["entities"]]
    assert ids == ["gabriel-de-leon", "gabriel", "gabriels-daughter"]
    assert t["removed"] == ["Gabriel's straight razor"] and not doc["relations"]
    gab = doc["entities"][1]   # the entry whose primary name is "Gabriel"
    assert "Mourns his sister." in {f["text"] for f in gab["facets"]}
    assert not [e for e in validate.check_index(doc) if e.level == "error"]


class Writer:
    name = "fake"

    def __init__(self):
        self.prompts = []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
        ids = [line.split(":")[0] for line in prompt.splitlines() if line.startswith("E")]
        return {"profiles": [{"id": i, "text": f"Profile {len(self.prompts)}.{i}"} for i in ids]}


def test_backfill_profiles_are_gated_and_chain_across_books():
    from lorekeeper import profiles
    recap = lambda key, a: {"id": f"{key}.recap.{a}", "from": a, "text": "So far.", "evidence": [a]}  # noqa: E731
    one = _doc("one", [_e("alice", ["Alice"], [("a1", "A teacher."), ("a2", "Has a brother."), ("a5", "Loses her post."), ("a6", "Leaves town.")]),
                       _e("bob", ["Bob"], [("a2", "A sailor.")])], [recap("one", "a3"), recap("one", "a6")])
    two = _doc("two", [_e("alice", ["Alice"], [("a2", "Runs an inn."), ("a3", "Has remarried.")])], [recap("two", "a4")])
    m = Writer()
    stats = profiles.backfill([one, two], m, log=lambda s: None)
    a1 = [f for f in one["entities"][0]["facets"] if f.get("kind") == "profile"]
    assert [f["from"] for f in a1] == ["a2", "a6"]                 # at the last fact each one draws on
    assert a1[1]["replaces"] == [a1[0]["id"]]
    assert "Loses her post." not in m.prompts[0]                    # nothing from after the first recap
    assert not any(f.get("kind") == "profile" for f in one["entities"][1]["facets"])   # one fact: too little yet
    a2 = next(f for f in two["entities"][0]["facets"] if f.get("kind") == "profile")
    assert a2["replaces"] == [a1[1]["id"]]                          # book two continues book one's profile
    assert "Leaves town." in m.prompts[-1] and "previous profile" in m.prompts[-1]
    assert stats["profiles"] == 3
    for d, prior in ((one, []), (two, [one])):
        assert not [e for e in validate.check_index(d, prior) if e.level == "error"]
    v = view([one], Position("one", "a4"), {"lorekeeper": "0.1", "kind": "series", "id": "s", "title": "S",
             "books": [{"key": "one", "title": "One"}], "orders": {"p": ["one"]}, "defaultOrder": "p"})
    assert next(e for e in v["entities"] if e["id"] == "alice")["profile"]["id"] == a1[0]["id"]


@pytest.mark.parametrize("text,momentary", [
    ("Harry Potter, the boy who survived, now on the train to Hogwarts.", True),
    ("He has begun to befriend Ron Weasley.", True),
    ("She is about to leave for the capital.", True),
    ("An orphaned boy raised by his aunt and uncle, who learns he is a famous wizard; brave and loyal.", False),
    ("The king's sister, exiled after the war and feared at court.", False),
])
def test_profiles_describe_not_narrate(text, momentary):
    assert bool(generate.MOMENTARY.search(text)) is momentary
