"""Book arcs (arcs.py) with a scripted model: who is due, what is sent, the checks, the facet it writes."""

import json
import re
from pathlib import Path

from lorekeeper import arcs, validate


def _doc(n=12, events=(), recaps=(), relations=(), book="bk", profile=None):
    anchors = [{"id": f"a{i}", "seq": i, "fp": f"{i:016x}"} for i in range(n)]
    facets = [{"id": "m.p", "kind": "profile", "from": "a0", "basis": "stated", "evidence": ["a0"], "text": profile}] if profile else []
    ents = [{"id": "mira", "type": "person", "from": "a0", "names": [{"value": "Mira", "kind": "primary", "from": "a0"}], "facets": facets},
            {"id": "oren", "type": "person", "from": "a0", "names": [{"value": "Oren", "kind": "primary", "from": "a0"}], "facets": []},
            {"id": "mill", "type": "place", "from": "a0", "names": [{"value": "the mill", "kind": "primary", "from": "a0"}], "facets": []}]
    evs = [{"id": f"e{i}", "from": f"a{at}", "summary": s, "entities": ents_, "evidence": [f"a{at}"], **({"kind": k} if k else {})}
           for i, (at, s, ents_, k) in enumerate(events)]
    rcs = [{"id": f"r{i}", "from": f"a{at}", "text": "A chapter recap of some length here.", "evidence": [f"a{at}"], "scope": "chapter",
            "title": f"Chapter {i + 1}"} for i, at in enumerate(recaps)]
    return {"id": book, "work": {"title": "Millbrook"}, "anchors": anchors, "entities": ents, "events": evs, "recaps": rcs,
            "relations": list(relations), "coverage": {"through": f"a{n - 1}"}}


class Fake:
    """Scripted replies per character (the entities are written in parallel): a list is for Mira, a dict maps a
    name to its replies; an empty queue answers with the last one given."""

    def __init__(self, replies, default=None):
        self.replies = dict(replies) if isinstance(replies, dict) else {"Mira": list(replies)}
        self.default = default or "Oren stays where he is and waits for news."
        self.prompts = []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
        name = re.search(r"that involve (\w+)", prompt).group(1)
        q = self.replies.get(name)
        return {"text": q.pop(0) if q else self.default}


GOOD = "Mira walks to the mill and meets Oren. She learns the secret of her birth."
EVENTS = [(1, "Mira walks to the mill with a basket.", ["mira", "mill"], None),
          (2, "Mira meets the miller Oren at the gate.", ["mira", "oren"], None),
          (3, "A tiny errand takes Mira to the well.", ["mira"], "minor"),
          (5, "Mira learns the secret of her birth from an old woman.", ["mira"], "turn"),
          (8, "Oren is taken away by soldiers.", ["oren", "mira"], None),
          (9, "Mira follows the soldiers to the keep.", ["mira"], None)]


def arc_facets(d, eid="mira"):
    return [f for e in d["entities"] if e["id"] == eid for f in e["facets"] if f.get("kind") == "arc"]


def test_arc_written_at_a_chapter_end_with_from_evidence_and_id():
    d = _doc(events=EVENTS, recaps=[6, 11])
    m = Fake([GOOD, "Mira follows Oren to the keep after soldiers take him away."])
    st = arcs.arcs([(d, None)], m, log=lambda *_: None)
    a = arc_facets(d)
    assert [f["id"] for f in a] == ["bk.mira.arc-1", "bk.mira.arc-2"] and st["arcs"] >= 2
    assert a[0]["kind"] == "arc" and a[0]["from"] == "a5" and a[0]["text"] == GOOD
    assert a[0]["evidence"] == ["a1", "a2", "a5"] and "replaces" not in a[0]
    assert a[1]["replaces"] == ["bk.mira.arc-1"] and a[1]["from"] == "a9"
    assert max(len(f["text"]) for f in a) <= 400


def test_prompt_has_only_events_up_to_the_chapter_end_and_no_minor_ones():
    d = _doc(events=EVENTS, recaps=[6, 11])
    m = Fake([GOOD] * 4)
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    first = m.prompts[0]
    assert "secret of her birth" in first and "tiny errand" not in first and "soldiers" not in first
    assert "turning point" in first and "chapter 1" in first


def test_an_entity_needs_two_events_or_a_turning_point_and_places_get_none():
    d = _doc(events=[(1, "Oren sells grain at the market.", ["oren", "mill"], None),
                     (7, "Oren is taken away by soldiers.", ["oren"], "turn")], recaps=[4, 11])
    m = Fake([GOOD] * 3)
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert arc_facets(d, "oren") and not arc_facets(d, "mira") and not arc_facets(d, "mill")
    assert arc_facets(d, "oren")[0]["from"] == "a7" and len(m.prompts) == 1


def test_a_second_arc_waits_for_two_new_events_or_a_turning_point():
    d = _doc(events=[(1, "Mira walks to the mill with a basket.", ["mira"], None),
                     (2, "Mira meets the miller Oren at the gate.", ["mira"], None),
                     (5, "Mira goes to the market alone.", ["mira"], None),
                     (8, "Mira is robbed on the road.", ["mira"], None)], recaps=[3, 6, 11])
    m = Fake([GOOD, "Mira meets Oren, goes to the market and is robbed on the road."])
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert [f["from"] for f in arc_facets(d)] == ["a2", "a8"]   # one new event at the second end, two at the third
    assert "Your summary from earlier" in m.prompts[1] and GOOD in m.prompts[1]


def test_rerun_continues_from_the_existing_arc_and_redo_clears():
    d = _doc(events=EVENTS, recaps=[6, 11])
    arcs.arcs([(d, None)], Fake([GOOD, "Mira follows Oren to the keep."]), log=lambda *_: None)
    m = Fake([])
    st = arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert m.prompts == [] and st.get("arcs", 0) == 0
    assert arcs.clear(d) == 3 and not arc_facets(d)


def test_checks_rewrite_once_then_drop():
    d = _doc(events=EVENTS, recaps=[11])
    # a name the index never revealed, then a good rewrite
    m = Fake(["Mira meets Zelda at the mill and learns who she is.", GOOD])
    st = arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert st["rewritten: unknown name"] == 1 and arc_facets(d)[0]["text"] == GOOD
    assert "Zelda" in m.prompts[1]
    # hindsight wording twice: dropped
    d = _doc(events=EVENTS, recaps=[11])
    st = arcs.arcs([(d, None)], Fake(["Mira is now a miller at the mill.", "Mira is now the miller at the mill."]), log=lambda *_: None)
    assert not arc_facets(d) and st["dropped: wording"] == 1
    # a tie the events never state
    d = _doc(events=EVENTS, recaps=[11])
    st = arcs.arcs([(d, None)], Fake(["Mira finds her mother at the mill.", "Mira finds a stranger at the mill."]), log=lambda *_: None)
    assert st["rewritten: tie not in the text"] == 1 and "stranger" in arc_facets(d)[0]["text"]


def test_length_sentences_and_talk_about_the_text_are_dropped():
    d = _doc(events=EVENTS, recaps=[11])
    long = "Mira walks to the mill. " * 30
    st = arcs.arcs([(d, None)], Fake([long, long]), log=lambda *_: None)
    assert not arc_facets(d) and st["dropped: too long"] == 1
    d = _doc(events=EVENTS, recaps=[11])
    four = "Mira walks. Mira runs far. Mira sits down. Mira sleeps well."
    st = arcs.arcs([(d, None)], Fake([four, GOOD]), log=lambda *_: None)
    assert st["rewritten: too many sentences"] == 1
    d = _doc(events=EVENTS, recaps=[11])
    st = arcs.arcs([(d, None)], Fake(["This passage shows Mira at the mill.", "This passage shows Mira at the mill."]), log=lambda *_: None)
    assert st["dropped: about the text"] == 1


def test_names_come_from_earlier_books_and_the_profile_is_context_only():
    prior = _doc(book="b0", profile="Mira is a miller's ward from Brook End.")
    d = _doc(events=EVENTS, recaps=[11])
    m = Fake(["Mira walks home to Brook End and meets Oren at the mill."])
    arcs.arcs([(d, None)], m, log=lambda *_: None, prior=[prior])
    assert "Brook End" in m.prompts[0] and arc_facets(d)   # the earlier book's description is context and its names are known


def test_arcs_are_not_details_for_the_other_stages():
    from lorekeeper import audit
    d = _doc(events=EVENTS, recaps=[11])
    arcs.arcs([(d, None)], Fake([GOOD]), log=lambda *_: None)
    resolved = {a["id"]: a["seq"] for a in d["anchors"]}
    assert all(GOOD not in c.text for c in audit.claims_of(d, resolved))


def test_marks_without_recaps_are_spaced_evenly_and_the_last_is_coverage():
    d = _doc(n=40, events=[])
    m = arcs.marks_for(d)
    assert len(m) >= 3 and m[-1] == 39


def test_sherlock_example_with_a_fake_model_stays_valid():
    p = Path("examples/sherlock-holmes/study-in-scarlet.json")
    d = json.loads(p.read_text())
    arcs.clear(d)
    names = {n["value"] for e in d["entities"] for n in e["names"]}
    m = Fake(["Holmes meets Watson and they agree to share rooms."] * 40)
    st = arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert st["arcs"] > 0
    assert not [e for e in validate.check_index(d) if e.level == "error"]
    assert names


def test_judge_entries_do_not_list_arcs_as_facts():
    from lorekeeper import merge
    d = _doc(events=EVENTS, recaps=[11])
    arcs.arcs([(d, None)], Fake([GOOD]), log=lambda *_: None)
    assert arc_facets(d)
    assert all(GOOD not in f for f in merge._entities([d])["mira"]["facts"])
