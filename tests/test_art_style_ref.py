"""A saved style's own reference picture (art.SAVED_STYLE_REFS): sent last with every drawing of its set, never with a fix,
within the image call's six pictures, with STYLE_REF last in the note and `styleRef` on the record; and `describe_style`, the
style sentence written from a picture, checked by code with one rewrite."""

import hashlib

import pytest

from lorekeeper import art, images, llm
from tests.test_art import (CH1, CH2, HEAD, Image, Mem, Seeing, Text, base_doc, book, fixes, pics, png, run,  # noqa: F401
                             short_gap)

STYLE_PNG = png(800, 600, b"style-reference")
SHA = hashlib.sha256(STYLE_PNG).hexdigest()


@pytest.fixture
def moody(tmp_path, monkeypatch):
    """A saved style "moody" whose picture is sent with its drawings."""
    f = tmp_path / "moody.ref.png"
    f.write_bytes(STYLE_PNG)
    monkeypatch.setattr(art, "SAVED_STYLES", {"moody": "Moody charcoal drawing, heavy smudged shadows, grey paper."})
    monkeypatch.setattr(art, "SAVED_STYLE_REFS", {"moody": {"path": str(f), "mime": "image/png", "sha": SHA}})
    return f


def test_the_style_picture_goes_last_with_every_drawing_of_a_run_and_the_record_names_it(moody):
    il, rep, sink, t, im = run(style="moody", level="all")
    assert rep.drawn == len(im.calls) > 4
    for call in im.calls:
        assert call["references"][-1] == STYLE_PNG and call["references"].count(STYLE_PNG) == 1
        assert call["note"].endswith(art.STYLE_REF) and call["note"].count(art.STYLE_REF) == 1
        assert len(call["references"]) <= images.MAX_REFERENCES
    assert all(r["styleRef"] == SHA for r in pics(sink))
    # the record's references are pictures of the set, never the style picture
    files = {r["id"] for r in pics(sink)}
    assert all(isinstance(x, dict) or x in files for r in pics(sink) for x in r["references"])
    # a first portrait has no other reference: the style picture alone, with STYLE_REF as the whole note
    first = next(c for c in im.calls if c["shape"] == "portrait")
    assert first["references"] == [STYLE_PNG] and first["note"] == art.STYLE_REF
    # a scene with labelled figures: their pictures first, in label order, then the style picture
    scene = next(c for c, r in zip(im.calls, sink.m["pictures"]) if r.get("figures"))
    assert len(scene["references"]) >= 2 and scene["note"].startswith("Reference picture 1 is figure A.")


def test_a_built_in_style_or_a_style_whose_picture_is_off_or_gone_sends_none(moody, monkeypatch):
    il, rep, sink, t, im = run(style="ink", portraits=False)
    assert all(STYLE_PNG not in c["references"] and art.STYLE_REF not in c["note"] for c in im.calls)
    assert not any("styleRef" in r for r in pics(sink))
    monkeypatch.setattr(art, "SAVED_STYLE_REFS", {})            # "send this picture" off: not in the table
    il, rep, sink, t, im = run(style="moody", portraits=False)
    assert all(STYLE_PNG not in c["references"] for c in im.calls) and not any("styleRef" in r for r in pics(sink))
    monkeypatch.setattr(art, "SAVED_STYLE_REFS", {"moody": {"path": str(moody.parent / "gone.png"), "mime": "image/png"}})
    assert art.style_reference("moody") is None


def test_the_style_reference_reads_the_file_and_its_sha_and_mime_from_the_bytes(moody):
    ref = art.style_reference("moody")
    assert ref.data == STYLE_PNG and ref.mime == "image/png" and ref.sha == SHA
    assert art.style_reference("Moody") is not None              # a style text that slugs to it
    assert art.style_reference("ink") is None and art.style_reference("") is None
    assert art.preview_references("moody") == {"references": [(STYLE_PNG, "image/png")], "note": art.STYLE_REF}
    assert art.preview_references("ink") == {}
    assert art.mime_of(b"\xff\xd8\xff\xe0rest") == "image/jpeg" and art.mime_of(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert art.mime_of(b"GIF89a") is None


def test_an_on_demand_draw_a_persons_own_text_and_a_same_prompt_redraw_send_it(moody):
    bk = book(("One", CH1), ("Two", CH2))
    im = Image()
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], Text(), im, "moody", portraits=False, workers=1,
                         log=lambda *_: None, pause=0)
    job = il.job("event", "e1", at="a11")
    rec = il.draw(job)                                              # on demand, the prompt written for it
    assert im.calls[-1]["references"][-1] == STYLE_PNG and rec["styleRef"] == SHA
    pr = il.make_prompt(job)                                        # the route's own Prompt with the person's text
    rec = il.draw(job, prompt=pr, text="A woman with a basket at a mill gate in the morning, a miller in the doorway.")
    assert im.calls[-1]["references"][-1] == STYLE_PNG and rec["styleRef"] == SHA
    # a same-prompt redraw (the route builds a Prompt from the record and its references) and a restyled draw (another
    # set's prompt, references taken again from this one) both go through `draw`
    old = pics(il.books[0].b.sink)[0]
    again = art.Prompt(text=old["prompt"], scene=old["prompt"], anchor=old["from"], shape=old["shape"], references=[],
                       sources=list(old["sources"]), body=old["prompt"])
    rec = il.draw(job, prompt=again, text=old["prompt"])
    assert im.calls[-1]["references"] == [STYLE_PNG] and im.calls[-1]["note"] == art.STYLE_REF and rec["styleRef"] == SHA


def test_a_fix_never_sends_the_style_picture_by_hand_or_after_the_check(moody):
    il, rep, sink, t, im = run(style="moody", portraits=False, cheap_text=Seeing(faults=[HEAD]), check=True)
    assert fixes(im) and all(STYLE_PNG not in c["references"] and len(c["references"]) == 1 for c in fixes(im))
    assert all(r["check"]["fixed"] for r in pics(sink)) and all(r["styleRef"] == SHA for r in pics(sink))
    il.fix(pics(sink)[0], [HEAD])
    assert im.calls[-1]["prompt"].startswith(art.FIX) and im.calls[-1]["references"] == [sink.files[pics(sink)[0]["file"]]]


def ref(kind, set_=None, target="x"):
    return (0, {"id": f"p{kind}{target}", "kind": kind, "target": target, "mime": "image/png", **({"set": set_} if set_ else {})})


def test_six_pictures_at_most_dropping_the_last_place_or_thing_never_the_style_picture():
    four = [(*ref("portrait", target=f"f{i}"), b"x") for i in range(4)]
    place, thing = (*ref("place", target="pl"), b"p"), (*ref("thing", target="th"), b"t")
    got, labels = art.Illustrator._room(four + [place, thing], ["A", "B", "C", "D", None, None], 1)
    assert [r["target"] for _, r, _ in got] == ["f0", "f1", "f2", "f3", "pl"] and labels == ["A", "B", "C", "D", None]
    got, labels = art.Illustrator._room(four + [thing, place], ["A", "B", "C", "D", None, None], 1)
    assert [r["target"] for _, r, _ in got] == ["f0", "f1", "f2", "f3", "th"]
    # no style picture, or room left: nothing is dropped
    assert art.Illustrator._room(four + [place, thing], ["A", "B", "C", "D", None, None], 0)[0] == four + [place, thing]
    assert art.Illustrator._room(four, ["A", "B", "C", "D"], 1)[0] == four
    # an older prompt's six unlabelled references: the last place or thing first, else the last one
    six = [(*ref("portrait", target=f"f{i}"), b"x") for i in range(6)]
    got, labels = art.Illustrator._room(six, None, 1)
    assert labels is None and [r["target"] for _, r, _ in got] == ["f0", "f1", "f2", "f3", "f4"]


def test_the_place_refs_run_keeps_within_six_with_the_style_picture(moody):
    il, rep, sink, t, im = run(style="moody", level="all", place_refs=True)
    assert all(len(c["references"]) <= images.MAX_REFERENCES and c["references"][-1] == STYLE_PNG for c in im.calls)


def test_the_note_ends_with_style_ref_numbered_with_the_others():
    note = art.Illustrator.reference_note
    a, b = ref("portrait", target="f0"), ref("portrait", "ink", target="f1")
    assert note("event", [], ["A"], style_ref=True) == art.STYLE_REF
    assert note("portrait", [], style_ref=True) == art.STYLE_REF
    assert note("portrait", [a], style_ref=True) == art.SAME_FIGURE + " " + art.STYLE_REF
    got = note("event", [a, b], ["A", "B"], style_ref=True)
    assert got == " ".join(["Reference picture 1 is figure A.", "Reference picture 2 is figure B.", art.FIGURE_NOTE,
                            art.LIKENESS.format(which="Reference picture 2 is", them="it"), art.CHEST_UP, art.STYLE_REF])
    # a lone likeness is still numbered when the style picture follows it: "the reference picture" would be ambiguous
    lone = note("plate", [b], None, style_ref=True)
    assert lone.startswith("Reference picture 1 is drawn in another style.") and lone.endswith(art.CHEST_UP + " " + art.STYLE_REF)
    assert note("plate", [b], None).startswith("The reference picture is drawn in another style.")
    place = ref("place", "ink", target="pl")
    got = note("event", [a, place], ["A", None], style_ref=True)
    assert art.PLACE_NOTE.format(n=2) + art.OTHER_STYLE in got and got.endswith(art.STYLE_REF)
    # without the style picture nothing changes
    assert art.STYLE_REF not in note("event", [a, b], ["A", "B"])


class Describer:
    """A text model with eyes that answers the style description with `replies`, one per call."""

    def __init__(self, *replies, blind=False):
        self.replies, self.blind, self.calls = list(replies), blind, []
        self.usage = llm.Usage()
        self.name = "fake-eye"

    def complete(self, system, prompt, schema, prefix=None, images=()):
        self.calls.append({"system": system, "prompt": prompt, "images": list(images)})
        if self.blind:
            raise llm.NoVision("fake-eye cannot see pictures")
        self.usage.cost_usd += 0.001
        r = self.replies.pop(0)
        return r if isinstance(r, dict) else {"text": r}


def test_describe_style_sends_the_picture_and_returns_a_checked_sentence():
    m = Describer("Watercolour painting, translucent washes, soft edges, pale paper showing through")
    got = art.describe_style(m, STYLE_PNG, "image/png")
    assert got == "Watercolour painting, translucent washes, soft edges, pale paper showing through."
    (call,) = m.calls
    assert call["system"] == art.STYLE_DESCRIBE and call["images"] == [(STYLE_PNG, "image/png")]
    assert "subjects" in art.STYLE_DESCRIBE and "in the style of" in art.STYLE_DESCRIBE


@pytest.mark.parametrize("bad, caught", [
    ("Oil painting in the manner of Rembrandt, deep shadows, warm glazes.", "capitalised word"),
    ("Bold ink outlines, flat colour, in the style of a vintage comic.", "compares with"),
    ("x" * 320, "over 300"),
    ("Ink. Wash. Grain. Light.", "more than two"),
    ("Ink.", "too short"),
    ({"nothing": 1}, "empty"),
])
def test_describe_style_rewrites_once_naming_what_was_caught(bad, caught):
    m = Describer(bad, "Thick oil paint, visible brushwork, deep shadows and warm glazes.")
    assert art.describe_style(m, STYLE_PNG, "image/png") == "Thick oil paint, visible brushwork, deep shadows and warm glazes."
    assert len(m.calls) == 2 and caught in m.calls[1]["prompt"] and m.calls[1]["images"] == [(STYLE_PNG, "image/png")]


def test_describe_style_gives_up_after_the_rewrite_and_passes_errors_on():
    m = Describer("Pencil sketch like Arthur Rackham drew.", "Pencil sketch like Arthur Rackham drew.")
    with pytest.raises(ValueError, match="could not describe"):
        art.describe_style(m, STYLE_PNG, "image/png")
    assert len(m.calls) == 2
    with pytest.raises(llm.NoVision):
        art.describe_style(Describer(blind=True), STYLE_PNG, "image/png")

    class Old:          # a backend whose complete() takes no pictures
        name = "old"

        def complete(self, system, prompt, schema, prefix=None):
            raise AssertionError

    with pytest.raises(llm.NoVision):
        art.describe_style(Old(), STYLE_PNG, "image/png")


def test_style_faults_lets_a_plain_style_line_pass():
    assert art.style_faults("Black ink drawing, fine cross-hatching, strong contrast, aged paper.") == []
    assert art.style_faults("Flat digital colour with soft gradients. Clean thin lines and gentle rim light.") == []
    assert "Mucha" in art.style_faults("Flowing lines, flat pastel colour, decorative borders like Mucha.")[0]


def test_a_300_character_description_with_no_closing_mark_passes_as_written():
    line = ("Soft digital painting, " + "fine brushed texture, " * 20)[:300].rstrip(" ,")
    line = line + "x" * (300 - len(line))
    assert len(line) == 300 and line[-1] not in ".!?"
    m = Describer(line)
    assert art.describe_style(m, STYLE_PNG, "image/png") == line and len(m.calls) == 1
    assert art.describe_style(Describer(line[:299]), STYLE_PNG, "image/png") == line[:299] + "."
