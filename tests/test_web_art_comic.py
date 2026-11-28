"""Comic pages in the web app: the prompt, draw, soften and redraw routes accept the `comic` kind, always at the reader's
position, drawn with the scene image model, with `panels` in every answer. Scripted models (tests/test_art.py)."""

import re

import pytest

pytest.importorskip("fastapi")

from lorekeeper import art  # noqa: E402

from .test_art import PANEL  # noqa: E402

from .test_web_art import GEMINI, KEY, env, series_env, short_world  # noqa: E402,F401

CH = "Chapter Two"     # e4 (a turning point), e5, e6


def anchor_of(e, eid: str) -> str:
    return next(x for x in e.doc["events"] if x["id"] == eid)["from"]


def ask(e, route: str, **body):
    return e.c.post(f"/api/library/{KEY}/art/{route}", json={"kind": "comic", "target": CH, "style": "ink", **body})


def test_a_comic_prompt_is_of_the_moments_reached_with_its_panels(env):
    env.key()
    r = ask(env, "prompt", p=1.0)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["kind"] == "comic" and got["target"] == CH and got["panels"] == ["e4", "e5", "e6"]
    assert got["shape"] == "portrait" and got["noText"] == f"{art.COMIC_NO_TEXT} {art.LABEL_NOT_TEXT}" and got["prompt"].endswith(art.LABEL_NOT_TEXT)
    assert got["body"].startswith(art.comic_frame(3)) and got["anchor"] == anchor_of(env, "e6") and env.image.calls == []
    # the current chapter so far: only the moments up to the position, shown from the last of them
    got = ask(env, "prompt", p=anchor_of(env, "e5")).json()
    assert got["panels"] == ["e4", "e5"] and got["anchor"] == anchor_of(env, "e5") and "silver coin" not in got["prompt"]
    # a scene's answer says it has no panels
    ev = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "event", "target": "e5", "style": "ink"}).json()
    assert ev["panels"] is None


def test_a_comic_needs_the_position_and_two_moments_reached(env):
    env.key()
    for route in ("prompt", "draw", "soften"):
        r = ask(env, route, prompt="A page.", text="A page.", reason="IMAGE_SAFETY")
        assert r.status_code == 422 and r.json()["code"] == "bad-position", route
    r = ask(env, "prompt", p=anchor_of(env, "e4"))                    # one moment of the chapter reached
    assert r.status_code == 422 and r.json()["code"] == "too-few-moments"
    r = ask(env, "prompt", p=anchor_of(env, "e2"))                    # none of it reached
    assert r.status_code == 422 and r.json()["code"] == "not-visible"
    r = ask(env, "draw", p=anchor_of(env, "e4"), prompt="A page.")
    assert r.status_code == 422 and r.json()["code"] == "too-few-moments" and env.image.calls == []
    assert ask(env, "prompt", p=1.0, target="Chapter Nine").status_code == 404
    r = env.c.post(f"/api/library/{KEY}/art/prompt", json={"kind": "strip", "target": CH, "style": "ink"})
    assert r.status_code == 422 and "comic" in r.json()["detail"]


def test_a_comic_is_drawn_with_the_scene_image_model(env):
    env.key()
    # a portrait image model chosen for portraits, places and things never draws a page
    got = ask(env, "prompt", p=1.0, portraitProvider="gemini", portraitModel="gemini-3-pro-image").json()
    assert got["model"] == GEMINI


def test_a_comic_is_drawn_and_served_with_its_panels_and_never_moved_to_the_position(env):
    env.key()
    at = anchor_of(env, "e5")
    got = ask(env, "prompt", p=at).json()
    n = len(env.text.prompts)
    edited = "Rain falls on every panel. " + got["prompt"]
    # an edited text and a position further on: a page stays at its last panel's moment (only entities move)
    r = ask(env, "draw", p=at, prompt=edited)
    assert r.status_code == 200, r.text
    rec = r.json()
    assert len(env.text.prompts) == n                        # the cached prompt is reused, no second call
    assert rec["kind"] == "comic" and rec["panels"] == ["e4", "e5"] and rec["from"] == at and rec["shape"] == "portrait"
    assert env.image.calls[-1]["shape"] == "portrait" and env.image.calls[-1]["prompt"] == " ".join(edited.split())
    served = next(p for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"] if p["id"] == rec["id"])
    assert served["panels"] == ["e4", "e5"]
    # a style change reuses the page's prompt and draws into the other set, panels kept
    other = ask(env, "draw", p=at, style="watercolour", prompt=got["body"] + " " + art.style_sentence("watercolour") + " "
                + got["noText"]).json()
    assert other["panels"] == ["e4", "e5"] and len(env.text.prompts) == n


def test_a_comic_takes_the_place_of_a_comic_of_the_same_chapter_with_undo(env):
    env.key()
    at = anchor_of(env, "e5")
    first = ask(env, "draw", p=at, prompt=ask(env, "prompt", p=at).json()["prompt"]).json()
    r = ask(env, "draw", p=1.0, prompt="Rain on the mill, in two panels.", replaces=first["id"])
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["replaces"] == first["id"] and new["panels"] == ["e4", "e5", "e6"]
    ids = {p["id"] for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]}
    assert first["id"] not in ids and new["id"] in ids
    assert env.c.post(f"/api/library/{KEY}/undo/{new['undo']}").status_code == 200
    ids = {p["id"] for p in env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"]}
    assert first["id"] in ids and new["id"] not in ids
    # never a comic in place of another chapter's or another kind's picture, nor one past the position
    ev = env.c.post(f"/api/library/{KEY}/art/draw", json={"kind": "event", "target": "e5", "style": "ink",
                                                          "prompt": "A gate."}).json()
    r = ask(env, "draw", p=1.0, prompt="A page.", replaces=ev["id"])
    assert r.status_code == 422 and r.json()["code"] == "bad-replaces"
    later = ask(env, "draw", p=1.0, prompt="A page of three.").json()
    r = ask(env, "draw", p=at, prompt="A page.", replaces=later["id"])
    assert r.status_code == 422 and r.json()["code"] == "not-visible"


def test_a_refused_comic_is_softened_with_its_panels(env):
    env.key()
    at = anchor_of(env, "e5")
    got = ask(env, "prompt", p=at).json()
    r = ask(env, "soften", p=at, text=got["prompt"], reason="IMAGE_SAFETY")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["kind"] == "comic" and out["panels"] == ["e4", "e5"] and out["noText"] == f"{art.COMIC_NO_TEXT} {art.LABEL_NOT_TEXT}"
    assert out["body"].startswith(art.comic_frame(2)) and env.image.calls == []


def test_a_comic_redraw_keeps_its_panels_and_its_no_text_line(env):
    env.key()
    at = anchor_of(env, "e5")
    old = ask(env, "draw", p=at, prompt=ask(env, "prompt", p=at).json()["prompt"]).json()
    r = env.c.post(f"/api/library/{KEY}/art/{old['id']}/redraw", json={"note": "Make it night", "p": 1.0})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["kind"] == "comic" and new["panels"] == ["e4", "e5"] and new["replaces"] == old["id"]
    assert new["from"] == at and new["prompt"].startswith("Make it night. ") and new["prompt"].endswith(art.LABEL_NOT_TEXT)
    assert [f["label"] for f in new.get("figures") or []] == [f["label"] for f in old.get("figures") or []]


# ---- a page's anchor moves with the position: its text is filed where the server wrote it, or at the position


def after(e, eid: str) -> str:
    """The anchor just after an event's: a position past it, short of the book's end."""
    ids = [a["id"] for a in e.doc["anchors"]]
    return ids[ids.index(anchor_of(e, eid)) + 1]


def test_a_page_written_for_a_frontier_not_reached_is_never_drawn_or_softened_earlier(env):
    env.key()
    late = ask(env, "prompt", p=1.0).json()                          # e4, e5 and e6, shown from e6
    assert late["panels"] == ["e4", "e5", "e6"]
    restyled = f'{late["body"]} {art.style_sentence("watercolour")} {late["noText"]}'
    n = len(env.text.prompts)
    for text_ in (late["prompt"], restyled):
        r = ask(env, "draw", p=anchor_of(env, "e5"), prompt=text_)
        assert r.status_code == 422 and r.json()["code"] == "not-visible"
        r = ask(env, "soften", p=anchor_of(env, "e5"), text=text_, reason="IMAGE_SAFETY")
        assert r.status_code == 422 and r.json()["code"] == "not-visible"
    assert env.image.calls == [] and len(env.text.prompts) == n         # refused before any call
    assert env.c.get(f"/api/library/{KEY}/art?style=ink").json()["pictures"] == []
    # nor in the place of a page the reader can see
    at = anchor_of(env, "e5")
    first = ask(env, "draw", p=at, prompt=ask(env, "prompt", p=at).json()["prompt"]).json()
    r = ask(env, "draw", p=at, prompt=late["prompt"], replaces=first["id"])
    assert r.status_code == 422 and r.json()["code"] == "not-visible" and len(env.image.calls) == 1
    # a reader who has reached its last panel draws it as the page it is, from that panel
    rec = ask(env, "draw", p=after(env, "e6"), prompt=late["prompt"]).json()
    assert rec["from"] == anchor_of(env, "e6") and rec["panels"] == ["e4", "e5", "e6"]


def test_a_text_the_person_wrote_is_filed_at_the_position_never_earlier(env):
    env.key()
    mid = after(env, "e6")
    late = ask(env, "prompt", p=1.0).json()
    typed = "A storm comes over the mill roof and two figures run for the door. " + late["prompt"]   # edited: not the server's
    rec = ask(env, "draw", p=mid, prompt=typed).json()
    assert rec["from"] == mid and rec["panels"] == ["e4", "e5", "e6"]
    rec = ask(env, "draw", p=anchor_of(env, "e5"), prompt=typed).json()
    assert rec["from"] == anchor_of(env, "e5") and rec["panels"] == ["e4", "e5"]
    first = ask(env, "draw", p=mid, prompt=ask(env, "prompt", p=mid).json()["prompt"]).json()
    assert first["from"] == anchor_of(env, "e6")                     # the server's own text keeps its last panel's anchor
    r = ask(env, "draw", p=mid, prompt="Rain on the mill, panel after panel.", replaces=first["id"])
    assert r.status_code == 200 and r.json()["from"] == mid and r.json()["replaces"] == first["id"]
    out = ask(env, "soften", p=mid, text=typed, reason="IMAGE_SAFETY").json()
    assert out["anchor"] == mid and out["panels"] == ["e4", "e5", "e6"]


def test_a_page_kept_while_the_reader_moved_on_keeps_its_own_anchor_and_panels(env):
    env.key()
    at = anchor_of(env, "e5")
    early = ask(env, "prompt", p=at).json()                          # e4 and e5
    n = len(env.text.prompts)
    rec = ask(env, "draw", p=1.0, prompt=early["prompt"]).json()
    assert rec["from"] == at and rec["panels"] == ["e4", "e5"] and len(env.text.prompts) == n   # nothing written again
    other = f'{early["body"]} {art.style_sentence("watercolour")} {early["noText"]}'
    rec = ask(env, "draw", p=1.0, style="watercolour", prompt=other).json()
    assert rec["from"] == at and rec["panels"] == ["e4", "e5"] and len(env.text.prompts) == n
    # a soften of it rewrites the same page, and that reply is the same page when drawn further on
    out = ask(env, "soften", p=1.0, text=early["prompt"], reason="IMAGE_SAFETY").json()
    assert out["anchor"] == at and out["panels"] == ["e4", "e5"]
    rec = ask(env, "draw", p=after(env, "e6"), prompt=out["prompt"]).json()
    assert rec["from"] == at and rec["panels"] == ["e4", "e5"]
    # written for a frontier further on than its page (p 1.0, page at e6), drawn at e6: the page it is
    late = ask(env, "prompt", p=1.0).json()
    rec = ask(env, "draw", p=anchor_of(env, "e6"), prompt=late["prompt"]).json()
    assert rec["from"] == anchor_of(env, "e6") and rec["panels"] == ["e4", "e5", "e6"]


def test_a_page_written_for_more_finished_books_is_hidden_until_they_are_finished(series_env, monkeypatch):
    c, _ = series_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    at = next(x for x in c.get(f"/api/library/{KEY}").json()["events"] if x["id"] == "e5")["from"]   # book two's
    base = {"kind": "comic", "target": CH, "style": "ink", "p": at}
    p0 = c.post(f"/api/library/{KEY}/art/prompt", json={**base, "finished": []}).json()      # the sheet opens: book one unread
    # written with book one read, so its words may carry what only book one tells
    c.text.comic = lambda prompt: [PANEL + " A mill wheel turns in the winter moonlight."] * len(re.findall(r"<moment n=", prompt))
    p1 = c.post(f"/api/library/{KEY}/art/prompt", json={**base, "finished": ["one"]}).json()
    c.text.comic = None
    assert p0["anchor"] == p1["anchor"] == at and "one" not in p0["inherits"] and p1["body"] != p0["body"]
    n = len(c.text.prompts)
    for text_ in (p1["prompt"], f'{p1["body"]} {art.style_sentence("watercolour")} {p1["noText"]}'):
        rec = c.post(f"/api/library/{KEY}/art/draw", json={**base, "finished": [], "prompt": text_}).json()
        assert rec["from"] == at and rec["inherits"] == ["one"] and rec["partial"] is True    # hidden until book one is
    assert len(c.text.prompts) == n                          # a cache hit: nothing written again
    # this reader's own page keeps only what it carries
    own = c.post(f"/api/library/{KEY}/art/draw", json={**base, "finished": [], "prompt": p0["prompt"]}).json()
    assert own["inherits"] == p0["inherits"] and "one" not in own["inherits"]
