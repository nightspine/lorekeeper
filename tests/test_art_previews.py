"""A book's own style previews (art.py): the opening scenes they may show (`opening_moments`, no call, the first two story
chapters only), the body written once from one of them (`preview_body`: the scene writer at the moment's anchor, no reference
pictures, nothing of an unfinished earlier book) and `preview_prompt(style, body)`."""

import pytest

from lorekeeper import art
from lorekeeper.web.jobs import is_story
from tests.test_art import (CH1, CH2, Image, Mem, Text, base_doc, book, doc, ent, event, pics, portrait, recap, run,  # noqa: F401
                            series_docs, short_gap)

CH3 = ["Years later Mira stood at the mill with Gabriel and the burning Zephyr tower lit the sky.",
       "Gabriel told Mira that Oren had been the King of the hills all along in secret.",
       "Oren wept by the wheel and confessed the crown to the whole village that night.",
       "The village gathered at the mill to see the old King take back his throne.",
       "Mira laid the silver coin on the stone and the Stranger came back for it.",
       "The Stranger lowered his hood at last and the village knew his face from long ago."]
FRONT = ["Contents of this book with every chapter listed below in their order for the reader to find.",
         "Chapter One, the mill and the gate. Chapter Two, the stranger at night. Chapter Three, the crown.",
         "A note on the text and on the many people who helped to put this little book together for you.",
         "The illustrations and the maps were made for this edition and are printed here with kind thanks."]


def three_chapters(front=False):
    parts = ([("Contents", FRONT), ("Part One", ["Part One."])] if front else []) + [("One", CH1), ("Two", CH2), ("Three", CH3)]
    bk = book(*parts)
    off = len(FRONT) + 1 if front else 0
    d = base_doc(bk)
    for x in d["events"] + d["recaps"]:                       # base_doc's anchors are chapter One's and Two's blocks
        x["from"] = f"a{int(x['from'][1:]) + off}"
        x["evidence"] = [x["from"]]
    for e in d["entities"]:
        e["from"] = f"a{int(e['from'][1:]) + off}"
        for n in e["names"]:
            n["from"] = e["from"]
        for f in e["facets"]:
            f["from"] = f"a{int(f['from'][1:]) + off}"
            f["evidence"] = [f["from"]]
    c3 = off + 12
    d["events"] += [event(7, c3 + 1, "Gabriel tells Mira that Oren was the King of the hills.", ["gabriel", "mira", "oren"], "turn"),
                    event(8, c3 + 5, "The Stranger lowers his hood and is known.", ["stranger"], "turn")]
    d["recaps"].append(recap(3, c3 + 5, "Three"))
    return bk, d


def illustrator(bk, d, sink=None, text_=None):
    return art.Illustrator([art.Book(d, bk, sink or Mem())], text_ or Text(), Image(), "ink", workers=1, log=lambda *_: None,
                           pause=0)


def test_the_opening_is_the_first_two_chapters_plates_first_then_the_other_events_in_reading_order():
    bk, d = three_chapters()
    il = illustrator(bk, d)
    ms = il.opening_moments(story=is_story)
    assert [(m.kind, m.target) for m in ms] == [("plate", "One"), ("plate", "Two"), ("event", "e1"), ("event", "e5"),
                                                ("event", "e6")]
    assert ms[0].event == "e2" and ms[0].anchor == "a5" and ms[0].summary == d["events"][1]["summary"]   # the turning point
    assert ms[2].anchor == "a0" and ms[2].chapter == "One" and ms[3].chapter == "Two"
    assert "e3" not in {m.target for m in ms}                                     # a minor event never
    assert not {m.target for m in ms} & {"Three", "e7", "e8"}                     # chapter three never
    assert art.Illustrator.default_moment(ms) is ms[0]
    assert il.text.prompts == []                                                  # no call


def test_a_chapter_three_moment_is_refused_even_when_asked_for():
    bk, d = three_chapters()
    il = illustrator(bk, d)
    for kind, target in (("plate", "Three"), ("event", "e7")):
        job = il.job(kind, target)
        with pytest.raises(ValueError, match="not an opening moment"):
            il.preview_body(art.Moment(kind, target, "Three", job.anchor, "", None))
    m = il.opening_moments()[0]
    with pytest.raises(ValueError, match="not an opening moment"):       # an opening target at a later anchor
        il.preview_body(art.Moment(m.kind, m.target, m.chapter, "a14", m.summary, m.event))
    assert il.text.prompts == []
    with pytest.raises(ValueError, match="not an opening moment"):       # one chapter: chapter two's moments are out too
        il.preview_body(il.opening_moments()[1], chapters=1)


def test_front_matter_and_a_part_divider_are_not_the_opening():
    bk, d = three_chapters(front=True)
    d["events"].insert(0, event(0, 0, "The contents list every chapter of the book in their order.", []))
    il = illustrator(bk, d)
    ms = il.opening_moments(story=is_story)
    assert {m.chapter for m in ms} == {"One", "Two"} and "e0" not in {m.target for m in ms}
    # every section counted as story: the contents and the divider take the two places, chapter Two is out
    assert {m.chapter for m in il.opening_moments()} <= {"Contents", "One"}


def test_an_index_that_starts_later_never_makes_a_later_chapter_its_opening():
    bk, d = three_chapters()
    d["events"] = [e for e in d["events"] if e["id"] in ("e7", "e8")]
    d["recaps"] = [r for r in d["recaps"] if r["title"] == "Three"]
    il = illustrator(bk, d)
    assert il.opening_moments(story=is_story) == []      # chapters One and Two hold nothing: no opening scene, not chapter three


def test_a_fresh_screening_narrows_the_moments_and_a_plate_with_no_event_is_told_by_its_recap():
    bk, d = three_chapters()
    il = illustrator(bk, d)
    one, two = il.books[0].chapters[:2]
    il.man[0]["screened"]["One"] = {"key": il._screen_key([e for e in one["events"] if e.get("kind") != "minor"]), "events": []}
    il.man[0]["screened"]["Two"] = {"key": il._screen_key([e for e in two["events"] if e.get("kind") != "minor"]), "events": ["e5"]}
    ms = il.opening_moments()
    assert [(m.kind, m.target, m.event) for m in ms] == [("plate", "One", None), ("plate", "Two", "e5")]
    assert ms[0].summary == d["recaps"][0]["text"].split(". ")[0].rstrip(".") + "." and ms[0].anchor == "a5"
    assert art.Illustrator.default_moment(ms) is ms[1]   # the first that shows an event


def test_the_body_has_nothing_past_the_moment_and_no_reference_pictures():
    bk, d = three_chapters()
    il, rep, sink, t, im = run(d, bk, level="all")       # the set holds portraits of everyone: none may be sent
    assert pics(sink, "portrait")
    calls = len(im.calls)
    t.prompts.clear(), t.systems.clear()
    m = il.opening_moments(story=is_story)[0]            # chapter One's plate, Mira and Oren in it
    pr = il.preview_body(m)
    assert pr.references == [] and len(im.calls) == calls                 # nothing attached, nothing drawn
    assert pr.anchor == m.anchor == "a5" and pr.shape == art.PREVIEW_SHAPE
    writer = t.scenes()[0]
    assert 'figure="A"' in writer and 'portrait="yes"' not in writer
    assert pr.figures and all(f.get("portrait") is False for f in pr.figures)
    assert "Stranger" not in writer and "Zephyr" not in writer and "King" not in writer
    o = {a["id"]: n for n, a in enumerate(d["anchors"])}
    assert all(o[a] <= o["a5"] for a in pr.sources)
    assert pr.body and art.preview_prompt("ink", pr.body) == f"{pr.body} {art.STYLES['ink']} {art.NO_TEXT}"


def test_a_series_books_body_uses_nothing_of_the_earlier_book():
    bk1, d1, bk2, d2 = series_docs()
    t = Text()
    il = art.Illustrator([art.Book(d1, bk1, Mem(), None, draw=False), art.Book(d2, bk2, Mem())], t, Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0)
    m = art.Illustrator.default_moment(il.opening_moments(story=is_story))
    assert m.bi == 1 and m.chapter == "Three"
    pr = il.preview_body(m)
    writer = t.scenes()[0]
    # book one's look of Mira (her "earlier look" for a reader who finished it) never reaches it
    assert "brown shawl" not in writer and "<earlier_look" not in writer and "Stranger" not in writer and "Gabriel" not in writer
    assert pr.inherits == [] and pr.partial and pr.references == []
    t.prompts.clear(), t.systems.clear()
    assert "brown shawl" in il.make_prompt(il.job(m.kind, m.target, 1, finished={"b1"})).check   # the control: it would


def test_preview_prompt_keeps_the_alice_scene_by_default():
    assert art.preview_prompt("ink") == f"{art.PREVIEW_SCENE} {art.STYLES['ink']} {art.NO_TEXT}"
    assert art.preview_prompt("Pastel crayon on brown paper.", "A girl at a gate.") == \
        f"A girl at a gate. Pastel crayon on brown paper. {art.NO_TEXT}"
