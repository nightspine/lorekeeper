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
        text = q.pop(0) if q else self.default
        if isinstance(text, dict):
            return text
        return {"text": text, "told": [int(n) for n in re.findall(r"^(\d+)\. ", prompt, re.M)]}


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


def test_an_earlier_books_profile_is_context_not_support():
    prior = _doc(book="b0", profile="Mira is a miller's ward from Brook End.")
    d = _doc(events=EVENTS, recaps=[11])
    m = Fake(["Mira walks home to Brook End and meets Oren at the mill.", "Mira returns to Brook End to meet Oren."])
    st = arcs.arcs([(d, None)], m, log=lambda *_: None, prior=[prior])
    assert "Brook End" in m.prompts[0]          # shown as context
    assert not arc_facets(d) and st["dropped: unknown name"] == 1   # but an arc may not tell it
    d = _doc(events=EVENTS, recaps=[11])
    arcs.arcs([(d, None)], Fake(["Mira meets Oren at the mill and learns her secret."]), log=lambda *_: None, prior=[prior])
    assert arc_facets(d)


def test_a_name_first_given_after_the_mark_is_never_shown():
    d = _doc(events=EVENTS, recaps=[11])
    d["entities"][0]["names"][0]["from"] = "a10"   # Mira's only name appears after every event
    d["entities"][0]["from"] = "a10"
    m = Fake([])
    st = arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert not [p for p in m.prompts if "Mira" in p.split("Events:")[0]] and not arc_facets(d)
    # inherited from an earlier book, it has a name already
    prior = _doc(book="b0")
    d = _doc(events=EVENTS, recaps=[11])
    d["entities"][0]["names"][0] = {"value": "Mira Dane", "kind": "alias", "from": "a10"}
    d["entities"][0]["from"] = "a0"
    m = Fake({"Mira": ["Mira meets Oren at the mill."]})
    arcs.arcs([(d, None)], m, log=lambda *_: None, prior=[prior])
    assert arc_facets(d) and "Mira Dane" not in m.prompts[0]


def test_a_group_gets_an_arc():
    d = _doc(events=[(1, "The guild shuts the mill gates.", ["guild"], None), (4, "The guild takes the miller's grain.", ["guild"], None)], recaps=[11])
    d["entities"].append({"id": "guild", "type": "group", "from": "a0", "names": [{"value": "the Guild", "kind": "primary", "from": "a0"}], "facets": []})
    arcs.arcs([(d, None)], Fake({"the": ["The Guild shuts the mill gates and takes the grain."]}), log=lambda *_: None)
    assert arc_facets(d, "guild")


def test_evidence_is_exactly_what_the_told_events_cite():
    d = _doc(events=EVENTS, recaps=[11])
    m = Fake([{"text": GOOD, "told": [2, 3]}])
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    f = arc_facets(d)[0]
    assert f["evidence"] == ["a2", "a5"] and f["from"] == "a9"   # events 2 and 3 (the turning point) cited, nothing else
    # more than 12 anchors are kept
    many = [(i, f"Mira does thing number {i} at the mill.", ["mira"], None) for i in range(1, 15)]
    d = _doc(n=20, events=many, recaps=[15])
    arcs.arcs([(d, None)], Fake(["Mira does thing after thing at the mill."]), log=lambda *_: None)
    assert len(arc_facets(d)[0]["evidence"]) == 14


def test_no_valid_told_list_gets_one_rewrite_then_counts_every_event():
    d = _doc(events=EVENTS, recaps=[11])
    m = Fake([{"text": GOOD}, {"text": GOOD, "told": [99]}])
    st = arcs.arcs([(d, None)], m, log=lambda *_: None)
    f = arc_facets(d)[0]
    mine = [p for p in m.prompts if "involve Mira" in p]
    assert st["rewritten: no list of told events"] == 1 and len(mine) == 2 and "Also return" in mine[1]
    assert f["evidence"] == ["a1", "a2", "a5", "a8", "a9"]


def test_epub_chapter_ends_work_for_anchors_without_seq():
    from lorekeeper import epub, text
    paras = [f"Mira walked to the mill with her basket number {i} and waited by the gate." for i in range(10)]
    blocks = [epub.Block(seq=i, spine=0, words=text.words(p), raw=p) for i, p in enumerate(paras)]
    book = epub.Book(blocks=blocks, chapters=[epub.Chapter(title="One", start=0), epub.Chapter(title="Two", start=5)])
    d = _doc(n=10, events=[])
    d["anchors"] = [{"id": f"a{i}", "fp": blocks[i].fingerprint} for i in range(10)]
    d["coverage"] = {"through": "a9"}
    assert arcs.marks_for(d, book) == [4, 9]
    d["anchors"][4]["fp"] = "0" * 16   # one that does not resolve takes the next one's place
    assert arcs.marks_for(d, book) == [3, 9]


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


def test_a_tie_to_an_entity_with_no_visible_name_is_left_out():
    rel = {"id": "r1", "src": "mira", "dst": "oren", "label": "ward of", "from": "a2", "evidence": ["a2"]}
    d = _doc(events=EVENTS, recaps=[11], relations=[rel])
    d["entities"][1]["names"][0]["from"] = "a10"   # Oren is named only after the events
    m = Fake({"Mira": ["Mira walks to the mill and learns her secret."]})
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert arc_facets(d) and "ward of" not in m.prompts[0]
    d = _doc(events=EVENTS, recaps=[11], relations=[rel])
    m = Fake({"Mira": ["Mira walks to the mill and learns her secret."]})
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert "Mira, ward of, Oren" in [p for p in m.prompts if "involve Mira" in p][0]


def test_events_prompted_but_not_told_stay_new():
    ev = [(1, "Mira walks to the mill with a basket.", ["mira"], None),
          (2, "Mira meets the miller Oren at the gate.", ["mira"], None),
          (4, "Mira is robbed on the road.", ["mira"], None),
          (5, "Mira loses her basket in the river.", ["mira"], None),
          (8, "Mira reaches the keep at dusk.", ["mira"], None)]
    d = _doc(events=ev, recaps=[3, 4, 11])
    # the first arc tells only event 1 of the two prompted; at the next end only one event is later than
    # event 2, but event 2 was never told, so two are new and the arc is rewritten
    m = Fake({"Mira": [{"text": "Mira walks to the mill with her basket.", "told": [1]},
                       {"text": "Mira walks to the mill, meets Oren and is robbed on the road.", "told": [1, 2, 3]}]})
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    assert [f["from"] for f in arc_facets(d)][:2] == ["a2", "a4"]


def test_from_is_the_newest_event_given_not_the_newest_told():
    d = _doc(events=EVENTS, recaps=[11])
    arcs.arcs([(d, None)], Fake([{"text": "Mira walks to the mill with her basket.", "told": [1]}]), log=lambda *_: None)
    f = arc_facets(d)[0]
    assert f["evidence"] == ["a1"] and f["from"] == "a9"   # everything the prompt held is visible at a9


def test_a_new_arc_is_never_before_the_arc_it_replaces():
    d = _doc(events=EVENTS, recaps=[6, 11])
    m = Fake([GOOD, {"text": "Mira follows Oren to the keep.", "told": [1, 2]}])
    arcs.arcs([(d, None)], m, log=lambda *_: None)
    a = arc_facets(d)
    assert len(a) == 2 and a[1]["replaces"] == [a[0]["id"]] and int(a[1]["from"][1:]) >= int(a[0]["from"][1:])


def test_abbreviations_and_initials_do_not_split_sentences():
    assert arcs.sentences("Mr. Harker meets Dr. Seward at St. Mary's. J. Harker leaves.") == [
        "Mr. Harker meets Dr. Seward at St. Mary's.", "J. Harker leaves."]
    d = _doc(events=EVENTS, recaps=[11])
    st = arcs.arcs([(d, None)], Fake(["Mira meets Mr. Oren at the mill. She learns her secret from Dr. Oren."]), log=lambda *_: None)
    assert arc_facets(d) and not st.get("dropped")


def test_a_rerun_without_redo_never_repeats_an_id():
    d = _doc(events=EVENTS, recaps=[6, 11])
    arcs.arcs([(d, None)], Fake([GOOD]), log=lambda *_: None)
    d["events"] += [{"id": "e9", "from": "a10", "summary": "Mira sails away from the keep.", "entities": ["mira"], "evidence": ["a10"], "kind": "turn"}]
    d["coverage"]["through"] = "a11"
    arcs.arcs([(d, None)], Fake(["Mira follows Oren to the keep and sails away."]), log=lambda *_: None)
    ids = [f["id"] for f in arc_facets(d)]
    assert len(ids) == len(set(ids)) >= 2


def test_the_copy_rule_applies_when_a_book_is_given():
    from lorekeeper import epub, text
    paras = [f"Mira walked to the mill with her basket number {i} and waited by the gate." for i in range(12)]
    blocks = [epub.Block(seq=i, spine=0, words=text.words(p), raw=p) for i, p in enumerate(paras)]
    book = epub.Book(blocks=blocks, chapters=[epub.Chapter(title="One", start=0)])
    d = _doc(events=EVENTS, recaps=[11])
    d["anchors"] = [{"id": f"a{i}", "seq": i, "fp": blocks[i].fingerprint} for i in range(12)]
    copy = "Mira walked to the mill with her basket number 3 and waited by the gate."
    st = arcs.arcs([(d, book)], Fake([copy, copy]), log=lambda *_: None)
    assert not arc_facets(d) and st["dropped: copies the book"] == 1
    d = _doc(events=EVENTS, recaps=[11])
    arcs.arcs([(d, None)], Fake([copy]), log=lambda *_: None)   # no book: the check is skipped
    assert arc_facets(d)


def test_a_valid_first_reply_survives_a_failed_rewrite_for_the_missing_told_list():
    d = _doc(events=EVENTS, recaps=[11])
    m = Fake([{"text": GOOD}, {"text": "Mira meets Zelda at the mill."}])
    st = arcs.arcs([(d, None)], m, log=lambda *_: None)
    f = arc_facets(d)[0]
    assert f["text"] == GOOD and f["evidence"] == ["a1", "a2", "a5", "a8", "a9"]


def test_drop_named_removes_only_the_named_arcs_and_their_references():
    d = _doc(events=EVENTS, recaps=[6, 11])
    arcs.arcs([(d, None)], Fake([GOOD, "Mira follows Oren to the keep."]), log=lambda *_: None)
    a = arc_facets(d)
    assert arcs.drop_named(d, [f"bk: {a[0]['id']} is revealed later"]) == 1
    left = arc_facets(d)
    assert [f["id"] for f in left] == [a[1]["id"]] and "replaces" not in left[0]
