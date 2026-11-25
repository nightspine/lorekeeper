"""Art sets (art.py) with a scripted text model and a scripted image model: what is drawn, from which anchor, with which
references; the checks, refusals, the cost cap, resume and series inheritance."""

import json
import re
import struct

import pytest

from lorekeeper import art, epub, images, llm, text

SCENE = ("A woman with a basket on her arm stands at a wooden gate beside a grey stone mill, while a miller watches her from the "
         "doorway with his arms folded. Soft morning light falls across the packed earth of the yard and a path leads off toward low "
         "green hills in the distance, the river bright beyond the mill wheel.")
OREN_SCENE = ("A broad old figure stands in a doorway, turned three-quarters toward the viewer, arms folded and face calm, lit by "
              "soft side light from a window. The background is a plain wall of grey stone, simple and muted, so the figure stands out "
              "clearly against it.")
PORTRAIT_SCENE = ("A head-and-shoulders portrait, the figure turned slightly toward the viewer with a calm and steady expression, lit "
                  "softly from one side. Behind, a plain wall of grey stone and a small window showing low green hills under a soft "
                  "morning sky. The background is simple and muted so the figure stands out.")
CH1 = ["Mira walked to the mill with her basket and waited by the gate that morning.",
       "The miller Oren came out of the mill door wiping flour from his hands slowly.",
       "Mira told Oren that her mother sent greetings from the village below the hill.",
       "Oren laughed and asked Mira to carry the sacks of flour into the cart outside.",
       "A small errand took Mira to the well behind the mill for a bucket of water.",
       "Evening came and Mira walked home along the dusty road beside the green river."]
CH2 = ["At dawn a hooded Stranger came to the mill and asked Oren for a bed that night.",
       "Mira saw the Stranger sit by the fire and never lower the hood from his face.",
       "Gabriel rode in from the hills with news that Zephyr the storm was coming soon.",
       "Oren barred the door and Mira watched the dark road from the narrow window.",
       "The Stranger left before sunrise and Mira found only a silver coin on the table.",
       "Mira kept the coin and told no one about the Stranger at the market that day."]


def book(*chapters):
    blocks, chs = [], []
    for title, paras in chapters:
        chs.append(epub.Chapter(title=title, start=len(blocks)))
        for p in paras:
            blocks.append(epub.Block(seq=len(blocks), spine=0, words=text.words(p), raw=p))
    return epub.Book(blocks=blocks, chapters=chs)


def ent(eid, name, typ="person", frm="a0", facets=()):
    return {"id": eid, "type": typ, "from": frm, "names": [{"value": name, "kind": "primary", "from": frm}], "facets": list(facets)}


def portrait(eid, at, txt, n=1, prefix="bk"):
    return {"id": f"{prefix}.{eid}.portrait-{n}", "kind": "profile", "from": at, "basis": "stated", "evidence": [at], "text": txt}


def event(i, at, summary, ents, kind=None, prefix=""):
    return {"id": f"e{i}", "from": f"{prefix}a{at}", "summary": summary, "entities": ents, "evidence": [f"{prefix}a{at}"],
            **({"kind": kind} if kind else {})}


def doc(bk, key=None, prefix="", entities=None, events=None, recaps=None):
    anchors = [{"id": f"{prefix}a{b.seq}", "seq": b.seq, "fp": b.fingerprint} for b in bk.blocks]
    return {"id": key or "bk", "work": {"title": "Millbrook"}, "editions": [{"textHash": "sha256:t" + (key or "bk"), "builtFrom": True}],
            **({"series": {"id": "mill", "book": key}} if key else {}),
            "anchors": anchors, "entities": entities, "events": events, "recaps": recaps,
            "relations": [], "coverage": {"complete": True, "through": anchors[-1]["id"]}}


def recap(i, at, title, prefix=""):
    return {"id": f"r{i}", "from": f"{prefix}a{at}", "text": f"Mira goes about her day at the mill in {title.lower()}.",
            "evidence": [f"{prefix}a{at}"], "scope": "chapter", "title": title}


def base_doc(bk):
    ents = [ent("mira", "Mira", facets=[portrait("mira", "a4", "A slight young woman of about twenty with dark braided hair, a brown "
                                                 "shawl and a plain grey dress, who carries baskets to the mill.")]),
            ent("oren", "Oren", frm="a1", facets=[portrait("oren", "a3", "The old miller, broad, with white hair, flour on his sleeves "
                                                              "and a leather apron, who runs the mill.")]),
            ent("mill", "the mill", "place", facets=[]),
            ent("stranger", "the Stranger", frm="a6", facets=[portrait("stranger", "a7", "A hooded traveller in a long black cloak, of "
                                                                         "middle height, whose face is never seen.")]),
            ent("gabriel", "Gabriel", frm="a8", facets=[portrait("gabriel", "a8", "A tall fair rider with short hair, a blue coat and "
                                                                    "tall riding boots, who brings news from the hills.")])]
    evs = [event(1, 0, "Mira walks to the mill with a basket and waits at the gate.", ["mira", "mill"]),
           event(2, 1, "Oren the miller comes out of the mill and greets her.", ["oren", "mira"], "turn"),
           event(3, 4, "A small errand takes Mira to the well.", ["mira"], "minor"),
           event(4, 6, "A hooded stranger comes to the mill asking for a bed.", ["stranger", "oren"], "turn"),
           event(5, 8, "Gabriel rides in with news of a coming storm.", ["gabriel"]),
           event(6, 9, "Mira finds a silver coin the stranger left behind.", ["mira", "stranger"])]
    return doc(bk, entities=ents, events=evs, recaps=[recap(1, 5, "One"), recap(2, 11, "Two")])


class Mem:
    """A Sink in memory."""

    def __init__(self):
        self.m, self.files = None, {}

    def manifest(self):
        return self.m

    def save(self, manifest):
        self.m = json.loads(json.dumps(manifest))

    def put(self, file, data):
        self.files[file] = data

    def read(self, file):
        return self.files.get(file)


# The scripted model's own idea of a look and of a look change (the test's, not the code's: art.py has no such lists).
FAKE_LOOK = re.compile(r"\b(hair|haired|shawl|dress|coat|cloak|hooded|apron|boots|tall|old|white|scar\w*|older|moustache|nose|"
                       r"eyes|wolf pelt|mud)\b", re.IGNORECASE)
FAKE_CHANGE = re.compile(r"\b(scarred|older than she was|lost an eye|eyes replaced)\b", re.IGNORECASE)


def is_portrait(prompt):
    return "Write the prompt for a portrait" in prompt


def drawing(prompt):
    """The name of the entry a writer's prompt draws ("Mira"), or None for a scene."""
    m = re.search(r'<entry_to_draw name="([^"]*)"', prompt)
    return m.group(1) if m else None


class Text:
    """A scripted text model that answers by the kind of prompt it is given and keeps every prompt (and its system text).
    `look(text)` and `change(text)` say how it classifies an entity's item (default: the FAKE_ regexes above); `scene` is the
    writer's text (a string, or a function of the writer's prompt), `shows` the figures it says it drew."""

    def __init__(self, scene=SCENE, screen=None, shows=None, look=None, change=None):
        self.scene, self.shows, self.screen = scene, shows, screen
        self.look = look or (lambda t: bool(FAKE_LOOK.search(t)))
        self.change = change or (lambda t: (m.group(0) if (m := FAKE_CHANGE.search(t)) else ""))
        self.prompts, self.systems = [], []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
        self.systems.append(system)
        if system == art.WRITER:
            scene = PORTRAIT_SCENE if is_portrait(prompt) and self.scene is SCENE else self.scene
            out = {"text": scene(prompt) if callable(scene) else scene}
            if self.shows is not None and "shows" in schema.get("properties", {}):
                out["shows"] = self.shows
            return out
        if "Which of them are a visible moment" in prompt:
            ns = [int(n) for n in re.findall(r"^(\d+)\. ", prompt, re.M)]
            keep = self.screen(ns, prompt) if self.screen else ns
            return {"moments": keep}
        if "notes a reader's companion keeps on some entries" in prompt:
            out = []
            for n, t in re.findall(r"^(\d+)\. (.*)$", prompt.split("\n## ", 1)[-1], re.M):
                look, change = self.look(t), self.change(t)
                if look or change:
                    out.append({"n": int(n), "look": bool(look), "change": change})
            return {"items": out}
        raise AssertionError("unexpected prompt: " + prompt[:80])

    def scenes(self):
        """The writer's prompts (first answers and rewrites)."""
        return [p for p, s in zip(self.prompts, self.systems) if s == art.WRITER]


def png(w, h, salt):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR" + struct.pack(">II", w, h) + salt


class Image:
    provider, name = "", "fake-image"

    def __init__(self, cost=0.05, refuse=()):
        self.calls, self.cost, self.refuse = [], cost, list(refuse)

    def draw(self, prompt, *, shape="landscape", quality="standard", references=()):
        self.calls.append({"prompt": prompt, "shape": shape, "quality": quality, "references": [r[0] for r in references]})
        if self.refuse and self.refuse[0](prompt, len(self.calls)):
            raise images.ImageRefused("safety: violence")
        return images.Drawn(png(1024, 576, str(len(self.calls)).encode() + prompt[:20].encode()), "image/png", "fake-image", self.cost)


@pytest.fixture(autouse=True)
def short_gap(monkeypatch):
    monkeypatch.setattr(art, "MIN_GAP", 2)


def run(d=None, bk=None, sink=None, text_=None, image=None, **kw):
    bk = bk or book(("One", CH1), ("Two", CH2))
    d = d or base_doc(bk)
    sink = sink or Mem()
    text_, image = text_ or Text(), image or Image()
    kw.setdefault("workers", 1)
    kw.setdefault("portraits", "all")    # the older rule: these tests are about portraits, not about who is main cast
    il = art.Illustrator([art.Book(d, bk, sink, kw.pop("level", "main"))], text_, image, kw.pop("style", "ink"), log=lambda *_: None,
                         pause=0, **kw)
    return il, il.run(), sink, text_, image


def pics(sink, kind=None):
    return [r for r in sink.m["pictures"] if kind is None or r["kind"] == kind]


def ordinal(d):
    return {a["id"]: i for i, a in enumerate(d["anchors"])}


def test_main_level_draws_a_plate_per_chapter_and_a_portrait_per_character():
    il, rep, sink, t, im = run()
    plates = pics(sink, "plate")
    assert [p["target"] for p in plates] == ["One", "Two"] and rep.drawn == len(sink.m["pictures"]) == len(im.calls)
    assert {p["target"] for p in pics(sink, "portrait")} == {"mira", "oren", "stranger", "gabriel"}
    assert sink.m["world"] is None and sink.m["style"] == "ink" and "appearance" not in sink.m and "groups" not in sink.m
    assert all(p["id"].startswith("p") and p["file"] in sink.files and p["width"] == 1024 for p in sink.m["pictures"])
    assert [p["shape"] for p in plates] == ["landscape"] * 2 and {p["shape"] for p in pics(sink, "portrait")} == {"portrait"}


def test_the_main_moment_is_the_turning_point_and_all_level_adds_the_other_drawable_ones():
    il, rep, sink, t, im = run()
    one = next(p for p in pics(sink, "plate") if p["target"] == "One")
    assert '<moment chapter="One">Oren the miller comes out' in next(s for s in t.scenes() if "main moment" in s) and one["from"] == "a5"
    _, _, sink2, _, _ = run(level="all")
    events = [p["target"] for p in pics(sink2, "event")]
    assert events == ["e1", "e5", "e6"] or set(events) == {"e1", "e5", "e6"}   # e2 and e4 are the plates; e3 is minor
    assert sink2.m["screened"]["One"]["events"] and "e3" not in {x for ms in sink2.m["screened"].values() for x in ms["events"]}


def test_a_chapter_with_nothing_drawable_gets_its_recap_as_a_plate():
    il, rep, sink, t, im = run(text_=Text(screen=lambda ns, p: []))
    assert [p["target"] for p in pics(sink, "plate")] == ["One", "Two"]
    assert any('<moment chapter="One">Mira goes about her day at the mill in one.</moment>' in s for s in t.scenes())


def test_a_custom_style_is_the_persons_own_sentence():
    il, rep, sink, t, im = run(style="Chalk on a blackboard, smudged white lines.")
    assert "Chalk on a blackboard" in sink.m["pictures"][0]["prompt"] and sink.m["style"].startswith("Chalk")
    assert art.style_slug("Chalk on a blackboard, smudged.") == "chalk-on-a-blackboard-smudged"


def test_no_picture_is_written_from_text_after_its_anchor():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il, rep, sink, t, im = run(d, bk, level="all")
    o = ordinal(d)
    for p in sink.m["pictures"]:
        assert all(o[a] <= o[p["from"]] for a in p["sources"]), p
    early = [p for p in sink.m["pictures"] if o[p["from"]] < o["a6"]]
    assert early
    for p in early:
        # nothing from chapter two (the Stranger, Gabriel, Zephyr) reached the writer of an early picture
        assert "Zephyr" not in p["prompt"] and "Stranger" not in p["prompt"]
    for s in t.scenes():
        if "Mira walked to the mill" in s and "Oren came out" in s:
            assert "Zephyr" not in s
    assert not any("Zephyr" in s for s in t.scenes() if "A small errand" in s)


def test_a_hidden_identity_gets_its_own_portrait_and_a_scene_never_borrows_the_others():
    il, rep, sink, t, im = run(level="all")
    by = {p["target"]: p for p in pics(sink, "portrait")}
    assert by["stranger"]["id"] != by["gabriel"]["id"]
    scene = next(p for p in pics(sink) if p["target"] == "e6")       # names mira and the stranger
    assert by["stranger"]["id"] in scene["references"] and by["gabriel"]["id"] not in scene["references"]
    assert by["oren"]["id"] not in scene["references"] and len(scene["references"]) <= art.MAX_REFERENCES
    gab = next(p for p in pics(sink) if p["target"] == "e5")
    assert gab["references"] == [by["gabriel"]["id"]]


def test_references_are_the_pictures_of_the_named_and_attached_as_bytes():
    coin = lambda p: PORTRAIT_SCENE if is_portrait(p) else SCENE + (" A coin glints." if "<moment" in p and "silver coin" in p.split("</moment>")[0] else "")   # noqa: E731
    il, rep, sink, t, im = run(level="all", text_=Text(scene=coin))
    e6 = next(p for p in pics(sink) if p["target"] == "e6")
    call = next(c for c in im.calls if c["prompt"] == e6["prompt"])
    assert len(call["references"]) == len(e6["references"]) and all(r in sink.files.values() for r in call["references"])


def test_unknown_names_and_names_of_characters_get_one_rewrite_then_a_skip_with_a_reason():
    il, rep, sink, t, im = run(text_=Text(scene=lambda p: "A tall rider named Valdemar rides a horse across a wide field under a heavy sky of low grey cloud, "
                                                          "a lone rider small against hills, the light thin and cold, mud on the road."))
    assert not pics(sink, "plate") and rep.skipped["dropped: unknown name in prompt"] >= 2
    # a name kept after the rewrite is replaced by code and the text checked again, so the picture is not lost to it
    il, rep, sink, t, im = run(text_=Text(scene=lambda p: SCENE.replace("A woman", "Mira, a woman")))
    plates = pics(sink, "plate")
    assert len(plates) == 2 and all("Mira," not in p["prompt"] and p["prompt"].startswith("The figure, a woman") for p in plates)
    assert rep.dropped["fixed: name replaced by a role"] == rep.drawn and not rep.skipped["dropped: name in prompt"]
    assert sum(1 for p in t.prompts if "It was rejected" in p) >= rep.drawn        # one rewrite each first


def test_a_refusal_gets_one_quieter_rewrite_then_a_recorded_skip():
    quiet = ("A door stands ajar in a stone mill at dusk and a long shadow lies across the worn floor. A lamp burns low on a wooden table "
             "beside a woven basket, and the last grey light comes through one small window. The room is quiet and empty and the air is still.")
    t = Text(scene=lambda p: quiet if "quieter composition" in p else SCENE)
    im = Image(refuse=[lambda prompt, n: prompt.startswith(SCENE[:30]) and n <= 1])
    il, rep, sink, t, im = run(text_=t, image=im, portraits=False)
    assert rep.refused == 0 and rep.softened == 1 and any(c["prompt"].startswith(quiet) for c in im.calls)
    assert len(pics(sink, "plate")) == 2
    t2 = Text()
    im2 = Image(refuse=[lambda prompt, n: True])
    il, rep, sink2, t2, im2 = run(text_=t2, image=im2, portraits=False)
    assert not pics(sink2) and rep.refused == 2 and not rep.skipped   # two plates, each refused twice: counted once, not as skips
    assert len(im2.calls) == 4
    assert {s["reason"] for s in sink2.m["skipped"]} == {"refused: provider declined"} and all(s["final"] for s in sink2.m["skipped"])
    assert any("safety: violence" in r["reason"] for r in rep.reasons)
    before = len(im2.calls)
    run(il.books[0].doc, il.books[0].epub, sink2, Text(), im2, portraits=False)   # resume: a refusal is not tried again
    assert len(im2.calls) == before


def test_a_provider_error_is_sent_again_only_when_it_never_reached_the_provider():
    class Flaky(Image):
        def __init__(self, sent):
            super().__init__()
            self.sent = sent

        def draw(self, prompt, *, shape="landscape", quality="standard", references=()):
            if shape == "portrait":
                return super().draw(prompt, shape=shape, quality=quality, references=references)
            self.calls.append({"prompt": prompt, "shape": shape})
            raise llm.ProviderError("could not reach the host" if self.sent is False else "the request failed after it was sent",
                                    None, self.sent)
    il, rep, sink, t, im = run(image=Flaky(False))                  # refused connection, 429: never sent, so sent once more
    assert sum(c["shape"] == "landscape" for c in im.calls) == 4 and not pics(sink, "plate")
    assert rep.skipped["skipped: provider error"] == 2 and rep.at_risk == 0
    il, rep, sink, t, im = run(image=Flaky(None))                   # a timeout after sending: maybe billed, never sent twice
    assert sum(c["shape"] == "landscape" for c in im.calls) == 2 and rep.skipped["skipped: provider error"] == 2
    assert rep.at_risk == pytest.approx(0.1) and "failed after sending" in rep.lines()[0]


def test_a_call_that_failed_after_sending_counts_against_the_cap():
    class Lost(Image):
        sends = 0

        def draw(self, prompt, *, shape="landscape", quality="standard", references=()):
            self.sends += 1
            if self.sends > 1:
                raise llm.ProviderError("the request failed after it was sent: timed out")
            return super().draw(prompt, shape=shape, quality=quality, references=references)
    il, rep, sink, t, im = run(image=Lost(cost=0.4), cap=1.0)
    assert rep.drawn == 1 and rep.at_risk == pytest.approx(0.4) and im.sends == 2 and "cost cap" in rep.stopped


def test_the_cost_cap_stops_the_run_and_keeps_what_was_drawn():
    il, rep, sink, t, im = run(image=Image(cost=0.4), cap=1.0)
    assert rep.drawn == 2 and len(pics(sink)) == 2 and "cost cap" in rep.stopped and len(im.calls) == 2
    assert sink.m["cost"] == pytest.approx(0.8)
    il2, rep2, sink2, _, im2 = run(il.books[0].doc, il.books[0].epub, sink, image=Image(cost=0.4), cap=5.0)
    assert len(pics(sink2)) > 2 and rep2.stopped == ""                # the same set resumes under a higher cap


def test_a_picture_limit_stops_the_run():
    il, rep, sink, t, im = run(max_pictures=3)
    assert rep.drawn == 3 and "limit" in rep.stopped


def test_resume_draws_nothing_it_already_has_and_asks_the_text_model_nothing():
    il, rep, sink, t, im = run(level="all")
    n_img, n_txt = len(im.calls), len(t.prompts)
    il2, rep2, _, t2, im2 = run(il.books[0].doc, il.books[0].epub, sink, level="all")
    assert not im2.calls and not t2.prompts and rep2.drawn == 0 and len(pics(sink)) == n_img


def test_a_look_change_is_the_classifiers_call_on_the_entitys_own_item():
    # a lost eye is a look change, a death alone is not, and what the entity does to something else never is (Sevro and the deer)
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    oren = d["entities"][1]
    oren["facets"].append({"id": "bk.oren.f1", "from": "a10", "basis": "stated", "evidence": ["a10"], "text": "Oren lost an eye in the storm."})
    il, rep, sink, t, im = run(d, bk)
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "oren"] == ["a3", "a10"]
    assert sink.m["looks"]["oren"]["items"]["bk.oren.f1"]["change"] is True
    oren["facets"][-1]["text"] = "Oren is found dead on the mill floor."
    _, _, sink2, _, _ = run(d, bk)
    assert [p["from"] for p in pics(sink2, "portrait") if p["target"] == "oren"] == ["a3"]
    # a change flag on an item number the call was never given, or for another entity's item, is not taken
    oren["facets"][-1]["text"] = "Oren drags the dead deer to the mill."
    t = Text(change=lambda txt: "the dead deer" if "deer" in txt else "")
    _, _, sink3, _, _ = run(d, bk, text_=t)
    assert [p["from"] for p in pics(sink3, "portrait") if p["target"] == "oren"] == ["a3", "a10"]   # the fake says so: code obeys

    class Wild(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "notes a reader's companion keeps on some entries" in prompt:
                self.prompts.append(prompt)
                return {"items": [{"n": 99, "look": True, "change": "remade"}, {"n": "x"}, {"n": 1, "look": True}]}
            return super().complete(system, prompt, schema, prefix)
    il, _, sink4, _, _ = run(d, bk, text_=Wild())
    flags = sink4.m["looks"]
    assert all("change" in v and not v["change"] for x in flags.values() for v in x["items"].values())
    assert sum(len(x["items"]) for x in flags.values()) == 1           # number 1 of the one call, nothing else


class Dummy:   # keeps the series test's second book readable
    pass


def series_docs():
    bk1 = book(("One", CH1), ("Two", CH2))
    d1 = base_doc(bk1)
    d1.update({"id": "b1", "series": {"id": "mill", "book": "b1"}})
    bk2 = book(("Three", ["Mira came back to the mill years later with a scar on her cheek and a new coat."] + CH1[1:],), )
    ents = [ent("mira", "Mira", frm="b2.a0", facets=[portrait("mira", "b2.a3", "A scarred young woman, older and harder, back at the mill.", 1, "b2")]),
            ent("oren", "Oren", frm="b2.a1", facets=[portrait("oren", "b2.a2", "The old miller, broad, with white hair, flour on his "
                                                                 "sleeves and a leather apron, who runs the mill.", 1, "b2")])]
    evs = [event(1, 1, "Oren the miller greets her at the door of the mill.", ["oren", "mira"], "turn", "b2."),
           event(2, 4, "Mira walks to the well behind the mill.", ["mira"], "turn", "b2.")]
    d2 = doc(bk2, "b2", "b2.", ents, evs, [recap(1, 5, "Three", "b2.")])
    for e in d2["entities"]:
        for n in e["names"]:
            n["from"] = e["from"]
    return bk1, d1, bk2, d2


def test_an_earlier_book_without_its_epub_is_only_read():
    bk1, d1, bk2, d2 = series_docs()
    s1 = Mem()
    t = Text()
    il = art.Illustrator([art.Book(d1, None, s1, None, draw=False), art.Book(d2, bk2, Mem())], t, Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    rep = il.run()
    assert s1.m is None and rep.drawn > 0
    assert {p["target"] for p in pics(il.books[1].b.sink, "portrait")} == {"mira", "oren"}   # nothing earlier to inherit


def test_on_demand_job_and_prompt_refuse_a_target_the_reader_has_not_reached():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][2]["facets"].append({"id": "bk.mill.f1", "from": "a1", "basis": "stated", "evidence": ["a1"],
                                       "text": "A grey stone mill with a wooden gate and a wheel on the river."})
    t = Text()
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    with pytest.raises(art.NotVisible):
        il.job("event", "e4", at="a3")
    with pytest.raises(art.NotVisible):
        il.job("portrait", "stranger", at="a3")
    job = il.job("event", "e2", at="a3")       # nothing holds a picture back: it shows from its own anchor
    pr = il.make_prompt(job)
    assert pr.anchor == "a1" and pr.text.startswith(SCENE) and pr.references == []
    place = il.job("place", "mill", at="a6", shape="square")
    assert il.make_prompt(place).shape == "square" and place.kind == "place"


def test_size_of_reads_png_jpeg_and_webp_headers():
    assert art.size_of(png(640, 480, b"")) == (640, 480)
    jpg = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x0b\x08\x01\xe0\x02\x80\x03\x00\x00\x00"
    assert art.size_of(jpg) == (640, 480)
    assert art.size_of(b"not a picture") == (None, None)


def test_file_sink_roundtrips_the_manifest_and_pictures(tmp_path):
    s = art.FileSink(tmp_path / "x" / "ink")
    assert s.manifest() is None and s.read("p1.png") is None
    s.put("p1.png", b"abc")
    s.save({"a": 1})
    assert s.manifest() == {"a": 1} and s.read("p1.png") == b"abc"


def test_progress_is_reported_per_picture():
    seen = []
    il = art.Illustrator([art.Book(base_doc(book(("One", CH1), ("Two", CH2))), book(("One", CH1), ("Two", CH2)), Mem())], Text(), Image(),
                         "ink", workers=1, log=lambda *_: None, pause=0, progress=lambda d, n: seen.append((d, n)))
    il.run()
    assert seen[-1][0] == seen[-1][1] and seen[0][1] == len(seen) and [d for d, _ in seen] == list(range(1, len(seen) + 1))


# ---- the portraits round (the lead's Dracula check run, chapters I to III)

VESNA_CH1 = CH1[:2] + ["Mira told Oren that Vesna would write to him soon from the town across the river."] + CH1[3:]
VESNA_CH2 = CH2[:3] + ["Vesna stood at the mill door, a tall old woman with white hair, a black shawl and a hooked nose."] + CH2[4:]


def vesna_doc():
    bk = book(("One", VESNA_CH1), ("Two", VESNA_CH2))
    d = base_doc(bk)
    d["entities"].append(ent("vesna", "Vesna", frm="a2", facets=[
        portrait("vesna", "a2", "A woman who writes letters to the miller from the town across the river."),
        {"id": "bk.vesna.f1", "from": "a9", "basis": "stated", "evidence": ["a9"], "text": "Is a tall old woman with white hair."}]))
    d["events"] += [event(7, 2, "Mira tells Oren that a letter from the town is coming.", ["vesna", "oren"]),
                    event(8, 9, "Vesna arrives at the mill door at last.", ["vesna", "mira"])]
    return bk, d


VESNA = ("an old woman", ["tall", "white hair", "a black shawl", "a hooked nose"])


def test_a_portrait_shows_from_where_the_look_is_first_described_not_from_a_first_profile_without_one():
    # Dracula: the Count's first profile (chapter I, a letter writer) shows no look; chapter II describes him at length
    bk, d = vesna_doc()
    il, rep, sink, t, im = run(d, bk)
    [v] = [p for p in pics(sink, "portrait") if p["target"] == "vesna"]
    assert v["from"] == "a9" and "a9" in v["sources"]                                       # the facet that describes her
    asked = [p for p in t.scenes() if drawing(p) == "Vesna"]
    assert "a tall old woman with white hair" in asked[-1]                                   # written from that paragraph
    # a paragraph that names her beside look words is not enough: it may describe someone else
    bk, d = vesna_doc()
    d["entities"][-1]["facets"].pop()
    il, rep, sink, t, im = run(d, bk)
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "vesna"] == ["a2"]   # at her first profile


# ---- portraits round 2 (review-portraits.md): no look reaches a picture unless the text states it

def test_names_are_replaced_by_a_role_when_the_rewrite_still_names_someone():
    # Jonathan Harker's portrait was dropped with "dropped: name in prompt"
    def scene(p):
        return ("A portrait of Mira, Oren's helper, " + PORTRAIT_SCENE[2:]) if is_portrait(p) else SCENE
    il, rep, sink, t, im = run(text_=Text(scene=scene))
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert m["prompt"].startswith("A portrait of the figure, the figure's helper, head-and-shoulders portrait")
    names = il.names[0]
    assert names.strip("Mr. Oren's mill and the Stranger at the door; Dawn breaks.") == \
        "The figure's mill and the figure at the door; Dawn breaks."


def test_portraits_are_drawn_most_named_first_so_a_cap_keeps_the_main_characters():
    il, rep, sink, t, im = run(max_pictures=2)
    assert [p["target"] for p in pics(sink)] == ["mira", "oren"] or {p["target"] for p in pics(sink)} <= {"mira", "oren", "stranger"}
    assert "mira" in {p["target"] for p in pics(sink)}


def test_illustrate_command_writes_the_art_set_beside_the_index_and_never_the_index(tmp_path, monkeypatch, capsys):
    import json

    from lorekeeper import cli
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    th = text.text_hash(b.words for b in bk.blocks)
    d["editions"] = [{"textHash": th, "builtFrom": True}]
    idx = tmp_path / "mill.lore.json"
    idx.write_text(json.dumps(d))
    before = idx.read_bytes()
    t, im = Text(), Image()
    monkeypatch.setattr(cli, "_make_model", lambda args, model=None, provider=None: t)
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: im)
    monkeypatch.setattr(epub, "read", lambda p: bk)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert cli.main(["illustrate", str(idx), "--epub", "x.epub", "--style", "comic-strip", "--text-provider", "gemini", "--cap", "5"]) == 0
    out = tmp_path / "art" / th / "comic-strip"
    m = json.loads((out / "art.json").read_text())
    assert m["style"] == "comic-strip" and len(m["pictures"]) == len(im.calls) and all((out / p["file"]).exists() for p in m["pictures"])
    assert idx.read_bytes() == before and "drawn" in capsys.readouterr().out
    n = len(im.calls)
    assert cli.main(["illustrate", str(idx), "--epub", "x.epub", "--style", "comic-strip", "--text-provider", "gemini"]) == 0
    assert len(im.calls) == n                                                    # resumed: nothing drawn twice


# ---- round 2: each of these fails on the first version of art.py


def test_a_plate_never_shows_from_before_the_event_it_shows():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["recaps"][0]["from"] = d["recaps"][0]["evidence"][0] = "a2"      # the chapter's recap comes before its turning point
    d["events"][1].update({"from": "a3", "evidence": ["a3"]})
    il, rep, sink, t, im = run(d, bk)
    o = ordinal(d)
    one = next(p for p in pics(sink, "plate") if p["target"] == "One")
    assert o[one["from"]] >= o["a3"] and "a3" in one["sources"]
    assert all(o[a] <= o[p["from"]] for p in sink.m["pictures"] for a in p["sources"])
    assert o[il.job("plate", "One").anchor] >= o["a3"]                    # on demand too
    with pytest.raises(art.NotVisible):
        il.job("plate", "One", at="a2")
    # a plate with no drawable event takes its figures from every event of the chapter: it shows from after all of them
    _, _, sink2, _, _ = run(d, bk, text_=Text(screen=lambda ns, p: []))
    assert o[next(p for p in pics(sink2, "plate") if p["target"] == "One")["from"]] >= o["a4"]


def test_screening_only_chooses_and_nothing_it_writes_reaches_a_prompt():
    class Framing(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "Which of them are a visible moment" in prompt:
                self.prompts.append(prompt)
                ns = [int(n) for n in re.findall(r"^(\d+)\. ", prompt, re.M)]
                return {"moments": [{"event": n, "frame": "Zephyr the storm breaks over the mill."} for n in ns]}
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(text_=Framing(), level="all")
    assert pics(sink, "event") and not any("Zephyr the storm breaks" in s for s in t.scenes())
    assert "Zephyr the storm breaks" not in str(sink.m["screened"])
    e1 = next(s for s in t.scenes() if '<moment chapter="One">Mira walks to the mill' in s)
    assert "Gabriel" not in e1 and "silver coin" not in e1


def test_anchors_without_seq_hints_are_found_in_the_epub():
    bk = book(("One", CH1), ("Two", CH2))
    _, _, sink, _, _ = run(base_doc(bk), bk)
    d = base_doc(bk)
    for a in d["anchors"]:
        del a["seq"]
    _, rep, sink2, _, _ = run(d, bk)
    assert [(p["kind"], p["target"], p["from"]) for p in pics(sink2)] == [(p["kind"], p["target"], p["from"]) for p in pics(sink)]
    other = book(("X", ["Nothing in this book matches the index at all, not one paragraph of it."] * 3))
    with pytest.raises(ValueError, match="edition"):
        art.Illustrator([art.Book(d, other, Mem())], Text(), Image(), "ink")


def test_a_text_model_failure_skips_that_picture_and_the_run_goes_on():
    class Failing(Text):
        def __init__(self, every=True):
            super().__init__()
            self.every, self.failed = every, 0

        def complete(self, system, prompt, schema, prefix=None):
            if '<moment chapter="One">Oren the miller' in prompt and (self.every or not self.failed):
                self.failed += 1
                raise llm.ProviderError("HTTP 429: slow down")
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(text_=Failing())
    assert "One" not in {p["target"] for p in pics(sink, "plate")} and "Two" in {p["target"] for p in pics(sink, "plate")}
    assert rep.skipped["skipped: text model failed"] == 1 and t.failed == 2 and pics(sink, "portrait")
    assert any("HTTP 429" in r["reason"] for r in rep.reasons)
    il, rep, sink, t, im = run(text_=Failing(every=False))                   # a passing failure is retried once
    assert "One" in {p["target"] for p in pics(sink, "plate")} and not rep.skipped


def test_a_later_book_knows_the_words_of_the_books_the_reader_finished():
    bk1, d1, bk2, d2 = series_docs()
    scene = SCENE + " A storm the farmers call Zephyr gathers far off."
    s1, s2 = Mem(), Mem()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], Text(scene=lambda p: scene), Image(), "ink",
                         portraits=False, workers=1, log=lambda *_: None, pause=0)
    rep = il.run()
    assert [p["target"] for p in pics(s1, "plate")] == ["Two"]               # Zephyr is not named before a8 in book one
    assert [p["target"] for p in pics(s2, "plate")] == ["Three"]             # book two never names it, book one did


def test_a_rebuilt_index_screens_its_chapters_again():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il, rep, sink, t, im = run(d, bk, level="all")
    d2 = base_doc(bk)
    d2["events"] = [e for e in d2["events"] if e["id"] != "e5"]
    d2["events"][-1]["id"] = "e9"
    il2, rep2, _, t2, _ = run(d2, bk, sink, level="all")
    screens = [p for p in t2.prompts if "Which of them are a visible moment" in p]
    assert len(screens) == 1 and "Mira finds a silver coin" in screens[0]
    assert sink.m["screened"]["Two"]["events"] == ["e4", "e9"]
    sink.m["screened"] = {"One": [{"event": "e1", "frame": "old"}], "Two": [{"event": "e5", "frame": "old"}]}   # round 1's shape
    _, _, _, t3, _ = run(d2, bk, sink, level="all")
    assert len([p for p in t3.prompts if "Which of them are a visible moment" in p]) == 2


def test_a_series_book_draws_a_portrait_the_earlier_book_planned_but_did_not_draw():
    bk1, d1, bk2, d2 = series_docs()
    seen = {"n": 0}

    class RefuseOrenOnce(Image):
        def draw(self, prompt, *, shape="landscape", quality="standard", references=()):
            if shape == "portrait" and prompt.startswith(OREN_SCENE[:30]) and seen["n"] < 2:
                seen["n"] += 1
                self.calls.append({"prompt": prompt, "shape": shape, "references": []})
                raise images.ImageRefused("safety")
            return super().draw(prompt, shape=shape, quality=quality, references=references)
    s1, s2 = Mem(), Mem()
    t = Text(scene=lambda p: OREN_SCENE if drawing(p) == "Oren" else (PORTRAIT_SCENE if is_portrait(p) else SCENE))
    il = art.Illustrator([art.Book(d1, bk1, s1, None), art.Book(d2, bk2, s2)], t, RefuseOrenOnce(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    rep = il.run()
    assert "oren" not in {p["target"] for p in pics(s1, "portrait")} and rep.refused == 1
    assert "oren" in {p["target"] for p in pics(s2, "portrait")}


def test_on_demand_job_refuses_an_unknown_chapter_and_finds_an_earlier_books_entity():
    bk1, d1, bk2, d2 = series_docs()
    il = art.Illustrator([art.Book(d1, bk1, Mem()), art.Book(d2, bk2, Mem())], Text(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    with pytest.raises(ValueError, match="no such chapter"):
        il.job("plate", "Nowhere")
    with pytest.raises(ValueError, match="no such event"):
        il.job("event", "e99")
    j = il.job("portrait", "stranger", at="b2.a2")
    assert j.bi == 0 and j.anchor in ordinal(d1)


def test_three_workers_draw_what_one_does():
    _, rep1, s1, _, _ = run(level="all")
    _, rep3, s3, _, _ = run(level="all", workers=3)
    key = lambda s: sorted((p["kind"], p["target"], p["from"]) for p in pics(s))   # noqa: E731
    assert key(s1) == key(s3) and rep1.drawn == rep3.drawn


def _cli_index(tmp_path, d, bk, name):
    import json
    d["editions"] = [{"textHash": text.text_hash(b.words for b in bk.blocks), "builtFrom": True}]
    p = tmp_path / name
    p.write_text(json.dumps(d))
    return p


def test_illustrate_command_refuses_a_quality_the_model_lacks_and_another_edition(tmp_path, monkeypatch):
    from lorekeeper import cli
    bk = book(("One", CH1), ("Two", CH2))
    idx = _cli_index(tmp_path, base_doc(bk), bk, "mill.lore.json")
    im = Image()
    monkeypatch.setattr(cli, "_make_model", lambda args, model=None, provider=None: Text())
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: im)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(epub, "read", lambda p: bk)
    with pytest.raises(SystemExit, match="draft, standard"):
        cli.main(["illustrate", str(idx), "--epub", "x.epub", "--model", "gemini-3.1-flash-lite-image", "--quality", "best"])
    other = book(("One", CH2), ("Two", CH1))
    monkeypatch.setattr(epub, "read", lambda p: other)
    with pytest.raises(SystemExit, match="not the edition"):
        cli.main(["illustrate", str(idx), "--epub", "x.epub"])
    assert not im.calls


def test_illustrate_command_gives_earlier_books_portraits_only_unless_a_level_is_given(tmp_path, monkeypatch):
    import json

    from lorekeeper import cli
    bk1, d1, bk2, d2 = series_docs()
    i1, i2 = _cli_index(tmp_path, d1, bk1, "b1.lore.json"), _cli_index(tmp_path, d2, bk2, "b2.lore.json")
    monkeypatch.setattr(cli, "_make_model", lambda args, model=None, provider=None: Text())
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: Image())
    monkeypatch.setattr(epub, "read", lambda p: {"1.epub": bk1, "2.epub": bk2}[p])
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert cli.main(["illustrate", str(i1), str(i2), "--epub", "1.epub", "--epub", "2.epub", "--out", str(tmp_path / "o")]) == 0
    m1 = json.loads(next((tmp_path / "art").glob("*/digital-art/art.json")).read_text())
    m2 = json.loads((tmp_path / "o" / "art.json").read_text())
    assert {p["kind"] for p in m1["pictures"]} == {"portrait"} and "plate" in {p["kind"] for p in m2["pictures"]}


# ---- round 3


def series_shared_ids():
    """Two books whose anchors have the same ids (the generator's tocNN.paraMM repeat in every book of a series)."""
    bk1, d1, _, _ = series_docs()
    bk2 = book(("Three", ["Mira came back to the mill years later with a new coat and a basket of eggs."] + CH1[1:]),
               ("Four", CH2))
    ents = [ent("mira", "Mira", facets=[portrait("mira", "a3", "A slight young woman of about twenty who carries baskets.", 1, "b2")]),
            ent("oren", "Oren", frm="a1", facets=[portrait("oren", "a2", "The old miller, white haired and broad, who runs the mill.", 1, "b2")])]
    evs = [event(1, 1, "Oren the miller greets her at the door of the mill.", ["oren", "mira"], "turn"),
           event(2, 3, "The hooded stranger is seen again on the road by the mill.", ["stranger", "mira"], "turn"),
           event(3, 8, "Mira walks to the well behind the mill.", ["mira"], "turn")]
    d2 = doc(bk2, "b2", "", ents, evs, [recap(1, 5, "Three"), recap(2, 11, "Four")])
    for e in d2["entities"]:
        for n in e["names"]:
            n["from"] = e["from"]
    return bk1, d1, bk2, d2


def test_anchor_ids_shared_by_two_books_are_never_confused(monkeypatch):
    bk1, d1, bk2, d2 = series_shared_ids()
    d1["entities"][3]["facets"][0].update({"from": "a7", "evidence": ["a7"]})   # the stranger's book-one profile at a7
    s1, s2 = Mem(), Mem()
    il = art.Illustrator([art.Book(d1, bk1, s1, None), art.Book(d2, bk2, s2, "all")], Text(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    rep = il.run()
    assert not rep.skipped.get("dropped: written from content after its anchor")
    o = ordinal(d2)
    early = [p for p in pics(s2) if o[p["from"]] < o["a5"]]
    assert {p["target"] for p in early} >= {"e2"}                            # book two's opening is drawn, held to nothing
    e2 = next(p for p in pics(s2) if p["target"] == "e2")
    assert "a7" not in e2["sources"] and all(o[a] <= o[e2["from"]] for a in e2["sources"])


def test_a_reservation_is_released_by_the_amount_it_reserved():
    il = art.Illustrator([art.Book(base_doc(book(("One", CH1), ("Two", CH2))), book(("One", CH1), ("Two", CH2)), Mem())],
                         Text(), Image(), "ink", portraits="all", cap=10, log=lambda *_: None, pause=0)
    _, a = il._reserve(0.0)                    # an unknown price, nothing drawn yet: reserves 0
    il.last_cost = 0.04
    _, b = il._reserve(0.0)                    # reserves the last picture's cost
    il._release(a)
    assert il.reserved == pytest.approx(0.04) and b == pytest.approx(0.04)
    il._release(b)
    assert il.reserved == 0 and il.in_flight == 0


def test_an_on_demand_picture_of_a_read_only_book_saves_that_books_set():
    bk1, d1, bk2, d2 = series_docs()
    s1 = Mem()
    il = art.Illustrator([art.Book(d1, bk1, s1, None, draw=False), art.Book(d2, bk2, Mem())], Text(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    rec = il.draw(il.job("portrait", "stranger", at="b2.a3"))
    assert rec and s1.m and [p["id"] for p in s1.m["pictures"]] == [rec["id"]] and rec["file"] in s1.files


def test_a_set_is_the_same_for_every_style_text_with_its_slug_and_belongs_to_one_edition():
    sink = Mem()
    run(sink=sink, style="Chalk on a blackboard, smudged white lines.", portraits=False)
    n = len(pics(sink))
    il, rep, _, _, im = run(sink=sink, style="chalk on a BLACKBOARD smudged white lines", portraits=True)
    assert sink.m["style"] == "Chalk on a blackboard, smudged white lines." and len(pics(sink)) > n
    assert all("Chalk on a blackboard, smudged white lines." in c["prompt"] for c in im.calls)
    with pytest.raises(ValueError, match="another style"):
        run(sink=sink, style="ink")
    other = base_doc(book(("One", CH1), ("Two", CH2)))
    other["editions"] = [{"textHash": "sha256:else", "builtFrom": True}]
    with pytest.raises(ValueError, match="another edition"):
        run(other, sink=sink, style="Chalk on a blackboard, smudged white lines.")


def test_several_standalone_indexes_are_refused(tmp_path, monkeypatch):
    from lorekeeper import cli
    bk = book(("One", CH1), ("Two", CH2))
    with pytest.raises(ValueError, match="one series"):
        art.Illustrator([art.Book(base_doc(bk), bk, Mem()), art.Book(base_doc(bk), bk, Mem())], Text(), Image(), "ink")
    a, b = _cli_index(tmp_path, base_doc(bk), bk, "a.lore.json"), _cli_index(tmp_path, base_doc(bk), bk, "b.lore.json")
    im = Image()
    monkeypatch.setattr(cli, "_make_model", lambda args, model=None, provider=None: Text())
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: im)
    monkeypatch.setattr(epub, "read", lambda p: bk)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    with pytest.raises(SystemExit, match="one series"):
        cli.main(["illustrate", str(a), str(b), "--epub", "1.epub", "--epub", "2.epub"])
    assert not im.calls


def test_a_trailing_anchor_not_in_the_epub_is_never_reached():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["anchors"][-1]["fp"] = "0000000000000000"
    d["anchors"][-2]["fp"] = "0000000000000001"
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", portraits="all", log=lambda *_: None)
    b = il.books[0]
    assert b.seq_of["a11"] == b.seq_of["a10"] == len(bk.blocks) and b.hi == len(bk.blocks) - 1 and b.seq_of["a9"] == 9


def test_counts_need_no_model_call():
    bk = book(("One", CH1), ("Two", CH2))
    t = Text()
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], t, Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    c = il.counts()
    assert c == {"plates": 2, "portraits": 4, "chapters": [{"title": "One", "drawable": None}, {"title": "Two", "drawable": None}]}
    assert not t.prompts
    il.run()
    n = len(t.prompts)
    assert [x["drawable"] for x in il.counts()["chapters"]] == [2, 3] and len(t.prompts) == n


def test_an_edited_prompt_is_never_softened_and_its_refusal_is_raised():
    bk = book(("One", CH1), ("Two", CH2))
    t, im = Text(), Image(refuse=[lambda prompt, n: True])
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], t, im, "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    with pytest.raises(images.ImageRefused):
        il.draw(il.job("plate", "One"), text="My own words: a mill at dusk, a woman at the gate, nothing else in view at all.")
    assert len(im.calls) == 1 and not any("quieter composition" in p for p in t.prompts) and il.report.refused == 1
    assert not il.man[0]["skipped"]                    # the person may try again


# ---- round 3, item 13: what a prompt inherits from earlier books, and readers who have not finished them

def names_for(entities):
    """The banned names of a one-book run over the fixture book with these entities."""
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"] = d["entities"] + entities
    return art._Names([(n["value"], e["type"] == "group") for e in d["entities"] if e["type"] in ("person", "creature", "group")
                       for n in e["names"]])


@pytest.mark.parametrize("text_", [
    "A half-Mira grin and an Oren-like frown.",                          # inside hyphenated words
    "Mira's basket and Mira’s shawl, Oren's apron and Oren’s hat.",      # both apostrophes
    "Mr. Oren waits; Professor Jonathan Harker’s case lies by Miss Mira.",   # titles before the name
    "A banner reads MIRA and JONATHAN HARKER in the dark.",              # capitals, a one-word name and a full one
    "The Stranger stands at the door. \"The Stranger waits,\" she says.",   # "The Stranger" opening a sentence
    "The Millers’ cart and the Jean-François look by the half-Jonathan Harker gate.",
])
def test_strip_removes_every_name_find_reports(text_):
    names = names_for([ent("harker", "Jonathan Harker", frm="a2"), ent("jf", "Jean-François", frm="a2"),
                       ent("millers", "the Millers", "group", frm="a2")])
    assert names.find(text_), text_
    out = names.strip(text_)
    assert not names.find(out), (out, names.find(out))
    assert not re.search(r"Mira|Oren|Harker|MIRA|HARKER|Stranger|François|Millers", out), out


def test_strip_keeps_an_ordinary_word_opening_a_sentence_and_puts_a_bare_noun_in_a_hyphenated_word():
    names = names_for([ent("millers", "the Millers", "group", frm="a2")])
    assert names.find("Stranger things wait.") == "" and names.strip("Stranger things wait.") == "Stranger things wait."
    assert names.strip("A half-Mira grin; Oren-like.") == "A half-figure grin; figure-like."
    assert names.strip("The Stranger stands.") == "The figure stands."
    assert names.strip("A red-haired Oren twin.", attributive=True) == "A red-haired twin."
    assert names.strip("A first-year Oren", attributive=True) == "A first-year"
    assert names.strip("A knight of the Millers", attributive=True) == "A knight"
    assert names.strip("The Millers", attributive=True) == "The people"
    assert names.strip("Mira's helper", attributive=True) == "The figure's helper"


def test_a_portrait_naming_itself_as_the_stranger_at_a_sentence_start_is_fixed_not_dropped():
    # "The Stranger ..." opening the scene: find saw the name, strip left it (it took "The" for the sentence's first word)
    def scene(p):
        return ("The Stranger sits by the fire, " + PORTRAIT_SCENE[2:]) if is_portrait(p) else SCENE
    il, rep, sink, t, im = run(text_=Text(scene=scene))
    [s] = [p for p in pics(sink, "portrait") if p["target"] == "stranger"]
    assert s["prompt"].startswith("The figure sits by the fire, head-and-shoulders portrait")
    assert rep.dropped["fixed: name replaced by a role"] >= 1 and not rep.dropped["dropped: name in prompt"]


def test_a_hyphenated_name_in_the_scene_is_fixed_not_dropped():
    def scene(p):
        return ("A half-Mira smile, " + PORTRAIT_SCENE[2:]) if is_portrait(p) else SCENE
    il, rep, sink, t, im = run(text_=Text(scene=scene))
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert m["prompt"].startswith("A half-figure smile, head-and-shoulders portrait") and "Mira" not in m["prompt"]


def test_a_dropped_prompt_says_the_check_and_the_word_only_when_the_reader_has_met_it():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il = art.Illustrator([art.Book(d, bk, Mem(), "main")], Text(), Image(), "ink", log=lambda *_: None, pause=0)
    assert il._say("name in the text: Oren", 0, "a5", None) == "a name the picture must not show: Oren"
    assert il._say("name in the text: Gabriel", 0, "a5", None) == "a name the book has not used by this point"   # met at a8
    assert il._say("name in the text: Gabriel", 0, "a9", None) == "a name the picture must not show: Gabriel"
    assert il._say("name in the text: GABRIEL", 0, "a9", None) == "a name the book has not used by this point"   # case-sensitive
    assert il._say("unknown name: Valdemar", 0, "a11", None) == "a name the book has not used by this point"
    assert art.explain("wording: has just") == "wording that looks back on the story or ahead: has just"


# Morning Star: Cassius au Bellona's portrait failed with "name in prompt" on every retry. A name with a lowercase particle, a
# family name that is also a group's ("House Bellona"), and profiles that use it as a modifier ("the Bellona heir").
CH_BELLONA = ["Cassius au Bellona rode to the mill at dawn in a long golden cloak and waited.",
              "Men of House Bellona followed him with their banners high over the dusty road.",
              "Mira saw the Bellona heir dismount and walk to the gate in his golden armour.",
              "Oren said a Bellona knight had never asked for flour at his mill before then.",
              "Cassius au Bellona laughed and paid Oren in gold for every sack in the cart.",
              "The Bellonas left before noon and the dust of the road settled behind them all."]


def bellona_run(scene):
    bk = book(("One", CH1), ("Two", CH_BELLONA))
    d = base_doc(bk)
    d["entities"] += [ent("cassius", "Cassius au Bellona", frm="a6", facets=[
                          portrait("cassius", "a8", "The Bellona heir, a Bellona knight in a long golden cloak and golden armour."),
                          {"id": "bk.cassius.f1", "kind": "role", "from": "a9", "basis": "stated", "evidence": ["a9"],
                           "text": "A Bellona knight who pays in gold."}]),
                      ent("house", "House Bellona", "group", frm="a7")]
    d["events"] += [event(7, 6, "Cassius au Bellona rides to the mill.", ["cassius"]),
                    event(8, 10, "Cassius au Bellona pays Oren in gold.", ["cassius", "oren"])]
    t = Text(scene=lambda p: scene if is_portrait(p) else SCENE)
    il = art.Illustrator([art.Book(d, bk, Mem(), "main")], t, Image(), "ink", log=lambda *_: None, pause=0)
    return il, il.make_prompt(il.job("portrait", "cassius", at="a11"))


@pytest.mark.parametrize("opening, kept", [
    ("Cassius au Bellona stands, ", None), ("CASSIUS AU BELLONA stands, ", None),
    ("The Bellona heir stands, ", "The Bellona heir"), ("A Bellona knight stands, ", "A Bellona knight"),
    ("The Bellonas' heir of House Bellona stands, ", "of House Bellona"),
    ("Cassius au Bellona’s half-Bellona sneer, ", "half-Bellona")])
def test_a_persons_full_name_is_removed_and_the_house_it_shares_a_word_with_stays(opening, kept):
    # Red Rising: "a Gold of House Bellona" says who Cassius is, while "Cassius au Bellona" names him; Fitchner's alias "Mars"
    # is also House Mars and the planet
    il, pr = bellona_run(opening + PORTRAIT_SCENE[2:])
    assert not re.search(r"(?i)cassius", pr.text), pr.text
    assert not il.names[0].find(pr.text)
    if kept:
        assert kept in pr.text


def test_a_word_a_person_shares_with_a_place_or_group_is_no_name_alone():
    names = art._Names([("Fitchner", False), ("Mars", False), ("Cassius au Bellona", False)], ["Mars", "House Mars", "House Bellona"])
    assert names.find("The red plains of Mars.") == "" and names.find("A knight of House Bellona.") == ""
    assert names.find("Cassius au Bellona waits.") == "Cassius au Bellona" and names.find("Fitchner grins.") == "Fitchner"


# Review round 1: find and strip share one boundary and possessive rule, so whatever find reports strip takes out, in every
# combination: capitals with 'S, quotes around a name, both apostrophes, non-ASCII letters, particles, hyphens.
SHAPE_NAMES = ["Mira", "Jonathan Harker", "Jean-François", "Gabriel de León", "Patrick O'Brien", "Cassius au Bellona", "Millers"]
SHAPES = ["{n} waits.", "He sees {n} by the gate.", "The coat of {n}'s, {n}’s and {n}'S.", "A banner reads {N}.",
          "A banner reads {N}'S coat and {N}’S hat.", "He calls '{n}' and \"{n}\" and ‘{n}’.",
          "A '{N}' sign.", "A half-{n} grin and {n}-like shadows.", "The {n}s' cart.", "The {N}S' cart.",
          "Mr. {n} and Professor {n}’s owl.", "Behind them, {n}'s.", "\"{n},\" he says.",
          "He said '{n}'s'.", "A sign reads '{N}'S'.", "He said ‘{n}’s’.", "A sign reads ‘{N}’S’.", "'{n}' waits."]


def planted_gone(names, name, t):
    """`find` reports the planted name (or its plural), and nothing of it is left after `strip`."""
    found = names.find(t).replace("’", "'").lower()
    assert found and found.rstrip("s") in name.replace("’", "'").lower(), (t, found)
    out = names.strip(t)
    assert names.find(out) == "", (t, out)
    for w in re.findall(r"[^\W\d_][\w'’]{2,}", name):
        if w[0].isupper():
            assert not re.search(re.escape(w), out, re.IGNORECASE), (t, out)
    return out


def shape_names():
    return names_for([ent("harker", "Jonathan Harker", frm="a2"), ent("jf", "Jean-François", frm="a2"),
                      ent("leon", "Gabriel de León", frm="a2"), ent("ob", "Patrick O'Brien", frm="a2"),
                      ent("cassius", "Cassius au Bellona", frm="a2"), ent("millers", "the Millers", "group", frm="a2")])


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("name", SHAPE_NAMES)
def test_find_after_strip_is_empty_for_every_shape(name, shape):
    planted_gone(shape_names(), name, shape.format(n=name, N=name.upper()))


ORDINARY_SHAPES = ["Someone whispers '{n}' and turns.", "He calls ‘{n}’ twice.", "She sees the {n} by the gate.",
                   "A banner of the {N} hangs.", "He fears the {n}'s hand.", "\"The {n},\" he says.", "'The {n}' waits."]


@pytest.mark.parametrize("shape", ORDINARY_SHAPES)
@pytest.mark.parametrize("name", ["Stranger", "Dead"])
def test_an_ordinary_word_name_away_from_a_sentence_start_is_found_and_stripped(name, shape):
    names = names_for([ent("dead", "the Dead", "group", frm="a2")])
    planted_gone(names, name, shape.format(n=name, N=name.upper()))


def test_an_ordinary_word_name_opening_a_sentence_is_no_name():
    names = names_for([ent("dead", "the Dead", "group", frm="a2")])
    for t in ("Stranger things wait.", "It ends. Dead leaves fall.", "Why? Stranger still, it rains."):
        assert names.find(t) == "" and names.strip(t) == t


def test_a_capitalised_possessive_and_curly_apostrophes_are_found():
    names = shape_names()
    assert names.find("A MIRA'S banner.") == "MIRA" and names.find("JONATHAN HARKER'S coat") == "JONATHAN HARKER"
    assert names.find("Patrick O’Brien waits.") == "Patrick O’Brien" and names.find("The O’BRIEN’S cart.") == "O’BRIEN"
    assert names.strip("'Cassius au Bellona' rides.") == "'The figure' rides."
    assert names.strip("JONATHAN HARKER'S coat") == "The figure's coat"


def series_say():
    """A two-book series: "Harker" is used only by the first book's index; the second book uses "Gabriel" from a8 on, and
    has a Gabriel de León and a Patrick O'Brien it never names."""
    bk0, bk1 = book(("One", CH1)), book(("One", CH1), ("Two", CH2))
    d0 = doc(bk0, "b0", entities=[ent("harker", "Jonathan Harker")], events=[], recaps=[])
    d0["work"]["title"] = "Harker's Mill"
    d1 = base_doc(bk1)
    d1["series"] = {"id": "mill", "book": "b1"}
    d1["entities"] += [ent("leon", "Gabriel de León", frm="a8"), ent("ob", "Patrick O'Brien", frm="a8")]
    return art.Illustrator([art.Book(d0, None, Mem(), None, draw=False), art.Book(d1, bk1, Mem(), "main")], Text(), Image(), "ink",
                           log=lambda *_: None, pause=0)


def test_the_reason_shows_a_name_only_when_the_whole_name_was_reached():
    il = series_say()
    hidden = "a name the book has not used by this point"
    assert il._say("name in the text: Gabriel", 1, "a9", frozenset()) == "a name the picture must not show: Gabriel"
    assert il._say("name in the text: Gabriel de León", 1, "a11", frozenset()) == hidden     # only "Gabriel" was met
    assert il._say("name in the text: Patrick O'Brien", 1, "a11", frozenset()) == hidden
    assert il._say("name in the text: Harker", 1, "a11", frozenset()) == hidden             # the earlier book is unfinished
    assert il._say("name in the text: Harker", 1, "a11", frozenset({"b0"})) == "a name the picture must not show: Harker"
    assert il._say("name in the text: Harker", 1, "a11", None) == "a name the picture must not show: Harker"


def test_a_custom_style_naming_someone_is_stripped_in_the_assembled_prompt():
    bk = book(("One", CH1), ("Two", CH2))
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem(), "main")], Text(), Image(), "drawn as Oren would draw it",
                         log=lambda *_: None, pause=0)
    pr = il.make_prompt(il.job("portrait", "mira", at="a11"))
    assert "Oren" not in pr.text and not il.names[0].find(pr.text) and "the figure would draw it" in pr.text


def test_a_softened_redraw_runs_the_same_last_name_check(monkeypatch):
    # the quieter rewrite after a refusal is assembled again; a name still in it then (here forced in) is never drawn
    quiet = ("A door stands ajar in a stone mill at dusk and a long shadow lies across the worn floor. A lamp burns low on a wooden table "
             "beside a woven basket, and the last grey light comes through one small window. The room is quiet and empty and the air is still.")
    real = art.Illustrator._assemble
    monkeypatch.setattr(art.Illustrator, "_assemble", staticmethod(lambda body, bi=-1, style="":
                        real(body, bi, style) + (" Oren" if body.startswith(quiet[:20]) else "")))
    t = Text(scene=lambda p: quiet if "quieter composition" in p else SCENE)
    im = Image(refuse=[lambda prompt, n: prompt.startswith(SCENE[:30])])
    il, rep, sink, t, im = run(text_=t, image=im, portraits=False)
    assert not any("Oren" in c["prompt"] and c["prompt"].startswith(quiet[:20]) for c in im.calls)
    assert rep.skipped["dropped: name in prompt"] == 2 and not pics(sink, "plate")


def test_the_reason_never_shows_a_name_known_only_as_a_lowercase_word():
    lower = ["They said the rain may come and the wind will rise before the evening fell.",
             "Mira hoped the dawn would bring the miller home before the market day began."]
    bk = book(("One", CH1), ("Two", CH2), ("Three", lower))
    d = base_doc(bk)
    d["entities"] += [ent("dawn", "Dawn", frm="a12"), ent("will", "Will", frm="a12"), ent("may", "May", frm="a12")]
    il = art.Illustrator([art.Book(d, bk, Mem(), "main")], Text(), Image(), "ink", log=lambda *_: None, pause=0)
    hidden = "a name the book has not used by this point"
    for w in ("Dawn", "Will", "May"):           # "at dawn", "will rise", "may come": the book never wrote the name
        assert il._say(f"name in the text: {w}", 0, "a13", None) == hidden
    assert il._say("name in the text: Mira", 0, "a13", None) == "a name the picture must not show: Mira"


# ---- whose portraits: the main cast by default


def cast_doc(counts, turns=None, froms=None, profiled=None, minor=0):
    """A book whose people are named by `counts[i]` non-minor events (and `turns[i]` of them turning points), each first
    appearing at block `froms[i]`; `minor` events naming everyone; `profiled[i]` False leaves person i without a profile."""
    bk = book(("One", CH1), ("Two", CH2))
    n = len(counts)
    turns, froms, profiled = turns or [0] * n, froms or list(range(n)), profiled or [True] * n
    ents = [ent(f"p{i}", f"Person{i}", frm=f"a{froms[i] % 12}",
                facets=[portrait(f"p{i}", "a11", "A tall woman with dark hair and a grey coat.")] if profiled[i] else [])
            for i in range(n)]
    evs = []
    for i, c in enumerate(counts):
        for k in range(c):
            evs.append(event(len(evs), 11, f"Person{i} does something that changes the story.", [f"p{i}"],
                             "turn" if k < turns[i] else None))
    for _ in range(minor):
        evs.append(event(len(evs), 11, "A small moment.", [e["id"] for e in ents], "minor"))
    return bk, doc(bk, entities=ents, events=evs, recaps=[recap(1, 5, "One")])


def plan_of(bk, d, portraits="main"):
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", portraits=portraits, workers=1, log=lambda *_: None, pause=0)
    return il, [(j.target, j.anchor) for j in il._portrait_jobs(0)]


def test_main_cast_keeps_the_most_named_and_drops_a_character_named_by_two_events():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)       # Mira is named by 3 non-minor events, Oren and the Stranger by 2, Gabriel by 1; all have portraits
    il, main = plan_of(bk, d)
    assert il.portraits == "main" and il.main_cast(0) == ["mira"] and {t for t, _ in main} == {"mira"}
    assert {t for t, _ in plan_of(bk, d, "all")[1]} == {"mira", "oren", "stranger", "gabriel"}
    assert il.counts()["portraits"] == 1


def test_main_cast_needs_a_share_of_the_books_events_that_grows_with_the_book():
    # 100 non-minor events: at least ceil(5% of 100) = 5 are needed, so 4 is not enough though it passes MAIN_CAST_MIN
    bk, d = cast_doc([5, 4] + [1] * 91)
    assert plan_of(bk, d)[0].main_cast(0) == ["p0"]
    bk, d = cast_doc([5, 4, 3], minor=100)      # minor events never count: 12 non-minor events, 3 is enough
    assert plan_of(bk, d)[0].main_cast(0) == ["p0", "p1", "p2"]


def test_main_cast_is_at_most_twelve_by_events_then_turning_points_then_first_appearance():
    counts = [3] * 11 + [4, 4, 5]
    turns = [0] * 11 + [0, 1, 0]
    froms = list(range(11, 0, -1)) + [0, 0, 0]     # p10 appears first of the threes, p0 last
    bk, d = cast_doc(counts, turns, froms)
    cast = plan_of(bk, d)[0].main_cast(0)
    assert len(cast) == art.MAIN_CAST_MAX == 12
    assert cast[:3] == ["p13", "p12", "p11"]
    assert cast[3:] == [f"p{i}" for i in range(10, 1, -1)]      # p1 and p0, the last to appear, are left out


def test_main_cast_needs_a_profile_and_only_people_and_creatures():
    bk, d = cast_doc([6, 6, 6], profiled=[True, False, True])
    d["entities"][2]["type"] = "place"
    assert plan_of(bk, d)[0].main_cast(0) == ["p0"]


def test_all_is_the_older_rule_and_true_means_all():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"].append(ent("ada", "Ada", frm="a2", facets=[portrait("ada", "a2", "A small girl with red hair.", prefix="x")]))
    d["entities"][-1]["facets"][0]["id"] = "bk.ada.f1"     # a plain profile, named by no event: never planned
    il, every = plan_of(bk, d, "all")
    assert plan_of(bk, d, True)[0].portraits == "all" and plan_of(bk, d, True)[1] == every
    assert [t for t, _ in every] == ["mira", "oren", "stranger", "gabriel"]
    assert plan_of(bk, d, None)[0].counts()["portraits"] == 0 and plan_of(bk, d, False)[0].portraits is None
    with pytest.raises(ValueError):
        art.portrait_cast("some")


def test_a_character_the_main_cast_leaves_out_can_still_be_drawn_on_demand():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    assert "gabriel" not in il.main_cast(0)
    job = il.job("portrait", "gabriel", at="a11")
    pr = il.make_prompt(job)
    rec = il.draw(job, pr)
    assert rec and rec["kind"] == "portrait" and rec["target"] == "gabriel"


def test_the_illustrate_command_takes_main_all_or_none(tmp_path, monkeypatch):
    from lorekeeper import cli
    bk = book(("One", CH1), ("Two", CH2))
    i1 = _cli_index(tmp_path, base_doc(bk), bk, "b.lore.json")
    monkeypatch.setattr(cli, "_make_model", lambda args, model=None, provider=None: Text())
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: Image())
    monkeypatch.setattr(epub, "read", lambda p: bk)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    seen = []
    real = art.Illustrator

    def spy(*a, **k):
        il = real(*a, **k)
        seen.append(il.portraits)
        return il
    monkeypatch.setattr(art, "Illustrator", spy)
    for flags in ([], ["--portraits", "all"], ["--portraits", "none"], ["--no-portraits"]):
        assert cli.main(["illustrate", str(i1), "--epub", "1.epub", "--out", str(tmp_path / "o"), "--limit", "0", *flags]) == 0
    assert seen == ["main", "all", None, None]

# ---- portrait fidelity: the look index, groups, milieu, era note and fuller notes


def test_the_look_index_is_cached_by_the_items_and_a_rebuilt_entity_is_classified_again(monkeypatch):
    monkeypatch.setattr(art, "LOOK_BATCH_ITEMS", 2)
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il, rep, sink, t, im = run(d, bk)
    calls = [p for p in t.prompts if "notes a reader's companion keeps on some entries" in p]
    assert len(calls) >= 3 and all(len(re.findall(r"^\d+\. ", c, re.M)) <= 2 for c in calls)   # small batches
    assert set(sink.m["looks"]) == {"mira", "oren", "stranger", "gabriel"}
    d2 = base_doc(bk)
    d2["entities"][0]["facets"][0]["text"] += " She hums."
    il, rep, sink, t2, im = run(d2, bk, sink)
    again = [p for p in t2.prompts if "notes a reader's companion keeps on some entries" in p]
    assert len(again) == 1 and "She hums" in again[0] and "## Oren" not in again[0]
    assert il.counts()["portraits"] == 4 and il._portrait_jobs(0, classify=False)



# ---- one writer call per picture, from the whole entry (KOAssistant's shape)


def writer_calls(t):
    return [p for p, s in zip(t.prompts, t.systems) if s == art.WRITER]


def fresh(d=None, bk=None, text_=None, style="ink", books=None, **kw):
    bk = bk or book(("One", CH1), ("Two", CH2))
    d = d or base_doc(bk)
    t = text_ or Text()
    il = art.Illustrator(books or [art.Book(d, bk, Mem())], t, Image(), style, portraits="all", workers=1, log=lambda *_: None,
                         pause=0, **kw)
    return il, t


def test_an_on_demand_prompt_is_one_writer_call_and_nothing_else_on_a_fresh_set():
    for kind, target in (("portrait", "mira"), ("event", "e5"), ("plate", "Two"), ("place", "mill")):
        il, t = fresh()
        pr = il.make_prompt(il.job(kind, target, at="a11"))
        assert len(t.prompts) == 1 and t.systems == [art.WRITER] and pr.body, kind
        assert il.man[0]["screened"] == {} and il.man[0]["looks"] == {}      # no screening, no look index on this path


def test_the_prompt_is_the_writers_text_then_the_style_sentence_then_no_text():
    il, t = fresh(style="anime")
    pr = il.make_prompt(il.job("portrait", "mira", at="a11"))
    assert (pr.body, pr.style, pr.no_text) == (PORTRAIT_SCENE, art.STYLES["anime"], art.NO_TEXT)
    assert pr.text == f"{PORTRAIT_SCENE} {art.STYLES['anime']} {art.NO_TEXT}" and pr.scene == pr.body
    assert il.style_sentence() == art.STYLES["anime"] and il.style_sentence("ink") == art.STYLES["ink"]
    assert il.style_sentence("  Chalk   on slate. ") == "Chalk on slate."
    writer = writer_calls(t)[0]
    assert art.STYLES["anime"] not in writer and "anime" not in writer.lower()       # the writer never sees the style
    for word in ("Figures:", "Their people:", "Place:", "World:"):
        assert word not in pr.text
    # nor the shape: the same text for every shape, so a shape change asks for nothing
    il2, t2 = fresh(style="ink")
    pr2 = il2.make_prompt(il2.job("portrait", "mira", at="a11", shape="square"))
    assert writer_calls(t2)[0] == writer and pr2.body == pr.body and pr2.shape == "square"


def guild_doc():
    """The mill with a guild Oren is tied to, a group his own detail names, settings on his events, and an author."""
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["work"]["authors"] = ["A. Writer"]
    d["entities"][1]["facets"].append({"id": "bk.oren.f1", "from": "a3", "basis": "stated", "evidence": ["a3"],
                                       "text": "Grinds for the Millbrook Folk, as his father did."})
    d["entities"] += [ent("guild", "Guild", "group", frm="a2", facets=[portrait(
                          "guild", "a2", "The Guild's members wear white aprons dusted with flour.")]),
                      ent("folk", "Millbrook Folk", "group", frm="a2", facets=[portrait(
                          "folk", "a2", "The valley people, farmers in plain wool.")]),
                      ent("crows", "the Crows", "group", frm="a2", facets=[portrait("crows", "a2", "A band of thieves.")])]
    d["relations"] = [{"id": "rel1", "src": "oren", "dst": "guild", "label": "member of", "kind": "alliance", "from": "a3",
                       "basis": "stated", "evidence": ["a3"]}]
    d["events"][1]["setting"] = {"text": "the mill yard"}
    d["events"][3]["setting"] = {"text": "the mill", "place": "mill"}
    return bk, d


def test_the_writer_reads_the_whole_entry_its_passages_its_groups_its_settings_and_the_book():
    bk, d = guild_doc()
    il, t = fresh(d, bk)
    pr = il.make_prompt(il.job("portrait", "oren", at="a11"))
    [w] = writer_calls(t)
    assert w.startswith(art.TASKS["portrait"])
    entry = w.split("<entry_to_draw", 1)[1].split("</entry_to_draw>")[0]
    assert entry.startswith(' name="Oren" type="person">The old miller, broad, with white hair') and "Millbrook Folk, as his father" in entry
    assert '<entry name="Guild" type="group">The Guild\'s members wear white aprons' in w          # tied to it
    assert '<entry name="Millbrook Folk" type="group">' in w                                    # his own detail names it
    assert 'name="the Crows"' not in w                                                          # neither: not his
    assert "<surroundings>the mill yard</surroundings>" in w and pr.anchor == "a3"           # his events up to his anchor
    assert '<book title="Millbrook" author="A. Writer"/>' in w and "<background>" not in w
    assert "<passage>" in w and "The miller Oren came out of the mill door" in w
    assert pr.milieu and pr.world == "" and pr.shows == ["oren"]
    assert {"a1", "a3"} <= set(pr.sources)                                                   # the setting's and facets' anchors
    il.make_prompt(il.job("event", "e5", at="a11"))
    w = writer_calls(t)[-1]
    assert "<surroundings>" not in w
    pr = il.make_prompt(il.job("portrait", "stranger", at="a11"))
    assert "<surroundings>the mill</surroundings>" in writer_calls(t)[-1] and "a6" in pr.sources


def test_a_scenes_writer_reads_the_moment_the_chapter_so_far_and_the_figures_entries_with_their_groups():
    bk, d = guild_doc()
    il, t = fresh(d, bk)
    pr = il.make_prompt(il.job("event", "e4", at="a11"))
    [w] = writer_calls(t)
    assert '<moment chapter="Two">A hooded stranger comes to the mill asking for a bed. (where: the mill)</moment>' in w
    assert '<entry name="the Stranger" type="person">' in w and '<entry name="Oren" type="person">' in w
    assert '<entry name="Guild" type="group">' in w and w.index('name="Oren"') < w.index('name="Guild"')   # figures first
    assert w.count("<entry ") <= art.MAX_ENTRIES and w.rstrip().endswith(art.SHOWS)
    assert "<surroundings>" not in w and pr.shows == ["stranger", "oren"]


def test_the_material_is_never_past_the_anchor():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append({"id": "bk.mira.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Wears a red ribbon in her dark braided hair at the market."})
    il, t = fresh(d, bk)
    pr = il.make_prompt(il.job("portrait", "mira", at="a5"))
    [w] = writer_calls(t)
    assert "red ribbon" not in w and not any(p in w for p in CH2) and "Stranger" not in w and "Gabriel" not in w
    o = ordinal(d)
    assert all(o[a] <= o[pr.anchor] for a in pr.sources) and o[pr.anchor] <= o["a5"]
    il, t = fresh(d, bk)
    il.make_prompt(il.job("event", "e2", at="a11"))
    [w] = writer_calls(t)
    assert not any(p in w for p in CH2) and "red ribbon" not in w
    il, t = fresh(d, bk)
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    assert "red ribbon" in writer_calls(t)[0]                                   # read once it is reached


def test_group_names_are_allowed_in_a_prompt_and_person_names_are_not():
    bk = book(("One", CH1), ("Two", CH2[:-1] + ["Mira kept the coin and told the Order of the Dawn nothing at the market."]))
    d = base_doc(bk)
    d["entities"].append(ent("order", "The Order of the Dawn", "group"))
    named = SCENE + " A banner of the Order of the Dawn hangs there."
    il, rep, sink, t, im = run(d, bk, text_=Text(scene=lambda p: named), portraits=False)
    # chapter Two's plate names the group the book has named by then; chapter One's comes before the book uses the name
    assert [p["target"] for p in pics(sink, "plate")] == ["Two"] and "Order of the Dawn" in pics(sink)[0]["prompt"]
    assert il.names[0].find(named) == "" and rep.skipped["dropped: unknown name in prompt"] == 1
    assert il.names[0].find("They meet Mira's brother.") == "Mira" and il.names[0].find("They watch the Stranger.") == "Stranger"


def test_a_name_gets_one_rewrite_naming_it_and_then_code_replaces_it():
    answers = iter([PORTRAIT_SCENE.replace("the figure", "Mira"), PORTRAIT_SCENE.replace("the figure", "Mira")])
    il, t = fresh(text_=Text(scene=lambda p: next(answers)))
    pr = il.make_prompt(il.job("portrait", "mira", at="a11"))
    ws = writer_calls(t)
    assert len(ws) == 2 and "It was rejected: name in the text: Mira." in ws[1]
    assert "Mira" not in pr.text and pr.body.startswith("A head-and-shoulders portrait, The figure turned") is False
    assert "Mira" not in pr.body and il.report.dropped["fixed: name replaced by a role"] == 1


def test_the_background_is_the_persons_own_note_only_and_an_automatic_one_is_set_aside():
    sink = Mem()
    sink.m = {**art.blank("sha256:tbk", "ink"), "world": {"text": "A valley of mills. The Stranger roams it.", "from": "a5", "era": True,
                                                         "edited": False}}
    il, t = fresh(books=[art.Book(base_doc(book(("One", CH1), ("Two", CH2))), book(("One", CH1), ("Two", CH2)), sink)])
    assert il.world(0) is None and il.man[0]["world"] is None and il.man[0]["worldOld"]["world"]["text"].startswith("A valley")
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    assert "valley of mills" not in writer_calls(t)[0] and "<background>" not in writer_calls(t)[0]
    sink2 = Mem()
    sink2.m = {**art.blank("sha256:tbk", "ink"), "world": {"text": "Grim, rain-soaked northern country.", "edited": True, "from": "a0"}}
    il, t = fresh(books=[art.Book(base_doc(book(("One", CH1), ("Two", CH2))), book(("One", CH1), ("Two", CH2)), sink2)])
    pr = il.make_prompt(il.job("portrait", "mira", at="a11"))
    assert "<background>Grim, rain-soaked northern country.</background>" in writer_calls(t)[0]
    assert pr.world == "Grim, rain-soaked northern country." and il.world(0)["edited"] and "rain-soaked" not in pr.text


def test_an_older_set_still_loads_and_draws_and_its_notes_are_never_read():
    old_pic = {"id": "pold0001", "kind": "portrait", "target": "mira", "chapter": "One", "from": "a4", "file": "pold0001.png",
               "mime": "image/png", "prompt": "old", "references": [], "sources": ["a4"], "inherits": [], "cost": 0.05, "notes": 2,
               "appearance": "A young woman: slight."}
    sink = Mem()
    sink.files["pold0001.png"] = png(10, 10, b"old")
    sink.m = {"lorekeeperArt": "0.1", "textHash": "sha256:tbk", "style": "ink", "pictures": [old_pic], "cost": 0.05,
              "world": {"text": "An old era note about mines.", "from": "a5"}, "screened": {}, "looks": {}, "skipped": [],
              "appearance": {"mira": [{"v": 2, "from": "a4", "text": "A miner in grey: dusty boots.", "details": [], "sources": [],
                                       "inherits": []}]},
              "groups": {"mira": {"key": "x", "picks": []}}, "intro": {"mira": {"key": "1-2", "paragraphs": []}}}
    il, rep, sink, t, im = run(sink=sink, level="all")
    assert rep.drawn and [p["id"] for p in pics(sink, "portrait") if p["target"] == "mira"] == ["pold0001"]   # not drawn again
    assert not any("dusty boots" in w or "mines" in w for w in t.scenes())
    assert all("pold0001" not in p["references"] for p in pics(sink))         # a picture of the older version is no reference
    assert all(p["notes"] == art.NOTE_VERSION and "appearance" not in p for p in pics(sink) if p["id"] != "pold0001")
    assert sink.m["appearance"]["mira"] and sink.m["world"] is None and sink.m["worldOld"]["world"]["text"].startswith("An old")


def test_a_look_change_draws_a_new_portrait_with_the_old_one_as_reference():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append(portrait("mira", "a10", "A scarred young woman, older than she was, who keeps a stranger's coin.", 2))
    il, rep, sink, t, im = run(d, bk)
    ms = sorted((p for p in pics(sink, "portrait") if p["target"] == "mira"), key=lambda p: ordinal(d)[p["from"]])
    assert [p["from"] for p in ms] == ["a4", "a10"] and ms[1]["references"] == [ms[0]["id"]] and not ms[0]["references"]
    second = next(w for w in t.scenes() if drawing(w) == "Mira" and "A scarred young woman" in w)
    assert second.split("<entry_to_draw")[1].split(">", 1)[1].startswith("A scarred young woman")   # the latest profile


def test_a_later_book_reuses_pictures_and_reads_an_earlier_book_only_when_the_reader_finished_it():
    bk1, d1, bk2, d2 = series_docs()
    d1["entities"][0]["facets"].append({"id": "b1.mira.f9", "from": "a6", "basis": "stated", "evidence": ["a6"],
                                        "text": "Wears a red ribbon in her dark braided hair."})
    s1, s2 = Mem(), Mem()
    t, im = Text(), Image()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], t, im, "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    il.run()
    one = {p["target"]: p for p in pics(s1, "portrait")}
    assert [p["target"] for p in pics(s2, "portrait")] == ["mira"]           # Oren is not drawn again; Mira's look changed
    plate = pics(s2, "plate")[0]
    assert one["oren"]["id"] in plate["references"] and plate["inherits"] == ["b1"]
    assert all(p["inherits"] == [] for p in pics(s1))
    # a reader who finished book one: its entries reach the material, and the prompt says so
    t.prompts.clear(), t.systems.clear()
    pr = il.make_prompt(il.job("portrait", "mira", at="b2.a5", finished={"b1"}))
    assert "red ribbon" in writer_calls(t)[0] and pr.inherits == ["b1"] and not pr.partial
    # one who did not: nothing of book one, no picture of it as a reference, and the prompt is marked partial
    with pytest.raises(art.NotVisible):
        il.job("portrait", "stranger", at="b2.a5", finished=set())          # only book one has the stranger
    t.prompts.clear(), t.systems.clear()
    job = il.job("plate", "Three", at="b2.a5", finished=set())
    pr = il.make_prompt(job)
    w = writer_calls(t)[0]
    assert "red ribbon" not in w and "slight young woman of about twenty" not in w
    assert pr.partial and pr.inherits == [] and all(j == 1 for j, _ in pr.references)
    rec = il.draw(job, pr)
    assert rec["partial"] and rec["inherits"] == [] and (rec["kind"], rec["target"], rec["from"]) not in {
        (r["kind"], r["target"], r["from"]) for r in s2.m["pictures"] if not r.get("partial")} - {(plate["kind"], plate["target"], plate["from"])}
    assert il._have(1) == {(r["kind"], r["target"], r["from"]) for r in s2.m["pictures"] if not r.get("partial")}


def test_an_earlier_books_own_background_note_is_read_only_by_a_reader_who_finished_it():
    bk1, d1, bk2, d2 = series_docs()
    s1, s2 = Mem(), Mem()
    s1.m = {**art.blank("sha256:tbk", "ink"), "world": {"text": "Grim northern country.", "edited": True, "from": "a0"}}
    t = Text()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], t, Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    pr = il.make_prompt(il.job("plate", "Three", at="b2.a5", finished={"b1"}))
    assert "<background>Grim northern country.</background>" in writer_calls(t)[-1] and "b1" in pr.inherits
    pr = il.make_prompt(il.job("plate", "Three", at="b2.a5", finished=set()))
    assert "<background>" not in writer_calls(t)[-1] and pr.inherits == [] and il.world(1, set()) is None


def test_shows_picks_the_figures_and_their_references():
    il, rep, sink, t, im = run(text_=Text(shows=["Oren"]), level="all")
    one = next(p for p in pics(sink) if p["target"] == "One")                  # the plate of e2, naming oren and mira
    by = {p["target"]: p for p in pics(sink, "portrait")}
    assert one["references"] == [by["oren"]["id"]]
    il2, t2 = fresh(text_=Text(shows=["Oren"]))
    assert il2.make_prompt(il2.job("event", "e2", at="a11")).shows == ["oren"]
    assert il2.make_prompt(il2.job("portrait", "mira", at="a11")).shows == ["mira"]
    il3, _ = fresh(text_=Text(shows=["Nobody Known"]))                       # a list that matches no one keeps them all
    assert il3.make_prompt(il3.job("event", "e2", at="a11")).shows == ["oren", "mira"]
    il4, _ = fresh(text_=Text(shows=[]))                                     # an empty list: no figure, no reference
    pr = il4.make_prompt(il4.job("event", "e2", at="a11"))
    assert pr.shows == [] and pr.references == []


def test_an_on_demand_plate_takes_the_turning_point_with_no_screening_call():
    il, t = fresh()
    j = il.job("plate", "One")
    assert j.event and j.event["id"] == "e2" and not t.prompts
    il.drawable(0, il.books[0].chapters[0])                                    # a fresh screening is used when there is one
    t2 = Text(screen=lambda ns, p: [1])
    il2, _ = fresh(text_=t2)
    il2.drawable(0, il2.books[0].chapters[0])
    assert il2.job("plate", "One").event["id"] == "e1"


def test_with_a_picture_limit_only_the_pictures_drawn_are_written_and_nothing_is_screened():
    il, rep, sink, t, im = run(max_pictures=1)
    assert rep.drawn == 1 and len(t.scenes()) == 1 and not any("Which of them are a visible moment" in p for p in t.prompts)


def test_text_spent_counts_against_the_cap():
    class Usage:
        cost_usd = 0.0

    class Costly(Text):
        usage = Usage()

        def complete(self, system, prompt, schema, prefix=None):
            self.usage.cost_usd += 0.3
            return super().complete(system, prompt, schema, prefix)
    t = Costly()
    il, rep, sink, _, im = run(text_=t, cap=1.0)
    assert "cost cap" in rep.stopped and len(t.scenes()) <= 1 and not im.calls


def test_details_a_fresh_look_index_flags_go_first_in_the_entry_and_none_is_classified_on_the_way():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    mira = d["entities"][0]
    mira["facets"] += [{"id": "bk.mira.f1", "from": "a2", "basis": "stated", "evidence": ["a2"], "text": "Has a freckled face."},
                       {"id": "bk.mira.f2", "from": "a5", "basis": "stated", "evidence": ["a5"], "text": "Sells eggs at the market."},
                       {"id": "bk.mira.f3", "from": "a6", "basis": "stated", "evidence": ["a6"], "text": "Keeps the mill's books."}]
    entry = lambda w: w.split("<entry_to_draw")[1].split("</entry_to_draw>")[0]   # noqa: E731
    il, t = fresh(d, bk)                       # no look index in the set: the latest details first, and no call to make one
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    e = entry(writer_calls(t)[0])
    assert e.index("Keeps the mill's books") < e.index("Sells eggs") < e.index("freckled face") and t.systems == [art.WRITER]
    il, t = fresh(d, bk, text_=Text(look=lambda txt: "freckled" in txt))
    il.classify(0, ["mira"])                   # a bulk run's look index, cached in the set
    t.prompts.clear(), t.systems.clear()
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    e = entry(writer_calls(t)[0])
    assert e.index("freckled face") < e.index("Keeps the mill's books") < e.index("Sells eggs") and t.systems == [art.WRITER]
    il.make_prompt(il.job("portrait", "mira", at="a1"))        # a flagged detail after the anchor is still not there
    assert "freckled" not in entry(writer_calls(t)[-1])
    d["entities"][0]["facets"][-1]["text"] += " She hums."      # a rebuilt entity: the cache is stale, today's order again
    il2 = art.Illustrator([art.Book(d, bk, il.books[0].b.sink)], Text(), Image(), "ink", portraits="all", workers=1,
                          log=lambda *_: None, pause=0)
    il2.make_prompt(il2.job("portrait", "mira", at="a11"))
    e = entry(il2.text.scenes()[0])
    assert e.index("Keeps the mill's books") < e.index("freckled face")
