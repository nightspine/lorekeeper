"""mentions.count on a synthetic book: the safety rules first (nothing past the position)."""

from types import SimpleNamespace as NS

from lorekeeper import mentions
from lorekeeper.gating import Position, view


def book(*paras):
    return [NS(seq=i, raw=t) for i, t in enumerate(paras)]


CH = [NS(start=0), NS(start=3), NS(start=6)]
B = book(
    "Harry Potter came in. Potter sat down.",       # 0  ch0
    "Mr. Potter, Lucy and Lucy's friend Lucy.",     # 1
    "Nothing here. The Count smiled.",              # 2
    "Lucy again. Counting the count, the Count.",   # 3  ch1
    "Harry looked at Harry.",                       # 4
    "She said Lucy.",                               # 5
    "Lucy Lucy Lucy.",                              # 6  ch2
    "Lucy.",                                        # 7
)
NAMES = {"harry": ["Harry Potter", "Harry"], "james": ["Potter"], "lucy": ["Lucy"], "dracula": ["Count"]}


def test_longest_name_wins_and_other_entity_counts_only_alone():
    m = mentions.count(B, CH, NAMES, 7)
    assert m.counts["harry"] == [1 + 0, 2, 0]  # "Harry Potter" in ch0; two bare "Harry" in ch1
    assert m.counts["james"] == [2, 0, 0]       # "Potter" alone (second sentence, "Mr. Potter"), not inside Harry Potter


def test_whole_words_only():
    m = mentions.count(B, CH, NAMES, 7)
    assert m.counts["dracula"][0] == 1          # "The Count"
    assert m.counts["dracula"][1] == 1          # "Counting" and lowercase "count" do not match
    assert m.counts["lucy"][:2] == [3, 2]       # possessive "Lucy's" counts


def test_current_chapter_cut_at_upto_seq():
    m = mentions.count(B, CH, NAMES, 4)
    assert m.current == 1 and m.counts["lucy"] == [3, 1] and m.counts["harry"][1] == 2
    m = mentions.count(B, CH, NAMES, 3)
    assert m.counts["lucy"] == [3, 1] and m.counts["harry"] == [1, 0]
    assert all(len(v) == 2 for v in m.counts.values())   # nothing for chapter 2


def test_before_first_chapter_and_front_matter():
    ch = [NS(start=2)]
    m = mentions.count(B, ch, NAMES, 1)
    assert m.current is None and all(v == [] for v in m.counts.values())
    m = mentions.count(B, ch, NAMES, 3)
    assert m.counts["lucy"] == [1]               # blocks 0-1 are front matter, not a chapter


def test_spans_only_for_current_chapter():
    m = mentions.count(B, CH, NAMES, 5)
    assert [s[0] for s in m.spans["lucy"]] == [3, 5]
    assert all(s[0] >= 3 for v in m.spans.values() for s in v)


def test_name_revealed_later_not_counted_earlier():
    idx = {
        "lorekeeper": "0.1", "kind": "index", "id": "t", "anchors": [{"id": "a0", "seq": 0}, {"id": "a1", "seq": 5}],
        "entities": [{"id": "stranger", "type": "person", "from": "a0",
                      "names": [{"value": "the stranger", "kind": "primary", "from": "a0"},
                                {"value": "Gabriel", "from": "a1"}]}],
        "coverage": {"through": "a1", "complete": True}, "relations": [], "events": [], "recaps": [],
    }
    blocks = book("Then the stranger came. Gabriel was his name.", "Gabriel!", "x", "Gabriel.")
    ch = [NS(start=0)]
    early = view([idx], Position(None, "a0"))
    assert mentions.visible_names(early) == {"stranger": ["the stranger"]}  # title-only: nothing derived
    assert mentions.count(blocks, ch, mentions.visible_names(early), 3).counts["stranger"] == [1]  # "Gabriel" is not visible yet
    late = view([idx], Position(None, "a1"))
    assert mentions.count(blocks, ch, mentions.visible_names(late), 3).counts["stranger"] == [4]


def test_parenthetical_notes_and_empty_names():
    m = mentions.count(B, CH, {"x": ["Lucy (the girl)", ""], "y": []}, 7)
    assert m.counts["x"] == [3, 2, 3 + 1] and m.counts["y"] == [0, 0, 0]
    assert mentions.count(B, CH, {}, 7).counts == {}


def test_snippets_sentence_offsets_and_abbreviations():
    raw = "Before it. Mr. Holmes said that Lucy was here. After."
    s = mentions.snippet(raw, raw.index("Lucy"), raw.index("Lucy") + 4)
    assert s["text"] == "Mr. Holmes said that Lucy was here."
    assert s["text"][s["start"]:s["end"]] == "Lucy"


def test_snippet_long_sentence_trimmed():
    raw = "word " * 100 + "Lucy" + " word" * 100 + "."
    i = raw.index("Lucy")
    s = mentions.snippet(raw, i, i + 4)
    assert len(s["text"]) <= mentions.SNIPPET_MAX and s["text"][s["start"]:s["end"]] == "Lucy"
    assert s["text"].startswith("…") and s["text"].endswith("…")


def test_snippets_dedupe_and_limit():
    blocks = book("Lucy and Lucy met.", *["Lucy."] * 70)
    m = mentions.count(blocks, [NS(start=0)], {"l": ["Lucy"]}, 70)
    sn = mentions.snippets({b.seq: b for b in blocks}, m.spans["l"])
    assert len(sn) == 60 and sn[-1]["seq"] == 70 and all(x["text"][x["start"]:x["end"]] == "Lucy" for x in sn)
    assert len([x for x in mentions.snippets({b.seq: b for b in blocks}, m.spans["l"], 100) if x["seq"] == 0]) == 1


def _ents(*specs):
    return [{"id": i, "type": t, "names": ns} for i, t, ns in specs]


def test_derived_given_name_and_surname():
    d = mentions.derive(_ents(("lucy", "person", ["Lucy Westenra"])))
    assert d == {"lucy": ["Lucy", "Westenra"]}
    b = book("Lucy was ill. Lucy Westenra said Westenra.")
    m = mentions.count(b, [NS(start=0)], {"lucy": ["Lucy Westenra", "Lucy", "Westenra"]}, 0)
    assert m.counts["lucy"] == [3]


def test_no_derived_form_shared_with_another_visible_entity():
    d = mentions.derive(_ents(("harry", "person", ["Harry Potter"]), ("james", "person", ["James Potter"]),
                              ("lily", "person", ["Lily Evans"])))
    assert d["harry"] == ["Harry"] and d["james"] == ["James"] and d["lily"] == ["Lily", "Evans"]
    d = mentions.derive(_ents(("lucy", "person", ["Lucy Westenra"]), ("mrs", "person", ["Mrs. Westenra"])))
    assert d == {"lucy": ["Lucy"]}   # "Westenra" is in both; Mrs. Westenra keeps only her spelled name


def test_titles_only_and_title_words_derive_nothing():
    assert mentions.derive(_ents(("p", "person", ["the Professor"]), ("c", "person", ["Count"]))) == {}
    d = mentions.derive(_ents(("d", "person", ["Count Dracula"])))
    assert d == {"d": ["Dracula"]}                      # not "Count"
    d = mentions.derive(_ents(("v", "person", ["Abraham Van Helsing"], ), ("w", "person", ["Dr. Seward"])))
    assert d["v"] == ["Abraham", "Helsing"] and d["w"] == ["Seward"]  # "Van" is a particle


def test_derived_forms_only_for_people_and_creatures():
    assert mentions.derive(_ents(("p", "place", ["Castle Dracula"]), ("t", "thing", ["Holy Grail"]))) == {}
    assert mentions.derive(_ents(("w", "creature", ["Black Fang"]))) == {"w": ["Black", "Fang"]}


def test_derived_forms_never_from_a_name_not_yet_visible():
    idx = {
        "lorekeeper": "0.1", "kind": "index", "id": "t", "anchors": [{"id": "a0", "seq": 0}, {"id": "a1", "seq": 5}],
        "entities": [{"id": "x", "type": "person", "from": "a0",
                      "names": [{"value": "the stranger", "kind": "primary", "from": "a0"},
                                {"value": "Gabriel Voss", "from": "a1"}]}],
        "coverage": {"through": "a1", "complete": True}, "relations": [], "events": [], "recaps": [],
    }
    assert mentions.visible_names(view([idx], Position(None, "a0"))) == {"x": ["the stranger"]}
    assert mentions.visible_names(view([idx], Position(None, "a1")))["x"][-2:] == ["Gabriel", "Voss"]
