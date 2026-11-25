"""Art sets (art.py) with a scripted text model and a scripted image model: what is drawn, from which anchor, with which
references; the checks, refusals, the cost cap, resume and series inheritance."""

import re
import struct

import pytest

from lorekeeper import art, epub, images, llm, text

WORLD = ("A quiet river valley in a pre-industrial farming country: stone mills, thatched cottages, dirt tracks and wooden carts. "
         "People wear plain wool and linen in browns and greys. The light is soft and low, the land green and rolling, the mood calm.")
SCENE = ("A woman with a basket on her arm stands at a wooden gate beside a grey stone mill, while a miller watches her from the "
         "doorway with his arms folded. Soft morning light falls across the packed earth of the yard and a path leads off toward low "
         "green hills in the distance, the river bright beyond the mill wheel.")
# Appearance notes as the fake writer returns them: a role and look details, each quoted from the entity's profile below.
NOTE_PARTS = {"Mira": ("a young woman", ["slight", "of about twenty", "dark braided hair", "a brown shawl", "a plain grey dress"]),
              "Oren": ("a miller", ["old", "broad", "white hair", "flour on his sleeves", "a leather apron"]),
              "Gabriel": ("a rider", ["tall", "fair", "short hair", "a blue coat", "tall riding boots"]),
              "Stranger": ("a traveller", ["hooded", "a long black cloak", "of middle height", "face is never seen"])}
NOTES = {k: art.compose_note(r, ds) for k, (r, ds) in NOTE_PARTS.items()}
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
        import json
        self.m = json.loads(json.dumps(manifest))

    def put(self, file, data):
        self.files[file] = data

    def read(self, file):
        return self.files.get(file)


class Text:
    """A scripted text model that answers by the kind of prompt it is given and keeps every prompt."""

    def __init__(self, scene=SCENE, world=WORLD, screen=None, notes=None, shows=None):
        self.scene, self.world, self.notes, self.shows = scene, world, dict(NOTE_PARTS, **(notes or {})), shows
        self.screen = screen
        self.prompts = []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
        if "Which of them are a visible moment" in prompt:
            ns = [int(n) for n in re.findall(r"^(\d+)\. ", prompt, re.M)]
            keep = self.screen(ns, prompt) if self.screen else ns
            return {"moments": keep}
        if "This is the opening of a later book" in prompt:
            return {"text": ""}
        if "visual world note" in prompt:
            return {"text": self.world}
        if "fixed appearance note" in prompt:
            who = re.search(r"appearance note for (.+?) \(", prompt).group(1).replace("the ", "").capitalize()
            if "Its fixed note until now" in prompt:      # an amended note: what changed, quoted from the change
                ds = [m.group(0) for m in re.finditer(r"\b(older|scar\w*)( \w+){0,3}", prompt.split("The text now shows")[1][:200])]
                return {"role": "young woman", "details": [{"text": d, "quote": d} for d in dict.fromkeys(ds)]
                        + [{"text": "a livid scar across one cheek", "quote": "a livid scar"}]}
            return note_reply(self.notes.get(who, None), prompt)
        if "Write the picture prompt" in prompt or "Write it again" in prompt or "quieter composition" in prompt:
            scene = PORTRAIT_SCENE if "This is a portrait" in prompt and self.scene is SCENE else self.scene
            out = {"text": scene(prompt) if callable(scene) else scene}
            if self.shows is not None and "Write the picture prompt" in prompt:
                out["shows"] = self.shows
            return out
        raise AssertionError("unexpected prompt: " + prompt[:80])

    def scenes(self):
        return [p for p in self.prompts if "Write the picture prompt" in p]


def note_reply(note, prompt):
    """The fake writer's note: NOTE_PARTS' role and details, each quoted with a word either side as the prompt has it (a detail
    the prompt lacks keeps its own words as the quote, which the check then drops); a plain string is one detail; "" none."""
    if note is None:          # someone the table does not know (a web test's book): a look word of what it was given
        given = prompt.split("What the companion has noted:", 1)[-1]
        m = re.search(r"[A-Za-z]+ (?:" + art.BODY_LOOK.pattern[3:-3] + r") [A-Za-z]+", given, re.IGNORECASE)
        return {"role": "a figure", "details": [{"text": m.group(0), "quote": m.group(0)}] if m else []}
    if not note:
        return {"role": "", "details": []}
    role, ds = note if isinstance(note, tuple) else ("", [note])
    out = []
    for d in ds:
        m = re.search(r"(\w+[ ,]+)?" + re.escape(d) + r"([ ,]+\w+)?", prompt)
        out.append({"text": d, "quote": m.group(0) if m else d})
    return {"role": role, "details": out}


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
def short_world(monkeypatch):
    monkeypatch.setattr(art, "WORLD_MIN_WORDS", 1)    # the world note ends with chapter one: nothing is held back
    monkeypatch.setattr(art, "MIN_GAP", 2)


def run(d=None, bk=None, sink=None, text_=None, image=None, **kw):
    bk = bk or book(("One", CH1), ("Two", CH2))
    d = d or base_doc(bk)
    sink = sink or Mem()
    text_, image = text_ or Text(), image or Image()
    kw.setdefault("workers", 1)
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
    assert sink.m["world"]["text"] == WORLD and sink.m["style"] == "ink"
    assert all(p["id"].startswith("p") and p["file"] in sink.files and p["width"] == 1024 for p in sink.m["pictures"])
    assert [p["shape"] for p in plates] == ["landscape"] * 2 and {p["shape"] for p in pics(sink, "portrait")} == {"portrait"}


def test_the_main_moment_is_the_turning_point_and_all_level_adds_the_other_drawable_ones():
    il, rep, sink, t, im = run()
    one = next(p for p in pics(sink, "plate") if p["target"] == "One")
    assert "main moment of a chapter: Oren the miller comes out" in next(s for s in t.scenes() if "chapter" in s) and one["from"] == "a5"
    _, _, sink2, _, _ = run(level="all")
    events = [p["target"] for p in pics(sink2, "event")]
    assert events == ["e1", "e5", "e6"] or set(events) == {"e1", "e5", "e6"}   # e2 and e4 are the plates; e3 is minor
    assert sink2.m["screened"]["One"]["events"] and "e3" not in {x for ms in sink2.m["screened"].values() for x in ms["events"]}


def test_a_chapter_with_nothing_drawable_gets_its_recap_as_a_plate():
    il, rep, sink, t, im = run(text_=Text(screen=lambda ns, p: []))
    assert [p["target"] for p in pics(sink, "plate")] == ["One", "Two"]
    assert "main moment of a chapter, as its recap tells it" in t.scenes()[0] or any("recap tells it" in s for s in t.scenes())


def test_prompt_is_the_scene_then_notes_world_and_style_added_by_code():
    il, rep, sink, t, im = run()
    p = next(r for r in pics(sink, "event") or pics(sink, "plate") if r["kind"] == "plate" and r["target"] == "One")
    assert p["prompt"].startswith(SCENE) and f"World: {WORLD}" in p["prompt"]
    assert art.STYLES["ink"] in p["prompt"] and art.NO_TEXT in p["prompt"]
    assert NOTES["Oren"].rstrip(".") in p["prompt"] and NOTES["Mira"].rstrip(".") in p["prompt"]
    assert "Mira" not in p["prompt"].split("Figures:")[0]


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


def test_pictures_of_the_first_chapters_are_held_to_the_world_notes_anchor(monkeypatch):
    monkeypatch.setattr(art, "WORLD_MIN_WORDS", 100)    # chapter one is shorter: the note takes chapter two too
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il, rep, sink, t, im = run(d, bk)
    assert sink.m["world"]["from"] == "a11" and rep.held >= 2
    o = ordinal(d)
    assert all(o[p["from"]] >= o["a11"] for p in sink.m["pictures"])
    assert all(o[a] <= o[p["from"]] for p in sink.m["pictures"] for a in p["sources"])


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
    il, rep, sink, t, im = run(level="all")
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


def test_a_look_change_draws_a_new_portrait_with_the_old_one_as_reference_and_an_amended_note():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    mira = d["entities"][0]
    mira["facets"].append(portrait("mira", "a10", "A scarred young woman, older than she was, who keeps a stranger's coin.", 2))
    il, rep, sink, t, im = run(d, bk)
    ms = sorted(pics(sink, "portrait"), key=lambda p: ordinal(d)[p["from"]])
    ms = [p for p in ms if p["target"] == "mira"]
    assert [p["from"] for p in ms] == ["a5", "a10"]      # held to the end of the world note's chapter and ms[1]["references"] == [ms[0]["id"]]
    assert ms[1]["appearance"].startswith("The same") and ms[0]["appearance"] == NOTES["Mira"]
    notes = sink.m["appearance"]["mira"]
    assert [n["from"] for n in notes] == ["a5", "a10"] and notes[1]["text"] == ms[1]["appearance"]
    assert ms[0]["appearance"] != ms[1]["appearance"]


def test_a_death_in_a_checked_fact_is_a_look_change_but_hedged_wording_is_not():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    oren = d["entities"][1]
    oren["facets"].append({"id": "bk.oren.f1", "from": "a10", "basis": "stated", "evidence": ["a10"], "text": "Oren is found dead on the mill floor."})
    il, rep, sink, t, im = run(d, bk)
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "oren"] == ["a5", "a10"]
    oren["facets"][-1]["text"] = "Oren is feared dead by the villagers."
    _, _, sink2, _, _ = run(d, bk)
    assert [p["from"] for p in pics(sink2, "portrait") if p["target"] == "oren"] == ["a5"]


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


def test_a_later_book_reuses_notes_and_pictures_and_redraws_only_on_a_look_change():
    bk1, d1, bk2, d2 = series_docs()
    s1, s2 = Mem(), Mem()
    t, im = Text(), Image()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], t, im, "ink", workers=1, log=lambda *_: None, pause=0)
    il.run()
    one = {p["target"]: p for p in pics(s1, "portrait")}
    two = pics(s2, "portrait")
    assert [p["target"] for p in two] == ["mira"]                      # Oren is not drawn again
    assert two[0]["references"] == [one["mira"]["id"]] and two[0]["appearance"].startswith("The same")
    assert "oren" not in s2.m["appearance"] or all(n["text"] == NOTES["Oren"] for n in s2.m["appearance"]["oren"])
    plate = pics(s2, "plate")[0]
    assert plate["from"] == "b2.a5" and set(plate["references"]) <= {two[0]["id"], one["oren"]["id"], one["mira"]["id"]}
    oren = next(p for p in pics(s2) if p["target"] != "mira" and p["kind"] == "plate")
    # Oren's note for book two is book one's, pasted verbatim
    assert NOTES["Oren"].rstrip(".") in oren["prompt"]
    assert one["oren"]["id"] in oren["references"]
    assert s2.m["world"]["text"] == WORLD and s2.m["world"]["inherited"] and not any("visual world note" in p and "series so far" not in p
                                                                                    for p in t.prompts[-12:])


def test_a_later_book_may_append_one_sentence_to_the_world_but_never_an_edited_note():
    bk1, d1, bk2, d2 = series_docs()
    s1, s2 = Mem(), Mem()
    class More(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "This is the opening of a later book" in prompt:
                self.prompts.append(prompt)
                return {"text": "Machines and iron rails cross the valley."}
            return super().complete(system, prompt, schema, prefix)
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], More(), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    il.run()
    assert s2.m["world"]["text"] == WORLD + " Machines and iron rails cross the valley." and s2.m["world"]["base"] == WORLD
    # an edited world note is inherited as it is
    s1b, s2b = Mem(), Mem()
    il = art.Illustrator([art.Book(d1, bk1, s1b), art.Book(d2, bk2, s2b)], More(), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    il.man[0]["world"].update({"text": "My own words about a green valley.", "edited": True})
    il.run()
    assert s2b.m["world"]["text"] == "My own words about a green valley." and s2b.m["world"]["inherited"]
    assert s1b.m["world"]["text"] == "My own words about a green valley."


def test_an_earlier_book_without_its_epub_is_only_read():
    bk1, d1, bk2, d2 = series_docs()
    s1 = Mem()
    t = Text(notes={"Mira": ("a young woman", ["scarred", "older and harder"])})
    il = art.Illustrator([art.Book(d1, None, s1, None, draw=False), art.Book(d2, bk2, Mem())], t, Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0)
    rep = il.run()
    assert s1.m is None and rep.drawn > 0
    assert {p["target"] for p in pics(il.books[1].b.sink, "portrait")} == {"mira", "oren"}   # nothing earlier to inherit


def test_on_demand_job_and_prompt_refuse_a_target_the_reader_has_not_reached():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][2]["facets"].append({"id": "bk.mill.f1", "from": "a1", "basis": "stated", "evidence": ["a1"],
                                       "text": "A grey stone mill with a wooden gate and a wheel on the river."})
    t = Text(notes={"Mill": ("a mill", ["grey stone", "a wooden gate"])})
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    with pytest.raises(art.NotVisible):
        il.job("event", "e4", at="a3")
    with pytest.raises(art.NotVisible):
        il.job("portrait", "stranger", at="a3")
    with pytest.raises(art.NotVisible):          # the world note ends at a5: an earlier picture is held to it
        il.job("event", "e2", at="a3")
    job = il.job("event", "e2", at="a6")
    pr = il.make_prompt(job)
    assert pr.anchor == "a5" and pr.text.startswith(SCENE) and pr.references == []
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


def test_an_entity_whose_look_the_text_never_shows_gets_no_portrait_and_a_reason():
    t = Text(notes={"Oren": ""})
    il, rep, sink, t, im = run(text_=t)
    assert "oren" not in {p["target"] for p in pics(sink, "portrait")} and rep.skipped["skipped: no look described"] == 1
    assert all(NOTES["Oren"].rstrip(".") not in p["prompt"] for p in pics(sink))
    n = len(im.calls)
    run(il.books[0].doc, il.books[0].epub, sink, Text(notes={"Oren": ""}), im)      # the skip is remembered: no new text call for Oren
    assert len(im.calls) == n


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
    il, rep, sink, t, im = run(d, bk, text_=Text(notes={"Vesna": VESNA}))
    [v] = [p for p in pics(sink, "portrait") if p["target"] == "vesna"]
    assert v["from"] == "a9" and "skipped: no look described" not in rep.skipped        # the facet that describes her
    assert v["appearance"] == art.compose_note(*VESNA) and "a9" in v["sources"]
    asked = [p for p in t.prompts if "appearance note for Vesna" in p]
    assert "a tall old woman with white hair" in asked[-1]                                   # written from that paragraph
    # a paragraph that names her beside look words is not enough: it may describe someone else
    bk, d = vesna_doc()
    d["entities"][-1]["facets"].pop()
    il, rep, sink, t, im = run(d, bk, text_=Text(notes={"Vesna": VESNA}))
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "vesna"] == []
    assert rep.skipped["skipped: no look described"] == 1          # at her first profile, as before: its note had no look


def test_an_empty_note_gives_way_when_the_book_describes_the_look_later():
    # the check run's set held the Count's empty note from chapter I and a final skip; a run of this version draws him
    bk, d = vesna_doc()
    sink = Mem()
    sink.m = {**art.blank("sha256:tbk", "ink"), "appearance": {"vesna": [{"from": "a5", "text": "", "details": [], "sources": ["a2"],
                                                                         "inherits": []}]},
              "skipped": [{"target": "vesna", "reason": "skipped: no look described", "kind": "portrait", "from": "a2", "final": True}]}
    il, rep, sink, t, im = run(d, bk, sink, Text(notes={"Vesna": VESNA}))
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "vesna"] == ["a9"]
    assert [n["from"] for n in sink.m["appearance"]["vesna"]] == ["a5", "a9"]
    # with no description after it the empty note stands: nothing is asked again
    bk2, d2 = vesna_doc()
    il, rep, sink2, t2, im = run(d2, bk2, text_=Text(notes={"Oren": ""}))
    asked = [p for p in t2.prompts if "appearance note for Oren" in p]
    assert len(asked) == 1 and "oren" not in {p["target"] for p in pics(sink2, "portrait")}


def test_a_note_that_says_no_look_is_shown_means_no_portrait_and_an_old_unchecked_note_is_written_again():
    # Peter Hawkins: "A solicitor, seen only through letters and papers; no physical appearance is shown"
    sink = Mem()
    sink.m = {**art.blank("sha256:tbk", "ink"), "appearance": {
        "oren": [{"from": "a3", "text": "A miller, seen only through his work; no physical appearance is shown.", "details": [],
                  "sources": [], "inherits": []}],
        # Mina's note of the check run: written before notes were quote-checked (no `details`), its look invented
        "mira": [{"from": "a5", "text": "A young woman with dark hair worn up and a pale, oval face.", "sources": [], "inherits": []}]}}
    il, rep, sink, t, im = run(sink=sink)
    assert "oren" not in {p["target"] for p in pics(sink, "portrait")} and rep.skipped["skipped: no look described"] == 1
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert m["appearance"] == NOTES["Mira"] and "worn up" not in m["prompt"]
    assert any("appearance note for Mira" in p for p in t.prompts)


def test_a_portrait_scene_takes_no_look_from_another_figure_or_the_passages():
    # Peter Hawkins was drawn as "the young solicitor in a dark coat", Harker's look: a portrait's look is its own note's only
    def scene(p):
        if "This is a portrait" in p and "- Oren:" in p:
            return PORTRAIT_SCENE.replace("the figure turned", "the young figure in a blue coat turned")
        return PORTRAIT_SCENE if "This is a portrait" in p else SCENE
    il, rep, sink, t, im = run(text_=Text(scene=scene))
    assert "oren" not in {p["target"] for p in pics(sink, "portrait")} and rep.skipped["dropped: look not in the note"] == 1
    assert {"mira", "stranger", "gabriel"} <= {p["target"] for p in pics(sink, "portrait")}
    writer = next(p for p in t.prompts if "This is a portrait" in p and "- Oren:" in p)
    assert "portrait of Oren" not in writer and "young solicitor" not in writer and "It was rejected: look not in the note" in \
        next(p for p in t.prompts if "It was rejected" in p and "- Oren:" in p)
    # a scene may give a figure the look its note or the passage gives it, and nothing else
    assert art.unlooked("an old miller with white hair", NOTES["Oren"]) == ""
    assert art.unlooked("a young miller with white hair", NOTES["Oren"]) == "young"
    # an adjective kept after the rewrite is taken out by code (the Count's "thin" in the chapter II plate of the check run);
    # a noun ("a blue coat" above) never is
    def thin(p):
        return SCENE.replace("a miller watches", "a thin, gaunt miller watches") if "This is a portrait" not in p else PORTRAIT_SCENE
    il, rep, sink, t, im = run(text_=Text(scene=thin))
    plates = pics(sink, "plate")
    assert len(plates) == 2 and all("while a miller watches" in p["prompt"] for p in plates)
    assert rep.dropped["fixed: look word not in the notes taken out"] >= 2 and not rep.skipped["dropped: look not in the note"]
    assert art._drop_word("a tall, thin old man", "thin") == "a tall, old man" and art._drop_word("a young, thin rider", "young") == \
        "a thin rider" and art._drop_word("A young English traveller", "young") == "An English traveller"


def test_look_details_the_passages_and_notes_do_not_show_are_dropped():
    # Mina at chapter I: "dark hair worn up and a pale, oval face", none of it in the text
    invented = ("a young woman", ["slight", "dark hair worn up", "a pale oval face", "braided hair"])
    il, rep, sink, t, im = run(text_=Text(notes={"Mira": invented}))
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert m["appearance"] == "A young woman: slight, braided hair." and rep.dropped["dropped: look detail not in the text"] == 2
    assert any("had no quote of the passages or notes" in p and "dark hair worn up" in p for p in t.prompts)   # one rewrite first
    note = sink.m["appearance"]["mira"][0]
    assert [d["text"] for d in note["details"]] == ["slight", "braided hair"] and all(d["quote"] for d in note["details"])
    # a detail with a word its quote lacks is not supported ("dark braided hair" on "braided hair"); a quote must be verbatim
    assert not art.detail_supported("dark braided hair", "braided hair")
    assert art.detail_supported("a scar", "a scarred face") and art.quoted("long white moustache", "a tall man, with a long white moustache")
    assert not art.quoted("long grey moustache", "a tall man, with a long white moustache")
    # nothing supported: no note, no portrait
    il, rep, sink, t, im = run(text_=Text(notes={"Mira": ("a young woman", ["dark hair worn up", "a pale oval face"])}))
    assert "mira" not in {p["target"] for p in pics(sink, "portrait")} and rep.skipped["skipped: no look described"] == 1


# ---- portraits round 2 (review-portraits.md): no look reaches a picture unless the text states it

def test_a_role_carries_no_look_its_details_do_not_quote():
    # the role "a young man with dark hair and a pale face" beside one honest quoted detail: every role word is somewhere in what
    # was sent, but none in a kept detail's quote, so the role keeps no look
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"][0]["text"] = ("The old man, with a long white moustache, who runs the mill; a young boy with dark "
                                             "hair and a pale face helps him.")
    t = Text(notes={"Oren": ("a young man with dark hair and a pale face", ["a long white moustache"])})
    il, rep, sink, t, im = run(d, bk, text_=t)
    [o] = [p for p in pics(sink, "portrait") if p["target"] == "oren"]
    assert o["appearance"] == "A man: a long white moustache." and rep.dropped["dropped: look word in the role not quoted"] == 4   # young, dark, hair, pale
    # a look word in the role that a kept detail quotes stays ("old" on "The old miller")
    t = Text(notes={"Oren": ("an old miller", ["old", "broad"])})
    il, rep, sink, t, im = run(text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "An old miller: old, broad."


def test_a_quote_is_a_phrase_of_the_text_and_holds_every_word_of_its_detail():
    src = "She had dark eyes. Her hair was long. The pale light fell on a scarlet cloak. A tall man, with a long white moustache."
    assert art.quoted("a long white moustache", src) and not art.quoted("long grey moustache", src)
    assert not art.quoted("dark ... hair", src)                   # pieces from two sentences
    assert art.quoted("a tall man ... white moustache", src)      # in order within one sentence
    assert not art.quoted("white moustache ... a tall man", src)  # out of order
    assert not art.detail_supported("pale skin", "the pale light")          # "skin" rides on nothing
    assert not art.detail_supported("dark hair", "dark eyes")
    assert art.detail_supported("dressed in black", "clad in black")        # a word naming only the kind of detail may
    # whole words with their endings, never a prefix
    assert not art.detail_supported("a scar", "a scarlet cloak") and not art.detail_supported("a big man", "many big hats")
    assert art.detail_supported("a scar", "a scarred face") and art.detail_supported("older", "the old miller")
    assert art.detail_supported("white moustaches", "a long white moustache") and art.detail_supported("freckled", "freckles")
    # in a run: the cross-sentence ellipsis and the borrowed "skin" are dropped, the real phrase kept
    t = Text(notes={"Mira": ("a woman", ["dark braided hair", "pale skin"])})

    def note(prompt):
        return {"role": "a woman", "details": [{"text": "dark braided hair", "quote": "with dark braided hair"},
                                               {"text": "a slight frame", "quote": "dark ... hair"},
                                               {"text": "pale skin", "quote": "a plain grey dress"}]}
    t.notes["Mira"] = None
    real = t.complete

    def complete(system, prompt, schema, prefix=None):
        if "appearance note for Mira" in prompt:
            t.prompts.append(prompt)
            return note(prompt)
        return real(system, prompt, schema, prefix)
    t.complete = complete
    il, rep, sink, t2, im = run(text_=t)
    assert sink.m["appearance"]["mira"][0]["text"] == "A woman: dark braided hair."
    assert rep.dropped["dropped: look detail not in the text"] == 2


def test_a_quote_from_a_paragraph_that_only_gives_context_is_not_the_figures():
    # Peter Hawkins with the Count's moustache: the paragraph beside his mention describes someone else
    ch1 = ["Mira walked to the mill behind a stranger with a long white moustache that morning."] + CH1[1:]
    bk = book(("One", ch1), ("Two", CH2))
    d = base_doc(bk)
    il, rep, sink, t, im = run(d, bk, text_=Text(notes={"Oren": ("a miller", ["broad", "a long white moustache"])}))
    assert any("a long white moustache" in p for p in t.prompts if "appearance note for Oren" in p)   # it was sent, as context
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: broad."
    assert rep.dropped["dropped: look detail not in the text"] == 1


def test_a_portrait_scene_gives_no_colour_or_shape_its_note_lacks():
    # the check run's Mina face on a note that is only "a brown shawl"
    note = "A woman: a brown shawl."
    assert art.unlooked("Her pale, oval face is turned to the window.", note) in ("pale", "oval")
    assert art.unlooked("She turns, her hair was dark and loose.", note) == "hair"
    assert art.figure_looks("her hair was dark and loose") == ["dark", "loose"]
    assert art.unlooked("Her brown shawl is drawn close.", note) == ""
    assert art.unlooked("Her thin red lips part.", note) in ("thin", "red")     # a body part described is a look (the live Count)
    for fine in ("A turn of the head toward a dark window.", "Her hands folded in her lap against a plain dark background.",
                 "Pale light falls across her face from one side.", "A faint smile on her lips, her teeth just showing."):
        assert art.unlooked(fine, note) == "", fine
    def scene(p):
        if "This is a portrait" in p and "- Mira:" in p:
            return PORTRAIT_SCENE.replace("the figure turned", "the figure with a pale, oval face turned")
        return PORTRAIT_SCENE if "This is a portrait" in p else SCENE
    il, rep, sink, t, im = run(text_=Text(notes={"Mira": ("a woman", ["a brown shawl"])}, scene=scene))
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert "pale" not in m["prompt"].split("Figures:")[0] and "oval" not in m["prompt"]     # taken out after the rewrite
    assert any(re.search("It was rejected: look not in the note: (pale|oval)", p) for p in t.prompts)


def test_the_drops_counted_are_those_of_the_reply_kept():
    # the first reply is kept (more details hold); the rewrite's discarded non-looks are not counted
    class T(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "appearance note for Mira" in prompt:
                self.prompts.append(prompt)
                if "had no quote" in prompt:
                    return {"role": "a woman", "details": [{"text": "a kind soul", "quote": "x y"},
                                                           {"text": "a cheerful heart", "quote": "x y"}]}
                return {"role": "a woman", "details": [{"text": "slight", "quote": "A slight young"},
                                                       {"text": "red hair", "quote": "red hair"}]}
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(text_=T())
    assert sink.m["appearance"]["mira"][0]["text"] == "A woman: slight."
    assert rep.dropped["dropped: look detail not in the text"] == 1 and rep.dropped["dropped: not a look detail"] == 0


def test_names_are_replaced_by_a_role_when_the_rewrite_still_names_someone():
    # Jonathan Harker's portrait was dropped with "dropped: name in prompt"
    def scene(p):
        return ("A portrait of Mira, Oren's helper, " + PORTRAIT_SCENE[2:]) if "This is a portrait" in p else SCENE
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


def test_only_the_figures_the_writer_says_are_in_the_picture_get_notes_and_references():
    il, rep, sink, t, im = run(text_=Text(shows=["Oren"]), level="all")
    two = next(p for p in pics(sink) if p["target"] == "One")                  # the plate of e2, naming oren and mira
    by = {p["target"]: p for p in pics(sink, "portrait")}
    assert NOTES["Oren"].rstrip(".") in two["prompt"] and NOTES["Mira"].rstrip(".") not in two["prompt"]
    assert two["references"] == [by["oren"]["id"]]
    il, rep, sink, t, im = run(text_=Text(shows=["Nobody Known"]), level="all")     # a list that matches no one keeps them all
    assert NOTES["Mira"].rstrip(".") in next(p for p in pics(sink) if p["target"] == "One")["prompt"]


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
    e1 = next(s for s in t.scenes() if "this moment: Mira walks to the mill" in s)
    assert "Gabriel" not in e1 and "silver coin" not in e1


def test_the_world_note_comes_from_the_first_story_chapter_unless_it_is_short(monkeypatch):
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    monkeypatch.setattr(art, "WORLD_MIN_WORDS", 50)
    il, rep, sink, t, im = run(d, bk)
    assert sink.m["world"]["from"] == "a5" and rep.held == 2               # the portraits at a3 and a4; the plate of One is at a5
    world = next(p for p in t.prompts if "visual world note" in p)
    assert "Zephyr" not in world and "Two" not in world
    monkeypatch.setattr(art, "WORLD_MIN_WORDS", 100)
    _, _, sink2, _, _ = run(base_doc(bk), bk)
    assert sink2.m["world"]["from"] == "a11"


def test_look_changes_held_to_one_anchor_give_one_portrait(monkeypatch):
    monkeypatch.setattr(art, "WORLD_MIN_WORDS", 100)                        # everything is held to a11
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append(portrait("mira", "a10", "A scarred young woman, older than she was, who keeps a coin.", 2))
    il, rep, sink, t, im = run(d, bk)
    ms = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert [p["from"] for p in ms] == ["a11"]
    froms = [(p["target"], p["from"]) for p in pics(sink, "portrait")]
    assert len(froms) == len(set(froms))


def test_a_look_word_gained_once_counts_once():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    mira = d["entities"][0]
    mira["facets"] += [portrait("mira", "a7", "A scarred young woman who carries baskets to the mill.", 2),
                       portrait("mira", "a10", "A scarred young woman in a new coat who keeps a coin.", 3)]
    il, rep, sink, t, im = run(d, bk)
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "mira"] == ["a5", "a7"]


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
            if "main moment of a chapter: Oren the miller" in prompt and (self.every or not self.failed):
                self.failed += 1
                raise llm.ProviderError("HTTP 429: slow down")
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(text_=Failing())
    assert "One" not in {p["target"] for p in pics(sink, "plate")} and "Two" in {p["target"] for p in pics(sink, "plate")}
    assert rep.skipped["skipped: text model failed"] == 1 and t.failed == 2 and pics(sink, "portrait")
    assert any("HTTP 429" in r["reason"] for r in rep.reasons)
    il, rep, sink, t, im = run(text_=Failing(every=False))                   # a passing failure is retried once
    assert "One" in {p["target"] for p in pics(sink, "plate")} and not rep.skipped


def test_an_empty_shows_list_means_no_figures():
    il, rep, sink, t, im = run(text_=Text(shows=[]))
    for p in pics(sink, "plate"):
        assert "Figures:" not in p["prompt"] and p["references"] == []
    assert all(NOTES[n].rstrip(".") in p["prompt"] for p in pics(sink, "portrait") for n in ["Mira"] if p["target"] == "mira")


def test_an_empty_appearance_note_is_stored_and_not_asked_for_again():
    t = Text(notes={"Oren": ""})
    il, rep, sink, t, im = run(text_=t, level="all")
    asked = [p for p in t.prompts if "appearance note for Oren" in p]
    assert len(asked) == 1 and [n["text"] for n in sink.m["appearance"]["oren"]] == [""]
    assert rep.skipped["skipped: no look described"] == 1


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
            if shape == "portrait" and NOTES["Oren"].rstrip(".") in prompt and seen["n"] < 2:
                seen["n"] += 1
                self.calls.append({"prompt": prompt, "shape": shape, "references": []})
                raise images.ImageRefused("safety")
            return super().draw(prompt, shape=shape, quality=quality, references=references)
    s1, s2 = Mem(), Mem()
    il = art.Illustrator([art.Book(d1, bk1, s1, None), art.Book(d2, bk2, s2)], Text(), RefuseOrenOnce(), "ink", workers=1,
                         log=lambda *_: None, pause=0)
    rep = il.run()
    assert "oren" not in {p["target"] for p in pics(s1, "portrait")} and rep.refused == 1
    assert "oren" in {p["target"] for p in pics(s2, "portrait")}


def test_on_demand_job_refuses_an_unknown_chapter_and_finds_an_earlier_books_entity():
    bk1, d1, bk2, d2 = series_docs()
    il = art.Illustrator([art.Book(d1, bk1, Mem()), art.Book(d2, bk2, Mem())], Text(), Image(), "ink", workers=1,
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
    il = art.Illustrator([art.Book(d1, bk1, s1, None), art.Book(d2, bk2, s2, "all")], Text(), Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0)
    rep = il.run()
    assert s1.m["world"]["from"] == "a5" and s2.m["world"]["book"] == "b1" and s2.m["world"]["inherited"]
    assert not rep.skipped.get("dropped: written from content after its anchor")
    o = ordinal(d2)
    early = [p for p in pics(s2) if o[p["from"]] < o["a5"]]
    assert {p["target"] for p in early} >= {"e2"}                            # book two's opening is drawn, held to nothing
    e2 = next(p for p in pics(s2) if p["target"] == "e2")
    assert "a7" not in e2["sources"] and all(o[a] <= o[e2["from"]] for a in e2["sources"])


def test_with_a_picture_limit_only_the_pictures_drawn_get_notes_and_screening():
    il, rep, sink, t, im = run(max_pictures=1)
    notes = [p for p in t.prompts if "fixed appearance note" in p]
    assert rep.drawn == 1 and len(notes) == 1 and not any("Which of them are a visible moment" in p for p in t.prompts)


def test_text_spent_on_notes_counts_against_the_cap():
    class Usage:
        cost_usd = 0.0

    class Costly(Text):
        usage = Usage()

        def complete(self, system, prompt, schema, prefix=None):
            self.usage.cost_usd += 0.3
            return super().complete(system, prompt, schema, prefix)
    t = Costly()
    il, rep, sink, _, im = run(text_=t, cap=1.0)
    notes = [p for p in t.prompts if "fixed appearance note" in p]
    assert "cost cap" in rep.stopped and len(notes) <= 1 and not im.calls


def test_a_reservation_is_released_by_the_amount_it_reserved():
    il = art.Illustrator([art.Book(base_doc(book(("One", CH1), ("Two", CH2))), book(("One", CH1), ("Two", CH2)), Mem())],
                         Text(), Image(), "ink", cap=10, log=lambda *_: None, pause=0)
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
    il = art.Illustrator([art.Book(d1, bk1, s1, None, draw=False), art.Book(d2, bk2, Mem())], Text(), Image(), "ink", workers=1,
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
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", log=lambda *_: None)
    b = il.books[0]
    assert b.seq_of["a11"] == b.seq_of["a10"] == len(bk.blocks) and b.hi == len(bk.blocks) - 1 and b.seq_of["a9"] == 9


def test_ordinary_words_of_a_group_name_are_not_names():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"].append(ent("order", "The Order of the Dawn", "group"))
    dawn = "Dawn breaks over a grey stone mill " + SCENE[SCENE.index("beside") + 21:]
    il, rep, sink, t, im = run(d, bk, text_=Text(scene=lambda p: dawn), portraits=False)
    assert len(pics(sink, "plate")) == 2
    named = dawn + " A banner of the Order of the Dawn hangs there."
    il, rep, sink, t, im = run(d, bk, text_=Text(scene=lambda p: named), portraits=False)
    assert not pics(sink, "plate") and il.names[0].find(named) == "Order of the Dawn"
    assert il.names[0].find("They meet Mira's brother.") == "Mira" and il.names[0].find("Stranger things wait. Grey hills.") == ""
    assert il.names[0].find("They watch the Stranger.") == "Stranger"


def test_an_on_demand_plate_screens_its_chapter_and_shows_the_turning_point():
    bk = book(("One", CH1), ("Two", CH2))
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], Text(), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    j = il.job("plate", "One")
    assert j.event and j.event["id"] == "e2"


def test_counts_need_no_model_call():
    bk = book(("One", CH1), ("Two", CH2))
    t = Text()
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    c = il.counts()
    assert c == {"plates": 2, "portraits": 4, "chapters": [{"title": "One", "drawable": None}, {"title": "Two", "drawable": None}]}
    assert not t.prompts
    il.run()
    n = len(t.prompts)
    assert [x["drawable"] for x in il.counts()["chapters"]] == [2, 3] and len(t.prompts) == n


def test_an_edited_prompt_is_never_softened_and_its_refusal_is_raised():
    bk = book(("One", CH1), ("Two", CH2))
    t, im = Text(), Image(refuse=[lambda prompt, n: True])
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], t, im, "ink", workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    with pytest.raises(images.ImageRefused):
        il.draw(il.job("plate", "One"), text="My own words: a mill at dusk, a woman at the gate, nothing else in view at all.")
    assert len(im.calls) == 1 and not any("quieter composition" in p for p in t.prompts) and il.report.refused == 1
    assert not il.man[0]["skipped"]                    # the person may try again


# ---- round 3, item 13: what a prompt inherits from earlier books, and readers who have not finished them

OWN_WORLD = ("A river valley seen years later: the same stone mills and cottages, some roofs mended with new slate, carts and "
             "a few iron tools. People wear wool coats. The light is clear and cold, the fields worked hard, the mood watchful.")


class TwoWorlds(Text):
    def complete(self, system, prompt, schema, prefix=None):
        if "visual world note" in prompt and "Three" in prompt:
            self.prompts.append(prompt)
            return {"text": OWN_WORLD}
        return super().complete(system, prompt, schema, prefix)


def test_every_picture_says_which_earlier_books_its_prompt_inherits():
    bk1, d1, bk2, d2 = series_docs()
    s1, s2 = Mem(), Mem()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], TwoWorlds(), Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0)
    il.run()
    assert all(p["inherits"] == [] for p in pics(s1))
    assert pics(s2) and all(p["inherits"] == ["b1"] and not p.get("partial") for p in pics(s2))   # book one's world note
    assert WORLD in pics(s2, "plate")[0]["prompt"]


def test_a_reader_who_has_not_finished_the_earlier_book_gets_nothing_from_it():
    bk1, d1, bk2, d2 = series_docs()
    s1, s2 = Mem(), Mem()
    t, im = TwoWorlds(), Image()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], t, im, "ink", workers=1, log=lambda *_: None, pause=0)
    il.run()                                                     # a build: book one counts as read
    full = next(p for p in pics(s2, "plate"))
    with pytest.raises(art.NotVisible):
        il.job("portrait", "stranger", at="b2.a5", finished=set())       # only book one has the stranger
    job = il.job("plate", "Three", at="b2.a5", finished=set())
    pr = il.make_prompt(job)
    assert pr.partial and pr.inherits == [] and WORLD not in pr.text and OWN_WORLD in pr.text
    assert s2.m["worldOwn"]["text"] == OWN_WORLD and s2.m["world"]["inherited"]               # the set's note is unchanged
    assert all(j == 1 for j, _ in pr.references)                # no picture of book one as a reference
    oren = [n for n in s2.m["appearance"]["oren"] if n.get("partial")]
    assert oren and oren[0]["inherits"] == []                   # written again from book two alone
    rec = il.draw(job, pr)
    assert rec["partial"] and rec["inherits"] == []
    # a reader who finished book one gets the set's own prompt, and a resumed build never counts the partial picture as drawn
    pr2 = il.make_prompt(il.job("plate", "Three", at="b2.a5", finished={"b1"}))
    assert not pr2.partial and pr2.inherits == ["b1"] and WORLD in pr2.text
    assert (rec["kind"], rec["target"], rec["from"]) not in il._have(1) or full["from"] == rec["from"]
    n = len(im.calls)
    art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], TwoWorlds(), im, "ink", workers=1, log=lambda *_: None,
                    pause=0).run()
    assert len(im.calls) == n                                   # the full plate was already there


def names_for(entities):
    """The banned names of a one-book run over the fixture book with these entities."""
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"] = d["entities"] + entities
    il = art.Illustrator([art.Book(d, bk, Mem(), "main")], Text(), Image(), "ink", log=lambda *_: None, pause=0)
    return il.names[0]


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
        return ("The Stranger sits by the fire, " + PORTRAIT_SCENE[2:]) if "This is a portrait" in p else SCENE
    il, rep, sink, t, im = run(text_=Text(scene=scene))
    [s] = [p for p in pics(sink, "portrait") if p["target"] == "stranger"]
    assert s["prompt"].startswith("The figure sits by the fire, head-and-shoulders portrait")
    assert rep.dropped["fixed: name replaced by a role"] >= 1 and not rep.dropped["dropped: name in prompt"]


def test_a_hyphenated_name_in_the_scene_is_fixed_not_dropped():
    def scene(p):
        return ("A half-Mira smile, " + PORTRAIT_SCENE[2:]) if "This is a portrait" in p else SCENE
    il, rep, sink, t, im = run(text_=Text(scene=scene))
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert m["prompt"].startswith("A half-figure smile, head-and-shoulders portrait") and "Mira" not in m["prompt"]


def test_a_name_in_an_appearance_note_role_never_reaches_the_prompt():
    # the writer gave the note a role with a name in it ("a Mira miller"): taken out when the note is written
    il, rep, sink, t, im = run(text_=Text(notes={"Oren": ("a Mira miller", NOTE_PARTS["Oren"][1])}))
    assert sink.m["appearance"]["oren"][0]["text"].startswith("A miller:")
    assert not any(re.search(r"\bOren\b", p["prompt"].split("Figures:")[1]) for p in pics(sink) if "Figures:" in p["prompt"])


def test_a_stored_note_or_world_note_that_names_someone_is_stripped_in_the_prompt():
    # a note stored before notes were checked for names, and a world note that names one: the prompt still names nobody
    sink = Mem()
    run(sink=sink)
    sink.m["appearance"]["mira"][-1]["text"] = "Oren's helper: slight, dark braided hair."
    sink.m["world"]["text"] = WORLD + " The Stranger roams it."
    sink.m["pictures"] = [p for p in sink.m["pictures"] if p["target"] != "mira"]
    il, rep, sink, t, im = run(sink=sink)
    [m] = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert "Oren" not in m["prompt"] and "Stranger" not in m["prompt"] and "The figure's helper: slight" in m["prompt"]


def test_a_dropped_prompt_says_the_check_and_the_word_only_when_the_reader_has_met_it():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il = art.Illustrator([art.Book(d, bk, Mem(), "main")], Text(), Image(), "ink", log=lambda *_: None, pause=0)
    assert il._say("name in the text: Oren", 0, "a5", None) == "a name the picture must not show: Oren"
    assert il._say("name in the text: Gabriel", 0, "a5", None) == "a name the book has not used by this point"   # met at a8
    assert il._say("name in the text: Gabriel", 0, "a9", None) == "a name the picture must not show: Gabriel"
    assert il._say("name in the text: GABRIEL", 0, "a9", None) == "a name the picture must not show: GABRIEL"
    assert il._say("unknown name: Valdemar", 0, "a11", None) == "a name the book has not used by this point"
    assert il._say("look not in the note: thin", 0, "a5", None) == "a look the book does not give this figure: thin"
    assert art.explain("wording: has just") == "wording that looks back on the story or ahead: has just"


# Morning Star: Cassius au Bellona's portrait failed with "name in prompt" on every retry. A name with a lowercase particle, a
# family name that is also a group's ("House Bellona"), and profiles that use it as a modifier ("the Bellona heir").
CH_BELLONA = ["Cassius au Bellona rode to the mill at dawn in a long golden cloak and waited.",
              "Men of House Bellona followed him with their banners high over the dusty road.",
              "Mira saw the Bellona heir dismount and walk to the gate in his golden armour.",
              "Oren said a Bellona knight had never asked for flour at his mill before then.",
              "Cassius au Bellona laughed and paid Oren in gold for every sack in the cart.",
              "The Bellonas left before noon and the dust of the road settled behind them all."]


def bellona_run(scene, note=("the Bellona heir", ["a long golden cloak", "golden armour"])):
    bk = book(("One", CH1), ("Two", CH_BELLONA))
    d = base_doc(bk)
    d["entities"] += [ent("cassius", "Cassius au Bellona", frm="a6", facets=[
                          portrait("cassius", "a8", "The Bellona heir, a Bellona knight in a long golden cloak and golden armour."),
                          {"id": "bk.cassius.f1", "kind": "role", "from": "a9", "basis": "stated", "evidence": ["a9"],
                           "text": "A Bellona knight who pays in gold."}]),
                      ent("house", "House Bellona", "group", frm="a7")]
    d["events"] += [event(7, 6, "Cassius au Bellona rides to the mill.", ["cassius"]),
                    event(8, 10, "Cassius au Bellona pays Oren in gold.", ["cassius", "oren"])]
    t = Text(scene=lambda p: scene if "This is a portrait" in p else SCENE, notes={"Cassius au bellona": note})
    il = art.Illustrator([art.Book(d, bk, Mem(), "main")], t, Image(), "ink", log=lambda *_: None, pause=0)
    return il, il.make_prompt(il.job("portrait", "cassius", at="a11"))


@pytest.mark.parametrize("opening", ["Cassius au Bellona stands, ", "The Bellona heir stands, ", "A Bellona knight stands, ",
                                     "The Bellonas' heir of House Bellona stands, ", "CASSIUS AU BELLONA stands, ",
                                     "Cassius au Bellona’s half-Bellona sneer, "])
def test_a_particle_name_and_a_house_name_in_a_portrait_scene_are_removed(opening):
    il, pr = bellona_run(opening + PORTRAIT_SCENE[2:])
    assert not re.search(r"(?i)bellona|cassius", pr.text), pr.text
    assert not il.names[0].find(pr.text)


def test_a_role_that_names_the_house_is_written_without_it():
    il, pr = bellona_run(PORTRAIT_SCENE)
    [(eid, note)] = pr.notes
    assert eid == "cassius" and note.startswith("The heir:") and "Bellona" not in note, note


# Review round 1: find and strip share one boundary and possessive rule, so whatever find reports strip takes out, in every
# combination: capitals with 'S, quotes around a name, both apostrophes, non-ASCII letters, particles, hyphens.
SHAPE_NAMES = ["Mira", "Jonathan Harker", "Jean-François", "Gabriel de León", "Patrick O'Brien", "Cassius au Bellona", "Millers"]
SHAPES = ["{n} waits.", "He sees {n} by the gate.", "The coat of {n}'s, {n}’s and {n}'S.", "A banner reads {N}.",
          "A banner reads {N}'S coat and {N}’S hat.", "He calls 'Mira' and '{n}' and \"{n}\" and ‘{n}’.",
          "A '{N}' sign.", "A half-{n} grin and {n}-like shadows.", "The {n}s' cart.", "The {N}S' cart.",
          "Mr. {n} and Professor {n}’s owl.", "Behind them, {n}'s.", "\"{n},\" he says."]


def shape_names():
    return names_for([ent("harker", "Jonathan Harker", frm="a2"), ent("jf", "Jean-François", frm="a2"),
                      ent("leon", "Gabriel de León", frm="a2"), ent("ob", "Patrick O'Brien", frm="a2"),
                      ent("cassius", "Cassius au Bellona", frm="a2"), ent("millers", "the Millers", "group", frm="a2")])


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("name", SHAPE_NAMES)
def test_find_after_strip_is_empty_for_every_shape(name, shape):
    names = shape_names()
    t = shape.format(n=name, N=name.upper())
    assert names.find(t), t
    out = names.strip(t)
    assert names.find(out) == "", (t, out)


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
