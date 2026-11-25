"""Art sets (art.py) with a scripted text model and a scripted image model: what is drawn, from which anchor, with which
references; the checks, refusals, the cost cap, resume and series inheritance."""

import json
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


# The scripted model's own idea of a look and of a look change (the test's, not the code's: art.py has no such lists).
FAKE_LOOK = re.compile(r"\b(hair|haired|shawl|dress|coat|cloak|hooded|apron|boots|tall|old|white|scar\w*|older|moustache|nose|"
                       r"eyes|wolf pelt|mud)\b", re.IGNORECASE)
FAKE_CHANGE = re.compile(r"\b(scarred|older than she was|lost an eye|eyes replaced)\b", re.IGNORECASE)


class Text:
    """A scripted text model that answers by the kind of prompt it is given and keeps every prompt. `look(text)` and
    `change(text)` say how it classifies an entity's item (default: the FAKE_ regexes above), `groups(who, items, groups)` which
    groups it picks (default none), `check(text, source)` which look phrases it calls unfounded (default none)."""

    def __init__(self, scene=SCENE, world=WORLD, screen=None, notes=None, shows=None, look=None, change=None, groups=None,
                 check=None, amend=None, intro=None):
        self.amend, self.intro = amend, intro
        self.scene, self.world, self.notes, self.shows = scene, world, dict(NOTE_PARTS, **(notes or {})), shows
        self.screen = screen
        self.look = look or (lambda t: bool(FAKE_LOOK.search(t)))
        self.change = change or (lambda t: (m.group(0) if (m := FAKE_CHANGE.search(t)) else ""))
        self.groups, self.check = groups, check
        self.prompts = []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
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
        if "Which of the groups is" in prompt:
            who = re.search(r"Which of the groups is (.+?) a member of", prompt).group(1)
            notes = re.findall(r"^(\d+)\. (.*)$", prompt.split("Notes on", 1)[1].split("Groups:")[0], re.M)
            groups = re.findall(r"^([A-Z])\. (.*)$", prompt.split("Groups:")[1], re.M)
            return {"groups": self.groups(who, notes, groups) if self.groups else []}
        if "describes someone as they enter, before it gives their name" in prompt:
            return {"paragraphs": self.intro(prompt) if self.intro else []}
        if "List every phrase of the prompt that says how a" in prompt:
            text = prompt.split("Prompt:\n", 1)[1]
            source = prompt.split("Notes and passage:\n", 1)[1].split("\n\nPrompt:")[0]
            return {"phrases": self.check(text, source) if self.check else []}
        if "This is the opening of a later book" in prompt:
            return {"text": ""}
        if "visual world note" in prompt:
            return {"text": self.world}
        if "fixed appearance note" in prompt:
            who = re.search(r"appearance note for (.+?) \(", prompt).group(1).replace("the ", "").capitalize()
            if "The text now shows a change" in prompt and self.amend:
                return self.amend(prompt)
            if "The text now shows a change" in prompt:      # an amended note: what changed, quoted from the change
                ds = [m.group(0) for m in re.finditer(r"\b(older|scar\w*)( \w+){0,3}", prompt.split("The text now shows")[1][:200])]
                return {"role": "young woman", "details": [{"text": d, "quote": d, "seen": True} for d in dict.fromkeys(ds)]
                        + [{"text": "a livid scar across one cheek", "quote": "a livid scar", "seen": True}]}
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
        m = re.search(r"[A-Za-z]+ (?:" + FAKE_LOOK.pattern[3:-3] + r") [A-Za-z]+", given, re.IGNORECASE)
        return {"role": "a figure", "role_look": [],
                "details": [{"text": m.group(0), "quote": m.group(0), "seen": True}] if m else []}
    if not note:
        return {"role": "", "role_look": [], "details": []}
    role, ds, home = (note + ([],))[:3] if isinstance(note, tuple) else ("", [note], [])

    def q(d):
        m = re.search(r"(\w+[ ,]+)?\b" + re.escape(d) + r"\b([ ,]+\w+)?", prompt.split("What the companion has noted:", 1)[-1])
        return {"text": d, "quote": m.group(0) if m else d, "seen": True}
    return {"role": role, "role_look": [], "details": [q(d) for d in ds], "home": [q(d) for d in home]}


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


def test_a_look_change_is_the_classifiers_call_on_the_entitys_own_item():
    # a lost eye is a look change, a death alone is not, and what the entity does to something else never is (Sevro and the deer)
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    oren = d["entities"][1]
    oren["facets"].append({"id": "bk.oren.f1", "from": "a10", "basis": "stated", "evidence": ["a10"], "text": "Oren lost an eye in the storm."})
    il, rep, sink, t, im = run(d, bk)
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "oren"] == ["a5", "a10"]
    assert sink.m["looks"]["oren"]["items"]["bk.oren.f1"]["change"] is True
    oren["facets"][-1]["text"] = "Oren is found dead on the mill floor."
    _, _, sink2, _, _ = run(d, bk)
    assert [p["from"] for p in pics(sink2, "portrait") if p["target"] == "oren"] == ["a5"]
    # a change flag on an item number the call was never given, or for another entity's item, is not taken
    oren["facets"][-1]["text"] = "Oren drags the dead deer to the mill."
    t = Text(change=lambda txt: "the dead deer" if "deer" in txt else "")
    _, _, sink3, _, _ = run(d, bk, text_=t)
    assert [p["from"] for p in pics(sink3, "portrait") if p["target"] == "oren"] == ["a5", "a10"]   # the fake says so: code obeys

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


def test_a_later_book_reuses_notes_and_pictures_and_redraws_only_on_a_look_change():
    bk1, d1, bk2, d2 = series_docs()
    s1, s2 = Mem(), Mem()
    t, im = Text(), Image()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], t, im, "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
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
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], More(), Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    il.run()
    assert s2.m["world"]["text"] == WORLD + " Machines and iron rails cross the valley." and s2.m["world"]["base"] == WORLD
    # an edited world note is inherited as it is
    s1b, s2b = Mem(), Mem()
    il = art.Illustrator([art.Book(d1, bk1, s1b), art.Book(d2, bk2, s2b)], More(), Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    il.man[0]["world"].update({"text": "My own words about a green valley.", "edited": True})
    il.run()
    assert s2b.m["world"]["text"] == "My own words about a green valley." and s2b.m["world"]["inherited"]
    assert s1b.m["world"]["text"] == "My own words about a green valley."


def test_an_earlier_book_without_its_epub_is_only_read():
    bk1, d1, bk2, d2 = series_docs()
    s1 = Mem()
    t = Text(notes={"Mira": ("a young woman", ["scarred", "older and harder"])})
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
    t = Text(notes={"Mill": ("a mill", ["grey stone", "a wooden gate"])})
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
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


def test_a_note_of_an_older_version_is_written_again_and_its_picture_is_no_reference():
    # Mina's note of the first version (word lists, no look index): never used for a new picture, and the portrait drawn from it
    # stays in the set but is no reference for a new one; nothing is redrawn
    old_pic = {"id": "pold0001", "kind": "portrait", "target": "mira", "chapter": "One", "from": "a5", "file": "pold0001.png",
               "mime": "image/png", "prompt": "old", "references": [], "sources": ["a5"], "inherits": [], "cost": 0.05}
    sink = Mem()
    sink.files["pold0001.png"] = png(10, 10, b"old")
    sink.m = {**art.blank("sha256:tbk", "ink"), "pictures": [old_pic], "appearance": {
        "oren": [{"from": "a3", "text": "A miller, seen only through his work; no physical appearance is shown.", "details": [],
                  "sources": [], "inherits": []}],
        "mira": [{"from": "a5", "text": "A young woman with dark hair worn up and a pale, oval face.", "details": [],
                  "sources": [], "inherits": []}]}}
    il, rep, sink, t, im = run(sink=sink, level="all")
    assert any("appearance note for Mira" in p for p in t.prompts) and any("appearance note for Oren" in p for p in t.prompts)
    assert [p["id"] for p in pics(sink, "portrait") if p["target"] == "mira"] == ["pold0001"]          # not drawn again
    assert all(n.get("v") == art.NOTE_VERSION for k in ("mira", "oren") for n in sink.m["appearance"][k][1:])
    assert all("worn up" not in p["prompt"] for p in pics(sink) if p["id"] != "pold0001")
    assert all("pold0001" not in p["references"] for p in pics(sink))
    assert all(p.get("notes") == art.NOTE_VERSION for p in pics(sink) if p["id"] != "pold0001")


def fake_check(*looks):
    """A look check that calls each of `looks` unfounded when the prompt has it and the notes and passage do not."""
    def check(text, source):
        return [x for x in looks if x.lower() in text.lower() and x.lower() not in source.lower()]
    return check


def test_a_portrait_scene_takes_no_look_from_another_figure_or_the_passages():
    # Peter Hawkins was drawn as "the young solicitor in a dark coat", Harker's look: a portrait's look is its own note's only.
    # The check call lists the unfounded phrases; after the one rewrite code takes them out and the text is checked again.
    def scene(p):
        if "This is a portrait" in p and "- Oren:" in p:
            return PORTRAIT_SCENE.replace("the figure turned", "the young figure in a blue coat turned")
        return PORTRAIT_SCENE if "This is a portrait" in p else SCENE
    t = Text(scene=scene, check=fake_check("young", "in a blue coat"))
    il, rep, sink, t, im = run(text_=t)
    [o] = [p for p in pics(sink, "portrait") if p["target"] == "oren"]
    assert "the figure turned" in o["prompt"] and "blue coat" not in o["prompt"] and "young" not in o["prompt"].split("Figures:")[0]
    assert rep.dropped["fixed: look word not in the notes taken out"] == 1
    assert any("It was rejected: look not in the note: young | in a blue coat" in p for p in t.prompts if "- Oren:" in p)
    checks = [p for p in t.prompts if "List every phrase of the prompt" in p and "young figure" in p]
    # the portrait's check reads the figure's own note, never the passages
    assert checks and all(NOTES["Oren"].rstrip(".") in c and "Mira walked to the mill" not in c for c in checks)
    # a phrase the call names that is not in the prompt is ignored: the check can point at words, never add any
    t2 = Text(check=lambda text, source: ["a crimson sash"])
    il, rep, sink, t2, im = run(text_=t2)
    assert not rep.dropped["fixed: look word not in the notes taken out"] and len(pics(sink, "portrait")) == 4
    assert art._drop_phrase("a tall, thin old man", "thin") == "a tall, old man" and art._drop_phrase("a young, thin rider", "young") == \
        "a thin rider" and art._drop_phrase("A young English traveller", "young") == "An English traveller"
    assert art._drop_phrase("the figure in a blue coat turns.", "in a blue coat") == "the figure turns."


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

def test_a_role_is_who_it_is_in_its_own_words_and_no_detail_repeats_it():
    # the role "a young man with dark hair and a pale face": its first clause only, and the words the writer marks as look go
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"][0]["text"] = ("The old man, with a long white moustache, who runs the mill; a young boy with dark "
                                             "hair and a pale face helps him.")
    t = Text(notes={"Oren": ("a young man with dark hair and a pale face", ["a long white moustache"])})
    real = t.complete

    def complete(system, prompt, schema, prefix=None):
        out = real(system, prompt, schema, prefix)
        if "appearance note for Oren" in prompt:
            out["role_look"] = ["young", "violet"]
        return out
    t.complete = complete
    il, rep, sink, t, im = run(d, bk, text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "A man: a long white moustache."
    assert rep.dropped["dropped: look word in the role"] == 1           # "violet" is not in the role: nothing to take out
    # a role word the entity's own text lacks goes ("miller" is in the passage that names Oren, "baker" nowhere)
    t = Text(notes={"Oren": ("an old baker", ["broad"])})
    il, rep, sink, t, im = run(text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "An old: broad." or sink.m["appearance"]["oren"][0]["role"] == "an old"
    # "A small, filthy boy: small, smallest boy": a detail the role already says goes
    assert art.compose_note("a small, filthy boy", ["small", "smallest boy in the Institute", "filthy", "khaki teeth"]) == \
        "A small, filthy boy: smallest boy in the Institute, khaki teeth."
    assert art.compose_note("a miller", ["white hair", "white hair, long"]) == "A miller: white hair, long."   # the fuller stands


def test_a_quote_is_a_phrase_of_the_text_and_holds_every_word_of_its_detail():
    src = "She had dark eyes. Her hair was long. The pale light fell on a scarlet cloak. A tall man, with a long white moustache."
    assert art.quoted("a long white moustache", src) and not art.quoted("long grey moustache", src)
    assert not art.quoted("dark ... hair", src)                   # pieces from two sentences
    assert art.quoted("a tall man ... white moustache", src)      # in order within one sentence
    assert not art.quoted("white moustache ... a tall man", src)  # out of order
    assert not art.detail_supported("pale skin", "the pale light")          # "skin" rides on nothing
    assert not art.detail_supported("dark hair", "dark eyes")
    assert not art.detail_supported("dressed in black", "clad in black")    # every word, a kind-of-detail word too, is quoted
    assert art.detail_supported("clad in black", "was clad in black from head to foot")
    # whole words with their endings, never a prefix
    assert not art.detail_supported("a scar", "a scarlet cloak") and not art.detail_supported("a big man", "many big hats")
    assert art.detail_supported("a scar", "a scarred face") and art.detail_supported("older", "the old miller")
    assert art.detail_supported("white moustaches", "a long white moustache") and art.detail_supported("freckled", "freckles")
    # in a run: the cross-sentence ellipsis and the borrowed "skin" are dropped, the real phrase kept
    t = Text(notes={"Mira": ("a woman", ["dark braided hair", "pale skin"])})

    def note(prompt):
        return {"role": "a woman", "details": [{"text": "dark braided hair", "quote": "with dark braided hair", "seen": True},
                                               {"text": "a slight frame", "quote": "dark ... hair", "seen": True},
                                               {"text": "pale skin", "quote": "a plain grey dress", "seen": True}]}
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


def test_a_scene_takes_a_figures_look_only_from_its_notes_or_the_passage():
    # the check run's Mina face on a note that is only "a brown shawl": the plate's check reads the notes and the passage
    t = Text(notes={"Mira": ("a woman", ["a brown shawl"])}, check=fake_check("pale, oval face", "brown shawl"),
             scene=lambda p: SCENE.replace("A woman with a basket", "A woman with a pale, oval face and a brown shawl") if
             "This is a portrait" not in p else PORTRAIT_SCENE)
    il, rep, sink, t, im = run(text_=t)
    plates = pics(sink, "plate")
    by = {p["target"]: p["prompt"].split("Figures:")[0] for p in plates}
    assert set(by) == {"One", "Two"} and not any("oval" in x for x in by.values())
    assert "brown shawl" in by["One"]                 # Mira is in chapter one's moment: her note gives it
    assert "brown shawl" not in by["Two"]             # chapter two's figures are the stranger and Oren: it is taken out


def test_the_drops_counted_are_those_of_the_reply_kept():
    # the first reply is kept (more details hold); the rewrite's discarded non-looks are not counted
    class T(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "appearance note for Mira" in prompt:
                self.prompts.append(prompt)
                if "had no quote" in prompt:
                    return {"role": "a woman", "details": [{"text": "a kind soul", "quote": "x y", "seen": True},
                                                           {"text": "a cheerful heart", "quote": "x y", "seen": True}]}
                return {"role": "a woman", "details": [{"text": "slight", "quote": "A slight young", "seen": True},
                                                       {"text": "red hair", "quote": "red hair", "seen": True}]}
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


def test_a_later_look_item_fills_the_note_in_without_a_new_portrait():
    # Gabriel de León's 26 look facets: each later one adds its quoted details to the note, nothing dropped, no new version
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append({"id": "bk.mira.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Wears a red ribbon in her dark braided hair at the market."})
    t = Text(notes={"Mira": ("a young woman", ["slight", "dark braided hair", "a red ribbon"])})
    il, rep, sink, t, im = run(d, bk, text_=t, level="all")
    assert [p["from"] for p in pics(sink, "portrait") if p["target"] == "mira"] == ["a5"]
    assert len(sink.m["appearance"]["mira"]) == 1                     # no picture needed her after a10 in this run
    note, _, srcs, _ = il.ensure_note(0, "mira", "a11")                 # a picture at a11 does: the note is filled in
    notes = sink.m["appearance"]["mira"]
    assert notes[0]["text"] == "A young woman: slight, dark braided hair." and notes[-1]["from"] == "a11" and note == notes[-1]["text"]
    assert notes[-1]["text"] == "A young woman: slight, dark braided hair, a red ribbon."
    assert any("has since noted more about how it looks" in p for p in t.prompts if "appearance note for Mira" in p)
    assert set(notes[-1]["items"]) == {"bk.mira.portrait-1", "bk.mira.f9"} and "a10" in notes[-1]["sources"]
    assert il.ensure_note(0, "mira", "a11")[0] == note and len(notes) == 2            # asked once
    e6 = next(p for p in pics(sink) if p["target"] == "e6")            # the scene at a9... sees the note before the fill
    assert "a red ribbon" not in e6["prompt"]
    # nothing in the first note comes from after its anchor
    assert all(ordinal(d)[a] <= ordinal(d)[notes[0]["from"]] for a in notes[0]["sources"])


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
    il = art.Illustrator([art.Book(d1, bk1, s1, None), art.Book(d2, bk2, s2)], Text(), RefuseOrenOnce(), "ink", portraits="all", workers=1,
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
    il = art.Illustrator([art.Book(base_doc(bk), bk, Mem())], Text(), Image(), "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    j = il.job("plate", "One")
    assert j.event and j.event["id"] == "e2"


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
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], TwoWorlds(), Image(), "ink", portraits="all", workers=1,
                         log=lambda *_: None, pause=0)
    il.run()
    assert all(p["inherits"] == [] for p in pics(s1))
    assert pics(s2) and all(p["inherits"] == ["b1"] and not p.get("partial") for p in pics(s2))   # book one's world note
    assert WORLD in pics(s2, "plate")[0]["prompt"]


def test_a_reader_who_has_not_finished_the_earlier_book_gets_nothing_from_it():
    bk1, d1, bk2, d2 = series_docs()
    s1, s2 = Mem(), Mem()
    t, im = TwoWorlds(), Image()
    il = art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], t, im, "ink", portraits="all", workers=1, log=lambda *_: None, pause=0)
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
    art.Illustrator([art.Book(d1, bk1, s1), art.Book(d2, bk2, s2)], TwoWorlds(), im, "ink", portraits="all", workers=1, log=lambda *_: None,
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
    assert il._say("name in the text: GABRIEL", 0, "a9", None) == "a name the book has not used by this point"   # case-sensitive
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
    monkeypatch.setattr(art.Illustrator, "_assemble", lambda self, scene, notes, world, bi=-1, place="":
                        real(self, scene, notes, world, bi, place) + (" Oren" if scene.startswith(quiet[:20]) else ""))
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


GUILD_PARTS = ("a guild", ["white aprons dusted with flour"], ["stone houses by the river"])


def guild_doc(tie=False, settings=False):
    """The mill with a group: Oren is of the Guild (his own item says so), whose members' look and homes its profile states."""
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"].append({"id": "bk.oren.f1", "from": "a3", "basis": "stated", "evidence": ["a3"],
                                       "text": "A member of the Guild of millers who has run the mill for years."})
    d["entities"].append(ent("guild", "Guild", "group", frm="a2", facets=[portrait(
        "guild", "a2", "The Guild's members wear white aprons dusted with flour and live in stone houses by the river.")]))
    if tie:
        d["relations"] = [{"id": "rel1", "src": "oren", "dst": "guild", "label": "member of", "kind": "alliance", "from": "a3",
                           "basis": "stated", "evidence": ["a3"]}]
    if settings:
        d["events"][1]["setting"] = {"text": "the mill yard"}
        d["events"][3]["setting"] = {"text": "the mill", "place": "mill"}
        d["entities"][2]["facets"].append({"id": "bk.mill.f1", "from": "a6", "basis": "stated", "evidence": ["a6"],
                                           "text": "A grey stone mill with a wooden gate and a wheel on the river."})
    return bk, d


def pick_guild(who, notes, groups):
    n = next(int(n) for n, t in notes if "Guild" in t)
    return [{"group": ltr, "from": n, "until": 0} for ltr, name in groups if name.startswith("Guild")] if who == "Oren" else []


def test_a_persons_group_adds_its_members_look_and_no_name():
    bk, d = guild_doc()
    t = Text(notes={"Guild": GUILD_PARTS}, groups=pick_guild)
    il, rep, sink, t, im = run(d, bk, text_=t)
    [o] = [p for p in pics(sink, "portrait") if p["target"] == "oren"]
    assert NOTES["Oren"].rstrip(".") + ". Their people: white aprons dusted with flour." in o["prompt"]
    assert "Guild" not in o["prompt"] and o["appearance"].endswith("white aprons dusted with flour.")
    assert sink.m["groups"]["oren"]["picks"] == [{"group": "guild", "from": "a3", "until": None}]
    assert "a2" in o["sources"]                                  # the group note's own sources are the picture's
    picks = [p for p in t.prompts if "Which of the groups is" in p]
    assert [re.search(r"groups is (\w+)", p).group(1) for p in picks] == ["Oren"]   # no one else's items name a group: no call
    # a resume asks nothing again
    _, _, _, t2, _ = run(d, bk, sink, Text(notes={"Guild": GUILD_PARTS}, groups=pick_guild))
    assert not t2.prompts


def test_a_group_pick_is_kept_only_for_a_listed_group_named_in_the_persons_own_items():
    bk, d = guild_doc()
    d["entities"].append(ent("order", "Order of Owls", "group", frm="a2"))
    t = Text(notes={"Guild": GUILD_PARTS},
             groups=lambda who, notes, groups: [{"group": "Z"}, {"group": "A", "from": 99}, {"group": "B"}])
    il, rep, sink, t, im = run(d, bk, text_=t)
    asked = next(p for p in t.prompts if "Which of the groups is" in p)
    assert "Order of Owls" not in asked                          # Oren's items never name it: not even offered
    assert sink.m["groups"]["oren"]["picks"] == [{"group": "guild", "from": "a3", "until": None}]   # from: where his item names it
    # a tie to a visible group counts with no pick at all
    bk, d = guild_doc(tie=True)
    il, rep, sink, t, im = run(d, bk, text_=Text(notes={"Guild": GUILD_PARTS}))
    assert il.groups_of(0, "oren", "a5") == ["guild"] and il.groups_of(0, "oren", "a2") == []    # the tie is from a3


def test_membership_ends_where_the_pick_says_and_the_most_specific_groups_come_first():
    bk, d = guild_doc()
    d["entities"][1]["facets"].append({"id": "bk.oren.f2", "from": "a9", "basis": "stated", "evidence": ["a9"],
                                       "text": "Leaves the Guild and joins the Riders of the hills."})
    d["entities"].append(ent("riders", "Riders", "group", frm="a8"))
    d["entities"].append(ent("valley", "Valley folk", "group", frm="a1"))
    d["entities"][0]["facets"].append({"id": "bk.mira.f8", "from": "a2", "basis": "stated", "evidence": ["a2"],
                                       "text": "One of the Valley folk, as are all at the mill."})
    d["entities"][1]["facets"].append({"id": "bk.oren.f3", "from": "a3", "basis": "stated", "evidence": ["a3"],
                                       "text": "Valley folk born and bred."})

    def groups(who, notes, groups):
        num = {t[:20]: int(n) for n, t in notes}
        at = {name.split(" (")[0]: ltr for ltr, name in groups}
        leave = next(int(n) for n, t in notes if t.startswith("Leaves the Guild"))
        if who != "Oren":
            return [{"group": at["Valley folk"]}]
        return [{"group": at["Guild"], "until": leave}, {"group": at["Riders"], "from": leave}, {"group": at["Valley folk"]}]
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(groups=groups), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    assert il.groups_of(0, "oren", "a5") == ["guild", "valley"]            # the Guild has one member here, the Valley folk two
    assert il.groups_of(0, "oren", "a10") == ["riders", "valley"]          # he left the Guild at a9
    assert il.groups_of(0, "oren", "a1") == []                            # nothing he is a member of is shown yet


def test_a_group_note_written_from_content_after_the_anchor_is_refused():
    bk, d = guild_doc()
    sink = Mem()
    sink.m = {**art.blank("sha256:tbk", "ink"), "appearance": {"guild": [
        {"v": art.NOTE_VERSION, "from": "a3", "text": "A guild: blue sashes.", "role": "a guild",
         "details": [{"text": "blue sashes", "quote": "wear blue sashes", "seen": True}], "home": [], "sources": ["a10"], "inherits": [],
         "items": ["bk.guild.portrait-1"]}]}}
    il = art.Illustrator([art.Book(d, bk, sink)], Text(notes={"Guild": GUILD_PARTS}, groups=pick_guild), Image(), "ink",
                         workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    with pytest.raises(ValueError, match="written from content after its anchor"):
        il.make_prompt(il.job("portrait", "oren", at="a5"))


def test_a_portraits_background_is_its_figures_milieu_else_its_groups_home_else_plain():
    # (a) the setting of its events, the latest chapter weighing most, as the place's note when it links a place
    bk, d = guild_doc(settings=True)
    t = Text(notes={"Guild": GUILD_PARTS, "Mill": ("a mill", ["grey stone", "a wooden gate"])}, groups=pick_guild)
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    assert il.milieu(0, "oren", "a5")[0] == "the mill yard."
    text, srcs, _ = il.milieu(0, "oren", "a11")
    assert text == art.compose_note("a mill", ["grey stone", "a wooden gate"]) and "a6" in srcs
    pr = il.make_prompt(il.job("portrait", "oren", at="a5"))
    assert "Place: the mill yard." in pr.text and pr.milieu == "the mill yard." and art.BACKGROUND in pr.writer
    assert "a1" in pr.sources
    # (b) no setting: where the members of its groups live
    bk, d = guild_doc()
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(notes={"Guild": GUILD_PARTS}, groups=pick_guild), Image(), "ink",
                         workers=1, log=lambda *_: None, pause=0)
    il.world(0)
    pr = il.make_prompt(il.job("portrait", "oren", at="a5"))
    assert "Place: Their people's home: stone houses by the river." in pr.text
    # (c) neither: plain and neutral, never the opening's places
    pr = il.make_prompt(il.job("portrait", "mira", at="a5"))
    assert "Plain, neutral background." in pr.text and art.PLAIN in pr.writer and "Place:" not in pr.text


def test_a_scenes_place_is_its_settings_note():
    bk, d = guild_doc(settings=True)
    t = Text(notes={"Guild": GUILD_PARTS, "Mill": ("a mill", ["grey stone", "a wooden gate"])}, groups=pick_guild)
    il, rep, sink, t, im = run(d, bk, text_=t)
    two = next(p for p in pics(sink, "plate") if p["target"] == "Two")      # its moment, e4, is set at the mill
    assert "Place: " + art.compose_note("a mill", ["grey stone", "a wooden gate"]) in two["prompt"]
    writer = next(p for p in t.scenes() if "A hooded stranger comes to the mill" in p and "main moment" in p)
    assert "Place (added by the program" in writer


def test_the_world_note_is_an_era_note_and_an_old_one_is_written_again():
    il, rep, sink, t, im = run()
    asked = next(p for p in t.prompts if "visual world note" in p)
    assert "era note" in asked and "no landscape, architecture" in asked and sink.m["world"]["era"]
    assert all("World (true of every picture" not in p and "Era (the time, technology and materials true of every picture" in p
               for p in t.scenes())
    assert all("suits the world" not in p for p in t.scenes())
    old = Mem()
    old.m = {**art.blank("sha256:tbk", "ink"), "world": {"text": "Mines and tunnels everywhere.", "edited": False, "from": "a5",
                                                         "book": "bk", "sources": ["a5"]}}
    il, rep, old, t, im = run(sink=old)
    assert old.m["world"]["text"] == WORLD and old.m["worldOld"]["world"]["text"] == "Mines and tunnels everywhere."
    edited = Mem()
    edited.m = {**art.blank("sha256:tbk", "ink"), "world": {"text": "My own words.", "edited": True, "from": "a0"}}
    il, rep, edited, t, im = run(sink=edited)
    assert edited.m["world"]["text"] == "My own words." and not any("visual world note" in p for p in t.prompts)


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


def test_an_amended_note_replaces_the_detail_the_change_supersedes_quoting_the_change():
    # Darrow carved: rust-red hair becomes golden hair; the other details still hold
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append({"id": "bk.mira.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Her hair was cut short and dyed golden by the stranger."})

    def amend(prompt):
        n = next(int(m.group(1)) for m in re.finditer(r"^(\d+)\. (.*)$", prompt, re.M) if "braided" in m.group(2))
        return {"role": "", "drop": [n], "details": [{"text": "short golden hair", "quote": "hair was cut short and dyed golden", "seen": True}]}
    t = Text(change=lambda txt: "dyed golden" if "dyed golden" in txt else "", amend=amend)
    il, rep, sink, t, im = run(d, bk, text_=t)
    ms = [p for p in pics(sink, "portrait") if p["target"] == "mira"]
    assert [p["from"] for p in ms] == ["a5", "a10"] and ms[1]["references"] == [ms[0]["id"]]
    assert "dark braided hair" in ms[0]["appearance"] and "dark braided hair" not in ms[1]["appearance"]
    assert ms[1]["appearance"].startswith("The same young woman") and "short golden hair" in ms[1]["appearance"]
    assert "a brown shawl" in ms[1]["appearance"] and rep.dropped["replaced: look detail the change supersedes"] == 1
    # a drop with no new detail quoting the change drops nothing
    t = Text(change=lambda txt: "dyed golden" if "dyed golden" in txt else "",
             amend=lambda p: {"role": "", "drop": [1, 2], "details": [{"text": "a brown shawl", "quote": "brown shawl and", "seen": True}]})
    il, rep, sink, t, im = run(d, bk, text_=t)
    m2 = [p for p in pics(sink, "portrait") if p["target"] == "mira"][-1]
    assert "slight" in m2["appearance"] and "dark braided hair" in m2["appearance"]


def test_a_reader_who_has_not_finished_the_earlier_book_gets_no_group_only_it_has():
    bk1, d1, bk2, d2 = series_docs()
    d1["entities"].append(ent("guild", "Guild", "group", frm="a2", facets=[portrait(
        "guild", "a2", "The Guild's members wear white aprons dusted with flour and live in stone houses by the river.")]))
    d2["entities"][1]["facets"].append({"id": "b2.oren.f1", "from": "b2.a3", "basis": "stated", "evidence": ["b2.a3"],
                                        "text": "Still of the Guild of millers."})
    t = TwoWorlds(notes={"Guild": GUILD_PARTS}, groups=pick_guild)
    il = art.Illustrator([art.Book(d1, bk1, Mem()), art.Book(d2, bk2, Mem())], t, Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0)
    full = il.make_prompt(il.job("portrait", "oren", at="b2.a5", finished={"b1"}))
    assert "white aprons" in full.text and "b1" in full.inherits
    own = il.make_prompt(il.job("portrait", "oren", at="b2.a5", finished=set()))
    assert own.partial and "white aprons" not in own.text and own.inherits == []


def test_a_full_note_takes_no_new_detail_and_drops_none(monkeypatch):
    monkeypatch.setattr(art, "MAX_DETAILS", 2)
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append({"id": "bk.mira.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Wears a red ribbon in her dark braided hair at the market."})
    t = Text(notes={"Mira": ("a young woman", ["slight", "dark braided hair", "a red ribbon"])})
    il, rep, sink, t, im = run(d, bk, text_=t)
    first = sink.m["appearance"]["mira"][0]["text"]
    assert first == "A young woman: slight, dark braided hair."               # the third did not fit
    assert il.ensure_note(0, "mira", "a11")[0] == first                       # the fill keeps both, adds nothing


def test_the_look_index_and_the_group_pick_only_choose_and_write_nothing_a_prompt_uses():
    bk, d = guild_doc()
    d["entities"][0]["facets"].append({"id": "bk.mira.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Her hair was cut short and dyed golden by the stranger."})

    class Chatty(Text):
        def complete(self, system, prompt, schema, prefix=None):
            out = super().complete(system, prompt, schema, prefix)
            if "notes a reader's companion keeps on some entries" in prompt:
                for x in out["items"]:
                    x["change"] = "Zephyr the storm-king's crown" if x["change"] else ""
                    x["frame"] = "Zephyr rides in"
            if "Which of the groups is" in prompt:
                for x in out["groups"]:
                    x["why"] = "Zephyr says so"
            return out
    t = Chatty(notes={"Guild": GUILD_PARTS}, groups=pick_guild, change=lambda txt: "x" if "dyed golden" in txt else "")
    il, rep, sink, t, im = run(d, bk, text_=t, level="all")
    assert len([p for p in pics(sink, "portrait") if p["target"] == "mira"]) == 2      # the change still chose a version
    assert not any("Zephyr the storm-king" in p for p in t.prompts if "notes a reader's companion keeps" not in p)
    assert "Zephyr" not in json.dumps(sink.m)                # nothing the choosers said is stored, so nothing is served
    assert not any("Zephyr says so" in p or "Zephyr rides in" in p for p in t.prompts)
    assert all("Zephyr the storm-king" not in p["prompt"] for p in pics(sink))


def test_a_note_detail_with_a_name_the_book_has_not_used_goes_and_the_note_stays():
    # the Count's and Greyhand's notes were lost to one detail carrying a word the book had not used by the portrait's anchor
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"][0]["text"] += " He wears a Valdemar pin."
    t = Text(notes={"Oren": ("a miller", ["broad", "white hair", "a Valdemar pin"])})
    il, rep, sink, t, im = run(d, bk, text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: broad, white hair."
    assert rep.dropped["dropped: look detail with an unknown name"] == 1 and "oren" in {p["target"] for p in pics(sink, "portrait")}


def nested_groups_doc():
    """Oren in four groups, from the most specific: the Mill hands (white hair, which his own note already says), the Guild
    (white aprons), the Order of Owls (no member look) and the Valley folk (plain wool coats; Mira is of them too)."""
    bk, d = guild_doc()
    d["entities"][1]["facets"].append({"id": "bk.oren.f2", "from": "a3", "basis": "stated", "evidence": ["a3"],
                                       "text": "Leads the Mill hands, sits with the Order of Owls and is Valley folk born."})
    d["entities"][0]["facets"].append({"id": "bk.mira.f8", "from": "a2", "basis": "stated", "evidence": ["a2"],
                                       "text": "Valley folk, like all at the mill."})
    for gid, name, text_ in (("hands", "Mill hands", "The Mill hands all have white hair from the flour."),
                             ("owls", "Order of Owls", "The Order of Owls meets at night in secret."),
                             ("valley", "Valley folk", "The Valley folk wear plain wool coats.")):
        d["entities"].append(ent(gid, name, "group", frm="a2", facets=[portrait(gid, "a2", text_)]))
    notes = {"Guild": GUILD_PARTS, "Mill hands": ("a crew", ["white hair"]), "Order of owls": "",
             "Valley folk": ("a people", ["plain wool coats"])}

    def groups(who, items, groups):
        return [{"group": ltr} for ltr, _ in groups] if who == "Oren" else []
    return bk, d, notes, groups


def test_a_group_takes_a_look_slot_only_when_its_member_look_adds_to_the_figures():
    bk, d, notes, groups = nested_groups_doc()
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(notes=notes, groups=groups), Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0, portraits="all")
    assert il.groups_of(0, "oren", "a5") == ["guild", "hands", "owls", "valley"]   # one member each, then the Valley's two
    note, _, srcs, _ = il.figure_note(0, "oren", "a5")
    assert note == (NOTES["Oren"] + " Their people: white aprons dusted with flour."
                    " Their people: plain wool coats.")
    # the Mill hands' white hair is in his own note and the Owls show no look: neither takes a slot
    assert "white hair from" not in note and note.count("Their people:") == 2
    monkey = art.MAX_GROUPS
    try:
        art.MAX_GROUPS = 1
        assert il.figure_note(0, "oren", "a5")[0].count("Their people:") == 1
    finally:
        art.MAX_GROUPS = monkey


def test_group_names_are_taken_out_of_a_role_not_replaced():
    bk, d = guild_doc()
    t = Text(notes={"Guild": GUILD_PARTS, "Oren": ("a Guild miller", ["broad"])}, groups=pick_guild)
    il, rep, sink, t, im = run(d, bk, text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: broad."
    t = Text(notes={"Guild": ("a guild", ["white aprons dusted with flour", "Guild"], ["stone houses by the river"])},
             groups=pick_guild)
    il, rep, sink, t, im = run(d, bk, text_=t)
    assert sink.m["appearance"]["guild"][0]["text"] == "White aprons dusted with flour."      # "Guild" alone is no detail
    n = art._Names([("Gryffindor", True), ("OPA", True), ("the Silver Order", True), ("Slytherin", True), ("Red", True),
                    ("Gold", True), ("Mira", False)])
    assert n.drop_groups("a first-year Gryffindor") == "a first-year"
    assert n.drop_groups("Head of OPA security") == "Head of security"
    assert n.drop_groups("a frère of the Silver Order") == "a frère"
    assert n.drop_groups("a Gryffindor's prefect") == "a prefect"
    assert n.drop_groups("A first-year gryffindor student") == "A first-year student"     # any case
    assert n.drop_groups("an OPA agent") == "an agent"
    assert n.drop_groups("a golden-haired miller") == "a golden-haired miller"            # a word, not the name
    assert n.drop_groups("a Red digger") == "a digger"                                    # in a role every group name goes
    # in a detail a group name that is an ordinary word stays, as the word (Red Rising's castes are colours)
    assert n.drop_groups("a huge Slytherin serpent", words=True) == "a huge serpent"
    assert n.drop_groups("Red Sigils on the hands", words=True) == "red Sigils on the hands"
    assert n.drop_groups("gold hair", words=True) == "gold hair" and n.drop_groups("Gold-class boy", words=True) == "gold-class boy"
    assert n.drop_groups("Gold", words=True) == "gold" and n.drop_groups("Mira's shawl", words=True) == "Mira's shawl"


def test_a_figure_whose_look_changed_after_joining_a_group_takes_no_look_from_it():
    # Darrow carved into a Gold is never given Red looks: his own amended note wins
    bk, d = guild_doc()
    d["entities"][1]["facets"].append({"id": "bk.oren.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Oren lost an eye in the storm."})
    t = Text(notes={"Guild": GUILD_PARTS}, groups=pick_guild)
    il, rep, sink, t, im = run(d, bk, text_=t)
    ports = sorted((p for p in pics(sink, "portrait") if p["target"] == "oren"), key=lambda p: ordinal(d)[p["from"]])
    assert [p["from"] for p in ports] == ["a5", "a10"]
    assert "white aprons" in ports[0]["appearance"] and "white aprons" not in ports[1]["appearance"]
    assert sink.m["appearance"]["oren"][-1]["changed"] == [["bk", "a10"]]



# ---- review round 1


def test_a_detail_the_writer_does_not_call_seen_is_no_look():
    class T(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "appearance note for Mira" in prompt:
                self.prompts.append(prompt)
                return {"role": "a woman", "role_look": [], "details": [
                    {"text": "slight", "quote": "A slight young"},                        # no "seen": no look
                    {"text": "a curious soul", "quote": "A slight young", "seen": False},
                    {"text": "dark braided hair", "quote": "with dark braided hair", "seen": True}]}
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(text_=T())
    assert sink.m["appearance"]["mira"][0]["text"] == "A woman: dark braided hair."
    assert rep.dropped["dropped: not a look detail"] == 2
    assert art.NOTE_SCHEMA["required"] == ["role", "role_look", "details"] and "seen" in art._DETAILS["items"]["required"]


def test_a_group_membership_never_starts_before_the_item_that_names_the_group():
    # the model read a later item and says "from: 1"; item 1 never names the Guild
    bk, d = guild_doc()
    t = Text(notes={"Guild": GUILD_PARTS}, groups=lambda who, items, groups: [{"group": "A", "from": 1, "until": 1}] if who == "Oren" else [])
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0, portraits="all")
    assert il.items(0, "oren")[0]["from"] == "a1" and "Guild" not in il.items(0, "oren")[0]["text"]
    assert il.group_picks(0, "oren") == [{"group": "guild", "from": "a3", "until": None}]     # an end before the start: none
    assert il.groups_of(0, "oren", "a2") == [] and il.groups_of(0, "oren", "a3") == ["guild"]
    # a later "from" than the evidence is kept
    d["entities"][1]["facets"].append({"id": "bk.oren.f5", "from": "a7", "basis": "stated", "evidence": ["a7"],
                                       "text": "Takes the Guild's oath at last."})
    n = next(i for i, it in enumerate(art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink").items(0, "oren"), 1)
             if it["from"] == "a7")
    t = Text(notes={"Guild": GUILD_PARTS}, groups=lambda who, items, groups: [{"group": "A", "from": n}] if who == "Oren" else [])
    il = art.Illustrator([art.Book(d, bk, Mem())], t, Image(), "ink", workers=1, log=lambda *_: None, pause=0, portraits="all")
    assert il.group_picks(0, "oren")[0]["from"] == "a7"


def test_red_rising_shapes_keep_their_colour_words_in_details_and_lose_the_name_in_roles():
    bk, d = guild_doc()
    for gid, name in (("red", "Red"), ("gold", "Gold")):
        d["entities"].append(ent(gid, name, "group", frm="a1"))
    d["entities"][1]["facets"][0]["text"] = ("The old Red miller, broad, with Red sigils on his hands, gold hair and a leather "
                                             "apron, who runs the mill.")
    t = Text(notes={"Oren": ("a Red miller", ["Red sigils on his hands", "gold hair", "broad"])})
    il, rep, sink, t, im = run(d, bk, text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: red sigils on his hands, gold hair, broad."


def test_a_look_item_cut_from_a_note_by_the_budget_is_left_for_the_next_fill(monkeypatch):
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append({"id": "bk.mira.f9", "from": "a4", "basis": "stated", "evidence": ["a4"],
                                       "text": "Wears a red ribbon in her dark braided hair at the well."})
    monkeypatch.setattr(art, "NOTE_ITEMS_CHARS", 70)      # room for the newest look item only
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(notes={"Mira": ("a young woman", ["slight", "a red ribbon"])}), Image(),
                         "ink", workers=1, log=lambda *_: None, pause=0, portraits="all")
    il.world(0)
    il.ensure_note(0, "mira", "a5")
    first = il.man[0]["appearance"]["mira"][0]
    assert first["items"] == ["bk.mira.f9"]                    # the profile at a4 was not sent: not marked as done
    il.ensure_note(0, "mira", "a6")
    assert "bk.mira.portrait-1" in il.man[0]["appearance"]["mira"][-1]["items"]


def test_an_amend_drops_a_detail_only_for_a_quote_from_the_change_item_and_its_paragraphs():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][0]["facets"].append({"id": "bk.mira.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                       "text": "Her hair was cut short and dyed golden by the stranger."})
    # the quote is from her profile (a recent look paragraph), not the change: nothing is dropped
    t = Text(change=lambda txt: "dyed golden" if "dyed golden" in txt else "",
             amend=lambda p: {"role": "", "role_look": [], "drop": [1, 3], "details": [
                 {"text": "dark braided hair", "quote": "with dark braided hair", "seen": True}]})
    il, rep, sink, t, im = run(d, bk, text_=t)
    last = sink.m["appearance"]["mira"][-1]
    assert [x["text"] for x in last["details"]][:2] == ["slight", "of about twenty"] and not rep.dropped["replaced: look detail the change supersedes"]


def test_a_look_change_in_an_earlier_finished_book_keeps_a_group_look_out_of_a_later_one():
    bk1, d1, bk2, d2 = series_docs()
    d1["entities"].append(ent("guild", "Guild", "group", frm="a2", facets=[portrait(
        "guild", "a2", "The Guild's members wear blue sashes and live in stone houses by the river.")]))
    d1["entities"][1]["facets"] += [{"id": "bk.oren.f1", "from": "a3", "basis": "stated", "evidence": ["a3"],
                                     "text": "A member of the Guild of millers."},
                                    {"id": "bk.oren.f9", "from": "a10", "basis": "stated", "evidence": ["a10"],
                                     "text": "Oren lost an eye in the storm."}]
    d2["entities"][1]["facets"].append({"id": "b2.oren.f1", "from": "b2.a3", "basis": "stated", "evidence": ["b2.a3"],
                                        "text": "Still of the Guild of millers."})
    guild = ("a guild", ["blue sashes"], [])
    il = art.Illustrator([art.Book(d1, bk1, Mem()), art.Book(d2, bk2, Mem())], TwoWorlds(notes={"Guild": guild}, groups=pick_guild),
                         Image(), "ink", workers=1, log=lambda *_: None, pause=0, portraits="all")
    assert "blue sashes" in il.figure_note(0, "oren", "a5")[0]
    il.figure_note(0, "oren", "a10", change="Oren lost an eye in the storm.")
    assert il.man[0]["appearance"]["oren"][-1]["changed"] == [["b1", "a10"]]
    assert "blue sashes" not in il.figure_note(1, "oren", "b2.a5", finished={"b1"})[0]


def test_the_most_specific_group_is_counted_from_what_has_been_read():
    bk, d, notes, groups = nested_groups_doc()
    # the Guild grows later: Gabriel's item at a8 names it; before a8 it still has one member
    d["entities"][4]["facets"].append({"id": "bk.gabriel.f1", "from": "a8", "basis": "stated", "evidence": ["a8"],
                                       "text": "Rides for the Guild of millers."})
    d["entities"][4]["facets"].append({"id": "bk.gabriel.f2", "from": "a9", "basis": "stated", "evidence": ["a9"],
                                       "text": "Joins the Guild and the Valley folk at the mill."})
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(notes=notes, groups=groups), Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0, portraits="all")
    assert il._member_count(0, "guild", "a5") == 1 and il._member_count(0, "guild", "a9") == 2


def test_a_look_check_that_fails_twice_skips_the_picture():
    class Down(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "List every phrase of the prompt" in prompt:
                self.prompts.append(prompt)
                raise llm.ProviderError("HTTP 503: down")
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(text_=Down())
    assert not im.calls and rep.skipped["skipped: text model failed"] == rep.skipped.total()


# ---- round 3: recall on plainly described looks


def intro_doc():
    """Vesna is described as she enters ("A tall woman with white hair...") one paragraph before the book names her; none of
    her index items says how she looks (Hermione in one Harry Potter index: "lots of bushy brown hair", nine paragraphs before
    her name)."""
    ch1 = CH1[:3] + ["A tall woman with white hair and a black shawl stood waiting at the gate of the mill.",
                     "Vesna had walked all the way from the town across the river that same morning."] + CH1[3:]
    bk = book(("One", ch1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"].append(ent("vesna", "Vesna", frm="a4", facets=[portrait("vesna", "a4", "A woman who walked from the town.")]))
    return bk, d


def test_a_figure_described_as_it_enters_before_its_name_gets_that_look():
    bk, d = intro_doc()

    def intro(prompt):
        paras = dict(re.findall(r"^(\d+)\. (.*)$", prompt.split("Where the name first appears")[0], re.M))
        return [int(n) for n, t in paras.items() if "white hair" in t] + [99]          # 99 is not a paragraph sent
    t = Text(notes={"Vesna": ("a woman", ["tall", "white hair", "a black shawl"])}, intro=intro)
    il, rep, sink, t, im = run(d, bk, text_=t)
    assert sink.m["appearance"]["vesna"][0]["text"] == "A woman: tall, white hair, a black shawl."
    assert sink.m["intro"]["vesna"]["paragraphs"] == [3] and "a3" in sink.m["appearance"]["vesna"][0]["sources"]
    asked = next(p for p in t.prompts if "describes someone as they enter" in p and "Vesna is first named" in p)
    assert "Vesna had walked" in asked.split("Where the name first appears")[1] and "Gabriel" not in asked
    # not picked: the paragraph that does not name her is not her own text, and the look is not quoted from it
    _, rep2, sink2, _, _ = run(d, bk, text_=Text(notes={"Vesna": ("a woman", ["tall", "white hair", "a black shawl"])}))
    assert "vesna" not in sink2.m["appearance"] or not sink2.m["appearance"]["vesna"][0]["text"]
    # a resume asks nothing again
    _, _, _, t3, _ = run(d, bk, sink, Text(notes={"Vesna": ("a woman", ["tall", "white hair", "a black shawl"])}, intro=intro))
    assert not any("describes someone as they enter" in p and "Vesna" in p for p in t3.prompts)


def test_a_detail_matches_its_quote_across_a_hyphenated_compound_and_a_final_e():
    # Uncle Vernon's "purple face" on "purple-faced"; Hermione's "bushy hair" on "a bushy-haired girl"
    assert art.detail_supported("purple face", "purple-faced") and art.detail_supported("bushy hair", "a bushy-haired girl")
    assert art.detail_supported("a pale face", "his face was pale") and not art.detail_supported("a scar", "a scarlet cloak")
    assert not art.detail_supported("pale skin", "the pale light") and not art.detail_supported("a big man", "many big hats")


def test_a_detail_worded_beyond_its_short_quote_becomes_the_quote():
    # "big, beefy build" on "A big, beefy man": the writer's "build" is not in the book, the quote is
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"][0]["text"] = "A big, beefy man with hardly any neck, who runs the mill."

    def reply(prompt):
        return {"role": "a miller", "role_look": [], "details": [
            {"text": "big, beefy build", "quote": "A big, beefy man", "seen": True},
            {"text": "a thick red neck", "quote": "A big, beefy man with hardly any neck, who runs the mill and", "seen": True}]}

    class T(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "appearance note for Oren" in prompt:
                self.prompts.append(prompt)
                return reply(prompt)
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(d, bk, text_=T())
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: a big, beefy man."
    assert rep.dropped["fixed: look detail in the book's own words"] == 1
    assert rep.dropped["dropped: look detail not in the text"] == 1      # a long quote that does not hold "thick red" goes



def test_a_one_word_look_quoted_from_the_figures_own_paragraphs_holds():
    # Dumbledore's "tall" and "thin", quoted one word each from "He was tall, thin, and very old"
    assert art.quoted("tall", "He was tall, thin, and very old.", one=True) and not art.quoted("tall", "He was tall.")
    assert not art.quoted("the", "the man", one=True) and not art.quoted("tallow", "He was tall.", one=True)
    t = Text(notes={"Oren": ("a miller", ["old", "broad"])})
    real = t.complete

    def complete(system, prompt, schema, prefix=None):
        out = real(system, prompt, schema, prefix)
        if "appearance note for Oren" in prompt:
            for d in out["details"]:
                d["quote"] = d["text"]                    # one word each, as Flash quoted Dumbledore's
        return out
    t.complete = complete
    il, rep, sink, t, im = run(text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: old, broad."



def test_a_detail_loses_a_capital_it_has_only_from_opening_a_sentence_and_the_fallback_takes_no_spoken_line():
    assert art._plain("Flamboyant robes", "he wore flamboyant robes") == "flamboyant robes"
    assert art._plain("Watch with planets") == "watch with planets" and art._plain("A big man") == "a big man"
    assert art._plain("Gryffindor robes", "the Gryffindor robes") == "Gryffindor robes"
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    d["entities"][1]["facets"][0]["text"] = "You are wearing our colours, said the old miller, broad and white haired."

    class T(Text):
        def complete(self, system, prompt, schema, prefix=None):
            if "appearance note for Oren" in prompt:
                self.prompts.append(prompt)
                return {"role": "a miller", "role_look": [], "details": [
                    {"text": "wears the valley colours", "quote": "You are wearing our colours", "seen": True},
                    {"text": "broad", "quote": "broad", "seen": True}]}
            return super().complete(system, prompt, schema, prefix)
    il, rep, sink, t, im = run(d, bk, text_=T())
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: broad."



def test_a_one_word_look_padded_with_a_kind_word_stands_as_the_book_words_it():
    # Holden's "tall build" and "lean build" on "tall", "lean"; Uncle Vernon's "large build" on "large"
    assert art._beyond("tall build", "tall") and art._beyond("big, beefy build", "A big, beefy man")
    assert not art._beyond("pale skin", "the pale light") and not art._beyond("dark hair", "dark eyes")
    t = Text(notes={"Oren": ("a miller", ["old", "broad"])})
    real = t.complete

    def complete(system, prompt, schema, prefix=None):
        out = real(system, prompt, schema, prefix)
        if "appearance note for Oren" in prompt:
            out["details"] = [{"text": "broad build", "quote": "broad", "seen": True},
                              {"text": "old age", "quote": "The old", "seen": True}]
        return out
    t.complete = complete
    il, rep, sink, t, im = run(text_=t)
    assert sink.m["appearance"]["oren"][0]["text"] == "A miller: broad, old."
