from lorekeeper import epub, generate, merge
from lorekeeper.gating import Position, view

from .test_generate import make_epub


def test_name_forms():
    assert merge.extends("Dior", "Dior LaChance")
    assert merge.extends("Greyhand", "Frère Greyhand")
    assert merge.extends("Gabriel", "Gabriel de León")
    assert merge.extends("Isla", "Isla (Ever After)")              # parenthetical notes don't count
    assert not merge.extends("Gabriel", "Gabriel's daughter")      # a description, not a name
    assert not merge.extends("Voss", "Laure Voss")                 # surnames are for match_known, with context
    assert not merge.extends("Coste", "Coste Cathedral", person=False)
    assert not merge.extends("Capitaine Moulin", "Capitaine Mathieu")
    assert not merge.extends("Al", "Al Capone")                    # too short to be sure
    # A name's tail, and middle names or initials, are the same person.
    assert merge.extends("Van Helsing", "Abraham Van Helsing")
    assert merge.extends("Quincey Morris", "Quincey P. Morris")
    assert merge.extends("Quincey P. Morris", "Quincey Peter Morris")
    assert merge.same_person_name("Quincey Morris", "Quincey P. Morris")
    assert not merge.same_person_name("Quincey Morris", "Quincey Morris")      # identical is not "differs only in..."
    assert not merge.same_person_name("Quincey Morris", "Arthur Morris")
    assert not merge.extends("Quincey Paul Morris", "Quincey Peter Morris")    # different middle names
    assert not merge.extends("de", "Gabriel de")                               # particles alone say nothing
    assert not merge.extends("Mr. Morris", "Mrs. Quincey P. Morris")


def _ent(eid, names, frm="a1", etype="person", facets=()):
    return {"id": eid, "type": etype, "from": frm,
            "names": [{"value": n, "kind": "primary" if i == 0 else "alias", "from": frm} for i, n in enumerate(names)],
            "facets": [{"id": f"{eid}.{i}", "from": a, "text": t, "basis": "stated", "evidence": [a]} for i, (a, t) in enumerate(facets)]}


def _index(entities, relations=(), events=()):
    return {"lorekeeper": "0.1", "kind": "index", "id": "b", "work": {"title": "B", "authors": ["X"], "language": "en"},
            "editions": [], "coverage": {"complete": True, "through": "a4"},
            "provenance": {"contributors": [], "review": [], "created": "2026-01-01T00:00:00Z", "license": "CC0-1.0"},
            "anchors": [{"id": f"a{i}", "fp": f"{i:016x}"} for i in range(1, 5)],
            "entities": list(entities), "relations": list(relations), "events": list(events), "recaps": []}


def test_candidates_sure_and_unsure():
    ix = _index([_ent("dior", ["Dior"], "a1"), _ent("dior-lachance", ["Dior LaChance"], "a3"),
                 _ent("laure", ["Laure Voss"]), _ent("danton", ["Danton Voss"]),
                 _ent("chloe", ["Chloe"]), _ent("chloe-s", ["Chloe Sauvage"], "a2"), _ent("chloe-b", ["Chloe Brun"], "a2")])
    c = {(x["keep"], x["drop"]): x["sure"] for x in merge.candidates([ix])}
    assert c[("dior", "dior-lachance")] is True                   # earliest is kept
    assert not any("laure" in k or "danton" in k for k in c)      # a shared surname is not a match
    # "Chloe" fits two different Chloes: offered, but not sure.
    assert c[("chloe", "chloe-s")] is False and c[("chloe", "chloe-b")] is False
    assert merge.candidates([ix], {frozenset(("dior", "dior-lachance"))}) and \
        ("dior", "dior-lachance") not in {(x["keep"], x["drop"]) for x in merge.candidates([ix], {frozenset(("dior", "dior-lachance"))})}


def test_merge_keeps_reveal_points():
    ix = _index([_ent("dior", ["Dior"], "a1", facets=[("a1", "A street thief.")]),
                 _ent("dior-lachance", ["Dior LaChance"], "a3", facets=[("a3", "Gives a full name at last.")]),
                 _ent("gabriel", ["Gabriel"])],
                relations=[{"id": "r1", "src": "gabriel", "dst": "dior", "label": "protects", "from": "a2", "basis": "stated", "evidence": ["a2"]},
                           {"id": "r2", "src": "gabriel", "dst": "dior-lachance", "label": "protects", "from": "a3", "basis": "stated", "evidence": ["a3"]},
                           {"id": "r3", "src": "dior", "dst": "dior-lachance", "label": "same as", "from": "a3", "basis": "stated", "evidence": ["a3"]}],
                events=[{"id": "e1", "from": "a3", "summary": "They talk.", "entities": ["dior", "dior-lachance", "gabriel"], "evidence": ["a3"]}])
    assert merge.merge([ix], "dior", "dior-lachance") == [0]
    ids = [e["id"] for e in ix["entities"]]
    assert "dior-lachance" not in ids
    assert [r["id"] for r in ix["relations"]] == ["r1"]           # duplicate and self relations go
    assert ix["events"][0]["entities"] == ["dior", "gabriel"]
    # Nothing from the merged-in entity shows before it was introduced; afterwards the fuller name leads.
    early = view([ix], Position(None, "a2"))
    d = next(e for e in early["entities"] if e["id"] == "dior")
    assert d["name"] == "Dior" and [f["text"] for f in d["facets"]] == ["A street thief."]
    late = next(e for e in view([ix], Position(None, "a3"))["entities"] if e["id"] == "dior")
    assert late["name"] == "Dior LaChance" and len(late["facets"]) == 2


def test_merge_renames_in_later_books():
    book1 = _index([_ent("dior", ["Dior"])])
    book2 = _index([_ent("dior-lachance", ["Dior LaChance"], "a2")],
                   relations=[], events=[{"id": "e", "from": "a2", "summary": "x", "entities": ["dior-lachance"], "evidence": ["a2"]}])
    assert merge.merge([book1, book2], "dior", "dior-lachance") == [1]
    assert book2["entities"][0]["id"] == "dior" and book2["events"][0]["entities"] == ["dior"]


def test_generator_joins_fuller_names(tmp_path):
    path = make_epub(tmp_path / "b.epub")
    g = generate.Generator(epub.read(path), None, generate.Options(), path, lambda m: None)
    g.entities["alice"] = {"id": "alice", "type": "person", "from": "x", "names": [{"value": "Alice", "kind": "primary", "from": "x"}], "facets": []}
    g.entities["tom"] = {"id": "tom", "type": "person", "from": "x", "names": [{"value": "Tom Marlow", "kind": "primary", "from": "x"}], "facets": []}
    assert g.match_known("Alice Marlow", "person") == "alice"
    assert g.match_known("Sister Alice", "person") == "alice"
    assert g.match_known("Tom", "person") == "tom"
    assert g.match_known("Alice's brother", "person") is None


def test_merge_shared_name_revealed_earlier_in_the_dropped_entry():
    """Regression: a name both entries share must keep the earlier reveal, or entity.from has no name."""
    from lorekeeper import validate
    gab = _ent("gabriel-de-leon", ["Gabriel de León", "Gabe"], "a2")
    gab["names"][1]["from"] = "a3"
    ix = _index([gab, _ent("gabe", ["Gabe"], "a1")])
    ix["editions"] = [{"textHash": "0" * 64, "fileHash": "0" * 64}]
    merge.merge([ix], "gabriel-de-leon", "gabe")
    e = ix["entities"][0]
    assert e["from"] == "a1" and next(n for n in e["names"] if n["value"] == "Gabe")["from"] == "a1"
    assert not [x for x in validate.check_index(ix) if x.level == "error"]


def test_judge_maps_verdicts_to_pairs():
    ix = _index([_ent("kiara", ["Kiara Dyvok", "The Wolfmother"], facets=[("a1", "Rules the Dyvok court.")]),
                 _ent("wolfmother-kiara", ["Wolfmother Kiara"], "a2"),
                 _ent("philippe", ["Prince Philippe"]), _ent("philippe-cook", ["Philippe"], "a2", facets=[("a2", "The castle cook.")])])
    pairs = merge.candidates([ix])
    assert {(p["keep"], p["drop"]) for p in pairs} >= {("kiara", "wolfmother-kiara"), ("philippe", "philippe-cook")}

    class Judge:
        name = "fake"
        def __init__(self): self.prompts = []
        def complete(self, system, prompt, schema, prefix=None):
            self.prompts.append(prompt)
            out = []
            for block in prompt.split("\nP")[1:]:
                n = block.split(":")[0]
                out.append({"id": f"P{n}", "verdict": "different" if "cook" in block else "same"})
            return {"verdicts": out}

    j = Judge()
    v = merge.judge([ix], pairs, j, batch=1)
    assert v["kiara|wolfmother-kiara"] == "same" and v["philippe|philippe-cook"] == "different"
    assert len(j.prompts) == len(pairs) and "Rules the Dyvok court." in j.prompts[0]


def test_proper_name():
    for n in ("Aaron", "Van Helsing", "Sister Chloe", "Emperor Alexandre III", "Gabriel de León", "Celene Castia", "Lachlan á Craeg"):
        assert merge.proper_name(n), n
    for n in ("the Professor", "Gabriel's grandfather", "Forever King", "Emperor", "Isla (Ever After)", "the stranger"):
        assert not merge.proper_name(n), n


def test_judge_evidence_has_ties_profile_and_the_introduction_passage():
    from types import SimpleNamespace as NS
    ix = _index([_ent("aaron-de-coste", ["Aaron de Coste"], "a1", facets=[("a1", "Betrothed to Vivienne.")]),
                 _ent("aaron", ["Aaron"], "a3", facets=[("a3", "A young man.")]), _ent("vivienne", ["Vivienne"], "a1")],
                relations=[{"id": "r1", "src": "aaron-de-coste", "dst": "vivienne", "label": "betrothed to", "from": "a1",
                            "basis": "stated", "evidence": ["a1"]}])
    ix["entities"][0]["facets"].append({"id": "p1", "kind": "profile", "from": "a1", "basis": "stated", "evidence": ["a1"],
                                        "text": "x" * 400 + " PROFILE-END"})
    paras = ["Rain fell on the road.", "Then Aaron arrived with the news.", "Nobody spoke.",
             "Aaron de Coste and Aaron stood together.", "Later still, it ended."]
    book = NS(blocks=[NS(raw=t) for t in paras])
    seen = []

    class Judge:
        def complete(self, system, prompt, schema, prefix=None):
            seen.append(prompt)
            return {"verdicts": [{"id": "P1", "verdict": "same", "because": "same betrothed", "hidden": True}]}
    meta = {}
    pairs = [{"keep": "aaron-de-coste", "drop": "aaron"}]
    v = merge.judge([ix], pairs, Judge(), books=lambda doc: book, meta=meta)
    prompt = seen[0]
    assert v == {"aaron-de-coste|aaron": "same"}
    assert meta["aaron-de-coste|aaron"] == {"because": "same betrothed", "text": True, "hidden": True}
    assert "PROFILE-END" in prompt                                    # the profile in full
    assert "Aaron de Coste: betrothed to Vivienne" in prompt          # ties as sentences
    assert "Then Aaron arrived with the news." in prompt and "Nobody spoke." in prompt   # first use, with a paragraph either side
    assert "Rain fell" in prompt and "Later still" not in prompt
    assert "Both names occur in paragraph 4" in prompt                # a shared paragraph
    # Without the EPUB the judge still works and says so.
    meta.clear()
    merge.judge([ix], pairs, Judge(), meta=meta)
    assert meta["aaron-de-coste|aaron"]["text"] is False and "Where B's name first occurs" not in seen[-1]


def test_display_name_is_the_obvious_one():
    gabriel = _ent("gabriel-de-leon", ["Gabriel de León", "Gabe", "The Black Lion of Lorson"], "a1",
                   facets=[("a1", "Gabriel rides north."), ("a2", "Gabriel drinks."), ("a3", "Gabriel fights.")])
    killer = _ent("killer", ["Killer (narrator)"], "a3")
    ix = _index([gabriel, killer])
    merge.merge([ix], "gabriel-de-leon", "killer")
    e = ix["entities"][0]
    assert [n["value"] for n in e["names"] if n["kind"] == "primary"] == ["Gabriel de León"]
    assert view([ix], Position(None, "a4"))["entities"][0]["name"] == "Gabriel de León"
    # Ranking: clean over annotated, plain over epithets, the most-mentioned word, then the fullest.
    counts = {"kiara": 9, "dyvok": 2, "wolfmother": 3}
    assert merge.rank_names(["Wolfmother Kiara", "Kiara (the Wolfmother)", "Kiara Dyvok", "Kiara"], counts)[0] == "Kiara Dyvok"


def test_explicit_name_wins():
    ix = _index([_ent("dior", ["Dior"], "a1"), _ent("dior-lachance", ["Dior LaChance"], "a3")])
    merge.merge([ix], "dior", "dior-lachance", name="Dior")
    assert [n["value"] for n in ix["entities"][0]["names"] if n["kind"] == "primary"] == ["Dior"]


def test_family_name_is_not_evidence():
    """Relatives share a surname; a bare "Chastain" on two entries doesn't make them one."""
    ix = _index([_ent("margot", ["Margot Chastain", "Chastain"]), _ent("jf", ["Jean-François Chastain", "Chastain"], "a2"),
                 _ent("nicolette", ["Nicolette Chastain"], "a2"), _ent("dior", ["Dior"]), _ent("dior-l", ["Dior LaChance"], "a3")])
    pairs = {frozenset((p["keep"], p["drop"])) for p in merge.candidates([ix])}
    assert frozenset(("margot", "jf")) not in pairs
    assert frozenset(("dior", "dior-l")) in pairs          # a fuller form of a first name still matches


def test_actual_name_over_title():
    """A family surname marks a real name; a title word anywhere marks a title."""
    ix = _index([_ent("fabien", ["Forever King", "Fabién Voss", "The Forever King"]), _ent("danton", ["Danton Voss"]),
                 _ent("jf", ["Marquis Jean-François Chastain", "Jean-François Chastain"]), _ent("margot", ["Margot Chastain"])])
    s, c = merge.family_names([ix]), merge.word_counts([ix])
    assert merge.rank_names(["Forever King", "Fabién Voss", "The Forever King"], c, s)[0] == "Fabién Voss"
    assert merge.rank_names(["Marquis Jean-François Chastain", "Jean-François Chastain"], c, s)[0] == "Jean-François Chastain"
    assert "dior" not in merge.family_names([_index([_ent("dior", ["Dior", "San Dior"]), _ent("san-dior", ["San Dior"])])])


def test_titles_and_forms_of_a_name():
    assert not merge.extends("Mr. Dursley", "Mrs. Dursley")           # titles say two people
    assert not merge.extends("Lady Arlynn", "Lord Arlynn")
    assert merge.extends("Professor Snape", "Severus Snape")          # title + surname → full name
    assert merge.extends("Muggles", "Muggle", person=False)           # plural
    assert merge.extends("Mirror of Erised", "Erised mirror", person=False)
    assert not merge.extends("Gryffindor", "Gryffindor common room", person=False)
    assert not merge.extends("Hogwarts", "Hogwarts Express", person=False)


def test_generator_identity_rules(tmp_path):
    path = make_epub(tmp_path / "b.epub")
    g = generate.Generator(epub.read(path), None, generate.Options(), path, lambda m: None)
    def add(eid, names, etype="person"):
        g.entities[eid] = {"id": eid, "type": etype, "from": "x", "names": [{"value": n, "kind": "primary", "from": "x"} for n in names], "facets": []}
    add("mr-dursley", ["Mr. Dursley"]); add("snape", ["Professor Snape"]); add("hagrid", ["Hagrid"]); add("dudley", ["Dudley"])
    add("muggles", ["Muggles"], "group")
    assert g.match_known("Mrs. Dursley", "person") is None             # his wife, not him
    assert g.match_known("Severus Snape", "person") == "snape"
    assert g.match_known("Rubeus Hagrid", "person") == "hagrid"
    assert g.match_known("Dudley Dursley", "person") == "dudley"       # first name beats surname
    assert g.match_known("Muggle", "term") == "muggles"


# ---- generation-time identity: suffix forms, initials, bridging names, disambiguation -----------

PARAS = [
    "Quincey P. Morris came up to the old house from Texas with a bowie knife.",   # seq 1
    "Quincey Morris laughed at the doctor and told the whole room a story.",       # seq 2
    "Arthur sat quietly by the window and waited for the evening to come.",        # seq 3
    "Mr. Holmwood was the heir to a great estate and a grave, gentle man.",        # seq 4
    "Arthur Holmwood rose from his chair and greeted everyone in the room.",       # seq 5
    "Mr. Morris spoke last and told the others to keep their courage up.",         # seq 6
    "Ron Weasley grinned at his friends across the crowded kitchen table.",        # seq 7
]


class Script:
    """Scripted model: one extraction naming `new` and an answer to the disambiguation question."""
    name = "script"

    def __init__(self, new, choice="none"):
        self.new, self.choice, self.asked = new, choice, []

    def complete(self, system, prompt, schema, prefix=None):
        props = schema.get("properties", {})
        if "choice" in props:
            self.asked.append(prompt)
            return {"choice": self.choice}
        if "new" in props:
            return {"new": self.new, "updates": [], "profiles": [], "names": [], "relations": [],
                    "events": [], "summary": [], "recap": ""}
        raise AssertionError("unexpected call")


def _gen(tmp_path, llm=None, **opts):
    path = make_epub(tmp_path / "b.epub", ch1=PARAS)
    return generate.Generator(epub.read(path), llm, generate.Options(**{"verify": False, **opts}), path, lambda m: None)


def _put(g, eid, names, seq, facets=()):
    seq = g.snap_forward(seq)
    a = g.anchor(seq)
    g.entities[eid] = {"id": eid, "type": "person", "from": a,
                       "names": [{"value": n, "kind": "primary" if i == 0 else "alias", "from": a} for i, n in enumerate(names)],
                       "facets": [{"id": f"x.{eid}.{i}", "from": a, "text": t, "basis": "stated", "evidence": [a]}
                                  for i, t in enumerate(facets)]}
    g.entity_from[eid] = seq


def _new(name, p):
    return [{"name": name, "type": "person", "p": p, "desc": "", "desc_p": p}]


def _chunk():
    return generate.Chunk(0, 0, len(PARAS))


def test_generator_matches_tail_and_initials(tmp_path):
    g = _gen(tmp_path)
    _put(g, "abraham-van-helsing", ["Abraham Van Helsing"], 1)
    _put(g, "quincey-p-morris", ["Quincey P. Morris"], 1)
    assert g.match_known("Van Helsing", "person") == "abraham-van-helsing"
    assert g.match_known("Quincey Morris", "person") == "quincey-p-morris"
    assert g.match_known("Professor Van Helsing", "person") == "abraham-van-helsing"


def test_bridging_name_merges_mutually_matching_entities(tmp_path):
    llm = Script(_new("Mr. Morris", 7))
    g = _gen(tmp_path, llm)
    _put(g, "quincey-p-morris", ["Quincey P. Morris"], 1, ["A Texan with a bowie knife."])
    _put(g, "quincey-morris", ["Quincey Morris"], 2, ["Laughs at the doctor."])
    g.relations.append({"id": "r1", "src": "quincey-morris", "dst": "quincey-p-morris", "label": "x", "from": "z",
                        "basis": "stated", "evidence": []})
    g.relations.clear()   # a relation between the two would forbid the merge: tested below
    _put(g, "lucy", ["Lucy"], 3)
    g.relations.append({"id": "r2", "src": "lucy", "dst": "quincey-morris", "label": "friend of", "from": g.anchors[3],
                        "basis": "stated", "evidence": []})
    g.events.append({"id": "e1", "from": g.anchors[3], "summary": "They talk.", "entities": ["quincey-morris", "lucy"],
                     "evidence": []})
    g.process(_chunk())
    assert llm.asked == []                                       # no model call needed
    assert list(e for e in g.entities if "morris" in e) == ["quincey-p-morris"]
    e = g.entities["quincey-p-morris"]
    by_name = {n["value"]: n["from"] for n in e["names"]}
    assert set(by_name) == {"Quincey P. Morris", "Quincey Morris", "Mr. Morris"}
    assert by_name["Quincey Morris"] == g.anchors[g.snap_forward(2)]            # keeps its own reveal point
    assert e["from"] == g.anchors[g.snap_forward(1)]
    assert [f["text"] for f in e["facets"]] == ["A Texan with a bowie knife.", "Laughs at the doctor."]
    assert g.relations[0]["dst"] == "quincey-p-morris" and g.events[0]["entities"] == ["quincey-p-morris", "lucy"]
    assert g.stats["merged by a bridging name"] == 1
    assert any(a["kind"] == "merge" and a["drop"] == "quincey-morris" for a in g.added)
    # The resume state names only entities that exist.
    assert set(g.state()["entity_from"]) == set(g.entities)


def test_bridging_name_respects_relations_and_distinct(tmp_path):
    for opts, rel in (({}, True), ({"distinct": {frozenset(("quincey-p-morris", "quincey-morris"))}}, False)):
        llm = Script(_new("Mr. Morris", 7), "none")
        g = _gen(tmp_path, llm, **opts)
        _put(g, "quincey-p-morris", ["Quincey P. Morris"], 1)
        _put(g, "quincey-morris", ["Quincey Morris"], 2)
        if rel:
            g.relations.append({"id": "r1", "src": "quincey-morris", "dst": "quincey-p-morris", "label": "cousin of",
                                "from": g.anchors[2], "basis": "stated", "evidence": []})
        g.process(_chunk())
        assert {"quincey-p-morris", "quincey-morris"} <= set(g.entities)    # not merged
        assert len(llm.asked) == 1                                           # the model was asked instead
        assert not g.stats["merged by a bridging name"]


def test_disambiguation_both_merges(tmp_path):
    llm = Script(_new("Arthur Holmwood", 6), "both")
    g = _gen(tmp_path, llm)
    _put(g, "arthur", ["Arthur"], 3, ["Waits quietly by the window."])
    _put(g, "mr-holmwood", ["Mr. Holmwood"], 4, ["Heir to a great estate."])
    g.process(_chunk())
    assert "mr-holmwood" not in g.entities and "arthur-holmwood" not in g.entities
    e = g.entities["arthur"]
    assert {n["value"] for n in e["names"]} == {"Arthur", "Mr. Holmwood", "Arthur Holmwood"}
    assert {n["value"]: n["from"] for n in e["names"]}["Mr. Holmwood"] == g.anchors[g.snap_forward(4)]
    assert len(e["facets"]) == 2
    assert g.stats["disambiguated: merged"] == 1
    assert len(llm.asked) == 1
    ask = llm.asked[0]
    assert "A: Arthur" in ask and "B: Mr. Holmwood" in ask and "Arthur Holmwood rose from his chair" in ask
    assert "Ron Weasley" not in ask                                          # only the paragraphs around the name


def test_disambiguation_picks_one_and_leaves_the_other(tmp_path):
    llm = Script(_new("Ron Weasley", 8), "A")
    g = _gen(tmp_path, llm)
    _put(g, "ron", ["Ron"], 3)
    _put(g, "mr-weasley", ["Mr. Weasley"], 4)
    g.process(_chunk())
    assert set(g.entities) == {"ron", "mr-weasley"}
    assert {n["value"] for n in g.entities["ron"]["names"]} == {"Ron", "Ron Weasley"}
    assert [n["value"] for n in g.entities["mr-weasley"]["names"]] == ["Mr. Weasley"]
    assert g.stats["disambiguated: attached"] == 1


def test_disambiguation_none_creates_a_new_entity(tmp_path):
    llm = Script(_new("Ron Weasley", 8), "none")
    g = _gen(tmp_path, llm)
    _put(g, "ron", ["Ron"], 3)
    _put(g, "mr-weasley", ["Mr. Weasley"], 4)
    g.process(_chunk())
    assert "ron-weasley" in g.entities and g.stats["disambiguated: new"] == 1


def test_first_name_asks_about_titled_surname_in_the_chunk(tmp_path):
    llm = Script(_new("Arthur", 4), "A")
    g = _gen(tmp_path, llm)
    _put(g, "mr-holmwood", ["Mr. Holmwood"], 4)
    g.process(generate.Chunk(0, 0, 5))
    assert set(g.entities) == {"mr-holmwood"}
    assert {n["value"] for n in g.entities["mr-holmwood"]["names"]} == {"Mr. Holmwood", "Arthur"}
    assert len(llm.asked) == 1 and "A: Mr. Holmwood" in llm.asked[0]
    # Mr. Holmwood is known but not named in this chunk: nobody to ask about.
    llm2 = Script(_new("Arthur", 4), "A")
    g2 = _gen(tmp_path, llm2)
    _put(g2, "mr-holmwood", ["Mr. Holmwood"], 4)
    g2.process(generate.Chunk(0, 0, 3))
    assert llm2.asked == [] and "arthur" in g2.entities


def test_failed_answer_falls_back_to_plain_matching(tmp_path):
    llm = Script(_new("Arthur Holmwood", 6), "maybe")
    g = _gen(tmp_path, llm)
    _put(g, "arthur", ["Arthur"], 3)
    _put(g, "mr-holmwood", ["Mr. Holmwood"], 4)
    g.process(_chunk())
    assert set(g.entities) == {"arthur", "mr-holmwood"}      # as before this change: attached to the first name
    assert "Arthur Holmwood" in {n["value"] for n in g.entities["arthur"]["names"]}
    assert g.stats["disambiguation failed"] == 1


def test_titled_name_is_not_backdated_to_its_bare_surname(tmp_path):
    paras = ["Jonathan Harker wrote in his journal on the long road to the castle.",     # seq 1
             "The coach rattled on through the dark valley for many more hours.",         # seq 2
             "Mina Murray waited at home and wrote long letters to her friend Lucy.",     # seq 3
             "Years later Mrs. Harker sat by the fire and told the story once more."]     # seq 4
    path = make_epub(tmp_path / "b.epub", ch1=paras)
    llm = Script(_new("Mrs. Harker", 5))
    g = generate.Generator(epub.read(path), llm, generate.Options(verify=False), path, lambda m: None)
    g.process(generate.Chunk(0, 0, 4))
    e = g.entities["mrs-harker"]
    assert e["from"] == g.anchors[g.snap_forward(4)]          # not the journal in seq 1
    assert g.earliest_mention("Mrs. Harker", 4) == 4
    assert g.earliest_mention("Harker", 4) == 1               # untitled names still search the bare form


def test_family_surname_is_never_added_as_a_name(tmp_path):
    g = _gen(tmp_path)
    _put(g, "jonathan-harker", ["Jonathan Harker"], 1)
    _put(g, "mina-harker", ["Mina Harker"], 2)
    g._add_name("mina-harker", "Harker", "alias", 3)
    assert [n["value"] for n in g.entities["mina-harker"]["names"]] == ["Mina Harker"]
    assert g.stats["dropped: family surname, not a name"] == 1
    g._add_name("mina-harker", "Mina", "alias", 3)             # a first name is fine
    assert "Mina" in [n["value"] for n in g.entities["mina-harker"]["names"]]


class NamesScript(Script):
    """Also proposes `names` for known entities and lets a verifier reject the ones in `reject`."""

    def __init__(self, names, reject=()):
        super().__init__([])
        self.names, self.reject, self.claims = names, reject, ""

    def complete(self, system, prompt, schema, prefix=None):
        props = schema.get("properties", {})
        if "unsupported" in props:
            self.claims = prompt
            bad = [ln.split(":")[0] for ln in prompt.splitlines() if any(f'"{r}"' in ln for r in self.reject)]
            return {"unsupported": bad, "later": [], "moments": []}
        out = super().complete(system, prompt, schema, prefix)
        if "new" in props:
            out["names"] = self.names
        return out


def test_names_are_claims_the_checker_can_reject(tmp_path):
    names = [{"id": "arthur", "name": "Quincey Morris", "p": 3}, {"id": "arthur", "name": "Mr. Holmwood", "p": 5}]
    llm = NamesScript(names, reject=["Quincey Morris"])
    g = _gen(tmp_path, llm, verify=True)
    _put(g, "arthur", ["Arthur"], 3)
    g.process(_chunk())
    assert {n["value"] for n in g.entities["arthur"]["names"]} == {"Arthur", "Mr. Holmwood"}
    assert '(name, cited at paragraph' in llm.claims and 'Arthur is also called "Mr. Holmwood"' in llm.claims


def test_name_owned_by_another_entry_is_not_taken(tmp_path):
    g = _gen(tmp_path, NamesScript([{"id": "arthur", "name": "Mr. Holmwood", "p": 5}]), verify=False)
    _put(g, "arthur", ["Arthur"], 3)
    _put(g, "lord-godalming", ["Mr. Holmwood"], 4)
    g.process(_chunk())
    assert [n["value"] for n in g.entities["arthur"]["names"]] == ["Arthur"]
    assert g.stats["dropped: name belongs to another entry"] == 1


def test_two_full_names_with_different_surnames_never_merge(tmp_path):
    g = _gen(tmp_path)
    _put(g, "arthur", ["Arthur Holmwood"], 1)
    _put(g, "quincey", ["Quincey P. Morris"], 2)
    _put(g, "godalming", ["Lord Godalming"], 3)
    _put(g, "mr-holmwood", ["Mr. Holmwood"], 4)
    assert not g._may_merge(["arthur", "quincey"], "Mr. Morris")
    assert g._may_merge(["arthur", "godalming"])              # a titled single word is not a full name
    assert g._may_merge(["arthur", "mr-holmwood"])


def test_possessive_is_not_an_alias(tmp_path):
    names = [{"id": "mina", "name": "Mina Harker's Journal", "p": 3}, {"id": "mina", "name": "Gabriel's fear", "p": 3}]
    llm = NamesScript(names)
    g = _gen(tmp_path, llm)
    _put(g, "mina", ["Mina Harker"], 3, ["A schoolmistress."])
    g.process(_chunk())
    assert [n["value"] for n in g.entities["mina"]["names"]] == ["Mina Harker"]
    assert g.stats["dropped: description, not a name"] == 2


def test_name_claim_shows_what_is_known(tmp_path):
    llm = NamesScript([{"id": "seward", "name": "Ron Weasley", "p": 8}], reject=["Ron Weasley"])
    g = _gen(tmp_path, llm, verify=True)
    _put(g, "seward", ["Dr. John Seward"], 3, ["Runs an asylum."])
    g.process(_chunk())
    assert "known: Runs an asylum." in llm.claims
    assert [n["value"] for n in g.entities["seward"]["names"]] == ["Dr. John Seward"]


def test_titled_surname_does_not_outrank_the_full_name():
    names = ["Lucy Westenra", "Lucy", "Mrs. Westenra"]
    assert merge.rank_names(names, {"westenra": 5, "lucy": 9})[0] == "Lucy Westenra"
    assert merge.rank_names(["Mrs. Westenra", "Lucy Westenra"], {})[0] == "Lucy Westenra"
