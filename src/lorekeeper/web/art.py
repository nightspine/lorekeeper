"""The art store: generated pictures kept beside the library, never inside an index (CLAUDE.md §8f).

One folder per edition and style, `lorekeeper-data/art/<textHash>/<style-slug>/`, holding the manifest
(`art.json`, written by `art.Illustrator` through a `Sink`) and the picture files `<id>.<ext>`. Requests
never carry paths: an edition is a 64-hex text hash, a style a slug, a picture an id `p<8 hex>` that is
looked up in the manifest, so a file name only ever comes from the manifest and is checked again here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from .. import art
from .styles import EXT as REF_EXT, MAX_REF_BYTES, StyleError, sniff

HASH = re.compile(r"[0-9a-f]{64}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PID = re.compile(r"p[0-9a-f]{8}")
FILE = re.compile(r"p[0-9a-f]{8}\.(png|jpg|webp|bin)")
MAX_WORLD = 800     # characters of a world note a person may write
# The person's own reference pictures, one folder per edition beside its style sets (shared by every set), by content.
REFS = "refs"       # never a style slug (`ArtStore.folder` refuses it)
SHA = re.compile(r"[0-9a-f]{64}")
REF_FILE = re.compile(r"([0-9a-f]{64})\.(png|jpg|webp)")
REF_MIME = {v: k for k, v in REF_EXT.items()}
MAX_USER_REFS = 4
REF_NOTE_CHARS = 120
REF_KEEP = 24 * 3600            # seconds an upload no picture or undo entry lists is kept
PRUNE_EVERY = 60                # seconds between prunes of one edition on saves


def user_ref_shas(value) -> set[str]:
    """Every sha listed under a `userRefs` key anywhere in a JSON value (a manifest, a journal entry)."""
    out: set[str] = set()
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            refs = v.get("userRefs")
            if isinstance(refs, list):
                out |= {r["sha"] for r in refs if isinstance(r, dict) and isinstance(r.get("sha"), str)}
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return out


def text_hash(doc: dict) -> str:
    """The edition an index's pictures belong to: its `builtFrom` edition, else its first (as `art.default_dir`)."""
    eds = doc.get("editions") or [{}]
    built = next((e for e in eds if e.get("builtFrom")), eds[0])
    h = str(built.get("textHash") or "")
    return h.split(":", 1)[1] if h.startswith("sha256:") else h


def world_of(bg: dict) -> dict:
    """The `world` a set's manifest carries for the edition's background note (what the pipeline reads)."""
    return {"text": bg["text"], "edited": True, "from": bg.get("from")}


class SeededSink(art.FileSink):
    """A set's folder read with the edition's background note (the person's own world note, kept once per edition in
    `background.json`) as its `world`; before its manifest is first written it reads as a blank set holding that note."""

    def __init__(self, folder: Path, seed) -> None:
        super().__init__(folder)
        self._seed = seed

    def manifest(self) -> dict | None:
        return self._seed(super().manifest())

    def save(self, manifest: dict) -> None:
        super().save(manifest)
        if self.saved is not None:
            self.saved()

    saved = None    # called after every save (the store prunes the edition's uploads)


class ReadOnlySink(art.FileSink):
    """Another style's set of the same edition, read for a likeness only: a run or a route never writes through it."""

    def save(self, manifest: dict) -> None:
        raise PermissionError("a likeness set is read only")

    def put(self, file: str, data: bytes) -> None:
        raise PermissionError("a likeness set is read only")


class ArtStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "art"
        self.journal = data_dir / "merges"      # the undo journal (`Identity.dir`): an entry may still need an upload
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()
        self.refs_lock = threading.RLock()      # every write, removal and prune of an edition's `refs/`
        self._pruned: dict[str, float] = {}
        for h in self.ref_editions():           # when the store opens
            self.prune_refs(h)

    # ---- addressing

    def folder(self, text_hash: str, style: str) -> Path:
        """The folder of one art set; `style` is a style name or a slug. KeyError for anything malformed."""
        slug = art.style_slug(style) if style else ""
        if not HASH.fullmatch(text_hash or "") or not SLUG.fullmatch(slug) or slug == REFS:
            raise KeyError(style)
        return self.root / text_hash / slug

    def sink(self, text_hash: str, style: str) -> art.FileSink:
        """What `art.Illustrator` writes through: the same four calls as the CLI's folder. `style` should be the style text
        (`resolve`), since a set not written yet starts as a blank set of that style with the edition's background note."""
        folder = self.folder(text_hash, style)

        def seed(m: dict | None) -> dict | None:
            bg = self.background(text_hash)
            if bg is None:
                return m
            if m is None:   # the edition's text hash as its sets store it (the index's own form, which the pipeline compares)
                m = art.blank(self._stored_hash(text_hash) or text_hash, style)
            m["world"] = world_of(bg)
            return m
        sink = SeededSink(folder, seed)
        sink.saved = lambda: self.prune_refs(text_hash, throttle=True)
        return sink

    def likeness(self, text_hash: str, style: str) -> dict[str, art.FileSink]:
        """slug -> read-only sink of every other style's set of the edition: where a picture in `style` may take a figure's
        likeness from when its own set has no picture of them (the pipeline gates each picture it takes)."""
        own = art.style_slug(style) if style else ""
        return {st["slug"]: ReadOnlySink(self.root / text_hash / st["slug"])
                for st in self.styles(text_hash) if st["slug"] != own}

    def book(self, doc: dict, book, text_hash: str, style: str, level: str | None = None, draw: bool = True) -> art.Book:
        """The pipeline's `Book` for one edition and style: its set's sink (style text, as `resolve` gives it) and the
        edition's other sets as likeness sinks."""
        return art.Book(doc, book, self.sink(text_hash, style), level, draw=draw, likeness=self.likeness(text_hash, style))

    def lock(self, text_hash: str, style: str) -> threading.Lock:
        """One writer at a time per art set (a route's draw and another route's delete would each save a manifest)."""
        k = (text_hash, art.style_slug(style))
        with self._guard:
            return self._locks.setdefault(k, threading.Lock())

    # ---- reading

    def manifest(self, text_hash: str, style: str) -> dict | None:
        try:
            p = self.folder(text_hash, style) / "art.json"
        except KeyError:
            return None
        try:
            return json.loads(p.read_text()) if p.is_file() else None
        except (OSError, json.JSONDecodeError):
            return None

    def styles(self, text_hash: str) -> list[dict]:
        """The art sets of an edition, most recently written first: slug, style, picture count, cost."""
        if not HASH.fullmatch(text_hash or ""):
            return []
        d = self.root / text_hash
        out = []
        for sub in d.iterdir() if d.is_dir() else ():
            m = self.manifest(text_hash, sub.name) if SLUG.fullmatch(sub.name) and sub.name != REFS else None
            if m is None:
                continue
            out.append({"slug": sub.name, "style": m.get("style"), "pictures": len(m.get("pictures", [])),
                        "cost": m.get("cost", 0.0), "bytes": sum(f.stat().st_size for f in sub.iterdir() if f.is_file()),
                        "_t": (sub / "art.json").stat().st_mtime})
        out.sort(key=lambda s: -s.pop("_t"))
        return out

    def background_path(self, text_hash: str) -> Path:
        if not HASH.fullmatch(text_hash or ""):
            raise KeyError(text_hash)
        return self.root / text_hash / "background.json"

    def background(self, text_hash: str) -> dict | None:
        """The edition's background note, `{text, from, edited: true, updated}`, from `background.json`. With no file yet, the
        person's newest edited note in a set (one written before notes were per edition) becomes it."""
        try:
            p = self.background_path(text_hash)
        except KeyError:
            return None
        if p.is_file():
            try:
                bg = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                return None
            return bg if isinstance(bg, dict) and bg.get("text") else None
        newest = None
        for st in self.styles(text_hash):
            w = (self.manifest(text_hash, st["slug"]) or {}).get("world")
            if isinstance(w, dict) and w.get("edited") and w.get("text"):
                newest = {"text": w["text"], "from": w.get("from"), "edited": True, "updated": None}
                break
        if newest is not None and not p.is_file():   # no lock: it may be read under a set's lock; a race writes the same
            self.set_background(text_hash, newest)
        return newest

    def set_background(self, text_hash: str, bg: dict | None) -> None:
        """Write (None: remove) the edition's background note, atomically."""
        p = self.background_path(text_hash)
        if bg is None:
            p.unlink(missing_ok=True)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"background.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(bg, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(p)

    def _stored_hash(self, text_hash: str) -> str:
        for st in self.styles(text_hash):
            h = (self.manifest(text_hash, st["slug"]) or {}).get("textHash")
            if h:
                return str(h)
        return ""

    def resolve(self, text_hash: str, style: str) -> tuple[str, str]:
        """(slug, style text) of the set a style names. A set is identified by its slug: "Ink" is the "ink" set, and a custom
        style whose slug is a stored set's uses that set's stored text, so the pipeline reuses the set and never starts it over."""
        slug = art.style_slug(style)
        m = self.manifest(text_hash, slug)
        if m and m.get("style"):
            return slug, m["style"]
        # a saved style (web/styles.py) is passed by its slug, which `art.style_sentence` turns into its words
        return slug, (slug if slug in art.SAVED_STYLES else style)

    def editions(self) -> list[str]:
        """Text hashes that have at least one art set."""
        if not self.root.is_dir():
            return []
        return sorted(d.name for d in self.root.iterdir() if HASH.fullmatch(d.name) and self.styles(d.name))

    def find(self, text_hash: str, pid: str) -> tuple[str, dict] | None:
        """(slug, record) of a picture id in any style of the edition."""
        if not PID.fullmatch(pid or ""):
            return None
        for s in self.styles(text_hash):
            rec = next((r for r in self.manifest(text_hash, s["slug"])["pictures"] if r.get("id") == pid), None)
            if rec:
                return s["slug"], rec
        return None

    def path(self, text_hash: str, style: str, pid: str) -> Path | None:
        """The file of a picture listed in the manifest, or None. The name is the manifest's, checked to be `<id>.<ext>`."""
        if not PID.fullmatch(pid or "") or not SLUG.fullmatch(style or ""):   # a slug as listed, never a style name
            return None
        m = self.manifest(text_hash, style)
        rec = next((r for r in (m or {}).get("pictures", []) if r.get("id") == pid), None)
        name = str((rec or {}).get("file") or "")
        if not FILE.fullmatch(name) or not name.startswith(pid + "."):
            return None
        p = self.folder(text_hash, style) / name
        return p if p.is_file() else None

    def size(self, text_hash: str) -> int:
        """Bytes on disk for every art set of an edition, its uploaded reference pictures included."""
        d = self.root / text_hash if HASH.fullmatch(text_hash or "") else None
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d and d.is_dir() else 0

    # ---- the person's reference pictures: `<textHash>/refs/<sha256>.<ext>`, by content, shared by the edition's sets

    def refs_dir(self, text_hash: str) -> Path:
        if not HASH.fullmatch(text_hash or ""):
            raise KeyError(text_hash)
        return self.root / text_hash / REFS

    def put_ref(self, text_hash: str, data: bytes) -> dict:
        """Keep one uploaded picture: checked by its bytes (`styles.sniff`, at most `MAX_REF_BYTES`), filed under its
        sha256. The same bytes again give the same sha and no new file (the file's time is renewed, so a fresh upload
        is never pruned before it is drawn). Raises `styles.StyleError` (413 `too-large`, 422 `bad-image`)."""
        if len(data) > MAX_REF_BYTES:
            raise StyleError(413, "too-large", "The picture is over 8 MB. Choose a smaller one.")
        mime, w, h = sniff(data)
        sha = hashlib.sha256(data).hexdigest()
        ext = REF_EXT[mime]
        d = self.refs_dir(text_hash)
        p = d / f"{sha}.{ext}"
        with self.refs_lock:
            if p.is_file():
                os.utime(p)
            else:
                d.mkdir(parents=True, exist_ok=True)
                tmp = d / f".{uuid.uuid4().hex}.tmp"
                tmp.write_bytes(data)
                tmp.replace(p)
        return {"sha": sha, "ext": ext, "mime": mime, "bytes": len(data), "width": w, "height": h}

    def ref_path(self, text_hash: str, sha: str) -> Path | None:
        """The file of an uploaded picture the edition's folder holds, named `<sha>.<ext>`; None otherwise."""
        if not HASH.fullmatch(text_hash or "") or not SHA.fullmatch(sha or ""):
            return None
        d = self.root / text_hash / REFS
        for ext in REF_MIME:
            p = d / f"{sha}.{ext}"
            if p.is_file():
                return p
        return None

    def touch_ref(self, text_hash: str, sha: str) -> Path | None:
        """Renew an upload's time because it is being used (checked for a draw, kept by `PUT .../refs`, brought back by an
        undo): pruning only takes files a day old, so one used in the last day is never pruned. Its path, or None."""
        with self.refs_lock:
            p = self.ref_path(text_hash, sha)
            if p is None:
                return None
            try:
                os.utime(p)
            except OSError:
                return None
            return p

    def ref_bytes(self, text_hash: str, sha: str) -> tuple[bytes, str, str] | None:
        """(bytes, mime, ext) of an uploaded picture, or None when the folder does not hold it (also when it went just now)."""
        p = self.ref_path(text_hash, sha)
        if p is None:
            return None
        ext = p.suffix[1:]
        try:
            return p.read_bytes(), REF_MIME[ext], ext
        except OSError:
            return None

    def ref_editions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(d.name for d in self.root.iterdir() if HASH.fullmatch(d.name) and (d / REFS).is_dir())

    def journal_refs(self) -> set[str] | None:
        """Every upload an undo journal entry still names: in its entry (a removed record, a `refs` change) or in a set
        it keeps a copy of (a removed set). None when an entry can't be read (being written or removed just now, or
        damaged): that counts as keeping everything, never as keeping nothing."""
        out: set[str] = set()
        try:
            entries = [d for d in self.journal.iterdir() if d.is_dir()] if self.journal.is_dir() else []
            for d in entries:
                for f in [d / "entry.json", *d.glob("*/art.json")]:
                    out |= user_ref_shas(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return out

    def listed_refs(self, text_hash: str) -> set[str]:
        """Every upload a picture record of any set of the edition lists."""
        out: set[str] = set()
        for st in self.styles(text_hash):
            out |= user_ref_shas((self.manifest(text_hash, st["slug"]) or {}).get("pictures", []))
        return out

    def prune_refs(self, text_hash: str, throttle: bool = False, now: float | None = None) -> int:
        """Delete the uploads of an edition that no picture record lists, no undo entry needs and that are older than a
        day (an upload never drawn goes after a day). Runs when the store opens and when a set is saved (`throttle`: at
        most once a minute per edition). Returns how many went."""
        try:
            d = self.refs_dir(text_hash)
        except KeyError:
            return 0
        now = time.time() if now is None else now
        with self.refs_lock:    # the kept set and the removals in one step: no upload, use or undo slips between them
            if throttle:
                if now - self._pruned.get(text_hash, 0) < PRUNE_EVERY:
                    return 0
                self._pruned[text_hash] = now

            def age(f: Path) -> float:
                try:
                    return now - f.stat().st_mtime
                except OSError:          # gone meanwhile
                    return 0.0
            try:
                files = [f for f in d.iterdir() if REF_FILE.fullmatch(f.name) or f.name.endswith(".tmp")] if d.is_dir() else []
            except OSError:              # the folder went (every set removed)
                return 0
            old = [f for f in files if age(f) > REF_KEEP]
            if not old:
                return 0
            held = self.journal_refs()
            if held is None:             # an undo entry can't be read: prune nothing this time
                return 0
            keep = self.listed_refs(text_hash) | held
            gone = 0
            for f in old:
                m = REF_FILE.fullmatch(f.name)
                if (m is None or m.group(1) not in keep) and age(f) > REF_KEEP:   # renewed since: kept
                    f.unlink(missing_ok=True)
                    gone += 1
            return gone

    # ---- writing (callers hold `lock`)

    def save(self, text_hash: str, style: str, manifest: dict) -> None:
        self.sink(text_hash, style).save(manifest)

    def put(self, text_hash: str, style: str, record: dict, data: bytes) -> None:
        """File a picture and add its record (a record with the same id is replaced)."""
        if not PID.fullmatch(record.get("id", "")) or not FILE.fullmatch(record.get("file", "")):
            raise ValueError("bad picture record")
        sink = self.sink(text_hash, style)
        sink.put(record["file"], data)
        m = sink.manifest() or art.blank(text_hash, style)
        m["pictures"] = [r for r in m.get("pictures", []) if r.get("id") != record["id"]] + [record]
        m["cost"] = round(sum(r.get("cost") or 0 for r in m["pictures"]), 6)
        sink.save(m)

    def remove(self, text_hash: str, style: str, pid: str) -> dict | None:
        """Take a picture out of the manifest and delete its file; returns its record."""
        sink = self.sink(text_hash, style)
        m = sink.manifest()
        rec = next((r for r in (m or {}).get("pictures", []) if r.get("id") == pid), None)
        if rec is None:
            return None
        p = self.path(text_hash, style, pid)
        m["pictures"] = [r for r in m["pictures"] if r.get("id") != pid]
        m["cost"] = round(sum(r.get("cost") or 0 for r in m["pictures"]), 6)
        sink.save(m)
        if p is not None:
            p.unlink(missing_ok=True)
        return rec

    def prune(self, text_hash: str, style: str) -> int:
        """Delete picture files the manifest no longer lists (left by an undone redraw). Returns how many."""
        try:
            d = self.folder(text_hash, style)
        except KeyError:
            return 0
        m = self.manifest(text_hash, style)
        if m is None or not d.is_dir():
            return 0
        keep = {r.get("file") for r in m.get("pictures", [])}
        gone = 0
        for f in d.iterdir():
            if FILE.fullmatch(f.name) and f.name not in keep:
                f.unlink(missing_ok=True)
                gone += 1
        return gone


    # ---- targeted undo: put back or take out exactly the records a journal entry touched (callers hold `lock`)

    def take_out(self, text_hash: str, slug: str, ids: list[str]) -> int:
        """Remove these pictures (records and files) and nothing else; returns how many were there."""
        n = 0
        for pid in ids:
            if self.remove(text_hash, slug, pid) is not None:
                n += 1
        return n

    def put_back(self, text_hash: str, slug: str, records: list[dict], src: Path) -> int:
        """Put records back, with their files from `src` (a journal snapshot), unless a record with the id is already there."""
        sink = self.sink(text_hash, slug)
        m = self.manifest(text_hash, slug)       # the stored one: a set that is gone is not made again from the seed
        if m is None:
            raise LookupError("this art set is gone")
        have = {r.get("id") for r in m.get("pictures", [])}
        n = 0
        for rec in records:
            name = str(rec.get("file") or "")
            if rec.get("id") in have or not FILE.fullmatch(name) or not (src / name).is_file():
                continue
            sink.put(name, (src / name).read_bytes())
            m["pictures"].append(rec)
            n += 1
        m["cost"] = round(sum(r.get("cost") or 0 for r in m["pictures"]), 6)
        sink.save(m)
        return n

