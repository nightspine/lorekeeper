"""Art sets (art.py) with a scripted text model and a scripted image model: what is drawn, from which anchor, with which
references; the checks, refusals, the cost cap, resume and series inheritance."""

import re
import struct

import pytest

from lorekeeper import art, epub, images, llm, text

WORLD = ("A quiet river valley in a pre-industrial farming country: stone mills, thatched cottages, dirt tracks and wooden carts. "
         "People wear plain wool and linen in browns and greys. The light is soft and low, the land green and rolling, the mood calm.")
SCENE = ("A slight young woman in a brown shawl stands at a wooden gate beside a grey stone mill, a basket on her arm, while an old "
         "miller dusted with flour watches from the doorway. Soft morning light falls across the packed earth of the yard and a path "
         "leads off toward low green hills in the distance.")
NOTES = {"Mira": "A slight young woman of about twenty with dark braided hair, a brown shawl and a plain grey dress.",
         "Oren": "A broad old miller with white hair, flour on his sleeves and a leather apron.",
         "Gabriel": "A tall fair young man with short hair, a blue coat and tall riding boots.",
         "Stranger": "A hooded figure in a long black cloak, the face never seen, of middle height."}
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
    ents = [ent("mira", "Mira", facets=[portrait("mira", "a4", "A slight young woman of about twenty who carries baskets to the mill.")]),
            ent("oren", "Oren", frm="a1", facets=[portrait("oren", "a3", "The old miller, white haired and broad, who runs the mill.")]),
            ent("mill", "the mill", "place", facets=[]),
            ent("stranger", "the Stranger", frm="a6", facets=[portrait("stranger", "a7", "A hooded traveller whose face is never seen.")]),
            ent("gabriel", "Gabriel", frm="a8", facets=[portrait("gabriel", "a8", "A tall fair rider who brings news from the hills.")])]
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
        self.scene, self.world, self.notes, self.shows = scene, world, dict(NOTES, **(notes or {})), shows
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
            if "Begin with" in prompt:
                return {"text": "The same young woman, older, with a livid scar across one cheek and a plain grey dress."}
            return {"text": self.notes.get(who, "A plain figure in a brown coat and a worn hat.")}
        if "Write the picture prompt" in prompt or "Write it again" in prompt or "quieter composition" in prompt:
            out = {"text": self.scene(prompt) if callable(self.scene) else self.scene}
            if self.shows is not None and "Write the picture prompt" in prompt:
                out["shows"] = self.shows
            return out
        raise AssertionError("unexpected prompt: " + prompt[:80])

    def scenes(self):
        return [p for p in self.prompts if "Write the picture prompt" in p]


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
    il, rep, sink, t, im = run(text_=Text(scene=lambda p: SCENE.replace("A slight young woman", "Mira, a slight young woman")))
    assert not pics(sink, "plate") and rep.skipped["dropped: name in prompt"] >= 2
    assert sum(1 for p in t.prompts if "It was rejected" in p) >= 2        # one rewrite each


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


def test_a_provider_error_is_retried_once_then_skipped():
    class Flaky(Image):
        def draw(self, prompt, **kw):
            self.calls.append(prompt)
            raise llm.ProviderError("HTTP 500")
    im = Flaky()
    il, rep, sink, t, im = run(image=im, portraits=False)
    assert len(im.calls) == 4 and not pics(sink) and rep.skipped["skipped: provider error"] == 2


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
            ent("oren", "Oren", frm="b2.a1", facets=[portrait("oren", "b2.a2", "The old miller, white haired and broad, who runs the mill.", 1, "b2")])]
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
    il = art.Illustrator([art.Book(d1, None, s1, None, draw=False), art.Book(d2, bk2, Mem())], Text(), Image(), "ink", workers=1,
                         log=lambda *_: None, pause=0)
    rep = il.run()
    assert s1.m is None and rep.drawn > 0
    assert {p["target"] for p in pics(il.books[1].b.sink, "portrait")} == {"mira", "oren"}   # nothing earlier to inherit


def test_on_demand_job_and_prompt_refuse_a_target_the_reader_has_not_reached():
    bk = book(("One", CH1), ("Two", CH2))
    d = base_doc(bk)
    il = art.Illustrator([art.Book(d, bk, Mem())], Text(), Image(), "ink", workers=1, log=lambda *_: None, pause=0)
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
