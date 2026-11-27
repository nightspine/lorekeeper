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


WRITERS = (art.WRITER, art.SCENE_WRITER, art.PORTRAIT_WRITER)


def unframed(prompt):
    """A portrait's prompt without the framing code puts first."""
    assert prompt.startswith(art.PORTRAIT_FRAME + " "), prompt[:80]
    return prompt[len(art.PORTRAIT_FRAME) + 1:]


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

    def __init__(self, scene=SCENE, screen=None, shows=None, look=None, change=None, picks=None, names=None):
        # `picks`: how it picks look sentences (a function of the sentence; default `look`), False: that call fails
        # `names`: whether a word the name check caught is used as a name (a function of word and sentence; default: every
        # one is, the old rule), False: that call fails
        self.scene, self.shows, self.screen, self.picks, self.names = scene, shows, screen, picks, names
        self.look = look or (lambda t: bool(FAKE_LOOK.search(t)))
        self.change = change or (lambda t: (m.group(0) if (m := FAKE_CHANGE.search(t)) else ""))
        self.prompts, self.systems = [], []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
        self.systems.append(system)
        if system in WRITERS:
            scene = PORTRAIT_SCENE if is_portrait(prompt) and self.scene is SCENE else self.scene
            out = {"text": scene(prompt) if callable(scene) else scene}
            if system == art.SCENE_WRITER and self.scene is SCENE:
                # the default scene draws every labelled figure it says it drew (all of them with no `shows`), by its label
                for name, label in re.findall(r'<entry name="([^"]*)"[^>]*? figure="([A-F])"', prompt):
                    if self.shows is None or name in self.shows:
                        out["text"] += f" Figure {label} waits by the wall."
            if self.shows is not None and "shows" in schema.get("properties", {}):
                out["shows"] = self.shows
            return out
        if "Which of them are a visible moment" in prompt:
            ns = [int(n) for n in re.findall(r"^(\d+)\. ", prompt, re.M)]
            keep = self.screen(ns, prompt) if self.screen else ns
            return {"moments": keep}
        if "was checked for names" in prompt:
            if self.names is False:
                raise RuntimeError("the cheap model is down")
            lines = re.findall(r'^(\d+)\. (\w+): "(.*)"$', prompt.split("\n\n", 1)[1], re.M)
            return {"words": [{"n": int(n), "name": bool(self.names(w, s)) if self.names else True} for n, w, s in lines]}
        if "Which sentences tell how" in prompt:
            if self.picks is False:
                raise RuntimeError("the cheap model is down")
            lines = re.findall(r"^(\d+)\. (.*)$", prompt.split("\n\n", 1)[1], re.M)
            return {"sentences": [int(n) for n, t in lines if (self.picks or self.look)(t)]}
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
        return [p for p, s in zip(self.prompts, self.systems) if s in WRITERS]


def png(w, h, salt):
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR" + struct.pack(">II", w, h) + salt


class Image:
    provider, name = "", "fake-image"

    def __init__(self, cost=0.05, refuse=()):
        self.calls, self.cost, self.refuse = [], cost, list(refuse)

    def draw(self, prompt, *, shape="landscape", quality="standard", references=(), **kw):
        self.calls.append({"prompt": prompt, "shape": shape, "quality": quality, "references": [r[0] for r in references],
                           "note": kw.get("note", "")})
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

        def draw(self, prompt, *, shape="landscape", quality="standard", references=(), **kw):
            if shape == "portrait":
                return super().draw(prompt, shape=shape, quality=quality, references=references, **kw)
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

        def draw(self, prompt, *, shape="landscape", quality="standard", references=(), **kw):
            self.sends += 1
            if self.sends > 1:
                raise llm.ProviderError("the request failed after it was sent: timed out")
            return super().draw(prompt, shape=shape, quality=quality, references=references, **kw)
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
    assert unframed(m["prompt"]).startswith("A portrait of the figure, the figure's helper, head-and-shoulders portrait")
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
        def draw(self, prompt, *, shape="landscape", quality="standard", references=(), **kw):
            if shape == "portrait" and unframed(prompt).startswith(OREN_SCENE[:30]) and seen["n"] < 2:
                seen["n"] += 1
                self.calls.append({"prompt": prompt, "shape": shape, "references": []})
                raise images.ImageRefused("safety")
            return super().draw(prompt, shape=shape, quality=quality, references=references, **kw)
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
    row = {"drawable": None, "pictures": 1, "drawn": 0, "chosen": True}
    assert c == {"plates": 2, "portraits": 4, "new_portraits": 4, "portraits_max": 4 * art.MAX_VERSIONS, "chapters": [{"n": 1, "title": "One", "events": 2, **row},
                                                           {"n": 2, "title": "Two", "events": 3, **row}]}
    assert not t.prompts
    il.run()
    n = len(t.prompts)
    assert [x["drawable"] for x in il.counts()["chapters"]] == [2, 3] and len(t.prompts) == n


# ---- choosing chapters, and portraits only


SCREENING = "Which of them are a visible moment"


@pytest.mark.parametrize("level", ["none", None])
def test_level_none_draws_portraits_only_and_never_screens(level):
    il, rep, sink, t, im = run(level=level, portraits="main")
    assert pics(sink) and {p["kind"] for p in pics(sink)} == {"portrait"}
    assert not any(SCREENING in p for p in t.prompts) and not sink.m["screened"]
    assert art.scene_level("none") is None and "none" in art.LEVELS


def test_an_unknown_level_is_refused_before_any_call():
    bk = book(("One", CH1), ("Two", CH2))
    t, im = Text(), Image()
    with pytest.raises(ValueError, match="level must be"):
        art.Illustrator([art.Book(base_doc(bk), bk, Mem(), "every")], t, im, "ink")
    assert not t.prompts and not im.calls


def test_scene_chapters_limit_scenes_but_never_portraits():
    # Gabriel's only profile is in chapter Two: the CLI's --chapters 1 leaves him out, the web's chapter choice does not
    il, rep, sink, t, im = run(level="all", scene_chapters={1})
    assert {p["target"] for p in pics(sink, "plate")} == {"One"} and {p["target"] for p in pics(sink, "event")} == {"e1"}
    assert {p["target"] for p in pics(sink, "portrait")} == {"mira", "oren", "stranger", "gabriel"}
    assert set(sink.m["screened"]) == {"One"} and sum(SCREENING in p for p in t.prompts) == 1
    _, _, cli_sink, _, _ = run(level="all", chapters={1})
    assert "gabriel" not in {p["target"] for p in pics(cli_sink, "portrait")}


def test_an_empty_chapter_choice_draws_no_scenes():
    il, rep, sink, t, im = run(level="main", scene_chapters=[])
    assert pics(sink) and {p["kind"] for p in pics(sink)} == {"portrait"} and not any(SCREENING in p for p in t.prompts)


def test_counts_list_every_chapter_with_its_pictures_at_the_level_and_what_is_drawn():
    bk = book(("One", CH1), ("Two", CH2), ("Three", ["Rain fell on the mill all through the long grey afternoon and evening.",
                                                     "Mira mended a sack by the fire while Oren slept in his chair by the stove."]))
    d = base_doc(bk)
    d["recaps"].append(recap(3, 13, "Three"))                        # a chapter with a recap and no events
    d["coverage"]["through"] = d["anchors"][-1]["id"]
    sink = Mem()
    il = art.Illustrator([art.Book(d, bk, sink, None)], Text(), Image(), "ink", portraits=None, scene_chapters={2, 3},
                         workers=1, log=lambda *_: None, pause=0)
    rows = lambda c, f: [x[f] for x in c["chapters"]]
    c = il.counts(level="all")                                       # not screened yet: up to one per non-minor event
    assert rows(c, "n") == [1, 2, 3] and rows(c, "title") == ["One", "Two", "Three"] and rows(c, "events") == [2, 3, 0]
    assert rows(c, "pictures") == [2, 3, 0] and rows(c, "chosen") == [False, True, True] and c["plates"] == 3
    c = il.counts(level="main")
    assert rows(c, "pictures") == [1, 1, 0] and c["plates"] == 1 and c["portraits"] == 0
    c = il.counts(level="none")
    assert rows(c, "pictures") == [0, 0, 0] and rows(c, "chosen") == [False] * 3 and c["plates"] == 0
    il2 = art.Illustrator([art.Book(d, bk, sink, "all")], Text(), Image(), "ink", portraits=None, workers=1,
                          log=lambda *_: None, pause=0)
    il2.run()
    c = il2.counts(level="all")
    assert rows(c, "drawable") == [2, 3, 0] and rows(c, "pictures") == [2, 3, 0] and rows(c, "drawn") == [2, 3, 0]
    assert rows(il2.counts(level="main"), "drawn") == [1, 1, 0]


def test_an_event_picture_never_counts_as_the_plate_of_an_unscreened_chapter():
    # chapter One with one non-minor event (e2), an on-demand picture of it and no plate: a run would still draw the plate
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["events"][0]["kind"] = "minor"
    _, _, sink, _, _ = run(d, bk, level="main", portraits=None)
    plate = next(p for p in pics(sink, "plate") if p["target"] == "One")
    plate.update({"kind": "event", "target": "e2", "from": "a1"})
    sink.m["screened"] = {}
    il = art.Illustrator([art.Book(d, bk, sink, None)], Text(), Image(), "ink", portraits=None, workers=1, log=lambda *_: None, pause=0)
    one = il.counts(level="all")["chapters"][0]
    assert one["title"] == "One" and one["drawable"] is None and one["pictures"] == 1 and one["drawn"] == 0
    two = il.counts(level="all")["chapters"][1]
    assert two["pictures"] == 3 and two["drawn"] == 1                  # its plate, held; no event picture yet


def test_counts_say_how_many_portraits_a_run_would_still_draw():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    _, _, sink, _, _ = run(d, bk, level=None, portraits="all")
    il = art.Illustrator([art.Book(d, bk, sink, None)], Text(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    c = il.counts(level="none")
    assert c["portraits"] > 0 and c["new_portraits"] == 0                # every one drawn
    first = next(p for p in sink.m["pictures"] if p["kind"] == "portrait")
    sink.m["pictures"] = [p for p in sink.m["pictures"] if p is not first]
    il = art.Illustrator([art.Book(d, bk, sink, None)], Text(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    again = il.counts(level="none")
    assert again["new_portraits"] == 1 and again["portraits"] == c["portraits"]


def test_counts_give_the_most_portraits_a_run_could_draw_before_looks_are_checked():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    mk = lambda sink: art.Illustrator([art.Book(d, bk, sink, None)], Text(), Image(), "ink", portraits="all", workers=1,  # noqa: E731
                                      log=lambda *_: None, pause=0)
    c = mk(Mem()).counts(level="none")
    assert c["new_portraits"] == c["portraits"] and c["portraits_max"] == c["portraits"] * art.MAX_VERSIONS
    _, _, sink, _, _ = run(d, bk, level=None, portraits="all")       # a run checks every look, and draws
    c = mk(sink).counts(level="none")
    assert c["new_portraits"] == c["portraits_max"] == 0
    first = next(p for p in sink.m["pictures"] if p["kind"] == "portrait")
    sink.m["pictures"] = [p for p in sink.m["pictures"] if p is not first]
    c = mk(sink).counts(level="none")
    assert c["new_portraits"] == c["portraits_max"] == 1             # checked: the plan is the most
    sink.m["looks"].pop(first["target"])                             # unchecked again: up to MAX_VERSIONS, less what it holds
    c = mk(sink).counts(level="none")
    held = sum(p["kind"] == "portrait" and p["target"] == first["target"] for p in sink.m["pictures"])
    assert c["portraits_max"] == art.MAX_VERSIONS - held and c["new_portraits"] == 1


def test_illustrate_command_level_none_draws_portraits_only(tmp_path, monkeypatch):
    from lorekeeper import cli
    bk = book(("One", CH1), ("Two", CH2))
    idx = _cli_index(tmp_path, base_doc(bk), bk, "a.lore.json")
    monkeypatch.setattr(cli, "_make_model", lambda args, model=None, provider=None: Text())
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: Image())
    monkeypatch.setattr(epub, "read", lambda p: bk)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert cli.main(["illustrate", str(idx), "--epub", "x.epub", "--level", "none", "--out", str(tmp_path / "o")]) == 0
    m = json.loads((tmp_path / "o" / "art.json").read_text())
    assert m["pictures"] and {p["kind"] for p in m["pictures"]} == {"portrait"}


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
    assert unframed(s["prompt"]).startswith("The figure sits by the fire, head-and-shoulders portrait")
    assert rep.dropped["fixed: name replaced by a role"] >= 1 and not rep.dropped["dropped: name in prompt"]


def test_a_hyphenated_name_in_the_scene_is_fixed_not_dropped():
    def scene(p):
        return ("A half-Mira smile, " + PORTRAIT_SCENE[2:]) if is_portrait(p) else SCENE
    il, rep, sink, t, im = run(text_=Text(scene=scene))
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert unframed(m["prompt"]).startswith("A half-figure smile, head-and-shoulders portrait") and "Mira" not in m["prompt"]


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
    return [p for p, s in zip(t.prompts, t.systems) if s in WRITERS]


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
        system = {"portrait": art.PORTRAIT_WRITER, "place": art.WRITER}.get(kind, art.SCENE_WRITER)
        # an entity with items is classified once on the way (where its picture shows from); the mill has none
        looked = kind == "portrait"
        assert len(writer_calls(t)) == 1 and t.systems[-1] == system and pr.body, kind
        assert len(t.prompts) == 1 + looked and il.man[0]["screened"] == {}       # no screening on this path
        assert set(il.man[0]["looks"]) == ({"mira"} if looked else set()), kind


def test_the_prompt_is_the_writers_text_then_the_style_sentence_then_no_text():
    il, t = fresh(style="anime")
    pr = il.make_prompt(il.job("portrait", "mira", at="a11"))
    assert (pr.body, pr.style, pr.no_text) == (f"{art.PORTRAIT_FRAME} {PORTRAIT_SCENE} {art.PORTRAIT_CROP}", art.STYLES["anime"], art.NO_TEXT)
    assert pr.text == f"{art.PORTRAIT_FRAME} {PORTRAIT_SCENE} {art.PORTRAIT_CROP} {art.STYLES['anime']} {art.NO_TEXT}" and pr.scene == pr.body
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
    # no portraits yet: every figure is labelled all the same (the most tied first), with no picture sent
    assert '<entry name="the Stranger" type="person" figure="B" portrait="no">' in w
    assert '<entry name="Oren" type="person" figure="A" portrait="no">' in w
    assert '<entry name="Guild" type="group">' in w and w.index('name="Oren"') < w.index('name="Guild"')   # figures first
    assert w.count("<entry ") <= art.MAX_ENTRIES and w.rstrip().endswith(art.SHOWS)
    assert "<surroundings>" not in w and pr.shows == ["oren", "stranger"]   # labelled, in label order


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
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    e = entry(writer_calls(t)[0])
    assert e.index("Keeps the mill's books") < e.index("Sells eggs") < e.index("freckled face") and t.systems == [art.PORTRAIT_WRITER]
    il, t = fresh(d, bk, text_=Text(look=lambda txt: "freckled" in txt))
    il.classify(0, ["mira"])                   # a bulk run's look index, cached in the set
    t.prompts.clear(), t.systems.clear()
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    e = entry(writer_calls(t)[0])
    assert e.index("freckled face") < e.index("Keeps the mill's books") < e.index("Sells eggs") and t.systems == [art.PORTRAIT_WRITER]
    il.make_prompt(il.job("portrait", "mira", at="a1", looks="off"))        # a flagged detail after the anchor is still not there
    assert "freckled" not in entry(writer_calls(t)[-1])
    d["entities"][0]["facets"][-1]["text"] += " She hums."      # a rebuilt entity: the cache is stale, today's order again
    il2 = art.Illustrator([art.Book(d, bk, il.books[0].b.sink)], Text(), Image(), "ink", portraits="all", workers=1,
                          log=lambda *_: None, pause=0)
    il2.make_prompt(il2.job("portrait", "mira", at="a11", looks="off"))
    e = entry(il2.text.scenes()[0])
    assert e.index("Keeps the mill's books") < e.index("freckled face")


# ---- review round 1 and lead decisions 3


def test_a_person_joins_a_group_only_by_a_name_the_group_has_at_the_anchor():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"].append({"id": "bk.oren.f1", "from": "a3", "basis": "stated", "evidence": ["a3"],
                                       "text": "He keeps the Dawn watch at the mill."})
    guild = ent("guild", "the Guild", "group", frm="a1", facets=[portrait("guild", "a1", "Millers in white aprons.")])
    guild["names"].append({"value": "the Dawn", "kind": "alias", "from": "a10"})     # a name it takes later
    d["entities"].append(guild)
    il, t = fresh(d, bk)
    il.make_prompt(il.job("portrait", "oren", at="a5"))
    assert 'name="the Guild"' not in writer_calls(t)[-1]
    il.make_prompt(il.job("portrait", "oren", at="a11", looks="off"))       # Oren's anchor is a3 still: the alias is not reached there
    assert 'name="the Guild"' not in writer_calls(t)[-1]
    d["entities"][1]["facets"][-1]["from"] = d["entities"][1]["facets"][-1]["evidence"][0] = "a10"
    il, t = fresh(d, bk)
    il.make_prompt(il.job("portrait", "oren", at="a11", looks="off"))       # at a10 the Guild is "the Dawn" too
    assert 'name="the Guild"' in writer_calls(t)[-1]


def test_a_group_only_an_unfinished_earlier_book_has_never_joins_a_person():
    bk1, d1, bk2, d2 = series_docs()
    d1["entities"].append(ent("guild", "the Guild", "group", frm="a1", facets=[portrait("guild", "a1", "Millers in white aprons.")]))
    d2["entities"][1]["facets"].append({"id": "b2.oren.f1", "from": "b2.a2", "basis": "stated", "evidence": ["b2.a2"],
                                        "text": "Still grinds for the Guild."})
    t = Text()
    il = art.Illustrator([art.Book(d1, bk1, Mem()), art.Book(d2, bk2, Mem())], t, Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    pr = il.make_prompt(il.job("portrait", "oren", at="b2.a5", finished=set()))
    assert 'name="the Guild"' not in writer_calls(t)[-1] and pr.inherits == []
    pr = il.make_prompt(il.job("portrait", "oren", at="b2.a5", finished={"b1"}))
    assert 'name="the Guild"' in writer_calls(t)[-1] and "b1" in pr.inherits


def test_an_unfinished_earlier_books_words_are_unknown_names():
    bk1, d1, bk2, d2 = series_docs()          # "Zephyr" is in book one's text only
    scene = SCENE + " A storm the farmers call Zephyr gathers far off."
    il = art.Illustrator([art.Book(d1, bk1, Mem()), art.Book(d2, bk2, Mem())], Text(scene=lambda p: scene), Image(), "ink",
                         portraits=False, workers=1, log=lambda *_: None, pause=0)
    with pytest.raises(art.Dropped, match="unknown name") as e:
        il.make_prompt(il.job("plate", "Three", at="b2.a5", finished=set()))
    assert e.value.say == "a name the book has not used by this point"
    assert "Zephyr" in il.make_prompt(il.job("plate", "Three", at="b2.a5", finished={"b1"})).text
    assert "Zephyr" in il.make_prompt(il.job("plate", "Three", at="b2.a5")).text      # a run: every earlier book read


def test_a_word_the_book_has_not_used_is_caught_when_it_opens_a_sentence():
    early = "Zephyr gathers over the mill. " + SCENE
    il, t = fresh(text_=Text(scene=lambda p: early))
    with pytest.raises(art.Dropped, match="unknown name"):
        il.make_prompt(il.job("event", "e2", at="a5"))       # the book names Zephyr at a8
    assert len(writer_calls(t)) == 2 and "It was rejected: unknown name: Zephyr" in writer_calls(t)[1]
    il, t = fresh(text_=Text(scene=lambda p: early))
    assert il.make_prompt(il.job("event", "e5", at="a11")).body.startswith("Zephyr gathers")    # reached by then
    flour = "Sacks of flour lie in the cart. " + SCENE      # an ordinary word the book uses, capitalised only by the sentence
    il, t = fresh(text_=Text(scene=lambda p: flour))
    assert il.make_prompt(il.job("event", "e2", at="a5")).body.startswith("Sacks of flour") and len(writer_calls(t)) == 1


# Words the name check catches that are not names: a period, styles, nationalities, a region used as an adjective, and an
# ordinary word opening a sentence (the drops measured on Dracula); the scripted chooser's own idea of a name is `NAMES_ARE`.
PERIOD = ("A woman in Victorian dress stands under Gothic arches beside an English coach, a Transylvanian road behind her and "
          "an Eastern European inn on the hill. Lamplit windows glow along the street.")
NAMES_ARE = {"Vlad", "Lucy"}


def chooser(**kw):
    return Text(names=lambda w, s: w in NAMES_ARE, **kw)


def name_calls(t):
    return [p for p in t.prompts if "was checked for names" in p]


def test_a_caught_word_that_is_not_a_name_is_let_through_after_one_choosing_call():
    il, t = fresh(text_=chooser(scene=lambda p: f"{SCENE} {PERIOD}"))
    pr = il.make_prompt(il.job("event", "e2", at="a5"))
    assert PERIOD in pr.body and len(writer_calls(t)) == 1
    [ask] = name_calls(t)
    for w in ("Victorian", "Gothic", "English", "Transylvanian", "Eastern", "European", "Lamplit"):
        assert f'. {w}: "' in ask
    assert '. Lamplit: "Lamplit windows glow along the street."' in ask        # each word with the sentence it stands in
    assert il.report.dropped["kept: a flagged word was not a name"] == 7
    # the chooser is told that any name in a story counts, and to call it a name when unsure
    assert "real or invented" in art.NAMES and "When you are unsure, answer that it is a name." in art.NAMES
    assert "The name of a place itself" in art.NAMES and "made\nup for the story's own world is a name" in art.NAMES


def test_a_caught_word_used_as_a_name_is_dropped_as_before():
    for name in ("Vlad", "Lucy"):
        il, t = fresh(text_=chooser(scene=lambda p, n=name: f"{SCENE} {PERIOD} A man called {n} waits by the coach."))
        with pytest.raises(art.Dropped, match="unknown name") as e:
            il.make_prompt(il.job("event", "e2", at="a5"))
        assert e.value.detail == f"unknown name: {name}"               # the confirmed name, not the first word caught
        assert f"It was rejected: unknown name: {name}" in writer_calls(t)[1]
        assert e.value.say == "a name the book has not used by this point"


def test_the_old_rule_holds_when_the_choosing_call_fails_or_says_nothing():
    il, t = fresh(text_=Text(scene=lambda p: f"{SCENE} {PERIOD}", names=False))
    with pytest.raises(art.Dropped, match="unknown name") as e:
        il.make_prompt(il.job("event", "e2", at="a5"))
    assert e.value.detail == "unknown name: Victorian" and not il.report.dropped["kept: a flagged word was not a name"]

    class Silent(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "was checked for names" in prompt:
                self.prompts.append(prompt)
                self.systems.append(system)
                return self.reply(prompt) if callable(self.reply) else self.reply
            return super().complete(system, prompt, schema, prefix)

    for reply in ({}, {"words": []}, {"words": "none"}, {"words": [{"n": 99, "name": False}, {"n": 1, "name": "no"}]}):
        t = Silent(scene=lambda p: f"{SCENE} {PERIOD}")
        t.reply = reply
        il, _ = fresh(text_=t)
        with pytest.raises(art.Dropped, match="unknown name"):
            il.make_prompt(il.job("event", "e2", at="a5"))
        assert len(name_calls(t)) == 2 and not il._name_answers           # nothing cached, so the rewrite asks again
    # a word left unanswered counts as a name
    t = Silent(scene=lambda p: f"{SCENE} {PERIOD}")
    t.reply = lambda p: {"words": [{"n": int(n), "name": False}                # every word answered but Lamplit
                                   for n, w in re.findall(r'^(\d+)\. (\w+): ', p, re.M) if w != "Lamplit"]}
    il, _ = fresh(text_=t)
    with pytest.raises(art.Dropped, match="unknown name") as e:
        il.make_prompt(il.job("event", "e2", at="a5"))
    assert e.value.detail == "unknown name: Lamplit" and il.report.dropped["kept: a flagged word was not a name"] == 6


def test_the_choosing_call_is_cached_per_word_and_sentence_and_made_by_the_picker():
    writer, picker = Text(scene=lambda p: f"{SCENE} {PERIOD}"), chooser()
    il, _ = fresh(text_=writer, cheap_text=picker)
    il.make_prompt(il.job("event", "e2", at="a5"))
    il.make_prompt(il.job("event", "e1", at="a5"))
    assert len(name_calls(picker)) == 1 and not name_calls(writer)        # the same sentences: asked once
    # another sentence for the same word is asked about on its own
    writer.scene = lambda p: f"{SCENE} A Victorian lamp hangs over the door."
    il.make_prompt(il.job("event", "e1", at="a5"))
    assert len(name_calls(picker)) == 2 and name_calls(picker)[1].count(": \"") == 1


def test_every_use_of_a_caught_word_is_judged_and_one_name_is_enough():
    two = f"{SCENE} Gothic arches fill the chapel. She holds the Gothic against her chest."
    il, t = fresh(text_=Text(scene=lambda p: two, names=lambda w, s: "holds the Gothic" in s))
    with pytest.raises(art.Dropped, match="unknown name") as e:
        il.make_prompt(il.job("event", "e2", at="a5"))
    assert e.value.detail == "unknown name: Gothic"
    ask = name_calls(t)[0]
    assert '1. Gothic: "Gothic arches fill the chapel."' in ask and '2. Gothic: "She holds the Gothic against her chest."' in ask


def test_a_long_sentence_is_sent_as_a_window_that_holds_the_word():
    long = ("A woman with a basket on her arm stands at a wooden gate beside a grey stone mill, while a miller watches her "
            "from the doorway with his arms folded, soft morning light falling across the packed earth of the yard, a path "
            "leading off toward low green hills, the river bright beyond the mill wheel, a cart of sacks by the wall, and "
            "far behind them all the pale towers of Kesterholm rise above the mist on the farthest hill")
    assert long.index("Kesterholm") > art.NAME_SENTENCE_CHARS and "." not in long
    il, t = fresh(text_=Text(scene=lambda p: long + ".", names=lambda w, s: w == "Kesterholm" and "Kesterholm" in s))
    with pytest.raises(art.Dropped, match="unknown name") as e:
        il.make_prompt(il.job("event", "e2", at="a5"))
    assert e.value.detail == "unknown name: Kesterholm"
    [piece] = re.findall(r'^\d+\. Kesterholm: "(.*)"$', name_calls(t)[0], re.M)
    assert "Kesterholm" in piece and len(piece) <= art.NAME_SENTENCE_CHARS and long.endswith(piece.rstrip("."))
    assert piece.split()[0] in long.split()                         # whole words at the cut
    for w in ("Gothic", "Kesterholm", "Lucy"):                       # every excerpt holds its word, whatever the length
        for x in art.Illustrator._excerpts(" ".join([long] * 3).replace(" the ", f" the {w} ", 2), w):
            assert w in x and len(x) <= art.NAME_SENTENCE_CHARS


def test_a_plural_or_stem_of_a_word_judged_not_a_name_is_still_caught():
    hl = f"{SCENE} A Highland cow stands in the heather. The road climbs into the Highlands."
    il, t = fresh(text_=Text(scene=lambda p: hl, names=lambda w, s: w == "Highlands"))
    with pytest.raises(art.Dropped, match="unknown name") as e:
        il.make_prompt(il.job("event", "e2", at="a5"))
    assert e.value.detail == "unknown name: Highlands"
    assert '. Highland: "' in name_calls(t)[0] and '. Highlands: "' in name_calls(t)[0]


def test_a_name_answer_from_another_thread_is_never_replaced_and_kept_is_counted_once():
    il, _ = fresh()
    text_ = "A coach waits below Whitby on the quay."
    key = ("Whitby", text_)

    class Racing:
        def __init__(self, other):
            self.other = other

        def complete(self, system, prompt, schema, prefix=None):
            il._name_answers[key] = self.other          # another thread's answer lands while this call is in flight
            return {"words": [{"n": 1, "name": False}]}

    il.cheap = Racing(True)
    assert il._names_among(text_, ["Whitby"]) == ["Whitby"] and il._name_answers[key] is True
    assert not il.report.dropped["kept: a flagged word was not a name"]
    il._name_answers.clear()
    il.cheap = Racing(False)          # the other thread already answered and counted "not a name": not counted again
    assert il._names_among(text_, ["Whitby"]) == [] and il.report.dropped["kept: a flagged word was not a name"] == 0
    il._name_answers.clear()

    class Plain:
        def complete(self, system, prompt, schema, prefix=None):
            return {"words": [{"n": 1, "name": False}]}
    il.cheap = Plain()
    assert il._names_among(text_, ["Whitby"]) == [] and il.report.dropped["kept: a flagged word was not a name"] == 1


def test_a_word_the_book_already_uses_is_never_asked_about():
    il, t = fresh(text_=chooser(scene=lambda p: f"{SCENE} Oren's mill stands by the river."))
    il.make_prompt(il.job("event", "e2", at="a5"))
    assert not name_calls(t)


def test_a_run_and_soften_let_through_words_that_are_not_names():
    il, rep, sink, t, im = run(text_=chooser(scene=lambda p: (PORTRAIT_SCENE if is_portrait(p) else SCENE) + " " + PERIOD),
                               portraits=False)
    assert pics(sink, "plate") and not rep.skipped["dropped: unknown name in prompt"]
    assert rep.dropped["kept: a flagged word was not a name"] == 7
    assert "kept: a flagged word was not a name" not in json.dumps(sink.m)           # the detail stays out of the manifest
    bk = book(("One", CH1), ("Two", CH2))
    t = chooser(scene=lambda p: f"{QUIET} {PERIOD}")
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    assert PERIOD in il.soften(il.job("event", "e2", at="a3"), SCENE, "safety").body


def test_an_old_final_skip_for_no_look_described_is_tried_again():
    sink = Mem()
    sink.m = {**art.blank("sha256:tbk", "ink"), "skipped": [
        {"target": "oren", "reason": "skipped: no look described", "kind": "portrait", "from": "a3", "final": True},
        {"target": "mira", "reason": "refused: provider declined", "kind": "portrait", "from": "a4", "final": True}]}
    il, rep, sink, t, im = run(sink=sink)
    drawn = {p["target"] for p in pics(sink, "portrait")}
    assert "oren" in drawn and "mira" not in drawn                   # a refusal stays final
    assert [s["reason"] for s in sink.m["skipped"]] == ["refused: provider declined"]


def test_an_entitys_passages_put_what_its_latest_items_cite_first_then_how_the_book_first_describes_it(monkeypatch):
    # Lucy at her staking: the paragraphs her latest profile cites (the churchyard, undead) come before her first description
    monkeypatch.setattr(art, "ENTITY_WORDS", 40)        # half of it for the latest items' paragraphs, room for one more
    il, t = fresh()
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    passage = writer_calls(t)[0].split("<passage>")[1].split("</passage>")[0]
    assert CH1[4] in passage                               # what her profile (a4) cites
    assert CH1[0] in passage                               # her first naming paragraph
    assert CH1[2] not in passage and not any(p in passage for p in CH2)   # no room for the other mentions
    monkeypatch.setattr(art, "ENTITY_WORDS", 20)           # room for one: the latest item's paragraph wins
    il, t = fresh()
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    passage = writer_calls(t)[0].split("<passage>")[1].split("</passage>")[0]
    assert passage == CH1[4]


def test_the_writer_is_told_to_draw_what_it_says_and_trust_the_passages():
    for rule in ("also say what that looks like as the material gives it", "Every sentence is something an illustrator can draw",
                 "the passages win", "Use no name at all except the names of peoples",
                 "A portrait shows the figure as a living presence", "never their death, corpse or burial",
                 "undead or transformed is drawn as it looks at this point",
                 "Never give a specific look the material does not state", "say less (\"formal dress\", \"a man\") rather than invent"):
        assert rule in art.WRITER


def test_references_is_public_and_what_make_prompt_attaches():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append(portrait("mira", "a10", "A scarred young woman, older than she was, who keeps a coin.", 2))
    d["entities"][2]["facets"].append({"id": "bk.mill.f1", "from": "a1", "basis": "stated", "evidence": ["a1"],
                                       "text": "A grey stone mill with a wooden gate."})
    il, rep, sink, t, im = run(d, bk, level="all")
    by = {}
    for p in sorted(pics(sink, "portrait"), key=lambda p: ordinal(d)[p["from"]]):
        by.setdefault(p["target"], []).append(p)
    e6 = il.job("event", "e6", at="a11")                         # names mira and the stranger
    pr = il.make_prompt(e6)
    assert il.references(e6, pr.shows) == pr.references and {r["id"] for _, r in pr.references} == {
        by["mira"][0]["id"], by["stranger"][0]["id"]}      # e6 shows from a9: Mira's second portrait (a10) is later
    assert [r["id"] for _, r in il.references(e6, ["oren"])] == [by["oren"][0]["id"]]    # only what is shown
    assert il.references(e6, []) == []
    many = il.references(il.job("event", "e4", at="a11"), ["mira", "oren", "stranger", "gabriel", "mill"])
    assert len(many) <= art.MAX_REFERENCES
    change = art.Job("portrait", "mira", 0, "a10", change="scarred", shape="portrait")
    assert [r["id"] for _, r in il.references(change, ["mira"])] == [by["mira"][0]["id"]]     # the previous portrait
    # any later portrait takes the latest one before it, a look change or not; one at the first portrait's anchor takes none
    assert [r["id"] for _, r in il.references(art.Job("portrait", "mira", 0, "a10", shape="portrait"), ["mira"])] == [by["mira"][0]["id"]]
    assert il.references(art.Job("portrait", "mira", 0, by["mira"][0]["from"], shape="portrait"), ["mira"]) == []
    assert il.references(il.job("place", "mill", at="a11"), ["mill", "mira"]) == []           # a place: none


QUIET = ("A small crowd stands in the yard of a grey stone mill at dusk, faces turned toward a covered shape carried between two "
         "figures. A woman in a brown shawl holds a basket to her chest and looks away. A lamp burns in a doorway and the last thin "
         "light falls across the mud of the yard.")


def test_soften_writes_the_refused_prompt_again_from_the_same_material():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    t = Text(scene=lambda p: QUIET if "image model refused" in p else SCENE)
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    job = il.job("event", "e2", at="a3")
    first = il.make_prompt(job)
    assert "image model refused" not in writer_calls(t)[-1]
    pr = il.soften(job, first.text, "IMAGE_SAFETY")
    w = writer_calls(t)[-1]
    # the same material, then the refused text (without the style and no-text lines code appended) and the provider's reason
    assert w.startswith(writer_calls(t)[0]) and f'"{SCENE}"' in w and '"IMAGE_SAFETY"' in w
    assert art.NO_TEXT not in w and first.style not in w and "say \"young\"" in w and "misspellings" in w
    assert pr.body == QUIET and pr.text == f"{QUIET} {first.style} {art.NO_TEXT}" and pr.anchor == first.anchor
    assert pr.sources == first.sources and pr.writer == first.writer and pr.shape == first.shape
    # no reason given: the request still reads well
    il.soften(job, first.text)
    assert "the reason it gave" not in writer_calls(t)[-1]


def test_soften_runs_the_same_checks_with_one_rewrite():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    bad = QUIET.replace("A woman in a brown shawl", "Mira")
    t = Text(scene=lambda p: QUIET if "It was rejected" in p else bad)
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    job = il.job("event", "e2", at="a3")
    pr = il.soften(job, SCENE, "safety")
    assert pr.body == QUIET and "image model refused" in writer_calls(t)[-1] and "It was rejected: name in the text" in writer_calls(t)[-1]
    # a writer that keeps naming an unknown person gives up with the reason, like make_prompt
    t2 = Text(scene=lambda p: QUIET.replace("A small crowd", "Valdemar and a small crowd"))
    il2 = art.Illustrator([art.Book(d, bk, Mem())], t2, Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    with pytest.raises(art.Dropped, match="unknown name"):
        il2.soften(il2.job("event", "e2", at="a3"), SCENE, "safety")


def test_soften_keeps_the_readers_gate():
    bk1, d1, bk2, d2 = series_docs()
    d1["entities"][0]["facets"].append({"id": "b1.mira.f9", "from": "a6", "basis": "stated", "evidence": ["a6"],
                                        "text": "Wears a red ribbon in her dark braided hair."})
    t = Text(scene=lambda p: QUIET)
    il = art.Illustrator([art.Book(d1, bk1, Mem()), art.Book(d2, bk2, Mem())], t, Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    pr = il.soften(il.job("portrait", "mira", at="b2.a5", finished=set()), SCENE, "safety")
    assert "red ribbon" not in writer_calls(t)[-1] and pr.partial and pr.inherits == []
    pr = il.soften(il.job("portrait", "mira", at="b2.a5"), SCENE, "safety", finished={"b1"})
    assert "red ribbon" in writer_calls(t)[-1] and pr.inherits == ["b1"]


def likeness_set(d, *pics_, style="digital-art"):
    """Another style's set of the same edition holding the given picture records (each with its file)."""
    sink = Mem()
    sink.m = {**art.blank(d["editions"][0]["textHash"], style), "pictures": list(pics_)}
    for r in pics_:
        sink.files[r["file"]] = b"bytes of " + r["id"].encode()
    return sink


def pic(pid, target, at, notes=art.NOTE_VERSION, kind="portrait", created="2026-11-20T10:00:00Z", **kw):
    return {"id": pid, "kind": kind, "target": target, "from": at, "file": f"{pid}.png", "mime": "image/png", "prompt": "a figure",
            "references": [], "sources": [at], "inherits": [], "cost": 0.05, **({"notes": notes} if notes is not None else {}),
            "created": created, **kw}


def comic(d, bk, likeness, sink=None, t=None, im=None):
    return art.Illustrator([art.Book(d, bk, sink or Mem(), likeness=likeness)], t or Text(), im or Image(), "comic-strip",
                           portraits="all", workers=1, log=lambda *_: None, pause=0)


def test_a_figure_with_no_picture_in_this_set_takes_its_likeness_from_another_style():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    digital = likeness_set(d, pic("pdig0001", "mira", "a4"))
    im = Image()
    il = comic(d, bk, {"digital-art": digital}, im=im)
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    assert [(j, r["id"], r["set"]) for j, r in pr.references] == [(0, "pdig0001", "digital-art")]
    rec = il.draw(job, pr)
    call = im.calls[-1]
    assert call["references"] == [b"bytes of pdig0001"]
    assert "likeness" in call["note"] and "never the drawing style" in call["note"] and call["note"] not in call["prompt"]
    assert rec["references"] == [{"set": "digital-art", "id": "pdig0001"}] and "likeness" not in rec["prompt"]
    assert digital.m["pictures"] == [pic("pdig0001", "mira", "a4")]         # the other set is only read
    # once this set has its own current picture of her, that one is the reference and no likeness words are sent
    own = Mem()
    own.m = {**art.blank(d["editions"][0]["textHash"], "comic-strip"), "pictures": [pic("pcom0001", "mira", "a4")]}
    own.files["pcom0001.png"] = b"comic mira"
    im2 = Image()
    il2 = comic(d, bk, {"digital-art": digital}, sink=own, im=im2)
    job = il2.job("event", "e6", at="a11")
    pr = il2.make_prompt(job)
    assert [r["id"] for _, r in pr.references] == ["pcom0001"] and not pr.references[0][1].get("set")
    il2.draw(job, pr)
    # (her own picture, so no likeness words: only which reference is which figure)
    assert im2.calls[-1]["references"] == [b"comic mira"] and im2.calls[-1]["note"] == (
        "Reference picture 1 is figure A. " + art.FIGURE_NOTE + " " + art.CHEST_UP)


def test_a_likeness_is_never_past_the_anchor_older_partial_or_another_edition():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    later = [pic("plate001", "mira", "a10"), pic("pold0001", "mira", "a8", notes=2), pic("pnone001", "mira", "a8", notes=None),
             pic("ppart001", "mira", "a8", partial=True)]
    sets = {"digital-art": likeness_set(d, pic("pok00001", "mira", "a3"), *later),
            "anime": likeness_set({"editions": [{"textHash": "sha256:other"}]}, pic("pothr001", "mira", "a7"), style="anime"),
            "comic-strip": likeness_set(d, pic("pself001", "mira", "a7"), style="comic-strip")}   # this set's own slug: never
    il = comic(d, bk, sets)
    pr = il.make_prompt(il.job("event", "e6", at="a11"))    # e6 shows from a9: the a10 portrait is after it
    assert [r["id"] for _, r in pr.references] == ["pok00001"]
    il = comic(d, bk, {"digital-art": likeness_set(d, *later)})
    assert il.make_prompt(il.job("event", "e6", at="a9")).references == []        # the reader is at the event: a10 not reached
    # read past it, the a10 portrait is the figure's earliest available one (the older, unversioned and partial ones never)
    assert [r["id"] for _, r in il.make_prompt(il.job("event", "e6", at="a11")).references] == ["plate001"]


def test_the_latest_likeness_wins_then_the_most_recently_drawn():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    sets = {"digital-art": likeness_set(d, pic("pa000001", "mira", "a4", created="2026-11-21T00:00:00Z"),
                                         pic("pb000001", "mira", "a6", created="2026-11-01T00:00:00Z")),
            "anime": likeness_set(d, pic("pc000001", "mira", "a6", created="2026-11-22T00:00:00Z"), style="anime")}
    il = comic(d, bk, sets)
    refs = il.make_prompt(il.job("event", "e6", at="a11")).references
    assert [(r["id"], r["set"]) for _, r in refs] == [("pc000001", "anime")]


def test_a_look_change_portrait_takes_the_previous_likeness_from_another_style():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append(portrait("mira", "a10", "A scarred young woman, older than she was, who keeps a stranger's coin.", 2))
    digital = likeness_set(d, pic("pdig0001", "mira", "a4"))
    il = comic(d, bk, {"digital-art": digital})
    job = art.Job("portrait", "mira", 0, "a10", change="scarred")
    assert [(r["id"], r["set"]) for _, r in il.references(job, ["mira"])] == [("pdig0001", "digital-art")]


def test_a_likeness_from_an_earlier_book_needs_that_book_finished():
    bk1, d1, bk2, d2 = series_docs()
    digital = likeness_set(d1, pic("pdig0001", "mira", "a4"))
    t = Text()
    il = art.Illustrator([art.Book(d1, bk1, Mem(), None, draw=False, likeness={"digital-art": digital}),
                          art.Book(d2, bk2, Mem(), likeness={"digital-art": likeness_set(d2)})], t, Image(), "comic-strip",
                         portraits="all", workers=1, log=lambda *_: None, pause=0)
    job = il.job("plate", "Three", at="b2.a5", finished=set())
    assert il.make_prompt(job).references == []
    pr = il.make_prompt(il.job("plate", "Three", at="b2.a5", finished={"b1"}))
    assert [(j, r["id"], r["set"]) for j, r in pr.references] == [(0, "pdig0001", "digital-art")] and "b1" in pr.inherits


def test_a_portrait_is_a_likeness_with_the_framing_put_first_by_code():
    il, t = fresh(style="anime")
    pr = il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    assert t.systems == [art.PORTRAIT_WRITER] and pr.text.startswith(art.PORTRAIT_FRAME + " " + PORTRAIT_SCENE)
    assert pr.body.startswith(art.PORTRAIT_FRAME) and pr.text.index(art.PORTRAIT_FRAME) == 0
    # the crop is said again at the end of the body, before the style sentence
    assert pr.body.endswith(" " + art.PORTRAIT_CROP) and pr.text == f"{pr.body} {pr.style} {art.NO_TEXT}"
    for rule in ("framing and the pose are fixed and put around your text by code: the picture is cropped at mid-chest",
                 "Describe only what shows from mid-chest up", "Never the hands or the arms below the shoulder, anything held or carried",
                 "a belt or anything at the waist or hip, the legs or the feet, even where the material describes them",
                 "no smoke or breath, no other figures", "Expression: one short phrase at most",
                 "Background: one short clause at most", "never a room or place the passages tell a scene in", "40 to 90 words",
                 "A portrait shows the figure as a living presence", "Never give a specific look the material does not state"):
        assert rule in art.PORTRAIT_WRITER, rule
    assert "60 to 150 words" not in art.PORTRAIT_WRITER and "framing and the pose" not in art.WRITER
    # nothing a mid-chest crop leaves out is asked for any more
    assert "a sword at the hip" not in art.PORTRAIT_WRITER and "what they wear and carry" not in art.PORTRAIT_WRITER
    assert "half length" not in art.PORTRAIT_FRAME and "mid-chest" in art.PORTRAIT_FRAME and "hands" in art.PORTRAIT_FRAME
    assert "creature" in art.PORTRAIT_FRAME     # worded for a creature too
    assert "mid-chest" not in art.WRITER and "mid-chest" not in art.SCENE_WRITER   # scenes still draw the whole figure
    assert "The framing and pose are given" in writer_calls(t)[0]
    il, t = fresh()                        # no event has a setting: no surroundings, so a plain background
    il.make_prompt(il.job("portrait", "oren", at="a11"))
    assert "<surroundings>" not in writer_calls(t)[0] and "No usual surroundings are known" in writer_calls(t)[0]
    d = base_doc(book(("One", CH1), ("Two", CH2)))
    for e in d["events"]:
        e["setting"] = {"text": "the mill yard"}
    il, t = fresh(d)
    il.make_prompt(il.job("portrait", "oren", at="a11"))
    assert "<surroundings>" in writer_calls(t)[0] and "No usual surroundings are known" not in writer_calls(t)[0]
    # scenes, places and things keep the scene writer and no frame
    for kind, target in (("event", "e5"), ("plate", "Two"), ("place", "mill")):
        il, t = fresh()
        pr = il.make_prompt(il.job(kind, target, at="a11"))
        assert t.systems == [art.WRITER if kind == "place" else art.SCENE_WRITER] and art.PORTRAIT_FRAME not in pr.text, kind
    # the checks take a portrait's own word range: a 150-word portrait is too long, a scene of that length is not
    long_ = " ".join(["A tall man with a grey coat stands still."] * 17)
    il, t = fresh(text_=Text(scene=long_))
    with pytest.raises(art.Dropped, match="too long"):
        il.make_prompt(il.job("portrait", "mira", at="a11"))
    il, t = fresh(text_=Text(scene=" ".join(["A tall man with a grey coat stands by the gate."] * 14)))
    assert il.make_prompt(il.job("event", "e5", at="a11")).body


def test_soften_of_a_portrait_keeps_the_frame_and_does_not_send_it_to_the_writer():
    il, t = fresh(text_=Text(scene=lambda p: QUIET.replace("A small crowd stands", "A young woman stands") if "refused" in p else PORTRAIT_SCENE))
    job = il.job("portrait", "mira", at="a11")
    first = il.make_prompt(job)
    pr = il.soften(job, first.text, "safety")
    w = writer_calls(t)[-1]
    assert art.PORTRAIT_FRAME not in w and f'"{PORTRAIT_SCENE}"' in w and t.systems[-1] == art.PORTRAIT_WRITER
    assert art.PORTRAIT_CROP not in w          # the crop line is code's too: the writer is never asked to rewrite it
    assert pr.body.startswith(art.PORTRAIT_FRAME + " A young woman stands") and pr.body.endswith(art.PORTRAIT_CROP)
    assert pr.body.count(art.PORTRAIT_CROP) == 1 and pr.body.count(art.PORTRAIT_FRAME) == 1
    # a refused portrait drawn with the older frame is stripped of it as well
    old = first.text.replace(art.PORTRAIT_FRAME, art.PORTRAIT_FRAME_OLD).replace(" " + art.PORTRAIT_CROP, "")
    il.soften(job, old, "safety")
    w = writer_calls(t)[-1]
    assert art.PORTRAIT_FRAME_OLD not in w and f'"{PORTRAIT_SCENE}"' in w


def test_unframed_and_reframed():
    body = f"{art.PORTRAIT_FRAME} A young woman. {art.PORTRAIT_CROP}"
    assert art.unframed(body) == "A young woman." == art.unframed(f"{art.PORTRAIT_FRAME_OLD} A young woman.")
    assert art.unframed("A grey mill yard at dusk.") == "A grey mill yard at dusk."
    old = f"{art.PORTRAIT_FRAME_OLD} A young woman. Ink. {art.NO_TEXT}"
    new = f"{art.PORTRAIT_FRAME} A young woman. {art.PORTRAIT_CROP} Ink. {art.NO_TEXT}"
    # the new frame, and the crop line at the end of the body, before the set's style sentence and the no-text line
    assert art.reframed("portrait", old, "Ink.") == new
    # an unknown style: before the no-text line; no no-text line either: at the end
    assert art.reframed("portrait", old) == f"{art.PORTRAIT_FRAME} A young woman. Ink. {art.PORTRAIT_CROP} {art.NO_TEXT}"
    assert art.reframed("portrait", old, "Chalk.") == art.reframed("portrait", old)
    assert (art.reframed("portrait", f"{art.PORTRAIT_FRAME_OLD} A young woman with a sword at the hip.")
            == f"{art.PORTRAIT_FRAME} A young woman with a sword at the hip. {art.PORTRAIT_CROP}")   # old body kept
    assert art.reframed("portrait", new, "Ink.") == new                   # already the new frame: as given
    assert art.reframed("portrait", f"{art.PORTRAIT_FRAME_OLD} A. {art.PORTRAIT_CROP} Ink.").count(art.PORTRAIT_CROP) == 1
    assert art.reframed("portrait", "Draw her by a window.") == "Draw her by a window."   # a person's own words: as given
    assert art.reframed("event", old) == old                              # only portraits carry the frame


def test_the_writer_states_age_only_as_the_material_gives_it():
    for system in (art.WRITER, art.PORTRAIT_WRITER):
        assert "three years back I was thirty-two" in system and "Never age a figure with words the material does not use" in system
        assert '"weathered", "aged", "in his middle years", "grizzled"' in system and "not as age" in system
        assert "an aged woman" not in system and 'say less ("aged"' not in system


# ---- a look stated once is not lost later in the book (label-scene-figures, part 2)


LOOK_CH1 = ["Mira walked to the mill with her basket and waited by the gate that morning.",
            "The miller Oren came out of the mill door wiping flour from his hands slowly.",
            "She wore her long dark hair in a single heavy braid that swung behind her as she walked.",
            "Oren laughed and asked Mira to carry the sacks of flour into the cart outside.",
            "A small errand took Mira to the well behind the mill for a bucket of water.",
            "Evening came and Mira walked home along the dusty road beside the green river."]


def look_doc():
    """Mira's hair is told once, by her second profile (a2, citing the one paragraph that states it), and her latest profile
    (a9) no longer repeats it."""
    bk = book(("One", LOOK_CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"] = [
        portrait("mira", "a0", "A young woman who keeps a secret coin.", 1),
        {**portrait("mira", "a2", "A young woman with long dark braided hair who carries baskets to the mill.", 2)},
        {**portrait("mira", "a9", "A young woman who found a stranger's silver coin and keeps it secret.", 3), "evidence": ["a9"]}]
    return bk, d


def tag(w, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", w, re.S)
    return m.group(1) if m else None


def test_an_earlier_profile_reaches_a_portraits_material_with_its_paragraph_only_at_or_after_its_anchor():
    bk, d = look_doc()
    il, t = fresh(d, bk)
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    w = writer_calls(t)[-1]
    assert "long dark braided hair" in tag(w, "earlier_look") and "long dark braided hair" not in tag(w, "entry_to_draw")
    assert "single heavy braid" in tag(w, "passage")                  # the paragraph the earlier profile rests on
    assert "keeps a secret coin" not in tag(w, "earlier_look")       # the current profile says it already: left out
    assert "A look the material stated earlier" in art.WRITER and "<earlier_look>" in art.PORTRAIT_WRITER
    il.make_prompt(il.job("portrait", "mira", at="a5"))               # her hair profile is the current one, not an earlier look
    w = writer_calls(t)[-1]
    assert "braided" not in (tag(w, "earlier_look") or "") and "braided" in tag(w, "entry_to_draw") and "silver coin" not in w
    il.make_prompt(il.job("portrait", "mira", at="a1"))               # nothing past the reader's position
    w = writer_calls(t)[-1]
    assert "braided" not in w and "single heavy braid" not in w and "silver coin" not in w


def test_an_earlier_books_look_reaches_the_material_only_when_the_reader_finished_that_book():
    bk1, d1, bk2, d2 = series_docs()
    t = Text()
    il = art.Illustrator([art.Book(d1, bk1, Mem(), None, draw=False), art.Book(d2, bk2, Mem())], t, Image(), "ink",
                         portraits="all", workers=1, log=lambda *_: None, pause=0)
    pr = il.make_prompt(il.job("portrait", "mira", at="b2.a5", finished={"b1"}))
    assert "dark braided hair" in tag(writer_calls(t)[-1], "earlier_look") and "b1" in pr.inherits
    pr = il.make_prompt(il.job("portrait", "mira", at="b2.a5", finished=set()))
    assert "braided" not in writer_calls(t)[-1] and pr.inherits == []


def test_without_look_sentences_a_fresh_look_index_puts_an_earlier_look_item_first_and_else_only_profiles_go(monkeypatch):
    bk, d = look_doc()
    mira = d["entities"][0]
    mira["facets"] += [{"id": "bk.mira.f1", "from": "a1", "basis": "stated", "evidence": ["a1"], "text": "Has a freckled face and a crooked nose."},
                       {"id": "bk.mira.f2", "from": "a6", "basis": "stated", "evidence": ["a6"], "replaces": ["bk.mira.f1"],
                        "text": "Has a sunburnt face."}]
    il, t = fresh(d, bk, text_=Text(picks=False))   # no picks, no look index: the replaced detail is no profile, not sent
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    assert "crooked nose" not in writer_calls(t)[-1] and "braided hair" in tag(writer_calls(t)[-1], "earlier_look")
    il, t = fresh(d, bk, text_=Text(look=lambda txt: "crooked nose" in txt, picks=False))
    il.classify(0, ["mira"])
    monkeypatch.setattr(art, "EARLIER_FALLBACK_CHARS", 45)    # room for one sentence: the flagged one goes first
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    got = tag(writer_calls(t)[-1], "earlier_look")
    assert "crooked nose" in got and "braided" not in got
    monkeypatch.setattr(art, "EARLIER_FALLBACK_CHARS", 2000)
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    got = tag(writer_calls(t)[-1], "earlier_look")    # both, printed in reading order
    assert got.index("crooked nose") < got.index("braided hair")
    il.make_prompt(il.job("portrait", "mira", at="a5", looks="off"))   # the flagged detail is not replaced yet: it is in the entry
    assert "crooked nose" in tag(writer_calls(t)[-1], "entry_to_draw")


def test_a_scene_figures_earlier_look_fits_in_its_entry_budget():
    bk, d = look_doc()
    d["events"].append(event(7, 10, "Mira hides the silver coin under a loose stone by the well.", ["mira"]))
    il, t = fresh(d, bk)
    il.make_prompt(il.job("event", "e7", at="a11"))
    w = writer_calls(t)[-1]
    assert 'earlier_look name="Mira"' in w and "braided hair" in w
    entry = re.search(r'<entry name="Mira"[^>]*>(.*?)</entry>\s*<earlier_look name="Mira">(.*?)</earlier_look>', w, re.S)
    assert entry and len(entry.group(1)) + len(entry.group(2)) <= art.OTHER_CHARS


def test_a_later_portrait_takes_the_earlier_one_as_reference_with_a_note_and_a_first_one_none():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append(portrait("mira", "a10", "A young woman who keeps a stranger's coin.", 2))
    il, rep, sink, t, im = run(d, bk, level=None)
    first = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert [p["from"] for p in first] == ["a4"] and first[0]["references"] == []
    assert all(c["note"] == "" for c in im.calls)                     # first portraits: no reference, no note
    job = il.job("portrait", "mira", at="a11", looks="off")                        # on demand, later in the book
    pr = il.make_prompt(job)
    assert [r["id"] for _, r in pr.references] == [first[0]["id"]]
    rec = il.draw(job, pr)
    call = im.calls[-1]
    assert call["references"] == [sink.files[first[0]["file"]]] and call["note"] == art.SAME_FIGURE
    assert art.SAME_FIGURE not in call["prompt"] and rec["references"] == [first[0]["id"]]
    assert il.references(il.job("portrait", "mira", at="a9"), ["mira"]) == []     # the same anchor as the first: none


def test_a_portrait_never_takes_one_past_its_anchor_or_from_an_unfinished_book_and_a_likeness_note_is_merged():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    own = Mem()
    own.m = {**art.blank(d["editions"][0]["textHash"], "comic-strip"), "pictures": [pic("plate001", "mira", "a10")]}
    own.files["plate001.png"] = b"later"
    il = comic(d, bk, {}, sink=own)
    assert il.references(il.job("portrait", "mira", at="a9"), ["mira"]) == []     # a4: the a10 portrait is past it
    im = Image()
    il = comic(d, bk, {"digital-art": likeness_set(d, pic("pdig0001", "mira", "a3"))}, im=im)
    job = il.job("portrait", "mira", at="a9")
    il.draw(job, il.make_prompt(job))
    note = im.calls[-1]["note"]
    assert note.startswith(art.SAME_FIGURE + " Reference picture 1 is drawn in another style") and "never the drawing style" in note
    bk1, d1, bk2, d2 = series_docs()
    s1 = Mem()
    s1.m = {**art.blank(d1["editions"][0]["textHash"], "ink"), "pictures": [pic("pb1mira1", "mira", "a4")]}
    s1.files["pb1mira1.png"] = b"book one mira"
    il = art.Illustrator([art.Book(d1, bk1, s1, None, draw=False), art.Book(d2, bk2, Mem())], Text(), Image(), "ink",
                         portraits="all", workers=1, log=lambda *_: None, pause=0)
    assert il.references(il.job("portrait", "mira", at="b2.a5", finished=set()), ["mira"]) == []
    assert [r["id"] for _, r in il.references(il.job("portrait", "mira", at="b2.a5", finished={"b1"}), ["mira"])] == ["pb1mira1"]


# ---- figure labels: which reference picture is which figure (label-scene-figures, part 1)


ROOM = ("A grey stone mill yard in the soft morning light, a wooden gate open onto a dusty road and low green hills beyond, the "
        "river bright behind the turning wheel.")


def labelled(text_):
    """A set holding everyone's portrait (drawn by a run), and an Illustrator on it with the writer `text_` for scenes."""
    il, rep, sink, t, im = run(level=None)
    by = {p["target"]: p for p in pics(sink, "portrait")}
    im2 = Image()
    t2 = text_ if isinstance(text_, Text) else Text(scene=text_)
    il2 = art.Illustrator([art.Book(il.books[0].doc, il.books[0].epub, sink)], t2, im2, "ink", portraits="all", workers=1,
                          log=lambda *_: None, pause=0)
    return il2, t2, im2, by, sink


def test_every_figure_gets_a_label_those_with_a_picture_first_most_tied_first():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["events"].append(event(7, 10, "Oren and Mira talk at the gate of the mill.", ["oren", "mira"]))
    d["relations"] = [{"id": "r1", "from": "a3", "src": "mira", "dst": "oren", "label": "friend of", "basis": "stated",
                       "evidence": ["a3"]}, {"id": "r2", "from": "a3", "src": "mira", "dst": "mill", "label": "visits",
                                             "basis": "stated", "evidence": ["a3"]}]
    sink = Mem()
    sink.m = {**art.blank(d["editions"][0]["textHash"], "ink"), "pictures": [pic("pmira001", "mira", "a4"), pic("poren001", "oren", "a3")]}
    t = Text()
    il = art.Illustrator([art.Book(d, bk, sink)], t, Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    pr = il.make_prompt(il.job("event", "e6", at="a11"))  # Mira and the Stranger: only Mira has a portrait
    w = writer_calls(t)[-1]
    assert t.systems[-1] == art.SCENE_WRITER and '<entry name="Mira" type="person" figure="A" portrait="yes">' in w
    assert '<entry name="the Stranger" type="person" figure="B" portrait="no">' in w
    assert pr.figures == [{"label": "A", "entity": "mira"}, {"label": "B", "entity": "stranger", "portrait": False}]
    assert [r["id"] for _, r in pr.references] == ["pmira001"]
    pr = il.make_prompt(il.job("event", "e7", at="a11"))  # Oren and Mira: Mira has two ties, Oren one
    w = writer_calls(t)[-1]
    assert 'name="Mira" type="person" figure="A"' in w and 'name="Oren" type="person" figure="B"' in w
    assert pr.figures == [{"label": "A", "entity": "mira"}, {"label": "B", "entity": "oren"}]
    assert [r["id"] for _, r in pr.references] == ["pmira001", "poren001"]
    il.make_prompt(il.job("event", "e2", at="a2"))       # e2 shows from a1, the reader at a2: neither portrait reached
    w = writer_calls(t)[-1]
    assert 'portrait="yes"' not in w and 'name="Oren" type="person" figure="A" portrait="no"' in w
    pr = il.make_prompt(il.job("event", "e2", at="a11"))  # read past both: each takes its earliest portrait, after the scene
    # (no tie is visible at a1, so the event's own order: Oren, then Mira)
    assert 'figure="A"' in writer_calls(t)[-1] and [r["id"] for _, r in pr.references] == ["poren001", "pmira001"]
    for rule in ('is called by that label the first time', '"figure A, a tidewarden in a dark travelling coat"',
                 'With portrait="yes", do not describe their face, hair, eyes, skin or build',
                 'With portrait="no", say after the label how they look', "Never write the label of a figure you do not draw",
                 'portrait="yes" means the image model is given a reference picture of it'):
        assert art.SCENE_WRITER.count(rule) == 1 and rule not in art.WRITER and rule not in art.PORTRAIT_WRITER


def test_references_are_the_labels_in_the_text_in_label_order_and_an_unseen_figure_gets_none():
    # e6 names Mira (figure A) and the Stranger (figure B), both drawn: the writer draws only the Stranger; Mira is unseen
    il, t, im, by, sink = labelled(Text(scene=lambda p: f"{ROOM} Figure B, a hooded traveller in a long cloak, waits at the gate.",
                                        shows=["Mira", "the Stranger"]))
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    assert pr.figures == [{"label": "B", "entity": "stranger"}] and pr.shows == ["stranger"]
    assert [r["id"] for _, r in pr.references] == [by["stranger"]["id"]]
    rec = il.draw(job, pr)
    call = im.calls[-1]
    assert call["references"] == [sink.files[by["stranger"]["file"]]]
    assert call["note"] == "Reference picture 1 is figure B. " + art.FIGURE_NOTE + " " + art.CHEST_UP
    assert call["note"] not in call["prompt"]
    assert rec["references"] == [by["stranger"]["id"]]
    assert rec["figures"] == [{"label": "B", "entity": "stranger", "ref": by["stranger"]["id"]}]
    for name in ("Mira", "Stranger"):
        assert name not in call["prompt"]
    # both labelled, written B before A: references still go in label order
    il, t, im, by, sink = labelled(lambda p: f"{ROOM} Figure B, a hooded traveller, talks with figure A, a young woman.")
    job = il.job("event", "e6", at="a11")
    rec = il.draw(job, il.make_prompt(job))
    assert im.calls[-1]["references"] == [sink.files[by["mira"]["file"]], sink.files[by["stranger"]["file"]]]
    assert im.calls[-1]["note"].startswith("Reference picture 1 is figure A. Reference picture 2 is figure B. ")
    assert [f["label"] for f in rec["figures"]] == ["A", "B"]


def test_a_label_the_material_did_not_give_gets_one_rewrite_then_the_picture_is_dropped():
    il, t, im, by, sink = labelled(lambda p: f"{ROOM} Figure C stands by the gate." if "It was rejected" not in p
                                   else f"{ROOM} Figure A stands by the gate.")
    pr = il.make_prompt(il.job("event", "e6", at="a11"))
    assert "It was rejected: unknown figure label: figure C" in writer_calls(t)[-1] and pr.figures == [{"label": "A", "entity": "mira"}]
    il, t, im, by, sink = labelled(lambda p: f"{ROOM} Figure D stands by the gate.")
    with pytest.raises(art.Dropped, match="dropped: unknown figure label in prompt") as e:
        il.make_prompt(il.job("event", "e6", at="a11"))
    assert e.value.say == "a figure label the picture has no reference for: figure D"


def test_figure_labels_pass_the_name_checks():
    il, t, im, by, sink = labelled(lambda p: f"Figure A waits at the gate. {ROOM} Beside her stands figure B.")
    pr = il.make_prompt(il.job("event", "e6", at="a11"))
    assert pr.body.startswith("Figure A waits") and [f["label"] for f in pr.figures] == ["A", "B"]
    assert not il.names[0].find(pr.body) and art._opening_unknown(pr.body, il._known(0, "a11", None)) == ""


def test_an_edited_text_without_a_label_drops_its_reference_and_a_label_with_no_reference_gets_nothing():
    il, t, im, by, sink = labelled(lambda p: f"{ROOM} Figure A, a young woman, and figure B, a hooded traveller, stand at the gate.")
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    whole = pr.text
    edited = whole.replace(" and figure B, a hooded traveller,", "")
    rec = il.draw(job, pr, text=edited)
    call = im.calls[-1]
    assert call["references"] == [sink.files[by["mira"]["file"]]] and "figure B" not in call["note"]
    assert rec["figures"] == [{"label": "A", "entity": "mira", "ref": by["mira"]["id"]}] and rec["references"] == [by["mira"]["id"]]
    # a label typed in with no figure behind it, and a figure whose picture file is gone: no reference, no line in the note
    sink.files.pop(by["mira"]["file"])
    rec = il.draw(job, pr, text=whole + " Figure C watches.")
    call = im.calls[-1]
    assert call["references"] == [sink.files[by["stranger"]["file"]]] and call["note"] == ("Reference picture 1 is figure B. " + art.FIGURE_NOTE
                                                                              + " " + art.CHEST_UP)
    assert rec["figures"] == [{"label": "A", "entity": "mira", "ref": None},
                              {"label": "B", "entity": "stranger", "ref": by["stranger"]["id"]}]
    rec = il.draw(job, pr, text=ROOM + " " + ROOM)          # no label left: nothing is sent
    assert im.calls[-1]["references"] == [] and im.calls[-1]["note"] == "" and rec["figures"] == [] and rec["references"] == []


def test_the_note_numbers_the_labels_and_merges_the_likeness_words():
    refs = [(0, {"id": "p1"}), (0, {"id": "p2", "set": "anime"}), (0, {"id": "p3", "set": "anime"})]
    note = art.Illustrator.reference_note("event", refs, ["A", "B", "D"])
    assert note == ("Reference picture 1 is figure A. Reference picture 2 is figure B. Reference picture 3 is figure D. "
                    + art.FIGURE_NOTE + " Reference pictures 2 and 3 are drawn in another style. Take from them only the "
                    "likeness of the figure (face, hair, build, marks), never the drawing style, medium, line or colour "
                    "treatment: draw everything in the style the prompt asks for.")
    assert art.Illustrator.reference_note("event", refs[:2], None) == art.Illustrator.likeness_note(refs[:2])   # older prompt
    assert art.Illustrator.reference_note("event", [], ["A"]) == "" and art.Illustrator.reference_note("portrait", []) == ""
    assert art.Illustrator.reference_note("plate", refs[:1], ["A"]) == "Reference picture 1 is figure A. " + art.FIGURE_NOTE


def test_a_likeness_from_another_style_is_labelled_like_any_reference():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    im = Image()
    il = comic(d, bk, {"digital-art": likeness_set(d, pic("pdig0001", "mira", "a4"))}, im=im)
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    assert pr.figures == [{"label": "A", "entity": "mira"}, {"label": "B", "entity": "stranger", "portrait": False}]
    rec = il.draw(job, pr)
    assert im.calls[-1]["note"] == ("Reference picture 1 is figure A. " + art.FIGURE_NOTE + " Reference picture 1 is drawn in "
                                    "another style. Take from it only the likeness of the figure (face, hair, build, marks), "
                                    "never the drawing style, medium, line or colour treatment: draw everything in the style "
                                    "the prompt asks for. " + art.CHEST_UP)
    assert rec["figures"] == [{"label": "A", "entity": "mira", "ref": {"set": "digital-art", "id": "pdig0001"}},
                              {"label": "B", "entity": "stranger", "ref": None}]


def test_soften_keeps_the_labels_of_the_figures_it_still_draws():
    first = f"{ROOM} Figure B, a hooded traveller, grips figure A, a young woman, by the arm."
    calm = f"{ROOM} Figure B, a hooded traveller, stands near figure A, a young woman, at the gate."
    il, t, im, by, sink = labelled(lambda p: calm if "image model refused" in p else first)
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    soft = il.soften(job, pr.text, "violence")
    assert 'keep that label for each figure you still' in writer_calls(t)[-1]
    assert soft.figures == pr.figures and [r["id"] for _, r in soft.references] == [by["mira"]["id"], by["stranger"]["id"]]
    # a softened text that drops every label gets one rewrite naming it, then is dropped
    il, t, im, by, sink = labelled(lambda p: ROOM + " " + ROOM if "image model refused" in p else first)
    with pytest.raises(art.Dropped, match="lost the figure labels"):
        il.soften(il.job("event", "e6", at="a11"), first, "violence")
    assert "It was rejected: lost the figure labels" in writer_calls(t)[-1]
    # nor may it label a figure the refused text did not
    il, t, im, by, sink = labelled(lambda p: (f"{ROOM} Figure B waits by the gate in the cold." if "It was rejected" in p
                                              else f"{ROOM} Figure A and figure B wait by the gate.")
                                   if "image model refused" in p else f"{ROOM} Figure B waits by the gate in the cold.")
    soft = il.soften(il.job("event", "e6", at="a11"), f"{ROOM} Figure B waits by the gate in the cold.", "violence")
    assert "unknown figure label: figure A" in writer_calls(t)[-1] and soft.figures == [{"label": "B", "entity": "stranger"}]


def test_a_runs_own_rewrite_after_a_refusal_keeps_the_labels():
    first = f"{ROOM} Figure A, a young woman, grips figure B, a hooded traveller, by the arm."
    calm = f"{ROOM} Figure A, a young woman, stands near the gate while a hooded traveller waits."
    il, t, im, by, sink = labelled(lambda p: calm if "image model refused" in p else first)
    im.refuse = [lambda prompt, n: n == 1]
    job = il.job("event", "e6", at="a11")
    rec = il.draw(job)
    assert rec and il.report.softened == 1 and rec["figures"] == [{"label": "A", "entity": "mira", "ref": by["mira"]["id"]}]
    assert im.calls[-1]["references"] == [sink.files[by["mira"]["file"]]]


def test_an_older_prompt_without_figures_is_drawn_as_before():
    il, t, im, by, sink = labelled(lambda p: f"{ROOM} A hooded traveller and a young woman stand at the gate.")
    job = il.job("event", "e6", at="a11")
    old = art.Prompt(text="A hooded traveller and a young woman stand at the gate. Ink.", scene="", anchor="a9", shape="landscape",
                     references=[(0, by["mira"]), (0, by["stranger"])], version=art.NOTE_VERSION)
    assert old.figures is None
    rec = il.draw(job, old, text=old.text)
    call = im.calls[-1]
    # an older prompt: no labels, so only the words on using a portrait cropped at mid-chest
    assert call["references"] == [sink.files[by["mira"]["file"]], sink.files[by["stranger"]["file"]]] and call["note"] == art.CHEST_UP
    assert "figures" not in rec and rec["references"] == [by["mira"]["id"], by["stranger"]["id"]]


def test_references_follow_the_figures_given_whatever_shows_says():
    il, t, im, by, sink = labelled(lambda p: f"{ROOM} Figure B, a hooded traveller, waits at the gate.")
    job = il.job("event", "e6", at="a11")
    figs = [{"label": "B", "entity": "stranger"}]
    assert [r["id"] for _, r in il.references(job, ["mira", "stranger"], None, figs)] == [by["stranger"]["id"]]
    assert [r["id"] for _, r in il.references(job, ["mira", "stranger"])] == [by["mira"]["id"], by["stranger"]["id"]]   # older
    pairs = art.Illustrator.pair_figures([{"label": "B", "entity": "mira"}, {"label": "A", "entity": "oren"}],
                                         [(0, by["mira"])], "figure B only")
    assert pairs == [({"label": "B", "entity": "mira"}, (0, by["mira"]))]
    assert art.labels_in("Figure B and figure A; figures C, a figure Dx, figure E, figure G") == ["A", "B", "E"]


# ---- the look sentences: which earlier sentences tell the look (label-scene-figures, round 1b)


def picks_of(t):
    return [p for p in t.prompts if "Which sentences tell how" in p]


def picked_doc():
    bk, d = look_doc()
    d["entities"][0]["facets"][1]["text"] = ("A young woman who sells eggs at the market. She wears her long dark hair in a braid. "
                                             "She carries baskets to the mill.")
    return bk, d


def test_earlier_look_holds_only_the_picked_sentences_and_a_second_prompt_makes_no_call():
    bk, d = picked_doc()
    il, t = fresh(d, bk)
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    w = writer_calls(t)[-1]
    assert tag(w, "earlier_look") == "She wears her long dark hair in a braid." and "single heavy braid" in tag(w, "passage")
    assert len(picks_of(t)) == 1 and "Mira (a character)" in picks_of(t)[0]
    assert il.man[0]["lookSentences"]["mira"]["picks"] == {"bk.mira.portrait-2": [1]}
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    il2 = art.Illustrator([art.Book(d, bk, il.books[0].b.sink)], Text(), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    il2.make_prompt(il2.job("portrait", "mira", at="a11"))       # the cache is in the set: another Illustrator reads it
    assert len(picks_of(t)) == 1 and picks_of(il2.text) == [] and art.EARLIER_CHARS == 1000
    d["entities"][0]["facets"][1]["text"] += " She hums."         # a rebuilt index: the key changes, it picks again
    il3 = art.Illustrator([art.Book(d, bk, il.books[0].b.sink)], Text(), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    il3.make_prompt(il3.job("portrait", "mira", at="a11"))
    assert len(picks_of(il3.text)) == 1


def test_a_picked_sentence_counts_only_through_its_own_item_at_the_anchor_and_for_a_finished_book():
    bk, d = picked_doc()
    d["entities"][0]["facets"].append(portrait("mira", "a10", "A young woman with a scar across her cheek.", 4))
    il, t = fresh(d, bk)
    il.make_prompt(il.job("portrait", "mira", at="a9", looks="off"))          # the cache covers the a10 item, the material does not
    assert il.man[0]["lookSentences"]["mira"]["picks"]["bk.mira.portrait-4"] == [0]
    w = writer_calls(t)[-1]
    assert "scar" not in w and tag(w, "earlier_look") == "She wears her long dark hair in a braid."
    bk1, d1, bk2, d2 = series_docs()
    d1["entities"][0]["facets"].append(portrait("mira", "a10", "A young woman who keeps a coin. She wears a red ribbon in her hair.", 2))
    t = Text()
    il = art.Illustrator([art.Book(d1, bk1, Mem(), None, draw=False), art.Book(d2, bk2, Mem())], t, Image(), "ink",
                         portraits="all", workers=1, log=lambda *_: None, pause=0)
    il.make_prompt(il.job("portrait", "mira", at="b2.a5", finished=set()))
    assert picks_of(t) == [] and "ribbon" not in writer_calls(t)[-1] and "braided" not in writer_calls(t)[-1]
    pr = il.make_prompt(il.job("portrait", "mira", at="b2.a5", finished={"b1"}))
    got = tag(writer_calls(t)[-1], "earlier_look")
    assert len(picks_of(t)) == 1 and "dark braided hair" in got and "red ribbon" in got and "b1" in pr.inherits


def test_a_failed_pick_falls_back_and_is_not_cached():
    bk, d = picked_doc()
    il, t = fresh(d, bk, text_=Text(picks=False))
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))        # no error: the earlier profiles, whole sentences that add words
    got = tag(writer_calls(t)[-1], "earlier_look")
    assert "long dark hair in a braid" in got and "sells eggs" in got
    assert "mira" not in il.man[0]["lookSentences"] and len(picks_of(t)) == 2     # tried (and retried) once, not stored


def test_a_run_picks_while_planning_and_its_prompts_never_wait_on_a_pick():
    bk, d = picked_doc()
    d["events"].append(event(7, 10, "Mira hides the coin under a loose stone by the well.", ["mira"]))
    t, cheap = Text(), Text()
    il = art.Illustrator([art.Book(d, bk, Mem(), "all")], t, Image(), "ink", portraits="all", workers=1, log=lambda *_: None,
                         pause=0, cheap_text=cheap)
    il.run()
    assert picks_of(t) == [] and [p.split(",")[1] for p in picks_of(cheap)] == [" Mira (a character)"]   # the cheap model
    assert il.man[0]["lookSentences"]["mira"]["picks"]
    assert not il.pick_on_demand
    il2 = art.Illustrator([art.Book(d, bk, Mem(), "all")], Text(picks=False), Image(), "ink", portraits="all", workers=1,
                          log=lambda *_: None, pause=0)
    il2.plan(0)                                       # planning tries; a run then never calls at prompt time
    n = len(picks_of(il2.text))
    il2.pick_on_demand = False
    il2.make_prompt(il2.job("portrait", "mira", at="a11"))
    assert len(picks_of(il2.text)) == n and "sells eggs" in writer_calls(il2.text)[-1]    # the fallback


def test_a_look_told_once_beats_a_run_of_near_repeats_and_a_long_pick_is_batched(monkeypatch):
    bk, d = look_doc()
    repeats = [portrait("mira", "a1", f"A tall young woman with grey eyes and a {w} shawl.", 10 + n)
               for n, w in enumerate(["brown", "dun", "tawny", "russet", "umber"])]
    once = portrait("mira", "a6", "She wears her long dark hair in a braid with copper rings.", 20)
    d["entities"][0]["facets"] = repeats + [once, portrait("mira", "a9", "A young woman who keeps a coin.", 30)]
    monkeypatch.setattr(art, "EARLIER_CHARS", 120)      # room for two sentences
    monkeypatch.setattr(art, "LOOK_SENTENCE_BATCH", 3)
    il, t = fresh(d, bk)
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    got = tag(writer_calls(t)[-1], "earlier_look")
    assert "copper rings" in got and got.index("shawl") < got.index("copper rings")   # chosen by what it adds, printed in order
    assert len(picks_of(t)) == 3                       # 7 distinct sentences, 3 a call (each numbered from 1)
    assert set(il.man[0]["lookSentences"]["mira"]["picks"]) == {f"bk.mira.portrait-{n}" for n in (10, 11, 12, 13, 14, 20)}
    calls = iter([None, RuntimeError("down"), RuntimeError("down")])

    class Flaky(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "Which sentences tell how" in prompt and (e := next(calls, None)):
                raise e
            return super().complete(system, prompt, schema, prefix)
    il, t = fresh(d, bk, text_=Flaky())                # the second batch fails: nothing is kept, the fallback is used
    il.make_prompt(il.job("portrait", "mira", at="a11"))
    assert "mira" not in il.man[0]["lookSentences"] and tag(writer_calls(t)[-1], "earlier_look")


# ---- review round 1


def test_a_rewrite_of_a_refused_text_with_no_label_never_adds_one():
    plain = f"{ROOM} A young woman in a brown shawl waits by the gate for the hooded traveller."
    added = f"{ROOM} Figure A, a young woman in a brown shawl, waits by the gate in the cold."
    il, t, im, by, sink = labelled(lambda p: (plain if "It was rejected" in p else added) if "image model refused" in p else plain)
    job = il.job("event", "e6", at="a11")
    soft = il.soften(job, plain, "violence")              # the person's sheet
    assert "It was rejected: unknown figure label: figure A" in writer_calls(t)[-1]
    assert soft.figures == [] and soft.references == []
    il, t, im, by, sink = labelled(lambda p: (plain if "It was rejected" in p else added) if "image model refused" in p else plain)
    im.refuse = [lambda prompt, n: n == 1]
    rec = il.draw(il.job("event", "e6", at="a11"))         # a run's own rewrite after a refusal
    assert rec and "It was rejected: unknown figure label: figure A" in writer_calls(t)[-1]
    assert rec["figures"] == [] and rec["references"] == [] and im.calls[-1]["references"] == []


def test_a_scene_figures_earlier_look_is_chosen_against_the_entry_as_sent(monkeypatch):
    bk, d = look_doc()
    d["entities"][0]["facets"] = [
        portrait("mira", "a2", "Her hair is long and dark. She wears copper rings in her braid.", 1),
        {"id": "bk.mira.f1", "from": "a3", "basis": "stated", "evidence": ["a3"], "text": "Wears copper rings in her braid."},
        {"id": "bk.mira.f2", "from": "a8", "basis": "stated", "evidence": ["a8"], "text": "Sells eggs at the market on market days."},
        portrait("mira", "a9", "A young woman who keeps a coin.", 2)]
    d["events"].append(event(7, 10, "Mira hides the coin under a loose stone by the well.", ["mira"]))
    monkeypatch.setattr(art, "OTHER_CHARS", 120)
    il, t = fresh(d, bk, text_=Text(picks=lambda s: True))
    il.make_prompt(il.job("event", "e7", at="a11"))
    w = writer_calls(t)[-1]
    entry, earlier = re.search(r'<entry name="Mira"[^>]*>(.*?)</entry>\s*<earlier_look name="Mira">(.*?)</earlier_look>', w, re.S).groups()
    assert "Wears copper rings" not in entry and "copper rings" in earlier    # the detail made room; its look came back
    assert len(entry) + len(earlier) + 1 <= 120


def test_picked_sentences_of_one_note_print_in_reading_order():
    bk, d = look_doc()
    d["entities"][0]["facets"][1]["text"] = "She has grey eyes. She wears long dark hair braided with copper rings and a brown shawl."
    il, t = fresh(d, bk, text_=Text(picks=lambda s: True))
    il.make_prompt(il.job("portrait", "mira", at="a11", looks="off"))
    got = tag(writer_calls(t)[-1], "earlier_look")
    assert got.index("She has grey eyes.") < got.index("She wears long dark hair")


# ---- the world's period and technology in the setting (label-scene-figures, part 3)


def test_the_writer_states_the_worlds_period_and_technology_wherever_it_draws_a_setting():
    rule = "say the period the story is set in and its level of technology"
    for system in (art.WRITER, art.SCENE_WRITER, art.PORTRAIT_WRITER):
        assert rule in system and "When neither the book nor the material makes the period plain, say nothing about it" in system
        assert "This is not a specific look the material lacks" in system
        assert "Never give a specific look the material does not state" in system      # the figures' rule is unchanged
    # a general rule: no book of the bench (or any of its places, castes or people) is named in what the writer is told
    told = " ".join([art.WRITER, art.SCENE_WRITER, art.PORTRAIT_WRITER, art.SOFTEN, art.LOOK_SENTENCES, *art.TASKS.values()])
    for word in ("Red Rising", "Mars", "Gold", "silversaint", "Dracula", "Transylvania", "Carpathian", "Sherlock", "Holmes", "London",
                 "Empire of the Vampire", "Victorian", "far-future"):
        assert word not in told, word


# ---- one entry drawn through the book up to the reader's position (draw-character-through-book)

def oren_with_a_lost_eye():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"].append({"id": "bk.oren.f1", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Oren lost an eye in the storm."})
    return bk, d


def test_chosen_entities_get_portraits_whatever_the_main_cast_rule_says():
    bk, d = oren_with_a_lost_eye()
    il, rep, sink, t, im = run(d, bk, portraits={"oren", "mill", "nobody"}, level="none")
    assert [(p["target"], p["from"]) for p in pics(sink)] == [("oren", "a3"), ("oren", "a10")]
    assert "oren" not in art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", log=lambda *_: None).main_cast(0)
    assert art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", portraits=set(), log=lambda *_: None).portraits is None


def test_until_draws_nothing_past_it_and_no_look_change_after_it():
    bk, d = oren_with_a_lost_eye()
    il, rep, sink, t, im = run(d, bk, portraits={"oren"}, level="main", until=(0, "a5"))
    ords = ordinal(d)
    assert [(p["kind"], p["target"], p["from"]) for p in pics(sink)] == [("portrait", "oren", "a3"), ("plate", "One", "a5")]
    assert all(ords[s] <= ords["a5"] for p in pics(sink) for s in p["sources"])
    assert not any("Which of them are a visible moment" in p and "Gabriel" in p for p in t.prompts)   # chapter Two not screened
    assert il.counts(0)["portraits"] == 1 and il.counts(0)["new_portraits"] == 0
    assert [c["chosen"] for c in il.counts(0)["chapters"]] == [True, False]
    # at the end of the book the look change is drawn too, and the first portrait is not drawn again
    _, rep2, _, _, im2 = run(d, bk, sink, portraits={"oren"}, level="none", until=(-1, len(d["anchors"]) - 1))
    assert rep2.drawn == 1 and [p["from"] for p in pics(sink, "portrait")] == ["a3", "a10"]
    with pytest.raises(ValueError, match="until"):
        art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", until=(0, "nowhere"), log=lambda *_: None)


def test_until_before_the_first_look_draws_the_first_profile_instead():
    bk, d = vesna_doc()                      # her look is first described at a9, her first profile is at a2
    _, _, sink, t, _ = run(d, bk, portraits={"vesna"}, level="none", until=(0, "a5"))
    assert [p["from"] for p in pics(sink)] == ["a2"]
    asked = [p for p in t.scenes() if drawing(p) == "Vesna"]
    assert asked and not any(w in p for p in asked for w in ("white hair", "hooked nose", "black shawl"))   # the a9 look
    _, _, sink2, _, _ = run(d, bk, portraits={"vesna"}, level="none")
    assert [p["from"] for p in pics(sink2)] == ["a9"]       # a bulk run's plan when nothing limits it


def test_a_reader_who_has_not_finished_the_earlier_book_gets_partial_pictures_of_this_one_only():
    bk1, d1, bk2, d2 = series_docs()

    def go(finished, s1, s2):
        il = art.Illustrator([art.Book(d1, bk1, s1, None), art.Book(d2, bk2, s2, None)], Text(), Image(), "ink",
                             portraits={"mira"}, until=(1, "b2.a5"), finished=finished, workers=1, log=lambda *_: None, pause=0)
        return il, il.run()
    s1, s2 = Mem(), Mem()
    il, rep = go([], s1, s2)
    assert s1.m is None or not pics(s1)                                   # the unfinished book is never drawn
    assert [p["target"] for p in pics(s2)] == ["mira"] and all(p.get("partial") for p in pics(s2))
    written = il.text.scenes()
    assert written and not any(w in p for p in written for w in ("Zephyr", "Gabriel", "Stranger", "silver coin"))   # book one only
    assert il.counts(0)["portraits"] == 0 and il.counts(1)["new_portraits"] == 0
    _, rep2 = go([], s1, s2)                                              # drawn already, for this reader
    assert rep2.drawn == 0
    s1b, s2b = Mem(), Mem()
    il3, rep3 = go(["b1"], s1b, s2b)                                      # a reader who finished book one
    assert [p["target"] for p in pics(s1b)] == ["mira"] and not any(p.get("partial") for p in pics(s1b) + pics(s2b))
    assert [p["target"] for p in pics(s2b)] == ["mira"]                   # the scar is a look change in book two
    # a partial picture never counts as drawn for a reader who finished every book
    il4 = art.Illustrator([art.Book(d1, bk1, Mem(), None), art.Book(d2, bk2, s2, None)], Text(), Image(), "ink",
                          portraits={"mira"}, finished=["b1"], workers=1, log=lambda *_: None, pause=0)
    assert il4.counts(1)["new_portraits"] == il4.counts(1)["portraits"] >= 1


def test_counts_for_chosen_entities_and_until_need_no_model_call():
    bk, d = oren_with_a_lost_eye()
    t = Text()
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", portraits={"oren"}, until=(0, "a11"),
                         log=lambda *_: None)
    got = il.counts(0, level=None)
    assert got["portraits"] == got["new_portraits"] == 1 and got["plates"] == 0 and t.prompts == []   # looks unchecked
    il.classify(0, ["oren"])
    assert il.counts(0, level=None)["new_portraits"] == 2
    il2 = art.Illustrator([art.Book(d, bk, il.books[0].b.sink)], t, Image(), "ink", portraits={"oren"}, until=(0, "a9"),
                          log=lambda *_: None)
    assert il2.counts(0, level=None)["new_portraits"] == 1


def test_until_after_a_look_but_before_the_first_profile_starts_at_the_look_and_keeps_its_changes():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    mira = d["entities"][0]
    mira["facets"] = [{"id": "bk.mira.f1", "from": "a1", "basis": "stated", "evidence": ["a1"],
                       "text": "A slight young woman with dark braided hair."},
                      {"id": "bk.mira.f2", "from": "a4", "basis": "stated", "evidence": ["a4"],
                       "text": "Her arm is scarred by the mill wheel."},
                      portrait("mira", "a10", "A slight young woman with a scarred arm who carries baskets to the mill.")]
    _, _, sink, _, _ = run(d, bk, portraits={"mira"}, level="none", until=(0, "a5"))
    assert [p["from"] for p in pics(sink)] == ["a1", "a4"]
    _, _, sink2, _, _ = run(d, bk, portraits={"mira"}, level="none")
    assert [p["from"] for p in pics(sink2)][:2] == ["a1", "a4"]      # what a bulk run plans up to there (then a10)


# ---- the scene's place and things (consistent-scene-places)


MILL_THEN = "A stone mill of grey granite with a slate roof and a great oak wheel turning in the river."
MILL_NOW = "The mill on the river where travellers ask for a bed."


def place_doc(link=True):
    """The mill described twice (its look told first, then only its use), a silver coin (a thing) described twice the same
    way, and e6 (Mira and the Stranger, with the coin) happening at the mill when `link`."""
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][2]["facets"] = [portrait("mill", "a0", MILL_THEN, 1), portrait("mill", "a7", MILL_NOW, 2)]
    d["entities"].append(ent("coin", "the silver coin", "object", frm="a7", facets=[
        portrait("coin", "a7", "A silver coin stamped with a crowned wolf on one face.", 1),
        portrait("coin", "a9", "The coin the stranger left behind on the table.", 2)]))
    d["events"][5]["entities"].append("coin")
    if link:
        d["events"][5]["setting"] = {"text": "the mill yard", "place": "mill"}
    return bk, d


def scene_writer(d, bk, kind="event", target="e6", at="a11", sink=None, **kw):
    sink = sink or Mem()
    t = Text(picks=lambda s: True)
    il = art.Illustrator([art.Book(d, bk, sink)], t, Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0, **kw)
    pr = il.make_prompt(il.job(kind, target, at=at))
    return writer_calls(t)[-1], pr, il


def labelled_sink(d):
    sink = Mem()
    sink.m = {**art.blank(d["editions"][0]["textHash"], "ink"), "pictures": [pic("pmira001", "mira", "a4"),
                                                                            pic("pstra001", "stranger", "a7")]}
    sink.files.update({"pmira001.png": b"mira", "pstra001.png": b"stranger"})
    return sink


def test_a_scene_at_a_linked_place_gets_the_places_entry_with_its_earlier_look_and_the_figures_are_unchanged():
    bk, d = place_doc()
    w, pr, _ = scene_writer(d, bk, sink=labelled_sink(d))
    assert tag(w, "setting_entry") == MILL_NOW and 'name="the mill" type="place"' in w
    assert "slate roof" in tag(w.split("<setting_entry", 1)[1], "earlier_look")        # its look told earlier comes with it
    assert '<entry name="the mill"' not in w and w.index("<setting_entry") > w.rindex("<entry ")
    assert "(where: the mill yard)" in tag(w, "moment") and {"a0", "a7"} <= set(pr.sources)
    # the thing the event names keeps its look the same way
    coin = re.search(r'<entry name="the silver coin" type="thing">(.*?)</entry>\s*<earlier_look name="the silver coin">(.*?)</earlier_look>', w, re.S)
    assert coin and "crowned wolf" in coin.group(2)
    # the figures' entries, their labels and what is attached are those of the same scene with no linked place
    bk2, d2 = place_doc(link=False)
    w2, pr2, _ = scene_writer(d2, bk2, sink=labelled_sink(d2))
    entries = lambda w: re.findall(r'<entry [^>]*>.*?</entry>(?:\s*<earlier_look name="[^"]*">.*?</earlier_look>)?', w, re.S)
    assert entries(w) == entries(w2) and "<setting_entry" not in w2
    assert pr.figures == pr2.figures == [{"label": "A", "entity": "mira"}, {"label": "B", "entity": "stranger"}]
    assert [r["id"] for _, r in pr.references] == [r["id"] for _, r in pr2.references] == ["pmira001", "pstra001"]
    assert pr.shows == pr2.shows


def test_the_place_entry_is_the_one_visible_at_the_anchor_and_never_counts_against_the_figures(monkeypatch):
    bk, d = place_doc()
    d["entities"][2]["facets"][1]["from"] = d["entities"][2]["facets"][1]["evidence"][0] = "a10"   # described after e6
    w, _, _ = scene_writer(d, bk, at="a9")
    assert tag(w, "setting_entry") == MILL_THEN and "travellers" not in w
    bk, d = place_doc()
    for e in d["entities"]:
        if e["id"] == "mill":            # the place itself is first named after the moment: no entry, the words stay
            e["from"] = e["names"][0]["from"] = "a10"
            e["facets"] = [portrait("mill", "a10", MILL_THEN, 1)]
    w, _, _ = scene_writer(d, bk, at="a11")
    assert "<setting_entry" not in w and "slate roof" not in w and "(where: the mill yard)" in w
    bk, d = place_doc()
    monkeypatch.setattr(art, "MAX_ENTRIES", 2)        # the figures fill the entries; the place still has its own
    w, _, _ = scene_writer(d, bk)
    assert w.count("<entry ") == 2 and 'name="Mira"' in w and 'name="the Stranger"' in w and tag(w, "setting_entry") == MILL_NOW


def test_a_place_only_an_unfinished_earlier_book_has_never_reaches_the_scene():
    bk1, d1, bk2, d2 = series_docs()
    d1["entities"][2]["facets"] = [portrait("mill", "a0", MILL_THEN, 1, "b1")]
    d2["events"][0]["setting"] = {"text": "the mill door", "place": "mill"}
    t = Text(picks=lambda s: True)
    il = art.Illustrator([art.Book(d1, bk1, Mem(), None, draw=False), art.Book(d2, bk2, Mem())], t, Image(), "ink",
                         portraits="all", workers=1, log=lambda *_: None, pause=0)
    pr = il.make_prompt(il.job("event", "e1", at="b2.a5", finished=set()))
    w = writer_calls(t)[-1]
    assert "<setting_entry" not in w and "slate roof" not in w and pr.inherits == [] and pr.partial
    pr = il.make_prompt(il.job("event", "e1", at="b2.a5", finished={"b1"}))
    assert tag(writer_calls(t)[-1], "setting_entry") == MILL_THEN and "b1" in pr.inherits


def test_a_plate_takes_the_place_of_its_main_moment_only():
    bk, d = place_doc()
    w, pr, il = scene_writer(d, bk, "plate", "Two")
    assert il.job("plate", "Two").event["id"] == "e4" and "<setting_entry" not in w     # e6's place is not the plate's
    d["events"][3]["setting"] = {"text": "the mill", "place": "mill"}
    w, pr, _ = scene_writer(d, bk, "plate", "Two")
    assert tag(w, "setting_entry") == MILL_NOW


def test_the_place_rule_is_the_scene_writers_and_a_portrait_never_gets_a_setting_entry():
    rule = "When there is a <setting_entry>, the moment happens there: draw the setting as it and its <earlier_look> describe it"
    assert rule in art.SCENE_WRITER and rule not in art.WRITER and rule not in art.PORTRAIT_WRITER
    assert "<setting_entry>" not in art.WRITER and "<setting_entry>" not in art.PORTRAIT_WRITER
    bk, d = place_doc()
    for kind, target in (("portrait", "stranger"), ("place", "mill"), ("thing", "coin")):
        w, _, _ = scene_writer(d, bk, kind, target)
        assert "<setting_entry" not in w, kind


def place_sink(d, *extra):
    sink = labelled_sink(d)
    for r in (pic("pmill001", "mill", "a0", kind="place"), pic("pcoin001", "coin", "a7", kind="thing")) + extra:
        sink.m["pictures"].append(r)
        sink.files[r["file"]] = r["id"].encode()
    return sink


def test_place_and_thing_pictures_are_no_reference_unless_the_flag_is_on():
    bk, d = place_doc()
    sink = place_sink(d)
    im = Image()
    il = art.Illustrator([art.Book(d, bk, sink)], Text(), im, "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    assert il.place_refs is False
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    assert [r["id"] for _, r in pr.references] == ["pmira001", "pstra001"]
    il.draw(job, pr)
    assert im.calls[-1]["references"] == [b"mira", b"stranger"] and "place where the scene happens" not in im.calls[-1]["note"]


def test_with_place_refs_the_place_then_the_things_take_the_slots_after_the_figures():
    bk, d = place_doc()
    sink = place_sink(d)
    im = Image()
    il = art.Illustrator([art.Book(d, bk, sink)], Text(), im, "ink", portraits="all", workers=1, log=lambda *_: None, pause=0,
                         place_refs=True)
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    assert pr.figures == [{"label": "A", "entity": "mira"}, {"label": "B", "entity": "stranger"}]      # figures unchanged
    assert [r["id"] for _, r in pr.references] == ["pmira001", "pstra001", "pmill001", "pcoin001"]
    assert il.references(job, pr.shows, figures=pr.figures) == pr.references
    rec = il.draw(job, pr)
    call = im.calls[-1]
    assert call["references"] == [b"mira", b"stranger", b"pmill001", b"pcoin001"]
    assert call["note"] == " ".join([
        "Reference picture 1 is figure A.", "Reference picture 2 is figure B.", art.FIGURE_NOTE,
        art.PLACE_NOTE.format(n=3), art.THING_NOTE.format(n=4, name="the silver coin"), art.CHEST_UP])
    assert "never its framing, viewpoint or composition" in call["note"] and "never its size or placement" in call["note"]
    assert call["note"] not in call["prompt"]
    assert rec["references"] == ["pmira001", "pstra001", "pmill001", "pcoin001"]
    assert rec["figures"] == [{"label": "A", "entity": "mira", "ref": "pmira001"},
                              {"label": "B", "entity": "stranger", "ref": "pstra001"}]
    # a person's text that drops a label: that figure's picture goes, the place and thing stay after the others
    im.calls.clear()
    il.draw(job, pr, text="A grey stone mill yard at dusk. Figure B, a hooded traveller, waits at the gate by the wheel.")
    assert im.calls[-1]["references"] == [b"stranger", b"pmill001", b"pcoin001"]
    assert im.calls[-1]["note"].startswith("Reference picture 1 is figure B. ") and art.PLACE_NOTE.format(n=2) in im.calls[-1]["note"]


def test_the_place_and_things_only_fill_slots_the_figures_leave(monkeypatch):
    bk, d = place_doc()
    il = art.Illustrator([art.Book(d, bk, place_sink(d))], Text(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0, place_refs=True)
    monkeypatch.setattr(art, "MAX_REFERENCES", 3)
    pr = il.make_prompt(il.job("event", "e6", at="a11"))
    assert [r["id"] for _, r in pr.references] == ["pmira001", "pstra001", "pmill001"]          # the place before the thing
    monkeypatch.setattr(art, "MAX_REFERENCES", 2)
    pr = il.make_prompt(il.job("event", "e6", at="a11"))
    assert [r["id"] for _, r in pr.references] == ["pmira001", "pstra001"]                       # no room: figures only


def test_a_place_reference_is_never_past_the_anchor_and_one_in_another_style_keeps_only_what_it_should():
    bk, d = place_doc()
    sink = labelled_sink(d)
    sink.m["pictures"].append(pic("pmill009", "mill", "a10", kind="place"))     # drawn from after the moment
    sink.files["pmill009.png"] = b"late mill"
    digital = likeness_set(d, pic("pmilldig", "mill", "a0", kind="place"))
    im = Image()
    il = art.Illustrator([art.Book(d, bk, sink, likeness={"digital-art": digital})], Text(), im, "ink", portraits="all",
                         workers=1, log=lambda *_: None, pause=0, place_refs=True)
    job = il.job("event", "e6", at="a11")
    pr = il.make_prompt(job)
    assert [r["id"] for _, r in pr.references] == ["pmira001", "pstra001", "pmilldig"]
    il.draw(job, pr)
    note = im.calls[-1]["note"]
    assert art.PLACE_NOTE.format(n=3) + art.OTHER_STYLE in note and "likeness of the figure" not in note
    # a place the reader has not reached is never a reference: its entity is not in the view
    bk, d = place_doc()
    for e in d["entities"]:
        if e["id"] == "mill":
            e["from"] = e["names"][0]["from"] = "a10"
            e["facets"] = [portrait("mill", "a10", MILL_THEN, 1)]
    il = art.Illustrator([art.Book(d, bk, place_sink(d))], Text(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0, place_refs=True)
    pr = il.make_prompt(il.job("event", "e6", at="a11"))
    assert "pmill001" not in [r["id"] for _, r in pr.references]


def test_a_thing_the_moments_summary_names_is_in_the_scene_and_one_not_reached_is_not():
    bk, d = place_doc()
    d["events"][5]["entities"].remove("coin")
    d["events"][5]["summary"] = "Mira finds the silver coin the stranger left behind."
    w, pr, _ = scene_writer(d, bk)
    assert '<entry name="the silver coin" type="thing">' in w and "coin" in pr.shows and "crowned wolf" in w
    d["events"][5]["summary"] = "Mira finds the silvery coins the stranger left behind."        # as written, or no match
    w, _, _ = scene_writer(d, bk)
    assert 'name="the silver coin"' not in w
    d["events"][5]["summary"] = "Mira finds a silver coin-shaped stone the stranger left behind."   # a compound is no coin
    for e in d["entities"]:
        if e["id"] == "coin":
            e["names"][0]["value"] = "coin"
    w, _, _ = scene_writer(d, bk)
    assert 'name="coin"' not in w and "crowned wolf" not in w
    d["events"][5]["summary"] = "Mira finds the coin the stranger left behind."                  # the whole word is
    w, _, _ = scene_writer(d, bk)
    assert '<entry name="coin" type="thing">' in w
    bk, d = place_doc()
    d["events"][5]["entities"].remove("coin")
    d["events"][4]["summary"] = "Gabriel rides in with the silver coin and news of a storm."     # e5 at a8: coin is from a7
    for e in d["entities"]:
        if e["id"] == "coin":
            e["from"] = e["names"][0]["from"] = "a9"
            for f in e["facets"]:
                f["from"] = f["evidence"][0] = "a9"
    w, _, _ = scene_writer(d, bk, target="e5", at="a11")
    assert '<entry name="the silver coin"' not in w and "crowned wolf" not in w          # not reached at a8


# ---- one model per responsibility


class Named(Text):
    """A scripted text model with a provider, a name and a usage counter that costs `each` dollars a call."""

    def __init__(self, provider, name, each=0.001, **kw):
        super().__init__(**kw)
        self.provider, self.name, self.each = provider, name, each
        self.usage = llm.Usage()

    def complete(self, system, prompt, schema, prefix=None):
        self.usage.cost_usd += self.each
        return super().complete(system, prompt, schema, prefix)

    def choosing(self):
        return [p for p, s in zip(self.prompts, self.systems) if s not in WRITERS]


class NamedImage(Image):
    def __init__(self, provider, name, **kw):
        super().__init__(**kw)
        self.provider, self.name = provider, name

    def draw(self, prompt, *, shape="landscape", quality="standard", references=(), **kw):
        got = super().draw(prompt, shape=shape, quality=quality, references=references, **kw)
        return images.Drawn(got.data, got.mime, self.name, got.cost)


def test_each_responsibility_uses_its_own_model():
    scene, portrait, picker = Named("claude", "haiku"), Named("claude", "sonnet"), Named("gemini", "flash-lite")
    il, rep, sink, _, im = run(text_=scene, portrait_text=portrait, cheap_text=picker)
    assert pics(sink, "plate") and pics(sink, "portrait")
    # the scene writer writes scene prompts only, the portrait writer portrait prompts only, the picker only chooses
    assert scene.scenes() and not any(is_portrait(p) for p in scene.scenes()) and not scene.choosing()
    assert portrait.scenes() and all(is_portrait(p) for p in portrait.scenes()) and not portrait.choosing()
    assert not picker.scenes() and any("Which of them are a visible moment" in p for p in picker.choosing())
    assert any("notes a reader's companion keeps" in p for p in picker.choosing())
    for r in pics(sink, "plate"):
        assert r["writer"] == {"provider": "claude", "model": "haiku"}
    for r in pics(sink, "portrait"):
        assert r["writer"] == {"provider": "claude", "model": "sonnet"}
    # the text cost is split by responsibility
    assert rep.text_costs == {"scene prompts": pytest.approx(scene.usage.cost_usd),
                              "portrait prompts": pytest.approx(portrait.usage.cost_usd),
                              "choosing calls": pytest.approx(picker.usage.cost_usd)}
    assert rep.text_cost == pytest.approx(scene.usage.cost_usd + portrait.usage.cost_usd + picker.usage.cost_usd)
    assert "portrait prompts $" in rep.lines()[0]


def test_the_look_sentences_are_picked_by_the_picker():
    scene, picker = Named("claude", "haiku"), Named("gemini", "flash-lite")
    il = art.Illustrator([art.Book(base_doc(book(("One", CH1), ("Two", CH2))), book(("One", CH1), ("Two", CH2)), Mem())],
                         scene, Image(), "ink", portraits="all", cheap_text=picker, workers=1, log=lambda *_: None, pause=0)
    il.look_sentences(0, "mira", make=True)
    assert any("Which sentences tell how" in p for p in picker.prompts) and not scene.prompts


def test_one_model_for_every_text_responsibility_behaves_as_before():
    t = Named("claude", "haiku")
    il, rep, sink, _, _ = run(text_=t)
    assert any(is_portrait(p) for p in t.scenes()) and any(not is_portrait(p) for p in t.scenes()) and t.choosing()
    assert rep.text_costs == {"scene prompts and portrait prompts and choosing calls": pytest.approx(t.usage.cost_usd)}
    assert "(" not in rep.lines()[0].split("text $")[1]       # no split shown for one model
    assert all(r["writer"] == {"provider": "claude", "model": "haiku"} for r in pics(sink))


def test_a_model_with_no_name_writes_no_writer_on_the_record():
    il, rep, sink, _, _ = run()
    assert pics(sink) and not any("writer" in r for r in pics(sink))


def test_the_portrait_image_model_draws_portraits_only():
    scenes, portraits = NamedImage("gemini", "nano-banana"), NamedImage("openai", "gpt-image")
    il, rep, sink, _, _ = run(image=scenes, portrait_image=portraits)
    assert scenes.calls and portraits.calls
    assert all(c["shape"] == "portrait" for c in portraits.calls) and all(c["shape"] != "portrait" for c in scenes.calls)
    assert all((r["provider"], r["model"]) == ("openai", "gpt-image") for r in pics(sink, "portrait"))
    assert all((r["provider"], r["model"]) == ("gemini", "nano-banana") for r in pics(sink, "plate"))
    assert il.image_for("place")[0] is portraits and il.image_for("thing")[0] is portraits
    assert il.image_for("event")[0] is scenes


def test_a_refused_portrait_in_a_run_is_rewritten_without_the_frame_and_framed_again():
    quiet = ("A woman of middle height in a plain grey collar and a dark shawl at the shoulders, her hair pinned back, a calm "
             "and steady look on her face, against a plain pale wall lit by soft daylight from one side, quiet and still.")
    portrait = Named("claude", "sonnet", scene=lambda p: quiet if "quieter composition" in p else PORTRAIT_SCENE)
    im = Image(refuse=[lambda prompt, n: prompt.startswith(art.PORTRAIT_FRAME) and quiet not in prompt and n <= 1])
    il, rep, sink, _, _ = run(text_=Named("claude", "haiku"), portrait_text=portrait, image=im)
    asks = [p for p in portrait.prompts if "quieter composition" in p]
    assert asks and all(art.PORTRAIT_FRAME not in p and art.PORTRAIT_CROP not in p for p in asks)
    drawn = [c["prompt"] for c in im.calls if quiet in c["prompt"]]
    assert drawn and all(c.startswith(art.PORTRAIT_FRAME + " " + quiet + " " + art.PORTRAIT_CROP + " ") for c in drawn)


def test_a_scene_note_says_how_to_use_a_portrait_cropped_at_mid_chest():
    portrait = (0, {"id": "p1", "kind": "portrait"})
    place = (0, {"id": "p2", "kind": "place", "target": "mill"})
    note = art.Illustrator.reference_note("event", [portrait], ["A"])
    assert note == "Reference picture 1 is figure A. " + art.FIGURE_NOTE + " " + art.CHEST_UP
    for words in ("only the head and upper chest", "arms, hands, dress below the chest, what they carry",
                  "natural proportions for the whole body"):
        assert words in art.CHEST_UP
    # an older prompt with no labels gets it too, after any likeness words
    assert art.Illustrator.reference_note("plate", [portrait], None) == art.CHEST_UP
    other = (0, {"id": "p3", "kind": "portrait", "set": "anime"})
    assert art.Illustrator.reference_note("plate", [other], None) == art.Illustrator.likeness_note([other]) + " " + art.CHEST_UP
    # no portrait among the references (a place in a spare slot, or an older record with no kind): no such words
    assert art.CHEST_UP not in art.Illustrator.reference_note("event", [place], [None])
    assert art.Illustrator.reference_note("event", [(0, {"id": "p4"})], None) == ""
    # a portrait's own reference keeps SAME_FIGURE, which leaves the framing to the prompt
    assert art.Illustrator.reference_note("portrait", [portrait]) == art.SAME_FIGURE
    assert "framing from the prompt, not from the reference picture" in art.SAME_FIGURE


def test_a_refused_portrait_is_rewritten_by_the_portrait_writer():
    quiet = ("A woman of middle height in a plain grey dress and a dark shawl, her hair pinned back, a calm and steady look "
             "on her face, against a plain pale wall lit by soft daylight from one side, quiet and still.")
    scene = Named("claude", "haiku")
    portrait = Named("claude", "sonnet", scene=lambda p: quiet if "quieter composition" in p else PORTRAIT_SCENE)
    im = Image(refuse=[lambda prompt, n: prompt.startswith(art.PORTRAIT_FRAME) and quiet not in prompt and n <= 1])
    il, rep, sink, _, _ = run(text_=scene, portrait_text=portrait, image=im)
    assert rep.softened == 1
    assert any("quieter composition" in p for p in portrait.prompts)
    assert not any("quieter composition" in p for p in scene.prompts)


def test_the_cheaper_image_model_bounds_the_room_left(monkeypatch):
    prices = {("gemini", "dear"): 1.0, ("gemini", "cheap"): 0.1}
    monkeypatch.setattr(llm, "image_price", lambda p, m, q="standard", s=None: prices.get((p, m)))
    il = art.Illustrator([art.Book(base_doc(book(("One", CH1), ("Two", CH2))), book(("One", CH1), ("Two", CH2)), Mem())],
                         Text(), NamedImage("gemini", "dear"), "ink", portrait_image=NamedImage("gemini", "cheap"), cap=1.0,
                         log=lambda *_: None, pause=0)
    assert il._budget() == 10


def test_illustrate_command_takes_a_model_per_responsibility(tmp_path, monkeypatch):
    from lorekeeper import cli
    bk = book(("One", CH1), ("Two", CH2))
    i1 = _cli_index(tmp_path, base_doc(bk), bk, "mill.lore.json")
    made, drew = {}, []

    def make_model(args, model=None, provider=None):
        return made.setdefault((provider, model), Named(provider, model))

    def make_image(provider, model=None, **k):
        drew.append((provider, model))
        return NamedImage(provider, model or llm.image_spec(provider)["default"])
    monkeypatch.setattr(cli, "_make_model", make_model)
    monkeypatch.setattr(llm, "make_image", make_image)
    monkeypatch.setattr(epub, "read", lambda p: bk)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen = []
    real = art.Illustrator

    def spy(*a, **k):
        il = real(*a, **k)
        seen.append(il)
        return il
    monkeypatch.setattr(art, "Illustrator", spy)
    base = ["illustrate", str(i1), "--epub", "1.epub", "--out", str(tmp_path / "o"), "--limit", "0", "--quality", "standard"]
    assert cli.main(base) == 0                              # defaults: cheap scene writer and picker, strong portrait writer
    il = seen[-1]
    cheap, strong = llm.PROVIDERS["gemini"]["cheap"], llm.PROVIDERS["gemini"]["strong"]
    assert (il.text.name, il.portrait_text.name, il.cheap.name) == (cheap, strong, cheap) and il.cheap is il.text
    assert il.portrait_image is il.image
    assert cli.main(base + ["--writer", "openai:gpt-x", "--portrait-writer", "gemini:g-pro", "--picker", "gemini",
                            "--portrait-model", "openai:" + llm.image_spec("openai")["default"]]) == 0
    il = seen[-1]
    assert (il.text.provider, il.text.name) == ("openai", "gpt-x")
    assert (il.portrait_text.provider, il.portrait_text.name) == ("gemini", "g-pro")
    assert (il.cheap.provider, il.cheap.name) == ("gemini", cheap)
    assert il.portrait_provider == "openai" and il.provider == "gemini"
    # --text-model alone keeps one model for the scene prompts and the choosing calls, as before; portraits go strong
    assert cli.main(base + ["--text-provider", "gemini", "--text-model", "gemini-x"]) == 0
    il = seen[-1]
    assert il.text.name == "gemini-x" and il.cheap is il.text and il.portrait_text.name == strong
    with pytest.raises(SystemExit, match="no provider"):
        cli.main(base + ["--writer", "nobody:x"])
    with pytest.raises(SystemExit, match="Claude cannot draw"):
        cli.main(base + ["--portrait-model", "claude:x"])


# ---- style previews


def test_preview_prompt_is_the_parts_a_real_prompt_has():
    for slug, sentence in art.STYLES.items():
        p = art.preview_prompt(slug)
        assert p == art.Illustrator._assemble(art.PREVIEW_SCENE, style=sentence)
        assert p == f"{art.PREVIEW_SCENE} {sentence} {art.NO_TEXT}"
    own = "  Pastel   crayon on brown paper. "
    assert art.preview_prompt(own) == f"{art.PREVIEW_SCENE} Pastel crayon on brown paper. {art.NO_TEXT}"


def test_preview_scene_has_no_style_words_and_no_names():
    # a real prompt names nobody and no illustrator; the only capitals are sentence openings
    words = art.PREVIEW_SCENE.split()
    for i, w in enumerate(words):
        if w[:1].isupper():
            assert i == 0 or words[i - 1].endswith((".", ";")), w
    scene = art.PREVIEW_SCENE.lower()
    for word in ("alice", "hatter", "tenniel", "carroll", "disney", "wonderland"):
        assert word not in scene, word
    for word in ("painting", "drawing", "illustration", "ink", "watercolour", "anime", "comic", "style", "text"):
        assert not re.search(rf"\b{word}\b", scene), word
    assert 30 <= len(art.PREVIEW_SCENE.split()) <= art.SCENE_WORDS[1]
    assert art.PREVIEW_SHAPE in images.SHAPES


def test_builtin_previews_ship_for_every_style():
    from pathlib import Path
    root = Path(art.__file__).parent / "web" / "static" / "styles"
    if not root.is_dir():
        pytest.skip("previews not drawn yet")
    total = 0
    for slug in art.STYLES:
        f = root / f"{slug}.webp"
        assert f.is_file(), slug
        data = f.read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", slug
        total += len(data)
    assert total < 1.5 * 1024 * 1024


# ---- a scene figure with no picture at or before the scene takes its earliest available one

def test_a_runs_early_scene_takes_the_figures_earliest_portrait_drawn_after_it():
    il, rep, sink, t, im = run(level="all")
    mira = next(p for p in pics(sink, "portrait") if p["target"] == "mira")
    assert ordinal(il.books[0].doc)[mira["from"]] > ordinal(il.books[0].doc)["a0"]
    e1 = next(p for p in pics(sink, "event") if p["target"] == "e1")     # at a0, before her only portrait (a4)
    assert mira["id"] in e1["references"] and {"label": "A", "entity": "mira", "ref": mira["id"]} in e1["figures"]
    assert il.latest_picture("mira", (0, 0)) is None                    # the at-or-before rule itself is unchanged


def test_a_look_change_recorded_between_the_scene_and_the_later_portrait_keeps_it_out():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].insert(0, {"id": "bk.mira.f0", "from": "a2", "basis": "stated", "evidence": ["a2"],
                                          "text": "Walks with a limp since the fall."})
    limp = lambda t: "limp" if "limp" in t else ""     # noqa: E731
    il, rep, sink, t, im = run(d, bk, level="all", text_=Text(change=limp))
    assert sink.m["looks"]["mira"]["items"]["bk.mira.f0"]["change"]
    e1 = next(p for p in pics(sink, "event") if p["target"] == "e1")     # a0; the limp at a2; her first portrait at a4
    assert not any(r["entity"] == "mira" and r["ref"] for r in e1["figures"] or []) and not e1["references"]
    # with no look index cached the later portrait is allowed
    sink.m["looks"] = {}
    il2 = art.Illustrator([art.Book(d, bk, sink)], Text(), Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    pr = il2.make_prompt(il2.job("event", "e1", at="a11"))
    assert [r["target"] for _, r in pr.references] == ["mira"]


def test_the_later_portrait_must_be_within_the_runs_until():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    sink = Mem()
    sink.m = {**art.blank(d["editions"][0]["textHash"], "ink"), "pictures": [pic("pmira001", "mira", "a4")]}
    sink.files["pmira001.png"] = b"mira"
    il = art.Illustrator([art.Book(d, bk, sink)], Text(), Image(), "ink", portraits="all", workers=1, log=lambda *_: None,
                         pause=0, until=(0, "a3"))
    job = art.Job("event", "e1", 0, "a0", event=il.books[0].events["e1"], chapter=il.books[0].chapter_of("a0"))
    assert il.references(job, ["mira"]) == []                             # a4 is past the run's until
    il = art.Illustrator([art.Book(d, bk, sink)], Text(), Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    assert [r["id"] for _, r in il.references(job, ["mira"])] == ["pmira001"]


def test_a_stored_later_reference_is_sent_again_only_within_the_readers_frontier():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    sink = Mem()
    sink.m = {**art.blank(d["editions"][0]["textHash"], "ink"),
              "pictures": [pic("pmira001", "mira", "a4"), pic("poren001", "oren", "a0")]}
    il = art.Illustrator([art.Book(d, bk, sink)], Text(), Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    mira, oren = sink.m["pictures"]
    at = lambda p: il.job("event", "e2", at=p)       # noqa: E731  (e2 shows from a1)
    assert il.reference_reached(at("a11"), 0, mira) and il.reference_reached(at("a4"), 0, mira)
    assert not il.reference_reached(at("a3"), 0, mira)                  # her portrait (a4) not reached yet
    assert il.reference_reached(at("a1"), 0, oren)                      # at or before the scene: always
    assert il.reference_reached(il.job("portrait", "mira", at="a11"), 0, mira)
    first = next(it["id"] for it in il.items(0, "mira") if it["from"] == "a4")   # her profile, flagged as a look change
    il.man[0]["looks"] = {"mira": {"key": il._items_key(il.items(0, "mira")), "items": {first: {"look": True, "change": True}}}}
    assert not il.reference_reached(at("a11"), 0, mira)                 # a recorded look change between: never


# ---- an on-demand entity picture shows from the first point its look is known


LOOK_CALL = "notes a reader's companion keeps on some entries"


def first_look_doc(*facets):
    """Millbrook with Mira's look told at a4 (her profile) and the given facets of hers after it."""
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"] += [{"id": f"bk.mira.f{n}", "from": at, "basis": "stated", "evidence": [at], "text": txt}
                                   for n, (at, txt) in enumerate(facets, 1)]
    return bk, d


class LookDown(Text):
    """A scripted model whose look index call fails (every time)."""

    def complete(self, system, prompt, schema, prefix=None):
        if LOOK_CALL in prompt:
            self.prompts.append(prompt)
            raise RuntimeError("the cheap model is down")
        return super().complete(system, prompt, schema, prefix)


def test_an_on_demand_portrait_shows_from_its_last_look_item_not_its_latest_detail():
    bk, d = first_look_doc(("a7", "Promised to the rider from the hills."))
    il, t = fresh(d, bk)
    job = il.job("portrait", "mira", at="a11")
    assert job.anchor == "a4" and job.reach == (0, 11)             # her look is told at a4; a7 says nothing about it
    pr = il.make_prompt(job)
    [w] = writer_calls(t)
    assert pr.anchor == "a4" and "Promised" not in w and all(il.books[0].ord_of[x] <= 4 for x in pr.sources)
    assert il.job("portrait", "mira", at="a5").anchor == "a4"          # the same anchor from anywhere after it
    assert sum(LOOK_CALL in p for p in t.prompts) == 1                    # classified once, then read from the set


def test_a_look_change_between_moves_the_anchor_to_the_change():
    bk, d = first_look_doc(("a6", "Her cheek was scarred by the fire at the mill."), ("a9", "Promised to the rider from the hills."))
    il, t = fresh(d, bk)
    assert il.job("portrait", "mira", at="a11").anchor == "a6"
    assert il.job("portrait", "mira", at="a5").anchor == "a4"           # before the change: her first look


def test_with_no_look_item_reached_the_picture_shows_from_the_first_profile_else_the_first_appearance():
    bk, d = first_look_doc()
    d["entities"].append(ent("zeph", "Zephyr", frm="a8", facets=[
        {"id": "bk.zeph.f1", "from": "a9", "basis": "stated", "evidence": ["a9"], "text": "A storm the riders fear."}]))
    d["entities"].append(ent("ida", "Ida", frm="a1", facets=[
        {"id": "bk.ida.f1", "from": "a2", "basis": "stated", "evidence": ["a2"], "text": "Keeps the village inn."},
        portrait("ida", "a5", "The innkeeper of the village, who knows everyone."),
        portrait("ida", "a9", "The innkeeper of the village, a widow.", n=2)]))
    il, t = fresh(d, bk)
    assert il.job("portrait", "zeph", at="a11").anchor == "a8"         # no profile: its first appearance
    assert il.job("portrait", "ida", at="a11").anchor == "a5"          # who she is: her first profile
    assert il.job("portrait", "ida", at="a4").anchor == "a1"           # before it: her first appearance
    # Mira's look comes at a4: drawn at a3 nothing is flagged yet and no profile reached
    assert il.job("portrait", "mira", at="a3").anchor == "a0"


def test_the_look_index_on_a_miss_is_one_picker_call_and_a_failure_keeps_the_old_anchor():
    bk, d = first_look_doc(("a7", "Promised to the rider from the hills."))
    cheap = Text()
    il, t = fresh(d, bk, cheap_text=cheap)
    assert il.job("portrait", "mira", at="a11").anchor == "a4"
    assert [LOOK_CALL in p for p in cheap.prompts] == [True] and not t.prompts       # on the picker, never the writer
    il, t = fresh(d, bk, cheap_text=LookDown())
    assert il.job("portrait", "mira", at="a11").anchor == "a7"         # later than needed, never unsafe
    assert il.man[0]["looks"] == {}                                    # nothing cached: the next request tries again
    il, t = fresh(d, bk)
    assert il.job("portrait", "mira", at="a11", looks="cached").anchor == "a7" and not t.prompts
    assert il.job("portrait", "mira", at="a11", looks="off").anchor == "a7" and not t.prompts
    with pytest.raises(ValueError):
        il.job("portrait", "mira", at="a11", looks="maybe")


def test_the_look_index_of_another_style_set_of_the_edition_is_read_with_no_call():
    bk, d = first_look_doc(("a7", "Promised to the rider from the hills."))
    oil = Mem()
    il, _ = fresh(d, bk, style="oil", books=[art.Book(d, bk, oil)])
    il.job("portrait", "mira", at="a11")                                # classified into the oil set
    il, t = fresh(d, bk, books=[art.Book(d, bk, Mem(), likeness={"oil": oil})])
    assert il.job("portrait", "mira", at="a11", looks="cached").anchor == "a4" and not t.prompts


def test_an_entity_after_the_position_is_still_not_visible_and_runs_keep_their_versions():
    bk, d = first_look_doc(("a7", "Promised to the rider from the hills."))
    il, t = fresh(d, bk)
    with pytest.raises(art.NotVisible):
        il.job("portrait", "stranger", at="a5")
    assert not t.prompts                                               # not visible: no call at all
    assert il.job("portrait", "mira").anchor == "a7"                   # no position: the old rule, the whole book


def test_the_scene_writer_asks_for_no_more_than_two_hands_can_do_and_figures_kept_apart():
    for words in ("a figure holds or uses at most two things in all, one in each hand",
                  "A thing they are not using is worn, stowed or set down, never held",
                  "Say which hand does what only when the moment turns on it",
                  "far enough apart to read as separate whole bodies, unless the moment is about their touching"):
        assert words in art.SCENE_WRITER, words
        assert words not in art.WRITER and words not in art.PORTRAIT_WRITER     # scenes only: places, things, portraits keep theirs
    assert art.SCENE_WRITER.index(art.SCENE_HANDS) < art.SCENE_WRITER.index("- No text, letters, titles")
    il, t = fresh()
    il.make_prompt(il.job("event", "e5", at="a11"))
    assert t.systems[-1] == art.SCENE_WRITER


# ---- drawing faults: the check, the fix and the automatic path --------------------------------

class Seeing(Text):
    """The scripted text model with eyes: the drawing-fault check answers `faults` (a list, or a function of the check's
    prompt and the picture's bytes), or fails (`fail`), or cannot see pictures (`blind`). Each check costs `cost`."""

    def __init__(self, faults=(), fail=False, blind=False, cost=0.001, **kw):
        super().__init__(**kw)
        self.faults, self.fail, self.blind, self.cost = faults, fail, blind, cost
        self.looked = []
        self.usage = llm.Usage()

    def complete(self, system, prompt, schema, prefix=None, images=()):
        if system != art.ANATOMY:
            return super().complete(system, prompt, schema, prefix)
        self.looked.append((prompt, [d for d, _ in images]))
        if self.blind:
            raise llm.NoVision("fake-picker cannot see pictures")
        self.usage.cost_usd += self.cost
        if self.fail:
            raise RuntimeError("the picker is down")
        got = self.faults(prompt, images[0][0]) if callable(self.faults) else self.faults
        return {"faults": list(got)}


HEAD = {"what": "the head is far too small for the body and robe", "where": "the woman at the gate"}


def fixes(im):
    return [c for c in im.calls if c["prompt"].startswith(art.FIX)]


def test_a_clean_picture_is_checked_and_kept_as_it_is():
    eye = Seeing()
    il, rep, sink, t, im = run(cheap_text=eye, check=True, portraits=False)
    assert rep.drawn == 2 and rep.checked == 2 and len(eye.looked) == 2 and not fixes(im)
    assert all("check" not in p and "fixes" not in p for p in pics(sink))
    assert [d for _, (d,) in eye.looked] == [sink.files[p["file"]] for p in pics(sink)]    # the picture itself was looked at
    assert rep.text_costs["choosing calls"] == pytest.approx(0.002)           # counted as choosing calls
    assert "checked: 2 pictures" in rep.lines()


def test_a_faulty_picture_is_fixed_once_and_never_checked_again():
    eye = Seeing(faults=[HEAD])
    il, rep, sink, t, im = run(cheap_text=eye, check=True, portraits=False)
    plates = pics(sink)
    assert rep.drawn == 2 and rep.fixed == 2 and len(eye.looked) == 2 and len(fixes(im)) == 2 and len(im.calls) == 4
    for p in plates:
        first = next(c for c in im.calls if c["prompt"] == p["prompt"])
        fix = next(c for c in fixes(im) if c["references"] == [eye.looked[plates.index(p)][1][0]])
        assert first["references"] == [] and fix["shape"] == p["shape"] and fix["quality"] == p["quality"]
        assert "- the woman at the gate: the head is far too small for the body and robe." in fix["prompt"]
        assert fix["prompt"].endswith(art.NO_TEXT) and fix["note"] == ""
        assert p["prompt"].startswith(SCENE) and p["check"] == {"faults": [HEAD], "fixed": True}   # the prompt is unchanged
        assert [f["faults"] for f in p["fixes"]] == [[HEAD]] and p["fixes"][0]["cost"] == 0.05
        first_bytes = eye.looked[plates.index(p)][1][0]
        assert p["cost"] == pytest.approx(0.10) and sink.files[p["file"]] != first_bytes      # the fixed picture is filed
        assert p["id"] == "p" + __import__("hashlib").sha256(sink.files[p["file"]]).hexdigest()[:8]
    assert sink.m["cost"] == pytest.approx(0.20) and rep.cost == pytest.approx(0.20)
    assert "  2 x fixed: drawing fault" in rep.lines()


def test_a_failed_or_blind_check_leaves_the_picture_as_it_is():
    eye = Seeing(faults=[HEAD], fail=True)
    il, rep, sink, t, im = run(cheap_text=eye, check=True, portraits=False)
    assert rep.drawn == 2 and not fixes(im) and rep.checked == 0 and rep.checks["not checked: check failed"] == 2
    assert all("check" not in p for p in pics(sink))
    logs = []
    eye = Seeing(faults=[HEAD], blind=True)
    bk = book(("One", CH1), ("Two", CH2))
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], Text(), Image(), "ink", portraits=False, workers=1,
                         log=logs.append, pause=0, cheap_text=eye, check=True)
    rep = il.run()
    assert rep.drawn == 2 and len(eye.looked) == 1 and il.no_vision          # asked once, then no more checks this run
    assert sum("not checked for drawing faults" in m for m in logs) == 1
    assert rep.checks["not checked: the model cannot see pictures"] == 1 and len(il.image.calls) == 2


def test_the_check_is_sent_the_prompt_so_a_maimed_figure_it_describes_is_no_fault():
    maimed = SCENE.replace("A woman with a basket on her arm", "A one-armed woman with a basket on her back")
    # the scripted picker only calls a missing arm a fault when the prompt does not describe it
    eye = Seeing(faults=lambda prompt, data: [] if "one-armed" in prompt else [{"what": "an arm is missing", "where": "the woman"}])
    il, rep, sink, t, im = run(text_=Text(scene=maimed), cheap_text=eye, check=True, portraits=False)
    assert rep.drawn == 2 and not fixes(im)
    prompt, _ = eye.looked[0]
    assert "<prompt>A one-armed woman" in prompt and "what it asks for is not a fault" in prompt
    assert "a figure the prompt says has lost an arm is drawn without it" in art.ANATOMY


def test_the_cap_reserves_the_check_and_skips_a_fix_it_has_no_room_for(monkeypatch):
    monkeypatch.setattr(art, "CHECK_FALLBACK", 0.002)
    monkeypatch.setattr(art, "FIX_RATE", 0.25)
    eye = Seeing(faults=[HEAD], cost=0.002)
    il, rep, sink, t, im = run(cheap_text=eye, check=True, portraits=False, cap=0.07)
    assert rep.drawn == 1 and not fixes(im) and rep.checks["skipped: fix, cap reached"] == 1
    assert pics(sink)[0]["check"] == {"faults": [HEAD], "fixed": False} and "fixes" not in pics(sink)[0]
    assert rep.cost + rep.text_cost <= 0.07 + 1e-9
    # room for a fix: one picture, its check and its fix (held at FIX_UNPRICED times the picture, the fake has no table
    # price), then the cap stops the run
    eye = Seeing(faults=[HEAD], cost=0.002)
    il, rep, sink, t, im = run(cheap_text=eye, check=True, portraits=False, cap=0.13)
    assert rep.drawn == 1 and rep.fixed == 1 and rep.cost + rep.text_cost <= 0.13 + 1e-9


def test_the_budget_counts_a_check_and_the_expected_fixes_per_picture(monkeypatch):
    monkeypatch.setattr(art, "CHECK_FALLBACK", 0.002)
    monkeypatch.setattr(llm, "image_price", lambda *a, **k: 0.05)
    bk = book(("One", CH1), ("Two", CH2))

    def budget(**kw):
        return art.Illustrator([art.Book(base_doc(bk), bk, Mem())], Text(), Image(), "ink", portraits=False, cap=0.30,
                               log=lambda *_: None, pause=0, cheap_text=Seeing(), **kw)._budget()
    assert budget() == 6                                   # 0.30 / 0.05
    monkeypatch.setattr(art, "FIX_RATE", 0.55)
    assert budget(check=True) == 3                         # 0.30 / (0.05 * 1.55 + 0.002)
    monkeypatch.setattr(art, "FIX_RATE", 0.0)
    assert budget(check=True) == 5                         # 0.30 / 0.052: the fixes are what take it to 3


class Billed(Image):
    """An image model with no table price whose fix (an edit sending the picture back) is billed `fix` per call."""

    def __init__(self, fix=0.2, cancel_fix=False, **kw):
        super().__init__(**kw)
        self.fix, self.cancel_fix = fix, cancel_fix

    def draw(self, prompt, **kw):
        if prompt.startswith(art.FIX) and self.cancel_fix:
            raise llm.Cancelled()
        d = super().draw(prompt, **kw)
        return images.Drawn(d.data, d.mime, d.model, self.fix) if prompt.startswith(art.FIX) else d


def test_a_token_billed_fix_is_held_above_the_picture_cost_and_its_bill_stops_the_run(monkeypatch):
    monkeypatch.setattr(art, "CHECK_FALLBACK", 0.002)
    monkeypatch.setattr(art, "FIX_RATE", 0.0)
    # held at 1.5 times the picture (0.075): 0.052 + 0.075 is over 0.12, so no fix starts (held at the picture's cost it would)
    il, rep, sink, t, im = run(cheap_text=Seeing(faults=[HEAD], cost=0.002), image=Billed(), check=True, portraits=False, cap=0.12)
    assert not fixes(im) and rep.checks["skipped: fix, cap reached"] >= 1
    # the fix fits what was held but is billed 0.2: it counts in full and the run stops there
    il, rep, sink, t, im = run(cheap_text=Seeing(faults=[HEAD], cost=0.002), image=Billed(), check=True, portraits=False, cap=0.20)
    assert rep.fixed == 1 and rep.drawn == 1 and rep.cost == pytest.approx(0.25) and len(im.calls) == 2
    assert rep.stopped == "cost cap $0.20 reached"


def test_a_stop_during_the_check_or_the_fix_still_files_the_drawn_picture():
    class Stopping(Seeing):
        def complete(self, system, prompt, schema, prefix=None, images=()):
            if system == art.ANATOMY:
                raise llm.Cancelled()
            return super().complete(system, prompt, schema, prefix, images)

    for eye, im in ((Stopping(), Image()), (Seeing(faults=[HEAD]), Billed(cancel_fix=True))):
        bk = book(("One", CH1), ("Two", CH2))
        sink = Mem()
        il = art.Illustrator([art.Book(base_doc(bk), bk, sink)], Text(), im, "ink", portraits=False, workers=1,
                             log=lambda *_: None, pause=0, cheap_text=eye, check=True)
        with pytest.raises(llm.Cancelled):
            il.draw(il.job("event", "e1", at="a11"))
        (rec,) = sink.m["pictures"]
        assert sink.files[rec["file"]] and rec["prompt"] == im.calls[0]["prompt"] and "check" not in rec
        assert il.report.drawn == 1 and il.reserved == 0


def test_the_pickers_faults_name_no_person_but_the_persons_own_are_kept_as_typed():
    named = {"what": "Mira's left hand has six fingers", "where": "Mira, the woman at the gate"}
    il, rep, sink, t, im = run(cheap_text=Seeing(faults=[named]), check=True, portraits=False)
    stored = pics(sink)[0]["check"]["faults"][0]
    assert "Mira" not in stored["what"] + stored["where"] and "figure" in stored["what"]
    assert all("Mira" not in c["prompt"] for c in fixes(im))
    new = il.fix(pics(sink)[0], [named])                          # the person's own list: their words
    assert "Mira's left hand has six fingers" in im.calls[-1]["prompt"] and new["fixes"][-1]["faults"] == [named]


def test_the_persons_own_text_is_checked_and_fixed_but_never_rewritten():
    eye = Seeing(faults=[HEAD])
    bk = book(("One", CH1), ("Two", CH2))
    im = Image()
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], Text(), im, "ink", portraits=False, workers=1,
                         log=lambda *_: None, pause=0, cheap_text=eye, check=True)
    mine = "My own words: a woman with a basket waits at a mill gate in morning light, a miller in the doorway."
    rec = il.draw(il.job("event", "e1", at="a11"), text=mine)
    assert rec["prompt"] == mine and im.calls[0]["prompt"] == mine and f"<prompt>{mine}</prompt>" in eye.looked[0][0]
    assert im.calls[1]["prompt"].startswith(art.FIX) and mine not in im.calls[1]["prompt"]
    assert rec["check"]["fixed"] is True and len(im.calls) == 2


def test_check_off_makes_no_check():
    eye = Seeing(faults=[HEAD])
    il, rep, sink, t, im = run(cheap_text=eye, portraits=False)
    assert not eye.looked and not fixes(im) and "checked:" not in " ".join(rep.lines())


def test_fix_on_demand_keeps_the_record_and_adds_to_its_fixes():
    eye = Seeing()
    il, rep, sink, t, im = run(cheap_text=eye, level="all")
    scene = next(p for p in pics(sink) if p["kind"] == "event" and p.get("figures"))
    found = il.check_picture(scene)
    assert found.checked and found.faults == [] and eye.looked[-1][1] == [sink.files[scene["file"]]]
    assert scene["prompt"] in eye.looked[-1][0]
    new = il.fix(scene, [HEAD, {"what": "  an extra   hand  ", "where": ""}], note="the cloak should stay red")
    call = im.calls[-1]
    assert call["references"] == [sink.files[scene["file"]]] and call["shape"] == scene["shape"]
    assert call["prompt"] == "\n".join([art.FIX, "- the woman at the gate: the head is far too small for the body and robe.",
                                        "- an extra hand.", "Also: the cloak should stay red.", art.NO_TEXT])
    assert new["id"] != scene["id"] and sink.files[new["file"]] and new["prompt"] == scene["prompt"]
    for k in ("figures", "references", "sources", "from", "kind", "target", "inherits", "notes"):
        assert new[k] == scene[k], k
    assert new["fixes"][-1]["faults"][1] == {"what": "an extra hand", "where": ""}
    assert new["fixes"][-1]["note"] == "the cloak should stay red" and new["cost"] == pytest.approx(scene["cost"] + 0.05)
    assert any(r["id"] == scene["id"] for r in sink.m["pictures"]) and sink.m["pictures"][-1]["id"] == new["id"]
    again = il.fix(new, [], note="the left eye open")                 # a second fix by hand adds to the list, newest last
    assert [f["note"] for f in again["fixes"]] == ["the cloak should stay red", "the left eye open"]
    with pytest.raises(ValueError):
        il.fix(scene, [{"what": " ", "where": "x"}], note="  ")
    im.refuse.append(lambda prompt, n: prompt.startswith(art.FIX))
    with pytest.raises(images.ImageRefused):
        il.fix(scene, [HEAD])


def test_fault_lists_are_cleaned_and_priced():
    raw = [{"what": "x" * 300, "where": "y" * 200}, {"what": "", "where": "z"}, "junk"] + [{"what": f"f{i}", "where": ""} for i in range(9)]
    got = art.clean_faults(raw)
    assert len(got) == art.MAX_FAULTS and len(got[0]["what"]) == art.FAULT_CHARS and len(got[0]["where"]) == art.WHERE_CHARS
    assert art.check_price("claude", "claude-haiku-5-5") == pytest.approx(
        (art.CHECK_TOKENS[0] * 0.10 + art.CHECK_TOKENS[1] * 0.50) / 1e6)
    assert art.check_price("custom", "mine") is None


def test_the_writers_ask_for_no_visible_writing_and_no_detail_too_small_once():
    for w in art.WRITERS if hasattr(art, "WRITERS") else (art.WRITER, art.SCENE_WRITER, art.PORTRAIT_WRITER):
        assert w.count("Never ask for writing, words, letters, numbers or runes to be visible") == 1
        assert w.count("say it is marked or inscribed, with no words shown") == 1
        assert w.count("Leave out details too small to show at the picture's framing.") == 1
    assert art.SCENE_WRITER.count("Two hands each") == 1


# ---- every figure of a scene is labelled, with or without a portrait (label-every-figure)

FIGS = ["mira", "oren", "stranger", "gabriel", "bram", "lio"]


def crowd(pictured=(), pictures_files=True, events=(), **text_kw):
    """The base book with two more people and an event at a10 naming six figures; `pictured` holds a portrait (at a4) each."""
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"] += [ent("bram", "Bram", facets=[portrait("bram", "a2", "A thin boy who sweeps the mill yard.")]),
                      ent("lio", "Lio", facets=[portrait("lio", "a2", "A stout carter with a red cap.")])]
    d["events"].append(event(7, 10, "Everyone gathers in the mill yard to see the stranger off.", list(FIGS)))
    d["events"] += list(events)
    sink = Mem()
    sink.m = {**art.blank(d["editions"][0]["textHash"], "ink"),
              "pictures": [pic(f"p{eid[:4]}0001", eid, "a4") for eid in pictured]}
    if pictures_files:
        for r in sink.m["pictures"]:
            sink.files[r["file"]] = b"bytes of " + r["id"].encode()
    t, im = Text(**text_kw), Image()
    il = art.Illustrator([art.Book(d, bk, sink)], t, im, "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    return il, t, im, sink


def entry_tags(w):
    return dict(re.findall(r'<entry name="([^"]*)" type="person"[^>]*? (figure="[A-F]" portrait="(?:yes|no)")>', w))


def test_three_figures_one_portrait_labels_all_three_the_pictured_one_first_and_sends_one_reference():
    il, t, im, sink = crowd(pictured=["stranger"], events=[event(8, 10, "Mira and Gabriel watch the stranger go.", ["mira", "gabriel", "stranger"])])
    job = il.job("event", "e8", at="a11")
    pr = il.make_prompt(job)
    tags = entry_tags(writer_calls(t)[-1])
    assert tags == {"the Stranger": 'figure="A" portrait="yes"', "Mira": 'figure="B" portrait="no"',
                    "Gabriel": 'figure="C" portrait="no"'}
    assert pr.figures == [{"label": "A", "entity": "stranger"}, {"label": "B", "entity": "mira", "portrait": False},
                          {"label": "C", "entity": "gabriel", "portrait": False}]
    assert [r["id"] for _, r in pr.references] == ["pstra0001"]
    rec = il.draw(job, pr)
    call = im.calls[-1]
    # a label with no picture: no note line and no reference; the record keeps it with ref None
    assert call["references"] == [b"bytes of pstra0001"]
    assert call["note"] == "Reference picture 1 is figure A. " + art.FIGURE_NOTE + " " + art.CHEST_UP
    assert rec["figures"] == [{"label": "A", "entity": "stranger", "ref": "pstra0001"},
                              {"label": "B", "entity": "mira", "ref": None}, {"label": "C", "entity": "gabriel", "ref": None}]
    assert rec["references"] == ["pstra0001"]


def test_six_figures_five_with_pictures_label_all_six_and_send_four_references():
    il, t, im, sink = crowd(pictured=["mira", "oren", "stranger", "gabriel", "bram"])
    job = il.job("event", "e7", at="a11")
    pr = il.make_prompt(job)
    tags = entry_tags(writer_calls(t)[-1])
    assert sorted(tags.values()) == [f'figure="{x}" portrait="{"yes" if x in "ABCD" else "no"}"' for x in "ABCDEF"]
    assert tags["Bram"] == 'figure="E" portrait="no"' and tags["Lio"] == 'figure="F" portrait="no"'   # the fifth picture: none sent
    assert [f["label"] for f in pr.figures] == list("ABCDEF") and len(pr.references) == art.MAX_REFERENCES == 4
    assert [r["target"] for _, r in pr.references] == FIGS[:4]
    rec = il.draw(job, pr)
    call = im.calls[-1]
    assert len(call["references"]) == 4 and call["note"].startswith(" ".join(f"Reference picture {n} is figure {x}." for n, x in
                                                                              enumerate("ABCD", 1)))
    assert "figure E" not in call["note"] and "figure F" not in call["note"]
    assert [(f["label"], bool(f["ref"])) for f in rec["figures"]] == [(x, x in "ABCD") for x in "ABCDEF"]
    assert len(art.LABELS) == art.MAX_ENTRIES and art.labels_in("figure E and figure F") == ["E", "F"]


def test_a_text_missing_a_shown_figures_label_is_rewritten_once_and_a_second_miss_is_kept_and_counted():
    names = ["Mira", "Oren", "the Stranger"]
    il, t, im, sink = crowd(pictured=["mira"], shows=names, events=[event(8, 10, "Mira, Oren and the stranger stand at the gate.", ["mira", "oren", "stranger"])])
    full = f"{ROOM} Figure A, a young woman, figure B, an old miller, and figure C, a hooded traveller, stand at the gate."
    short = f"{ROOM} Figure A, a young woman, and figure B, an old miller, stand at the gate with a hooded traveller."
    t.scene = lambda p: full if "It was rejected" in p else short
    pr = il.make_prompt(il.job("event", "e8", at="a11"))
    ws = writer_calls(t)
    assert len(ws) == 2 and "It was rejected: figure not called by its label: figure C." in ws[-1]
    assert pr.body == full and [f["label"] for f in pr.figures] == ["A", "B", "C"]
    assert il.report.dropped["rewritten: figure not called by its label"] == 1
    assert "kept: figure not called by its label" not in il.report.dropped
    # the rewrite misses it again: the rewrite is kept (it passes every check) and counted, never dropped
    t.scene = lambda p: short
    pr = il.make_prompt(il.job("event", "e8", at="a11"))
    assert pr.body == short and il.report.dropped["kept: figure not called by its label"] == 1
    assert il.report.dropped["rewritten: figure not called by its label"] == 2
    # a rewrite that loses a label the first text used: the first text stands
    t.scene = lambda p: (f"{ROOM} Figure C, a hooded traveller, stands at the gate with a young woman and an old miller."
                         if "It was rejected" in p else short)
    pr = il.make_prompt(il.job("event", "e8", at="a11"))
    assert pr.body == short and il.report.dropped["kept: figure not called by its label"] == 2
    assert "  2 x kept: figure not called by its label" in il.report.lines()
    assert "  3 x rewritten: figure not called by its label" in il.report.lines()


def test_a_figure_not_in_shows_is_never_asked_for():
    il, t, im, sink = crowd(pictured=["mira"], shows=["Mira"], events=[event(8, 10, "Mira and the stranger stand at the gate.", ["mira", "stranger"])])
    t.scene = lambda p: f"{ROOM} Figure A, a young woman, stands alone at the gate."
    pr = il.make_prompt(il.job("event", "e8", at="a11"))
    assert len(writer_calls(t)) == 1 and pr.figures == [{"label": "A", "entity": "mira"}]
    assert not il.report.dropped
    # no shows list, or one matching no one: nothing asked for either
    for shows in (None, ["Nobody Known"], []):
        il, t, im, sink = crowd(pictured=["mira"], shows=shows, events=[event(8, 10, "Mira and the stranger stand at the gate.", ["mira", "stranger"])])
        t.scene = lambda p: f"{ROOM} Figure A, a young woman, stands alone at the gate."
        il.make_prompt(il.job("event", "e8", at="a11"))
        assert len(writer_calls(t)) == 1 and not il.report.dropped


def test_a_same_prompt_redraw_a_restyle_and_a_soften_keep_every_label_e_and_f_included():
    six = (f"{ROOM} Figure A, figure B and figure C stand by the wheel; figure D waits at the door; figure E sweeps the yard and "
           "figure F leans on the cart.")
    calm = six.replace("leans on", "stands by")
    il, t, im, sink = crowd(pictured=["mira", "oren", "stranger", "gabriel", "bram"])
    t.scene = lambda p: calm if "image model refused" in p else six
    job = il.job("event", "e7", at="a11")
    pr = il.make_prompt(job)
    rec = il.draw(job, pr)
    # the same prompt drawn again, its figures read back from the record as the redraw route does
    figs = [{"label": f["label"], "entity": f["entity"]} for f in rec["figures"]]
    again = art.Prompt(text=rec["prompt"], scene=rec["prompt"], anchor=rec["from"], shape=rec["shape"],
                       references=[(0, r) for r in sink.m["pictures"] if r["id"] in rec["references"]],
                       version=art.NOTE_VERSION, body=rec["prompt"], figures=figs)
    rec2 = il.draw(job, again, text=rec["prompt"])
    assert [f["label"] for f in rec2["figures"]] == list("ABCDEF") and rec2["references"] == rec["references"]
    assert [f["ref"] is None for f in rec2["figures"]] == [False] * 4 + [True] * 2
    # a restyle: the labelled figures' pictures in label order, at most four, whatever shows says
    assert [r["target"] for _, r in il.references(job, [], None, figs)] == FIGS[:4]
    # a soften keeps E and F
    soft = il.soften(job, pr.text, "violence")
    assert [f["label"] for f in soft.figures] == list("ABCDEF") and soft.body == calm


def test_an_older_record_labelled_a_to_d_draws_as_before():
    il, t, im, by, sink = labelled(lambda p: ROOM)
    job = il.job("event", "e6", at="a11")
    text_ = f"{ROOM} Figure A, figure B, figure C and figure D stand at the gate."
    figs = [{"label": x, "entity": e} for x, e in zip("ABCD", ["mira", "oren", "stranger", "gabriel"])]
    old = art.Prompt(text=text_, scene=text_, anchor="a9", shape="landscape",
                     references=[(0, by[e]) for e in ["mira", "oren", "stranger", "gabriel"]], version=art.NOTE_VERSION,
                     body=text_, figures=figs)
    rec = il.draw(job, old, text=text_)
    call = im.calls[-1]
    assert call["references"] == [sink.files[by[e]["file"]] for e in ["mira", "oren", "stranger", "gabriel"]]
    assert call["note"] == (" ".join(f"Reference picture {n} is figure {x}." for n, x in enumerate("ABCD", 1)) + " "
                            + art.FIGURE_NOTE + " " + art.CHEST_UP)
    assert [(f["label"], f["ref"]) for f in rec["figures"]] == [(x, by[e]["id"]) for x, e in zip("ABCD", ["mira", "oren", "stranger", "gabriel"])]


def test_a_rewrite_naming_a_missing_label_besides_another_fault_may_not_lose_a_label_the_first_text_used():
    # the first text uses figure A, leaves out figure B (in shows) and names Mira; the one rewrite adds B and C but drops A
    names = ["Mira", "Oren", "the Stranger"]
    il, t, im, sink = crowd(pictured=["mira"], shows=names,
                            events=[event(8, 10, "Mira, Oren and the stranger stand at the gate.", ["mira", "oren", "stranger"])])
    first = f"{ROOM} Figure A, a young woman, waits by Mira's cart, and figure C, a hooded traveller, watches her."
    t.scene = lambda p: (f"{ROOM} Figure B, an old miller, and figure C, a hooded traveller, wait at the gate."
                         if "It was rejected" in p else first)
    pr = il.make_prompt(il.job("event", "e8", at="a11"))
    ws = writer_calls(t)
    assert len(ws) == 2 and "It was rejected: name in the text: Mira; and figure not called by its label: figure B." in ws[-1]
    # the rewrite is not accepted: the first text stands, its name replaced by a role, and the missing label is counted
    assert "Figure A, a young woman" in pr.body and "Mira" not in pr.body and [f["label"] for f in pr.figures] == ["A", "C"]
    assert il.report.dropped["fixed: name replaced by a role"] == 1
    assert il.report.dropped["kept: figure not called by its label"] == 1
    # a rewrite that keeps A and adds B is taken
    t.scene = lambda p: (f"{ROOM} Figure A, a young woman, figure B, an old miller, and figure C, a hooded traveller, wait."
                         if "It was rejected" in p else first)
    pr = il.make_prompt(il.job("event", "e8", at="a11"))
    assert [f["label"] for f in pr.figures] == ["A", "B", "C"] and il.report.dropped["kept: figure not called by its label"] == 1


def test_only_a_figure_marked_portrait_yes_gets_a_reference_even_when_a_label_before_it_is_left_out():
    il, t, im, sink = crowd(pictured=["mira", "oren", "stranger", "gabriel", "bram"])
    t.scene = lambda p: f"{ROOM} Figure A, figure B and figure C stand by the wheel while figure E sweeps the yard."
    job = il.job("event", "e7", at="a11")
    pr = il.make_prompt(job)
    # D is left out, so only three of the four portrait="yes" figures are in the text; E, told "no", gets no picture
    assert [f["label"] for f in pr.figures] == ["A", "B", "C", "E"] and pr.figures[-1] == {"label": "E", "entity": "bram",
                                                                                        "portrait": False}
    assert [r["target"] for _, r in pr.references] == ["mira", "oren", "stranger"]
    assert [r["target"] for _, r in il.references(job, [], None, pr.figures)] == ["mira", "oren", "stranger"]   # a restyle too
    rec = il.draw(job, pr)
    call = im.calls[-1]
    assert len(call["references"]) == 3 and "figure E" not in call["note"]
    assert [(f["label"], bool(f["ref"])) for f in rec["figures"]] == [("A", True), ("B", True), ("C", True), ("E", False)]
