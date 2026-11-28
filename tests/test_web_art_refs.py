"""The person's own reference pictures in the web app (`refs`): the upload and its store (`ArtStore.put_ref`, pruning),
serving, `refs` on draw and redraw, `PUT .../refs` with undo, fix never sending them, remove-all taking `refs/`, and the
describe path for an image model that takes no pictures. The pipeline's side (`Illustrator.draw(user_refs=)`, the order,
the note) is faked here: these tests are about what the routes hand it and what they store. Scripted models; no network."""

import dataclasses
import json
import os
import time

import pytest

pytest.importorskip("fastapi")

from lorekeeper import art, epub, llm  # noqa: E402
from lorekeeper.web import art as store_mod  # noqa: E402
from lorekeeper.web import jobs as jobs_mod  # noqa: E402
from lorekeeper.web import styles as styles_mod  # noqa: E402

from .test_art import png  # noqa: E402
from .test_web_art import KEY, env, pictures, run_set, short_world, undo  # noqa: E402,F401


@dataclasses.dataclass
class FakeUserRef:
    data: bytes
    mime: str
    sha: str
    note: str
    description: str | None = None


@pytest.fixture
def refs(env, monkeypatch):
    """The pipeline's part as the contract has it: `draw(user_refs=)` files `userRefs` on the record. Every call's refs
    are kept in `refs.sent` (one list per draw)."""
    monkeypatch.setattr(art, "UserRef", getattr(art, "UserRef", FakeUserRef), raising=False)
    sent: list[list] = []
    orig = art.Illustrator.draw

    def draw(self, job, *args, user_refs=(), **kw):
        sent.append(list(user_refs))
        rec = orig(self, job, *args, **kw)
        if rec is not None and user_refs:
            rec["userRefs"] = [{"sha": r.sha, "ext": store_mod.REF_EXT[r.mime], "note": r.note,
                                **({"description": r.description} if r.description is not None else {})} for r in user_refs]
            for r in self.man[job.bi]["pictures"]:
                if r["id"] == rec["id"]:
                    r["userRefs"] = rec["userRefs"]
            self._save(job.bi)
        return rec
    monkeypatch.setattr(art.Illustrator, "draw", draw)
    env.sent = sent
    env.key()
    return env


def pic(n: int, side: int = 300) -> bytes:
    return png(side, side, b"ref" + str(n).encode())


def upload(e, data: bytes, key: str = KEY, name: str = "x.png"):
    return e.c.post(f"/api/library/{key}/art/refs", files={"file": (name, data, "application/octet-stream")})


def sha_of(e, data: bytes) -> str:
    r = upload(e, data)
    assert r.status_code == 200, r.text
    return r.json()["sha"]


DRAW = {"kind": "event", "target": "e5", "style": "ink", "prompt": "A gate at dusk."}


# ---- upload and serve


def test_an_upload_is_checked_by_its_bytes_kept_by_content_and_served(refs):
    e = refs
    data = pic(1)
    r = upload(e, data, name="photo.gif")       # the name says nothing: the bytes are a PNG
    assert r.status_code == 200, r.text
    got = r.json()
    assert set(got) == {"sha", "ext", "width", "height", "bytes", "url"}
    assert got["ext"] == "png" and got["width"] == got["height"] == 300 and got["bytes"] == len(data)
    assert got["url"] == f"/api/art/{e.hash}/refs/{got['sha']}"
    f = e.c.get(got["url"])
    assert f.status_code == 200 and f.content == data and f.headers["content-type"] == "image/png"
    assert f.headers["cache-control"] == "max-age=86400"
    again = upload(e, data).json()                                      # the same bytes: the same sha, no new file
    assert again["sha"] == got["sha"]
    assert [p.name for p in (e.data / "art" / e.hash / "refs").iterdir()] == [f"{got['sha']}.png"]


@pytest.mark.parametrize("data,status,code", [
    (b"GIF89a not a picture", 422, "bad-image"),
    (png(100, 100, b"small"), 422, "bad-image"),
    (b"", 422, "bad-image"),
])
def test_a_bad_upload_is_refused_with_the_sniff_sentence(refs, data, status, code):
    r = upload(refs, data)
    assert r.status_code == status and r.json()["code"] == code and r.json()["detail"]
    assert not (refs.data / "art" / refs.hash / "refs").exists() or not any((refs.data / "art" / refs.hash / "refs").iterdir())


def test_an_upload_over_8_mb_is_413(refs, monkeypatch):
    monkeypatch.setattr(styles_mod, "MAX_REF_BYTES", 1000)
    r = upload(refs, pic(1) + b"\0" * 2000)
    assert r.status_code == 413 and r.json()["code"] == "too-large"
    big = store_mod.ArtStore(refs.data)                         # the store's own limit is the styles one, imported
    with pytest.raises(styles_mod.StyleError) as err:
        big.put_ref(refs.hash, b"\x89PNG" + b"\0" * (store_mod.MAX_REF_BYTES + 1))
    assert err.value.status == 413


def test_serving_never_reaches_past_the_refs_folder(refs):
    e = refs
    s = sha_of(e, pic(1))
    assert e.c.get(f"/api/art/{e.hash}/refs/{'0' * 64}").status_code == 404
    assert e.c.get(f"/api/art/{e.hash}/refs/{s[:10]}").status_code == 404
    assert e.c.get(f"/api/art/{'ab' * 32}/refs/{s}").status_code == 404           # another edition
    assert e.c.get(f"/api/art/{e.hash}/refs/..%2Fbackground.json").status_code == 404
    with pytest.raises(KeyError):
        store_mod.ArtStore(e.data).folder(e.hash, "refs")                          # never a style set


def test_an_unknown_index_is_404_and_the_upload_route_needs_the_csrf_header(refs):
    e = refs
    assert upload(e, pic(1), key="examples--nope").status_code == 404
    r = e.c.post(f"/api/library/{KEY}/art/refs", files={"file": ("x.png", pic(1))}, headers={"X-Lorekeeper": ""})
    assert r.status_code == 403
    r = e.c.put(f"/api/library/{KEY}/art/p00000000/refs", json={"refs": []}, headers={"X-Lorekeeper": ""})
    assert r.status_code == 403


# ---- draw


def test_draw_hands_the_persons_pictures_to_the_pipeline_and_the_record_serves_them(refs):
    e = refs
    a, b = sha_of(e, pic(1)), sha_of(e, pic(2))
    r = e.c.post(f"/api/library/{KEY}/art/draw",
                 json={**DRAW, "refs": [{"sha": a, "note": "  use this   for her dress "}, {"sha": b}]})
    assert r.status_code == 200, r.text
    rec = r.json()
    sent = e.sent[-1]
    assert [x.sha for x in sent] == [a, b] and [x.note for x in sent] == ["use this for her dress", ""]
    assert sent[0].data == pic(1) and sent[0].mime == "image/png" and sent[0].description is None
    assert [x["sha"] for x in rec["userRefs"]] == [a, b] and rec["userRefs"][0]["note"] == "use this for her dress"
    assert rec["userRefs"][0]["url"] == f"/api/art/{e.hash}/refs/{a}"
    listed = next(p for p in pictures(e) if p["id"] == rec["id"])
    assert listed["userRefs"][1]["url"].endswith(b) and listed["references"] == rec["references"]


def test_a_draw_without_refs_sends_none(refs):
    r = refs.c.post(f"/api/library/{KEY}/art/draw", json=DRAW)
    assert r.status_code == 200 and refs.sent[-1] == [] and "userRefs" not in r.json()


def bad_refs(e):
    a, b = sha_of(e, pic(1)), sha_of(e, pic(2))
    shas = [sha_of(e, pic(n)) for n in range(3, 6)]
    return [
        [{"sha": a}, {"sha": b}, *({"sha": s} for s in shas)],     # five
        [{"sha": a}, {"sha": a}],                                    # the same twice
        [{"sha": a, "note": "x" * 121}],                             # a long note
        [{"sha": "0" * 64}],                                         # not uploaded
        [{"sha": "../a"}],
        [{"note": "no sha"}],
        [{"sha": a, "note": 3}],
        {"sha": a},
    ]


def test_bad_refs_are_refused_before_any_call(refs):
    e = refs
    for body in bad_refs(e):
        n_text, n_image = len(e.text.prompts), len(e.image.calls)
        r = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": body})
        assert r.status_code == 422 and r.json()["code"] == "bad-refs" and r.json()["detail"], body
        assert len(e.text.prompts) == n_text and len(e.image.calls) == n_image, body
    ok = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": [{"sha": bad_refs(e)[0][0]["sha"],
                                                                             "note": "x" * 120}]})
    assert ok.status_code == 200, ok.text


def test_the_prompt_route_never_takes_refs(refs):
    e = refs
    a = sha_of(e, pic(1))
    r = e.c.post(f"/api/library/{KEY}/art/prompt", json={**DRAW, "refs": [{"sha": a, "note": "her dress"}]})
    assert r.status_code == 200
    assert not any("her dress" in str(p) for p in e.text.prompts) and e.sent == []


# ---- redraw


def drawn_with_refs(e, *notes):
    shas = [sha_of(e, pic(n)) for n in range(1, len(notes) + 1)]
    r = e.c.post(f"/api/library/{KEY}/art/draw",
                 json={**DRAW, "refs": [{"sha": s, "note": n} for s, n in zip(shas, notes)]})
    assert r.status_code == 200, r.text
    return r.json(), shas


def test_a_redraw_reuses_the_records_refs_and_can_replace_or_drop_them(refs):
    e = refs
    rec, (a, b) = drawn_with_refs(e, "her dress", "the house")
    new = e.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={}).json()
    assert [(x.sha, x.note) for x in e.sent[-1]] == [(a, "her dress"), (b, "the house")]
    assert [x["sha"] for x in new["userRefs"]] == [a, b]
    c = sha_of(e, pic(9))
    newer = e.c.post(f"/api/library/{KEY}/art/{new['id']}/redraw", json={"refs": [{"sha": c, "note": "a hat"}]}).json()
    assert [(x.sha, x.note) for x in e.sent[-1]] == [(c, "a hat")] and [x["sha"] for x in newer["userRefs"]] == [c]
    none = e.c.post(f"/api/library/{KEY}/art/{newer['id']}/redraw", json={"refs": []}).json()
    assert e.sent[-1] == [] and "userRefs" not in none
    r = e.c.post(f"/api/library/{KEY}/art/{none['id']}/redraw", json={"refs": [{"sha": "0" * 64}]})
    assert r.status_code == 422 and r.json()["code"] == "bad-refs"


def test_a_draw_that_replaces_a_picture_sends_the_sheets_refs(refs):
    e = refs
    rec, (a,) = drawn_with_refs(e, "her dress")
    r = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "prompt": "A gate at night.", "replaces": rec["id"],
                                                        "refs": [{"sha": a, "note": "her coat"}]})
    assert r.status_code == 200, r.text
    assert r.json()["replaces"] == rec["id"] and r.json()["userRefs"][0]["note"] == "her coat"


# ---- PUT refs


def test_put_refs_changes_the_list_without_drawing_and_undo_puts_it_back(refs):
    e = refs
    rec, (a, b) = drawn_with_refs(e, "her dress", "the house")
    calls = len(e.image.calls)
    r = e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": [{"sha": b, "note": "the old house"}]})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["userRefs"] == [{"sha": b, "ext": "png", "note": "the old house", "url": f"/api/art/{e.hash}/refs/{b}"}]
    assert got["id"] == rec["id"] and got["file"] == rec["file"] and len(e.image.calls) == calls
    entry = json.loads((e.data / "merges" / got["undo"] / "entry.json").read_text())
    assert entry["kind"] == "art" and entry["art"]["op"] == "refs" and entry["art"]["id"] == rec["id"]
    assert next(p for p in pictures(e) if p["id"] == rec["id"])["userRefs"][0]["sha"] == b
    undo(e, got["undo"])
    back = next(p for p in pictures(e) if p["id"] == rec["id"])
    assert [(x["sha"], x["note"]) for x in back["userRefs"]] == [(a, "her dress"), (b, "the house")]
    gone = e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": []}).json()
    assert "userRefs" not in gone and "userRefs" not in next(p for p in pictures(e) if p["id"] == rec["id"])
    undo(e, gone["undo"])
    assert len(next(p for p in pictures(e) if p["id"] == rec["id"])["userRefs"]) == 2


def test_put_refs_is_checked(refs):
    e = refs
    rec, _ = drawn_with_refs(e, "her dress")
    for body in [{}, {"refs": "no"}, {"refs": [{"sha": "0" * 64}]}]:
        r = e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json=body)
        assert r.status_code == 422 and r.json()["code"] == "bad-refs", body
    assert e.c.put(f"/api/library/{KEY}/art/p00000000/refs", json={"refs": []}).status_code == 404
    early = e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": [], "p": "a2"})
    assert early.status_code == 422 and early.json()["code"] == "not-visible"


# ---- fix


def test_a_fix_never_sends_the_persons_pictures_and_keeps_them_on_the_picture(refs, monkeypatch):
    e = refs
    rec, (a,) = drawn_with_refs(e, "her dress")
    e.image.calls.clear()
    r = e.c.post(f"/api/library/{KEY}/art/{rec['id']}/fix", json={"faults": [{"what": "an extra hand", "where": "left"}]})
    assert r.status_code == 200, r.text
    assert len(e.image.calls) == 1 and len(e.image.calls[0]["references"]) == 1    # the picture itself only
    assert pic(1) not in e.image.calls[0]["references"]
    new = r.json()
    assert new["userRefs"][0]["sha"] == a and new["userRefs"][0]["url"].endswith(a)
    assert next(p for p in pictures(e) if p["id"] == new["id"])["userRefs"][0]["sha"] == a


# ---- the describe path (an image model that takes no pictures)


@pytest.fixture
def blind(refs, monkeypatch):
    """The drawing model takes no pictures; `blind.described` lists each `describe_reference` call."""
    monkeypatch.setattr(llm, "image_takes_references", lambda provider, model: False, raising=False)
    seer = object()
    monkeypatch.setattr(jobs_mod.JobManager, "describe_model", lambda self: seer)
    described = []

    def describe(model, data, mime, note):
        assert model is seer
        described.append((data, note))
        return f"a long blue coat with brass buttons ({len(described)})"
    monkeypatch.setattr(art, "describe_reference", describe, raising=False)
    refs.described = described
    return refs


def test_on_a_model_that_takes_no_pictures_each_is_described_once_and_reused(blind):
    e = blind
    rec, (a, b) = drawn_with_refs(e, "her coat", "")
    assert [n for _, n in e.described] == ["her coat", ""]
    assert [x.description for x in e.sent[-1]] == ["a long blue coat with brass buttons (1)",
                                                   "a long blue coat with brass buttons (2)"]
    assert rec["userRefs"][0]["description"].endswith("(1)")
    new = e.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={}).json()
    assert len(e.described) == 2 and [x.description for x in e.sent[-1]] == [x["description"] for x in rec["userRefs"]]
    again = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": [{"sha": a, "note": "her coat"}]})
    assert again.status_code == 200 and len(e.described) == 2              # the same picture and note: no new call
    other = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": [{"sha": a, "note": "her hat"}]})
    assert other.status_code == 200 and len(e.described) == 3              # another note asks something else of it
    kept = e.c.put(f"/api/library/{KEY}/art/{new['id']}/refs", json={"refs": [{"sha": a, "note": "her coat"}]}).json()
    assert kept["userRefs"][0]["description"].endswith("(1)")


def test_without_a_model_that_can_see_pictures_refs_on_a_blind_model_are_409(blind, monkeypatch):
    e = blind
    monkeypatch.setattr(jobs_mod.JobManager, "describe_model", lambda self: None)
    a = sha_of(e, pic(1))
    n_text, n_image = len(e.text.prompts), len(e.image.calls)
    r = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": [{"sha": a}]})
    assert r.status_code == 409 and r.json()["code"] == "no-vision-model"
    assert len(e.text.prompts) == n_text and len(e.image.calls) == n_image
    assert e.c.post(f"/api/library/{KEY}/art/draw", json=DRAW).status_code == 200      # drawn without them


def test_a_description_that_fails_is_502_and_nothing_is_drawn(blind, monkeypatch):
    e = blind

    def fail(model, data, mime, note):
        raise ValueError("could not describe")
    monkeypatch.setattr(art, "describe_reference", fail, raising=False)
    a = sha_of(e, pic(1))
    n = len(e.image.calls)
    r = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": [{"sha": a}]})
    assert r.status_code == 502 and r.json()["code"] == "model-failed" and len(e.image.calls) == n


# ---- the store: pruning and removal


def age(p, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(p, (t, t))


def test_pruning_takes_unlisted_uploads_older_than_a_day_and_keeps_the_rest(tmp_path):
    s = store_mod.ArtStore(tmp_path)
    h = "ab" * 32
    old, fresh, listed, held = (s.put_ref(h, pic(n)) for n in range(4))
    files = {k: s.ref_path(h, x["sha"]) for k, x in zip(("old", "fresh", "listed", "held"), (old, fresh, listed, held))}
    s.put(h, "ink", {"id": "p0000aaaa", "file": "p0000aaaa.png", "kind": "plate", "target": "One", "from": "a1",
                     "userRefs": [{"sha": listed["sha"], "ext": "png", "note": ""}]}, png(4, 3, b"x"))
    j = tmp_path / "merges" / "e1"
    j.mkdir(parents=True)
    (j / "entry.json").write_text(json.dumps({"kind": "art", "art": {"op": "delete", "removed": [
        {"id": "p1111bbbb", "userRefs": [{"sha": held["sha"]}]}]}}))
    for k in ("old", "listed", "held"):
        age(files[k], 30)
    age(files["fresh"], 2)
    assert s.prune_refs(h) == 1
    assert s.ref_path(h, old["sha"]) is None
    assert all(s.ref_path(h, x["sha"]) for x in (fresh, listed, held))
    again = s.put_ref(h, pic(1))                # the same bytes again renew the file's time
    age(s.ref_path(h, again["sha"]), 30)
    s.put_ref(h, pic(1))
    assert s.prune_refs(h) == 0


def test_pruning_runs_when_a_set_is_saved_and_when_the_store_opens(tmp_path):
    h = "cd" * 32
    s = store_mod.ArtStore(tmp_path)
    a, b = s.put_ref(h, pic(1)), s.put_ref(h, pic(2))
    age(s.ref_path(h, a["sha"]), 30)
    s.put(h, "ink", {"id": "p0000aaaa", "file": "p0000aaaa.png"}, b"one")       # a set saved
    assert s.ref_path(h, a["sha"]) is None and s.ref_path(h, b["sha"])
    age(s.ref_path(h, b["sha"]), 30)
    store_mod.ArtStore(tmp_path)                                                  # the store opens
    assert s.ref_path(h, b["sha"]) is None


def test_a_journal_held_upload_stays_until_its_undo_is_gone(refs):
    e = refs
    rec, (a,) = drawn_with_refs(e, "her dress")
    gone = e.c.delete(f"/api/library/{KEY}/art/{rec['id']}").json()
    s = store_mod.ArtStore(e.data)
    age(s.ref_path(e.hash, a), 30)
    assert s.prune_refs(e.hash) == 0                    # the delete's undo still needs it
    undo(e, gone["undo"])
    assert e.c.get(f"/api/art/{e.hash}/refs/{a}").status_code == 200


def test_removing_every_set_takes_refs_and_undo_brings_them_back(refs):
    e = refs
    rec, (a,) = drawn_with_refs(e, "her dress")
    listing = next(x for x in e.c.get("/api/art").json() if x["textHash"] == e.hash)
    assert listing["refsBytes"] == len(pic(1)) and listing["bytes"] >= sum(s["bytes"] for s in listing["styles"]) + len(pic(1))
    r = e.c.delete(f"/api/art/{e.hash}")
    assert r.status_code == 200
    assert not (e.data / "art" / e.hash / "refs").exists() and e.c.get(f"/api/art/{e.hash}/refs/{a}").status_code == 404
    undo(e, r.json()["undo"])
    assert e.c.get(f"/api/art/{e.hash}/refs/{a}").status_code == 200
    assert next(p for p in pictures(e) if p["id"] == rec["id"])["userRefs"][0]["sha"] == a


def test_removing_one_style_keeps_refs_for_the_others(refs):
    e = refs
    _, (a,) = drawn_with_refs(e, "her dress")
    assert e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "style": "watercolour"}).status_code == 200
    assert e.c.delete(f"/api/art/{e.hash}", params={"style": "ink"}).status_code == 200
    assert e.c.get(f"/api/art/{e.hash}/refs/{a}").status_code == 200


# ---- never shared, and allowed on every kind of index


def copy_index(e, folder: str, name: str) -> str:
    """The Millbrook index again under another library source (`generated`, `community`); its key."""
    d = e.data / folder
    d.mkdir(parents=True, exist_ok=True)
    doc = json.loads(e.index.read_text())
    if folder == "indexes":     # made shareable: the schema's provenance and work fields, no anchor on a heading
        doc.setdefault("provenance", {}).update(created="2026-11-01T00:00:00Z", license="CC0-1.0")
        doc.setdefault("work", {}).update(authors=["A. Writer"], language="en")
        blocks = epub.read(e.data.parent / "epubs" / "mill.epub").blocks
        short = {f"a{b.seq}" for b in blocks if len(b.words) < 6}
        doc["anchors"] = [a for a in doc["anchors"] if a["id"] not in short]
    (d / f"{name}.json").write_text(json.dumps(doc))
    src = folder.split("/")[0] if folder == "indexes" else "community"
    key = f"{'generated' if src == 'indexes' else 'community'}--{name}"
    assert any(x["key"] == key for x in e.c.get("/api/library").json()["indexes"]), key
    return key


def test_uploads_are_allowed_on_example_and_community_indexes(refs):
    e = refs
    for key in (KEY, copy_index(e, "community/indexes", "theirs")):
        r = upload(e, pic(7), key=key)
        assert r.status_code == 200, (key, r.text)
        assert r.json()["url"] == f"/api/art/{e.hash}/refs/{r.json()['sha']}"


def test_the_protocol_the_index_and_a_share_bundle_never_carry_them(refs):
    e = refs
    key = copy_index(e, "indexes", "mine")          # only an index built here can be shared
    a = sha_of(e, pic(1))
    r = e.c.post(f"/api/library/{key}/art/draw", json={**DRAW, "refs": [{"sha": a, "note": "her dress"}]})
    assert r.status_code == 200, r.text
    assert r.json()["userRefs"][0]["sha"] == a
    for path in ["/.well-known/lorekeeper", f"/v0/indexes/{key}", f"/v0/indexes/{key}/view?anchor=a5",
                 f"/api/library/{key}", f"/api/community/share/{key}/bundle"]:
        got = e.c.get(path)
        assert got.status_code == 200, (path, got.status_code, got.text[:200])
        assert a not in got.text and "userRefs" not in got.text, path
    assert a not in (e.data / "indexes" / "mine.json").read_text()


# ---- round 2: a file in use is never pruned; strict refs; what the sheet needs from /api/providers


def mtime_age(path) -> float:
    return time.time() - path.stat().st_mtime


def test_every_use_renews_an_upload_so_a_prune_never_takes_it(refs):
    e = refs
    s = store_mod.ArtStore(e.data)
    a = sha_of(e, pic(1))
    f = s.ref_path(e.hash, a)
    age(f, 30)
    assert e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": [{"sha": a}]}).status_code == 200
    assert mtime_age(f) < 60                                # checked for the draw: renewed
    rec = pictures(e)[-1]
    age(f, 30)
    assert e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": [{"sha": a, "note": "x"}]}).status_code == 200
    assert mtime_age(f) < 60                                # kept by PUT: renewed
    age(f, 30)
    assert e.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={}).status_code == 200
    assert mtime_age(f) < 60                                # reused by a redraw: renewed
    rec = pictures(e)[-1]
    gone = e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": []}).json()
    age(f, 30)
    undo(e, gone["undo"])
    assert mtime_age(f) < 60                                # brought back by undo: renewed
    assert s.prune_refs(e.hash) == 0 and s.ref_path(e.hash, a)


def test_a_journal_entry_that_cant_be_read_keeps_everything(tmp_path):
    s = store_mod.ArtStore(tmp_path)
    h = "ab" * 32
    a = s.put_ref(h, pic(1))
    age(s.ref_path(h, a["sha"]), 30)
    (tmp_path / "merges" / "half").mkdir(parents=True)       # being written, or being removed by an undo
    assert s.prune_refs(h) == 0 and s.ref_path(h, a["sha"])
    (tmp_path / "merges" / "half" / "entry.json").write_text("{not json")
    assert s.prune_refs(h) == 0 and s.ref_path(h, a["sha"])
    (tmp_path / "merges" / "half" / "entry.json").write_text("{}")
    assert s.prune_refs(h) == 1


def test_a_prune_tolerates_the_folder_going(tmp_path, monkeypatch):
    s = store_mod.ArtStore(tmp_path)
    h = "ab" * 32
    s.put_ref(h, pic(1))
    import shutil
    shutil.rmtree(s.refs_dir(h))
    assert s.prune_refs(h) == 0


def test_a_ref_that_goes_before_its_bytes_are_read_is_422_not_500(refs, monkeypatch):
    e = refs
    a = sha_of(e, pic(1))
    monkeypatch.setattr(store_mod.ArtStore, "ref_bytes", lambda self, h, sha: None)
    r = e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": [{"sha": a}]})
    assert r.status_code == 422 and r.json() == {"detail": "That reference picture is no longer here. Add it again.",
                                                 "code": "bad-refs"}


def test_refs_null_on_redraw_or_put_is_refused_and_only_an_absent_key_reuses(refs):
    e = refs
    rec, (a,) = drawn_with_refs(e, "her dress")
    n = len(e.image.calls)
    r = e.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={"refs": None})
    assert r.status_code == 422 and r.json()["code"] == "bad-refs" and len(e.image.calls) == n
    r = e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": None})
    assert r.status_code == 422 and r.json()["code"] == "bad-refs"
    assert e.c.post(f"/api/library/{KEY}/art/draw", json={**DRAW, "refs": None}).status_code == 200   # a draw: none


def test_a_redraw_uses_the_refs_a_put_left_on_the_picture(refs):
    e = refs
    rec, (a, b) = drawn_with_refs(e, "her dress", "the house")
    assert e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": [{"sha": b, "note": "only this"}]}).status_code == 200
    e.c.post(f"/api/library/{KEY}/art/{rec['id']}/redraw", json={})
    assert [(x.sha, x.note) for x in e.sent[-1]] == [(b, "only this")]


def test_providers_say_which_image_models_take_pictures_and_who_would_describe_them(refs, monkeypatch):
    e = refs
    got = e.c.get("/api/providers").json()
    gem = next(p for p in got["providers"] if p["id"] == "gemini")
    assert gem["images"]["models"] and all(m["references"] is True for m in gem["images"]["models"])
    assert "describe" in got
    monkeypatch.setattr(llm, "image_takes_references", lambda provider, model: model != gem["images"]["default"],
                        raising=False)

    class Seer:
        provider, name = "claude", "claude-sonnet-5-5"
    monkeypatch.setattr(jobs_mod.JobManager, "describe_model", lambda self: Seer())
    got = e.c.get("/api/providers").json()
    gem = next(p for p in got["providers"] if p["id"] == "gemini")
    flags = {m["id"]: m["references"] for m in gem["images"]["models"]}
    assert flags[gem["images"]["default"]] is False
    assert got["describe"] == {"provider": "claude", "model": "claude-sonnet-5-5"}
    monkeypatch.setattr(jobs_mod.JobManager, "describe_model", lambda self: None)
    assert e.c.get("/api/providers").json()["describe"] is None


def test_a_put_refs_undo_entry_holds_the_uploads_it_can_bring_back(refs):
    e = refs
    rec, (a,) = drawn_with_refs(e, "her dress")
    gone = e.c.put(f"/api/library/{KEY}/art/{rec['id']}/refs", json={"refs": []}).json()
    s = store_mod.ArtStore(e.data)
    age(s.ref_path(e.hash, a), 30)
    assert s.prune_refs(e.hash) == 0 and s.ref_path(e.hash, a)
    undo(e, gone["undo"])
    assert next(p for p in pictures(e) if p["id"] == rec["id"])["userRefs"][0]["sha"] == a
