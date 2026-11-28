"""What each of the person's reference pictures applies to (`UserRef.applies`): the whole picture ("scene", the default and
the lines of #170), the subject of a portrait, place or thing ("subject"), or one labelled figure of a scene ("A".."F"). The
note line says so, the closing line is scoped when a picture is tied to a figure, `draw` falls back to the whole picture when
the choice does not hold for the text being drawn (and says which), and the record keeps the value as used, only when it is
not the whole picture."""

import pytest

from lorekeeper import art
from tests.test_art import pics, png  # noqa: F401
from tests.test_art_style_ref import STYLE_PNG, moody, ref  # noqa: F401
from tests.test_art_user_refs import Eye, blind_image, drawn, uref  # noqa: F401


def tied(i, note="", applies="scene", description=None):
    u = uref(i, note, description)
    return art.UserRef(u.data, u.mime, u.sha, u.note, u.description, applies)


# ---- the note words


def test_each_note_line_says_what_the_picture_is_for():
    line = art.user_ref_line
    assert line(3, "her dress") == "Reference picture 3 is from the person: her dress."
    assert line(3, "") == "Reference picture 3 is from the person: draw what the prompt describes with its help."
    assert line(2, "use this for his coat.", "A") == (
        "Reference picture 2 is from the person and is only for figure A: use this for his coat. Use it for figure A alone; "
        "nothing else about figure A, and nothing about any other figure, changes.")
    assert line(2, "", "B") == ("Reference picture 2 is from the person and is only for figure B: draw figure B with its help. "
                                "Nothing about any other figure changes.")
    assert line(1, "her dress", "subject") == "Reference picture 1 is from the person and is for the subject of this picture: her dress."
    assert line(1, "", "subject") == art.USER_REF_SUBJECT_PLAIN.format(n=1)
    # anything else reads as the whole picture
    assert line(1, "x", None) == line(1, "x", "Z") == line(1, "x", "scene") == "Reference picture 1 is from the person: x."


def test_the_closing_line_is_scoped_once_a_picture_is_tied_to_a_figure():
    note = art.Illustrator.reference_note
    a, b = ref("portrait", target="f0"), ref("portrait", "ink", target="f1")
    # none tied: today's words exactly, notes given as plain strings or (note, "scene")
    assert note("event", [a], ["A"], user=["the house"]) == note("event", [a], ["A"], user=[("the house", "scene")])
    assert note("event", [a], ["A"], user=[("the house", None)]).endswith(art.USER_REF)
    # every picture tied to a figure: numbered on from the set pictures, after figure, likeness and chest-up lines, the
    # scoped closing line, then the style picture's
    got = note("event", [a, b], ["A", "B"], user=[("her dress", "B"), ("", "A")], style_ref=True)
    assert got == " ".join([
        "Reference picture 1 is figure A.", "Reference picture 2 is figure B.", art.FIGURE_NOTE,
        art.LIKENESS.format(which="Reference picture 2 is", them="it"), art.CHEST_UP,
        art.USER_REF_FIGURE_LINE.format(n=3, label="B", note="her dress"),
        art.USER_REF_FIGURE_PLAIN.format(n=4, label="A"), art.USER_REF_SCOPED, art.STYLE_REF])
    assert art.USER_REF not in got
    # one tied and one for the whole picture: both lines, the scoped close
    mixed = note("event", [], ["A"], user=[("the house", "scene"), ("his hat", "A")])
    assert mixed == " ".join([art.USER_REF_LINE.format(n=1, note="the house"),
                              art.USER_REF_FIGURE_LINE.format(n=2, label="A", note="his hat"), art.USER_REF_SCOPED])
    # the subject of a portrait: after its earlier self, today's closing line
    assert note("portrait", [a], user=[("her dress", "subject")]) == " ".join(
        [art.SAME_FIGURE, art.USER_REF_SUBJECT_LINE.format(n=2, note="her dress"), art.USER_REF])
    # the scoped line still says what the plain one says, and scopes it
    assert art.USER_REF_SCOPED.startswith(art.USER_REF[:-1])
    assert art.USER_REF_SCOPED.endswith("changes that thing on that figure alone.")


# ---- draw: the rules, the fallback and the record


def test_a_picture_tied_to_a_labelled_figure_is_read_within_that_figure(moody):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e6", at="a11")
    mine = [tied(1, "use this for her dress", "A"), tied(2, "the mill", "scene")]
    rec = il.draw(job, user_refs=mine)
    call = im.calls[-1]
    assert call["references"][-3:] == [mine[0].data, mine[1].data, STYLE_PNG]
    assert call["note"].endswith(" ".join([
        art.USER_REF_FIGURE_LINE.format(n=3, label="A", note="use this for her dress"),
        art.USER_REF_LINE.format(n=4, note="the mill"), art.USER_REF_SCOPED, art.STYLE_REF]))
    # stored as used, the whole picture left out; no fallback
    assert rec["userRefs"] == [{"sha": mine[0].sha, "ext": "png", "note": "use this for her dress", "appliesTo": "A"},
                               {"sha": mine[1].sha, "ext": "png", "note": "the mill"}]
    assert "appliesFallback" not in rec


def test_a_label_the_text_no_longer_uses_falls_back_to_the_whole_picture(moody):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    text = pr.text.replace(" Figure B waits by the wall.", "")
    assert "Figure B" not in text and "Figure A" in text
    mine = [tied(1, "his coat", "B"), tied(2, "her shawl", "A")]
    rec = il.draw(job, prompt=pr, text=text, user_refs=mine)
    note = im.calls[-1]["note"]
    assert "Reference picture 2 is from the person: his coat." in note
    assert art.USER_REF_FIGURE_LINE.format(n=3, label="A", note="her shawl") in note
    assert rec["userRefs"][0].get("appliesTo") is None and rec["userRefs"][1]["appliesTo"] == "A"
    assert rec["appliesFallback"] == [mine[0].sha]
    # the reply only: the filed record, as every later read gets it, has no appliesFallback
    filed = next(r for r in pics(sink) if r["id"] == rec["id"])
    assert "appliesFallback" not in filed and filed["userRefs"] == rec["userRefs"]
    assert all("appliesFallback" not in r for r in il.man[0]["pictures"])


def test_subject_holds_for_a_portrait_and_reads_as_the_whole_picture_on_a_scene():
    il, sink, im = drawn()
    u = tied(1, "her dress", "subject")
    rec = il.draw(il.job("portrait", "mira", at="a11"), user_refs=[u])
    assert art.USER_REF_SUBJECT_LINE.format(n=1, note="her dress") in im.calls[-1]["note"]
    assert rec["userRefs"][0]["appliesTo"] == "subject" and "appliesFallback" not in rec
    # on a scene it means the same as the whole picture: stored as such, nothing to report
    rec = il.draw(il.job("event", "e6", at="a11"), user_refs=[u])
    assert "from the person: her dress." in im.calls[-1]["note"] and art.USER_REF_SCOPED not in im.calls[-1]["note"]
    assert "appliesTo" not in rec["userRefs"][0] and "appliesFallback" not in rec
    # a figure label on a portrait has no figure to hold: the whole picture, reported
    rec = il.draw(il.job("portrait", "mira", at="a11"), user_refs=[tied(2, "x", "A")])
    assert "appliesTo" not in rec["userRefs"][0] and rec["appliesFallback"] == [uref(2).sha]


def test_a_label_held_by_no_person_or_creature_falls_back(moody):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    # figure B said to be the mill (a place), and figure C used in the text but never given
    pr.figures = [f if f["label"] == "A" else {**f, "entity": "mill"} for f in pr.figures]
    text = pr.text.replace("Figure B waits", "Figure C waits by figure B and")
    mine = [tied(1, "", "B"), tied(2, "", "C"), tied(3, "", "A")]
    rec = il.draw(job, prompt=pr, text=text, user_refs=mine)
    assert rec["appliesFallback"] == [mine[0].sha, mine[1].sha]
    assert [r.get("appliesTo") for r in rec["userRefs"]] == [None, None, "A"]
    # an unknown value from an older or foreign caller is the whole picture too
    rec = il.draw(job, user_refs=[tied(4, "x", "everything")])
    assert "appliesTo" not in rec["userRefs"][0] and rec["appliesFallback"] == [tied(4).sha]


def test_a_same_prompt_redraw_keeps_a_figure_its_text_still_labels(moody):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e6", at="a11")
    mine = [tied(1, "his coat", "B")]
    rec = il.draw(job, user_refs=mine)
    # the redraw rebuilds the prompt from the record and passes the stored value back
    again = art.Prompt(text=rec["prompt"], scene=rec["prompt"], anchor=rec["from"], shape=rec["shape"], references=[],
                       sources=list(rec["sources"]), body=rec["prompt"],
                       figures=[{"label": f["label"], "entity": f["entity"]} for f in rec["figures"]])
    stored = [art.UserRef(mine[0].data, mine[0].mime, r["sha"], r["note"], r.get("description"), r.get("appliesTo") or "scene")
              for r in rec["userRefs"]]
    rec2 = il.draw(job, prompt=again, text=rec["prompt"], user_refs=stored)
    assert rec2["userRefs"] == rec["userRefs"] and rec2["userRefs"][0]["appliesTo"] == "B"
    assert art.USER_REF_FIGURE_LINE.format(n=1, label="B", note="his coat") in im.calls[-1]["note"]
    # an older record without figures (no labels known): the whole picture
    older = art.Prompt(text=rec["prompt"], scene=rec["prompt"], anchor=rec["from"], shape=rec["shape"], references=[],
                       sources=list(rec["sources"]), body=rec["prompt"])
    rec3 = il.draw(job, prompt=older, text=rec["prompt"], user_refs=stored)
    assert "appliesTo" not in rec3["userRefs"][0] and rec3["appliesFallback"] == [mine[0].sha]


def test_a_fix_keeps_what_each_picture_applies_to():
    il, sink, im = drawn()
    rec = il.draw(il.job("event", "e6", at="a11"), user_refs=[tied(1, "her shawl", "A")])
    fixed = il.fix(rec, [{"what": "an extra hand", "where": "left"}])
    assert fixed["userRefs"] == rec["userRefs"] and fixed["userRefs"][0]["appliesTo"] == "A"


def test_the_described_line_names_the_figure(moody, blind_image):
    assert art.described_line(art.UserRef(b"", "image/png", "s", "her coat!", "a long red coat.", "A")) == \
        "Also, for figure A only: her coat: a long red coat."
    assert art.described_line(art.UserRef(b"", "image/png", "s", "", "a long red coat.", "B")) == \
        "Also, for figure B only: a long red coat."
    assert art.described_line(art.UserRef(b"", "image/png", "s", "", "a long red coat.", "subject")) == "Also: a long red coat."
    eye = Eye("a long dark green velvet dress with a high lace collar.")
    il, sink, im = drawn(style="moody", image=blind_image(), describer=eye)
    rec = il.draw(il.job("event", "e6", at="a11"), user_refs=[tied(1, "her dress", "A"), tied(2, "", "C")])
    sent = im.calls[-1]["prompt"]
    assert "Also, for figure A only: her dress: a long dark green velvet dress with a high lace collar." in sent
    assert "Also: a long dark green velvet dress with a high lace collar." in sent     # C is not in the text: whole picture
    assert [r.get("appliesTo") for r in rec["userRefs"]] == ["A", None] and rec["appliesFallback"] == [tied(2).sha]
    assert rec["userRefs"][0]["description"].startswith("a long dark green")


@pytest.mark.parametrize("applies", ["scene", None])
def test_the_whole_picture_draws_exactly_as_before(moody, applies):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e6", at="a11")
    rec = il.draw(job, user_refs=[tied(1, "her dress", applies)])
    old_note = im.calls[-1]["note"]
    rec0 = il.draw(job, user_refs=[uref(1, "her dress")])
    assert im.calls[-1]["note"] == old_note and art.USER_REF in old_note
    assert rec["userRefs"] == rec0["userRefs"] == [{"sha": uref(1).sha, "ext": "png", "note": "her dress"}]
