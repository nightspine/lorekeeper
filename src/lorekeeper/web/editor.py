"""Editing indexes by hand: add, change and delete entries, with the same undo journal as merges.

A person is the second author of every index built here. They can write one from scratch
(`blank`), fix what the model produced (`apply`), and have their edits survive a rebuild
(`reapply`). Every change is one journal entry (`Identity._commit`) that `undo` reverts, and is also
recorded in a sidecar keyed by the edition's text hash (`edits/<textHash>.json`) so that a new build of
the same book gets the same edits applied again, by id where the id survived and by text otherwise.

The rules a person's wording must keep are the generator's own (`check_wording`): no hindsight, no
"now"-style descriptions, no tie word the cited passage never uses, no 9+ words copied from the book,
and the spec's length limits. Anchors are never typed: an edit names a paragraph (`seq`) and the server
picks the first paragraph at or after it that can carry an anchor (6+ words, unique fingerprint).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .. import epub, generate, text, validate
from ..generate import slug

TYPES = ("person", "creature", "place", "group", "object", "concept", "term")
NAME_KINDS = ("primary", "alias", "title", "nickname", "descriptive")
BASES = ("stated", "inferred")
SCOPES = ("story", "chapter")
EVENT_KINDS = ("turn", "minor")   # how much an event drives the story; absent = an ordinary development
RELATION_KINDS = generate.RELATION_KINDS   # what sort of tie a relation is; absent = not said
# Spec length limits (SPEC.md §6.3), with how the message names each kind of text.
LIMITS = {"detail": (280, "a detail"), "profile": (800, "a description"), "event": (280, "an event"),
          "label": (80, "a connection"), "name": (120, "a name"), "story": (1200, "a story recap"),
          "chapter": (2000, "a chapter recap"), "title": (120, "a chapter title")}
MAX_PASSAGE = 60   # paragraphs either side in one passage request


class EditError(ValueError):
    """A malformed or impossible edit (HTTP 400)."""


class WordingError(ValueError):
    """Text that breaks a content rule (HTTP 422, code "wording")."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _q(s: str) -> str:
    return f"“{s}”"


def check_wording(kind: str, s: str, cited: str | None = None, shingles: set | None = None) -> None:
    """Raise WordingError when `s` breaks a content rule for text of this `kind` (a LIMITS key).
    `cited` is the text of the paragraphs the item cites (tie words must appear in it) and
    `shingles` the book's word runs (`text.shingles`); both checks are skipped when not given."""
    limit, noun = LIMITS[kind]
    if len(s) > limit:
        raise WordingError(f"Too long: {noun} is at most {limit} characters")
    if kind in ("name", "title"):
        return
    m = generate.HINDSIGHT.search(s)
    if m:
        raise WordingError(f"Reads with hindsight ({_q(m.group(0))}): describe what a reader knows at this point.")
    if kind == "profile":
        m = generate.MOMENTARY.search(s)
        if m:
            raise WordingError(f"Describes a moment ({_q(m.group(0))}): a description says who they are, not where the story is.")
    if cited is not None and kind in ("detail", "profile", "label"):
        tie = generate.unsupported_tie(s, cited)
        if tie:
            raise WordingError(f"Says {_q(tie)}, but the passage never does. Cite a paragraph that states the tie, or leave it out.")
    if shingles is not None:
        runs = text.copied_runs(s, shingles, generate.MAX_COPY)
        if runs:
            raise WordingError(f"Quotes {generate.MAX_COPY} or more words in a row from the book: {_q(runs[0])}. Put it in your own words.")


class _Ctx:
    """One edit's working set: the group's documents, the edited index and its book."""

    def __init__(self, items: list[dict], docs: list[dict], i: int, book: epub.Book | None, eligible: list[int]) -> None:
        self.items, self.docs, self.i, self.book, self.eligible = items, docs, i, book, eligible
        self.doc = docs[i]
        self.changed: set[int] = set()
        self.extra: dict = {}

    # ---- lookups ----------------------------------------------------------------------------

    def entity(self, eid: str, doc: dict | None = None) -> dict:
        for e in (doc or self.doc).get("entities", []):
            if e["id"] == eid:
                return e
        raise EditError(f"no entry {eid!r} in this index")

    def entity_anywhere(self, eid: str) -> list[tuple[int, dict]]:
        return [(j, e) for j, d in enumerate(self.docs) for e in d.get("entities", []) if e["id"] == eid]

    def item(self, kind: str, iid: str) -> tuple[int, dict, dict | None]:
        """(doc index, item, owning entity for facets) for an item id anywhere in the group."""
        for j, d in enumerate(self.docs):
            if kind == "facet":
                for e in d.get("entities", []):
                    for f in e.get("facets", []):
                        if f["id"] == iid:
                            return j, f, e
            else:
                for x in d.get(kind + "s", []):
                    if x["id"] == iid:
                        return j, x, None
        raise EditError(f"no {kind} {iid!r} in this index")

    def names(self, eid: str) -> list[str]:
        for _, e in self.entity_anywhere(eid):
            vals = [n["value"] for n in e.get("names", []) if n.get("kind") == "primary"] or [n["value"] for n in e.get("names", [])]
            if vals:
                return vals
        return [eid]

    def all_ids(self) -> set[str]:
        out = set()
        for d in self.docs:
            for _, _, x in validate._items(d):
                if "id" in x:
                    out.add(x["id"])
        return out

    def new_id(self, prefix: str) -> str:
        taken = self.all_ids()
        n = 1
        while f"{prefix}.{n}" in taken:
            n += 1
        return f"{prefix}.{n}"

    @property
    def book_key(self) -> str:
        return (self.doc.get("series") or {}).get("book") or self.doc["id"]

    # ---- anchors ----------------------------------------------------------------------------

    def ords(self, doc: dict | None = None) -> dict[str, int]:
        return {a["id"]: i for i, a in enumerate((doc or self.doc)["anchors"])}

    def seq_of(self, anchor: str) -> int | None:
        return next((a.get("seq") for a in self.doc["anchors"] if a["id"] == anchor), None)

    def anchor(self, seq) -> str:
        """The anchor for a paragraph, created if needed: the first paragraph at or after `seq` that
        can carry one. The generator's id scheme (`toc03.para12`) is kept."""
        if self.book is None:
            raise EditError("the book file for this index isn't on this machine, so paragraphs can't be chosen")
        try:
            seq = int(seq)
        except (TypeError, ValueError) as e:
            raise EditError("seq must be a paragraph number") from e
        if not 0 <= seq < len(self.book.blocks):
            raise EditError("no such paragraph")
        k = bisect_left(self.eligible, seq)
        if k == len(self.eligible):
            raise EditError("no paragraph at or after that one can hold an anchor (too short, or repeated in the book)")
        seq = self.eligible[k]
        anchors = self.doc["anchors"]
        for a in anchors:
            if a.get("seq") == seq:
                return a["id"]
        ch = -1
        for i, c in enumerate(self.book.chapters):
            if c.start <= seq:
                ch = i
            else:
                break
        base = f"front.para{seq:02d}" if ch < 0 else f"toc{ch + 1:02d}.para{seq - self.book.chapters[ch].start:02d}"
        aid, n = base, 1
        while any(a["id"] == aid for a in anchors):
            n += 1
            aid = f"{base}-{n}"
        new = {"id": aid, "fp": self.book.blocks[seq].fingerprint, "seq": seq}
        pos = next((i for i, a in enumerate(anchors) if a.get("seq", -1) > seq), len(anchors))
        anchors.insert(pos, new)
        self.changed.add(self.i)
        return aid

    def reveal(self, seq) -> str:
        """Anchor for a new or moved item, keeping `coverage.through` at or past it."""
        aid = self.anchor(seq)
        ords = self.ords()
        if ords[aid] > ords.get(self.doc["coverage"]["through"], -1):
            self.doc["coverage"]["through"] = aid
        self.extra["anchor"] = aid
        return aid

    def cited(self, anchor: str) -> str | None:
        """Text of the cited paragraph and its neighbours, for the tie check."""
        seq = self.seq_of(anchor)
        if self.book is None or seq is None:
            return None
        return " ".join(b.raw for b in self.book.blocks[max(0, seq - 2):seq + 3])


class Editor:
    def __init__(self, identity, library, data_dir: Path, reader=None) -> None:
        self.identity, self.library = identity, library
        self.dir = data_dir / "edits"
        self.reader = reader or epub.read    # path → epub.Book (the app passes its cache)
        self._lock = threading.Lock()
        self._elig: dict[tuple, list[int]] = {}
        self._shingles: dict[tuple, set] = {}

    # ---- books ---------------------------------------------------------------------------------

    def book_for(self, doc: dict) -> epub.Book | None:
        for ed in doc.get("editions", []):
            if ed.get("builtFrom", True) and ed.get("textHash"):
                p = self.library.edition_path(ed["textHash"])
                if p:
                    try:
                        return self.reader(p)
                    except Exception:   # noqa: BLE001 - an unreadable file means "no book", not a crash
                        return None
        return None

    def eligible(self, book: epub.Book) -> list[int]:
        k = (book.path, len(book.blocks))
        if k not in self._elig:
            counts = Counter(b.fingerprint for b in book.blocks)
            self._elig[k] = [b.seq for b in book.blocks if len(b.words) >= text.MIN_ANCHOR_WORDS and counts[b.fingerprint] == 1]
        return self._elig[k]

    def shingles(self, book: epub.Book) -> set:
        k = (book.path, len(book.blocks))
        if k not in self._shingles:
            self._shingles.clear()
            self._shingles[k] = text.shingles([w for b in book.blocks for w in b.words], generate.MAX_COPY)
        return self._shingles[k]

    def passage(self, book: epub.Book, seq: int, before: int = 2, after: int = 2, doc: dict | None = None) -> dict:
        """Paragraphs around `seq` for the anchor picker."""
        n = len(book.blocks)
        if n == 0:
            return {"seq": 0, "total": 0, "chapter": None, "blocks": []}
        seq = max(0, min(n - 1, int(seq)))
        before, after = max(0, min(MAX_PASSAGE, int(before))), max(0, min(MAX_PASSAGE, int(after)))
        elig = set(self.eligible(book))
        anchored = {a.get("seq"): a["id"] for a in (doc or {}).get("anchors", [])}
        ch = None
        for i, c in enumerate(book.chapters):
            if c.start <= seq:
                ch = {"n": i + 1, "title": c.title or f"Section {i + 1}", "start": c.start}
            else:
                break
        blocks = [{"seq": b.seq, "text": b.raw, "words": len(b.words), "eligible": b.seq in elig, "anchor": anchored.get(b.seq)}
                  for b in book.blocks[max(0, seq - before):seq + after + 1]]
        return {"seq": seq, "total": n, "chapter": ch, "blocks": blocks}

    # ---- a new index ---------------------------------------------------------------------------

    def blank(self, book: epub.Book, epub_path: Path, license: str = "CC0-1.0", user: str = "admin",
              series_id: str | None = None, book_key: str | None = None) -> dict:
        """An empty index for a book: one anchor at its first anchorable paragraph, nothing revealed."""
        elig = self.eligible(book)
        if not elig:
            raise EditError("this book has no paragraph that can carry an anchor")
        key = book_key or slug(book.title or epub_path.stem)
        doc = {
            "lorekeeper": "0.1", "kind": "index",
            "id": f"{series_id}.{key}" if series_id else key,
            "work": {"title": book.title or key, "authors": book.authors or ["Unknown"], "language": book.language or "und"},
            "editions": [{"textHash": text.text_hash(b.words for b in book.blocks),
                          "fileHash": hashlib.sha256(epub_path.read_bytes()).hexdigest(),
                          "label": epub_path.name, "builtFrom": True}],
            "coverage": {"complete": False, "through": ""},
            "provenance": {"contributors": [{"name": user, "role": "author"}], "created": _now(), "updated": _now(),
                           "license": license if license in ("CC0-1.0", "CC-BY-4.0") else "CC0-1.0"},
            "anchors": [], "entities": [], "relations": [], "events": [], "recaps": [],
        }
        if series_id:
            doc["series"] = {"id": series_id, "book": key}
        ctx = _Ctx([], [doc], 0, book, elig)
        doc["coverage"]["through"] = ctx.anchor(elig[0])
        return doc

    # ---- one edit ------------------------------------------------------------------------------

    def apply(self, key: str, op: dict, user: str = "admin") -> dict:
        """Apply one edit to the index `key` (and, for entity-wide changes, the rest of its series)
        as one undoable journal entry; record it for later builds of the same book."""
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise EditError("an edit needs an \"op\"")
        with self._lock, self.identity._lock:
            gid, items, docs = self.identity._writable(key)
            i = next(j for j, it in enumerate(items) if it["key"] == key)
            book = self.book_for(docs[i])
            ctx = _Ctx(items, docs, i, book, self.eligible(book) if book else [])
            what, target = self._apply(ctx, op)
            for j in ctx.changed:
                self._touch(docs[j], user)
            entry = self.identity._commit(gid, items, docs, ctx.changed,
                                          {"kind": "edit", "what": what, "keepNames": [], "dropNames": []})
            self._record(docs[i], op, target, entry["id"])
            return {"undo": entry["id"], "what": what, **ctx.extra}

    def undo(self, key: str, entry_id: str) -> dict:
        """Undo the latest journal entry, a merge or an edit; an edit also leaves the sidecar."""
        with self._lock:
            out = self.identity.undo(key, entry_id)
            item = self.library.scan().get(key)
            if item:
                self._forget(item["doc"], entry_id)
            return out

    @staticmethod
    def _touch(doc: dict, user: str) -> None:
        prov = doc.setdefault("provenance", {})
        prov["updated"] = _now()
        role = "editor" if prov.get("generator") else "author"
        contribs = prov.setdefault("contributors", [])
        if not any(c.get("name") == user and c.get("role") == role for c in contribs):
            contribs.append({"name": user, "role": role})

    # ---- the operations -------------------------------------------------------------------------

    def _apply(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        """Apply `op` to ctx (marking changed documents); returns (what, target) where target says
        how to find the same thing again in a later build."""
        name = op["op"]
        fn = getattr(self, "_op_" + name.replace("-", "_"), None)
        if fn is None:
            raise EditError(f"unknown op {name!r}")
        return fn(ctx, op)

    @staticmethod
    def _need(op: dict, *fields: str) -> list:
        out = []
        for f in fields:
            if f not in op or op[f] is None or (isinstance(op[f], str) and not op[f].strip()):
                raise EditError(f"{f} is required")
            out.append(op[f].strip() if isinstance(op[f], str) else op[f])
        return out

    @staticmethod
    def _choice(op: dict, field: str, allowed: tuple, default=None):
        v = op.get(field, default)
        if v is None:
            return default
        if v not in allowed:
            raise EditError(f"{field} must be one of {', '.join(allowed)}")
        return v

    def _text(self, ctx: _Ctx, kind: str, s: str, anchor: str | None) -> str:
        s = " ".join(str(s).split())
        if not s:
            raise EditError("text is required")
        check_wording(kind, s, ctx.cited(anchor) if anchor else None, self.shingles(ctx.book) if ctx.book else None)
        return s

    def _entity_ids(self, ctx: _Ctx, ids, anchor: str) -> list[str]:
        if not isinstance(ids, list):
            raise EditError("entities must be a list of entry ids")
        ords = ctx.ords()
        out = []
        for eid in ids:
            e = ctx.entity(str(eid))
            if ords.get(e["from"], 0) > ords[anchor]:
                raise EditError(f"{ctx.names(e['id'])[0]} is not revealed yet at that paragraph")
            if e["id"] not in out:
                out.append(e["id"])
        return out

    # entities

    def _op_add_entity(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        name, etype, seq = self._need(op, "name", "type", "seq")
        etype = self._choice(op, "type", TYPES)
        check_wording("name", name)
        anchor = ctx.reveal(seq)
        base = slug(name) or "entity"
        eid, n = base, 1
        taken = {e["id"] for d in ctx.docs for e in d.get("entities", [])}
        while eid in taken:
            n += 1
            eid = f"{base}-{n}"
        ent = {"id": eid, "type": etype, "from": anchor, "names": [{"value": name, "kind": "primary", "from": anchor}], "facets": []}
        desc = " ".join(str(op.get("desc") or "").split())
        if desc:
            basis = self._choice(op, "basis", BASES, "stated")
            desc = self._text(ctx, "profile", desc, anchor)
            ent["facets"].append({"id": ctx.new_id(f"{ctx.book_key}.{eid}"), "kind": "profile", "from": anchor,
                                  "basis": basis, "evidence": [anchor], "text": desc})
        ctx.doc["entities"].append(ent)
        ctx.changed.add(ctx.i)
        ctx.extra["id"] = eid
        return f"Added {name}", {"entity": eid}

    def _op_set_type(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        eid, = self._need(op, "entity")
        etype = self._choice(op, "type", TYPES)
        found = ctx.entity_anywhere(eid)
        if not found:
            raise EditError(f"no entry {eid!r}")
        for j, e in found:
            if e["type"] != etype:
                e["type"] = etype
                ctx.changed.add(j)
        return f"Changed {ctx.names(eid)[0]} to a {etype}", {"entity": eid}

    def _op_delete_entity(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        eid, = self._need(op, "entity")
        found = ctx.entity_anywhere(eid)
        if not found:
            raise EditError(f"no entry {eid!r}")
        name = ctx.names(eid)[0]
        gone = {f["id"] for _, e in found for f in e.get("facets", [])}
        for j, d in enumerate(ctx.docs):
            before = json.dumps(d, sort_keys=True)
            d["entities"] = [e for e in d.get("entities", []) if e["id"] != eid]
            d["relations"] = [r for r in d.get("relations", []) if eid not in (r["src"], r["dst"])]
            for ev in d.get("events", []):
                ev["entities"] = [x for x in ev["entities"] if x != eid]
            for e in d.get("entities", []):
                for f in e.get("facets", []):
                    self._unreplace(f, gone)
            if json.dumps(d, sort_keys=True) != before:
                ctx.changed.add(j)
        return f"Deleted {name}", {"entity": eid}

    @staticmethod
    def _unreplace(item: dict, gone: set[str]) -> None:
        if "replaces" in item:
            item["replaces"] = [r for r in item["replaces"] if r not in gone]
            if not item["replaces"]:
                del item["replaces"]

    def _fix_from(self, ctx: _Ctx, ent: dict) -> None:
        """An entity is revealed with its earliest name (SPEC.md §4.2); names stay in reveal order."""
        ords = ctx.ords()
        ent["names"].sort(key=lambda n: ords.get(n["from"], 0))
        if ent["names"]:
            ent["from"] = ent["names"][0]["from"]

    # names

    def _op_add_name(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        eid, value, seq = self._need(op, "entity", "value", "seq")
        kind = self._choice(op, "kind", NAME_KINDS, "alias")
        ent = ctx.entity(eid)
        check_wording("name", value)
        anchor = ctx.reveal(seq)
        ent["names"].append({"value": value, "kind": kind, "from": anchor})
        self._fix_from(ctx, ent)
        ctx.changed.add(ctx.i)
        return f"Added the name {value} to {ctx.names(eid)[0]}", {"entity": eid, "text": value}

    def _name_at(self, ctx: _Ctx, op: dict) -> tuple[dict, dict]:
        eid, = self._need(op, "entity")
        ent = ctx.entity(eid)
        try:
            idx = int(op["index"])
            nm = ent["names"][idx]
        except (KeyError, TypeError, ValueError, IndexError) as e:
            raise EditError("index must point at one of the entry's names") from e
        return ent, nm

    def _op_edit_name(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        ent, nm = self._name_at(ctx, op)
        old = nm["value"]
        if op.get("value") is not None:
            value = " ".join(str(op["value"]).split())
            if not value:
                raise EditError("value is required")
            check_wording("name", value)
            nm["value"] = value
        if op.get("kind") is not None:
            nm["kind"] = self._choice(op, "kind", NAME_KINDS)
        if op.get("seq") is not None:
            nm["from"] = ctx.reveal(op["seq"])
        self._fix_from(ctx, ent)
        ctx.changed.add(ctx.i)
        what = f"Renamed {old} to {nm['value']}" if nm["value"] != old else f"Edited the name {old} of {ctx.names(ent['id'])[0]}"
        return what, {"entity": ent["id"], "text": old}

    def _op_delete_name(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        ent, nm = self._name_at(ctx, op)
        if len(ent["names"]) < 2:
            raise EditError("an entry keeps at least one name; delete the entry instead")
        ent["names"].remove(nm)
        self._fix_from(ctx, ent)
        ctx.changed.add(ctx.i)
        return f"Removed the name {nm['value']} from {ctx.names(ent['id'])[0]}", {"entity": ent["id"], "text": nm["value"]}

    # facets

    def _op_add_facet(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        eid, s, seq = self._need(op, "entity", "text", "seq")
        ent = ctx.entity(eid)
        basis = self._choice(op, "basis", BASES, "stated")
        profile = op.get("kind") == "profile"
        anchor = ctx.reveal(seq)
        s = self._text(ctx, "profile" if profile else "detail", s, anchor)
        ords = ctx.ords()
        if ords[anchor] < ords.get(ent["from"], 0):
            raise EditError(f"{ctx.names(eid)[0]} is not revealed yet at that paragraph")
        item = {"id": ctx.new_id(f"{ctx.book_key}.{eid}"), "from": anchor, "basis": basis, "evidence": [anchor], "text": s}
        if profile:
            item = {"id": item["id"], "kind": "profile", **{k: v for k, v in item.items() if k != "id"}}
            current = [f for f in ent["facets"] if f.get("kind") == "profile" and ords.get(f["from"], 0) <= ords[anchor]]
            if current:
                item["replaces"] = [max(current, key=lambda f: ords.get(f["from"], 0))["id"]]
        ent["facets"].append(item)
        ent["facets"].sort(key=lambda f: ords.get(f["from"], 0))
        ctx.changed.add(ctx.i)
        ctx.extra["id"] = item["id"]
        noun = "a description of" if profile else "a detail to"
        return f"Added {noun} {ctx.names(eid)[0]}", {"entity": eid, "id": item["id"], "text": s}

    def _op_edit_facet(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, f, ent = ctx.item("facet", iid)
        if j != ctx.i:
            raise EditError("that detail belongs to another book of the series; open that book to edit it")
        old = f["text"]
        if op.get("seq") is not None:
            f["from"] = ctx.reveal(op["seq"])
            f["evidence"] = [f["from"]]
        if op.get("basis") is not None:
            f["basis"] = self._choice(op, "basis", BASES)
        if op.get("text") is not None:
            f["text"] = self._text(ctx, "profile" if f.get("kind") == "profile" else "detail", op["text"], f["from"])
        ords = ctx.ords()
        ent["facets"].sort(key=lambda x: ords.get(x["from"], 0))
        ctx.changed.add(j)
        noun = "description" if f.get("kind") == "profile" else "detail"
        return f"Edited a {noun} of {ctx.names(ent['id'])[0]}", {"entity": ent["id"], "id": iid, "text": old}

    def _op_delete_facet(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, f, ent = ctx.item("facet", iid)
        ent["facets"].remove(f)
        for d in ctx.docs:
            for e in d.get("entities", []):
                for x in e.get("facets", []):
                    self._unreplace(x, {iid})
        ctx.changed.add(j)
        noun = "description" if f.get("kind") == "profile" else "detail"
        return f"Deleted a {noun} of {ctx.names(ent['id'])[0]}", {"entity": ent["id"], "id": iid, "text": f["text"]}

    # relations

    def _op_add_relation(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        src, dst, label, seq = self._need(op, "src", "dst", "label", "seq")
        basis = self._choice(op, "basis", BASES, "stated")
        if src == dst:
            raise EditError("a connection joins two different entries")
        anchor = ctx.reveal(seq)
        self._entity_ids(ctx, [src, dst], anchor)
        label = self._text(ctx, "label", label, anchor)
        item = {"id": ctx.new_id(f"{ctx.book_key}.rel"), "src": src, "dst": dst, "label": label,
                "from": anchor, "basis": basis, "evidence": [anchor]}
        if op.get("kind"):
            item["kind"] = self._choice(op, "kind", RELATION_KINDS)
        ctx.doc["relations"].append(item)
        ctx.changed.add(ctx.i)
        ctx.extra["id"] = item["id"]
        return f"Added a connection: {ctx.names(src)[0]} {label} {ctx.names(dst)[0]}", {"id": item["id"], "src": src, "dst": dst, "text": label}

    def _op_edit_relation(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, r, _ = ctx.item("relation", iid)
        if j != ctx.i:
            raise EditError("that connection belongs to another book of the series; open that book to edit it")
        old = dict(r)
        if op.get("seq") is not None:
            r["from"] = ctx.reveal(op["seq"])
            r["evidence"] = [r["from"]]
        if op.get("src") is not None:
            r["src"] = str(op["src"])
        if op.get("dst") is not None:
            r["dst"] = str(op["dst"])
        if r["src"] == r["dst"]:
            raise EditError("a connection joins two different entries")
        self._entity_ids(ctx, [r["src"], r["dst"]], r["from"])
        if op.get("basis") is not None:
            r["basis"] = self._choice(op, "basis", BASES)
        if op.get("label") is not None:
            r["label"] = self._text(ctx, "label", op["label"], r["from"])
        if "kind" in op:
            if op["kind"]:
                r["kind"] = self._choice(op, "kind", RELATION_KINDS)
            else:
                r.pop("kind", None)
        ctx.changed.add(j)
        return f"Edited a connection of {ctx.names(r['src'])[0]}", {"id": iid, "src": old["src"], "dst": old["dst"], "text": old["label"]}

    def _op_delete_relation(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, r, _ = ctx.item("relation", iid)
        ctx.docs[j]["relations"].remove(r)
        for d in ctx.docs:
            for x in d.get("relations", []):
                self._unreplace(x, {iid})
        ctx.changed.add(j)
        return f"Deleted a connection of {ctx.names(r['src'])[0]}", {"id": iid, "src": r["src"], "dst": r["dst"], "text": r["label"]}

    # events

    def _op_add_event(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        summary, seq = self._need(op, "summary", "seq")
        anchor = ctx.reveal(seq)
        ents = self._entity_ids(ctx, op.get("entities", []), anchor)
        summary = self._text(ctx, "event", summary, anchor)
        item = {"id": ctx.new_id(f"{ctx.book_key}.ev"), "from": anchor, "summary": summary, "entities": ents, "evidence": [anchor]}
        kind = self._event_kind(op)
        if kind:
            item["kind"] = kind
        ords = ctx.ords()
        evs = ctx.doc["events"]
        pos = next((k for k, x in enumerate(evs) if ords.get(x["from"], 0) > ords[anchor]), len(evs))
        evs.insert(pos, item)
        ctx.changed.add(ctx.i)
        ctx.extra["id"] = item["id"]
        return "Added an event", {"id": item["id"], "text": summary}

    def _op_edit_event(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, ev, _ = ctx.item("event", iid)
        if j != ctx.i:
            raise EditError("that event belongs to another book of the series; open that book to edit it")
        old = ev["summary"]
        if op.get("seq") is not None:
            ev["from"] = ctx.reveal(op["seq"])
            ev["evidence"] = [ev["from"]]
        if op.get("entities") is not None:
            ev["entities"] = self._entity_ids(ctx, op["entities"], ev["from"])
        else:
            self._entity_ids(ctx, ev["entities"], ev["from"])
        if op.get("summary") is not None:
            ev["summary"] = self._text(ctx, "event", op["summary"], ev["from"])
        if "kind" in op:
            kind = self._event_kind(op)
            if kind:
                ev["kind"] = kind
            else:
                ev.pop("kind", None)
        ords = ctx.ords()
        ctx.doc["events"].sort(key=lambda x: ords.get(x["from"], 0))
        ctx.changed.add(j)
        return "Edited an event", {"id": iid, "text": old}

    @staticmethod
    def _event_kind(op: dict) -> str | None:
        """An event's weight: "turn" or "minor", or None for an ordinary development ("" and "plot" count as none)."""
        kind = op.get("kind")
        if kind in (None, "", "plot"):
            return None
        if kind not in EVENT_KINDS:
            raise EditError("kind must be turn, minor, or left out")
        return kind

    def _op_delete_event(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, ev, _ = ctx.item("event", iid)
        ctx.docs[j]["events"].remove(ev)
        ctx.changed.add(j)
        return "Deleted an event", {"id": iid, "text": ev["summary"]}

    # recaps

    def _op_add_recap(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        s, seq = self._need(op, "text", "seq")
        scope = self._choice(op, "scope", SCOPES, "story")
        anchor = ctx.reveal(seq)
        s = self._text(ctx, scope, s, anchor)
        item = {"id": ctx.new_id(f"{ctx.book_key}.recap"), "from": anchor, "text": s, "evidence": [anchor]}
        if scope == "chapter":
            title = " ".join(str(op.get("title") or "").split())
            if not title:
                raise EditError("a chapter recap needs the chapter's title")
            check_wording("title", title)
            item["scope"], item["title"] = "chapter", title
        ords = ctx.ords()
        rcs = ctx.doc["recaps"]
        pos = next((k for k, x in enumerate(rcs) if ords.get(x["from"], 0) > ords[anchor]), len(rcs))
        rcs.insert(pos, item)
        ctx.changed.add(ctx.i)
        ctx.extra["id"] = item["id"]
        return f"Added a {scope} recap", {"id": item["id"], "text": s}

    def _op_edit_recap(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, rc, _ = ctx.item("recap", iid)
        if j != ctx.i:
            raise EditError("that recap belongs to another book of the series; open that book to edit it")
        old = rc["text"]
        scope = rc.get("scope", "story")
        if op.get("seq") is not None:
            rc["from"] = ctx.reveal(op["seq"])
            rc["evidence"] = [rc["from"]]
        if op.get("title") is not None and scope == "chapter":
            title = " ".join(str(op["title"]).split())
            if not title:
                raise EditError("a chapter recap needs the chapter's title")
            check_wording("title", title)
            rc["title"] = title
        if op.get("text") is not None:
            rc["text"] = self._text(ctx, scope, op["text"], rc["from"])
        ords = ctx.ords()
        ctx.doc["recaps"].sort(key=lambda x: ords.get(x["from"], 0))
        ctx.changed.add(j)
        return f"Edited a {scope} recap", {"id": iid, "text": old}

    def _op_delete_recap(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        iid, = self._need(op, "id")
        j, rc, _ = ctx.item("recap", iid)
        ctx.docs[j]["recaps"].remove(rc)
        ctx.changed.add(j)
        return f"Deleted a {rc.get('scope', 'story')} recap", {"id": iid, "text": rc["text"]}

    # coverage

    def _op_set_complete(self, ctx: _Ctx, op: dict) -> tuple[str, dict]:
        complete = bool(op.get("complete"))
        cov = ctx.doc["coverage"]
        cov["complete"] = complete
        if complete and ctx.doc["anchors"]:
            cov["through"] = ctx.doc["anchors"][-1]["id"]
        ctx.changed.add(ctx.i)
        return "Marked the index complete" if complete else "Marked the index as still in progress", {}

    # ---- edits that outlive a rebuild ------------------------------------------------------------

    def _sidecar(self, doc: dict) -> Path | None:
        th = next((ed.get("textHash") for ed in doc.get("editions", []) if ed.get("builtFrom", True) and ed.get("textHash")), None)
        return self.dir / f"{th}.json" if th and re.fullmatch(r"[0-9a-f]{8,128}", th) else None

    def edits_for(self, doc: dict) -> list[dict]:
        p = self._sidecar(doc)
        if p is None or not p.exists():
            return []
        try:
            return json.loads(p.read_text()).get("edits", [])
        except (OSError, json.JSONDecodeError):
            return []

    def _write_sidecar(self, p: Path, edits: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"textHash": p.stem, "edits": edits}, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(p)

    def _record(self, doc: dict, op: dict, target: dict, journal: str) -> None:
        p = self._sidecar(doc)
        if p is None:
            return
        edits = self.edits_for(doc)
        edits.append({"at": _now(), "journal": journal, "op": op, "target": target})
        self._write_sidecar(p, edits[-500:])

    def _forget(self, doc: dict, journal: str) -> None:
        p = self._sidecar(doc)
        if p is None:
            return
        edits = [e for e in self.edits_for(doc) if e.get("journal") != journal]
        if p.exists():
            self._write_sidecar(p, edits)

    def reapply(self, path: Path, user: str = "admin") -> dict:
        """Apply the recorded edits for this book to a freshly built index at `path`, by id where it
        survived and by the original text otherwise. Nothing is journaled: the build is the baseline."""
        doc = json.loads(path.read_text())
        edits = self.edits_for(doc)
        if not edits:
            return {"applied": 0, "skipped": 0}
        book = self.book_for(doc)
        ctx = _Ctx([], [doc], 0, book, self.eligible(book) if book else [])
        applied, skipped = 0, []
        for e in edits:
            op = self._retarget(ctx, e.get("op") or {}, e.get("target") or {})
            if op is None:
                skipped.append(e)
                continue
            try:
                self._apply(ctx, op)
                applied += 1
            except (EditError, WordingError, KeyError, TypeError, ValueError):
                skipped.append(e)
        if not applied:
            return {"applied": 0, "skipped": len(skipped)}
        errors = [x for x in validate.check_index(doc) if x.level == "error"]
        if errors:
            return {"applied": 0, "skipped": len(edits), "failed": str(errors[0])}
        self._touch(doc, user)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)
        return {"applied": applied, "skipped": len(skipped)}

    @staticmethod
    def _retarget(ctx: _Ctx, op: dict, target: dict) -> dict | None:
        """The op with ids that exist in the new document, or None when its target is gone."""
        op = dict(op)
        name = op.get("op", "")
        norm = lambda s: " ".join(str(s or "").split()).casefold()  # noqa: E731
        ents = {e["id"]: e for e in ctx.doc.get("entities", [])}
        if name in ("add-entity", "set-complete"):
            return op
        if name in ("set-type", "delete-entity", "add-name", "add-facet"):
            return op if op.get("entity") in ents else None
        if name in ("edit-name", "delete-name"):
            ent = ents.get(op.get("entity"))
            if not ent:
                return None
            idx = next((k for k, n in enumerate(ent["names"]) if norm(n["value"]) == norm(target.get("text"))), None)
            if idx is None:
                return None
            return {**op, "index": idx}
        if name in ("edit-facet", "delete-facet"):
            ent = ents.get(target.get("entity"))
            if not ent:
                return None
            have = {f["id"] for f in ent["facets"]}
            if op.get("id") in have:
                return op
            f = next((f for f in ent["facets"] if norm(f["text"]) == norm(target.get("text"))), None)
            return {**op, "id": f["id"]} if f else None
        if name in ("add-relation", "add-event", "add-recap"):
            ids = [op.get("src"), op.get("dst")] if name == "add-relation" else op.get("entities", [])
            return op if all(x in ents for x in ids) else None
        kind = {"edit-relation": "relation", "delete-relation": "relation", "edit-event": "event", "delete-event": "event",
                "edit-recap": "recap", "delete-recap": "recap"}.get(name)
        if kind is None:
            return None
        pool = ctx.doc.get(kind + "s", [])
        if any(x["id"] == op.get("id") for x in pool):
            return op
        field = {"relation": "label", "event": "summary", "recap": "text"}[kind]
        for x in pool:
            if norm(x[field]) != norm(target.get("text")):
                continue
            if kind == "relation" and (x["src"], x["dst"]) != (target.get("src"), target.get("dst")):
                continue
            return {**op, "id": x["id"]}
        return None
