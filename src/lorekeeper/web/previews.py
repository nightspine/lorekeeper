"""Style previews from the book itself: one opening scene of a book drawn in every style, once per image provider.

The shared previews (`static/styles/<slug>.webp`, and a saved style's own in `web/styles.py`) all show the Mad Tea-Party.
These show a moment of the book being read instead, so a person choosing a style sees it on their own book. They are kept
apart from the art sets, in `lorekeeper-data/previews/<textHash>/`, so nothing ever takes them for pictures of the book:

    previews.json   {scene: {kind, target, chapter, storyChapter, anchor, summary, body, writer, chosen, created},
                     items: {<provider>: {<style slug>: {file, model, quality, cost, created, text, reference}}}}
    <provider>-<slug>.<ext>

`text` is the style words a preview was drawn from and `reference` the sha of the style picture sent with it (null for
none), so a saved style whose words or picture changed reads as stale, as its shared preview does. A new scene clears
`items`; the old files go only after the new json is written.

The scene comes only from the opening, the first `OPENING_CHAPTERS` story chapters (`jobs.is_story`), so the previews are
safe to show before the book is begun: `opening` asks the pipeline (`Illustrator.opening_moments`) and then keeps only
moments whose anchor lies inside those chapters, whatever it returned. The prompt body is written once per scene by the
scene writer with no earlier series book finished and no reference pictures, then drawn with each style's sentence.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .. import art

HASH = re.compile(r"[0-9a-f]{64}")
PROVIDER = re.compile(r"[a-z0-9]{1,32}")          # image provider ids (no dash: the file name is <provider>-<slug>)
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
FILE = re.compile(r"[a-z0-9]{1,32}-[a-z0-9][a-z0-9-]{0,39}\.(png|jpg|webp)")
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MIME = {v: k for k, v in EXT.items()}
OPENING_CHAPTERS = art.OPENING_CHAPTERS   # the story chapters a preview scene may come from
CAP = 5.0                  # USD: the most one previews job may cost (the route refuses a bigger estimate)
QUALITY = "draft"          # the cheapest level, through `llm.nearest_quality`
BODY_STYLE = "digital-art"  # the style the scratch Illustrator is made with; the body never depends on it


def now_ms() -> int:
    return int(time.time() * 1000)


class PreviewStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "previews"
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def folder(self, text_hash: str) -> Path:
        if not HASH.fullmatch(text_hash or ""):
            raise KeyError(text_hash)
        return self.root / text_hash

    def lock(self, text_hash: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(text_hash, threading.RLock())

    def get(self, text_hash: str) -> dict | None:
        try:
            p = self.folder(text_hash) / "previews.json"
        except KeyError:
            return None
        try:
            data = json.loads(p.read_text()) if p.is_file() else None
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("scene", None)
        if not isinstance(data.get("items"), dict):
            data["items"] = {}
        return data

    def _write(self, text_hash: str, data: dict) -> None:
        d = self.folder(text_hash)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"previews.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(d / "previews.json")

    def _drop(self, text_hash: str, files) -> None:
        d = self.folder(text_hash)
        for f in files:
            if isinstance(f, str) and FILE.fullmatch(f):
                (d / f).unlink(missing_ok=True)

    def set_scene(self, text_hash: str, scene: dict) -> dict:
        """A new scene: `items` cleared, the old files removed once the new json is written."""
        with self.lock(text_hash):
            data = {"scene": scene, "items": {}}
            self._write(text_hash, data)
            d = self.folder(text_hash)       # every preview file, listed or not: the old scene's pictures all go
            self._drop(text_hash, [f.name for f in d.iterdir() if f.is_file() and FILE.fullmatch(f.name)])
            return data

    def put(self, text_hash: str, provider: str, slug: str, data: bytes, mime: str, meta: dict) -> dict:
        """File one preview (a provider and a style), replacing that one only. Returns its item."""
        if not PROVIDER.fullmatch(provider or "") or not SLUG.fullmatch(slug or ""):
            raise KeyError((provider, slug))
        ext = EXT.get(mime, "png")
        name = f"{provider}-{slug}.{ext}"
        with self.lock(text_hash):
            cur = self.get(text_hash) or {"scene": None, "items": {}}
            d = self.folder(text_hash)
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f"{name}.{uuid.uuid4().hex}.tmp"
            tmp.write_bytes(data)
            tmp.replace(d / name)
            old = ((cur["items"].get(provider) or {}).get(slug) or {}).get("file")
            item = {"file": name, "created": now_ms(), **meta}
            cur["items"].setdefault(provider, {})[slug] = item
            self._write(text_hash, cur)
            if old and old != name:
                self._drop(text_hash, [old])
            return item

    def path(self, text_hash: str, provider: str, slug: str) -> Path | None:
        """A preview's file, only when the json lists it under its own `<provider>-<slug>.<ext>` name."""
        if not PROVIDER.fullmatch(provider or "") or not SLUG.fullmatch(slug or ""):
            return None
        data = self.get(text_hash)
        item = (((data or {}).get("items") or {}).get(provider) or {}).get(slug)
        f = item.get("file") if isinstance(item, dict) else None
        if not isinstance(f, str) or not re.fullmatch(rf"{re.escape(provider)}-{re.escape(slug)}\.(png|jpg|webp)", f):
            return None
        p = self.folder(text_hash) / f
        return p if p.is_file() else None

    def remove(self, text_hash: str) -> bool:
        with self.lock(text_hash):
            try:
                d = self.folder(text_hash)
            except KeyError:
                return False
            if not d.is_dir():
                return False
            for f in d.iterdir():
                if f.is_file():
                    f.unlink(missing_ok=True)
            try:
                d.rmdir()
            except OSError:
                pass
            return True


def stale(item: dict, words: str, ref_sha: str | None) -> bool:
    """A preview drawn from other style words, or with another style picture sent (or none), is stale."""
    return item.get("text") != words or item.get("reference") != ref_sha


# ---- the scratch Illustrator: a book's index read for its opening, writing nothing anywhere

class ScratchSink:
    """An art set kept in memory, seeded with a copy of a real set's manifest (for a fresh screening): the prompt path may
    cache what it works out, and none of it is ever written to the art store."""

    def __init__(self, seed: dict | None = None) -> None:
        self.m = copy.deepcopy(seed) if seed else None

    def manifest(self) -> dict | None:
        return copy.deepcopy(self.m) if self.m else None

    def save(self, manifest: dict) -> None:
        self.m = copy.deepcopy(manifest)

    def put(self, file: str, data: bytes) -> None:
        pass

    def read(self, file: str) -> bytes | None:
        return None


def illustrator(doc: dict, book, prior: list[dict], text, seed: dict | None = None):
    """An Illustrator over the book (earlier series books only read, never finished for the body) that writes nothing:
    every sink is a `ScratchSink`. `seed` a manifest of one of the book's sets, for its screening."""
    books = [art.Book(d, None, ScratchSink(), None, draw=False) for d in prior]
    books.append(art.Book(doc, book, ScratchSink(seed), "main"))
    return art.Illustrator(books, text, None, BODY_STYLE, portraits=None, log=lambda m: None, pause=0,
                           cheap_text=text, portrait_text=text)


def _field(m, name: str, default=None):
    return m.get(name, default) if isinstance(m, dict) else getattr(m, name, default)


def opening(il, is_story: Callable[[str, int], bool]) -> list[dict]:
    """The opening's drawable moments as dicts {kind, target, chapter, storyChapter, anchor, summary, moment} (`moment` the
    pipeline's own object, for `preview_body`), in the pipeline's order. Only moments whose anchor lies in the first
    `OPENING_CHAPTERS` story chapters are kept, whatever the pipeline returned."""
    b = il.books[-1]
    story = [c for c in b.chapters if is_story(c["title"], c.get("words", 0))][:OPENING_CHAPTERS]
    if not story:
        return []
    got = il.opening_moments(-1, OPENING_CHAPTERS, story=is_story)
    out = []
    for m in got or []:
        anchor = _field(m, "anchor")
        if anchor not in b.seq_of:
            continue
        s = b.seq_of[anchor]
        at = next((i for i, c in enumerate(story) if c["lo"] <= s <= c["hi"]), None)
        if at is None:
            continue
        kind, target = _field(m, "kind"), _field(m, "target")
        if kind not in ("plate", "event") or not isinstance(target, str) or not target:
            continue
        out.append({"kind": kind, "target": target, "chapter": _field(m, "chapter") or story[at]["title"],
                    "storyChapter": at + 1, "anchor": anchor, "summary": str(_field(m, "summary") or ""), "moment": m})
    return out


def public_moment(m: dict) -> dict:
    return {k: m[k] for k in ("kind", "target", "chapter", "storyChapter", "summary")}


def choose(moments: list[dict], want: str | None, scene: dict | None) -> dict | None:
    """The moment to draw: the one asked for (None when it is not among them), else the stored scene's when it is still
    in the opening, else the pipeline's default (`Illustrator.default_moment`: the first moment that shows an event)."""
    if want is not None:
        return next((m for m in moments if m["target"] == want), None)
    if scene:
        hit = next((m for m in moments if m["target"] == scene.get("target") and m["kind"] == scene.get("kind")), None)
        if hit:
            return hit
    first = art.Illustrator.default_moment([m["moment"] for m in moments])
    return next((m for m in moments if m["moment"] is first), None)


def same_scene(scene: dict | None, m: dict) -> bool:
    """The stored scene is this moment as it stands now (a rebuilt index may move or reword it), with a body."""
    return bool(scene and scene.get("body") and all(scene.get(k) == m[k] for k in ("kind", "target", "anchor", "summary")))


def scene_record(m: dict, pr, writer: dict | None, chosen: str) -> dict:
    body = (getattr(pr, "body", "") or getattr(pr, "scene", "") or "").strip()
    return {**public_moment(m), "anchor": m["anchor"], "body": body,
            "writer": getattr(pr, "written_by", None) or writer, "chosen": chosen, "created": now_ms()}
