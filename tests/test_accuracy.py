"""Generator rules that keep an index faithful to the text: names from the passage only, couples as two
people, introductions at the first use of a name, facts that describe rather than narrate."""

import json

from lorekeeper import epub, generate, merge

from .test_generate import make_epub

CH1 = [
    "Mr. and Mrs. Marlow of the harbour house were proud to say they were perfectly normal.",
    "Every morning the Marlows walked past the lighthouse, where a quiet keeper named Ezra Pike lived alone.",
    "Their nephew Tom had vanished a year before, and Mrs. Marlow never spoke of him.",
    "That night Ezra Pike climbed the stairs and lit the great lamp as he always did.",
]


class Script:
    """Chapter one: a couple named together, a name the passage never uses, and a fact that is a moment."""
    name = "fake"

    def __init__(self):
        self.prompts = []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append((prefix or "") + prompt)
        props = schema.get("properties", {})
        if "new" in props:
            if "perfectly normal" in (prefix or ""):
                return {"new": [{"name": "Mr. and Mrs. Marlow", "type": "person", "p": 2, "desc": "", "desc_p": 2},
                                {"name": "Ezra Pike", "type": "person", "p": 5, "desc": "", "desc_p": 5},
                                {"name": "Thomas Albert Marlow", "type": "person", "p": 4, "desc": "", "desc_p": 4}],
                        "updates": [{"id": "Mr. and Mrs. Marlow", "p": 2, "desc": "Proud of being perfectly ordinary."},
                                    {"id": "Ezra Pike", "p": 5, "desc": "He climbs the stairs and lights the lamp."}],
                        "profiles": [], "names": [], "relations": [], "events": [], "summary": [], "recap": "The Marlows live by the sea."}
            return {"new": [], "updates": [], "profiles": [], "names": [], "relations": [], "events": [], "summary": [], "recap": ""}
        if "unsupported" in props:
            moment = [line.split(":")[0] for line in prompt.splitlines() if "lights the lamp" in line]
            return {"unsupported": [], "later": [], "moments": moment}
        return {}


def test_names_couples_introductions_and_moments(tmp_path):
    path = make_epub(tmp_path / "b.epub", ch1=CH1)
    llm = Script()
    out = tmp_path / "out.lore.json"
    generate.run(epub.read(path), llm, generate.Options(), path, out, log=lambda m: None)
    ix = json.loads(out.read_text())
    names = {n["value"]: e for e in ix["entities"] for n in e["names"]}
    assert "Mr. Marlow" in names and "Mrs. Marlow" in names and names["Mr. Marlow"] is not names["Mrs. Marlow"]
    for who in ("Mr. Marlow", "Mrs. Marlow"):                       # the couple's fact goes to both
        assert "Proud of being perfectly ordinary." in [f["text"] for f in names[who]["facets"]]
    assert "Thomas Albert Marlow" not in names                         # never used by the passage
    seq = {a["id"]: a["seq"] for a in ix["anchors"]}
    pike = names["Ezra Pike"]
    assert seq[pike["from"]] == 2                                      # first used in paragraph 2, not 4
    assert not any("lights the lamp" in f["text"] for f in pike["facets"])
    assert any("lights the lamp" in ev["summary"] and "ezra-pike" in ev["entities"] for ev in ix["events"])


def test_check_model_is_used_for_the_fact_check(tmp_path):
    path = make_epub(tmp_path / "b.epub", ch1=CH1)
    extract, check = Script(), Script()
    generate.run(epub.read(path), extract, generate.Options(check_llm=check), path, tmp_path / "o.lore.json", log=lambda m: None)
    assert any("Check each claim" in p for p in check.prompts)
    assert not any("Check each claim" in p for p in extract.prompts)


def test_proposed_pairs_from_descriptions():
    from .test_merge import _ent, _index
    ix = _index([_ent("mr-dursley", ["Mr. Dursley"], facets=[("a1", "Director of a drill firm, Dudley's father.")]),
                 _ent("vernon", ["Uncle Vernon"], "a2", facets=[("a2", "Harry's uncle and Dudley's father.")]),
                 _ent("dudley", ["Dudley"], facets=[("a1", "The Dursleys' son.")])])

    class Model:
        def complete(self, system, prompt, schema, prefix=None):
            rows = {line.split(":")[0]: line for line in prompt.splitlines() if line.startswith("E")}
            a = next(k for k, v in rows.items() if "Mr. Dursley" in v)
            b = next(k for k, v in rows.items() if "Uncle Vernon" in v)
            return {"pairs": [{"a": a, "b": b}, {"a": a, "b": a}]}
    pairs = merge.propose([ix], Model())
    assert [(p["keep"], p["drop"], p["sure"]) for p in pairs] == [("mr-dursley", "vernon", False)]
