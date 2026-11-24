"""The art store: generated pictures kept beside the library, never inside an index (CLAUDE.md §8f).

One folder per edition and style, `lorekeeper-data/art/<textHash>/<style-slug>/`, holding the manifest
(`art.json`, written by `art.Illustrator` through a `Sink`) and the picture files `<id>.<ext>`. Requests
never carry paths: an edition is a 64-hex text hash, a style a slug, a picture an id `p<8 hex>` that is
looked up in the manifest, so a file name only ever comes from the manifest and is checked again here.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .. import art

HASH = re.compile(r"[0-9a-f]{64}")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PID = re.compile(r"p[0-9a-f]{8}")
FILE = re.compile(r"p[0-9a-f]{8}\.(png|jpg|webp|bin)")
MAX_WORLD = 800     # characters of a world note a person may write


def text_hash(doc: dict) -> str:
    """The edition an index's pictures belong to: its `builtFrom` edition, else its first (as `art.default_dir`)."""
    eds = doc.get("editions") or [{}]
    built = next((e for e in eds if e.get("builtFrom")), eds[0])
    h = str(built.get("textHash") or "")
    return h.split(":", 1)[1] if h.startswith("sha256:") else h


class ArtStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "art"
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    # ---- addressing

    def folder(self, text_hash: str, style: str) -> Path:
        """The folder of one art set; `style` is a style name or a slug. KeyError for anything malformed."""
        slug = art.style_slug(style) if style else ""
        if not HASH.fullmatch(text_hash or "") or not SLUG.fullmatch(slug):
            raise KeyError(style)
        return self.root / text_hash / slug

    def sink(self, text_hash: str, style: str) -> art.FileSink:
        """What `art.Illustrator` writes through: the same four calls as the CLI's folder."""
        return art.FileSink(self.folder(text_hash, style))

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
            m = self.manifest(text_hash, sub.name) if SLUG.fullmatch(sub.name) else None
            if m is None:
                continue
            out.append({"slug": sub.name, "style": m.get("style"), "pictures": len(m.get("pictures", [])),
                        "cost": m.get("cost", 0.0), "_t": (sub / "art.json").stat().st_mtime})
        out.sort(key=lambda s: -s.pop("_t"))
        return out

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
        """Bytes on disk for every art set of an edition."""
        d = self.root / text_hash if HASH.fullmatch(text_hash or "") else None
        return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d and d.is_dir() else 0

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


def screened_count(screened: dict, title: str) -> int | None:
    """How many drawable moments the set's screening found in a chapter, or None when it was not screened. The cache is
    keyed by the chapter title (a key may carry more after the title, such as a stamp of the chapter's events) and holds
    a list of moments, or an object with one."""
    hit = screened.get(title)
    if hit is None:
        hit = next((v for k, v in screened.items() if isinstance(k, str) and k.startswith(title)
                    and k[len(title):len(title) + 1] in ("|", "#", "@", "\t", "\x1f")), None)
    if isinstance(hit, dict):
        hit = next((v for k in ("moments", "events", "chosen", "drawable") if isinstance(v := hit.get(k), list)), None)
    return len(hit) if isinstance(hit, list) else None
