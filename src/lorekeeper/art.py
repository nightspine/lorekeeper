"""Art sets: pictures for an index, drawn ahead of time and gated by anchor (CLAUDE.md §8f).

The model only proposes. A text model (the run's cheap one) writes every prompt; an image model draws it.
Python decides what is drawn, the anchor of every picture (a picture shows from that anchor on and is
written only from content visible at it), the references attached, the cost cap, and drops what fails a
check, with a reason. Pictures never go into the index document: an art set is a manifest (`art.json`)
and the picture files, one per edition and style, behind a `Sink`.

Consistency comes from notes pasted verbatim into every prompt (code appends them, the writer never re-words
them): an *era note* for the whole book (time, technology, materials; inherited down a series), an *appearance
note* per entity (filled in as the book describes more of it, amended when its look changes, never rewritten),
the look of the members of a person's groups, the *milieu* a figure belongs to (from the settings of its
events, else its groups' home) and, as a reference picture, the entity's current portrait. Which items
describe a look or change it, and which groups a person belongs to, are chosen by cheap model calls that
only choose; every detail that reaches a prompt is quoted from the entity's own text and checked by code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import threading
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
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
NOTE_TYPES = ("person", "creature", "place", "object", "group")
PORTRAIT_TYPES = ("person", "creature")

WORLD_MIN_WORDS = 1500  # the world note is written from the first story chapter, and the next ones until this many words
MAX_REFERENCES = 4     # pictures attached to a scene, most tied first
MAX_VERSIONS = 3       # portraits per entity per book (first one and look changes)
MIN_GAP = 30           # blocks between two portraits of one entity
MAIN_CAST_MIN = 3      # main cast: named by at least this many non-minor events of the book...
MAIN_CAST_SHARE = 0.05  # ...and by at least this share of the book's non-minor events (rounded up)
MAIN_CAST_MAX = 12     # main cast: at most this many characters per book, most named first
CASTS = ("main", "all")  # whose portraits a run draws: the main cast, or everyone with a part (the older rule)
MAX_GROUPS = 3         # groups whose members' look a figure's note carries: only groups with a member look that adds to it
GROUPS_TRIED = 6       # groups asked for a note before giving up on finding more that add a look
PASSAGE_WORDS = 600
WORKERS = 3
SCENE_WORDS = (35, 170)
NOTE_WORDS = (1, 200)      # an appearance note is put together by code from the role and the quoted details
MAX_DETAILS = 16           # details a note holds: a fill adds new ones only while it has room (nothing is dropped for them)
MAX_HOME = 6               # a group note's details of where its members live
INTRO_PARAS = 12           # paragraphs before an entity's first name that may describe it as it enters (within its chapter)
OWN_WORDS = 8              # a quote this short stands as the detail itself when the writer's words go beyond it
WORLD_WORDS = (15, 90)
NOTE_VERSION = 2           # a note (and a picture drawn from one) of another version is never used for a new picture
# A note is written from the paragraphs its look items cite first, then from paragraphs that name the entity, within these
# budgets. About twice the first version's (300 and 450): a main character has 10 to 25 look items in a novel, each citing a
# paragraph of 50 to 100 words, and the note is filled in as more appear, so 600 words of look paragraphs hold the latest
# 6 to 12 of them and leave 300 words of plain mentions for the role.
LOOK_PASSAGE_WORDS = 600
NOTE_PASSAGE_WORDS = 900
NOTE_ITEMS_CHARS = 3000    # of the look items' own texts sent with a note, the latest first
LOOK_BATCH_ITEMS = 40      # items classified in one call (small entities share one, a large one takes several); Flash returned no flags at all for 80
LOOK_ITEM_CHARS = 300
# Story time: a book that tells more than one time (a frame, a flashback strand, two timelines, a nested tale, a prologue years
# apart, a long jump forward) draws each figure as it looks at the time the scene tells. A *scope* is (strand, era), written
# "s2.1": a strand is a line of telling, an era a stretch of it between stated jumps of years. A book with one scope draws as
# before; an entity whose items span two or more scopes gets a note, references and portraits per scope.
STORY_TIME = True          # label the chapters' lines of telling (one cheap call per chapter); False draws as before
LIKENESS_REFERENCE = True  # a scope's first portrait takes the figure's portrait from another scope as a likeness reference
MAX_STRANDS = 4            # lines of telling per series; a chapter opening a fifth is unknown
MAX_ERAS = 4               # eras per strand; a chapter past the fourth is unknown
MAX_SCOPE_VERSIONS = 5     # portraits per entity per book over all its scopes (MAX_VERSIONS within one)
OPENING_WORDS = 200        # of a chapter's opening sent to the timeline call (the time is often stated a few lines in)
EVIDENCE_WORDS = 25        # of the paragraph each event cites
OTHER = "other"            # an event told in no known line (a dream, a vision, a brief memory): its figures get passage only
PASSAGE = "passage"        # a figure drawn from the passage only: no fixed note, no reference
LIKENESS = ("The reference shows the same figure at another time of life: keep the likeness; age, marks and dress follow the "
            "note.")

_STOP = frozenset("a an the of with and or in on at to by for from his her its their is are has have who which that as "
                  "very rather quite somewhat".split())

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
are set, and a few passages. Write the era note (the visual world note) an illustrator needs to keep every picture of this book
in one time: the era, the level of technology, and the general materials things are made of, as these pages show them.

Only what is true of every place and people of the book: no landscape, architecture, weather, light or clothing of any one place
or people, since the pictures will show many places. 20 to 60 words, plain descriptive sentences, no names of any person,
creature or place, nothing known from elsewhere (you may recognise the book: use nothing you know from outside these pages),
no plot.

Recaps and settings:
{recaps}

Passages:
{passages}"""

WORLD_MORE = """The era note of this series so far is:
"{base}"
This is the opening of a later book. If its chapters show the era, the technology or the materials of things have changed, write
ONE sentence adding what an illustrator now needs, as the pages show it, with no names and nothing about one place or people. If
nothing changed, return an empty text.
"""

LOOK_INDEX = """Below are notes a reader's companion keeps on some entries of a novel, numbered. For each note, decide:
- "look": true when the note tells how the entry looks to the eye: its body, face, colouring, hair, size, age as it shows, what
  it wears or carries on it, the marks on it; for a group, how its members look and dress; for a place or a thing, its form and
  what it is made of. False for what it does, thinks, says or owns, its rank, history or character.
- "change": when the note records a lasting change in how THIS entry looks (a scar or wound it keeps, a lost eye or limb, a body
  remade, a new form, a disguise, ageing, a new way of dressing it keeps), a short phrase of what changed, in the note's words.
  Otherwise "". The notes of one entry are in reading order: a change is one the entry's earlier notes above do not show yet
  (a note that restates a look told before is a look, not a change). What the entry does to something else ("drags a dead deer",
  "burns a letter") is no change of its own look, and a death alone is no change of look.
A note marked "(a turning point it is in)" may be about others too: flag it only for what it says of this entry.
Return "items": one object {{"n", "look", "change"}} for each note that is a look or a change; leave the others out.

{entries}"""
LOOK_INDEX_SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}, "look": {"type": "boolean"},
                                                        "change": {"type": "string"}}}}}}

GROUP_PICK = """These are a reader's companion's notes on {who}, numbered, and some groups of the novel (peoples, castes,
houses, orders, crews, families), lettered. Which of the groups is {who} a member of, as the notes show it (not a group it only
meets, fights, serves or talks about)?
Return "groups": one object for each group {who} belongs to: "group" (its letter), "from" (the number of the first note that shows
{who} as a member) and "until" (the number of the note where {who} stops being one or stops looking like one, such as remade as
another people or cast out; 0 when it never does).

Notes on {who}:
{items}

Groups:
{groups}"""
GROUP_SCHEMA = {"type": "object", "required": ["groups"], "properties": {"groups": {"type": "array", "items": {
    "type": "object", "required": ["group"], "properties": {"group": {"type": "string"}, "from": {"type": "integer"},
                                                            "until": {"type": "integer"}}}}}}

APPEAR = """Write a fixed appearance note for {who} ({kind}) so an illustrator draws it the same way every time.
Return "role": who or what it is in 2 to 8 words, in the words the passages or notes use for it: occupation, rank, people,
allegiance ("a miller of the valley", "a stone bridge"); no look in the role (no size, colour, age or dress: those are details),
and in "role_look" any word of your role that still says how it looks (an empty list when none does).
Return "details": {looks}, as the passages and notes show it, each a short phrase ("text") with "quote", the exact words of the
passages or notes that show it about {who} (copied word for word, 2 to 25 words), and "seen": true when it is something an
illustrator can draw and that lasts (a body, a face, a colour, a garment, a mark, gear it keeps), false when it is character,
mood, situation or a passing state ("curious", "a prisoner", "bleeding", "in tears", "holding a cup"). At most 12 details, the
ones an illustrator most needs, each a short phrase, not a sentence. Use no name. A detail you cannot quote about {who} is not shown: leave it out, never guess it and never
take it from another figure. Give each detail once, never again in other words. If nothing of its look is shown (a name only mentioned, an author, a person only written about),
return an EMPTY details list.{home}{more}
{prev}
What the companion has noted:
{notes}

Passages:
{passages}"""
HOME = """
Return also "home": the places its members live in and how those places look, as the passages and notes show it (their
dwellings, the kind of place, how rich or poor it is; not what they do or rule), at most 6, each a short phrase with its "quote"
and "seen", the same way; an empty list when the text does not show it."""
_DETAILS = {"type": "array", "items": {"type": "object", "required": ["text", "quote", "seen"],
                                       "properties": {"text": {"type": "string"}, "quote": {"type": "string"},
                                                      "seen": {"type": "boolean"}}}}
NOTE_SCHEMA = {"type": "object", "required": ["role", "role_look", "details"],
               "properties": {"role": {"type": "string"}, "role_look": {"type": "array", "items": {"type": "string"}},
                              "details": _DETAILS, "home": _DETAILS,
                              "drop": {"type": "array", "items": {"type": "integer"}}}}
LOOKS = {"person": "its look (body, face, hair, colouring, dress, marks, what it wears or carries)",
         "creature": "its look (form, size, colouring, marks)",
         "place": "its look (form, materials, state, surroundings)", "object": "its look (form, materials, size, state)",
         "group": "what its members share in how they look (colouring, dress, marks, insignia; never one member's own look)"}
FILL = """Its fixed note until now was:
"{prev}"
The companion has since noted more about how it looks (the first notes below). Return in "details" only what the note does not
have yet, quoted the same way; an empty list when the note already says it all. Do not repeat the note's details."""
AMEND = """Its fixed note until now was, detail by detail:
{prev}
The text now shows a change: "{change}". Return in "details" what changed, quoted from the change or the passages, and in "drop"
the numbers of the earlier details the change makes untrue (hair that is now another colour); the other details still hold and
are kept. Do not use the word "now"."""
NOTE_AGAIN = """\n\nIn your first answer these details had no quote of the passages or notes that shows them about {who}: {bad}.
Quote each detail word for word from the passages or notes, or leave it out."""

INTRO = """A novel often describes someone as they enter, before it gives their name. Below are the paragraphs just before
{who} is first named, numbered, then the paragraph where the name first appears. Which numbered paragraphs describe how {who}
looks (not anyone else, and not only what {who} says or does)? Return "paragraphs": their numbers; an empty list when none does.

{paras}

Where the name first appears:
{first}"""
INTRO_SCHEMA = {"type": "object", "required": ["paragraphs"], "properties": {"paragraphs": {"type": "array", "items": {"type": "integer"}}}}

LOOK_CHECK = """A picture prompt was written from the notes and passage below. List every phrase of the prompt that says how a
figure (a person or a creature) looks, such as its body, face, hair, colouring, age, dress, marks or gear, and that the notes and
passage do not state for that figure. Light, places, things, poses, gestures and expressions are not a figure's look. Copy each
phrase exactly as it is written in the prompt, as short as it can be (the words of the look only). Return "phrases": an empty list
when there is none.

Notes and passage:
{source}

Prompt:
{text}"""
LOOK_CHECK_SCHEMA = {"type": "object", "required": ["phrases"], "properties": {"phrases": {"type": "array", "items": {"type": "string"}}}}

WRITE = """Write the picture prompt for an illustrator. The picture shows {what}

Era (the time, technology and materials true of every picture in this book; the code adds it, do not repeat it): {world}
{figures}{place}
Index content, for context only:
{context}

Passage:
{passage}

Rules:
- One scene, one moment: what is in frame, the composition and the light. 60 to 120 words, plain sentences, present tense.
- Describe people by role, never by name ("the miller", "the rider"); name no person, creature or group. A look you give a
  figure must come from its fixed note or the passage, never from another figure's.
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
    "portrait": "a portrait of the figure listed below, as they are at this point: head and shoulders or half length, {background}",
    "place": "{who}, a place, as it looks at this point; people only if the passages put them there.",
    "thing": "{who}, an object, as it looks at this point, in a plain setting or the place the passages put it.",
}
BACKGROUND = "in the surroundings they belong to, given below as their place (the program adds it after your text)."
PLAIN = "against a plain, neutral background with no place in it."
TIMELINE = """A reader's companion draws the people of a novel as they look at the time each scene shows. Below is one chapter
of the novel, read in order: say which line of telling it belongs to.

A line of telling is one TIME the story is set in, kept up over chapters. Most books have one. Some have several: the time of a
frame (someone telling, writing or remembering the story, with whoever listens) and the time of the tale; or two or more times,
years apart, that the book moves between (the same people as children in some chapters and as adults in others).
- Decide by WHEN the chapter's events happen, never by who tells them or how. The frame's line holds only the scenes at the time
  of the telling (the teller and the listener themselves); what the tale shows happens at the tale's time, even when it is told
  aloud, in quotation marks or in the first person. A chapter that opens in the frame and then tells the past at length is in
  the line of that past; its frame scenes are exceptions (see "events").
- Times years apart that the book moves between are separate lines, even inside one tale and one frame: a teller's boyhood
  and his life as a grown man are two lines. A chapter set years before or after the line it follows goes to the known line
  set at that time, or opens a new line.
- Not a new line: the story moving on by hours, days or months; another narrator, viewpoint, character or place at the same
  time or within months; a marriage, an illness or any other change in people's lives; letters, diaries and reports written in
  the story's own time; a dream, a vision or a memory of a few lines inside a scene (mark its
  events "other").
- A chapter goes on in the line of the chapter before when its opening shows no other time. At a new part or section, decide
  afresh from the opening: its time (an age, a date, years passed), its people and their situation.

Lines known so far (its era is 1 at first and one more after each stated jump of years):
{catalog}
{previous}
Return, in this order:
- "when": in a few words, when this chapter's events happen as the text states or shows it: the age or stage of life of its
  main people, a year, or how far before or after the line before it.
- "gap": how many years lie between this chapter's events and the previous chapter's events, before or after (0 when it is
  less than a year).
- "lines": for EVERY known line, {{"id": its id, "same_time": true when this chapter's events happen in that line's years
  (where it left off, or soon after), whoever the chapter follows; false when they are set years before or after it, or in the
  frame when the line is the tale (or the reverse)}}.
- "events": for EVERY numbered event, {{"n": its number, "line": the id of the known line it happens in, "new" for a line this
  chapter opens, or "other" only for a dream, a vision or a memory of a few lines inside a scene}}.
- "strand": the line most of the chapter's events happen in: a known line you marked "same_time", or "new" when none is, with "label": the time of that line in a few
  plain words, never the telling: who is seen then, at what age or stage of life, and their situation (such as "the two sisters
  as children on the farm" or "the sailor, old, at the inn where he tells his tale").
- "cue": {{"paragraph": the number of the paragraph where the chapter's own line first shows, "quote": 3 to 12 words copied
  exactly from that paragraph that show it}}. For a chapter that simply goes on from the previous one, paragraph 1.
- "skip": true only when the chapter goes on in its line but itself states a jump forward long enough that looks change (years
  pass: children have grown up, adults have aged), with "skip_quote": the words of the cue paragraph that state the jump. False
  for days, weeks or months, and for a new line.

Chapter: {path}

Its opening, numbered by paragraph:
{opening}

Its events, each with the start of the paragraph it is told in:
{events}"""
SHOWS_AT = """A novel tells more than one time. These are its lines of telling, each set at one time:
{catalog}

Below are paragraphs that mention {who}, numbered, each marked with the line its chapter is told in. For each paragraph, at which
line's time does it show {who}? The marked line; another line's id when the paragraph shows {who} at that other time (a scene of
the frame, or a description of the teller as he or she is then, inside a chapter of the tale, or the reverse); or "none" when it
shows {who} at no one time (a look back across the years, a summary of a life).
Return "paragraphs": one {{"n": its number, "line": a line id or "none"}} for every paragraph.

{paras}"""
SHOWS_SCHEMA = {"type": "object", "required": ["paragraphs"], "properties": {"paragraphs": {"type": "array", "items": {
    "type": "object", "required": ["n", "line"], "properties": {"n": {"type": "integer"}, "line": {"type": "string"}}}}}}
SHOWS_BATCH = 30           # paragraphs per call of the paragraph check
SHOWS_WORDS = 90           # of each paragraph sent to it
# The timeline call only chooses: its labels are internal, and what it says never reaches a picture's prompt.
TIMELINE_SCHEMA = {"type": "object", "required": ["when", "gap", "lines", "events", "strand", "cue"], "properties": {
    "when": {"type": "string"}, "gap": {"type": "number"},
    "lines": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"},
                                                                          "same_time": {"type": "boolean"}}}},
    "events": {"type": "array", "items": {"type": "object", "properties": {"n": {"type": "integer"},
                                                                           "line": {"type": "string"}}}},
    "strand": {"type": "string"}, "label": {"type": "string"},
    "cue": {"type": "object", "properties": {"paragraph": {"type": "integer"}, "quote": {"type": "string"}}},
    "skip": {"type": "boolean"}, "skip_quote": {"type": "string"}}}
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
            "appearance": {}, "screened": {}, "looks": {}, "groups": {}, "intro": {}, "skipped": [], "cost": 0.0}


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


# Inflection endings, longest first: (ending, what it replaces). One word matches another only through these (`_same`).
_ENDINGS = (("iest", "y"), ("ier", "y"), ("ies", "y"), ("ied", "y"), ("ing", ""), ("est", ""), ("ed", ""), ("er", ""),
            ("es", ""), ("ly", ""), ("en", ""), ("s", ""), ("d", ""), ("y", ""), ("n", ""))


@lru_cache(maxsize=65536)
def _forms(w: str) -> frozenset[str]:
    """A word and the words it is an inflection of: the word less one ending, that stem with a final "e" put back, and with a
    doubled consonant made single ("scarred": scar; "faced": face; "haired": hair; "paler": pale; "freckles": freckle). A stem
    keeps 3 letters at least ("eyes": eye), 4 before "y" or "ly" ("many" is no form of "man"); "d" follows only an "e"
    ("faced", never "beard" for "bear") and "n" only an "e" or a "w" ("grown")."""
    out = {w}
    for end, rep in _ENDINGS:
        if not w.endswith(end) or (end == "s" and w.endswith("ss")):
            continue
        stem = w[:-len(end)]
        if len(stem) < (4 if end in ("y", "ly") else 3) or (end == "d" and not stem.endswith("e")) \
                or (end == "n" and not stem.endswith(("e", "w"))):
            continue
        stem += rep
        out |= {stem, stem + "e"} if not rep else {stem}
        if len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] not in "aeiouls":
            out.add(stem[:-1])
    return frozenset(out)


def _same(a: str, b: str) -> bool:
    """Whether two words are one word with its endings: equal, or one an inflection of the other, or both of one stem ("face"
    and "faced", "scar" and "scarred", "hair" and "haired", "freckled" and "freckles"); never "cap" and "cape", "man" and
    "mane", "robe" and "rob", "scar" and "scarlet"."""
    return a == b or b in _forms(a) or a in _forms(b) or bool((_forms(a) - {a}) & (_forms(b) - {b}))


def _has(words, w: str) -> bool:
    """Whether a word is among `words`, endings allowed (`_same`)."""
    return w in words or any(_same(w, x) for x in words)


def _within(small, big) -> bool:
    return all(_has(big, w) for w in small)


def _drop_phrase(text: str, phrase: str) -> str:
    """The text without `phrase` (every use of it, matched whole and ignoring case), with the commas, "and" and article it
    leaves wrong tidied ("a young, thin man" without "young" is "a thin man"; "a old man" becomes "an old man")."""
    words = phrase.split()
    if not words:
        return text
    rx = r"(?<![\w-])" + r"[\s,]+".join(re.escape(w.strip(",")) for w in words) + r"(?![\w-])"
    text = re.sub(rx, "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(a|an|the)\s*(?:,|\band\b|\bor\b)\s*", r"\1 ", text, flags=re.IGNORECASE)   # "the , thin" / "the and thin"
    text = re.sub(r",\s*(?=,|\band\b)", " ", text)                                             # "tall, , thin" / "tall, and"
    text = re.sub(r"\b(and|or|with)\s+(?=(?:and|or)\b|[.,;])", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([,;])\s*(?=[.;])", "", text)
    text = re.sub(r"\b([Aa])n?\s+(?=(\w))", lambda m: m.group(1) + ("n " if m.group(2).lower() in "aeiou" else " "), text)
    return " ".join(text.split())


def _toks(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("’", "'").replace("'", ""))


def _sentences(text: str) -> list[list[str]]:
    return [t for t in (_toks(x) for x in re.split(r"(?<=[.!?;])\s+|\n+", text)) if t]


def quoted(quote: str, source: str, one: bool = False) -> bool:
    """Whether `quote` (2 words or more) is a phrase of `source`, word for word, ignoring case and punctuation. A quote with an
    ellipsis counts only when its pieces come in order within one sentence of the source ("dark ... hair" across two
    sentences does not). `one`: a single word counts too when it is no function word ("tall" of "He was tall, thin, and very
    old"), for a look detail quoted from the entity's own paragraphs."""
    parts = [_toks(p) for p in re.split(r"\.\.\.|…", quote)]
    parts = [p for p in parts if p]
    if one and len(parts) == 1 and len(parts[0]) == 1 and parts[0][0] not in _STOP and len(parts[0][0]) > 2:
        return " " + parts[0][0] + " " in " " + " ".join(_toks(source)) + " "
    if not parts or sum(map(len, parts)) < 2:
        return False
    if len(parts) == 1:
        return " " + " ".join(parts[0]) + " " in " " + " ".join(_toks(source)) + " "
    for sent in _sentences(source):
        line, at = " " + " ".join(sent) + " ", 0
        for p in parts:
            k = line.find(" " + " ".join(p) + " ", at)
            if k < 0:
                break
            at = k + len(" ".join(p)) + 1
        else:
            return True
    return False


def detail_supported(detail: str, quote: str) -> bool:
    """Whether every content word of a look detail is in its own quote ("long white moustache" in "a long white moustache";
    "dark hair" not in "her hair"; "pale skin" not in "the pale light"), words matched whole with their endings (`_base`)."""
    have = set(_toks(quote))
    words = [w for w in _toks(detail) if w not in _STOP and len(w) > 2]
    return bool(words) and _within(words, have)


# What a character says: a stretch inside quotation marks, double or single, straight or curly. A single mark counts as a
# quotation mark only where no letter touches it from outside, so the apostrophes of "don't" and "Mira's" never open one.
_SPOKEN = re.compile(r"“[^”]*”|\"[^\"\n]*\"|(?<![^\W_])‘.*?’(?![^\W_])|(?<![^\W_])'.*?'(?![^\W_])")


def spoken(quote: str, source: str) -> bool:
    """Whether a quote is only ever a line a character says in `source` (every place it occurs is inside quotation marks),
    not the narrator's description: "“I'll stay big and beefy,” he said" is spoken; the same words in narration are not."""
    return quoted(quote, source, one=True) and not quoted(quote, _SPOKEN.sub(" … ", source), one=True)


def _plain(detail: str, own: str = "") -> str:
    """A detail without the capital its first word has only because it opened a sentence of the book ("Flamboyant robes" is
    "flamboyant robes", "A big, beefy man" is "a big, beefy man"; "Gryffindor robes" keeps its capital), so it is never taken
    for a name in the middle of a note: lowered when the word is an ordinary sentence word or `own` (the entity's own text)
    also writes it in lowercase."""
    m = re.match(r"([A-Z][a-z]*)\b", detail)
    if not m:
        return detail
    w = m.group(1)
    if w.lower() in SENTENCE_FIRST or w.lower() in _STOP or re.search(r"(?<![\w-])" + re.escape(w.lower()) + r"(?![\w-])", own):
        return detail[:1].lower() + detail[1:]
    return detail


def _beyond(detail: str, quote: str) -> bool:
    """Whether a detail says its quote plus at most one word: every word of the quote is in it ("tall build" on "tall"), or
    they share two words or more ("big, beefy build" on "A big, beefy man"). Never "pale skin" on "the pale light"."""
    d, q = _content(detail), _content(quote)
    return bool(q) and sum(not _has(q, w) for w in d) <= 1 and (_within(q, d) or sum(_has(q, w) for w in d) >= 2)


def _content(text: str) -> set[str]:
    return {w for w in _toks(text) if w not in _STOP and len(w) > 2}


def compose_note(role: str, details: list[str], same: bool = False, capital: bool = True) -> str:
    """The appearance note as pasted into prompts: "A miller: white hair, a leather apron." ("The same miller: ..." for an
    amended one); "" when no detail is left. A detail whose words the role, an earlier detail or a fuller detail already says
    goes ("A small, filthy boy: small" is "A small, filthy boy" plus the rest)."""
    role = " ".join(role.split()).rstrip(".;:,")
    if re.fullmatch(r"(?i)(?:the|a|an)?", role):
        role = ""                      # every word of it was taken out
    ds = [d.strip().rstrip(".;,") for d in details if d and d.strip()]
    sets = [_content(d) for d in ds]
    said, kept = _content(role), []
    for n, d in enumerate(ds):
        words = sets[n]
        # a detail another one holds whole goes ("a common room" beside "a round, cozy common room"; of two equal, the first)
        if words and (_within(words, said) or any(m != n and _within(words, sets[m]) and (not _within(sets[m], words) or m < n)
                                                   for m in range(len(ds)))):
            continue
        kept.append(d)
        said |= words
    if not kept:
        return ""
    if same:
        role = "the same " + re.sub(r"^(?:the\s+same|the|a|an)\s+", "", role, flags=re.IGNORECASE)
    head = (role[:1].upper() + role[1:]) if role else ""
    body = ", ".join(kept)
    return f"{head}: {body}." if head else (body[:1].upper() if capital else body[:1]) + body[1:] + "."


# A title as it stands before a name ("Mr.", "Mrs", "Uncle"): capitalised, so "the police chief" keeps its head.
_TITLE_END = re.compile(r"(?:\s+|^)(?:" + "|".join(sorted((re.escape(t.capitalize()) + r"\.?" for t in P.TITLES), key=len,
                                                           reverse=True)) + r")[\s,;:]*$")


def _tidy(text: str, titles: bool = False) -> str:
    """A text with words taken out made whole again: spaces, a doubled article, an article, preposition or conjunction left
    dangling at the end (with `titles`, after names were taken out, a title left without its name too), a stray hyphen, and
    "a"/"an" before what now follows."""
    text = re.sub(r"(?<![\w])-(?=\w)|(?<=\w)-(?![\w])", "", text)
    text = " ".join(text.split())
    for _ in range(6):        # "Only son of Mr. and" (Mrs. Dursley taken out): "and", "Mr.", "of" go in turn (`titles`)
        text = re.sub(r"(?:\s+|^)(?:of|from|in|for|to|with|at|among|and|or|the|a|an)[\s,;:]*$", "", text,
                      flags=re.IGNORECASE).strip(" ,;:")
        if titles:
            text = _TITLE_END.sub("", text).strip(" ,;:")
    text = re.sub(r"\b(the|a|an)\s+(?=(?:the|a|an)\b)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([,;:])", r"\1", text)
    return re.sub(r"\b([Aa])n?\s+(?=(\w))", lambda m: m.group(1) + ("n " if m.group(2).lower() in "aeiou" else " "), text)


def group_look(details: list[str]) -> str:
    """A group's members' look as a figure's note carries it, with no name: "Their people: golden eyes, a winged
    sigil on the back of each hand."."""
    return f"Their people: {_list(details)}" if _list(details) else ""


def _list(details: list[str]) -> str:
    """Details as one list after a colon: compose_note's choice and order, the first one's own case kept ("a winged sigil",
    never "angelina" for "Angelina")."""
    return compose_note("", details, capital=False)


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
    finished: frozenset[str] | None = None   # earlier book keys the reader finished (None: all, as a run)
    scope: str | None = None  # a portrait's story time ("s2.1") for an entity drawn per scope; None for one with one scope


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
    inherits: list[str] = field(default_factory=list)   # earlier book keys whose content the prompt uses
    partial: bool = False     # written for a reader who has not finished every earlier book
    world: str = ""           # the world note text pasted in
    milieu: str = ""          # the surroundings pasted in (a portrait's figure's milieu, a scene's place note)
    version: int | None = NOTE_VERSION   # the note version its notes were written in (None: older, or not known)
    scope: str | None = None  # the story time its figures' notes and references are of (None: one scope, as before)
    likeness: bool = False    # a reference shows the figure at another time: code says so after the notes


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
    dropped: Counter = field(default_factory=Counter)   # parts of a text dropped or fixed by code (a note's unquoted detail...)

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
        out += [f"  {n} x {why}" for why, n in sorted(self.dropped.items())]
        out += [f"  - {r['target']}: {r['reason']}" for r in self.reasons]
        return out


class _Names:
    """The names a prompt may not use, and the one tokenizer `find` and `strip` share, so they never disagree: `strip`
    replaces exactly the spans `find`'s scan (`_spans`) reports.

    A text is split into word runs: letters and digits with inner apostrophes ("O'Brien", "Mira's"); a hyphen, a quote or any
    other mark ends a run ("half-Hagrid" is two runs, "'Mira'" is the run Mira). A run is compared without a trailing
    possessive ('s or ’s in any case; for s' the apostrophe is outside the run anyway), with either apostrophe, as the name
    writes it or all in capitals ("HAGRID"). A name is one run ("Mira", the plural "Bellonas" of a name word "Bellona") or a
    sequence of runs joined by spaces, hyphens or a title's dot, lowercase particles included ("Cassius au Bellona",
    "Jean-François", "Mrs. Figg"). A one-word name that is an ordinary sentence word ("Grace", "the Stranger") counts only
    where it does not open a sentence: the previous non-space character is ".", "!" or "?", or there is none (a quote or an
    apostrophe never opens one)."""

    _RUN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
    _GAP = re.compile(r"[\s-]+|\.\s*")                          # what joins the runs of one name
    _BEFORE = re.compile(r"(?i)(?:(?<![\w'’-])(?:(?:the|a|an)\s+)?(?:(?:" + "|".join(
        sorted((re.escape(t) for t in P.TITLES), key=len, reverse=True)) + r")\.?\s+)*)?$")   # an article and titles go too

    def __init__(self, names: list[tuple[str, bool]]) -> None:
        """`names`: (a name as the index has it, the entity is a group). An article and notes in brackets are taken off."""
        self.tokens: dict[str, tuple[str, bool]] = {}    # folded -> (as written, group): name words that count anywhere
        self.alone: dict[str, tuple[str, bool]] = {}     # one-word names that are ordinary words: "Stranger", "Grace"
        self.phrases: dict[str, list[tuple[tuple[str, ...], bool]]] = {}   # first run folded -> (runs, group), longest first
        for value, group in names:
            value = " ".join(re.sub(r"\(.*?\)", " ", value).split())
            value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.IGNORECASE)
            runs = [self._apos(self._base(m.group(0))) for m in self._RUN.finditer(value)]
            if not runs:
                continue
            words = [r for r in runs if r[:1].isupper() and len(r) >= 3 and r.lower() not in P.TITLES
                     and r.lower() not in ("the", "and", "van", "von")]
            if len(runs) > 1:
                self.phrases.setdefault(runs[0].lower(), []).append((tuple(runs), group))
            elif words and words[0].lower() in SENTENCE_FIRST:
                self._add(self.alone, words[0], group)
            for w in words:
                if w.lower() not in SENTENCE_FIRST:     # "Mira", "Harker"; never "Order" or "Dawn" alone
                    self._add(self.tokens, w, group)
        for v in self.phrases.values():
            v.sort(key=lambda t: -len(t[0]))

    @staticmethod
    def _add(table: dict, word: str, group: bool) -> None:
        was = table.get(word.lower())
        table[word.lower()] = (word, group or bool(was and was[1]))

    # ---- the tokenizer

    @staticmethod
    def _base(run: str) -> str:
        """A run without a trailing possessive ("Mira's", "MIRA’S": "Mira", "MIRA")."""
        return re.sub(r"(?<=[^\W_])['’][sS]$", "", run)

    @staticmethod
    def _apos(s: str) -> str:
        return s.replace("’", "'")

    @classmethod
    def _same(cls, run: str, name: str) -> bool:
        """A run of the text is this word of a name: as the name writes it (either apostrophe), or all in capitals."""
        run = cls._apos(run)
        return run == name or (run.isupper() and run.lower() == name.lower())

    def _runs(self, text: str) -> list[tuple[int, int, int, str]]:
        """(start, end of the name part, end of the run, the name part) per run: the name part leaves a possessive out."""
        out = []
        for m in self._RUN.finditer(text):
            base = self._base(m.group(0))
            out.append((m.start(), m.start() + len(base), m.end(), base))
        return out

    @staticmethod
    def _opens(text: str, at: int) -> bool:
        before = text[:at].rstrip()
        return not before or before[-1] in ".!?"

    def _word(self, text: str, run: tuple[int, int, int, str]) -> bool | None:
        """For a one-run name word: None when it is no name here, else whether it is a group's."""
        s, _, _, w = run
        for x in (w, w[:-1] if len(w) > 4 and w[-1] in "sS" else ""):     # "Bellonas" of "Bellona"
            if not x:
                continue
            for table, anywhere in ((self.tokens, True), (self.alone, False)):
                hit = table.get(self._apos(x).lower())
                if hit and self._same(x, hit[0]) and (anywhere or not self._opens(text, s)):
                    return hit[1]
        return None

    def _spans(self, text: str) -> list[tuple[int, int, bool]]:
        """(start, end, a group's name) of every name in the text, left to right, never overlapping; `end` leaves a
        possessive out. The one scan that `find` and `strip` both use."""
        runs, out, i = self._runs(text), [], 0
        while i < len(runs):
            hit = None
            for key, group in self.phrases.get(self._apos(runs[i][3]).lower(), []):
                seq = runs[i:i + len(key)]
                if len(seq) == len(key) and all(self._same(r[3], k) for r, k in zip(seq, key)) and all(
                        a[1] == a[2] and self._GAP.fullmatch(text[a[2]:b[0]]) for a, b in zip(seq, seq[1:])):
                    hit = (runs[i][0], seq[-1][1], group, len(key))
                    break
            if hit is None and (group := self._word(text, runs[i])) is not None:
                hit = (runs[i][0], runs[i][1], group, 1)
            if hit:
                out.append(hit[:3])
            i += hit[3] if hit else 1
        return out

    def drop_groups(self, text: str, words: bool = False) -> str:
        """The text with every group name `_spans` finds taken out, nothing put in ("A first-year Gryffindor": "A first-year",
        "Head of OPA security": "Head of security", "a frère of the Silver Order": "a frère", "a Gryffindor's prefect": "a
        prefect"), with what it leaves tidied (`_tidy`). `words` (a look detail): a one-word group name that is an ordinary
        sentence word (`SENTENCE_FIRST`, the test the name lists use) stays, lowercased, since it is also what it says ("Red
        Sigils": "red sigils", "Gold-class": "gold-class"). The text is scanned as if after other words, so a name opening it
        counts too."""
        pad = "x "
        t = pad + text
        out, last = [], 0
        for s, e, group in self._spans(t):
            if not group:
                continue
            pos = re.match(r"['’][sS](?![^\W_])", t[e:]) or (re.match(r"['’](?![^\W_])", t[e:]) if t[e - 1] in "sS" else None)
            end = e + (len(pos.group(0)) if pos else 0)
            name = t[s:e]
            if words and self._RUN.fullmatch(name) and name.lower() in SENTENCE_FIRST:
                out.append(t[last:s] + name.lower() + t[e:end])
            else:
                out.append(t[last:s] + " ")
            last = end
        new = ("".join(out) + t[last:])[len(pad):]
        # a group's name word written in another case ("a first-year gryffindor student"), never an ordinary word ("red")
        runs = [(m.start(), m.end()) for m in self._RUN.finditer(new)
                if (hit := self.tokens.get(self._apos(self._base(m.group(0))).lower())) and hit[1]
                and m.group(0).lower() not in SENTENCE_FIRST]
        for s, e in reversed(runs):
            new = new[:s] + " " + new[e:]
        return _tidy(new) if new != text else text

    # ---- the two calls

    def find(self, text: str) -> str:
        """The first name the text uses, as it is written there ("Hagrid’s" gives "Hagrid", "HAGRID" stays), or ""."""
        spans = self._spans(text)
        return text[spans[0][0]:spans[0][1]] if spans else ""

    def strip(self, text: str, attributive: bool = False) -> str:
        """The text with every name `find` reports replaced by a role word: "Jonathan Harker's case" becomes "the figure's
        case" (an article and titles before it go too), a group's name becomes "the people", and a name joined by a hyphen
        the bare noun ("half-Hagrid grin": "half-figure grin"). `attributive`, for a note's role (a noun phrase): a name before
        a lowercase word is a modifier and goes with nothing put in ("A red-haired Weasley twin": "A red-haired twin"), and so
        does a name at the end after other words, with an "of" before it ("a knight of House Bellona": "a knight")."""
        out, last = [], 0
        for s, e, group in self._spans(text):
            role = "the people" if group else "the figure"
            pos = re.match(r"['’][sS](?![^\W_])", text[e:]) or (re.match(r"['’](?![^\W_])", text[e:]) if text[e - 1] in "sS" else None)
            end = e + (len(pos.group(0)) if pos else 0)
            joined = text[s - 1:s] == "-" or text[end:end + 1] == "-"
            pre = "" if joined else self._BEFORE.search(text[last:s]).group(0)
            start = s - len(pre)
            rest = text[end:]
            if attributive and not pos and not joined and re.match(r"\s+[a-z]", rest):
                put = pre                                # a modifier ("the Bellona heir"): it goes, the article stays
            elif attributive and not pos and not joined and text[:start].strip() and re.fullmatch(r"[\s.,;:]*", rest):
                put = "\x00"                             # the head after other words ("A first-year Gryffindor")
            elif joined:
                put = role.split()[1] + ("'s" if pos else "")
            else:
                put = role + ("'s" if pos else "")
                if self._capital(text, start):
                    put = put[0].upper() + put[1:]
            out.append(text[last:start] + put)
            last = end
        new = "".join(out) + text[last:]
        new = re.sub(r"(?:\b(?:of|from|in|for|to|with|at|among)\s+)?\x00", "", new)   # "a knight", not "a knight of"
        return " ".join(new.split())

    @staticmethod
    def _capital(text: str, at: int) -> bool:
        """A role word put in at `at` starts with a capital: only spaces and quotes after a ".", "!", "?" or the start."""
        before = text[:at].rstrip(" \t\n\"'“”‘’")
        return not before or before[-1] in ".!?"


class NotVisible(ValueError):
    """The picture asked for sits after the reader's position."""


class Dropped(ValueError):
    """No usable prompt came out: the message is the report's reason, `detail` the check's own words ("unknown name: Vlad")."""

    def __init__(self, reason: str, detail: str = "", say: str = "") -> None:
        super().__init__(reason)
        self.detail = detail
        self.say = say            # the same for the person who asked, the caught word only when the reader has met it


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
        head = ""                          # a table-of-contents entry with no story of its own just before (a part's page)
        for n, (title, start) in enumerate(marks):
            end = marks[n + 1][1] - 1 if n + 1 < len(marks) else max(self.hi, start)
            evs = [e for e in d.get("events", []) if e["from"] in self.seq_of and start <= self.seq_of[e["from"]] <= end]
            rcs = [r for r in recaps if start <= self.seq_of[r["from"]] <= end]
            if not evs and not rcs:
                head = title if out else ""
                continue
            seen[title] += 1
            key = title if seen[title] == 1 else f"{title} ({seen[title]})"
            # the chapter's last anchor: its recaps' and every event's (an event may come after the last recap), so a plate,
            # whose moment, figures and passage come from any of them, is never written from content after it
            last = max([r["from"] for r in rcs] + [e["from"] for e in evs], key=self.ord_of.get)
            words = sum(_words(t) for s, t in self.text.items() if start <= s <= end)
            out.append({"n": n + 1, "title": key, "lo": start, "hi": end, "events": evs, "recaps": rcs, "last": last, "words": words,
                        "path": [head, title] if head else [title]})
            head = ""
        return out

    def chapter_of(self, anchor: str) -> dict | None:
        s = self.seq_of[anchor]
        return next((c for c in self.chapters if c["lo"] <= s <= c["hi"]), None)


def _key(rec: dict) -> tuple:
    """What a picture is, for a resume: its kind, target, anchor and, for a portrait, its story time (None for one of an entity
    with one scope, and for an older record). A scene's scope follows from its moment, so it is no part of the key."""
    return rec["kind"], rec["target"], rec["from"], rec.get("scope") if rec["kind"] == "portrait" else None


# What a failed check is called in the report.
def _reason(why: str) -> str:
    for start, name in (("unknown name", "dropped: unknown name in prompt"), ("name in the text", "dropped: name in prompt"),
                        ("wording", "dropped: hindsight wording in prompt"), ("tie not in the text", "dropped: tie not in the text"),
                        ("too short", "dropped: prompt too short"), ("look not in the note", "dropped: look not in the note"), ("too long", "dropped: prompt too long"),
                        ("empty", "dropped: empty prompt")):
        if why.startswith(start):
            return name
    return f"dropped: {why}"


# What a failed check says to the person who asked for the picture: the check and the word it caught.
_EXPLAIN = (("name in the text: ", "a name the picture must not show: {}"),
            ("unknown name: ", "a name the book has not used by this point: {}"),
            ("look not in the note: ", "a look the book does not give this figure: {}"),
            ("wording: ", "wording that looks back on the story or ahead: {}"),
            ("tie not in the text: ", "a tie the book does not state: {}"),
            ("copies the book: ", "words copied from the book: {}"))


def explain(why: str) -> str:
    """One phrase for a check's reason ("name in the text: Hagrid" gives "a name the picture must not show: Hagrid")."""
    why = " ".join(str(why or "").split())
    for start, phrase in _EXPLAIN:
        if why.startswith(start):
            return phrase.format(why[len(start):].strip())
    return {"too short": "a text too short to draw from", "too long": "a text too long", "empty": "no text at all"}.get(why, why)


def portrait_cast(value) -> str | None:
    """Whose portraits a run draws, normalized: "main", "all", or None for no portraits. True (an older config) means "all"."""
    if value is True:
        return "all"
    if not value:
        return None
    if value in CASTS:
        return value
    raise ValueError(f"portraits must be one of main, all or none, not {value!r}")


class Illustrator:
    """The pipeline over one or more books in series reading order. `run()` draws whole sets; `job`, `make_prompt`
    and `draw` are the pieces the Reader's on-demand drawing calls one at a time."""

    def __init__(self, books: list[Book], text, image, style: str, *, portraits: str | bool | None = "main", quality: str = "standard",
                 cap: float | None = None, max_pictures: int | None = None, chapters: set[int] | None = None,
                 series: dict | None = None, workers: int = WORKERS, log: Callable = print, progress: Callable | None = None,
                 pause: float = 1.0, story_time: bool | None = None, likeness: bool | None = None) -> None:
        self.books = [_B(i, b) for i, b in enumerate(books)]
        if len(self.books) > 1:
            ids = {(b.doc.get("series") or {}).get("id") for b in self.books}
            if None in ids or len(ids) > 1 or any(not (b.doc.get("series") or {}).get("book") for b in self.books):
                raise ValueError("several indexes are drawn together only as books of one series (each with the same "
                                 "`series.id` and its own `series.book`); draw standalone books one at a time")
        self.text, self.image, self.style = text, image, style
        self.portraits = portrait_cast(portraits)      # "main", "all" or None
        self.quality, self.cap, self.max_pictures = quality, cap, max_pictures
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
        self._checks: dict[str, list[str]] = {}
        self._class_lock = threading.Lock()
        self._members: dict[int, dict[str, list[int]]] = {}
        self._earlier = [frozenset().union(*(b.first for b in self.books[:i])) for i in range(len(self.books))]
        self._dirty: set[int] = set()     # books whose sink received a picture
        self.story_time = STORY_TIME if story_time is None else story_time
        self.likeness = LIKENESS_REFERENCE if likeness is None else likeness
        self._tl_lock = threading.RLock()
        self._tl_version = 0              # bumped whenever a chapter is labelled: the scope memos below are rebuilt
        self._tl_memo: dict[tuple, dict] = {}
        self._presence_memo: dict[tuple, list] = {}
        self._tl_partial: dict[tuple, dict] = {}   # timelines of a reader who has not finished every earlier book: never stored
        self._tl_failed: set[tuple] = set()        # (book, finished) whose labelling failed in this run: unknown from there
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
            for k in ("world", "worldOwn"):
                w = m.get(k)
                if w and not w.get("edited") and not w.get("era"):
                    # a world note of the first version describes the opening's places and clothing as true of every
                    # picture: set aside, and an era note is written in its place (pictures drawn from it keep their prompts)
                    m.setdefault("worldOld", {})[k] = m.pop(k)
                    m[k] = None
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
        names: list[tuple[str, bool]] = []
        for b in self.books:
            names += [(n["value"], e["type"] == "group") for e in b.doc["entities"]
                      if e["type"] in ("person", "creature", "group") for n in e["names"]]
            out[b.i] = _Names(list(names))
        return out

    def _vet(self, text, bi: int, anchor: str, source: str, words: tuple[int, int],
             looks: str | None = None) -> tuple[str | None, str]:
        """(text, "") when a written text is usable at an anchor, else (None, reason). With `looks`, the text may give a figure
        no look that `looks` (the notes and passage it was written from) does not state: one cheap check call lists such
        phrases (`unfounded_looks`), and the reason names them ("look not in the note: a blue coat | young")."""
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
        if looks is not None and (bad := self.unfounded_looks(got, looks)):
            return None, "look not in the note: " + " | ".join(bad)
        return got, ""

    def unfounded_looks(self, text: str, source: str) -> list[str]:
        """The phrases of a written prompt that give a figure a look `source` (its notes and passage) does not state, as one
        cheap call lists them (`LOOK_CHECK`, the pattern of the fact-check). Only a phrase that is in the text, word for word,
        counts: the call can point at words, never add any. Cached per text and source. A call that fails twice raises
        TextFailed, so the picture is skipped as when its writer fails ("skipped: text model failed")."""
        key = hashlib.sha256((text + "\x00" + source).encode()).hexdigest()
        with self.lock:
            if key in self._checks:
                return self._checks[key]
        out = self._complete(LOOK_CHECK.format(source=source[:6000] or "(none)", text=text), LOOK_CHECK_SCHEMA)
        bad: list[str] = []
        flat = " ".join(_toks(text))
        for x in out.get("phrases") or [] if isinstance(out.get("phrases"), list) else []:
            x = " ".join(str(x).split()).strip(" .,;:")
            if _toks(x) and " " + " ".join(_toks(x)) + " " in " " + flat + " " and x not in bad:
                bad.append(x)
        with self.lock:
            self._checks[key] = bad
        return bad

    def _write_full(self, prompt: str, bi: int, anchor: str, source: str, words: tuple[int, int],
                    schema: dict = SCHEMA, looks: str | None = None) -> tuple[str | None, str, dict]:
        """One call, one rewrite when a check fails, then, when only a name or an unfounded look is wrong, code takes it out
        (`_fix`) and the text is checked again. (text, "", the reply) or (None, reason, the reply)."""
        out = self._complete(prompt, schema)
        got, why = self._vet(out.get("text"), bi, anchor, source, words, looks)
        if got is None and why != "empty":
            tries = [(str(out.get("text") or ""), why, out)]
            out = self._complete(prompt + REWRITE.format(text=str(out.get("text", ""))[:600], why=why), schema)
            got, why = self._vet(out.get("text"), bi, anchor, source, words, looks)
            if got is None:
                for text_, why_, out_ in [(str(out.get("text") or ""), why, out)] + tries:
                    fixed, how = self._fix(text_, why_, bi, anchor, source, words, looks)
                    if fixed is not None:
                        with self.lock:
                            for h in how:
                                self.report.dropped[h] += 1
                        return fixed, "", out_
        return got, why, out

    def _fix(self, text: str, why: str, bi: int, anchor: str, source: str, words: tuple[int, int],
             looks: str | None) -> tuple[str | None, list[str]]:
        """The last try on a text that failed twice: a name is replaced by a role ("the figure") and the phrases the look check
        found unfounded are taken out, then the text is checked again (the look check is not asked again about what is left:
        code only removed words). (text, what was fixed) or (None, []) when it still fails or failed for another reason."""
        how: list[str] = []
        checked = False
        for _ in range(8):
            if why.startswith("name in the text"):
                text, how = self.names[bi].strip(text), how + ["fixed: name replaced by a role"]
            elif why.startswith("look not in the note: ") and looks is not None:
                for phrase in why[len("look not in the note: "):].split(" | "):
                    text = _drop_phrase(text, phrase)
                how.append("fixed: look word not in the notes taken out")
                checked = True
            else:
                return None, []
            got, why = self._vet(text, bi, anchor, source, words, None if checked else looks)
            if got is not None:
                return got, how
        return None, []

    def _write(self, prompt: str, bi: int, anchor: str, source: str, words: tuple[int, int],
               looks: str | None = None) -> tuple[str | None, str]:
        got, why, _ = self._write_full(prompt, bi, anchor, source, words, looks=looks)
        return got, why

    def view(self, bi: int, anchor: str, finished: frozenset[str] | None = None) -> dict:
        """The gated view of books 0..bi at an anchor of book bi: what a reader there may know (`finished`: the earlier books
        they finished; None: all of them)."""
        finished = self._finished(bi, finished)
        key = (bi, anchor, finished)
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
                    fin = None if finished is None else set(finished)
                    if self.series:
                        v = gating.view(docs, gating.Position(keys[-1], anchor), self.series, finished=fin)
                    else:
                        v = gating.view(docs, gating.Position(keys[-1], anchor),
                                        {"defaultOrder": "run", "orders": {"run": keys}}, "run", fin)
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

    def _mentions(self, bi: int, eid: str, upto: int, k: int = 6, scope: str | None = None,
                  fin: frozenset[str] | None = None) -> list[int]:
        """Up to k blocks naming the entity up to block `upto`, sampled evenly; with a `scope`, only blocks told in it."""
        return P._sample(self._in_scope(bi, [s for s, at in self.books[bi].hits(eid) if s <= upto and at <= upto], scope, fin), k)

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

    def _world_inputs(self, bi: int):
        """(anchor, recaps, passages, sources) the book's own world note is written from, or None (no story chapter)."""
        chs = self._story_chapters(bi)
        if not chs:
            return None
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
        return at, recaps, passages, sources

    def world(self, bi: int, finished: frozenset[str] | set[str] | None = None) -> dict | None:
        """The book's world note: written once from its first chapters; in a series the first book's, to which a later book may
        append one sentence. A note the person edited is never rewritten. None when none can be written.

        `finished`: the earlier books' keys the reader has finished (None: all of them, as a run assumes). When the note the
        set uses comes from an earlier book outside it, the book's *own* note is used instead (`worldOwn`, written from its
        own first story chapters the first time it is needed), so a reader who skipped book one never gets its world."""
        finished = self._finished(bi, finished)
        m = self.man[bi]
        if not m.get("world"):
            base = next((self.man[j]["world"] for j in range(bi - 1, -1, -1) if self.man[j].get("world")), None)
            got = self._world_inputs(bi)
            if got is None:
                if finished is None or base is None or set(self._world_books(bi, base)) <= finished:
                    return base
                return None
            at, recaps, passages, sources = got
            body = f"Recaps and settings:\n{recaps}\n\nPassages:\n{passages}"
            try:
                w = self._world_write(bi, base, body, recaps, passages, at, sources)
            except TextFailed as e:
                self.log(f"  world note: text model failed ({e.detail})")
                self._skip(bi, "world note", str(e))
                w = None
            if w is not None:
                m["world"] = w
                self._save(bi)
            elif finished is None:
                return base
        w, _, inherits = self._world_for(bi, None)
        if finished is None or set(inherits) <= finished:
            return w
        return self._own_world(bi)

    def _own_world(self, bi: int) -> dict | None:
        """The world note written from this book alone (`worldOwn`): for a reader who has not finished the earlier book the
        set's note comes from."""
        m = self.man[bi]
        if m.get("worldOwn"):
            return m["worldOwn"]
        if (m.get("world") or {}).get("book") == self._bkey(bi) and not m["world"].get("base") and not m["world"].get("inherited"):
            return m["world"]
        got = self._world_inputs(bi)
        if got is None:
            return None
        at, recaps, passages, sources = got
        try:
            w = self._world_write(bi, None, "", recaps, passages, at, sources)
        except TextFailed as e:
            self.log(f"  own world note: text model failed ({e.detail})")
            return None
        if w is not None:
            with self.lock:
                m["worldOwn"] = w
                self._dirty.add(bi)
                self._save(bi)
        return w

    def _world_write(self, bi, base, body, recaps, passages, at, sources) -> dict | None:
        if base:
            # an inherited note keeps the anchor of the book it was written in, with that book's key: anchor ids repeat across
            # the books of a series, so an anchor is only ever looked up in its own book
            j = next(j for j in range(bi - 1, -1, -1) if self.man[j].get("world") is base)
            books = self._world_books(j, base)
            w = {"text": base["text"], "edited": base.get("edited", False), "from": base["from"],
                 "book": base.get("book", self._bkey(j)), "inherited": True, "inherits": books, "era": True}
            if not base.get("edited"):
                more, why = self._write(WORLD_MORE.format(base=base["text"]) + "\n" + body, bi, at, passages + recaps, (0, 70))
                if more:
                    w = {"text": base["text"] + " " + more, "edited": False, "from": at, "book": self._bkey(bi), "base": base["text"],
                         "inherits": books, "sources": sources, "era": True}
                elif why != "empty":
                    self.log(f"  world note: no addition ({why})")
        else:
            got, why = self._write(WORLD.format(recaps=recaps, passages=passages), bi, at, passages + "\n" + recaps, WORLD_WORDS)
            if got is None:
                self.log(f"  world note dropped: {why}")
                self._skip(bi, "world note", _reason(why).replace("prompt", "world note"))
                return None
            w = {"text": got, "edited": False, "from": at, "book": self._bkey(bi), "sources": sources, "era": True}
        return w

    def _world_books(self, j: int, w: dict) -> list[str]:
        """The book keys whose content a world note found in book j's manifest holds: the book that wrote it and the books its
        own text came from."""
        own = w.get("book") or (self._bkey(j) if not w.get("inherited") else None)
        prev = next((jj for jj in range(j - 1, -1, -1) if self.man[jj].get("world")), None)
        if own is None and prev is not None:
            own = self._bkey(prev)
        return sorted(set(w.get("inherits", [])) | ({own} if own else set()))

    def _world_for(self, bi: int, finished: frozenset[str] | None) -> tuple[dict | None, bool, list[str]]:
        """(the world note a picture of book bi uses for a reader who finished `finished`, whether its `from` is an anchor of
        book bi, the earlier book keys its text comes from)."""
        w, own = self._world_of(bi)
        if not w:
            return None, False, []
        j = bi if self.man[bi].get("world") else next(jj for jj in range(bi - 1, -1, -1) if self.man[jj].get("world") is w)
        inherits = [k for k in self._world_books(j, w) if k != self._bkey(bi)]
        if finished is None or set(inherits) <= finished:
            return w, own, inherits
        mine = self.man[bi].get("worldOwn") or (w if own and not w.get("base") and not inherits else None)
        return (mine, bool(mine), []) if mine else (None, False, [])

    def _world_text(self, bi: int, pos: tuple[int, int], finished: frozenset[str] | None = None) -> tuple[str, str | None, list[str]]:
        """(text, anchor, inherits) of the world note a picture at `pos` is written from: the series' older text when an
        appended sentence of this book lies ahead of it; for a reader who has not finished the book it comes from, this book's
        own note, or none."""
        w, own, inherits = self._world_for(bi, finished)
        if not w:
            return "", None, []
        o = self.books[bi].ord_of
        if not own:                    # written in an earlier, finished book: nothing in it lies ahead of the reader
            return w["text"], None, inherits
        if w.get("base") and (bi, o[w["from"]]) > pos:
            return w["base"], None, inherits
        return w["text"], w["from"], inherits

    def _bkey(self, bi: int) -> str:
        """The key of a book that anchors are stored with: its series book key, else its id."""
        d = self.books[bi].doc
        return str((d.get("series") or {}).get("book") or d.get("id") or bi)

    def _finished(self, bi: int, finished) -> frozenset[str] | None:
        """`finished` as a set of earlier book keys, or None when it covers every earlier book (a run's reader)."""
        if finished is None:
            return None
        f = frozenset(str(k) for k in finished)
        return None if all(self._bkey(j) in f for j in range(bi)) else f

    def _usable(self, j: int, bi: int, finished: frozenset[str] | None, rec: dict | None = None) -> bool:
        """Whether content of book j (a stored note or picture `rec` from its manifest) may reach a prompt of book bi for a
        reader who finished `finished`: book bi itself or a finished one, and every book the record inherits finished too.
        A record written for a reader with fewer books (`partial`) is used only for such a reader."""
        if rec is not None and rec.get("partial") and finished is None:
            return False
        if finished is None:
            return True
        if j != bi and self._bkey(j) not in finished:
            return False
        return rec is None or set(rec.get("inherits", [])) <= finished | {self._bkey(bi)}

    def _world_of(self, bi: int) -> tuple[dict | None, bool]:
        """(the world note a picture of book bi uses, whether its `from` is an anchor of book bi)."""
        w = self.man[bi].get("world")
        if w:
            book = w.get("book")
            own = (book == self._bkey(bi)) if book is not None else not w.get("inherited")
            return w, own and w["from"] in self.books[bi].ord_of
        return next((self.man[j]["world"] for j in range(bi - 1, -1, -1) if self.man[j].get("world")), None), False

    def _anchor(self, bi: int, anchor: str, finished: frozenset[str] | None = None) -> str:
        """The anchor a picture shows from: a picture of the book's first chapters is held to the end of the chapters its
        world note is written from, so that no picture is written from content after its anchor."""
        w, own, _ = self._world_for(bi, finished)
        b = self.books[bi]
        if w and own and not w.get("base") and b.ord_of[w["from"]] > b.ord_of[anchor]:
            return w["from"]
        return anchor

    # ---- story time: the line of telling (strand) and the era each chapter tells, and the scope of what is drawn

    def _tl_get(self, j: int, fin: frozenset[str] | None) -> dict:
        """The timeline book j's labels are read from for a reader of book bi who finished `fin` (None: all): the manifest's
        when every book before j is finished, else an in-memory one (labels made with fewer books are never stored)."""
        fj = self._finished(j, fin)
        if fj is None:
            return self.man[j].get("timeline") or {}
        return self._tl_partial.get((j, fj)) or {}

    def _tl_put(self, j: int, fin: frozenset[str] | None) -> dict:
        fj = self._finished(j, fin)
        if fj is None:
            tl = self.man[j].setdefault("timeline", {})
        else:
            tl = self._tl_partial.setdefault((j, fj), {})
        tl.setdefault("strands", [])
        tl.setdefault("chapters", {})
        return tl

    def _tl_books(self, bi: int, fin: frozenset[str] | None) -> list[int]:
        """The books whose lines of telling a reader of book bi knows: bi and the earlier books they finished."""
        return [j for j in range(bi + 1) if j == bi or fin is None or self._bkey(j) in fin]

    def _tl_keys(self, j: int, fin: frozenset[str] | None) -> list[str]:
        """Each chapter's key: its title and non-minor events (`_screen_key`), chained to the chapter before (the last one of
        the previous book the reader knows), so a rebuilt index is labelled again from its first changed chapter on."""
        memo = ("keys", j, fin)
        if memo in self._tl_memo:
            return self._tl_memo[memo]
        books = [x for x in self._tl_books(j, fin) if x < j]
        prev = self._tl_keys(books[-1], fin)[-1:] if books else []
        prev = prev[0] if prev else ""
        out = []
        for ch in self.books[j].chapters:
            evs = [e for e in ch["events"] if e.get("kind") != "minor"]
            prev = hashlib.sha256(json.dumps([prev, ch["title"], self._screen_key(evs)], ensure_ascii=False).encode()).hexdigest()[:16]
            out.append(prev)
        self._tl_memo[memo] = out
        return out

    def _walk(self, bi: int, fin: frozenset[str] | None = None) -> dict:
        """The labelled chapters of the books a reader of book bi knows, replayed in reading order (in each book its labelled
        prefix only): {"strands": {id: {"id", "label", "book", "from", "era"}}, "books": {j: {title: chapter}}, "last":
        the last labelled chapter's strand or None}, each chapter {"lo", "hi", "scope", "cue", "events", "blocks"}: its scope
        ("s2.1", None when unknown), the block its scope starts at, its events' own scopes (an exception: a scope, OTHER or
        None) and the blocks those events cite. No model call."""
        memo = ("walk", bi, fin, self._tl_version)
        with self.lock:
            if memo in self._tl_memo:
                return self._tl_memo[memo]
        strands: dict[str, dict] = {}
        books: dict[int, dict] = {}
        last = None
        for j in self._tl_books(bi, fin):
            b, tl, keys = self.books[j], self._tl_get(j, fin), self._tl_keys(j, fin)
            got = tl.get("chapters") or {}
            books[j] = {}
            for n, ch in enumerate(b.chapters):
                lab = got.get(ch["title"])
                if not isinstance(lab, dict) or lab.get("key") != keys[n]:
                    break
                s, era = lab.get("strand"), lab.get("era")
                if s and lab.get("label") and s not in strands and isinstance(era, int):
                    cue = (lab.get("cue") or {}).get("seq", ch["lo"])
                    strands[s] = {"id": s, "label": lab["label"], "book": self._bkey(j), "from": b.anchor_at(cue), "era": 1}
                scope = None
                if s in strands and isinstance(era, int):
                    strands[s]["era"] = era
                    strands[s]["last"] = (j, ch["title"])
                    if lab.get("when"):
                        strands[s]["when"] = lab["when"]
                    scope = f"{s}.{era}" if era <= MAX_ERAS else None
                exc, blocks = {}, {}
                for eid, x in (lab.get("events") or {}).items():
                    if x == OTHER:
                        exc[eid] = OTHER
                    elif x in strands:
                        e2 = strands[x]["era"]
                        exc[eid] = f"{x}.{e2}" if e2 <= MAX_ERAS else None
                    else:
                        continue
                    ev = b.events.get(eid)
                    for a in (ev or {}).get("evidence", []):
                        if a in b.seq_of:
                            sq = b.seq_of[a]
                            blocks[sq] = exc[eid] if blocks.get(sq, exc[eid]) == exc[eid] else None
                cue = (lab.get("cue") or {}).get("seq", ch["lo"]) if scope else ch["lo"]
                books[j][ch["title"]] = {"lo": ch["lo"], "hi": ch["hi"], "scope": scope, "cue": cue, "events": exc,
                                         "blocks": blocks, "strand": s if s in strands else None, "key": keys[n],
                                         "eras": {k: v["era"] for k, v in strands.items()}}
                last = s if s in strands else None
        out = {"strands": strands, "books": books, "last": last}
        with self.lock:
            self._tl_memo[memo] = out
        return out

    def timeline(self, bi: int = -1, upto: int | None = None, finished=None) -> None:
        """Label the chapters of book bi (and of the earlier books the reader knows, first) with the line of telling and the era
        each tells: one cheap call per chapter, in reading order, up to the chapter holding block `upto` (None: the whole book),
        resumable (a chapter whose chained key still matches is not asked again). The call for a chapter sees that chapter's
        heading, its opening, its events and the lines known so far (their recaps' first and last sentence), never anything
        after it, and a label is never revised. Code keeps a label only when it holds: a new line or a jump of years needs a
        quote found in the cue paragraph, an event's line must be a known one or "other", MAX_STRANDS and MAX_ERAS bound
        them; a chapter that fails them is stored as unknown. A failed call, or the cost cap, leaves that chapter and the rest
        unlabelled (unknown) for this run. For a reader who finished fewer earlier books (`finished`) the labels are made with
        only those books' lines and kept in memory, never stored. Nothing is labelled without the book's EPUB."""
        if not self.story_time:
            return
        bi = len(self.books) - 1 if bi < 0 else bi
        fin = self._finished(bi, finished)
        with self._tl_lock:
            for j in self._tl_books(bi, fin):
                if not self._label_book(j, fin, upto if j == bi else None):
                    return

    def _label_book(self, j: int, fin: frozenset[str] | None, upto: int | None) -> bool:
        """Label book j's chapters up to block `upto`; False when it stopped (a failure or the cap)."""
        b = self.books[j]
        if b.epub is None:
            return True                     # an earlier book that is only read: its stored labels are used as they are
        fj = self._finished(j, fin)
        if (j, fj) in self._tl_failed:
            return False
        keys = self._tl_keys(j, fin)
        for n, ch in enumerate(b.chapters):
            if upto is not None and ch["lo"] > upto:
                break
            lab = (self._tl_get(j, fin).get("chapters") or {}).get(ch["title"])
            if isinstance(lab, dict) and lab.get("key") == keys[n]:
                continue
            with self.lock:
                if self._over_cap():
                    self.stop = self.stop or f"cost cap ${self.cap:.2f} reached"
            if self.stop:
                self._tl_failed.add((j, fj))
                return False
            try:
                got = self._label_chapter(j, fin, ch)
            except TextFailed as e:
                self.log(f"  timeline of {ch['title']}: text model failed ({e.detail})")
                self._tl_failed.add((j, fj))
                return False
            with self.lock:
                tl = self._tl_put(j, fin)
                tl["chapters"][ch["title"]] = {"key": keys[n], **got}
                self._tl_version += 1
                self._tl_memo = {k: v for k, v in self._tl_memo.items() if k[0] == "keys"}
                self._presence_memo.clear()
                if fj is None:
                    w = self._walk(j, fin)
                    tl["strands"] = [{k: v for k, v in s.items() if k not in ("era", "last", "when")} for s in w["strands"].values()
                                     if s["book"] == self._bkey(j)]
                    tl["runs"] = self._runs(j, w)
                    self._dirty.add(j)
                    self._save(j)
        return True

    def _runs(self, j: int, w: dict) -> list[dict]:
        """The story time of book j's blocks for the Reader: [{"seq", "scope"}] sorted by block, a run starting at each
        chapter's first block (None until the chapter's cue, then its scope), None for a chapter not labelled."""
        b = self.books[j]
        out: list[dict] = []

        def put(seq: int, scope) -> None:
            if out and out[-1]["seq"] == seq:
                out[-1]["scope"] = scope
                if len(out) > 1 and out[-2]["scope"] == scope:
                    out.pop()
            elif not out or out[-1]["scope"] != scope:
                out.append({"seq": seq, "scope": scope})
        labs = w["books"].get(j, {})
        for n, ch in enumerate(b.chapters):
            lab = labs.get(ch["title"])
            put(ch["lo"], None)
            if lab and lab["scope"]:
                put(lab["cue"], lab["scope"])
            nxt = b.chapters[n + 1]["lo"] if n + 1 < len(b.chapters) else None
            if nxt is not None and nxt > ch["hi"] + 1:
                put(ch["hi"] + 1, None)      # a stretch no chapter covers (a part's page): unknown
        return out

    def _label_chapter(self, j: int, fin: frozenset[str] | None, ch: dict) -> dict:
        """One timeline call for a chapter, checked: {"strand", "era", "cue": {"seq", "quote"}, "events", "label"?, "skip"?}
        or {"strand": None, "why"} for a label that does not hold. Raises TextFailed."""
        b = self.books[j]
        w = self._walk(j, fin)                 # the chapters before this one (labels are made in order)
        strands = w["strands"]
        prev = w["last"]
        # the opening: whole paragraphs until OPENING_WORDS, then the events with the start of the paragraph each cites
        paras: list[int] = []
        lines, n_words = [], 0
        for s in range(ch["lo"], ch["hi"] + 1):
            if s not in b.text:
                continue
            ws = b.text[s].split()
            take = ws[:max(1, OPENING_WORDS - n_words)]
            paras.append(s)
            lines.append(f"[{len(paras)}] " + " ".join(take) + (" …" if len(take) < len(ws) else ""))
            n_words += len(take)
            if n_words >= OPENING_WORDS:
                break
        evs = [e for e in ch["events"] if e.get("kind") != "minor"]
        ev_lines = []
        for n, e in enumerate(evs, 1):
            seqs = sorted(b.seq_of[a] for a in e.get("evidence", []) if a in b.seq_of and b.seq_of[a] in b.text)
            line = f"{n}. {' '.join(e['summary'].split())}"
            if seqs:
                s = seqs[0]
                if s not in paras:
                    paras.append(s)
                ws = b.text[s].split()
                line += f"\n   [{paras.index(s) + 1}] " + " ".join(ws[:EVIDENCE_WORDS]) + (" …" if len(ws) > EVIDENCE_WORDS else "")
            ev_lines.append(line)
        cat = [self._catalog_line(s, w) for s in strands.values()]
        catalog = "\n".join(cat) if cat else '(none yet: this is the first chapter, so return "new")'
        previous = f"The previous chapter was told in {prev}.\n" if prev else ""
        out = self._complete(TIMELINE.format(catalog=catalog, previous=previous, path=" > ".join(ch["path"]),
                                             opening="\n".join(lines) or "(no text)",
                                             events="\n".join(ev_lines) or "(none)"), TIMELINE_SCHEMA)
        if not out:
            return {"strand": None, "why": "no answer"}     # refused or cut off: this chapter is unknown, the rest goes on
        said = str(out.get("strand") or "").strip().strip(".").lower()
        # the per-line check decides when it is plain: the one known line the reply says is at this chapter's time
        same = [str(x.get("id") or "").strip().lower() for x in out.get("lines") or [] if isinstance(out.get("lines"), list)
                and isinstance(x, dict) and x.get("same_time") is True]
        same = [x for x in dict.fromkeys(same) if x in strands]
        if len(same) == 1 and said != same[0]:
            said = same[0]
        # looks change over years: a chapter the reply puts less than a year from the chapter before opens no new line
        try:
            gap = abs(float(out.get("gap")))
        except (TypeError, ValueError):
            gap = None
        if said == "new" and prev and gap is not None and gap < 1:
            said = prev
        cue = out.get("cue") if isinstance(out.get("cue"), dict) else {}
        try:
            cn = int(cue.get("paragraph"))
        except (TypeError, ValueError):
            cn = 0
        cue_seq = paras[cn - 1] if 1 <= cn <= len(paras) else None
        quote = " ".join(str(cue.get("quote") or "").split())

        def found(q: str) -> bool:
            t = _toks(q)
            return bool(t) and cue_seq in b.text and f" {' '.join(t)} " in f" {' '.join(_toks(b.text[cue_seq]))} "
        if said == "new" or (not strands and said not in strands):
            if strands:
                if len(strands) >= MAX_STRANDS:
                    return {"strand": None, "why": "too many lines of telling"}
                if not found(quote):
                    return {"strand": None, "why": "new line without a quoted cue"}
            else:
                cue_seq, quote = ch["lo"], quote if found(quote) else ""    # the first line of all is no shift: nothing to show
            ids = {s["id"] for jj in range(len(self.books)) for s in (self.man[jj].get("timeline") or {}).get("strands", [])}
            k = len(strands) + 1
            while f"s{k}" in ids or f"s{k}" in strands:
                k += 1
            sid = f"s{k}"
            got = {"strand": sid, "label": " ".join(str(out.get("label") or "").split())[:120] or "a line of telling", "era": 1,
                   "cue": {"seq": cue_seq, "quote": quote}}
        elif said in strands:
            era = strands[said]["era"]
            sq = " ".join(str(out.get("skip_quote") or "").split())
            skip = out.get("skip") is True and found(sq)
            got = {"strand": said, "era": era + 1 if skip else era}
            if skip:
                got["skip"] = sq
            if cue_seq is None or (prev == said and not skip):
                cue_seq = ch["lo"]          # the chapter goes on in the line before it: known from its first block
            got["cue"] = {"seq": cue_seq, "quote": quote if found(quote) else ""}
        else:
            return {"strand": None, "why": "no such line of telling"}
        when = " ".join(str(out.get("when") or "").split())[:160]
        if when:
            got["when"] = when             # internal: the line's latest time, shown with it to the next chapters' calls
        known = set(strands) | {got["strand"]}
        exc = {}
        for x in out.get("events") or [] if isinstance(out.get("events"), list) else []:
            if not isinstance(x, dict):
                continue
            try:
                n = int(x.get("n"))
            except (TypeError, ValueError):
                continue
            s = str(x.get("line") or x.get("strand") or "").strip().strip(".").lower()
            if s == "new" and said == "new":
                s = got["strand"]           # the line this chapter opens
            if 1 <= n <= len(evs) and s != got["strand"] and (s == OTHER or s in known):
                exc[evs[n - 1]["id"]] = s
        got["events"] = exc
        return got

    def _catalog_line(self, s: dict, w: dict) -> str:
        """One known line for the timeline call, from what is labelled so far only: its id and label, where it was first told
        (its first chapter's heading), the people its events name most, its era, and its latest chapter's opening and last
        recap sentence (else its first and last events)."""
        names: Counter = Counter()
        first = None
        for j, labs in w["books"].items():
            b = self.books[j]
            for c in b.chapters:
                lab = labs.get(c["title"])
                if not lab:
                    continue
                for e in c["events"]:
                    own = lab["events"].get(e["id"])
                    if (own or "").split(".")[0] == s["id"] or (own is None and e["id"] not in lab["events"] and lab["strand"] == s["id"]):
                        if first is None:
                            first = " > ".join(c["path"])
                        for x in e.get("entities", []):
                            ent = next((bb.ents[x] for bb in self.books if x in bb.ents), None)
                            if ent and ent["type"] in PORTRAIT_TYPES:
                                names[ent["names"][0]["value"]] += 1
        said = ""
        jj, title = s.get("last", (None, None))
        if jj is not None:
            c = next((c for c in self.books[jj].chapters if c["title"] == title), None)
            sents = [x for r in (c or {}).get("recaps", []) for x in re.split(r"(?<=[.!?])\s+", " ".join(r["text"].split())) if x]
            if not sents:
                sents = [" ".join(e["summary"].split()) for e in (c or {}).get("events", []) if e.get("kind") != "minor"]
            if sents:
                said = f'\n  its latest chapter opens "{sents[0]}"' + (f' and ends "{sents[-1]}"' if len(sents) > 1 else "")
        who = ", ".join(n for n, _ in names.most_common(6))
        return (f"- {s['id']}: {s['label']}; era {min(s['era'], MAX_ERAS + 1)}" + (f"; first told in {first}" if first else "")
                + (f"\n  its time in its latest chapter: {s['when']}" if s.get("when") else "")
                + (f"\n  people seen in it: {who}" if who else "") + said)

    def shows_at(self, bi: int, eid: str, seqs, upto: int, fin: frozenset[str] | None = None) -> dict[int, str | None]:
        """{block: scope} for paragraphs naming an entity drawn per story time: at which line's time each one shows it (a frame
        paragraph inside a chapter of the tale shows the teller at the frame's time, which the chapter's scope cannot say). One
        cheap call per SHOWS_BATCH paragraphs not checked yet, which only chooses (a line id among those known by block `upto`, or
        "none"); cached in the manifest as `shows[eid][block] = {"k": the chapter's label key, "line"}`, so a relabelled chapter
        is checked again. A paragraph the call leaves out, or one of a failed call, keeps its chapter's scope; "none" is None."""
        b = self.books[bi]
        w = self._walk(bi, fin)
        labs = w["books"].get(bi, {})
        of = {}
        for s in sorted({x for x in seqs if x in b.text and x <= upto}):
            ch = next((c for c in b.chapters if c["lo"] <= s <= c["hi"]), None)
            if ch and ch["title"] in labs and labs[ch["title"]]["scope"]:
                of[s] = labs[ch["title"]]
        store = (self.man[bi].setdefault("shows", {}) if self._finished(bi, fin) is None else
                 self._tl_partial.setdefault(("shows", bi, fin), {})).setdefault(eid, {})
        out: dict[int, str | None] = {}
        todo = []
        for s, lab in of.items():
            got = store.get(str(s))
            if isinstance(got, dict) and got.get("k") == lab["key"]:
                out[s] = self._line_scope(got.get("line"), lab)
            else:
                todo.append(s)
        known = {k: v for k, v in w["strands"].items() if b.ord_of.get(v["from"], -1) <= b.ord_of[b.anchor_at(upto)]
                 or v["book"] != self._bkey(bi)}
        e = next((bb.ents[eid] for bb in self.books if eid in bb.ents), None)
        who = e["names"][0]["value"] if e else eid
        catalog = "\n".join(f"- {k}: {v['label']}" for k, v in known.items())
        for i in range(0, len(todo), SHOWS_BATCH):
            batch = todo[i:i + SHOWS_BATCH]
            paras = "\n\n".join(f"{n}. [{of[s]['strand']}] " + " ".join(b.text[s].split()[:SHOWS_WORDS]) for n, s in enumerate(batch, 1))
            try:
                got = self._complete(SHOWS_AT.format(catalog=catalog, who=who, paras=paras), SHOWS_SCHEMA)
            except TextFailed as e2:
                self.log(f"  paragraph check for {eid}: text model failed ({e2.detail})")
                got = {}
            said = {}
            for x in got.get("paragraphs") or [] if isinstance(got.get("paragraphs"), list) else []:
                try:
                    n = int(x.get("n"))
                except (AttributeError, TypeError, ValueError):
                    continue
                line = str(x.get("line") or "").strip().strip(".").lower()
                if 1 <= n <= len(batch) and (line in known or line == "none"):
                    said[batch[n - 1]] = line
            with self.lock:
                for s in batch:
                    if s in said:
                        store[str(s)] = {"k": of[s]["key"], "line": said[s]}
                    out[s] = self._line_scope(said.get(s, of[s]["strand"]), of[s])
                if said and self._finished(bi, fin) is None:
                    self._dirty.add(bi)
                    self._save(bi)
        return out

    @staticmethod
    def _line_scope(line: str | None, lab: dict) -> str | None:
        """A line id said of a paragraph, as the scope it has in that paragraph's chapter."""
        if not line or line == "none":
            return None
        if line == lab["strand"]:
            return lab["scope"]
        era = lab["eras"].get(line)
        return f"{line}.{era}" if era and era <= MAX_ERAS else None

    def _chapter_lab(self, bi: int, seq: int, fin: frozenset[str] | None) -> dict | None:
        ch = next((c for c in self.books[bi].chapters if c["lo"] <= seq <= c["hi"]), None)
        return self._walk(bi, fin)["books"].get(bi, {}).get(ch["title"]) if ch else None

    def scope_of(self, bi: int, anchor: str, fin: frozenset[str] | None = None) -> str | None:
        """The story time at an anchor: its chapter's scope from the chapter's cue on, else None (not labelled, or before)."""
        if not self.story_time or anchor not in self.books[bi].seq_of:
            return None
        s = self.books[bi].seq_of[anchor]
        lab = self._chapter_lab(bi, s, fin)
        return lab["scope"] if lab and s >= lab["cue"] else None

    def _block_scope(self, bi: int, seq: int, fin: frozenset[str] | None = None) -> str | None:
        """The story time of one block: an exception event's when it is that event's evidence, else its chapter's."""
        lab = self._chapter_lab(bi, seq, fin)
        if not lab:
            return None
        if seq in lab["blocks"]:
            return lab["blocks"][seq]
        return lab["scope"] if seq >= lab["cue"] else None

    def event_scope(self, bi: int, ev: dict, fin: frozenset[str] | None = None) -> str | None:
        """An event's story time: its exception (a scope, OTHER, or None for a line past the caps), else its chapter's at its
        anchor."""
        if not self.story_time or ev.get("from") not in self.books[bi].seq_of:
            return None
        lab = self._chapter_lab(bi, self.books[bi].seq_of[ev["from"]], fin)
        if lab and ev["id"] in lab["events"]:
            return lab["events"][ev["id"]]
        return self.scope_of(bi, ev["from"], fin)

    def item_scope(self, bi: int, it: dict, fin: frozenset[str] | None = None) -> str | None:
        """The story time of an item (a facet, a profile, a turning point "event:<id>"): the one all its evidence blocks agree
        on, else None (mixed, other or unknown); an item citing nothing takes its anchor's."""
        if not self.story_time:
            return None
        b = self.books[bi]
        if str(it.get("id", "")).startswith("event:"):
            ev = b.events.get(it["id"][len("event:"):])
            s = self.event_scope(bi, ev, fin) if ev else None
            return None if s == OTHER else s
        seqs = {b.seq_of[a] for a in it.get("evidence", []) if a in b.seq_of}
        if not seqs:
            return self.scope_of(bi, it["from"], fin)
        got = {self._block_scope(bi, s, fin) for s in seqs}
        s = got.pop() if len(got) == 1 else None
        return None if s == OTHER else s

    def _presence(self, eid: str, bi: int, fin: frozenset[str] | None = None) -> list[tuple[tuple[int, int], str]]:
        """((book, ordinal), scope) of every item of the entity and every event naming it whose story time is known, over the
        books a reader of book bi knows, in reading order."""
        memo = (eid, bi, fin, self._tl_version)
        with self.lock:
            if memo in self._presence_memo:
                return self._presence_memo[memo]
        out = []
        for j in self._tl_books(bi, fin):
            b = self.books[j]
            if eid not in b.ents and not any(eid in ev.get("entities", []) for ev in b.doc.get("events", [])):
                continue
            for it in self.items(j, eid):
                if not it["id"].startswith("event:") and (s := self.item_scope(j, it, fin)):
                    out.append(((j, b.ord_of[it["from"]]), s))
            for ev in b.doc.get("events", []):
                if eid in ev.get("entities", []) and ev["from"] in b.ord_of:
                    s = self.event_scope(j, ev, fin)
                    if s and s != OTHER:
                        out.append(((j, b.ord_of[ev["from"]]), s))
        out.sort()
        with self.lock:
            self._presence_memo[memo] = out
        return out

    def scopes_of(self, eid: str, bi: int, anchor: str | None = None, fin: frozenset[str] | None = None) -> list[str]:
        """The entity's known story times at or before an anchor of book bi (None: the book's end), in order of first use."""
        if not self.story_time:
            return []
        b = self.books[bi]
        pos = (bi, b.ord_of[anchor]) if anchor in b.ord_of else (bi, len(b.anchors))
        return list(dict.fromkeys(s for p, s in self._presence(eid, bi, fin) if p <= pos))

    def multi(self, eid: str, bi: int, anchor: str | None = None, fin: frozenset[str] | None = None) -> bool:
        """Whether the entity is drawn per story time at an anchor: its items or events span two or more scopes at or before
        it. Per entity, so one flash-forward prologue changes nothing for the rest of the cast."""
        return len(self.scopes_of(eid, bi, anchor, fin)) >= 2

    def _second(self, eid: str, bi: int, fin: frozenset[str] | None = None) -> tuple[str | None, tuple[int, int] | None]:
        """(the entity's first scope, where its second scope begins) over books 0..bi."""
        first, at = None, None
        for p, s in self._presence(eid, bi, fin):
            if first is None:
                first = s
            elif s != first:
                at = p
                break
        return first, at

    def figure_scope(self, bi: int, eid: str, anchor: str, scene: str | None, fin: frozenset[str] | None = None) -> str | None:
        """How a figure is drawn in a picture whose story time is `scene`: None as before (one scope), a scope string (its note
        and references of that scope only), or PASSAGE (from the passage only: a scene told in no known line, or a figure with
        several scopes in a scene whose time is unknown)."""
        if not self.story_time:
            return None
        if scene == OTHER:
            return PASSAGE
        if not self.multi(eid, bi, anchor, fin):
            return None
        return scene or PASSAGE

    def _scene_scope(self, job: Job, fin: frozenset[str] | None = None) -> str | None:
        """A picture's story time: a portrait's own (the job's), an event's, a plate's main moment's, else the chapter's at its
        last anchor."""
        if not self.story_time:
            return None
        if job.kind in ENTITY_KINDS:
            return job.scope
        if job.event:
            return self.event_scope(job.bi, job.event, fin)
        return self.scope_of(job.bi, job.chapter["last"], fin) if job.chapter else None

    def _in_scope(self, bi: int, seqs, scope: str | None, fin: frozenset[str] | None = None) -> list[int]:
        """The blocks of `seqs` told in `scope` (all of them when scope is None)."""
        if scope is None:
            return list(seqs)
        return [s for s in seqs if self._block_scope(bi, s, fin) == scope]

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
        """`_key` of what a book's set already holds or will never draw (a final skip)."""
        return {_key(r) for r in self.man[bi]["pictures"] if not r.get("partial")} | \
            {(x.get("kind"), x["target"], x.get("from"), x.get("scope") if x.get("kind") == "portrait" else None)
             for x in self.man[bi]["skipped"]}

    def _is_new(self, bi: int, j: Job, have: set[tuple]) -> bool:
        sc = j.scope if j.kind == "portrait" else None
        return (j.kind, j.target, self._anchor(bi, j.anchor), sc) not in have and (j.kind, j.target, j.anchor, sc) not in have

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
        self.timeline(bi)                # the story time of every chapter, before the portraits are planned per scope
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
        ports = self._portrait_jobs(bi, classify=False) if self.portraits else []
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

    def main_cast(self, bi: int) -> list[str]:
        """The characters book bi is most about: people and creatures with a profile named by at least
        max(MAIN_CAST_MIN, ceil(MAIN_CAST_SHARE x the book's non-minor events)) of this book's non-minor events, at most
        MAIN_CAST_MAX of them, ranked by those events, then by turning points, then by first appearance."""
        b = self.books[bi]
        evs = [e for e in b.doc.get("events", []) if e.get("kind") != "minor"]
        named = Counter(x for e in evs for x in set(e.get("entities", [])))
        turns = Counter(x for e in evs if e.get("kind") == "turn" for x in set(e.get("entities", [])))
        need = max(MAIN_CAST_MIN, math.ceil(MAIN_CAST_SHARE * len(evs)))
        order = {e["id"]: i for i, e in enumerate(b.doc["entities"])}
        first = lambda e: (b.ord_of.get(e["from"], len(b.ord_of)), order[e["id"]])     # noqa: E731
        keep = [e for e in b.doc["entities"] if e["type"] in PORTRAIT_TYPES and named[e["id"]] >= need
                and self._profiles(bi, e["id"])]
        keep.sort(key=lambda e: (-named[e["id"]], -turns[e["id"]], first(e)))
        return [e["id"] for e in keep[:MAIN_CAST_MAX]]

    def _portrait_cast(self, bi: int) -> list[dict]:
        """Who gets a portrait in book bi (the selection, one step): under `portraits="main"` the main cast (`main_cast`),
        else every person and creature with a profile that has a portrait-stage profile or is named by two non-minor events."""
        b = self.books[bi]
        if self.portraits == "main":
            return [b.ents[x] for x in self.main_cast(bi)]
        named = Counter(x for e in b.doc.get("events", []) if e.get("kind") != "minor" for x in e.get("entities", []))
        out = []
        for e in b.doc["entities"]:
            if e["type"] not in PORTRAIT_TYPES:
                continue
            profs = self._profiles(bi, e["id"])
            written = [p for p in profs if p[0] == bi and ".portrait-" in p[2]["id"]]
            if profs and (written or named[e["id"]] >= 2):
                out.append(e)
        return out

    def _portrait_jobs(self, bi: int, classify: bool = True) -> list[Job]:
        """A portrait per selected person and creature (`_portrait_cast`) at its first look item (`look_index`), and a new one
        (the earlier kept) at each look change the look index records for one of its own items. An entity drawn in an earlier
        book is not drawn again without such a change. `classify` False plans from the look indexes already in the manifest and
        makes no call (an estimate: an entity not classified yet gets one portrait, at its first profile)."""
        cast = self._portrait_cast(bi)
        if classify:
            self.classify(bi, [e["id"] for e in cast])
        jobs = []
        for e in cast:
            jobs += self._versions(bi, e)
        return jobs

    def _versions(self, bi: int, e: dict) -> list[Job]:
        """The portraits of one entity in book bi, from the look index: the first at its first look item (else its first profile,
        whose note will then be empty unless the passages show a look), then one per look change, within MAX_VERSIONS and
        MIN_GAP blocks; versions are compared on the anchors they show from after the hold to the world note, so two never share
        one (the later stands for both)."""
        b, eid = self.books[bi], e["id"]
        if self.story_time and self.multi(eid, bi):
            return self._scoped_versions(bi, e)
        mine = [p for p in self._profiles(bi, eid) if p[0] == bi]
        written = [p for p in mine if ".portrait-" in p[2]["id"]]
        # only a drawn picture counts: one planned in an earlier book and then skipped does not
        before = any(r["kind"] == "portrait" and r["target"] == eid and not r.get("partial")
                     for j in range(bi) for r in self.man[j]["pictures"])
        idx = self.look_index(bi, eid)
        own = [(b.ord_of[it["from"]], it) for it in self.items(bi, eid)
               if it["from"] in b.ord_of and b.ord_of[it["from"]] >= b.ord_of.get(e["from"], 0)]
        own.sort(key=lambda t: t[0])
        looks = [it for _, it in own if idx.get(it["id"], {}).get("look") and not it["id"].startswith("event:")]
        # a change's reason is the item's own text (index content at its anchor, which the amended note may quote), never the
        # classifier's phrase
        changes = [(o, it["from"], it["text"]) for o, it in own if idx.get(it["id"], {}).get("change")]
        if before:
            start = None
        elif mine:
            start = looks[0]["from"] if looks else (written or mine)[0][2]["from"]
        else:
            return []
        versions: list[list] = [[start, "", self._anchor(bi, start)]] if start else []
        for o, a, why in changes:
            if start is not None and o <= b.ord_of[start]:
                continue                # told already where the first portrait shows from
            held = self._anchor(bi, a)
            if versions and versions[-1][2] == held:
                versions[-1] = [a, why if (len(versions) > 1 or before) else "", held]   # the later one stands for both
            elif len(versions) >= MAX_VERSIONS or (versions and b.seq_of[held] - b.seq_of[versions[-1][2]] < MIN_GAP):
                continue
            else:
                versions.append([a, why, held])
        return [Job("portrait", eid, bi, a, change=why, shape="portrait") for a, why, _ in versions]

    def _scoped_versions(self, bi: int, e: dict) -> list[Job]:
        """`_versions` for an entity drawn per story time: in each scope of this book a first portrait at its first look item
        told in that scope (else its first profile told in it, else its second event there: the note decides then, and a
        scope whose text shows no look is skipped), then one per look change told in that scope; MAX_VERSIONS, MIN_GAP and the
        merge of two versions on one anchor within a scope only, at most MAX_SCOPE_VERSIONS in all (the earliest). A scope
        drawn in an earlier book is not drawn again without a change in it."""
        b, eid = self.books[bi], e["id"]
        first, second = self._second(eid, bi)
        idx = self.look_index(bi, eid)
        lo = b.ord_of.get(e["from"], 0)
        own = sorted(((b.ord_of[it["from"]], it) for it in self.items(bi, eid) if it["from"] in b.ord_of
                      and b.ord_of[it["from"]] >= lo), key=lambda t: t[0])
        scoped = [(o, it, self.item_scope(bi, it)) for o, it in own]
        profiles = {p[2]["id"] for p in self._profiles(bi, eid) if p[0] == bi}
        evs = sorted((b.ord_of[ev["from"]], ev["from"], sc) for ev in b.doc.get("events", [])
                     if eid in ev.get("entities", []) and ev.get("kind") != "minor" and ev["from"] in b.ord_of
                     and (sc := self.event_scope(bi, ev)) not in (None, OTHER))
        order = list(dict.fromkeys([sc for _, _, sc in scoped if sc] + [sc for _, _, sc in evs]))
        order.sort(key=lambda sc: min([o for o, _, x in scoped if x == sc] + [o for o, _, x in evs if x == sc]))
        out: list[tuple[int, Job]] = []
        for sc in order:
            before = any(r["kind"] == "portrait" and r["target"] == eid and not r.get("partial") and (
                r.get("scope") == sc or ("scope" not in r and sc == first and (second is None or (j, self.books[j].ord_of.get(
                    r["from"], 1 << 30)) < second))) for j in range(bi) for r in self.man[j]["pictures"])
            looks = [it for _, it, x in scoped if x == sc and idx.get(it["id"], {}).get("look") and not it["id"].startswith("event:")]
            profs = [it for _, it, x in scoped if x == sc and it["id"] in profiles]
            seen = [a for _, a, x in evs if x == sc]
            changes = [(o, it["from"], it["text"]) for o, it, x in scoped if x == sc and idx.get(it["id"], {}).get("change")]
            if before:
                start = None
            elif looks or profs:
                start = (looks or profs)[0]["from"]
            elif len(seen) >= 2:
                start = seen[1]
            else:
                continue
            versions: list[list] = [[start, "", self._anchor(bi, start)]] if start else []
            for o, a, why in changes:
                if start is not None and o <= b.ord_of[start]:
                    continue
                held = self._anchor(bi, a)
                if versions and versions[-1][2] == held:
                    versions[-1] = [a, why if (len(versions) > 1 or before) else "", held]
                elif len(versions) >= MAX_VERSIONS or (versions and b.seq_of[held] - b.seq_of[versions[-1][2]] < MIN_GAP):
                    continue
                else:
                    versions.append([a, why, held])
            out += [(b.ord_of[held], Job("portrait", eid, bi, a, change=why, shape="portrait", scope=sc)) for a, why, held in versions]
        out.sort(key=lambda t: t[0])
        return [j for _, j in out[:MAX_SCOPE_VERSIONS]]

    # ---- the look index: which of an entity's items describe its look, and which change it

    def items(self, bi: int, eid: str) -> list[dict]:
        """The entity's own items in book bi, in reading order: its facets and profiles (never an arc) and the turning points that
        name it, each as {"id", "from", "text", "evidence"} (a turning point's id is "event:<id>")."""
        b = self.books[bi]
        out = [{"id": f["id"], "from": f["from"], "text": f["text"], "evidence": f.get("evidence", [])}
               for f in b.ents.get(eid, {}).get("facets", []) if f.get("kind") != "arc" and f["from"] in b.ord_of]
        out += [{"id": "event:" + ev["id"], "from": ev["from"], "text": ev["summary"], "evidence": ev.get("evidence", [])}
                for ev in b.doc.get("events", []) if ev.get("kind") == "turn" and eid in ev.get("entities", [])
                and ev["from"] in b.ord_of]
        return sorted(out, key=lambda it: b.ord_of[it["from"]])

    @staticmethod
    def _items_key(items: list[dict]) -> str:
        """What a look index was made from: a rebuilt index (other items, ids or words) is classified again."""
        return hashlib.sha256(json.dumps([[i["id"], i["text"]] for i in items], ensure_ascii=False).encode()).hexdigest()[:16]

    def look_index(self, bi: int, eid: str) -> dict[str, dict]:
        """{item id: {"look": bool, "change": bool}} for the entity's items in book bi (`items`), from a fresh classification in
        the manifest (`looks[eid] = {"key", "items"}`), else {} (not classified yet, or the index changed since).

        Gating: the classification call sees all the entity's items up to the end of the book, but it only chooses. Every use
        is filtered by anchor (a look item or a change counts only at or after its own `from`), and nothing it writes reaches a
        prompt (a change is stored as true or false, never its phrase; an amended note quotes the change item's own text).
        The manifest holds item ids and flags only, so it says nothing the index the Reader holds does not."""
        items = self.items(bi, eid)
        got = self.man[bi]["looks"].get(eid)
        if not items or not isinstance(got, dict) or got.get("key") != self._items_key(items):
            return {}
        ids = {i["id"] for i in items}
        return {k: v for k, v in (got.get("items") or {}).items() if k in ids}

    def classify(self, bi: int, eids: list[str]) -> None:
        """Classify the items of every entity in `eids` that has no fresh look index: one cheap call per batch of up to
        LOOK_BATCH_ITEMS items (small entities share a call, a large one takes several). Each item is numbered in the call and a
        reply's number is kept only when it exists, so a flag always belongs to that entity's own item. A call that fails twice
        leaves those entities unclassified (no look items: a first portrait at the first profile, no versions)."""
        with self._class_lock:
            b = self.books[bi]
            todo = []
            for eid in dict.fromkeys(eids):
                items = self.items(bi, eid)
                got = self.man[bi]["looks"].get(eid)
                if items and not (isinstance(got, dict) and got.get("key") == self._items_key(items)):
                    todo.append((eid, items))
            if not todo:
                return
            batches: list[list[tuple[str, dict]]] = [[]]
            for eid, items in todo:
                if batches[-1] and len(batches[-1]) + len(items) > LOOK_BATCH_ITEMS:
                    batches.append([])
                for it in items:
                    if len(batches[-1]) >= LOOK_BATCH_ITEMS:
                        batches.append([])
                    batches[-1].append((eid, it))
            found: dict[str, dict] = {eid: {} for eid, _ in todo}
            failed: set[str] = set()
            for batch in batches:
                lines, last = [], None
                for n, (eid, it) in enumerate(batch, 1):
                    if eid != last:
                        e = b.ents[eid]
                        lines.append(f"\n## {e['names'][0]['value']} ({P.KINDS.get(e['type'], e['type'])})")
                        last = eid
                    what = "(a turning point it is in) " if it["id"].startswith("event:") else ""
                    lines.append(f"{n}. {what}{' '.join(it['text'].split())[:LOOK_ITEM_CHARS]}")
                try:
                    out = self._complete(LOOK_INDEX.format(entries="\n".join(lines).strip()), LOOK_INDEX_SCHEMA)
                except TextFailed as e:
                    self.log(f"  look index: text model failed ({e.detail})")
                    failed |= {eid for eid, _ in batch}
                    continue
                for x in out.get("items") or [] if isinstance(out.get("items"), list) else []:
                    try:
                        n = int(x.get("n"))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if not 1 <= n <= len(batch):
                        continue
                    eid, it = batch[n - 1]
                    # only whether it changes: the phrase is not kept (the manifest is served whole, and the phrase may tell
                    # what happens late in the book; the amended note quotes the item's own text at its anchor)
                    change = bool(" ".join(str(x.get("change") or "").split()))
                    look = x.get("look") is True
                    if look or change:
                        found[eid][it["id"]] = {"look": look, "change": change}
            with self.lock:
                for eid, items in todo:
                    if eid not in failed:
                        self.man[bi]["looks"][eid] = {"key": self._items_key(items), "items": found[eid]}
                self._dirty.add(bi)
                self._save(bi)

    # ---- groups: which group a person belongs to at an anchor

    def _group_names(self, gid: str) -> list[str]:
        e = next((bb.ents[gid] for bb in self.books if gid in bb.ents), None)
        out = []
        for n in (e or {}).get("names", []):
            v = " ".join(re.sub(r"\(.*?\)", " ", n["value"]).split())
            v = re.sub(r"^(?:the|a|an)\s+", "", v, flags=re.IGNORECASE)
            if len(v) >= 3:
                out.append(v)
        return out

    @staticmethod
    def _names_in(names: list[str], text: str) -> bool:
        """Whether a text uses one of a group's names, as written (case kept, so "golden" is not "Gold"), plural or as the
        head of a hyphenated word ("Gold-class", "Golds")."""
        return any(re.search(r"(?<![\w-])" + re.escape(n) + r"(?:s|es)?(?![\w])", text) for n in names)

    def _group_candidates(self, bi: int, eid: str) -> list[str]:
        """The group entities of the run's books up to bi whose name one of the entity's own items uses, or that it has a tie of
        no kind to (an index older than tie kinds: the tie may be membership or enmity, so the pick decides)."""
        texts = [it["text"] for it in self.items(bi, eid)]
        tied = {r["dst"] if r["src"] == eid else r["src"] for j in range(bi + 1) for r in self.books[j].doc.get("relations", [])
                if eid in (r["src"], r["dst"]) and "kind" not in r}
        seen, out = set(), []
        for j in range(bi + 1):
            for e in self.books[j].doc["entities"]:
                if e["type"] == "group" and e["id"] not in seen and e["id"] != eid:
                    seen.add(e["id"])
                    names = self._group_names(e["id"])
                    if names and (e["id"] in tied or any(self._names_in(names, t) for t in texts)):
                        out.append(e["id"])
        return out[:26]

    def group_picks(self, bi: int, eid: str) -> list[dict]:
        """[{"group", "from", "until"}] for the entity in book bi: the groups among `_group_candidates` the model says it belongs
        to, with the anchors of the items where its membership starts and ends ("until" None when it never does). One cheap call
        per entity and book, cached in the manifest as `groups[eid] = {"key", "picks"}`; no candidate, no call. The call only
        chooses: a pick is kept only for a listed group whose name one of the entity's items uses, its anchors come from the
        entity's own items, and nothing it says reaches a prompt."""
        items = self.items(bi, eid)
        cands = self._group_candidates(bi, eid)
        if not items or not cands:
            return []
        key = hashlib.sha256(json.dumps([self._items_key(items), cands]).encode()).hexdigest()[:16]
        got = self.man[bi]["groups"].get(eid)
        if isinstance(got, dict) and got.get("key") == key:
            return list(got.get("picks") or [])
        e = self.books[bi].ents[eid]
        who = e["names"][0]["value"]
        letters = [chr(ord("A") + i) for i in range(len(cands))]
        groups = "\n".join(f"{ltr}. {self._group_names(g)[0]}" + (f" (also {', '.join(self._group_names(g)[1:3])})"
                                                                   if len(self._group_names(g)) > 1 else "")
                           for ltr, g in zip(letters, cands))
        lines = "\n".join(f"{n}. {' '.join(it['text'].split())[:LOOK_ITEM_CHARS]}" for n, it in enumerate(items, 1))
        try:
            out = self._complete(GROUP_PICK.format(who=who, items=lines, groups=groups), GROUP_SCHEMA)
        except TextFailed as e2:
            self.log(f"  groups of {eid}: text model failed ({e2.detail})")
            return []
        picks: list[dict] = []
        b = self.books[bi]
        for x in out.get("groups") or [] if isinstance(out.get("groups"), list) else []:
            if not isinstance(x, dict):
                continue
            ltr = str(x.get("group") or "").strip().strip(".").upper()[:1]
            if ltr not in letters:
                continue
            gid = cands[letters.index(ltr)]
            names = self._group_names(gid)
            uses = [it for it in items if self._names_in(names, it["text"])]
            tied = any(eid in (r["src"], r["dst"]) and gid in (r["src"], r["dst"]) for r in b.doc.get("relations", []))
            if not (uses or tied) or any(p["group"] == gid for p in picks):
                continue
            try:
                f, u = int(x.get("from") or 0), int(x.get("until") or 0)
            except (TypeError, ValueError):
                f, u = 0, 0
            first = min((it["from"] for it in uses), key=b.ord_of.get) if uses else \
                min((r["from"] for r in b.doc.get("relations", []) if eid in (r["src"], r["dst"]) and gid in (r["src"], r["dst"])
                     and r["from"] in b.ord_of), key=b.ord_of.get, default=items[0]["from"])
            # membership never starts before its evidence (the first item naming the group, or the tie): the model's item counts
            # only when it is later, and an end only when it is after the start
            start = first
            if 1 <= f <= len(items) and b.ord_of[items[f - 1]["from"]] > b.ord_of[first]:
                start = items[f - 1]["from"]
            until = items[u - 1]["from"] if 1 <= u <= len(items) and b.ord_of[items[u - 1]["from"]] > b.ord_of[start] else None
            picks.append({"group": gid, "from": start, "until": until})
        with self.lock:
            self.man[bi]["groups"][eid] = {"key": key, "picks": picks}
            self._dirty.add(bi)
            self._save(bi)
        return picks

    def _member_count(self, bi: int, gid: str, anchor: str) -> int:
        """How many people and creatures of book bi the group has at an anchor, as far as the index shows: those with an item at
        or before it that uses its name (the measure of "the most specific group first"; never counted from unread text)."""
        with self.lock:
            firsts = self._members.get(bi)
        b = self.books[bi]
        if firsts is None:
            firsts = {}
            groups = {e["id"]: self._group_names(e["id"]) for bb in self.books[:bi + 1] for e in bb.doc["entities"] if e["type"] == "group"}
            for e in b.doc["entities"]:
                if e["type"] in PORTRAIT_TYPES:
                    items = self.items(bi, e["id"])
                    for g, names in groups.items():
                        at = next((b.ord_of[it["from"]] for it in items if names and self._names_in(names, it["text"])), None)
                        if at is not None:
                            firsts.setdefault(g, []).append(at)
            firsts = {g: sorted(v) for g, v in firsts.items()}
            with self.lock:
                self._members[bi] = firsts
        return bisect_right(firsts.get(gid, []), b.ord_of[anchor])

    def groups_of(self, bi: int, eid: str, anchor: str, finished: frozenset[str] | None = None) -> list[str]:
        """The groups an entity belongs to at an anchor, most specific first (fewest members): groups visible there that it has
        a tie of kind family, alliance or service to, and the model's picks (`group_picks`) whose membership began
        at or before the anchor and has not ended by it."""
        b = self.books[bi]
        view = self.view(bi, anchor, self._finished(bi, finished))
        vis = {x["id"]: x for x in view["entities"] if x.get("type") == "group"}
        out = []
        for r in view["relations"]:
            if eid in (r["src"], r["dst"]) and r.get("kind") in ("family", "alliance", "service"):
                other = r["dst"] if r["src"] == eid else r["src"]
                if other in vis and other not in out:
                    out.append(other)
        at = b.ord_of[anchor]
        if eid in b.ents:
            for p in self.group_picks(bi, eid):
                if p["group"] in vis and p["group"] not in out and b.ord_of.get(p["from"], 1 << 30) <= at \
                        and (p.get("until") is None or b.ord_of.get(p["until"], 1 << 30) > at):
                    out.append(p["group"])
        order = {g: n for n, g in enumerate(out)}
        return sorted(out, key=lambda g: (self._member_count(bi, g, anchor), order[g]))

    # ---- notes, pictures and references (a later book reads the earlier books' manifests)

    def _pos(self, bi: int, anchor: str) -> tuple[int, int]:
        return bi, self.books[bi].ord_of[anchor]

    def latest_note(self, eid: str, pos: tuple[int, int], finished: frozenset[str] | None = None,
                    scope: str | None = None) -> tuple[int, dict] | None:
        """(book, note) of the entity's latest appearance note at or before `pos`, among those a reader who finished `finished`
        may be shown (`_usable`). A note of another version (`v`, NOTE_VERSION) is never used: it is written again. With a
        `scope` (an entity drawn per story time) only a note of that scope counts, and a note with no scope (written while the
        entity had one) only for its first scope and only if it comes before its second scope begins."""
        best = None
        first, second = self._second(eid, pos[0], self._finished(pos[0], finished)) if scope else (None, None)
        for j in range(pos[0] + 1):
            for n in self.man[j]["appearance"].get(eid, []):
                if n.get("v") != NOTE_VERSION:
                    continue
                k = (j, self.books[j].ord_of.get(n["from"], -1))
                if scope and n.get("scope") != scope and not (
                        "scope" not in n and scope == first and (second is None or k < second)):
                    continue
                if k[1] >= 0 and k <= pos and (best is None or k >= best[0]) and self._usable(j, pos[0], finished, n):
                    best = (k, j, n)
        return (best[1], best[2]) if best else None

    def latest_picture(self, eid: str, pos: tuple[int, int], kinds=ENTITY_KINDS, strict: bool = False,
                       finished: frozenset[str] | None = None, current: bool = False, scope: str | None = None,
                       other: str | None = None) -> tuple[int, dict] | None:
        """(book, record) of the entity's latest picture at or (strict) before `pos`, among those `finished` allows; `current`:
        only one drawn from a note of this version (a reference for a new picture); `scope`: only one of that story time (a
        record with no scope never); `other`: only one of another story time than this."""
        best = None
        for j in range(pos[0] + 1):
            for r in self.man[j]["pictures"]:
                k = (j, self.books[j].ord_of.get(r["from"], -1))
                if r["kind"] in kinds and r["target"] == eid and 0 <= k[1] and (k < pos if strict else k <= pos) \
                        and (not current or r.get("notes") == NOTE_VERSION) \
                        and (scope is None or r.get("scope") == scope) and (other is None or r.get("scope") != other) \
                        and (best is None or k >= best[0]) and self._usable(j, pos[0], finished, r):
                    best = (k, j, r)
        return (best[1], best[2]) if best else None

    def ensure_note(self, bi: int, eid: str, anchor: str, change: str = "",
                    finished: frozenset[str] | None = None, scope: str | None = None) -> tuple[str | None, str, list[str], list[str]]:
        """The entity's appearance note at `anchor`: the latest earlier one; *filled in* (new quoted details added, nothing
        dropped) when the look index has look items of it at or before the anchor that the note was not written from; amended
        ("the same man...", a superseded detail replaced, quoting the change) when `change` says its look changed there; written
        now when it has none. Returns (note, reason when None, anchors of this book it was written from, the earlier book keys
        its text comes from), and stores a new note in the manifest. For a reader who has not finished every earlier book
        (`finished`) only notes from books they finished are used, and a note written for them is stored as `partial` (never
        used for anyone else). A group's note is the look of its members, plus `home` details: where and how they live.

        A note holds only look details the writer quotes from the entity's own text (`detail_supported`, `quoted`); the others
        are dropped ("dropped: look detail not in the text"), and a note with no detail left is empty: the text shows no look,
        so the entity gets no portrait. An empty note is remembered and not asked again until a look change or a new look
        item.

        With a `scope` (an entity drawn per story time) the note is that scope's: found among that scope's notes, and written,
        filled in or amended only from items whose evidence all lies in it and from paragraphs that show the entity at that
        time (`shows_at`): no profile (the index's profiles blend the times), no item with mixed or other evidence, no other
        scope's note; a new era starts a fresh note."""
        got = self._note(bi, eid, anchor, change, finished, scope)
        return (got["text"], "", got["sources"], got["inherits"]) if got.get("text") else (None, got.get("why", "empty"), [], [])

    def _facet_scope(self, bi: int, eid: str, f: dict, fin: frozenset[str] | None = None) -> str | None:
        """The story time of a facet as the view shows it (it carries its own book's key in a series)."""
        j = next((x for x in range(bi + 1) if self._bkey(x) == str(f.get("book"))), bi) if f.get("book") is not None else bi
        own = next((x for x in self.books[j].ents.get(eid, {}).get("facets", []) if x["id"] == f["id"]), None)
        return self.item_scope(j, own, fin) if own and own["from"] in self.books[j].ord_of else None

    def _note(self, bi: int, eid: str, anchor: str, change: str = "", finished: frozenset[str] | None = None,
              scope: str | None = None) -> dict:
        """`ensure_note`'s work, returning the stored note itself with "sources" and "inherits" for this book, or {"why": ...}."""
        b = self.books[bi]
        me = self._bkey(bi)
        finished = self._finished(bi, finished)
        pos = self._pos(bi, anchor)
        with self.lock:
            lock = self._entity_locks.setdefault(eid, threading.Lock())
        with lock:
            hit = self.latest_note(eid, pos, finished, scope)
            cur = hit[1] if hit else None
            cur_books = sorted(set(cur.get("inherits", [])) | ({self._bkey(hit[0])} if hit[0] != bi else set())) if cur else []
            if eid in b.ents:
                self.classify(bi, [eid])
            idx = self.look_index(bi, eid)
            at = b.ord_of[anchor]
            looks = [it for it in self.items(bi, eid) if idx.get(it["id"], {}).get("look") and b.ord_of[it["from"]] <= at
                     and not it["id"].startswith("event:")    # a turning point counts only as a change of its own look
                     and (scope is None or self.item_scope(bi, it, finished) == scope)]
            shows: dict[int, str | None] = {}
            if scope:
                # no profile: the index writes each one from passages across the book so far and from the profile before it,
                # so its words blend the times even when the paragraphs it cites are of one
                profiles = {f["id"] for f in b.ents.get(eid, {}).get("facets", []) if f.get("kind") == "profile"}
                looks = [it for it in looks if it["id"] not in profiles]
                # the paragraphs a scope's note may be written from: each checked for the time it shows the entity at
                upto0 = b.seq_of[anchor]
                ev = {b.seq_of[a] for it in looks for a in it["evidence"] if a in b.seq_of}
                shows = self.shows_at(bi, eid, ev, upto0, finished)
                looks = [it for it in looks if all(shows.get(b.seq_of[a], scope) == scope for a in it["evidence"] if a in b.seq_of)]
            looks.sort(key=lambda it: b.ord_of[it["from"]])
            done = set(cur.get("items", [])) if cur and hit[0] == bi else set()
            new = [it for it in looks if it["id"] not in done]

            def back(n: dict) -> dict:
                return {**n, "sources": n.get("sources", []) if hit[0] == bi else [], "inherits": [k for k in cur_books if k != me]}
            if cur and not cur.get("text") and not cur.get("home"):
                if not change and not new:
                    return {"why": "empty"}   # the text shows no look: remembered, not asked again until it does
                cur, cur_books = None, []     # nothing to fill in or amend: the note is written afresh
            if cur and ((hit[0] == bi and cur["from"] == anchor) or (not change and not new)):
                return back(cur)
            mode = "write" if cur is None else ("amend" if change else "fill")
            view = self.view(bi, anchor, finished)
            ve = next((x for x in view["entities"] if x["id"] == eid), None)
            e = b.ents.get(eid) or next((bb.ents[eid] for bb in self.books if eid in bb.ents), None)
            if ve is None or e is None:
                return {"why": "not visible"}
            upto = b.seq_of[anchor]
            # the paragraphs the look items cite first (the newest first for a fill), then other paragraphs naming it
            pick = (new if mode == "fill" else looks)[::-1]
            # the paragraphs before its first name that describe it as it enters come first (a book often describes a newcomer
            # before naming them), then those its look items cite
            look_seqs: list[int] = self._in_scope(bi, self._introduction(bi, eid, upto), scope, finished) \
                if e["type"] in PORTRAIT_TYPES and mode == "write" else []
            facets = [f for f in ([ve["profile"]] if ve["profile"] and scope is None else []) + ve["facets"]
                      if scope is None or self._facet_scope(bi, eid, f, finished) == scope]
            if scope:
                pool = self._mentions(bi, eid, upto, 24, scope, finished)
                fseqs = {b.seq_of[a] for f in facets for x in self.books[bi].ents.get(eid, {}).get("facets", []) if x["id"] == f["id"]
                         for a in x.get("evidence", []) if a in b.seq_of}
                shows.update(self.shows_at(bi, eid, set(look_seqs) | set(pool) | fseqs, upto, finished))
                look_seqs = [x for x in look_seqs if shows.get(x, scope) == scope]
                facets = [f for f in facets if all(shows.get(b.seq_of[a], scope) == scope for x in b.ents.get(eid, {}).get("facets", [])
                                                   if x["id"] == f["id"] for a in x.get("evidence", []) if a in b.seq_of)]
            for it in pick:
                for a in it["evidence"]:
                    if a in b.seq_of and b.seq_of[a] <= upto and b.seq_of[a] not in look_seqs:
                        look_seqs.append(b.seq_of[a])
            lp, lused = self._passage_first(bi, look_seqs, upto, LOOK_PASSAGE_WORDS)
            rest = [s for s in (P._sample([x for x in pool if shows.get(x, scope) == scope], 10) if scope else
                                self._mentions(bi, eid, upto, 10, scope, finished)) if s not in set(lused)]
            left = max(150, NOTE_PASSAGE_WORDS - sum(_words(b.text[s]) for s in lused))
            mp, mused = self.passage(bi, rest, upto, left) if rest else ("", [])
            passages = "\n…\n".join(x for x in (lp, mp) if x)
            used = lused + mused
            # the look items themselves (the latest first, within NOTE_ITEMS_CHARS), then the current profile and facets
            shown_items, size = [], 0
            for it in pick:
                if shown_items and size + len(it["text"]) > NOTE_ITEMS_CHARS:
                    break          # the newest one is always sent; the ones left out stay for a later fill
                shown_items.append(it)
                size += len(it["text"])
            listed = {it["id"] for it in shown_items}
            shown = [f for f in facets[:9 if ve["profile"] and scope is None else 8] if f["id"] not in listed]
            own_from = {f["id"]: f["from"] for f in b.ents.get(eid, {}).get("facets", []) if f["from"] in b.ord_of}
            srcs = {b.anchor_at(s) for s in used} | {it["from"] for it in shown_items} | {own_from[f["id"]] for f in shown if f["id"] in own_from}
            inherits = sorted((set(cur_books) | {str(f["book"]) for f in shown if f.get("book") not in (None, me)}) - {me})
            kind = P.KINDS.get(e["type"], e["type"])
            prev_details = list(cur.get("details", [])) if cur else []
            if mode == "amend":
                prev = AMEND.format(prev="\n".join(f"{n}. {d['text']}" for n, d in enumerate(prev_details, 1)), change=change)
            elif mode == "fill":
                prev = FILL.format(prev=cur["text"])
            else:
                prev = ""
            notes = "\n".join(f"- {x['text']}" for x in shown_items + shown)
            prompt = APPEAR.format(who=ve["name"], kind=kind, looks=LOOKS.get(e["type"], "its look"), more="",
                                   home=HOME if e["type"] == "group" else "", prev=prev,
                                   notes=notes or "(nothing yet)", passages=passages or "(no passage names it)")
            prev_text = [cur["text"]] + ([change] if change else []) if cur else []
            source = "\n".join([passages, notes] + prev_text)
            # a quote must come from the entity's own text: its notes, the earlier note and change, and the paragraphs that name it
            # or describe it, never the paragraph either side that only gives them context (it often describes someone else)
            core = sorted((set(look_seqs) | set(rest)) & set(used))
            own = "\n".join([b.text[s] for s in core] + [notes] + prev_text)
            # what an amend may quote to replace a detail: the change item's own text and the paragraphs it cites, nothing else
            citem = next((it for it in reversed(self.items(bi, eid)) if it["text"] == change and b.ord_of[it["from"]] <= at), None)
            cseqs = [b.seq_of[a] for a in (citem or {}).get("evidence", []) if a in b.seq_of and b.seq_of[a] <= upto]
            change_src = "\n".join([change] + [b.text[s] for s in cseqs if s in b.text]) if mode == "amend" else ""
            got, why, details, home, role = self._write_note(prompt, bi, anchor, source, ve["name"], mode, eid, own,
                                                       prev_details, list(cur.get("home", [])) if cur else [], change_src,
                                                       str(cur.get("role", "")) if cur else "")
            sources = sorted(srcs | (set(cur.get("sources", [])) if cur and hit[0] == bi else set()), key=b.ord_of.get)
            if got is None and why != "empty":
                return {"why": why}
            note = {"v": NOTE_VERSION, "from": anchor, "text": got or "", "role": role, "details": details,
                    "sources": sources, "inherits": inherits, "items": sorted(done | {it["id"] for it in shown_items}),
                    "changed": (list(cur.get("changed", [])) if cur else []) + ([[me, anchor]] if mode == "amend" and got else [])}
            if e["type"] == "group":
                note["home"] = home
            if scope:
                note["scope"] = scope
            if finished is not None:
                note["partial"] = True
            with self.lock:
                self.man[bi]["appearance"].setdefault(eid, []).append(note)
                self._dirty.add(bi)
                self._save(bi)
            return {**note, "inherits": inherits} if (got or home) else {"why": "empty"}

    def _introduction(self, bi: int, eid: str, upto: int) -> list[int]:
        """The blocks among the INTRO_PARAS before the entity's first named paragraph (in its chapter, never past block `upto`)
        that describe how it looks, as one cheap call picks them (`INTRO`). The call only chooses: a number that is not one of
        the paragraphs sent is ignored, and a note may still quote only what those paragraphs say. Cached in the manifest as
        `intro[eid] = {"key", "paragraphs"}` (block numbers only); a failed call picks none and is asked again next time."""
        b = self.books[bi]
        hits = b.hits(eid)
        if not hits or hits[0][0] > upto:
            return []
        first = hits[0][0]
        ch = next((c for c in b.chapters if c["lo"] <= first <= c["hi"]), None)
        window = [x for x in range(max(ch["lo"] if ch else b.lo, first - INTRO_PARAS), first) if x in b.text]
        if not window:
            return []
        key = f"{window[0]}-{first}"
        got = self.man[bi].setdefault("intro", {}).get(eid)
        if isinstance(got, dict) and got.get("key") == key:
            return [x for x in got.get("paragraphs", []) if x in window]
        who = (b.ents.get(eid) or {}).get("names", [{"value": eid}])[0]["value"]
        paras = "\n\n".join(f"{n}. {b.text[x]}" for n, x in enumerate(window, 1))
        try:
            out = self._complete(INTRO.format(who=who, paras=paras, first=b.text[first]), INTRO_SCHEMA)
        except TextFailed as e:
            self.log(f"  introduction of {eid}: text model failed ({e.detail})")
            return []
        chosen = []
        for x in out.get("paragraphs") or [] if isinstance(out.get("paragraphs"), list) else []:
            try:
                n = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(window) and window[n - 1] not in chosen:
                chosen.append(window[n - 1])
        with self.lock:
            self.man[bi]["intro"][eid] = {"key": key, "paragraphs": sorted(chosen)}
            self._dirty.add(bi)
            self._save(bi)
        return sorted(chosen)

    def _passage_first(self, bi: int, seqs: list[int], upto: int, words: int) -> tuple[str, list[int]]:
        """The paragraphs at `seqs` in that order of priority (the first ones kept when the budget runs out), each with one
        paragraph of context when there is room, printed in book order."""
        b = self.books[bi]
        keep, n = [], 0
        for s in seqs:
            w = _words(b.text.get(s, ""))
            if s in b.text and s <= upto and (not keep or n + w <= words):
                keep.append(s)
                n += w
        if not keep:
            return "", []
        return self.passage(bi, keep, upto, words)

    def _write_note(self, prompt: str, bi: int, anchor: str, source: str, who: str, mode: str, eid: str, own: str,
                    prev: list[dict], prev_home: list[dict], change_src: str = "",
                    prev_role: str = "") -> tuple[str | None, str, list[dict], list[dict], str]:
        """Ask for a note's role and quoted details, keep the details the writer marks as seen whose quote is a phrase of `own`
        (the entity's own text: its notes and the paragraphs naming or describing it) and holds every word of the detail (one
        rewrite naming the others), and put the note together. `mode` "write", "fill" (the earlier details `prev` all kept,
        new ones added) or "amend" (an earlier detail dropped only when the reply says the change supersedes it and a new detail
        quotes the change, `change_src`). The role keeps only words the entity's own text has, and every detail the role or an
        earlier detail already says goes (`compose_note`). (text, "", details, home, role) or ("", "empty", [], home, role) when
        no detail holds, or (None, reason, [], [], "") when the note fails a check."""
        def kept(out: dict) -> tuple[list[dict], list[str], list[str], list[dict], list[str]]:
            good, bad, not_look, home, reworded = [], [], [], [], []
            for field_, into in (("details", good), ("home", home)):
                for d in out.get(field_) or [] if isinstance(out.get(field_), list) else []:
                    if not isinstance(d, dict) or not str(d.get("text") or "").strip():
                        continue
                    t, q = " ".join(str(d["text"]).split()), " ".join(str(d.get("quote") or "").split())
                    if d.get("seen") is not True:      # a detail the writer does not call seen is no look
                        not_look.append(t)
                        continue
                    if quoted(q, own, one=True) and detail_supported(t, q):
                        into.append({"text": _plain(t, own), "quote": q})
                    elif quoted(q, own, one=True) and len(_toks(q)) <= OWN_WORDS and _beyond(t, q) and not spoken(q, own):
                        # the writer's words go one word beyond its quote ("big, beefy build" on "A big, beefy man", "tall build"
                        # on "tall"): the quote is the book's own short phrase about it, so it becomes the detail (never a line
                        # someone speaks)
                        into.append({"text": _plain(re.sub(r"(?i)^the\s+", "", q.strip(" ,.;:")), own), "quote": q})
                        reworded.append(t)
                    else:
                        bad.append(t)
            return good, bad, not_look, home, reworded

        out = self._complete(prompt, NOTE_SCHEMA)
        good, bad, not_look, home, reworded = kept(out)
        if bad:
            again = self._complete(prompt + NOTE_AGAIN.format(who=who, bad="; ".join(bad)[:500]), NOTE_SCHEMA)
            got2 = kept(again)
            if len(got2[0]) + len(got2[3]) >= len(good) + len(home):      # the reply kept is the one whose drops are counted
                out, (good, bad, not_look, home, reworded) = again, got2
        with self.lock:
            for t in bad:
                self.report.dropped["dropped: look detail not in the text"] += 1
                self.log(f"    note for {eid}: dropped a look detail not in the text ({t[:80]})")
            for t in reworded:
                self.report.dropped["fixed: look detail in the book's own words"] += 1
            for t in not_look:
                self.report.dropped["dropped: not a look detail"] += 1
                self.log(f"    note for {eid}: dropped a detail that is no look ({t[:80]})")
        details = list(prev) if mode != "write" else []
        if mode == "amend" and good:
            drops = set()
            for x in out.get("drop") or [] if isinstance(out.get("drop"), list) else []:
                try:
                    drops.add(int(x))
                except (TypeError, ValueError):
                    continue
            if drops and any(quoted(d["quote"], change_src) for d in good):
                details = [d for n, d in enumerate(details, 1) if n not in drops]
                with self.lock:
                    self.report.dropped["replaced: look detail the change supersedes"] += len(prev) - len(details)
        have = {d["text"].lower() for d in details}
        # a note keeps what it had and takes new details while it has room (MAX_DETAILS; MAX_HOME for a group's homes)
        details += [d for d in good if d["text"].lower() not in have][:max(0, MAX_DETAILS - len(details))]
        homes = list(prev_home) + [d for d in home if d["text"].lower() not in {h["text"].lower() for h in prev_home}]
        homes = homes[:max(MAX_HOME, len(prev_home))]
        role = str(out.get("role") or "") if mode == "write" or not prev_role else prev_role
        if mode == "write" or not prev_role:
            # the role is who it is, not how it looks: its first clause only ("a miller with white hair" is "a miller"), and the
            # words the writer itself marks as look ("role_look") go, each one only if the role has it
            role = re.split(r",|;|:|\b(?:with|wearing|who|whose|in)\b", role, maxsplit=1)[0]
            for w in out.get("role_look") or [] if isinstance(out.get("role_look"), list) else []:
                w = " ".join(str(w).split())
                if w and re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", role, re.IGNORECASE):
                    role = _drop_phrase(role, w)
                    with self.lock:
                        self.report.dropped["dropped: look word in the role"] += 1
        mine = set(_toks(own))
        role = " ".join(w for w in role.split() if not _toks(w) or all(x in _STOP or _has(mine, x) for x in _toks(w)))
        names = self.names[bi]
        # a group's name goes from the role with nothing put in ("A first-year Gryffindor": "A first-year"); in a detail one
        # that is an ordinary word stays as that word ("Red Sigils": "red sigils"), another goes ("a huge Slytherin serpent":
        # "a huge serpent"); a detail left with no word goes. Then person and creature names are stripped as everywhere.
        role = names.drop_groups(role)
        details = [{**d, "text": t} for d in details if _content(t := names.drop_groups(d["text"], words=True))]
        homes = [{**d, "text": t} for d in homes if _content(t := names.drop_groups(d["text"], words=True))]
        # a title, "and" or "of" a stripped name leaves at the end of the role goes too ("Only son of Mr. and": "Only son")
        bare = names.strip(role, attributive=True)
        text = compose_note(_tidy(bare, titles=True) if bare != role else bare, [names.strip(d["text"]) for d in details],
                            mode == "amend" and bool(good))
        if not text:
            return "", "empty", [], homes, role
        got, why = self._vet(text, bi, anchor, source, NOTE_WORDS)
        for _ in range(8):
            # a word the book has not used by this point (the index's own spelling, a later name): the details and role words
            # carrying it go, not the whole note; a note left with no detail is empty
            if got is not None or not why.startswith("unknown name: "):
                break
            word = why[len("unknown name: "):].strip()
            has = lambda t: re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", t) is not None   # noqa: E731
            details, homes = [d for d in details if not has(d["text"])], [d for d in homes if not has(d["text"])]
            role = " ".join(w for w in role.split() if not has(w))
            with self.lock:
                self.report.dropped["dropped: look detail with an unknown name"] += 1
            text = compose_note(role, [d["text"] for d in details], mode == "amend" and bool(good))
            if not text:
                return "", "empty", [], homes, role
            got, why = self._vet(text, bi, anchor, source, NOTE_WORDS)
        if got is None and why.startswith("name in the text"):
            got, why = self._vet(self.names[bi].strip(text), bi, anchor, source, NOTE_WORDS)
        if got is None:
            return None, why, [], [], ""
        return got, "", details, homes, role

    def figure_note(self, bi: int, eid: str, anchor: str, change: str = "",
                    finished: frozenset[str] | None = None, scope: str | None = None) -> tuple[str | None, str, list[str], list[str]]:
        """The note a picture carries for a figure: its own appearance note, then, for a person or creature, the look of the
        members of up to MAX_GROUPS of its groups at the anchor (`groups_of`, the most specific first). A group takes a slot only
        when its member look adds something the figure's own note and the earlier groups do not already say (only the new
        details are carried), and never when the figure's own look was amended by a change after it joined. No group name. Its own quoted look comes first and wins where the two disagree (the writer and the
        image model are told so by the order). (text, reason when None, sources, inherits), as `ensure_note`.

        With a `scope` the figure's own note is that scope's, and a group's look counts only from the same story time: the
        group's note of that scope when it is drawn per scope too, else only a group whose one known scope is this one."""
        own = self._note(bi, eid, anchor, change, finished, scope)
        if not own.get("text"):
            return None, own.get("why", "empty"), [], []
        note, srcs, books = own["text"], list(own["sources"]), list(own["inherits"])
        e = next((bb.ents[eid] for bb in self.books if eid in bb.ents), {})
        if e.get("type") not in PORTRAIT_TYPES or eid not in self.books[bi].ents:
            return note, "", srcs, books
        b = self.books[bi]
        fin = self._finished(bi, finished)
        # where its own note was amended by a look change, as (book, ordinal): this book's, and an earlier book's the reader
        # finished (one from an unfinished book is not visible, so it does not count)
        keys = {self._bkey(j): j for j in range(bi + 1)}
        changed = [(keys[k], self.books[keys[k]].ord_of[a]) for k, a in own.get("changed", [])
                   if k in keys and a in self.books[keys[k]].ord_of and (keys[k] == bi or fin is None or k in fin)]
        parts, said, found = [note], _content(note), 0
        for gid in self.groups_of(bi, eid, anchor, finished)[:GROUPS_TRIED]:
            since = self._member_since(bi, eid, gid, fin) if changed else None
            if since is not None and any(c > since for c in changed):
                continue      # its own look changed after it joined (a Red carved into a Gold): its own look wins
            gs = self._group_scope(bi, gid, anchor, scope, fin)
            if gs == PASSAGE:
                continue      # the group's look at another time, or at no known one
            g = self._note(bi, gid, anchor, "", finished, gs)
            new = [d["text"] for d in g.get("details") or [] if not _within(_content(d["text"]), said)]
            if not new:
                continue      # no member look, or nothing the figure's note and the earlier groups do not already say
            parts.append(group_look(new))
            said |= set().union(*(_content(x) for x in new))
            srcs += g["sources"]
            books += g["inherits"]
            found += 1
            if found >= MAX_GROUPS:
                break
        return " ".join(p.rstrip(".") + "." for p in parts), "", sorted(set(srcs), key=b.ord_of.get), sorted(set(books))

    def _group_scope(self, bi: int, gid: str, anchor: str, scope: str | None, fin: frozenset[str] | None) -> str | None:
        """Which of a group's notes a figure drawn in `scope` may carry: None (the group's only note) for a figure with one
        scope; for one drawn per scope, the group's note of that scope, or its only note when its one known scope is the same;
        PASSAGE when none fits."""
        if scope is None:
            return None
        if self.multi(gid, bi, anchor, fin):
            return scope
        return None if self.scopes_of(gid, bi, anchor, fin) == [scope] else PASSAGE

    def _member_since(self, bi: int, eid: str, gid: str, finished: frozenset[str] | None = None) -> tuple[int, int] | None:
        """(book, ordinal) where the entity's membership of a group begins, over books 0..bi (an earlier book only when the
        reader finished it): the earliest of its ties to the group and the group pick's start."""
        out = []
        for j in range(bi + 1):
            if j < bi and finished is not None and self._bkey(j) not in finished:
                continue
            b = self.books[j]
            froms = [r["from"] for r in b.doc.get("relations", []) if {r["src"], r["dst"]} == {eid, gid} and r["from"] in b.ord_of]
            if eid in b.ents:
                froms += [p["from"] for p in self.group_picks(j, eid) if p["group"] == gid and p["from"] in b.ord_of]
            out += [(j, b.ord_of[a]) for a in froms]
        return min(out) if out else None

    def milieu(self, bi: int, eid: str, anchor: str, finished: frozenset[str] | None = None,
               scope: str | None = None) -> tuple[str, list[str], list[str]]:
        """(text, sources, inherits) of the surroundings a figure belongs to at an anchor, from the index's structure only:
        (a) the setting its visible events of this book are most often set in, the latest chapters weighing most (a chapter's
        events count 1, the one before 1/2, then 1/3...), as that place's appearance note when the setting links a place entity
        and it has a note, else as the setting's own words; (b) else where the members of its groups live (a group note's
        `home` details); (c) else "" (a portrait then gets a plain, neutral background, never the opening's). With a `scope`
        only events told in it count, and only groups and places whose look is of that time."""
        b = self.books[bi]
        at = b.ord_of[anchor]
        fin = self._finished(bi, finished)
        evs = [ev for ev in b.doc.get("events", []) if eid in ev.get("entities", []) and ev.get("setting")
               and ev["from"] in b.ord_of and b.ord_of[ev["from"]] <= at
               and (scope is None or self.event_scope(bi, ev, fin) == scope)]
        if evs:
            chapters = []
            for ev in evs:
                c = (b.chapter_of(ev["from"]) or {}).get("title", "")
                if c not in chapters:
                    chapters.append(c)
            score: Counter = Counter()
            latest: dict[str, int] = {}
            text_of: dict[str, dict] = {}
            for ev in evs:
                st = ev["setting"]
                k = st.get("place") or " ".join(str(st.get("text", "")).lower().split())
                if not k:
                    continue
                c = (b.chapter_of(ev["from"]) or {}).get("title", "")
                score[k] += 1 / (len(chapters) - chapters.index(c))
                latest[k] = max(latest.get(k, -1), b.ord_of[ev["from"]])
                text_of[k] = st
            if score:
                k = max(score, key=lambda x: (score[x], latest[x]))
                srcs = [ev["from"] for ev in evs if (ev["setting"].get("place") or " ".join(str(ev["setting"].get("text", "")).lower().split())) == k]
                place = text_of[k].get("place")
                vis = {x["id"] for x in self.view(bi, anchor, self._finished(bi, finished))["entities"]}
                ps = self._group_scope(bi, place, anchor, scope, fin) if place else None
                if place and place in vis and ps != PASSAGE:
                    note, _, nsrcs, books = self.ensure_note(bi, place, anchor, "", finished, ps)
                    if note:
                        return note, sorted(set(srcs) | set(nsrcs), key=b.ord_of.get), books
                return " ".join(str(text_of[k].get("text", "")).split()).rstrip(".") + ".", sorted(set(srcs), key=b.ord_of.get), []
        e = b.ents.get(eid) or {}
        if e.get("type") in PORTRAIT_TYPES:
            for gid in self.groups_of(bi, eid, anchor, finished)[:GROUPS_TRIED]:
                gs = self._group_scope(bi, gid, anchor, scope, fin)
                if gs == PASSAGE:
                    continue
                g = self._note(bi, gid, anchor, "", finished, gs)
                if g.get("home"):
                    return f"Their people's home: {_list([d['text'] for d in g['home']])}", list(g["sources"]), list(g["inherits"])
        return "", [], []

    def _references(self, ids: list[str], pos: tuple[int, int], view: dict,
                    finished: frozenset[str] | None = None, scopes: dict | None = None) -> list[tuple[int, dict]]:
        """The current pictures of the entities a scene names and no others, most tied first, at most MAX_REFERENCES; a picture
        drawn from a note of an older version is no reference. `scopes` (entity id: `figure_scope`): a figure drawn per story
        time takes only a picture of the scene's scope, and one drawn from the passage none."""
        deg = Counter()
        for r in view["relations"]:
            deg[r["src"]] += 1
            deg[r["dst"]] += 1
        scopes = scopes or {}
        found = [(eid, hit) for eid in dict.fromkeys(ids) if scopes.get(eid) != PASSAGE
                 and (hit := self.latest_picture(eid, pos, finished=finished, current=True, scope=scopes.get(eid)))]
        order = {eid: n for n, (eid, _) in enumerate(found)}
        found.sort(key=lambda t: (-deg[t[0]], order[t[0]]))
        return [h for _, h in found[:MAX_REFERENCES]]

    # ---- one picture: its job, its prompt, its drawing (also the entry points of on-demand drawing)

    def job(self, kind: str, target: str, bi: int = -1, at: str | None = None, shape: str | None = None,
            finished: set[str] | frozenset[str] | None = None) -> Job:
        """The job for a picture of any kind: an event id, a chapter title, or an entity id (the anchor is the latest facet or
        profile visible at `at`). A target after `at` raises NotVisible. `finished`: the earlier book keys the reader has
        finished (None: all); the job carries it to `make_prompt`, and an entity only an unfinished earlier book has is not
        visible."""
        bi = len(self.books) - 1 if bi < 0 else bi
        at_bi = bi
        if kind not in SHAPE_OF:
            raise ValueError(f"no such kind of picture: {kind}")
        fin = self._finished(bi, finished)
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
                if fin is not None and self._bkey(bi) not in fin:
                    raise NotVisible(target)
                b = self.books[bi]
            e = b.ents[target]
            anchor = e["from"] if e["from"] in b.ord_of else b.anchors[0]["id"]
            for f in e.get("facets", []):
                if f["from"] in b.ord_of and b.ord_of[f["from"]] >= b.ord_of[anchor] and (
                        at is None or bi < at_bi or b.ord_of[f["from"]] <= b.ord_of[at]):
                    anchor = f["from"]
            job = Job(kind, target, bi, anchor, shape=shape)
        job.finished = fin if bi == at_bi else (None if fin is None else frozenset(fin))
        if self.story_time and kind in ENTITY_KINDS:
            job.scope = self._job_scope(job, at if bi == at_bi else None, at_bi, fin)
        if job.finished is not None:
            self.world(bi, job.finished)   # the reader's own world note, written when the set has none for them
        if at is not None and bi == at_bi and b.ord_of[self._anchor(bi, job.anchor, self._finished(bi, job.finished))] > b.ord_of[at]:
            raise NotVisible(target)
        return job

    def _job_scope(self, job: Job, at: str | None, at_bi: int, fin: frozenset[str] | None) -> str | None:
        """The story time an on-demand picture of an entity is drawn at, for an entity drawn per scope: the reader's own (the
        scope at `at`) when the entity has been seen in it, else the scope of its latest item or event at or before the job's
        anchor. None for an entity with one scope."""
        bi = job.bi
        self.timeline(at_bi, self.books[at_bi].seq_of.get(at) if at else None, fin)
        jf = None if fin is None or bi != at_bi else fin
        if not self.multi(job.target, bi, job.anchor, jf):
            return None
        mine = self.scopes_of(job.target, bi, job.anchor, jf)
        here = self.scope_of(bi, at, jf) if at else None
        if here in mine:
            return here
        pos = self._pos(bi, job.anchor)
        last = [s for p, s in self._presence(job.target, bi, jf) if p <= pos]
        return last[-1] if last else None

    def _figures(self, job: Job) -> list[str]:
        """The entities a picture shows: the ones its event names (a plate with no event, the chapter's most named), no others."""
        if job.kind in ENTITY_KINDS:
            return [job.target]
        if job.event:
            return list(dict.fromkeys(job.event.get("entities", [])))
        ids = [x for e in (job.chapter or {}).get("events", []) if e.get("kind") != "minor" for x in e.get("entities", [])]
        return [x for x, _ in Counter(ids).most_common(4)]

    def make_prompt(self, job: Job, finished: set[str] | frozenset[str] | None = None) -> Prompt:
        """Write the prompt for one picture from what is visible at its anchor: the passage the event cites, the index content
        (`gating.view`), the world note and the figures' appearance notes (appended verbatim by code). Raises
        ValueError("dropped: ...") with the reason when no usable prompt comes out.

        `finished` (else the job's): the earlier book keys the reader has finished, None for all of them (a run). Content of an
        earlier book outside it never reaches the prompt: not its world note (this book's own is used), its appearance notes,
        its facets or its pictures as references. `Prompt.inherits` lists the earlier books whose content the prompt does use
        (world note, notes, profiles, entries and reference pictures from them), and `partial` says it was written for a
        reader with fewer books."""
        bi, b = job.bi, self.books[job.bi]
        me = self._bkey(bi)
        fin = self._finished(bi, finished if finished is not None else job.finished)
        if fin is not None:
            self.world(bi, fin)
        anchor = self._anchor(bi, job.anchor, fin)
        upto, pos = b.seq_of[anchor], self._pos(bi, anchor)
        view = self.view(bi, anchor, fin)
        vis = {x["id"]: x for x in view["entities"]}
        names = self.names[bi]
        world, wfrom, inherits = self._world_text(bi, pos, fin)
        world = names.strip(world) if world else world     # a note stored earlier or by an earlier book may name someone
        inherits = set(inherits)
        sources = {job.anchor} | ({wfrom} if wfrom else set())
        if job.event:                      # the moment shown: its text, setting and figures reach the prompt
            sources |= {job.event["from"]} | {a for a in job.event.get("evidence", []) if a in b.ord_of}
        ids = [x for x in self._figures(job) if x in vis]
        # story time: a figure drawn per scope takes the note and references of the scene's scope, or the passage only
        if self.story_time:
            self.timeline(bi, upto, fin)
        scene_scope = self._scene_scope(job, fin)
        fscope = {eid: self.figure_scope(bi, eid, anchor, scene_scope, fin) for eid in ids}
        figures: list[tuple[str, str]] = []
        for eid in ids:
            e = b.ents.get(eid) or next((bb.ents[eid] for bb in self.books if eid in bb.ents), None)
            if e is None or e["type"] not in NOTE_TYPES or len(figures) >= 6:
                continue
            if fscope[eid] == PASSAGE:
                if job.kind in ENTITY_KINDS and eid == job.target:
                    raise ValueError("skipped: story time not known")
                continue
            note, why, srcs, books = self.figure_note(bi, eid, anchor, job.change if job.kind == "portrait" and eid == job.target else "",
                                                      fin, fscope[eid])
            if note:
                figures.append((eid, names.strip(note)))
                sources |= set(srcs)
                inherits |= set(books)
            elif job.kind in ENTITY_KINDS and eid == job.target:
                if why == "empty":
                    raise ValueError("skipped: no look described")
                raise Dropped(_reason(why), why, self._say(why, bi, anchor, fin))
        if job.kind in ENTITY_KINDS:
            e = b.ents.get(job.target) or {}
            ts = fscope.get(job.target)
            seqs = self._mentions(bi, job.target, upto, 8, ts, fin) if job.kind == "portrait" else \
                self._in_scope(bi, [b.seq_of[a] for f in e.get("facets", []) for a in f["evidence"] if a in b.seq_of
                                    and b.seq_of[a] <= upto], ts, fin)
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
        notes_of = dict(figures)
        for eid in ids[:6]:
            v = vis[eid]
            also = f" (also {', '.join(v['aliases'][:2])})" if v["aliases"] else ""
            if fscope[eid] is not None:
                # drawn per story time: its profile blends the times, so the line is its note of this scope, or its kind
                ctx.append(v["name"] + also + (": " + notes_of[eid] if notes_of.get(eid) else f" ({P.KINDS.get(v.get('type'), v.get('type') or 'figure')})"))
                continue
            line = v["name"] + also + (": " + v["profile"]["text"] if v["profile"] else "")
            ctx.append(line)
            own = {f["id"]: f["from"] for f in b.ents.get(eid, {}).get("facets", []) if f["from"] in b.ord_of}
            sources |= {own[f["id"]] for f in ([v["profile"]] if v["profile"] else []) + v["facets"][:4] if f["id"] in own}
            if v["profile"] and v["profile"].get("book") not in (None, me):
                inherits.add(str(v["profile"]["book"]))
            if eid not in b.ents:       # an entry this book has not named yet: its names come from an earlier book
                inherits |= {self._bkey(j) for j in range(bi) if eid in self.books[j].ents}
        # the surroundings: a portrait's are its figure's milieu, a scene's the setting of its moment (its place's note when it has one)
        milieu = ""
        if job.kind == "portrait":
            milieu, msrcs, mbooks = self.milieu(bi, job.target, anchor, fin, fscope.get(job.target))
            sources |= set(msrcs)
            inherits |= set(mbooks)
        elif job.event and (st := job.event.get("setting")) and st.get("place") in vis \
                and (b.ents.get(st["place"]) or {}).get("type") in NOTE_TYPES \
                and (ps := self.figure_scope(bi, st["place"], anchor, scene_scope, fin)) != PASSAGE:
            note, _, msrcs, mbooks = self.ensure_note(bi, st["place"], anchor, "", fin, ps)
            if note:
                milieu = note
                sources |= set(msrcs) | {job.event["from"]}
                inherits |= set(mbooks)
        milieu = self.names[bi].strip(milieu) if milieu else ""
        what = KINDS_WHAT[job.kind].format(frame=(job.event or {}).get("summary", ""), who=who,
                                           background=BACKGROUND if milieu else PLAIN)
        if job.kind == "plate" and not job.event:
            what = "the main moment of a chapter, as its recap tells it."
        fig = "\n".join(f"- {vis[eid]['name']}: {note}" for eid, note in figures)
        extra = ("- This is a portrait of the figure listed above: say only pose, expression and background. Describe nothing of its look\n"
                 "  (age, build, hair, clothing): its fixed note is its whole look and is added by the program.\n") if job.kind == "portrait" else ""
        writer = WRITE.format(what=what, world=world or "(none)", figures=f"\nFigures (fixed notes):\n{fig}\n" if fig else "",
                              place=f"\nPlace (added by the program; do not repeat it): {milieu}\n" if milieu else "",
                              context="\n".join(ctx) or "(none)", passage=passage or "(no passage)", extra=extra)
        check = "\n".join([passage, world, milieu] + [n for _, n in figures] + ctx)
        # a figure's look comes from its own note or the passage; a portrait's only from its own note, never from the passages
        # (which describe whoever else is in them) or another figure
        looks = dict(figures).get(job.target, "") if job.kind in ENTITY_KINDS else check
        scene, why, out = self._write_full(writer, bi, anchor, check, SCENE_WORDS, SCENE_SCHEMA, looks=looks)
        if scene is None:
            raise Dropped(_reason(why), why, self._say(why, bi, anchor, fin))
        if job.kind not in ENTITY_KINDS:   # an entity's own picture always shows it
            figures, ids = self._shown(out, figures, ids, vis)
        if any(a in b.ord_of and b.ord_of[a] > b.ord_of[anchor] for a in sources):
            raise ValueError("dropped: written from content after its anchor")
        refs: list[tuple[int, dict]] = []
        likeness = False
        ts = fscope.get(job.target) if job.kind in ENTITY_KINDS else None
        if job.kind in ("plate", "event"):
            refs = self._references(ids, pos, view, fin, fscope)
        elif job.kind == "portrait" and job.change:
            prev = self.latest_picture(job.target, pos, ("portrait",), strict=True, finished=fin, current=True, scope=ts)
            refs = [prev] if prev else []
        elif job.kind == "portrait" and ts and self.likeness and not self.latest_picture(
                job.target, pos, ("portrait",), strict=True, finished=fin, scope=ts):
            # the first portrait of a story time: the figure's portrait at another time keeps the likeness
            other = self.latest_picture(job.target, pos, ("portrait",), strict=True, finished=fin, current=True, other=ts)
            refs, likeness = ([other], True) if other else ([], False)
        for j, r in refs:                  # a reference picture carries what its own prompt came from
            inherits |= set(r.get("inherits", [])) | ({self._bkey(j)} if j != bi else set())
        # the last check, on the whole prompt as the image model gets it (`_assemble` strips what it can; a custom style may
        # name someone too): what is still there drops the prompt
        text = self._assemble(scene, [n for _, n in figures], world, bi, self._place_line(job.kind, milieu), likeness)
        if hit := names.find(text):
            raise Dropped("dropped: name in prompt", f"name in the text: {hit}", self._say(f"name in the text: {hit}", bi, anchor, fin))
        if job.kind in ENTITY_KINDS:
            scope = ts if ts not in (None, PASSAGE) else job.scope
        else:
            scope = scene_scope if scene_scope != OTHER and any(v not in (None, PASSAGE) for v in fscope.values()) else None
        return Prompt(text, scene, anchor, job.shape or SHAPE_OF[job.kind], figures, refs,
                      sorted((a for a in sources if a in b.ord_of), key=b.ord_of.get), writer, check,
                      sorted(inherits - {me}), fin is not None, world, milieu, scope=scope, likeness=likeness)

    def _say(self, why: str, bi: int, anchor: str, finished: frozenset[str] | None) -> str:
        """`explain(why)` for the reader at `anchor`: a caught name only when the whole name, as caught, occurs in the text the
        reader has reached (this book up to the anchor, or a finished earlier book's index); else "a name the book has not used
        by this point", since it could be a spoiler. An unknown name is never shown."""
        phrase = explain(why)
        if not why.startswith(("name in the text: ", "unknown name: ")):
            return phrase
        hidden = "a name the book has not used by this point"
        if why.startswith("unknown name: "):
            return hidden
        word = why.split(": ", 1)[1]
        rx = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)")   # case-sensitive: "Dawn" is never shown off "at dawn"
        b = self.books[bi]
        upto = b.seq_of[anchor]
        if b.epub is not None and any(rx.search(t) for s, t in b.text.items() if s <= upto):
            return phrase
        for j in range(bi):         # a finished earlier book: every word of its index was read
            if (finished is None or self._bkey(j) in finished) and rx.search(json.dumps(self.books[j].doc, ensure_ascii=False)):
                return phrase
        return hidden

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

    @staticmethod
    def _place_line(kind: str, milieu: str) -> str:
        """The surroundings line code adds: the milieu, or for a portrait with none a plain background."""
        if milieu:
            return "Place: " + milieu.rstrip()
        return "Plain, neutral background." if kind == "portrait" else ""

    def _assemble(self, scene: str, notes: list[str], world: str, bi: int = -1, place: str = "", likeness: bool = False) -> str:
        """What the image model gets: the scene, then the notes (and, for a likeness reference from another story time, the
        line saying so), the place, the era note and the style, all added by code. A name any of them still carries (a custom
        style naming someone) is replaced by a role word, as in the scene."""
        parts = [scene.rstrip()]
        if notes:
            parts.append("Figures: " + " ".join(n.rstrip(".") + "." for n in notes))
        if likeness:
            parts.append(LIKENESS)
        if place:
            parts.append(place)
        if world:
            parts.append("World: " + world.rstrip())
        text = " ".join(parts + [style_sentence(self.styles[bi]), NO_TEXT])
        names = self.names.get(bi if bi >= 0 else len(self.books) - 1)
        return names.strip(text) if names is not None and names.find(text) else text

    # ---- cost and the cap

    def _skip(self, bi: int, target: str, reason: str, final: bool = False, kind: str = "", anchor: str = "",
              detail: str = "", refused: bool = False, scope: str | None = None) -> None:
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
                self.man[bi]["skipped"].append({"target": target, "reason": reason, "kind": kind, "from": anchor, "final": True,
                                                **({"scope": scope} if scope else {})})
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
               "references": [r["id"] for _, r in pr.references], "sources": pr.sources, "inherits": list(pr.inherits),
               **({"notes": pr.version} if pr.version is not None else {})}
        if pr.partial:
            rec["partial"] = True
        scope = pr.scope if pr.scope is not None else job.scope
        if scope:
            rec["scope"] = scope
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
                       detail=getattr(e, "detail", ""), scope=job.scope)
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
                        soft, _ = self._write(pr.writer + SOFTEN.format(text=pr.scene), bi, pr.anchor, pr.check, SCENE_WORDS,
                                             looks=dict(pr.notes).get(job.target, "") if job.kind in ENTITY_KINDS else pr.check)
                    except TextFailed:
                        soft = None
                if not soft:
                    self._skip(bi, job.target, "refused: provider declined", final=True, kind=job.kind, anchor=job.anchor,
                               detail=e.reason[:160], refused=True, scope=job.scope)
                    return None
                text_ = self._assemble(soft, [n for _, n in pr.notes], pr.world, bi, self._place_line(job.kind, pr.milieu), pr.likeness)
                if hit := self.names[bi].find(text_):     # the same last check as make_prompt's
                    self._skip(bi, job.target, "dropped: name in prompt", final=True, kind=job.kind, anchor=job.anchor,
                               detail=f"name in the text: {hit}", scope=job.scope)
                    return None
                pr.scene, pr.text = soft, text_
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
        self.timeline(bi)
        for j in jobs:
            a = self._anchor(bi, j.anchor)
            view = self.view(bi, a)
            vis = {x["id"] for x in view["entities"]}
            scene = self._scene_scope(j)
            for eid in self._figures(j):
                e = b.ents.get(eid)
                if eid in vis and e and e["type"] in NOTE_TYPES:
                    fs = self.figure_scope(bi, eid, a, scene)
                    if fs == PASSAGE:
                        continue             # drawn from the passage only: no note
                    needs.setdefault(eid, []).append((b.ord_of[a], a, j.change if j.kind == "portrait" and eid == j.target else "",
                                                      j.kind == "portrait" and eid == j.target, fs or ""))

        def entity(item):
            for _, a, change, portrait, fs in sorted(set(item[1])):
                with self.lock:
                    if not self.stop and self._over_cap():
                        self.stop = f"cost cap ${self.cap:.2f} reached"
                if self.stop:
                    return
                try:
                    self.figure_note(bi, item[0], a, change, None, fs or None)     # its groups' notes too, at the same anchor
                    if portrait:
                        self.milieu(bi, item[0], a, None, fs or None)
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
