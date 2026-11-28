"""The person's own reference pictures (`art.UserRef`) on a hand-started draw: sent after the figure, place and thing
pictures and before the style picture, never trimmed (a figure is, stored with `ref: None`), one note line each before
STYLE_REF, kept on the record as `userRefs`; for an image model that takes no pictures, described in words once and put in
the prompt, with nothing sent as a picture. Runs never send them."""

import hashlib

import pytest

from lorekeeper import art, images, llm
from tests.test_art import CH1, CH2, Image, Mem, Text, base_doc, book, pics, png, run, short_gap  # noqa: F401
from tests.test_art_style_ref import STYLE_PNG, SHA, moody, ref  # noqa: F401


def uref(i, note="", description=None, mime="image/png"):
    data = png(640, 480, f"mine-{i}".encode())
    return art.UserRef(data, mime, hashlib.sha256(data).hexdigest(), note, description)


def drawn(style="ink", image=None, **kw):
    """An Illustrator whose run drew every portrait (so scenes have figure pictures), and its image model."""
    image = image or Image()
    il, rep, sink, t, im = run(style=style, level="all", image=image, **kw)
    return il, sink, im


class Eye:
    """A text model with eyes that describes reference pictures with `replies`, one per call (the last one repeated)."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies) or ["a long dark green velvet dress with a high lace collar and narrow sleeves."], []
        self.usage = llm.Usage()
        self.name = "fake-eye"

    def complete(self, system, prompt, schema, prefix=None, images=()):
        self.calls.append({"system": system, "prompt": prompt, "images": [d for d, _ in images]})
        self.usage.cost_usd += 0.002
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return {"text": r}


@pytest.fixture
def blind_image(monkeypatch):
    """An image provider whose one model is marked as taking no pictures."""
    table = dict(llm.IMAGE_PROVIDERS)
    table["fakeprov"] = {"kind": "gemini", "base_url": "", "quality": {"draft": "1K", "standard": "1K", "best": "2K"},
                         "shapes": {}, "default": "blind-image",
                         "models": [{"id": "blind-image", "label": "Blind", "price": {}, "tier": "cheap", "references": False}]}
    monkeypatch.setattr(llm, "IMAGE_PROVIDERS", table)

    class Blind(Image):
        provider, name = "fakeprov", "blind-image"
    return Blind


def test_image_takes_references_reads_the_table_flag(blind_image):
    assert llm.image_takes_references("fakeprov", "blind-image") is False
    assert llm.image_takes_references("gemini", "gemini-nano-banana-2.1") is True
    assert llm.image_takes_references("openai", "unknown-model") is True     # not in the table: tried as given
    assert all(m.get("references", True) for k, p in llm.IMAGE_PROVIDERS.items() if k != "fakeprov" for m in p["models"])


def test_the_persons_pictures_go_after_the_figures_and_before_the_style_picture(moody):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e6", at="a11")
    mine = [uref(1, "use this for her dress"), uref(2)]
    rec = il.draw(job, user_refs=mine)
    call = im.calls[-1]
    figs = [f for f in rec["figures"] if f["ref"]]
    assert len(figs) == 2
    assert call["references"][2:] == [mine[0].data, mine[1].data, STYLE_PNG]
    assert call["note"] == " ".join([
        "Reference picture 1 is figure A.", "Reference picture 2 is figure B.", art.FIGURE_NOTE, art.CHEST_UP,
        "Reference picture 3 is from the person: use this for her dress.",
        "Reference picture 4 is from the person: draw what the prompt describes with its help.", art.USER_REF, art.STYLE_REF])
    assert rec["userRefs"] == [{"sha": mine[0].sha, "ext": "png", "note": "use this for her dress"},
                               {"sha": mine[1].sha, "ext": "png", "note": ""}]
    assert "refsLeftOut" not in rec and rec["styleRef"] == SHA
    # `references` lists only pictures of the set, never the person's
    assert rec["references"] == [f["ref"] for f in figs]
    assert il.report.refs_left_out == 0


def test_room_for_four_of_the_persons_pictures_trims_the_last_labelled_figure(moody):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e6", at="a11")
    mine = [uref(i, f"note {i}") for i in range(4)]
    rec = il.draw(job, user_refs=mine)
    call = im.calls[-1]
    assert len(call["references"]) == images.MAX_REFERENCES
    assert call["references"][1:] == [m.data for m in mine] + [STYLE_PNG]
    a, b = rec["figures"]
    assert a["label"] == "A" and a["ref"] and b["label"] == "B" and b["ref"] is None     # stored like a figure whose file is gone
    assert rec["refsLeftOut"] == 1 and il.report.refs_left_out == 1 and rec["references"] == [a["ref"]]
    assert "  1 x figure picture left out to make room" in il.report.lines()
    assert "figure B" not in call["note"] and call["note"].startswith("Reference picture 1 is figure A. " + art.FIGURE_NOTE)
    assert "Reference picture 2 is from the person: note 0." in call["note"]
    assert "Reference picture 5 is from the person: note 3." in call["note"] and call["note"].endswith(art.USER_REF + " " + art.STYLE_REF)
    # without the style picture both figures still fit beside four of the person's
    il, sink, im = drawn()
    rec = il.draw(il.job("event", "e6", at="a11"), user_refs=mine)
    assert all(f["ref"] for f in rec["figures"]) and "refsLeftOut" not in rec and len(im.calls[-1]["references"]) == 6


def test_room_trims_a_spare_place_first_then_the_last_figure_never_the_persons_or_the_style_picture():
    four = [(*ref("portrait", target=f"f{i}"), b"x") for i in range(4)]
    place, thing = (*ref("place", target="pl"), b"p"), (*ref("thing", target="th"), b"t")
    labels = ["A", "B", "C", "D", None, None]
    # four of the person's pictures and the style picture leave one slot: the places go, then figures from the end
    got, labs = art.Illustrator._room(four + [place, thing], labels, 5)
    assert [r["target"] for _, r, _ in got] == ["f0"] and labs == ["A"]
    got, labs = art.Illustrator._room(four + [place, thing], labels, 3)
    assert [r["target"] for _, r, _ in got] == ["f0", "f1", "f2"] and labs == ["A", "B", "C"]
    got, labs = art.Illustrator._room(four + [place], labels[:5], 1)
    assert [r["target"] for _, r, _ in got] == ["f0", "f1", "f2", "f3", "pl"]
    assert art.Illustrator._room(four, ["A", "B", "C", "D"], 6) == ([], [])


def test_the_note_lines_are_numbered_on_from_the_set_pictures():
    note = art.Illustrator.reference_note
    a, b = ref("portrait", target="f0"), ref("portrait", "ink", target="f1")
    mine = ["this is the house.", ""]
    lines = ["Reference picture {} is from the person: this is the house.",
             "Reference picture {} is from the person: draw what the prompt describes with its help."]
    # alone, with and without the style picture
    assert note("event", [], ["A"], user=mine) == " ".join([lines[0].format(1), lines[1].format(2), art.USER_REF])
    assert note("portrait", [], user=mine[:1], style_ref=True) == " ".join([lines[0].format(1), art.USER_REF, art.STYLE_REF])
    # after a portrait's earlier self
    assert note("portrait", [a], user=mine[1:]) == " ".join([art.SAME_FIGURE, lines[1].format(2), art.USER_REF])
    # after labelled figures, a likeness and the chest-up line, before the style picture
    got = note("event", [a, b], ["A", "B"], user=mine, style_ref=True)
    assert got == " ".join(["Reference picture 1 is figure A.", "Reference picture 2 is figure B.", art.FIGURE_NOTE,
                            art.LIKENESS.format(which="Reference picture 2 is", them="it"), art.CHEST_UP,
                            lines[0].format(3), lines[1].format(4), art.USER_REF, art.STYLE_REF])
    # an older prompt's lone likeness is numbered when the person's pictures follow it
    lone = note("plate", [b], None, user=mine[:1])
    assert lone.startswith("Reference picture 1 is drawn in another style.")
    assert lone.endswith(art.CHEST_UP + " " + lines[0].format(2) + " " + art.USER_REF)
    # the person's words go as written, whitespace collapsed and cut at 120 characters
    long = "use  the\ncolours   " + "x" * 200
    assert ("from the person: use the colours " + "x" * 104 + ".") in note("event", [], None, user=[long])
    # none of the person's: nothing changes
    assert art.USER_REF not in note("event", [a, b], ["A", "B"], style_ref=True)


def test_a_persons_own_text_and_a_restyled_or_same_prompt_redraw_send_them(moody):
    il, sink, im = drawn(style="moody")
    job = il.job("event", "e1", at="a11")
    pr = il.make_prompt(job)
    mine = [uref(7, "her shawl")]
    rec = il.draw(job, prompt=pr, text="A woman with a basket at a mill gate in the morning, a miller in the doorway.",
                  user_refs=mine)
    assert im.calls[-1]["references"][-2:] == [mine[0].data, STYLE_PNG] and rec["userRefs"][0]["note"] == "her shawl"
    again = art.Prompt(text=rec["prompt"], scene=rec["prompt"], anchor=rec["from"], shape=rec["shape"], references=[],
                       sources=list(rec["sources"]), body=rec["prompt"])
    rec2 = il.draw(job, prompt=again, text=rec["prompt"], user_refs=mine)
    assert im.calls[-1]["references"] == [mine[0].data, STYLE_PNG] and rec2["userRefs"] == rec["userRefs"]
    assert im.calls[-1]["note"] == " ".join(["Reference picture 1 is from the person: her shawl.", art.USER_REF, art.STYLE_REF])


def test_a_comic_and_a_portrait_send_them_like_any_picture():
    il, sink, im = drawn()
    rec = il.draw(il.job("comic", "Two", at="a11"), user_refs=[uref(1, "the house")])
    assert im.calls[-1]["references"][-1] == uref(1).data and rec["userRefs"][0]["note"] == "the house"
    rec = il.draw(il.job("portrait", "mira", at="a11"), user_refs=[uref(2)])
    assert im.calls[-1]["references"][-1] == uref(2).data and rec["kind"] == "portrait"


def test_runs_never_send_them(moody):
    il, sink, im = drawn(style="moody")
    assert all(not any(r.startswith(b"\x89PNG") and b"mine-" in r for r in c["references"]) for c in im.calls)
    assert not any("userRefs" in r or "refsLeftOut" in r for r in pics(sink))
    assert all(art.USER_REF not in c["note"] for c in im.calls)


def test_more_than_four_is_refused_before_any_call():
    il, sink, im = drawn()
    n = len(im.calls)
    with pytest.raises(ValueError, match="at most 4"):
        il.draw(il.job("event", "e1", at="a11"), user_refs=[uref(i) for i in range(5)])
    assert len(im.calls) == n


def test_a_fix_never_sends_them():
    il, sink, im = drawn()
    rec = il.draw(il.job("event", "e1", at="a11"), user_refs=[uref(1, "her shawl")])
    fixed = il.fix(rec, [{"what": "an extra hand", "where": "left"}])
    assert im.calls[-1]["references"] == [sink.files[rec["file"]]]
    assert fixed["userRefs"] == rec["userRefs"]          # kept with the picture, for a later redraw


# ---- an image model that takes no pictures: described in words


def test_without_image_input_each_is_described_once_into_the_prompt_and_nothing_is_sent(moody, blind_image):
    eye = Eye("a long dark green velvet dress with a high lace collar and narrow sleeves.",
              "a tall narrow stone house with a steep slate roof and two round towers")
    il, sink, im = drawn(style="moody", image=blind_image(), describer=eye)
    job = il.job("event", "e6", at="a11")
    mine = [uref(1, "use this for her dress."), uref(2)]
    rec = il.draw(job, user_refs=mine)
    call = im.calls[-1]
    assert call["references"] == [] and call["note"] == ""              # no figure, no person's, no style picture
    assert [c["images"] for c in eye.calls] == [[mine[0].data], [mine[1].data]]
    assert all(c["system"] == art.REF_DESCRIBE for c in eye.calls)
    assert "use this for her dress" in eye.calls[0]["prompt"] and "main subject" in eye.calls[1]["prompt"]
    style = art.style_sentence("moody")
    also = ("Also: use this for her dress: a long dark green velvet dress with a high lace collar and narrow sleeves. "
            "Also: a tall narrow stone house with a steep slate roof and two round towers.")
    assert also + " " + style + " " + art.NO_TEXT in call["prompt"] and call["prompt"].endswith(art.LABEL_NOT_TEXT)
    assert rec["prompt"] == call["prompt"]                              # the stored prompt is what was sent
    assert rec["userRefs"][0]["description"].startswith("a long dark green velvet dress")
    assert rec["userRefs"][1]["description"].startswith("a tall narrow stone house")
    assert "styleRef" not in rec and rec["references"] == [] and all(f["ref"] is None for f in rec["figures"])
    assert "refsLeftOut" not in rec
    assert il.text_costs().get("reference descriptions", 0) == pytest.approx(0.004)
    # a redraw passes the stored descriptions: no new call, and the words are not added twice
    stored = [art.UserRef(m.data, m.mime, m.sha, r["note"], r.get("description")) for m, r in zip(mine, rec["userRefs"])]
    again = art.Prompt(text=rec["prompt"], scene=rec["prompt"], anchor=rec["from"], shape=rec["shape"], references=[],
                       sources=list(rec["sources"]), body=rec["prompt"], style=style)
    rec2 = il.draw(job, prompt=again, text=rec["prompt"], user_refs=stored)
    assert len(eye.calls) == 2 and im.calls[-1]["prompt"] == rec["prompt"] and rec2["userRefs"] == rec["userRefs"]


def test_without_image_input_and_no_describer_the_draw_raises_no_vision_before_drawing(blind_image):
    il, sink, im = drawn(image=blind_image())
    n = len(im.calls)
    with pytest.raises(llm.NoVision):
        il.draw(il.job("event", "e1", at="a11"), user_refs=[uref(1)])
    assert len(im.calls) == n
    # the web layer describes them itself: a description already set is used as given, with no describer and no call
    rec = il.draw(il.job("event", "e1", at="a11"), user_refs=[uref(1, "her coat", "a long red wool coat.")])
    assert im.calls[-1]["references"] == [] and "Also: her coat: a long red wool coat." in im.calls[-1]["prompt"]
    assert rec["userRefs"][0]["description"] == "a long red wool coat."
    # with none of the person's pictures it draws, sending nothing
    rec = il.draw(il.job("event", "e1", at="a11"))
    assert im.calls[-1]["references"] == [] and "userRefs" not in rec


def test_a_described_line_goes_before_the_style_sentence_else_the_no_text_line_else_at_the_end():
    lines = ["Also: a red coat."]
    assert art.with_described("A man at a gate. Ink drawing. " + art.NO_TEXT, lines, "Ink drawing.") == \
        "A man at a gate. Also: a red coat. Ink drawing. " + art.NO_TEXT
    assert art.with_described("A man at a gate. " + art.NO_TEXT, lines, "Oil painting.") == \
        "A man at a gate. Also: a red coat. " + art.NO_TEXT
    assert art.with_described("A man at a gate.", lines, "Oil painting.") == "A man at a gate. Also: a red coat."
    assert art.with_described("A man. Also: a red coat. Ink.", lines, "Ink.") == "A man. Also: a red coat. Ink."
    assert art.described_line(art.UserRef(b"", "image/png", "s", "her coat!", "a long red coat.")) == "Also: her coat: a long red coat."


def test_describe_reference_checks_the_words_with_one_rewrite():
    eye = Eye("A coat Worn by Lucy Westenra in London.", "a long red wool coat with brass buttons and a wide collar")
    got = art.describe_reference(eye, b"img", "image/png", "her coat")
    assert got == "a long red wool coat with brass buttons and a wide collar."
    assert len(eye.calls) == 2 and "capitalised word" in eye.calls[1]["prompt"] and "<refused>" in eye.calls[1]["prompt"]
    assert eye.calls[0]["images"] == [b"img"] and "<note>her coat</note>" in eye.calls[0]["prompt"]
    with pytest.raises(ValueError, match="could not describe"):
        art.describe_reference(Eye("Red.", "Blue."), b"img", "image/png")
    long = "a " + "very " * 80 + "red coat."
    with pytest.raises(ValueError):
        art.describe_reference(Eye(long), b"img", "image/png")

    class NoEyes:
        def complete(self, system, prompt, schema, prefix=None):
            return {"text": "x"}
    with pytest.raises(llm.NoVision):
        art.describe_reference(NoEyes(), b"img", "image/png")


def test_a_description_opening_with_a_name_is_caught_and_rewritten():
    assert art.ref_faults("A long green velvet dress with a lace collar.") == []
    assert art.ref_faults("Long sleeves puffed at the shoulder. Two rows of brass buttons down the front.") == []
    assert any("Lucy" in f for f in art.ref_faults("Lucy wears a long green velvet dress with a lace collar."))
    assert any("Whitby" in f for f in art.ref_faults("A grey stone abbey on a cliff. Whitby lies below it in fog."))
    eye = Eye("Lucy wears a long green velvet dress with a lace collar.", "a long green velvet dress with a lace collar.")
    assert art.describe_reference(eye, b"img", "image/png", "her dress") == "a long green velvet dress with a lace collar."
    assert len(eye.calls) == 2 and "Lucy" in eye.calls[1]["prompt"]


def test_the_automatic_check_reads_the_text_as_sent_with_the_described_lines(blind_image):
    from tests.test_art import Seeing
    seeing = Seeing(faults=[])
    il, sink, im = drawn(image=blind_image(), cheap_text=seeing, check=True,
                         describer=Eye("a tall man with one arm, in a long grey coat."))
    rec = il.draw(il.job("event", "e1", at="a11"), user_refs=[uref(1, "the coat")])
    assert "Also: the coat: a tall man with one arm, in a long grey coat." in rec["prompt"]
    checked, _ = seeing.looked[-1]
    assert rec["prompt"] in checked and im.calls[-1]["prompt"] == rec["prompt"]
