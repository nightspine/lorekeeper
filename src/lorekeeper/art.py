"""Art sets: pictures for an index, drawn ahead of time and gated by anchor (CLAUDE.md §8f).

The model only proposes. A text model (the run's cheap one) writes every prompt; an image model draws it.
Python decides what is drawn, the anchor of every picture (a picture shows from that anchor on and is
written only from content visible at it), the references attached, the cost cap, and drops what fails a
check, with a reason. Pictures never go into the index document: an art set is a manifest (`art.json`)
and the picture files, one per edition and style, behind a `Sink`.

Consistency comes from three notes pasted verbatim into every prompt (code appends them, the writer never
re-words them): a *world note* for the whole book (inherited down a series), an *appearance note* per
entity (amended, never rewritten, when its look changes) and, as a reference picture, the entity's current
portrait.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import anchors as A
from . import epub, gating, images, llm
from . import portraits as P
from .common_words import SENTENCE_FIRST
from .generate import ABOUT_THE_TEXT

VERSION = "0.1"

STYLES: dict[str, str] = {
    "digital-art": "Digital art, clean detailed rendering, rich colour, soft cinematic lighting.",
    "comic-strip": "Comic strip panel, bold ink outlines, flat colour, halftone shading.",
    "anime": "Anime illustration, clean line art, expressive faces, soft cel shading.",
    "realism": "Realistic painting, natural proportions, careful detail, natural light.",
    "hand-drawn": "Hand-drawn illustration, loose pencil and pen lines, light washes of colour on paper.",
    "oil-painting": "Oil painting, visible brushwork, deep colour, dramatic chiaroscuro.",
    "epic-sci-fi": "Epic science-fiction concept art, vast scale, hard-surface detail, glowing light against dark.",
    "watercolour": "Watercolour painting, translucent washes, soft edges, pale paper showing through.",
    "ink": "Black ink drawing, fine cross-hatching, strong contrast, aged paper.",
}
NO_TEXT = "No text, captions, lettering, titles or signatures anywhere in the picture."
SHAPE_OF = {"plate": "landscape", "event": "landscape", "portrait": "portrait", "place": "landscape", "thing": "landscape"}
ENTITY_KINDS = ("portrait", "place", "thing")
LEVELS = ("main", "all")
NOTE_TYPES = ("person", "creature", "place", "object")
PORTRAIT_TYPES = ("person", "creature")

WORLD_MIN_WORDS = 1500  # the world note is written from the first story chapter, and the next ones until this many words
MAX_REFERENCES = 4     # pictures attached to a scene, most tied first
MAX_VERSIONS = 3       # portraits per entity per book (first one and look changes)
MIN_GAP = 30           # blocks between two portraits of one entity
PASSAGE_WORDS = 600
WORKERS = 3
SCENE_WORDS = (35, 170)
NOTE_WORDS = (10, 60)
WORLD_WORDS = (25, 110)

# What a new profile or fact says when the entity's look has changed (and the earlier text did not).
LOOK_PROFILE = re.compile(r"\b(older|aged|ageing|scars?|scarred|wounds?|wounded|bandaged|limps?|limping|disguised|blinded|crippled|"
                          r"maimed|bald|shaved|crowned|uniform|armou?red|masked|transformed|dead|dies|died|killed|corpse|"
                          r"pregnant|unrecognisable|unrecognizable|emaciated|haggard)\b", re.IGNORECASE)
LOOK_STANDING = re.compile(r"\b(dead|dies|died|death|killed|murdered|slain|wounded|injured|crippled|blinded|transformed|turned|"
                           r"disguised|unmasked|captured|imprisoned|pregnant)\b", re.IGNORECASE)

SYSTEM = """You prepare pictures for a spoiler-free illustrated companion to a novel.
Use ONLY the passage, the notes and the index content you are given. Never use outside knowledge of this book, its author or its sequels.
Describe things as a careful reader understands them at this point: no hindsight, no hints about what comes later."""

SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
SCENE_SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"},
                                                                        "shows": {"type": "array", "items": {"type": "string"}}}}

SCREEN = """These are the events of {where}, numbered. Which of them are a visible moment: a place, people or things that could be
seen in one picture ("the innkeeper presses a crucifix into Harker's hand" is; "Harker concludes he is a prisoner" and
"the war is declared" are not)?

Return "moments": the numbers of the events that are a visible moment, in order, leaving out the others.

Events:
{events}"""
# The screening call only chooses: what it sees of the whole chapter never reaches a picture's prompt.
SCREEN_SCHEMA = {"type": "object", "required": ["moments"], "properties": {"moments": {"type": "array", "items": {"type": "integer"}}}}

WORLD = """Below is the opening of a novel as a reader's companion records it: the recaps of its first chapters, where scenes
are set, and a few passages. Write the visual world note an illustrator needs to draw anything in this book consistently.

Cover: era and technology, architecture, clothing, landscape, light and tone, as these pages show them. 40 to 80 words,
plain descriptive sentences, no names of any person, creature or place, nothing known from elsewhere (you may recognise the book:
use nothing you know from outside these pages), no plot. Say what the world looks like.

Recaps and settings:
{recaps}

Passages:
{passages}"""

WORLD_MORE = """The world note of this series so far is:
"{base}"
This is the opening of a later book. If its chapters show the world has changed or widened (a new era, technology, region, culture),
write ONE sentence adding what an illustrator now needs, as the pages show it, with no names. If nothing changed, return an empty text.
"""

APPEAR = """Write a short, fixed appearance note for {who} ({kind}) so an illustrator draws it the same way every time.
20 to 40 words: say who or what it is by role in a few words ("a young solicitor", "a ruined castle"), then its look as the
passages and notes show it ({looks}). Look only: no situation (prisoner, guest, host), no plot, no personality, no mood. Do not use
its name or any other name. State only what is shown: leave out anything not shown. If the passages and notes show nothing of its
look beyond who or what it is (a name only mentioned, an author, a person only written about), return an EMPTY text: never write
that nothing is shown. {more}
{prev}
What the companion has noted:
{notes}

Passages:
{passages}"""
LOOKS = {"person": "age, build, face, hair, clothing, marks", "creature": "form, size, colouring, marks",
         "place": "form, materials, state, surroundings", "object": "form, materials, size, state"}
AMEND = """Its fixed note until now was:
"{prev}"
The text now shows a change: "{change}". Write the AMENDED note: the same {who} with what changed, kept short. Begin with "The same".
Do not use the word "now"; never start over."""

WRITE = """Write the picture prompt for an illustrator. The picture shows {what}

World (true of every picture in this book; the code adds it, do not repeat it): {world}
{figures}
Index content, for context only:
{context}

Passage:
{passage}

Rules:
- One scene, one moment: what is in frame, the composition and the light. 60 to 120 words, plain sentences, present tense.
- Describe people by look and role, never by name ("a gaunt old man in black", "the young solicitor"); name no person, creature or group.
  A place may be named.
- Put nothing in the picture that the passage or the notes do not show. You may recognise the book: use nothing from outside these pages.
- The figures' fixed notes are added after your text by the program: say which figure is which by role, and do NOT repeat their looks.
- No text, captions, signs with words, titles or signatures in the picture. No style words: the style is added later.
- Never show what comes after the passage.
{extra}- Also return "shows": the names, as listed above, of the figures that are IN your picture (this list is the only place a name may
  appear; leave out any figure you do not draw).
"""
KINDS_WHAT = {
    "plate": "the main moment of a chapter: {frame}",
    "event": "this moment: {frame}",   # frame = the event's own summary
    "portrait": "a portrait of {who}, as they are at this point: head and shoulders or half length, a simple background that suits the world.",
    "place": "{who}, a place, as it looks at this point; people only if the passages put them there.",
    "thing": "{who}, an object, as it looks at this point, in a setting that suits the world.",
}
REWRITE = """\n\nYour first answer was: "{text}"\nIt was rejected: {why}. Write it again, fixing that."""
SOFTEN = """\n\nThe image model refused to draw this: "{text}". Write a quieter composition of the same moment: nothing graphic,
nothing violent or frightening in view (a doorway, a shadow, a face turned away, the room afterwards). Keep who and where."""


class Sink(Protocol):
    """Where one edition's art set lives. `FileSink` is a folder; the web store implements the same four calls."""

    def manifest(self) -> dict | None: ...
    def save(self, manifest: dict) -> None: ...
    def put(self, file: str, data: bytes) -> None: ...
    def read(self, file: str) -> bytes | None: ...


class FileSink:
    def __init__(self, folder: str | Path) -> None:
        self.dir = Path(folder)

    def manifest(self) -> dict | None:
        p = self.dir / "art.json"
        return json.loads(p.read_text()) if p.exists() else None

    def save(self, manifest: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "art.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.dir / "art.json")

    def put(self, file: str, data: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / file).write_bytes(data)

    def read(self, file: str) -> bytes | None:
        p = self.dir / file
        return p.read_bytes() if p.is_file() else None


def style_sentence(style: str) -> str:
    return STYLES.get(style) or " ".join(style.split())


def style_slug(style: str) -> str:
    if style in STYLES:
        return style
    return re.sub(r"[^a-z0-9]+", "-", style.lower()).strip("-")[:40] or "custom"


def default_dir(index_path: str | Path, doc: dict, style: str) -> Path:
    built = next((e for e in doc.get("editions", []) if e.get("builtFrom")), doc.get("editions", [{}])[0])
    return Path(index_path).parent / "art" / str(built.get("textHash", "unknown")).replace(":", "-") / style_slug(style)


def blank(text_hash: str, style: str) -> dict:
    return {"lorekeeperArt": VERSION, "textHash": text_hash, "style": style, "world": None, "pictures": [],
            "appearance": {}, "screened": {}, "skipped": [], "cost": 0.0}


def size_of(data: bytes) -> tuple[int | None, int | None]:
    """Width and height read from a PNG, JPEG or WebP header; (None, None) when not recognised."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            tag = data[12:16]
            if tag == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if tag == b"VP8L":
                b = struct.unpack("<I", data[21:25])[0]
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if tag == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    except (struct.error, IndexError):
        pass
    return None, None


def _ext(mime: str) -> str:
    return {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime, "bin")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _words(text: str) -> int:
    return len(text.split())


class Known:
    """The capitalised words the book has used up to a block, plus every word of the earlier books of the series (which the
    reader has finished): the set `portraits._checked` wants, by position."""

    def __init__(self, first: dict[str, int], at: int, earlier: frozenset[str] = frozenset()) -> None:
        self.first, self.at, self.earlier = first, at, earlier

    def __contains__(self, w: str) -> bool:
        return w in self.earlier or self.first.get(w, 1 << 30) <= self.at


@dataclass
class Book:
    """One book of the run: its index, its EPUB (None for an earlier book that is only read), where its art set lives
    and what is drawn for it: `level` "main" or "all" chapter pictures or None for none; `draw` False reads the set only."""
    doc: dict
    epub: epub.Book | None
    sink: Sink
    level: str | None = "main"
    draw: bool = True


@dataclass
class Job:
    kind: str                 # plate, event, portrait, place, thing
    target: str               # chapter title, event id or entity id
    bi: int                   # book index in the run
    anchor: str               # the anchor the picture shows from (before any hold, see Illustrator._anchor)
    event: dict | None = None
    chapter: dict | None = None
    change: str = ""          # a portrait's reason for a new version (what changed), "" for the first
    shape: str = ""


@dataclass
class Prompt:
    text: str                 # what the image model gets
    scene: str                # what the writer wrote
    anchor: str
    shape: str
    notes: list[tuple[str, str]] = field(default_factory=list)       # (entity id, appearance note) pasted into text
    references: list[tuple[int, dict]] = field(default_factory=list)  # (book index, picture record) attached
    sources: list[str] = field(default_factory=list)                 # anchors (of this book) it was written from
    writer: str = ""          # the writer's prompt, kept so a refusal can be rewritten from it
    check: str = ""           # the text the checks read it against


@dataclass
class Report:
    drawn: int = 0
    refused: int = 0            # pictures given up after the provider refused (each counted once; a refusal is not also a skip)
    softened: int = 0           # pictures drawn from a quieter rewrite after a refusal
    skipped: Counter = field(default_factory=Counter)
    cost: float = 0.0           # image calls
    at_risk: float = 0.0        # the price of image calls that failed after they were sent (maybe billed; counted in the cap)
    text_cost: float = 0.0
    stopped: str = ""           # why the run stopped early ("" = it did not)
    held: int = 0               # pictures moved later to the world note's anchor (the end of its first story chapter)
    reasons: list[dict] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"drawn {self.drawn}, skipped {sum(self.skipped.values())}, refused {self.refused}"
               + (f" ({self.softened} drawn quieter after a refusal)" if self.softened else "") + f"; images ${self.cost:.2f}"
               + (f", text ${self.text_cost:.4f}" if self.text_cost else "")
               + (f", up to ${self.at_risk:.2f} for calls that failed after sending" if self.at_risk else "")]
        if self.stopped:
            out.append(f"stopped: {self.stopped}")
        if self.held:
            out.append(f"{self.held} picture(s) at the start of the book are held to the end of the chapter the world note is "
                       "written from")
        out += [f"  {n} x {why}" for why, n in sorted(self.skipped.items())]
        out += [f"  - {r['target']}: {r['reason']}" for r in self.reasons]
        return out


@dataclass(frozen=True)
class _Names:
    """The names a prompt may not use: full names of two words or more (one alternation), name words that are not ordinary
    sentence words ("Mira"), and one-word names that are ordinary words ("Grace"), which count only away from a sentence's start."""
    phrases: re.Pattern | None
    tokens: frozenset[str]
    alone: frozenset[str]

    def find(self, text: str) -> str:
        if self.phrases is not None and (m := self.phrases.search(text)):
            return m.group(0)
        for m in re.finditer(r"[A-Z][\w'’-]{2,}", text):
            w = m.group(0)
            for x in (w, re.sub(r"['’]s$", "", w)):
                if x in self.tokens:
                    return x
                if x in self.alone and not re.search(r"(^|[.!?:;\"“”]\s*)$", text[:m.start()]):
                    return x
        return ""


class NotVisible(ValueError):
    """The picture asked for sits after the reader's position."""


class TextFailed(ValueError):
    """The text model failed twice on one call: that picture (or note) is skipped, the run goes on."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("skipped: text model failed")
        self.detail = detail


def _seqs(anchors: list[dict], ep: epub.Book | None) -> dict[str, int]:
    """Block of every anchor. With the EPUB, resolved by fingerprint (the `seq` hint is optional in the schema); an anchor
    that cannot be found takes the next found one's block, as a reader's client would (SPEC §3.6). Without it, the hints,
    else the anchors' order (an earlier book that is only read needs no more)."""
    if ep is None:
        if all("seq" in a for a in anchors):
            return {a["id"]: a["seq"] for a in anchors}
        return {a["id"]: n for n, a in enumerate(anchors)}
    found = A.resolve(anchors, ep.blocks)
    if not any(v is not None for v in found.values()):
        raise ValueError("none of the index's anchors is in this EPUB: is it the edition the index was built from?")
    never = len(ep.blocks)               # past the last block: a trailing unresolved anchor is never reached
    return {a["id"]: (never if loc == A.INFINITY else int(loc))
            for a, loc in zip(anchors, A.effective_locations(anchors, found))}



class _B:
    """What the pipeline needs of one book, worked out once."""

    def __init__(self, i: int, b: Book) -> None:
        self.i, self.b, self.doc, self.epub = i, b, b.doc, b.epub
        d = self.doc
        self.anchors = d["anchors"]
        if not self.anchors:
            raise ValueError(f"{d.get('work', {}).get('title', 'the index')}: the index has no anchors")
        self.seq_of = _seqs(self.anchors, self.epub)
        self.ord_of = {a["id"]: n for n, a in enumerate(self.anchors)}
        self.aseqs = [self.seq_of[a["id"]] for a in self.anchors]
        self.ents = {e["id"]: e for e in d["entities"]}
        self.events = {e["id"]: e for e in d.get("events", [])}
        self.lo = self.aseqs[0]
        self.hi = max(self.seq_of.get(d.get("coverage", {}).get("through"), self.aseqs[-1]), self.lo)
        if self.epub is not None:
            self.hi = min(self.hi, len(self.epub.blocks) - 1)
        self.text: dict[int, str] = {}
        self.first: dict[str, int] = {}
        if self.epub is not None:
            self.text = {x.seq: x.raw for x in self.epub.blocks[self.lo:self.hi + 1]}
            for s, t in self.text.items():
                for w in set(P._CAPS.findall(t)):
                    self.first.setdefault(w, s)
        else:                              # only ever a finished earlier book: every word of its index was read
            for w in set(P._CAPS.findall(json.dumps(d, ensure_ascii=False))):
                self.first.setdefault(w, self.lo)
        self.pats = P._patterns(d)
        self._hits: dict[str, list[tuple[int, int]]] = {}
        self.chapters = self._chapters()

    def anchor_at(self, seq: int) -> str:
        """The first anchor at or after a block."""
        return self.anchors[min(bisect_left(self.aseqs, seq), len(self.anchors) - 1)]["id"]

    def hits(self, eid: str) -> list[tuple[int, int]]:
        """(block, seq of the anchor that reveals the matching name) for the blocks that mention the entity."""
        if eid not in self._hits:
            found: dict[int, int] = {}
            for p, at in self.pats.get(eid, ()):
                for s, t in self.text.items():
                    if p.search(t):
                        found[s] = min(found.get(s, 1 << 30), self.seq_of.get(at, self.hi))
            self._hits[eid] = sorted(found.items())
        return self._hits[eid]

    def _chapters(self) -> list[dict]:
        d = self.doc
        recaps = [r for r in d.get("recaps", []) if r.get("scope") == "chapter" and r["from"] in self.seq_of]
        marks = [(c.title, c.start) for c in self.epub.chapters] if self.epub and self.epub.chapters else []
        if not marks:                      # no table of contents: the recaps' own titles cut the book
            start = self.lo
            for r in sorted(recaps, key=lambda r: self.seq_of[r["from"]]):
                if not marks or marks[-1][0] != r.get("title"):
                    marks.append((r.get("title") or f"Part {len(marks) + 1}", start))
                start = self.seq_of[r["from"]] + 1
        out, seen = [], Counter()
        for n, (title, start) in enumerate(marks):
            end = marks[n + 1][1] - 1 if n + 1 < len(marks) else max(self.hi, start)
            evs = [e for e in d.get("events", []) if e["from"] in self.seq_of and start <= self.seq_of[e["from"]] <= end]
            rcs = [r for r in recaps if start <= self.seq_of[r["from"]] <= end]
            if not evs and not rcs:
                continue
            seen[title] += 1
            key = title if seen[title] == 1 else f"{title} ({seen[title]})"
            # the chapter's last anchor: its recaps' and every event's (an event may come after the last recap), so a plate,
            # whose moment, figures and passage come from any of them, is never written from content after it
            last = max([r["from"] for r in rcs] + [e["from"] for e in evs], key=self.ord_of.get)
            words = sum(_words(t) for s, t in self.text.items() if start <= s <= end)
            out.append({"n": n + 1, "title": key, "lo": start, "hi": end, "events": evs, "recaps": rcs, "last": last, "words": words})
        return out

    def chapter_of(self, anchor: str) -> dict | None:
        s = self.seq_of[anchor]
        return next((c for c in self.chapters if c["lo"] <= s <= c["hi"]), None)


def _key(rec: dict) -> tuple:
    return rec["kind"], rec["target"], rec["from"]


# What a failed check is called in the report.
def _reason(why: str) -> str:
    for start, name in (("unknown name", "dropped: unknown name in prompt"), ("name in the text", "dropped: name in prompt"),
                        ("wording", "dropped: hindsight wording in prompt"), ("tie not in the text", "dropped: tie not in the text"),
                        ("too short", "dropped: prompt too short"), ("too long", "dropped: prompt too long"),
                        ("empty", "dropped: empty prompt")):
        if why.startswith(start):
            return name
    return f"dropped: {why}"


class Illustrator:
    """The pipeline over one or more books in series reading order. `run()` draws whole sets; `job`, `make_prompt`
    and `draw` are the pieces the Reader's on-demand drawing calls one at a time."""

    def __init__(self, books: list[Book], text, image, style: str, *, portraits: bool = True, quality: str = "standard",
                 cap: float | None = None, max_pictures: int | None = None, chapters: set[int] | None = None,
                 series: dict | None = None, workers: int = WORKERS, log: Callable = print, progress: Callable | None = None,
                 pause: float = 1.0) -> None:
        self.books = [_B(i, b) for i, b in enumerate(books)]
        if len(self.books) > 1:
            ids = {(b.doc.get("series") or {}).get("id") for b in self.books}
            if None in ids or len(ids) > 1 or any(not (b.doc.get("series") or {}).get("book") for b in self.books):
                raise ValueError("several indexes are drawn together only as books of one series (each with the same "
                                 "`series.id` and its own `series.book`); draw standalone books one at a time")
        self.text, self.image, self.style = text, image, style
        self.portraits, self.quality, self.cap, self.max_pictures = portraits, quality, cap, max_pictures
        self.chapter_filter, self.series, self.workers = chapters, series, workers
        self.log, self.progress, self.pause = log, progress, pause
        self.provider = getattr(image, "provider", "") or ""
        self.model = getattr(image, "name", "") or ""
        self.report = Report()
        self.lock = threading.RLock()
        self.stop = ""
        self.reserved = 0.0          # price of the draw calls in flight
        self.in_flight = 0
        self.last_cost = 0.0         # the last picture's cost: the estimate when a price is unknown
        self.done = self.total = 0
        self._usage0 = self._usage()
        self._views: dict[tuple, dict] = {}
        self._view_locks: dict[tuple, threading.Lock] = {}
        self._entity_locks: dict[str, threading.Lock] = {}
        self._earlier = [frozenset().union(*(b.first for b in self.books[:i])) for i in range(len(self.books))]
        self._dirty: set[int] = set()     # books whose sink received a picture
        self.man: list[dict] = []
        self.styles: list[str] = []       # each set's own style text: the one stored first, for every style of the same slug
        for b in self.books:
            m = b.b.sink.manifest()
            th = self._hash(b)
            if m:
                if m.get("textHash") and th and m["textHash"] != th:
                    raise ValueError(f"the art set there belongs to another edition ({m['textHash'][:12]}...), not "
                                     f"{b.doc.get('work', {}).get('title', 'this book')}: choose another folder")
                if style_slug(str(m.get("style", ""))) != style_slug(style):
                    raise ValueError(f"the art set there is of another style ({m.get('style')!r}): choose another folder")
            else:
                m = blank(th, style)
            for k, v in blank("", "").items():
                m.setdefault(k, v)
            m["textHash"] = m["textHash"] or th
            m["skipped"] = [s for s in m["skipped"] if s.get("final")]
            self.man.append(m)
            self.styles.append(m["style"] or style)
        self.names = self._banned_names()

    # ---- small helpers

    @staticmethod
    def _hash(b: _B) -> str:
        built = next((e for e in b.doc.get("editions", []) if e.get("builtFrom")), (b.doc.get("editions") or [{}])[0])
        return built.get("textHash", "")

    def _save(self, bi: int) -> None:
        """Write a book's manifest: a book that is drawn, or a read-only one whose sink received a picture (on demand)."""
        if self.books[bi].b.draw or bi in self._dirty:
            self.books[bi].b.sink.save(self.man[bi])

    def _complete(self, prompt: str, schema: dict = SCHEMA) -> dict:
        """One text call; a failure (provider error, timeout, a reply that is not JSON) is retried once, then TextFailed."""
        for attempt in (0, 1):
            try:
                out = self.text.complete(SYSTEM, prompt, schema)
                return out if isinstance(out, dict) else {}
            except llm.Cancelled:
                raise
            except Exception as e:      # noqa: BLE001 - every backend has its own errors; one picture never stops the run
                if attempt:
                    raise TextFailed(f"{type(e).__name__}: {str(e)[:160]}") from e
                time.sleep(self.pause)
        return {}

    def _usage(self) -> float:
        return float(getattr(getattr(self.text, "usage", None), "cost_usd", 0.0) or 0.0)

    def text_spent(self) -> float:
        return self._usage() - self._usage0

    def _banned_names(self) -> dict[int, "_Names"]:
        """Per book, the names of every person, creature and group of it and the earlier books: a prompt names none."""
        out: dict[int, _Names] = {}
        phrases: set[str] = set()
        tokens: set[str] = set()
        alone: set[str] = set()
        for b in self.books:
            for e in b.doc["entities"]:
                if e["type"] not in ("person", "creature", "group"):
                    continue
                for n in e["names"]:
                    value = " ".join(re.sub(r"\(.*?\)", " ", n["value"]).split())
                    value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.IGNORECASE)
                    toks = [t for t in re.findall(r"[A-Z][\w'’-]{2,}", value)
                            if t.lower() not in P.TITLES and t.lower() not in ("the", "and", "van", "von")]
                    if len(value.split()) > 1:
                        phrases.add(value)
                    elif toks and toks[0].lower() in SENTENCE_FIRST:
                        alone.add(toks[0])      # "Grace", "the Stranger": a name only when not opening a sentence
                    for t in toks:
                        if t.lower() not in SENTENCE_FIRST:
                            tokens.add(t)       # "Mira", "Harker"; never "Order" or "Dawn" alone
            rx = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(x) for x in sorted(phrases, key=len, reverse=True)) + r")(?!\w)") \
                if phrases else None
            out[b.i] = _Names(rx, frozenset(tokens), frozenset(alone))
        return out

    def _vet(self, text, bi: int, anchor: str, source: str, words: tuple[int, int]) -> tuple[str | None, str]:
        """(text, "") when a written text is usable at an anchor, else (None, reason)."""
        text = " ".join(str(text or "").split())
        n = _words(text)
        if n and n < words[0]:
            return None, "too short"
        if n > words[1]:
            return None, "too long"
        got, why = P._checked(text, Known(self.books[bi].first, self.books[bi].seq_of[anchor], self._earlier[bi]), source or None, None,
                              limit=words[1] * 9)
        if got is None:
            return None, why
        if ABOUT_THE_TEXT.search(got):
            return None, "wording: about the passage"
        hit = self.names[bi].find(got)
        if hit:
            return None, f"name in the text: {hit}"
        return got, ""

    def _write_full(self, prompt: str, bi: int, anchor: str, source: str, words: tuple[int, int],
                    schema: dict = SCHEMA) -> tuple[str | None, str, dict]:
        """One call, one rewrite when a check fails. (text, "", the reply) or (None, reason, the reply)."""
        out = self._complete(prompt, schema)
        got, why = self._vet(out.get("text"), bi, anchor, source, words)
        if got is None and why != "empty":
            out = self._complete(prompt + REWRITE.format(text=str(out.get("text", ""))[:600], why=why), schema)
            got, why = self._vet(out.get("text"), bi, anchor, source, words)
        return got, why, out

    def _write(self, prompt: str, bi: int, anchor: str, source: str, words: tuple[int, int]) -> tuple[str | None, str]:
        got, why, _ = self._write_full(prompt, bi, anchor, source, words)
        return got, why

    def view(self, bi: int, anchor: str) -> dict:
        """The gated view of books 0..bi at an anchor of book bi: what a reader there may know."""
        key = (bi, anchor)
        with self.lock:              # the global lock only finds the key's own lock: views at other anchors are built at once
            if key in self._views:
                return self._views[key]
            lk = self._view_locks.setdefault(key, threading.Lock())
        with lk:
            if key not in self._views:
                docs = [b.doc for b in self.books[:bi + 1]]
                if len(docs) == 1:
                    v = gating.view(docs, gating.Position(None, anchor))
                else:
                    keys = [d["series"]["book"] for d in docs]
                    if self.series:
                        v = gating.view(docs, gating.Position(keys[-1], anchor), self.series)
                    else:
                        v = gating.view(docs, gating.Position(keys[-1], anchor),
                                        {"defaultOrder": "run", "orders": {"run": keys}}, "run")
                self._views[key] = v
            return self._views[key]

    def passage(self, bi: int, seqs: list[int], upto: int, words: int = PASSAGE_WORDS, context: int = 1) -> tuple[str, list[int]]:
        """The paragraphs at `seqs` with `context` either side, merged, never past block `upto`, within `words`
        (context goes first, then the earliest paragraphs). Returns (text, the blocks used)."""
        b = self.books[bi]
        keep = sorted({s for s in seqs if s in b.text and s <= upto})
        used: list[int] = []
        for ctx in (context, 0):
            ranges: list[list[int]] = []
            for s in keep:
                lo, hi = max(b.lo, s - ctx), min(upto, s + ctx)
                if ranges and lo <= ranges[-1][1] + 1:
                    ranges[-1][1] = max(ranges[-1][1], hi)
                else:
                    ranges.append([lo, hi])
            used = [s for a, z in ranges for s in range(a, z + 1) if s in b.text]
            if sum(_words(b.text[s]) for s in used) <= words:
                break
        while len(used) > 1 and sum(_words(b.text[s]) for s in used) > words:
            used.pop(0)
        out, prev = [], None
        for s in used:
            out.append(("…\n" if prev is not None and s != prev + 1 else "") + b.text[s])
            prev = s
        return "\n".join(out), used

    def _mentions(self, bi: int, eid: str, upto: int, k: int = 6) -> list[int]:
        return P._sample([s for s, at in self.books[bi].hits(eid) if s <= upto and at <= upto], k)

    # ---- the world note

    def _story_chapters(self, bi: int) -> list[dict]:
        """The chapters the world note is written from: the first one with a story event, then the next ones until they hold
        WORLD_MIN_WORDS words of story."""
        out, words = [], 0
        for c in self.books[bi].chapters:
            if not c["events"] and not out:
                continue
            out.append(c)
            words += c["words"]
            if words >= WORLD_MIN_WORDS:
                break
        return out

    def world(self, bi: int) -> dict | None:
        """The book's world note: written once from its first chapters; in a series the first book's, to which a later book may
        append one sentence. A note the person edited is never rewritten. None when none can be written."""
        m = self.man[bi]
        if m.get("world"):
            return m["world"]
        base = next((self.man[j]["world"] for j in range(bi - 1, -1, -1) if self.man[j].get("world")), None)
        chs = self._story_chapters(bi)
        if not chs:
            return base
        b = self.books[bi]
        at = max((c["last"] for c in chs), key=b.ord_of.get)     # a chapter's last anchor is at or after all its events
        set_at = [e for c in chs for e in c["events"][:3] if e.get("setting")]
        recaps = "\n".join(f"- {c['title']}: " + " ".join(r["text"] for r in c["recaps"]) + "".join(
            f" (set at {e['setting']['text']})" for e in c["events"][:3] if e.get("setting")) for c in chs)
        turns = [e for c in chs for e in c["events"] if e.get("kind") == "turn"][:3] or [e for c in chs for e in c["events"]][:2]
        passages, used = self.passage(bi, [b.seq_of[a] for e in turns for a in e["evidence"] if a in b.seq_of], b.seq_of[at], 450)
        sources = sorted({c["last"] for c in chs} | {b.anchor_at(s) for s in used} | {e["from"] for e in set_at + turns}
                         | {r["from"] for c in chs for r in c["recaps"]}, key=b.ord_of.get)
        if any(b.ord_of[a] > b.ord_of[at] for a in sources):    # by construction never; kept as the rule's last guard
            raise ValueError("world note written from content after its anchor")
        body = f"Recaps and settings:\n{recaps}\n\nPassages:\n{passages}"
        try:
            w = self._world_write(bi, base, body, recaps, passages, at, sources)
        except TextFailed as e:
            self.log(f"  world note: text model failed ({e.detail})")
            self._skip(bi, "world note", str(e))
            return base
        if w is None:
            return None
        m["world"] = w
        self._save(bi)
        return w

    def _world_write(self, bi, base, body, recaps, passages, at, sources) -> dict | None:
        if base:
            # an inherited note keeps the anchor of the book it was written in, with that book's key: anchor ids repeat across
            # the books of a series, so an anchor is only ever looked up in its own book
            j = next(j for j in range(bi - 1, -1, -1) if self.man[j].get("world") is base)
            w = {"text": base["text"], "edited": base.get("edited", False), "from": base["from"],
                 "book": base.get("book", self._bkey(j)), "inherited": True}
            if not base.get("edited"):
                more, why = self._write(WORLD_MORE.format(base=base["text"]) + "\n" + body, bi, at, passages + recaps, (0, 70))
                if more:
                    w = {"text": base["text"] + " " + more, "edited": False, "from": at, "book": self._bkey(bi), "base": base["text"],
                         "sources": sources}
                elif why != "empty":
                    self.log(f"  world note: no addition ({why})")
        else:
            got, why = self._write(WORLD.format(recaps=recaps, passages=passages), bi, at, passages + "\n" + recaps, WORLD_WORDS)
            if got is None:
                self.log(f"  world note dropped: {why}")
                self._skip(bi, "world note", _reason(why).replace("prompt", "world note"))
                return None
            w = {"text": got, "edited": False, "from": at, "book": self._bkey(bi), "sources": sources}
        return w

    def _world_text(self, bi: int, pos: tuple[int, int]) -> tuple[str, str | None]:
        """(text, anchor) of the world note a picture at `pos` is written from: the series' older text when an
        appended sentence of this book lies ahead of it."""
        w, own = self._world_of(bi)
        if not w:
            return "", None
        o = self.books[bi].ord_of
        if not own:                    # written in an earlier, finished book: nothing in it lies ahead of the reader
            return w["text"], None
        if w.get("base") and (bi, o[w["from"]]) > pos:
            return w["base"], None
        return w["text"], w["from"]

    def _bkey(self, bi: int) -> str:
        """The key of a book that anchors are stored with: its series book key, else its id."""
        d = self.books[bi].doc
        return str((d.get("series") or {}).get("book") or d.get("id") or bi)

    def _world_of(self, bi: int) -> tuple[dict | None, bool]:
        """(the world note a picture of book bi uses, whether its `from` is an anchor of book bi)."""
        w = self.man[bi].get("world")
        if w:
            book = w.get("book")
            own = (book == self._bkey(bi)) if book is not None else not w.get("inherited")
            return w, own and w["from"] in self.books[bi].ord_of
        return next((self.man[j]["world"] for j in range(bi - 1, -1, -1) if self.man[j].get("world")), None), False

    def _anchor(self, bi: int, anchor: str) -> str:
        """The anchor a picture shows from: a picture of the book's first chapters is held to the end of the chapters its
        world note is written from, so that no picture is written from content after its anchor."""
        w, own = self._world_of(bi)
        b = self.books[bi]
        if w and own and not w.get("base") and b.ord_of[w["from"]] > b.ord_of[anchor]:
            return w["from"]
        return anchor

    # ---- screening and choosing

    @staticmethod
    def _screen_key(evs: list[dict]) -> str:
        """What a chapter's screening was made from: a rebuilt index (other events, other ids, other words) screens again."""
        h = hashlib.sha256(json.dumps([[e["id"], e["from"], e["summary"]] for e in evs], ensure_ascii=False).encode())
        return h.hexdigest()[:16]

    def drawable(self, bi: int, chapter: dict) -> list[str]:
        """The ids of the chapter's visible moments, in order (never a minor event): one cheap call that only chooses, cached in
        the manifest as `screened[title] = {"key", "events"}`; a key that no longer matches the chapter's events screens again.
        Raises TextFailed when the text model fails twice."""
        m = self.man[bi]
        evs = [e for e in chapter["events"] if e.get("kind") != "minor"]
        key = self._screen_key(evs)
        fresh = self.screened(bi, chapter)
        if fresh is not None:
            return fresh
        ids: list[str] = []
        if evs:
            lines = "\n".join(f"{n}. {e['summary']}" + (f" (at {e['setting']['text']})" if e.get("setting") else "")
                              for n, e in enumerate(evs, 1))
            out = self._complete(SCREEN.format(where=chapter["title"], events=lines), SCREEN_SCHEMA)
            for x in out.get("moments", []) if isinstance(out.get("moments"), list) else []:
                try:
                    n = int(x["event"] if isinstance(x, dict) else x)
                except (KeyError, TypeError, ValueError):
                    continue
                if 1 <= n <= len(evs) and evs[n - 1]["id"] not in ids:
                    ids.append(evs[n - 1]["id"])
        with self.lock:
            m["screened"][chapter["title"]] = {"key": key, "events": ids}
            self._save(bi)
        return ids

    def _main_moment(self, bi: int, chapter: dict, moments: list[str]) -> str | None:
        """The event id of the one picture of a chapter: its turning point, else the drawable event its recap gives the most
        words to."""
        b = self.books[bi]
        chosen = set(moments)
        turns = [e["id"] for e in chapter["events"] if e["id"] in chosen and e.get("kind") == "turn"]
        if turns or not moments:
            return turns[0] if turns else None
        recap = set(re.findall(r"[a-z]{4,}", " ".join(r["text"] for r in chapter["recaps"]).lower()))
        return max(moments, key=lambda x: (len(set(re.findall(r"[a-z]{4,}", b.events[x]["summary"].lower())) & recap),
                                           -b.ord_of[b.events[x]["from"]]))

    def _have(self, bi: int) -> set[tuple]:
        """(kind, target, from) of what a book's set already holds or will never draw (a final skip)."""
        return {_key(r) for r in self.man[bi]["pictures"]} | {(x.get("kind"), x["target"], x.get("from")) for x in self.man[bi]["skipped"]}

    def _is_new(self, bi: int, j: Job, have: set[tuple]) -> bool:
        return (j.kind, j.target, self._anchor(bi, j.anchor)) not in have and (j.kind, j.target, j.anchor) not in have

    def _budget(self) -> int | None:
        """How many more pictures the picture limit and the cost cap leave room for (None: no bound known). With an unknown
        price the cap bounds nothing until a picture has cost something."""
        room = None
        with self.lock:
            if self.max_pictures is not None:
                room = max(0, self.max_pictures - self.report.drawn - self.in_flight)
            price = llm.image_price(self.provider, self.model, self.quality, "landscape") or self.last_cost
            if self.cap is not None and price > 0:
                n = max(0, int((self.cap - self._spent() - self.reserved) / price + 1e-9))
                room = n if room is None else min(room, n)
        return room

    def plan(self, bi: int, record: bool = True) -> list[Job]:
        """Every picture a book is to have: chapter plates (and the other drawable moments at level "all"), then portraits.
        `record` False plans without reporting skips (the first count of a series run, planned again before drawing).
        Portraits are drawn first, so when the picture limit or the cost cap leaves room for only some pictures, chapters past
        that room are not screened (no text call for what will not be drawn)."""
        b, jobs = self.books[bi], []
        level = b.b.level
        have = self._have(bi)
        ports: list[Job] = []
        if self.portraits:
            ports = self._portrait_jobs(bi)
            if self.chapter_filter:      # a run limited to some chapters draws portraits only up to the last of them
                end = max((c["hi"] for c in b.chapters if c["n"] in self.chapter_filter), default=-1)
                ports = [j for j in ports if b.seq_of[j.anchor] <= end]
        budget = self._budget()
        room = None if budget is None else max(0, budget - sum(self._is_new(bi, j, have) for j in ports))
        new = 0
        for ch in b.chapters if level else ():
            if self.chapter_filter and ch["n"] not in self.chapter_filter:
                continue
            if not ch["events"]:
                if record:
                    self._skip(bi, ch["title"], "skipped: chapter has no events")
                continue
            if (room is not None and new >= room) or self._over_cap():
                break                        # nothing past here would be drawn
            try:
                moments = self.drawable(bi, ch)
            except TextFailed as e:
                if record:
                    self._skip(bi, ch["title"], str(e), detail=e.detail)
                continue
            main = self._main_moment(bi, ch, moments)
            got: list[Job] = []
            if main:
                got.append(Job("plate", ch["title"], bi, ch["last"], event=b.events[main], chapter=ch, shape="landscape"))
            elif ch["recaps"]:
                got.append(Job("plate", ch["title"], bi, ch["last"], chapter=ch, shape="landscape"))
            if level == "all":
                for x in moments:
                    if x != main:
                        ev = b.events[x]
                        got.append(Job("event", ev["id"], bi, ev["from"], event=ev, chapter=ch, shape="landscape"))
            new += sum(self._is_new(bi, j, have) for j in got)
            jobs += got
        return jobs + ports

    def screened(self, bi: int, chapter: dict) -> list[str] | None:
        """The chapter's drawable event ids from a fresh screening in the manifest, or None when it has not been screened (or
        the index changed since). The manifest holds `screened[<chapter title>] = {"key": <hash of the chapter's non-minor
        events' ids, anchors and summaries>, "events": [event id, ...]}`; titles are the plan's (" (2)" for a repeat)."""
        got = self.man[bi]["screened"].get(chapter["title"])
        evs = [e for e in chapter["events"] if e.get("kind") != "minor"]
        if isinstance(got, dict) and got.get("key") == self._screen_key(evs) and all(x in self.books[bi].events for x in got.get("events", [])):
            return list(got["events"])
        return None

    def counts(self, bi: int = -1) -> dict:
        """What a run on book bi would draw, with no model call (for an estimate): `plates` (chapters with events that get a
        plate: all of them unless a fresh screening found nothing drawable and there is no recap), `portraits` (the
        portrait jobs, versions included), and `chapters`: [{"title", "drawable"}], `drawable` the fresh screening's count of
        visible moments or None when not screened yet (at level "all" a chapter has up to that many pictures)."""
        bi = len(self.books) - 1 if bi < 0 else bi
        b = self.books[bi]
        chapters, plates = [], 0
        for ch in b.chapters:
            if self.chapter_filter and ch["n"] not in self.chapter_filter:
                continue
            if not ch["events"]:
                continue
            ids = self.screened(bi, ch)
            chapters.append({"title": ch["title"], "drawable": None if ids is None else len(ids)})
            plates += 1 if ids is None or ids or ch["recaps"] else 0
        ports = self._portrait_jobs(bi) if self.portraits else []
        if self.chapter_filter:
            end = max((c["hi"] for c in b.chapters if c["n"] in self.chapter_filter), default=-1)
            ports = [j for j in ports if b.seq_of[j.anchor] <= end]
        return {"plates": plates, "portraits": len(ports), "chapters": chapters}

    def _profiles(self, bi: int, eid: str) -> list[tuple[int, int, dict]]:
        """(book, ordinal, facet) of the entity's profiles in books 0..bi, in reading order."""
        out = []
        for j in range(bi + 1):
            b = self.books[j]
            for f in b.ents.get(eid, {}).get("facets", []):
                if f.get("kind") == "profile" and f["from"] in b.ord_of:
                    out.append((j, b.ord_of[f["from"]], f))
        return sorted(out, key=lambda t: t[:2])

    def _portrait_jobs(self, bi: int) -> list[Job]:
        """A portrait per person and creature at the first point the index describes it, and a new one (the earlier kept) when its
        look changes: a new profile whose first sentence has a look word the last one lacked, or a checked fact or turning point
        recording a wound, a death, a change of form. An entity drawn in an earlier book is not drawn again without such a change."""
        b = self.books[bi]
        named = Counter(x for e in b.doc.get("events", []) if e.get("kind") != "minor" for x in e.get("entities", []))
        jobs = []
        for e in b.doc["entities"]:
            eid = e["id"]
            if e["type"] not in PORTRAIT_TYPES:
                continue
            profs = self._profiles(bi, eid)
            mine = [p for p in profs if p[0] == bi]
            written = [p for p in mine if ".portrait-" in p[2]["id"]]
            if not profs or not (written or named[eid] >= 2):
                continue
            # only a drawn picture counts: one planned in an earlier book and then skipped does not
            before = any(r["kind"] == "portrait" and r["target"] == eid for j in range(bi) for r in self.man[j]["pictures"])
            looks = lambda t: {w.lower() for w in LOOK_PROFILE.findall(t)}     # noqa: E731
            if before:
                start, seen = None, looks(next((p[2]["text"] for p in reversed(profs) if p[0] < bi), ""))
            elif mine:
                first = (written or mine)[0][2]
                start, seen = first, looks(first["text"])
            else:
                continue
            changes: list[tuple[int, str, str, set]] = []
            for _, o, f in mine:
                if start is None or o > b.ord_of[start["from"]]:
                    changes.append((o, f["from"], P._first(f["text"]), looks(P._first(f["text"]))))
            standing = [(f["from"], f["text"]) for f in e["facets"] if f.get("kind") not in ("profile", "arc")]
            standing += [(ev["from"], ev["summary"]) for ev in b.doc.get("events", []) if ev.get("kind") == "turn" and eid in ev.get("entities", [])]
            changes += [(b.ord_of[a], a, t, None) for a, t in standing
                        if a in b.ord_of and P.changes_standing(t) and LOOK_STANDING.search(P.HEDGED.sub(" ", t))]
            # versions are compared on the anchors they will show from (after the hold to the world note), so two never share one
            versions: list[list] = [[start["from"], "", self._anchor(bi, start["from"])]] if start else []
            for o, a, why, words in sorted(changes, key=lambda c: c[0]):
                if words is not None:
                    gained = words - seen
                    if not gained:
                        continue
                held = self._anchor(bi, a)
                if versions and versions[-1][2] == held:
                    versions[-1] = [a, why if (len(versions) > 1 or before) else "", held]   # the later one stands for both
                elif len(versions) >= MAX_VERSIONS or (versions and b.seq_of[held] - b.seq_of[versions[-1][2]] < MIN_GAP):
                    continue
                else:
                    versions.append([a, why, held])
                if words is not None:
                    seen |= gained          # a look word counts once
            jobs += [Job("portrait", eid, bi, a, change=why, shape="portrait") for a, why, _ in versions]
        return jobs

    # ---- notes, pictures and references (a later book reads the earlier books' manifests)

    def _pos(self, bi: int, anchor: str) -> tuple[int, int]:
        return bi, self.books[bi].ord_of[anchor]

    def latest_note(self, eid: str, pos: tuple[int, int]) -> tuple[int, dict] | None:
        """(book, note) of the entity's latest appearance note at or before `pos`."""
        best = None
        for j in range(pos[0] + 1):
            for n in self.man[j]["appearance"].get(eid, []):
                k = (j, self.books[j].ord_of.get(n["from"], -1))
                if k[1] >= 0 and k <= pos and (best is None or k >= best[0]):
                    best = (k, j, n)
        return (best[1], best[2]) if best else None

    def latest_picture(self, eid: str, pos: tuple[int, int], kinds=ENTITY_KINDS, strict: bool = False) -> tuple[int, dict] | None:
        """(book, record) of the entity's latest picture at or (strict) before `pos`."""
        best = None
        for j in range(pos[0] + 1):
            for r in self.man[j]["pictures"]:
                k = (j, self.books[j].ord_of.get(r["from"], -1))
                if r["kind"] in kinds and r["target"] == eid and 0 <= k[1] and (k < pos if strict else k <= pos) \
                        and (best is None or k >= best[0]):
                    best = (k, j, r)
        return (best[1], best[2]) if best else None

    def ensure_note(self, bi: int, eid: str, anchor: str, change: str = "") -> tuple[str | None, str, list[str]]:
        """The entity's appearance note at `anchor`: the latest earlier one; amended ("the same man, aged...") when `change` says its
        look changed there; written now when it has none. Returns (note, reason when None, anchors of this book it was written
        from), and stores a new note in the manifest."""
        b = self.books[bi]
        pos = self._pos(bi, anchor)
        with self.lock:
            lock = self._entity_locks.setdefault(eid, threading.Lock())
        with lock:
            hit = self.latest_note(eid, pos)
            cur = hit[1] if hit else None
            if cur and (not change or (hit[0] == bi and cur["from"] == anchor)):
                if not cur["text"]:          # the text shows no look: remembered, not asked again until a look change
                    return None, "empty", []
                return cur["text"], "", (cur.get("sources", []) if hit[0] == bi else [])
            if cur and not cur["text"]:
                cur = None                   # nothing to amend: the note is written afresh
            view = self.view(bi, anchor)
            ve = next((x for x in view["entities"] if x["id"] == eid), None)
            e = b.ents.get(eid) or next((bb.ents[eid] for bb in self.books if eid in bb.ents), None)
            if ve is None or e is None:
                return None, "not visible", []
            passages, used = self.passage(bi, self._mentions(bi, eid, b.seq_of[anchor]), b.seq_of[anchor], 450)
            shown = ([ve["profile"]] if ve["profile"] else []) + ve["facets"][:8]
            # only this book's own facets carry anchors of this book (an earlier book's are finished content, and its anchor
            # ids may repeat here)
            own = {f["id"]: f["from"] for f in b.ents.get(eid, {}).get("facets", []) if f["from"] in b.ord_of}
            srcs = {b.anchor_at(s) for s in used} | {own[f["id"]] for f in shown if f["id"] in own}
            kind = P.KINDS.get(e["type"], e["type"])
            amend = AMEND.format(prev=cur["text"], change=change, who=kind.split(" ", 1)[-1]) if cur and change else ""
            prompt = APPEAR.format(who=ve["name"], kind=kind, looks=LOOKS.get(e["type"], "look"), more="", prev=amend,
                                   notes=" / ".join(f["text"] for f in shown) or "(nothing yet)",
                                   passages=passages or "(no passage names it)")
            got, why = self._write(prompt, bi, anchor, "\n".join([passages] + [f["text"] for f in shown] + ([cur["text"]] if cur else [])),
                                   NOTE_WORDS)
            sources = sorted(srcs, key=b.ord_of.get)
            if got is None and why != "empty":
                return None, why, []
            with self.lock:
                self.man[bi]["appearance"].setdefault(eid, []).append({"from": anchor, "text": got or "", "sources": sources})
                self._save(bi)
            return (got, "", sources) if got else (None, "empty", [])

    def _references(self, ids: list[str], pos: tuple[int, int], view: dict) -> list[tuple[int, dict]]:
        """The current pictures of the entities a scene names and no others, most tied first, at most MAX_REFERENCES."""
        deg = Counter()
        for r in view["relations"]:
            deg[r["src"]] += 1
            deg[r["dst"]] += 1
        found = [(eid, hit) for eid in dict.fromkeys(ids) if (hit := self.latest_picture(eid, pos))]
        order = {eid: n for n, (eid, _) in enumerate(found)}
        found.sort(key=lambda t: (-deg[t[0]], order[t[0]]))
        return [h for _, h in found[:MAX_REFERENCES]]

    # ---- one picture: its job, its prompt, its drawing (also the entry points of on-demand drawing)

    def job(self, kind: str, target: str, bi: int = -1, at: str | None = None, shape: str | None = None) -> Job:
        """The job for a picture of any kind: an event id, a chapter title, or an entity id (the anchor is the latest facet or
        profile visible at `at`). A target after `at` raises NotVisible."""
        bi = len(self.books) - 1 if bi < 0 else bi
        at_bi = bi
        if kind not in SHAPE_OF:
            raise ValueError(f"no such kind of picture: {kind}")
        b = self.books[bi]
        shape = shape or SHAPE_OF[kind]
        if kind == "event":
            if target not in b.events:
                raise ValueError("no such event")
            ev = b.events[target]
            job = Job("event", target, bi, ev["from"], event=ev, chapter=b.chapter_of(ev["from"]), shape=shape)
        elif kind == "plate":
            ch = next((c for c in b.chapters if c["title"] == target), None)
            if ch is None:
                raise ValueError("no such chapter")
            moments = self.drawable(bi, ch)      # cached; screened now when the set has no fresh screening (TextFailed if it fails)
            main = self._main_moment(bi, ch, moments)
            job = Job("plate", target, bi, ch["last"], event=b.events[main] if main else None, chapter=ch, shape=shape)
        else:
            if target not in b.ents:       # an entity of an earlier book only: its picture is that (finished) book's
                bi = next((j for j in range(bi - 1, -1, -1) if target in self.books[j].ents), -1)
                if bi < 0:
                    raise ValueError("no such entry")
                b = self.books[bi]
            e = b.ents[target]
            anchor = e["from"] if e["from"] in b.ord_of else b.anchors[0]["id"]
            for f in e.get("facets", []):
                if f["from"] in b.ord_of and b.ord_of[f["from"]] >= b.ord_of[anchor] and (
                        at is None or bi < at_bi or b.ord_of[f["from"]] <= b.ord_of[at]):
                    anchor = f["from"]
            job = Job(kind, target, bi, anchor, shape=shape)
        if at is not None and bi == at_bi and b.ord_of[self._anchor(bi, job.anchor)] > b.ord_of[at]:
            raise NotVisible(target)
        return job

    def _figures(self, job: Job) -> list[str]:
        """The entities a picture shows: the ones its event names (a plate with no event, the chapter's most named), no others."""
        if job.kind in ENTITY_KINDS:
            return [job.target]
        if job.event:
            return list(dict.fromkeys(job.event.get("entities", [])))
        ids = [x for e in (job.chapter or {}).get("events", []) if e.get("kind") != "minor" for x in e.get("entities", [])]
        return [x for x, _ in Counter(ids).most_common(4)]

    def make_prompt(self, job: Job) -> Prompt:
        """Write the prompt for one picture from what is visible at its anchor: the passage the event cites, the index content
        (`gating.view`), the world note and the figures' appearance notes (appended verbatim by code). Raises
        ValueError("dropped: ...") with the reason when no usable prompt comes out."""
        bi, b = job.bi, self.books[job.bi]
        anchor = self._anchor(bi, job.anchor)
        upto, pos = b.seq_of[anchor], self._pos(bi, anchor)
        view = self.view(bi, anchor)
        vis = {x["id"]: x for x in view["entities"]}
        world, wfrom = self._world_text(bi, pos)
        sources = {job.anchor} | ({wfrom} if wfrom else set())
        if job.event:                      # the moment shown: its text, setting and figures reach the prompt
            sources |= {job.event["from"]} | {a for a in job.event.get("evidence", []) if a in b.ord_of}
        ids = [x for x in self._figures(job) if x in vis]
        figures: list[tuple[str, str]] = []
        for eid in ids:
            e = b.ents.get(eid) or next((bb.ents[eid] for bb in self.books if eid in bb.ents), None)
            if e is None or e["type"] not in NOTE_TYPES or len(figures) >= 6:
                continue
            note, why, srcs = self.ensure_note(bi, eid, anchor, job.change if job.kind == "portrait" and eid == job.target else "")
            if note:
                figures.append((eid, note))
                sources |= set(srcs)
            elif job.kind in ENTITY_KINDS and eid == job.target:
                raise ValueError("skipped: no look described" if why == "empty" else _reason(why))
        if job.kind in ENTITY_KINDS:
            e = b.ents.get(job.target) or {}
            seqs = self._mentions(bi, job.target, upto, 8) if job.kind == "portrait" else \
                [b.seq_of[a] for f in e.get("facets", []) for a in f["evidence"] if a in b.seq_of and b.seq_of[a] <= upto]
            who = vis[job.target]["name"] if job.target in vis else job.target
        else:
            cited = job.event["evidence"] if job.event else [a for e2 in (job.chapter or {}).get("events", [])[:6] for a in e2["evidence"]]
            seqs = [b.seq_of[a] for a in cited if a in b.seq_of and b.seq_of[a] <= upto]
            who = ""
            if not job.event and job.chapter:   # a plate with no event: its figures and passage come from the chapter's events
                sources |= {e2["from"] for e2 in job.chapter["events"] if e2.get("kind") != "minor"}
                sources |= {e2["from"] for e2 in job.chapter["events"][:6]}
        passage, used = self.passage(bi, seqs, upto)
        sources |= {b.anchor_at(s) for s in used}
        ctx: list[str] = []
        if job.kind in ("plate", "event") and job.chapter:
            # this chapter's own recaps and events up to the anchor, by its own book's ordinals: ids can repeat across books
            # (r1, e3), so "the view has an item with this id" could be an earlier book's
            at = b.ord_of[anchor]
            if job.event:
                ctx.append(f"The moment: {job.event['summary']}" + (f" (at {job.event['setting']['text']})" if job.event.get("setting") else ""))
            recaps = [r for r in job.chapter["recaps"] if b.ord_of[r["from"]] <= at]
            if recaps:
                ctx.append("The chapter so far: " + " ".join(r["text"] for r in recaps))
                sources |= {r["from"] for r in recaps}
            before = [e2 for e2 in job.chapter["events"] if b.ord_of[e2["from"]] <= at and e2["id"] != (job.event or {}).get("id")
                      and (not job.event or b.ord_of[e2["from"]] < b.ord_of[job.event["from"]])][-5:]
            if before:
                ctx.append("Earlier in the chapter: " + " / ".join(e2["summary"] for e2 in before))
                sources |= {e2["from"] for e2 in before}
        for eid in ids[:6]:
            v = vis[eid]
            line = v["name"] + (f" (also {', '.join(v['aliases'][:2])})" if v["aliases"] else "") + (": " + v["profile"]["text"] if v["profile"] else "")
            ctx.append(line)
            own = {f["id"]: f["from"] for f in b.ents.get(eid, {}).get("facets", []) if f["from"] in b.ord_of}
            sources |= {own[f["id"]] for f in ([v["profile"]] if v["profile"] else []) + v["facets"][:4] if f["id"] in own}
        what = KINDS_WHAT[job.kind].format(frame=(job.event or {}).get("summary", ""), who=who)
        if job.kind == "plate" and not job.event:
            what = "the main moment of a chapter, as its recap tells it."
        fig = "\n".join(f"- {vis[eid]['name']}: {note}" for eid, note in figures)
        extra = "- This is a portrait of the figure listed above: say only pose, expression and background.\n" if job.kind == "portrait" else ""
        writer = WRITE.format(what=what, world=world or "(none)", figures=f"\nFigures (fixed notes):\n{fig}\n" if fig else "",
                              context="\n".join(ctx) or "(none)", passage=passage or "(no passage)", extra=extra)
        check = "\n".join([passage, world] + [n for _, n in figures] + ctx)
        scene, why, out = self._write_full(writer, bi, anchor, check, SCENE_WORDS, SCENE_SCHEMA)
        if scene is None:
            raise ValueError(_reason(why))
        if job.kind not in ENTITY_KINDS:   # an entity's own picture always shows it
            figures, ids = self._shown(out, figures, ids, vis)
        if any(a in b.ord_of and b.ord_of[a] > b.ord_of[anchor] for a in sources):
            raise ValueError("dropped: written from content after its anchor")
        refs: list[tuple[int, dict]] = []
        if job.kind in ("plate", "event"):
            refs = self._references(ids, pos, view)
        elif job.kind == "portrait" and job.change:
            prev = self.latest_picture(job.target, pos, ("portrait",), strict=True)
            refs = [prev] if prev else []
        return Prompt(self._assemble(scene, [n for _, n in figures], world, bi), scene, anchor, job.shape or SHAPE_OF[job.kind], figures, refs,
                      sorted((a for a in sources if a in b.ord_of), key=b.ord_of.get), writer, check)

    @staticmethod
    def _shown(out: dict, figures: list[tuple[str, str]], ids: list[str], vis: dict) -> tuple[list, list]:
        """Keep only the figures the writer says are in its picture (matched by their visible names): none for an empty list, all
        of them when the reply has no list or a list that matches no one (a name it mistyped)."""
        shows = out.get("shows")
        if not isinstance(shows, list):
            return figures, ids
        if not shows:
            return [], []
        named = {str(x).strip().lower() for x in shows}
        keep = {eid for eid in ids if eid in vis and ({vis[eid]["name"].lower()} | {a.lower() for a in vis[eid]["aliases"]}) & named}
        if not keep:
            return figures, ids
        return [(eid, n) for eid, n in figures if eid in keep], [eid for eid in ids if eid in keep]

    def _assemble(self, scene: str, notes: list[str], world: str, bi: int = -1) -> str:
        """What the image model gets: the scene, then the notes, the world note and the style, all added by code."""
        parts = [scene.rstrip()]
        if notes:
            parts.append("Figures: " + " ".join(n.rstrip(".") + "." for n in notes))
        if world:
            parts.append("World: " + world.rstrip())
        return " ".join(parts + [style_sentence(self.styles[bi]), NO_TEXT])

    # ---- cost and the cap

    def _skip(self, bi: int, target: str, reason: str, final: bool = False, kind: str = "", anchor: str = "",
              detail: str = "", refused: bool = False) -> None:
        """Record a skip with its reason (and the provider's or model's own words in `detail`), all under the lock. A `final`
        one (a refusal, no look described) is kept in the manifest and not tried again on resume. A refusal counts once, as
        refused, never also as a skip."""
        with self.lock:
            if refused:
                self.report.refused += 1
            else:
                self.report.skipped[reason] += 1
            self.report.reasons.append({"target": target, "reason": f"{reason} ({detail})" if detail else reason})
            if final:
                self.man[bi]["skipped"].append({"target": target, "reason": reason, "kind": kind, "from": anchor, "final": True})
                self._save(bi)

    def _spent(self) -> float:
        """What the run has spent and may have spent: pictures, calls that failed after they were sent, and text."""
        return self.report.cost + self.report.at_risk + self.text_spent()

    def _over_cap(self, more: float = 0.0) -> bool:
        return self.cap is not None and self._spent() + self.reserved + more > self.cap + 1e-9

    def _reserve(self, price: float) -> tuple[str, float]:
        """Count a draw call in flight, or say why none may start (the cost cap, the picture limit). Returns (why not, the
        amount reserved), and `_release` gives back exactly that amount."""
        with self.lock:
            amount = price or self.last_cost
            if not self.stop:
                if self.max_pictures is not None and self.report.drawn + self.in_flight >= self.max_pictures:
                    self.stop = f"picture limit {self.max_pictures} reached"
                elif self._over_cap(amount):
                    self.stop = f"cost cap ${self.cap:.2f} reached"
            if self.stop:
                return self.stop, 0.0
            self.reserved += amount
            self.in_flight += 1
            return "", amount

    def _release(self, amount: float) -> None:
        with self.lock:
            self.reserved = max(0.0, self.reserved - amount)
            self.in_flight = max(0, self.in_flight - 1)

    def _record(self, job: Job, pr: Prompt, pic: images.Drawn) -> dict:
        b = self.books[job.bi]
        fid = "p" + hashlib.sha256(pic.data).hexdigest()[:8]
        mime = pic.mime or "image/png"
        w, h = (pic.width, pic.height) if pic.width else size_of(pic.data)
        rec = {"id": fid, "kind": job.kind, "target": job.target,
               "chapter": (job.chapter or b.chapter_of(pr.anchor) or {}).get("title"), "from": pr.anchor,
               "file": f"{fid}.{_ext(mime)}", "mime": mime, "width": w, "height": h, "prompt": pr.text,
               "references": [r["id"] for _, r in pr.references], "sources": pr.sources}
        if job.kind in ENTITY_KINDS:
            rec["appearance"] = dict(pr.notes).get(job.target, "")
        rec.update({"provider": self.provider, "model": pic.model or self.model, "quality": self.quality, "shape": pr.shape,
                    "cost": round(pic.cost, 6), "created": _now()})
        return rec

    def draw(self, job: Job, prompt: Prompt | None = None, text: str | None = None, soften: bool | None = None) -> dict | None:
        """Draw one picture and file it; `text` replaces the written prompt (a person edited it). None when skipped, with the reason
        in the report. A refusal gets one quieter rewrite of the scene (`soften`, on unless `text` is given: a person's own prompt
        is never rewritten, and its refusal is raised as `images.ImageRefused`) and then a recorded skip. A provider error is sent
        again once only when `images` says the request never reached the provider (`ProviderError.sent is False`): a timeout, a
        reset or a 5xx after sending may have drawn and billed the picture, so its price counts against the cap and the picture
        is skipped. The cost cap is checked before every call."""
        bi = job.bi
        soften = text is None if soften is None else soften
        if self._budget() == 0:            # the limit or the cap leaves no room: no prompt is written for it
            why, _ = self._reserve(llm.image_price(self.provider, self.model, self.quality, job.shape or SHAPE_OF[job.kind]) or 0.0)
            if why:
                with self.lock:
                    self.report.skipped[f"stopped: {why}"] += 1
                return None
            self._release(_)
        try:
            pr = prompt or self.make_prompt(job)
        except ValueError as e:
            self._skip(bi, job.target, str(e), final=str(e) == "skipped: no look described", kind=job.kind, anchor=job.anchor,
                       detail=getattr(e, "detail", ""))
            self.log(f"    skipped {job.kind} {job.target}: {e}")
            return None
        if text:
            pr.text = text
        price = llm.image_price(self.provider, self.model, self.quality, pr.shape) or 0.0
        softened = retried = False
        while True:
            refs = [(d, r["mime"]) for j, r in pr.references if (d := self.books[j].b.sink.read(r["file"]))][:images.MAX_REFERENCES]
            why, amount = self._reserve(price)
            if why:
                with self.lock:
                    self.report.skipped[f"stopped: {why}"] += 1
                return None
            try:
                pic = self.image.draw(pr.text, shape=pr.shape, quality=self.quality, references=refs)
            except images.ImageRefused as e:
                self._release(amount)
                if not soften:
                    with self.lock:
                        self.report.refused += 1
                        self.report.reasons.append({"target": job.target, "reason": f"refused: provider declined ({e.reason[:160]})"})
                    raise
                soft = None
                if not softened:
                    softened = True
                    try:
                        soft, _ = self._write(pr.writer + SOFTEN.format(text=pr.scene), bi, pr.anchor, pr.check, SCENE_WORDS)
                    except TextFailed:
                        soft = None
                if not soft:
                    self._skip(bi, job.target, "refused: provider declined", final=True, kind=job.kind, anchor=job.anchor,
                               detail=e.reason[:160], refused=True)
                    return None
                pr.scene = soft
                pr.text = self._assemble(soft, [n for _, n in pr.notes], self._world_text(bi, self._pos(bi, pr.anchor))[0], bi)
                continue
            except llm.ProviderError as e:
                self._release(amount)
                if getattr(e, "sent", None) is False and not retried:
                    retried = True
                    time.sleep(self.pause)
                    continue
                billed = getattr(e, "sent", None) is not False and not (e.status and 400 <= e.status < 500)
                with self.lock:
                    if billed:          # it may have been drawn and billed: counted, never sent again
                        self.report.at_risk += amount
                self._skip(bi, job.target, "skipped: provider error", kind=job.kind, anchor=job.anchor, detail=str(e)[:160])
                return None
            self._release(amount)
            break
        rec = self._record(job, pr, pic)
        with self.lock:
            self.books[bi].b.sink.put(rec["file"], pic.data)
            self._dirty.add(bi)
            self.man[bi]["pictures"].append(rec)
            self.report.drawn += 1
            self.report.softened += softened
            self.report.cost += pic.cost
            self.last_cost = pic.cost or self.last_cost
            self.man[bi]["cost"] = round(sum(r["cost"] for r in self.man[bi]["pictures"]), 6)
            self._save(bi)
        self.log(f"    drew {job.kind} {job.target} ({rec['id']}, ${pic.cost:.3f})")
        return rec

    # ---- the run

    def _tick(self) -> None:
        with self.lock:
            self.done += 1
            if self.progress:
                self.progress(self.done, self.total)

    def run(self) -> Report:
        """Plan, write the notes, draw the portraits (an entity's versions one after another, entities three at a time), then the
        scenes. Resumes: a picture that is in the manifest is not drawn again. Returns the report."""
        try:
            self._run()
        finally:
            self.report.text_cost = self.text_spent()
            if self.stop:
                self.report.stopped = self.stop
            for bi in range(len(self.books)):
                self._save(bi)
        return self.report

    def _notes_pass(self, bi: int, jobs: list[Job]) -> None:
        """Every entity's note exists before any picture is drawn, written at the first anchor that needs it, so that two
        pictures of one entity never carry two notes. `jobs` in drawing order; only those the picture limit and the cost cap
        leave room for get their notes here (a picture past them writes its own, if it is ever drawn), and the pass stops
        when the text spent reaches the cap."""
        budget = self._budget()
        if budget is not None:
            jobs = jobs[:budget]
        needs: dict[str, list[tuple[int, str, str]]] = {}
        b = self.books[bi]
        for j in jobs:
            a = self._anchor(bi, j.anchor)
            view = self.view(bi, a)
            vis = {x["id"] for x in view["entities"]}
            for eid in self._figures(j):
                e = b.ents.get(eid)
                if eid in vis and e and e["type"] in NOTE_TYPES:
                    needs.setdefault(eid, []).append((b.ord_of[a], a, j.change if j.kind == "portrait" and eid == j.target else ""))

        def entity(item):
            for _, a, change in sorted(set(item[1])):
                with self.lock:
                    if not self.stop and self._over_cap():
                        self.stop = f"cost cap ${self.cap:.2f} reached"
                if self.stop:
                    return
                try:
                    self.ensure_note(bi, item[0], a, change)
                except TextFailed as e:     # the picture that needs it tries once more, then is skipped
                    self.log(f"  note for {item[0]}: text model failed ({e.detail})")
        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(entity, needs.items()))

    def _run(self) -> None:
        drawn = [bi for bi, b in enumerate(self.books) if b.b.draw and b.epub is not None]
        first: dict[int, int] = {}
        for bi in drawn:                   # a first count for the progress total
            self.world(bi)
            first[bi] = len(self.plan(bi, record=False))
        self.total = sum(first.values())
        for bi in drawn:
            # planned again now that the earlier books are drawn: only a drawn portrait spares a later book's
            jobs = self.plan(bi)
            with self.lock:
                self.total += len(jobs) - first[bi]
            self.report.held += sum(1 for j in jobs if self._anchor(bi, j.anchor) != j.anchor)
            if not jobs:
                continue
            b = self.books[bi]
            have = self._have(bi)
            todo = [j for j in jobs if self._is_new(bi, j, have)]
            self.done += len(jobs) - len(todo)
            self.log(f"{b.doc['work']['title']}: {len(todo)} pictures to draw, {len(jobs) - len(todo)} already there")
            by: dict[str, list[Job]] = {}
            named = Counter(x for e in b.doc.get("events", []) for x in e.get("entities", []))
            for j in sorted((j for j in todo if j.kind == "portrait"), key=lambda j: (-named[j.target], b.ord_of[j.anchor])):
                by.setdefault(j.target, []).append(j)
            scenes = sorted((j for j in todo if j.kind != "portrait"), key=lambda j: b.ord_of[j.anchor])
            # drawing order: the entities' portraits run side by side (first versions first), then the scenes
            rounds = max((len(c) for c in by.values()), default=0)
            self._notes_pass(bi, [c[k] for k in range(rounds) for c in by.values() if k < len(c)] + scenes)

            def one(j: Job) -> None:
                if not self.stop:
                    self.draw(j)
                    self._tick()

            def chain(js: list[Job]) -> None:
                for j in js:
                    one(j)
            with ThreadPoolExecutor(self.workers) as pool:       # portraits first: scenes take them as references
                list(pool.map(chain, by.values()))
            with ThreadPoolExecutor(self.workers) as pool:
                list(pool.map(one, scenes))
            if self.stop:
                break


def illustrate(books: list[Book], text, image, style: str, **kw) -> Report:
    """Draw the art sets of `books` (series reading order); see `Illustrator` for the options."""
    return Illustrator(books, text, image, style, **kw).run()
