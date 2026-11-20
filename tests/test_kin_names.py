"""Kin and role words are never people: bare words dropped, "X's sister" resolved to the named relative,
unnamed relatives admitted only with a part, and the description folded into the relative named later."""

import pytest

from lorekeeper import epub, generate, profiles, validate
from lorekeeper.gating import Position, view

from . import test_generate as tg

CH1 = [
    "Gabriel de Leon crossed the bridge into Aveline at dusk, his hood pulled low against the rain.",
    "Gabriel's sister waited at the inn, and she said that Mama had sent word from the north.",
    "Father would not come, for Father had never forgiven the family for leaving the north.",
    "Gabriel's father had been a hard man, and Gabriel's cousin laughed about it over supper.",
]
CH2 = [
    "Amelie de Leon greeted her brother Gabriel warmly in the courtyard of the old keep.",
    "Uncle Vernon arrived late and sat without a word beside the cold hearth in the hall.",
    "Celene de Leon followed her into the courtyard and stood close to the old well.",
]


def _new(name, p, typ="person"):
    return {"name": name, "type": typ, "p": p, "desc": "", "desc_p": p}


def _resp(**kw):
    out = {"new": [], "updates": [], "profiles": [], "names": [], "relations": [], "events": [], "summary": [], "recap": ""}
    out.update(kw)
    if any(n["name"] == "Gabriel de Leon" for n in out["new"]):   # an entity with nothing to say is pruned
        out["updates"] = [{"id": "Gabriel de Leon", "p": 2, "desc": "A hooded traveller who reaches Aveline at dusk."}, *out["updates"]]
    return out


class Script(tg.FakeLLM):
    """Chapter one and two answers are given; every claim passes the fact-check."""
    def __init__(self, ch1, ch2):
        super().__init__()
        self.ch1, self.ch2 = ch1, ch2

    def complete(self, system, prompt, schema, prefix=None):
        if "new" in schema.get("properties", {}):
            self.calls.append((prefix or "") + prompt)
            return self.ch1 if "Gabriel de Leon crossed" in (prefix or "") else self.ch2
        if "verdicts" in schema.get("properties", {}):
            self.calls.append((prefix or "") + prompt)
            return {"verdicts": [], "later": [], "moments": []}
        return super().complete(system, prompt, schema, prefix)


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "CH2", CH2)
    return tg.make_epub(tmp_path / "gabriel.epub", ch1=CH1)


@pytest.fixture
def book_a(tmp_path, monkeypatch):
    """The same book with the sisters already on the page in chapter one."""
    monkeypatch.setattr(tg, "CH2", CH2)
    return tg.make_epub(tmp_path / "sisters.epub", ch1=[*CH1, "Amelie de Leon, a tall healer, sat with Celene de Leon beside the fire in the common room.",
        "Mr. Ezra Pike, a gaunt man in a grey coat, stood at the door of the inn."])


def run(book, tmp_path, ch1, ch2=None):
    ix, report, llm = tg._run(book, tmp_path, llm=Script(ch1, ch2 or _resp()))
    assert [str(e) for e in validate.check_index(ix) if e.level == "error"] == []
    return ix, report


def names(ix):
    return {e["id"]: [n["value"] for n in e["names"]] for e in ix["entities"]}


GABRIEL = _new("Gabriel de Leon", 2)


def test_bare_kin_and_role_words():
    for bad in ("Father", "Mama", "mother", "my mother", "the boy", "His father", "the old man", "Guards", "Papa", "the voice", "Frère", "Maman"):
        assert generate.bare_kin_word(bad), bad
    for ok in ("Uncle Vernon", "Father Brown", "Mother Superior", "Aunt Petunia", "Gabriel's father", "Mina", "Mr. Holmwood"):
        assert not generate.bare_kin_word(ok), ok


def test_bare_kin_words_are_not_entities(book, tmp_path):
    ix, report = run(book, tmp_path, _resp(new=[GABRIEL, _new("Mama", 3), _new("Father", 4), _new("the boy", 3)]))
    assert list(names(ix)) == ["gabriel-de-leon"]
    assert report["stats"][generate.KIN_NOT_NAME] == 3


def test_bare_kin_words_are_not_added_as_names(book, tmp_path):
    ch2 = _resp(names=[{"id": "gabriel-de-leon", "name": "Father", "p": 1}, {"id": "gabriel-de-leon", "name": "Mama", "p": 1}])
    ix, report = run(book, tmp_path, _resp(new=[GABRIEL]), ch2)
    assert names(ix)["gabriel-de-leon"] == ["Gabriel de Leon"]
    assert report["stats"][generate.KIN_NOT_NAME] == 2


def test_a_kin_word_with_a_proper_name_is_kept(book, tmp_path):
    ix, _ = run(book, tmp_path, _resp(new=[GABRIEL]), _resp(new=[_new("Uncle Vernon", 3)], updates=[
        {"id": "Uncle Vernon", "p": 3, "desc": "Sits silently by the cold hearth in the hall."}]))
    assert "uncle-vernon" in names(ix)


def test_add_name_refuses_a_bare_word_but_takes_a_kin_word_with_a_name(book, tmp_path):
    g = generate.Generator(epub.read(book), tg.FakeLLM(), generate.Options(), book, lambda m: None)
    g.entities["g"] = {"id": "g", "type": "person", "from": g.anchor(1), "names": [], "facets": []}
    g.entity_from["g"] = 1
    g._add_name("g", "Mama", "alias", 1)
    g._add_name("g", "Uncle Vernon", "alias", 1)
    assert [n["value"] for n in g.entities["g"]["names"]] == ["Uncle Vernon"]
    assert g.stats[generate.KIN_NOT_NAME] == 1


AMELIE_FIRST = _resp(
    new=[GABRIEL, _new("Amelie de Leon", 6)],
    relations=[{"a": "Amelie de Leon", "label": "sister of", "b": "Gabriel de Leon", "p": 6, "kind": "family"}])


def test_a_kin_description_resolves_to_the_named_relative(book_a, tmp_path):
    ch2 = _resp(new=[_new("Gabriel's sister", 2)], events=[
        {"p": 2, "text": "Gabriel's sister greets him in the courtyard of the keep.", "who": ["Gabriel's sister"], "weight": "plot"}])
    ix, report = run(book_a, tmp_path, AMELIE_FIRST, ch2)
    assert sorted(names(ix)) == ["amelie-de-leon", "gabriel-de-leon"]
    assert report["stats"]["kin descriptions matched to a known relative"] == 1
    assert ix["events"][0]["entities"] == ["amelie-de-leon"]


def test_a_converse_tie_also_identifies_the_relative(book_a, tmp_path):
    first = _resp(new=[GABRIEL, _new("Amelie de Leon", 6)],
                  relations=[{"a": "Gabriel de Leon", "label": "brother of", "b": "Amelie de Leon", "p": 6, "kind": "family"}])
    ix, report = run(book_a, tmp_path, first, _resp(new=[_new("Gabriel's sister", 2)]))
    assert sorted(names(ix)) == ["amelie-de-leon", "gabriel-de-leon"]
    assert report["stats"]["kin descriptions matched to a known relative"] == 1


def test_a_kin_description_that_fits_several_is_dropped(book_a, tmp_path):
    first = _resp(new=[GABRIEL, _new("Amelie de Leon", 6), _new("Celene de Leon", 6)], relations=[
        {"a": "Amelie de Leon", "label": "sister of", "b": "Gabriel de Leon", "p": 6, "kind": "family"},
        {"a": "Celene de Leon", "label": "sister of", "b": "Gabriel de Leon", "p": 6, "kind": "family"}])
    ix, report = run(book_a, tmp_path, first, _resp(new=[_new("Gabriel's sister", 2)]))
    assert sorted(names(ix)) == ["amelie-de-leon", "celene-de-leon", "gabriel-de-leon"]
    assert report["stats"]["dropped: kin description fits several"] == 1


def test_the_other_sex_is_not_a_fit(book_a, tmp_path):
    first = _resp(new=[GABRIEL, _new("Mr. Ezra Pike", 7)], relations=[
        {"a": "Mr. Ezra Pike", "label": "brother of", "b": "Gabriel de Leon", "p": 7, "kind": "family"}])
    ix, report = run(book_a, tmp_path, first, _resp(new=[_new("Gabriel's brother", 2), _new("Gabriel's sister", 2)]))
    assert report["stats"]["kin descriptions matched to a known relative"] == 1     # the brother is Mr. Pike
    assert report["stats"]["dropped: unnamed relative with no part"] == 1           # the sister is not, and has no part
    assert sorted(names(ix)) == ["gabriel-de-leon", "mr-ezra-pike"]


def test_an_unnamed_relative_needs_a_part_and_a_named_owner(book, tmp_path):
    ch1 = _resp(new=[GABRIEL, _new("Gabriel's sister", 3), _new("Gabriel's father", 5), _new("Gabriel's cousin", 5),
                     _new("Dior's mother", 3), _new("the old priest", 3)],
                updates=[{"id": "Gabriel's sister", "p": 3, "desc": "Waits for Gabriel at the inn in Aveline."}])
    ix, report = run(book, tmp_path, ch1)
    assert sorted(names(ix)) == ["gabriel-de-leon", "gabriels-sister"]
    stats = report["stats"]
    assert stats["dropped: unnamed relative with no part"] == 3          # father, cousin, old priest
    assert stats["dropped: unnamed relative of no one known"] == 1       # Dior is nobody yet


def _description_then_name(ch1_extra=None, ch2_extra=None):
    ch1 = _resp(new=[GABRIEL, _new("Gabriel's sister", 3)], **(ch1_extra or {}),
                updates=[{"id": "Gabriel's sister", "p": 3, "desc": "Waits for Gabriel at the inn in Aveline."}])
    ch2 = _resp(new=[_new("Amelie de Leon", 2)], relations=[
        {"a": "Amelie de Leon", "label": "sister of", "b": "Gabriel de Leon", "p": 2, "kind": "family"}], **(ch2_extra or {}))
    return ch1, ch2


def test_the_description_folds_into_the_relative_named_later(book, tmp_path):
    ix, report = run(book, tmp_path, *_description_then_name())
    assert list(names(ix)) == ["gabriel-de-leon", "amelie-de-leon"]
    assert report["stats"]["merged: kin description into the named relative"] == 1
    amelie = next(e for e in ix["entities"] if e["id"] == "amelie-de-leon")
    assert {n["value"] for n in amelie["names"]} == {"Gabriel's sister", "Amelie de Leon"}
    assert next(n["value"] for n in amelie["names"] if n["kind"] == "primary") == "Amelie de Leon"
    assert any("Waits for Gabriel" in f["text"] for f in amelie["facets"])
    # Spoiler rule: before the book names her she is still only "Gabriel's sister".
    at = lambda seq: [a["id"] for a in ix["anchors"] if a["seq"] <= seq][-1]  # noqa: E731
    early = view([ix], Position(None, at(4)))
    sister = next(e for e in early["entities"] if e["id"] == "amelie-de-leon")
    assert sister["name"] == "Gabriel's sister" and "Amelie" not in str(early)
    late = view([ix], Position(None, ix["coverage"]["through"]))
    assert next(e for e in late["entities"] if e["id"] == "amelie-de-leon")["name"] == "Amelie de Leon"


def test_no_fold_when_two_named_relatives_fit(book, tmp_path):
    ch1, ch2 = _description_then_name()
    ch2["new"].append(_new("Celene de Leon", 4))
    ch2["relations"].append({"a": "Celene de Leon", "label": "sister of", "b": "Gabriel de Leon", "p": 4, "kind": "family"})
    ix, report = run(book, tmp_path, ch1, ch2)
    assert len(ix["entities"]) == 4 and not report["stats"].get("merged: kin description into the named relative")


def test_no_fold_when_two_descriptions_stand_for_the_kin(book, tmp_path):
    g = generate.Generator(epub.read(book), tg.FakeLLM(), generate.Options(), book, lambda m: None)

    def person(eid, name, seq):
        g.entities[eid] = {"id": eid, "type": "person", "from": g.anchor(seq), "facets": [],
                           "names": [{"value": name, "kind": "primary", "from": g.anchor(seq)}]}
        g.entity_from[eid] = seq
    person("gabriel", "Gabriel de Leon", 1)
    person("amelie", "Amelie de Leon", 5)
    person("sister-a", "Gabriel's sister", 2)
    g.relations.append({"id": "r1", "src": "amelie", "dst": "gabriel", "label": "sister of"})
    g._fold_kin_descriptions()
    assert "sister-a" not in g.entities and g.entities["amelie"]["names"][-1]["value"] == "Gabriel's sister"   # one: folds
    person("sister-b", "Gabriel's sister", 3)
    person("sister-c", "Gabriel's elder sister", 4)
    person("celene", "Celene de Leon", 6)
    g.relations.append({"id": "r2", "src": "celene", "dst": "gabriel", "label": "sister of"})
    g._fold_kin_descriptions()
    assert {"sister-b", "sister-c", "celene"} <= set(g.entities)                                                # two descriptions: none


def test_no_fold_for_a_hidden_identity(book, tmp_path):
    ch1, ch2 = _description_then_name(ch1_extra=None)
    ch1["updates"].append({"id": "Gabriel's sister", "p": 3, "desc": "Her identity is a mystery to Gabriel and the town."})
    ix, report = run(book, tmp_path, ch1, ch2)
    assert len(ix["entities"]) == 3 and not report["stats"].get("merged: kin description into the named relative")


def test_no_fold_when_a_turning_point_names_both(book, tmp_path):
    ch1, ch2 = _description_then_name()
    ch2["events"] = [{"p": 2, "text": "Amelie de Leon is revealed to be the woman Gabriel called his sister.",
                      "who": ["Amelie de Leon", "Gabriel's sister"], "weight": "turn"}]
    ix, report = run(book, tmp_path, ch1, ch2)
    assert len(ix["entities"]) == 3 and not report["stats"].get("merged: kin description into the named relative")


def test_the_prompt_is_selective_about_people(book, tmp_path):
    _, _, llm = tg._run(book, tmp_path, llm=Script(_resp(), _resp()))
    prompt = next(c for c in llm.calls if "Update the companion" in c)
    assert 'Never a kin or role word alone ("Father", "Mother", "Mama"' in prompt


# ---- profiles --tidy ----------------------------------------------------------------------------

def test_tidy_removes_bare_kin_entities_and_folds_kin_descriptions():
    doc = tg_doc()
    t = profiles.tidy([doc])
    assert [e["id"] for e in doc["entities"]] == ["gabriel", "amelie", "gabriels-cousin", "uncle-vernon"]
    assert "Mama" in t["removed"] and "Father" in t["removed"]
    assert t["kin_folded"] == [("Gabriel's sister", "Amelie")]
    amelie = next(e for e in doc["entities"] if e["id"] == "amelie")
    assert {n["value"] for n in amelie["names"]} == {"Amelie", "Gabriel's sister"}
    assert [r for r in doc["relations"] if "mama" in (r["src"], r["dst"])] == []
    assert not [e for e in validate.check_index(doc) if e.level == "error"]


def tg_doc():
    from .test_profiles import _doc, _e
    doc = _doc("one", [
        _e("gabriel", ["Gabriel"], [("a1", "A silversaint.")]),
        _e("amelie", ["Amelie"], [("a4", "Gabriel's sister, a healer.")], frm="a4"),
        _e("gabriels-sister", ["Gabriel's sister"], [("a2", "Waits at the inn.")], frm="a2"),
        _e("mama", ["Mama"], [("a2", "Sent word from the north.")]),
        _e("father", ["Father", "the father"], [("a3", "Will not come.")]),
        _e("gabriels-cousin", ["Gabriel's cousin"], [("a3", "Laughs at supper.")]),      # no tie: kept
        _e("uncle-vernon", ["Uncle Vernon"], [("a5", "Sits by the hearth.")]),
    ])
    doc["relations"] = [
        {"id": "r1", "src": "amelie", "dst": "gabriel", "label": "sister of", "kind": "family", "from": "a4",
         "basis": "stated", "evidence": ["a4"]},
        {"id": "r2", "src": "mama", "dst": "gabriel", "label": "mother of", "kind": "family", "from": "a2",
         "basis": "stated", "evidence": ["a2"]}]
    return doc


def test_tidy_does_not_fold_a_kin_description_when_two_relatives_fit():
    doc = tg_doc()
    doc["entities"].append(__import__("tests.test_profiles", fromlist=["_e"])._e("celene", ["Celene"], [("a4", "Another sister.")], frm="a4"))
    doc["relations"].append({"id": "r3", "src": "celene", "dst": "gabriel", "label": "sister of", "kind": "family",
                             "from": "a4", "basis": "stated", "evidence": ["a4"]})
    t = profiles.tidy([doc])
    assert t["kin_folded"] == [] and "gabriels-sister" in [e["id"] for e in doc["entities"]]
