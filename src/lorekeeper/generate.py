"""Generate a Lorekeeper index from an EPUB with a model (see SPEC.md §6 for the content rules).

The book is read forward in large chunks of whole paragraphs (about a chapter), one extraction call
each. The model sees only that chunk, the story-so-far recap, and what is already known about
entities named in it, so it cannot leak later text, and it reports only what the chunk adds:
output size is what makes local generation slow. Python, not the model, owns everything structural:
anchors, ids, reveal points, name matching and de-duplication. Every claim then passes the
mechanical rules, a rephrase retry if it copies the book, and a support check against the
paragraphs it cites. Anything that fails is dropped: a missing fact is better than a wrong one.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import merge, text, validate
from .epub import Book
from .llm import LLM, Cancelled

GENERATOR_VERSION = "0.2.0"
PROMPT_VERSION = "extract-v5"
MAX_COPY = validate.MAX_VERBATIM_WORDS + 1
MAX_CONTEXT_FACTS = 4
MAX_KNOWN_LIST = 150
MAX_DISAMBIGUATE = 4   # model calls per chunk that settle which entry a name belongs to

TYPES = {
    "person": "an individual character, named or clearly identified; includes non-humans who speak and act as characters (a ghost, a centaur, a talking cat)",
    "creature": "an animal, monster or kind of being: a pet, a mount, a dragon, a species",
    "place": "a geographic location, building, room or establishment",
    "group": "a regiment, organization, institution, family or other collective",
    "object": "a ship, document, weapon or other significant physical thing",
    "concept": "an idea, method, discovery or event-like phenomenon (a war, a disease)",
    "term": "a word or phrase with a special meaning in the story",
}

# How much to extract. Output size is what makes generation slow, so depth trades coverage for speed.
DEPTHS = {
    "light": {"who": "only characters, places and things that matter to the story or recur",
              "per_entity": lambda words: 1, "relations": False},
    "standard": {"who": "the cast and anything a reader might want to look up later; skip one-off mentions",
                 "per_entity": lambda words: max(2, round(words / 1500)), "relations": True},
    "deep": {"who": "everyone and everything named, including minor characters and terms",
             "per_entity": lambda words: max(3, round(words / 800)), "relations": True},
}

# Wording that only makes sense with knowledge of later text (SPEC.md §6.2).
# A profile describes who someone is, not where the story has got to with them.
MOMENTARY = re.compile(r"\b(now|currently|at the moment|at present|has just|have just|has begun to|have begun to|is about to|"
                       r"are about to|on (his|her|their|its) way|so far in the story)\b", re.IGNORECASE)

# Words that state how two people are tied. A description may use one only if the text it was
# written from does (or the entity's notes do): models know these ties from the book itself.
TIE_WORDS = ("wife", "husband", "fiancé", "fiancée", "betrothed", "intended", "engaged", "married", "widow",
             "widower", "bride", "groom", "lover", "mistress", "mother", "father", "parents", "son", "daughter",
             "brother", "sister", "uncle", "aunt", "nephew", "niece", "cousin", "heir")
_TIE_FORMS = {"wife": {"wives"}, "parents": {"parent"}, "married": {"marry", "marries", "marriage", "wed"},
              "fiancé": {"fiance", "fiancee", "fiances", "fiancees"}, "fiancée": {"fiance", "fiancee", "fiances", "fiancees"},
              "widow": {"widowed"}, "bride": {"brides"}, "heir": {"heirs", "heiress"}}


def _plain_words(t: str) -> list[str]:
    t = unicodedata.normalize("NFKD", str(t)).casefold()
    return re.findall(r"\w+", "".join(ch for ch in t if not unicodedata.combining(ch)))


def _tie_forms(w: str) -> set[str]:
    base = _plain_words(w)[0]
    return {base, base + "s", base + "es", *_TIE_FORMS.get(w, set())} | {f for x in _TIE_FORMS.get(w, ()) for f in (x,)}


def unsupported_tie(claim: str, *sources: str) -> str | None:
    """The first tie word in `claim` that none of `sources` uses (whole words, ignoring case and
    accents; plurals and close forms count), else None."""
    have = set(_plain_words(" ".join(sources)))
    mine = set(_plain_words(claim))
    for w in TIE_WORDS:
        if _plain_words(w)[0] in mine:
            if not (_tie_forms(w) & have):
                return w
    return None


HINDSIGHT = re.compile(
    r"\b(seemingly|ostensibly|apparently innocent|unbeknownst|little (does|did) (he|she|they|we)|"
    r"(would|will) (later|eventually|soon)|turns out|in reality|actually (is|was) the|foreshadow\w*|"
    r"at first glance|for now)\b", re.IGNORECASE)

HONORIFICS = merge.HONORIFICS

SYSTEM = """You keep a spoiler-free reader's companion for a novel, reading it one passage at a time.
Use ONLY the passage and the notes you are given. Never use outside knowledge of this book, its author or its sequels.
Describe things as a careful reader understands them at this point: no hindsight, no hints about what comes later.
Write in your own words; never copy phrases from the text. Only state what happened or is said; keep events in order."""

# The passage always comes first, so a second call over the same passage reuses the cached prefix.
PASSAGE = """Passage (paragraphs are numbered):

{passage}
"""

EXTRACT = """
Story so far: {recap}

The section just before this passage: {previous}

Already in the companion (id: name): {known}

What the companion says about entities named in this passage:
{context}

Entity types: {types}

Update the companion with what THIS passage adds. Report only new information.
- new: entities first named in this passage. Include {who}. "name" = the name exactly as THIS passage uses it, with no notes or descriptions added (not "Isla (thrall)"); never a name you know from elsewhere that the passage doesn't use. First check the companion: if the passage calls someone already there by another name (a first name, a title, a role such as "his uncle"), that is not new: report the name under names. One entity per individual: "Mr. and Mrs. Dursley" or "Lily and James Potter" are two entities. An entity has a name of its own, or is a person the text describes because it has not named them yet ("the old priest"). Someone's possessions, feelings, rooms, plans or fights are not entities ("Gabriel's sword", "Dior's fear"), and a known entity described another way ("Celene's brother", "the Lion") is that entity: use its id. "p" = paragraph where first named; "desc" = ONE sentence (under 30 words) that reads like the first line of a character sheet: who or what this is in the story as the passage shows it, its tie to the viewpoint character or to someone already known, its role or kind. Never its errands, plans, intentions or what it does in the scene. For someone named only in passing, say what the passage lets a reader know ("Someone close to Harker at home, whom he writes to and keeps notes for"), not a paraphrase of the sentence that names them; or "" if the passage shows nothing. A tie, rank, role, status or place goes in only if THIS passage says it, never from what you know of the book; "desc_p" = the paragraph that sentence relies on.
- updates: what this passage reveals about what an entity IS, for entities in the companion (use the id) or in your "new" list (use the name): identity or role, appearance, nature, abilities, beliefs, allegiances, ties to others, and changes in status (wounded, promoted, captured, turned, dead). Not what they do in a scene: actions and plot belong in events and the summary. Write a lasting fact ("Can boil a person's blood, a power from his father"), not a moment ("He boils Alba's blood"). Something a character says, is told or believes is reported as that ("Has been told his parents died in a crash"), not as fact, unless the text confirms it. ONE sentence under 30 words each, "p" = the paragraph where it is shown, at most {per_entity} per entity. If it overturns something the companion says, put that fact's [id] in "corrects".
- profiles: for EVERY new person, creature and group, however briefly it appears, and for each other entity this passage teaches something lasting about, a profile (a tie, rank, role, status or whereabouts may be stated ONLY when this passage or the companion's notes for that entity say it, even if you know it from the book: when the passage shows someone only in passing, name the connection the text actually shows and stop, "Someone Harker keeps notes for while travelling", not "Harker's fiancée"): the description a reader would want on a character sheet, of who or what it is as known so far. Identity and role, look, nature, abilities, and the ties that define it ("Harry's best friend", "the king's sister"). Only lasting changes in status count (orphaned, knighted, exiled, dead); never where it is or what it is doing at the moment, and never recent events ("now on the train", "has begun to trust X", "has just arrived"). Timeless present tense: "An orphaned farm boy raised by his uncle who discovers he can work magic; quick-tempered but loyal", not "Now on the road to the capital, he has begun to trust Mira". 1 to 3 sentences, under 70 words, rewritten in full from the companion's profile plus this passage. "id" = id or new name; "p" = the paragraph by which everything in it has been shown. At most {profiles}.
- names: a new name or title for an entity already in the companion.{relations}
- events: at most {events} key moments, one short sentence each, with "who" (ids or new names).
- summary: what happens in THIS passage, in order, as {summary} sentences (each under 35 words) with the key details:
  who does what and where, what is learned, decided or left unresolved. Use names, not pronouns.
- recap: the story so far, updated to the end of this passage, in at most 6 sentences: the main threads briefly,
  then where things stand at the end of this passage (who is where, what they are after, what is unresolved).
  Only what the story so far and this passage say.
Leave a list empty when there is nothing to add."""

VERIFY = """
Check each claim against the WHOLE passage. Most claims are right; reject only what is actually wrong.
"cited at paragraph N" is only where the claim says it is shown. Judge it on the whole passage and the story so
far; if what it says is only shown after paragraph N, put it under later. Never reject a claim for where it is
cited. Claims marked (recap) or (summary) may also use the earlier chapters below. Claims marked (profile)
describe an entity as a whole and may also use the "known" notes given with them. A (profile) claim that states a
relationship, title, rank, role, status or whereabouts that neither the passage nor its "known" notes state is
unsupported, even if it is likely or commonly known.
A (name) claim is unsupported unless the passage itself uses that name for that individual. It is also unsupported when the passage uses that name for a relative or another person (a mother, husband, servant, someone the entity speaks to).
- unsupported: ids of claims that the text contradicts, or that state something it does not say: invented details,
  the wrong person, place or number, events out of order, or a guess, rumour or character's claim given as
  fact. Do NOT list a claim just because its support is spread over several paragraphs or lies in the notes,
  the story so far or the earlier chapters.
- later: claims that are right but are only fully shown after the paragraph they cite. Give the id and the
  earliest paragraph by which everything in the claim has been shown.
- moments: ids of (fact) claims that are right but narrate one moment or action in a scene ("He jumps onto the
  troll") instead of saying what the entity is: its role, look, nature, abilities, beliefs, ties or status.
Claims not listed are accepted.

Story so far: {recap}

Earlier chapters:
{chapters}

Claims:
{claims}"""

DISAMBIGUATE = """A name has just been used in the passage below. Decide which entry of the companion it names.
Use only the passage and the entries.

Name: {name}

The passage around where the name is first used:
{context}

Entries in the companion:
{entries}

Answer with "choice":
- the letter of the entry this name belongs to, when the passage or the entry's ties, role or look show it;
- "both" when the entries listed are one and the same individual and this name is that individual;
- "none" when the name is someone else, a person not listed.
A shared first name or surname is not enough by itself. When the passage does not tell you, answer "none"."""

REPHRASE = """These sentences copy too much wording from the book. Rewrite each one in plain, different words,
keeping exactly the same meaning and length.

{items}"""


def clean_name(raw) -> str:
    """A name without the notes models like to add: "Isla (thrall)" → "Isla"."""
    name = " ".join(str(raw or "").split())
    bare = " ".join(re.sub(r"\s*\([^)]*\)", " ", name).split())
    return bare or name.strip("() ")


_POSSESSIVE = re.compile(r"^(?P<owner>.+?)['’]s\s+(?P<rest>.+)$")
_PARTICLES = {"de", "du", "des", "la", "le", "van", "von", "der", "di", "da", "of", "á", "a", "y", "el"}
_PREPOSITIONS = {"of", "at", "in", "on", "from", "to", "by", "with", "near", "for", "under", "over", "beyond"}


def _trailing_name(phrase: str) -> str:
    """The proper name a descriptive phrase ends with: "sister Celene" → "Celene",
    "bedchamber, White Rabbit" → "White Rabbit", "straight razor" → "". A place after a
    preposition ("baron of León", "room at Aveléne") is not the entry's name, and neither is
    one of several people ("Saoirse and Bellamy")."""
    words = phrase.replace(",", " , ").split()
    if "and" in words or "&" in words:
        return ""
    i = len(words)
    while i > 0 and (words[i - 1][:1].isupper() or (words[i - 1] in _PARTICLES and i < len(words))):
        i -= 1
    while i < len(words) and words[i] in _PARTICLES:
        i += 1
    if 0 < i < len(words) and words[i - 1] in _PREPOSITIONS:
        return ""
    return " ".join(words[i:])


def _described(outer: str) -> bool:
    """Is this a description or title rather than a name? ("Gabriel's friend", "Chevalier of the
    Golden Host", "the old priest")"""
    outer = outer.strip()
    first = merge._plain(outer.split()[0].casefold()) if outer else ""
    return bool(_POSSESSIVE.match(outer)) or outer[:1].islower() or " of " in f" {outer} " or first in HONORIFICS or first == "the"


def split_couple(name: str) -> list[str] | None:
    """Two people named together, as two names: "Lily and James Potter" → Lily Potter, James Potter;
    "Mr. and Mrs. Dursley" → Mr. Dursley, Mrs. Dursley; "Fred and George" → Fred, George."""
    m = re.fullmatch(r"(?P<a>[^,]+?)\s+(?:and|&)\s+(?P<b>[^,]+)", name)
    if not m or _POSSESSIVE.search(name):
        return None
    a, b = m["a"].split(), m["b"].split()
    if not (a[0][:1].isupper() and b[0][:1].isupper()):
        return None
    if len(a) == 1 and len(b) >= 2:
        # The second name carries the shared surname: "Lily and James Potter", "Mr. and Mrs. Dursley".
        return [" ".join([a[0], *b[1:]]), " ".join(b)]
    return [" ".join(a), " ".join(b)]


def triage_name(raw, etype: str | None, known) -> tuple[str, str]:
    """What a proposed entity name really is. Models propose descriptions as names ("Gabriel's
    straight razor"), wrap a known entity in a description ("Celene's brother Gabriel") or bury the
    real name in a note ("Chevalier of the Golden Host (Gabriel)").

    A possessive description of a person ("Gabriel's grandfather") is kept: it is how the book
    refers to someone not yet named. One of a thing ("Gabriel's fear", "Gabriel's cell") is not an
    entity. known(name) → entity id or None. Returns ("known", id), ("name", name) or ("drop", reason).
    """
    name = " ".join(str(raw or "").split())
    m = re.fullmatch(r"(?P<outer>.+?)\s*\((?P<inner>[^)]*)\)", name)
    if m and m["inner"].strip() and _described(m["outer"]):
        # "Chevalier of the Golden Host (Gabriel)": a title or description, then who it is. A name
        # with a note ("Lashaame (High Inquisitrix)") stays that name: the note may be a shared title.
        eid = known(m["inner"].strip())
        if eid:
            return "known", eid
    name = clean_name(name)
    m = _POSSESSIVE.match(name)
    if m:
        rest = m["rest"]
        tail = _trailing_name(rest)
        if tail and tail != rest:
            eid = known(tail)
            return ("known", eid) if eid else ("name", tail)
        if rest[:1].islower() and etype not in ("person", "group"):
            return "drop", "description, not a name"
    return "name", name


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "required": required, "properties": props}


_INT = {"type": "integer"}
_STR = {"type": "string"}
_ARR = lambda item: {"type": "array", "items": item}  # noqa: E731


def extract_schema(relations: bool) -> dict:
    props = {
        "new": _ARR(_obj({"name": _STR, "type": {"enum": list(TYPES)}, "p": _INT, "desc": _STR, "desc_p": _INT},
                         ["name", "type", "p", "desc", "desc_p"])),
        "updates": _ARR(_obj({"id": _STR, "p": _INT, "desc": _STR, "corrects": _STR}, ["id", "p", "desc"])),
        "profiles": _ARR(_obj({"id": _STR, "p": _INT, "text": _STR}, ["id", "p", "text"])),
        "names": _ARR(_obj({"id": _STR, "name": _STR, "p": _INT}, ["id", "name", "p"])),
        "events": _ARR(_obj({"p": _INT, "text": _STR, "who": _ARR(_STR)}, ["p", "text", "who"])),
        "summary": _ARR(_STR),
        "recap": _STR,
    }
    if relations:
        props["relations"] = _ARR(_obj({"a": _STR, "label": _STR, "b": _STR, "p": _INT}, ["a", "label", "b", "p"]))
    return _obj(props, list(props))


EXTRACT_SCHEMA = extract_schema(True)
VERIFY_SCHEMA = _obj({"unsupported": _ARR(_STR), "later": _ARR(_obj({"id": _STR, "p": _INT}, ["id", "p"])),
                      "moments": _ARR(_STR)}, ["unsupported", "later", "moments"])
EARLIER_CHAPTERS = 9000   # characters of earlier chapter recaps shown to the fact-check
REPHRASE_SCHEMA = _obj({"items": _ARR(_obj({"id": _STR, "text": _STR}, ["id", "text"]))}, ["items"])


@dataclass
class Options:
    chapters: list[int] | None = None    # 1-based table-of-contents entries; None = whole book
    chunk_words: int = 2500
    depth: str = "standard"              # light | standard | deep
    events: bool = True
    verify: bool = True
    index_id: str | None = None
    series_id: str | None = None
    book_key: str | None = None
    license: str = "CC0-1.0"
    prior: list = field(default_factory=list)   # index documents of earlier books in the series, in order
    check_llm: object = None    # model for the fact-check; None = the extraction model
    whole_story: bool = False   # the chosen chapters are all of the story (only front/back matter skipped)
    distinct: set = field(default_factory=set)   # frozenset({id, id}) pairs a person ruled "not the same"


@dataclass
class Chunk:
    chapter: int      # 0-based chapter index, -1 for front matter
    start: int        # first block seq
    end: int          # last block seq (inclusive)

    @property
    def key(self) -> str:
        return f"{self.start}-{self.end}"


@dataclass
class Candidate:
    """A claim proposed by the model, normalized and waiting for checks."""
    kind: str                     # facet | relation | event
    cid: str
    text: str
    from_seq: int
    evidence: list[int]
    data: dict = field(default_factory=dict)


_LIGATURES = str.maketrans({"æ": "ae", "œ": "oe", "ß": "ss", "ø": "o", "đ": "d", "ł": "l", "þ": "th", "ð": "d"})


def slug(name: str) -> str:
    """ASCII id fragment (ids must match [a-z0-9._-]): transliterate Latin, hash anything else."""
    parts = []
    for w in text.words(name)[:6]:
        ascii_w = "".join(c for c in unicodedata.normalize("NFKD", w.translate(_LIGATURES))
                          if c.isascii() and c.isalnum())
        parts.append(ascii_w or "u" + hashlib.sha256(w.encode()).hexdigest()[:6])
    return "-".join(parts) or "entity"


class Generator:
    def __init__(self, book: Book, llm: LLM, opts: Options, epub_path: str | Path,
                 log: Callable[[str], None] = print) -> None:
        self.book, self.llm, self.opts, self.log = book, llm, opts, log
        self.epub_path = Path(epub_path)
        self.key = opts.book_key or slug(book.title) or "book"
        self.book_shingles = text.shingles([w for b in book.blocks for w in b.words], MAX_COPY)
        self._eligible = self._eligible_blocks()

        self.anchors: dict[int, str] = {}          # seq → anchor id
        self.seq_of: dict[str, int] = {}           # anchor id → seq
        self.entities: dict[str, dict] = {}        # id → index-format entity
        self.entity_from: dict[str, int] = {}      # id → seq of first name
        self.relations: list[dict] = []
        self.events: list[dict] = []
        self.recaps: list[dict] = []
        self.item_seq: dict[str, int] = {}         # facet/relation id → from seq
        self.item_kind: dict[str, str] = {}        # facet/relation id → kind
        self.done: list[str] = []                  # processed chunk keys
        self.on_phase: Callable[[str], None] = lambda phase: None
        self.added: list[dict] = []                # items committed by the current chunk, for live UIs
        self.rejected: list[dict] = []             # claims the fact-check turned down (capped), for review
        self.recap_text = ""
        self.summary_text = ""                     # the previous chunk's summary, for continuity
        self.counters: Counter = Counter()
        self.stats: Counter = Counter()
        self._asked = 0                            # disambiguation calls made in the current chunk
        self._redirect: dict[str, str] = {}        # ids folded into another entity during the current chunk
        self.created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        # What earlier books in the series established. Used for matching, context and cross-book
        # corrections; an entity enters THIS book's index only once this book mentions it.
        self.prior: dict[str, dict] = {}           # id → {"type", "names", "facets": [{"id", "text"}]}
        self.prior_items: dict[str, str] = {}      # facet/relation id → kind
        self._load_prior(opts.prior)

    def _load_prior(self, indexes: list[dict]) -> None:
        replaced: set[str] = set()
        for ix in indexes:
            for e in ix.get("entities", []):
                p = self.prior.setdefault(e["id"], {"type": e["type"], "names": [], "facets": []})
                for n in e.get("names", []):
                    if n["value"] not in p["names"]:
                        p["names"].append(n["value"])
                for f in e.get("facets", []):
                    p["facets"].append({"id": f["id"], "text": f["text"], **({"kind": f["kind"]} if f.get("kind") else {})})
                    self.prior_items[f["id"]] = "facet"
                    replaced.update(f.get("replaces", []))
            for r in ix.get("relations", []):
                self.prior_items[r["id"]] = "relation"
                replaced.update(r.get("replaces", []))
            story = [rc for rc in ix.get("recaps", []) if rc.get("scope", "story") == "story"]
            if story:
                self.recap_text = story[-1]["text"]   # the story so far, as the last book left it
        for p in self.prior.values():
            p["facets"] = [f for f in p["facets"] if f["id"] not in replaced]
        self.prior = {k: v for k, v in self.prior.items() if v["names"]}

    # ---- anchors ---------------------------------------------------------------------------

    def _eligible_blocks(self) -> list[int]:
        """Seqs usable as anchors: at least 6 words and a fingerprint unique in this edition."""
        counts = Counter(b.fingerprint for b in self.book.blocks)
        return [b.seq for b in self.book.blocks
                if len(b.words) >= text.MIN_ANCHOR_WORDS and counts[b.fingerprint] == 1]

    def snap_forward(self, seq: int) -> int | None:
        i = bisect_left(self._eligible, seq)
        return self._eligible[i] if i < len(self._eligible) else None

    def snap_back(self, seq: int) -> int | None:
        i = bisect_right(self._eligible, seq)
        return self._eligible[i - 1] if i else None

    def chapter_of(self, seq: int) -> int:
        idx = -1
        for i, c in enumerate(self.book.chapters):
            if c.start <= seq:
                idx = i
            else:
                break
        return idx

    def anchor(self, seq: int) -> str:
        if seq not in self.anchors:
            ch = self.chapter_of(seq)
            if ch < 0:
                self.anchors[seq] = f"front.para{seq:02d}"
            else:
                self.anchors[seq] = f"toc{ch + 1:02d}.para{seq - self.book.chapters[ch].start:02d}"
            self.seq_of[self.anchors[seq]] = seq
        return self.anchors[seq]

    # ---- chunking --------------------------------------------------------------------------

    def chunks(self) -> list[Chunk]:
        chapters = self.book.chapters
        bounds: list[tuple[int, int, int]] = []    # (chapter idx, start, end)
        if not chapters:
            bounds.append((-1, 0, len(self.book.blocks) - 1))
        else:
            if chapters[0].start > 0 and self.opts.chapters is None:
                bounds.append((-1, 0, chapters[0].start - 1))
            for i, c in enumerate(chapters):
                if self.opts.chapters is not None and (i + 1) not in self.opts.chapters:
                    continue
                end = chapters[i + 1].start - 1 if i + 1 < len(chapters) else len(self.book.blocks) - 1
                bounds.append((i, c.start, end))
        out: list[Chunk] = []
        for ch, start, end in bounds:
            cur, words = start, 0
            for seq in range(start, end + 1):
                n = len(self.book.blocks[seq].words)
                if words and words + n > self.opts.chunk_words:
                    out.append(Chunk(ch, cur, seq - 1))
                    cur, words = seq, 0
                words += n
            out.append(Chunk(ch, cur, end))
        return [c for c in out if sum(len(self.book.blocks[s].words) for s in range(c.start, c.end + 1)) >= 30]

    # ---- entity matching -------------------------------------------------------------------

    @staticmethod
    def _core(name: str) -> tuple[str, ...]:
        return merge.core(name)

    def _names(self, eid: str) -> list[str]:
        local = [n["value"] for n in self.entities[eid]["names"]] if eid in self.entities else []
        return local + [n for n in self.prior.get(eid, {}).get("names", []) if n not in local]

    def _ids(self) -> list[str]:
        """Every known entity: earlier books' first, then this book's."""
        return list(dict.fromkeys([*self.prior, *self.entities]))

    def _type(self, eid: str) -> str:
        return self.entities[eid]["type"] if eid in self.entities else self.prior[eid]["type"]

    def _localize(self, eid: str, seq: int) -> str | None:
        """Bring an entity from an earlier book into this book's index at its first mention here."""
        if eid in self.entities:
            return eid
        snapped = self.snap_forward(seq)
        if snapped is None:
            return None
        p = self.prior[eid]
        self.entities[eid] = {"id": eid, "type": p["type"], "from": self.anchor(snapped),
                              "names": [{"value": p["names"][0], "kind": "primary", "from": self.anchor(snapped)}],
                              "facets": []}
        self.entity_from[eid] = snapped
        self.stats["returning entities"] += 1
        return eid

    def match_known(self, name: str, etype: str | None = None) -> str | None:
        """Find an existing entity for a name. In order, first unique answer wins:
        1. the same name (ignoring titles, plural and word order), unless the titles say otherwise;
        2. for things, a known name of two or more words found whole inside this one;
        3. for people: a first name or fuller form ("Dior" / "Dior LaChance"); then a surname that one
           known person goes by ("Snape" or "Professor Snape" for "Severus Snape", "Hagrid" for
           "Rubeus Hagrid"), unless several people share it as a family name."""
        core = self._core(name)
        if not core:
            return None
        ids = self._ids()
        same = [eid for eid in ids if any((self._core(n) == core or merge.same_words(self._core(n), core))
                                          and merge.compatible(name, n) for n in self._names(eid))]
        if len(same) == 1:
            return same[0]
        if len(same) > 1:
            typed = [eid for eid in same if self._type(eid) == etype]
            return typed[0] if len(typed) == 1 else same[0]
        if etype not in (None, "person"):
            for eid in ids:
                if self._type(eid) != etype:
                    continue
                for n in self._names(eid):
                    k = self._core(n)
                    if len(k) >= 2 and any(core[i:i + len(k)] == k for i in range(len(core) - len(k) + 1)):
                        return eid
            return None
        people = [eid for eid in ids if self._type(eid) == "person"]
        family = merge.family_names([{"entities": [{"id": x, "type": "person", "names": [{"value": n} for n in self._names(x)]}
                                                   for x in people]}])
        if len(core) == 1:
            hits = [eid for eid in people if any(self._core(n)[-1:] == core and len(self._core(n)) > 1 and merge.compatible(name, n)
                                                 for n in self._names(eid))]
            if len(hits) == 1 and core[0] not in family:
                return hits[0]
        # A fuller or shorter form of one known person's name ("Dior" / "Dior LaChance").
        hits = [eid for eid in people if any(merge.compatible(name, n) and not merge.titled_surname(name, n)
                                             and merge.extends(name, n) for n in self._names(eid))]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
        # A full name for someone known so far only by surname ("Professor Snape", "Hagrid").
        if len(core) > 1 and core[-1] not in family:
            hits = [eid for eid in people if any(self._core(n) == core[-1:] and merge.compatible(name, n)
                                                 for n in self._names(eid))]
            if len(hits) == 1:
                return hits[0]
        return None

    # ---- names that fit more than one entity -----------------------------------------------

    def _family(self) -> set[str]:
        people = [eid for eid in self._ids() if self._type(eid) == "person"]
        return merge.family_names([{"entities": [{"id": x, "type": "person", "names": [{"value": n} for n in self._names(x)]}
                                                 for x in people]}])

    def ambiguous(self, name: str, etype: str | None = None) -> tuple[list[str], bool]:
        """The people a name could be when two or more fit: (ids, same_name). Same-name hits share
        a whole name; the others are first names, fuller forms and surnames (a bare or titled surname
        for each known person whose name ends with it, unless it is a family name). Empty when at
        most one fits: match_known covers that."""
        core = self._core(name)
        if etype not in (None, "person") or not core or merge.POSSESSIVE.search(name):
            return [], False
        people = [eid for eid in self._ids() if self._type(eid) == "person"]
        same = [eid for eid in people if any((self._core(n) == core or merge.same_words(self._core(n), core))
                                             and merge.compatible(name, n) for n in self._names(eid))]
        if same:
            return (same, True) if len(same) > 1 else ([], False)
        family = self._family()
        hits: list[str] = []
        if len(core) == 1 and core[0] not in family:
            hits += [eid for eid in people if any(self._core(n)[-1:] == core and len(self._core(n)) > 1
                                                  and merge.compatible(name, n) for n in self._names(eid))]
        hits += [eid for eid in people if any(merge.compatible(name, n) and not merge.titled_surname(name, n)
                                              and merge.extends(name, n) for n in self._names(eid))]
        if len(core) > 1 and core[-1] not in family:
            hits += [eid for eid in people if any(self._core(n) == core[-1:] and merge.compatible(name, n)
                                                  for n in self._names(eid))]
        hits = list(dict.fromkeys(hits))
        return (hits, False) if len(hits) > 1 else ([], False)

    def first_name_candidates(self, name: str, chunk: Chunk) -> list[str]:
        """People in the chunk known only by a title and surname ("Mr. Holmwood") who a new first name
        ("Arthur") might be, when nobody known has that first name yet."""
        core = self._core(name)
        if len(core) != 1 or merge._titles(name) or merge.POSSESSIVE.search(name) or not name[:1].isupper():
            return []
        people = [eid for eid in self._ids() if self._type(eid) == "person"]
        if any(self._core(n)[:1] == core for eid in people for n in self._names(eid)):
            return []
        titled = [eid for eid in self.mentioned(chunk) if eid in people
                  and all(merge._titles(n) and len(self._core(n)) == 1 for n in self._names(eid))]
        return titled[:MAX_DISAMBIGUATE]

    def _linked(self, a: str, b: str) -> bool:
        return any({r["src"], r["dst"]} == {a, b} for r in self.relations)

    def _all_match(self, ids: list[str]) -> bool:
        """Every one of these entities goes by a name that fits every other (not titles of the other sex)."""
        return all(merge.same_names(self._names(a), self._names(b))
                   for n, a in enumerate(ids) for b in ids[n + 1:])

    def _full_names(self, eid: str, bridge: str | None) -> list[tuple[str, ...]]:
        out = []
        for n in self._names(eid):
            c = self._core(n)
            if len(c) >= 2 and merge.proper_name(n) and not (bridge and c == self._core(bridge)):
                out.append(c)
        return out

    def _may_merge(self, ids: list[str], bridge: str | None = None) -> bool:
        """No reason against folding these into one: at most one comes from an earlier book (their files
        are not rewritten), nobody ruled any two different, no relation ties two together, and no
        titles say two of them are different sexes."""
        if sum(1 for i in ids if i in self.prior) > 1:
            return False
        for n, a in enumerate(ids):
            for b in ids[n + 1:]:
                if frozenset((a, b)) in self.opts.distinct or self._linked(a, b):
                    return False
                if not all(merge.compatible(x, y) for x in self._names(a) for y in self._names(b)):
                    return False
                # Two people who each carry a full name with a different surname are two people
                # ("Arthur Holmwood", "Quincey P. Morris"), unless that name is the bridge itself.
                if any(x[-1] != y[-1] for x in self._full_names(a, bridge) for y in self._full_names(b, bridge)):
                    return False
        return True

    def _pick_keep(self, ids: list[str]) -> str:
        """The entity introduced first: an earlier book's, else the one with the earliest first name."""
        earlier = [i for i in ids if i in self.prior]
        if earlier:
            return earlier[0]
        order = {eid: n for n, eid in enumerate(self.entities)}
        return min(ids, key=lambda i: (self.entity_from.get(i, 1 << 30), order.get(i, 0)))

    def _merge_group(self, ids: list[str], at: int) -> str | None:
        """Fold entities that are one individual into the earliest; returns the survivor (None when an
        earlier book's entity could not be brought into this one)."""
        keep = self._pick_keep(ids)
        if keep not in self.entities:
            first = min([at, *(self.entity_from[i] for i in ids if i in self.entity_from)])
            if self._localize(keep, first) is None:
                return None
        for drop in ids:
            if drop != keep:
                self._merge_entities(keep, drop)
        return keep

    def _merge_entities(self, keep: str, drop: str, name: str | None = None) -> None:
        """Fold entity `drop` into `keep` everywhere this run holds ids: names and facets (each keeps its
        own `from`, so nothing shows before the book used it), relations, events, first-mention
        positions and the ids the current chunk still refers to. The survivor's `from` is its earliest
        name; its display name is the best-ranked (merge.rank_names) unless `name` is given."""
        if keep == drop or keep not in self.entities or drop not in self.entities:
            return
        label = self._names(drop)[0]
        doc = {"anchors": [{"id": self.anchors[s]} for s in sorted(self.anchors)],
               "entities": list(self.entities.values()), "relations": self.relations, "events": self.events}
        merge.merge([doc], keep, drop, name)
        self.entities.pop(drop, None)
        self.relations[:] = doc["relations"]
        self.entity_from[keep] = min(self.entity_from.get(keep, self.entity_from[drop]), self.entity_from.pop(drop))
        for k, v in list(self._redirect.items()):
            if v == drop:
                self._redirect[k] = keep
        self._redirect[drop] = keep
        self.added.append({"kind": "merge", "keep": keep, "drop": drop, "name": label,
                           "text": f"{label} is the same as {self._names(keep)[0]}."})

    def _entry(self, letter: str, eid: str) -> str:
        profile = self.current_profile(eid)
        about = [profile["text"]] if profile else [f["text"] for f in self._current_facets(eid)[-3:]]
        nm = lambda x: self._names(x)[0]  # noqa: E731
        rels = [f"{nm(r['src'])} {r['label']} {nm(r['dst'])}" for r in self.relations
                if eid in (r["src"], r["dst"]) and r["src"] in self._ids() and r["dst"] in self._ids()][-5:]
        return (f"{letter}: {'; '.join(self._names(eid))}. " + " ".join(about)
                + (f" Ties: {'; '.join(rels)}." if rels else "")).strip()

    def _ask(self, name: str, cands: list[str], chunk: Chunk, at: int) -> str | None:
        """Ask the model which of these entities a name is: an id, "both", "none", or None when no
        answer was asked for or usable. Sees only the paragraphs around the name and the entries."""
        if self._asked >= MAX_DISAMBIGUATE:
            return None
        self._asked += 1
        letters = [chr(65 + i) for i in range(len(cands))]
        lo, hi = max(chunk.start, at - 1), min(chunk.end, at + 1)
        context = "\n\n".join(self.book.blocks[s].raw for s in range(lo, hi + 1))
        schema = _obj({"choice": {"type": "string", "enum": [*letters, "both", "none"]}}, ["choice"])
        try:
            out = self.llm.complete(SYSTEM, DISAMBIGUATE.format(
                name=name, context=context,
                entries="\n".join(self._entry(c, e) for c, e in zip(letters, cands))), schema)
        except Cancelled:
            raise
        except Exception:
            self.stats["disambiguation failed"] += 1
            return None
        choice = str(out.get("choice", "")).strip() if isinstance(out, dict) else ""
        if choice.upper() in letters:
            return cands[letters.index(choice.upper())]
        if choice.lower() in ("both", "none"):
            return choice.lower()
        self.stats["disambiguation failed"] += 1
        return None

    def _apply_choice(self, cands: list[str], choice: str | None, at: int,
                      bridge: str | None = None) -> tuple[bool, str | None]:
        """Act on the model's answer: (settled, entity). Not settled means fall back to plain matching."""
        if choice in cands:
            self.stats["disambiguated: attached"] += 1
            return True, choice
        if choice == "none":
            self.stats["disambiguated: new"] += 1
            return True, None
        if choice == "both":
            if self._may_merge(cands, bridge):
                keep = self._merge_group(cands, at)
                if keep:
                    self.stats["disambiguated: merged"] += 1
                    return True, keep
            self.stats["disambiguated: attached"] += 1
            return True, cands[0]
        return False, None

    def resolve_name(self, name: str, etype: str | None, chunk: Chunk, at: int) -> str | None:
        """The entity a proposed new name belongs to, or None for a new one. A name that fits several
        people never creates a third: when they all match each other it merges them (a name that
        bridges them proves they are one); otherwise the model is asked which one it is."""
        hits, same = self.ambiguous(name, etype)
        if hits:
            if self._all_match(hits) and self._may_merge(hits, name):
                keep = self._merge_group(hits, at)
                if keep:
                    self.stats["merged by a bridging name"] += 1
                    return keep
            elif not same:
                done, eid = self._apply_choice(hits, self._ask(name, hits, chunk, at), at, name)
                if done:
                    return eid
            return self.match_known(name, etype)
        eid = self.match_known(name, etype)
        if eid is None and etype in (None, "person"):
            cands = self.first_name_candidates(name, chunk)
            if cands:
                done, eid2 = self._apply_choice(cands, self._ask(name, cands, chunk, at), at, name)
                if done:
                    return eid2
        return eid

    def _apply_redirect(self, cands: list[Candidate]) -> None:
        """Point candidates built before a merge at the surviving entity."""
        if not self._redirect:
            return
        to = lambda x: self._redirect.get(x, x)  # noqa: E731
        for c in cands:
            for k in ("entity", "src", "dst"):
                if k in c.data and isinstance(c.data[k], str):
                    c.data[k] = to(c.data[k])
            if isinstance(c.data.get("entities"), list):
                c.data["entities"] = list(dict.fromkeys(to(x) for x in c.data["entities"]))

    def mentioned(self, chunk: Chunk) -> list[str]:
        """Known entities named in the chunk, by any name or, for people, a surname unique among them."""
        words = [w for s in range(chunk.start, chunk.end + 1) for w in self.book.blocks[s].words]
        joined = " " + " ".join(words) + " "
        surnames = Counter(self._core(n)[-1] for eid in self._ids() if self._type(eid) == "person"
                           for n in {self._names(eid)[0]} if len(self._core(n)) > 1)
        out = []
        for eid in self._ids():
            forms = {" ".join(self._core(n)) for n in self._names(eid)}
            if self._type(eid) == "person":
                forms |= {self._core(n)[-1] for n in self._names(eid)
                          if len(self._core(n)) > 1 and surnames[self._core(n)[-1]] == 1}
            if any(f and f" {f} " in joined for f in forms):
                out.append(eid)
        return out

    def _name_kind(self, eid: str, name: str) -> str:
        """"primary" when the new name is now the best one for the entity (merge.rank_names): its actual
        name over a title ("Fabién Voss" over "the Forever King"), a fuller form over a short one
        ("Dior LaChance" over "Dior"). It becomes the display name from where it is revealed."""
        people = [{"id": x, "type": self._type(x), "names": [{"value": n} for n in self._names(x)]} for x in self._ids()]
        best = merge.rank_names([*self._names(eid), name], {}, merge.family_names([{"entities": people}]))[0]
        return "primary" if best == name else "alias"

    def find(self, name: str, chunk: Chunk) -> int | None:
        """The first block of the chunk that uses this name (as written, or without its titles)."""
        targets = [t for t in (text.words(name), list(self._core(name))) if t]
        for seq in range(chunk.start, chunk.end + 1):
            w = self.book.blocks[seq].words
            if any(w[i:i + len(t)] == t for t in targets for i in range(len(w) - len(t) + 1)):
                return seq
        return None

    def earliest_mention(self, name: str, at: int) -> int:
        """The first block, in the part of the book being indexed, that uses this name as a name:
        the exact capitalized form, and a one-word name only mid-sentence ("said Fang", not "Fang
        marks…" at a sentence start, where any word is capitalized)."""
        bare = " ".join(w for w in name.split() if merge._plain(w.rstrip(".").casefold()) not in HONORIFICS) or name
        # A titled name is found only as written: the bare rest ("Harker" in "Mrs. Harker") may be someone else.
        forms = {name} if merge._titles(name) else {name, bare}
        pats = [re.compile((r"(?<=[a-z,;:] )" if len(f.split()) == 1 else r"(?<![\w'’])") + re.escape(f) + r"(?![\w'’])")
                for f in forms if f[:1].isupper()]
        for seq in range(self.scope_start, at):
            if any(p.search(self.book.blocks[seq].raw) for p in pats):
                return seq
        return at

    def first_mention(self, name: str, chunk: Chunk, hint: int) -> int:
        """Correct the model's first-paragraph guess when the name visibly occurs elsewhere in the chunk."""
        target = text.words(name)
        core = list(self._core(name))
        def has(seq: int, t: list[str]) -> bool:
            w = self.book.blocks[seq].words
            return any(w[i:i + len(t)] == t for i in range(len(w) - len(t) + 1))
        for t in ((target,) if merge._titles(name) else (target, core)):
            if t:
                for seq in range(chunk.start, chunk.end + 1):
                    if has(seq, t):
                        return seq
        return hint

    def new_entity_id(self, name: str) -> str:
        base = slug(name)
        eid, n = base, 2
        while eid in self.entities or eid in self.prior:
            eid, n = f"{base}-{n}", n + 1
        return eid

    # ---- prompts ---------------------------------------------------------------------------

    def _passage(self, chunk: Chunk) -> str:
        """Passage with local paragraph numbers 1..n (short, and the same in every call)."""
        return PASSAGE.format(passage="\n\n".join(
            f"[{i + 1}] {self.book.blocks[s].raw}" for i, s in enumerate(range(chunk.start, chunk.end + 1))))

    def _current_facets(self, eid: str) -> list[dict]:
        replaced = {r for e in self.entities.values() for f in e["facets"] for r in f.get("replaces", [])}
        facets = self.prior.get(eid, {}).get("facets", []) + self.entities.get(eid, {}).get("facets", [])
        return [f for f in facets if f["id"] not in replaced]

    def current_profile(self, eid: str) -> dict | None:
        """The entity's latest profile: this book's, else the one an earlier book ended with."""
        mine = [f for f in self.entities.get(eid, {}).get("facets", []) if f.get("kind") == "profile"]
        if mine:
            return mine[-1]
        prior = [f for f in self.prior.get(eid, {}).get("facets", []) if f.get("kind") == "profile"]
        return prior[-1] if prior else None

    def _context(self, chunk: Chunk) -> tuple[str, str]:
        known = self._ids()[-MAX_KNOWN_LIST:]

        def who(eid: str) -> str:
            # People and creatures carry a few words on who they are, so the model can tell that
            # "Uncle Vernon" or "her father" is someone already listed under another name.
            if self._type(eid) not in ("person", "creature", "group"):
                return f"{eid}: {self._names(eid)[0]}"
            about = (self.current_profile(eid) or next(iter(self._current_facets(eid)), None) or {}).get("text", "")
            about = (about[:70].rsplit(" ", 1)[0] + "…") if len(about) > 70 else about
            return f"{eid}: {self._names(eid)[0]}" + (f" ({about})" if about else "")
        known_s = "; ".join(who(eid) for eid in known) or "(nothing yet)"
        lines = []
        for eid in self.mentioned(chunk):
            lines.append(f"- {eid} ({self._type(eid)}; {', '.join(self._names(eid))})")
            profile = self.current_profile(eid)
            if profile:
                lines.append(f"    profile: {profile['text']}")
            for f in [f for f in self._current_facets(eid) if f.get("kind") != "profile"][-MAX_CONTEXT_FACTS:]:
                lines.append(f"    [{f['id']}] {f['text']}")
        return known_s, "\n".join(lines) or "(none)"

    # ---- one chunk -------------------------------------------------------------------------

    def process(self, chunk: Chunk) -> dict:
        """Run one chunk: one extraction call, then checks. Returns the change in stats."""
        t0 = time.time()
        self.added = []
        self._asked, self._redirect = 0, {}
        self.scope_start = min(self.scope_start, chunk.start) if hasattr(self, "scope_start") else chunk.start
        depth = DEPTHS.get(self.opts.depth, DEPTHS["standard"])
        words = sum(len(self.book.blocks[s].words) for s in range(chunk.start, chunk.end + 1))
        n_events = max(2, round(words / 1500)) if self.opts.events else 0
        known, context = self._context(chunk)
        passage = self._passage(chunk)
        self._chunk_len = chunk.end - chunk.start + 1
        self.on_phase("extract")
        raw = self.llm.complete(SYSTEM, EXTRACT.format(
            recap=self.recap_text or "(the book is just beginning)", known=known, context=context,
            previous=self.summary_text or "(none)",
            summary=f"{max(3, min(8, round(words / 700)))} to {max(4, min(12, round(words / 450)))}",
            types="; ".join(f"{k} = {v}" for k, v in TYPES.items()), who=depth["who"],
            per_entity=depth["per_entity"](words), events=n_events, profiles=max(8, min(16, round(words / 300))),   # room for every new entity plus the ones that grew
            relations=("\n- relations: how two entities relate, as a (short label) b, e.g. a=\"Murray\", "
                       "label=\"orderly who saved\", b=\"Watson\". \"p\" = paragraph that shows it."
                       if depth["relations"] else "")),
            extract_schema(depth["relations"]), prefix=passage)
        before = Counter(self.stats)
        n = chunk.end - chunk.start + 1
        # Local paragraph number → block seq; None when the model cites a paragraph that isn't there.
        seq = lambda p: chunk.start + p - 1 if isinstance(p, int) and 1 <= p <= n else None  # noqa: E731

        # New entities and names are structural: applied directly, never fact-checked.
        new_by_name: dict[tuple[str, ...], str] = {}
        couples: dict[tuple[str, ...], list[str]] = {}   # "Lily and James Potter" → both of them
        cands: list[Candidate] = []
        def here(n: str) -> str | None:
            """An entity named earlier in this passage, unless the titles say it's someone else."""
            eid = new_by_name.get(self._core(n))
            eid = self._redirect.get(eid, eid) if eid else eid
            return eid if eid and all(merge.compatible(n, x) for x in self._names(eid)) else None

        proposals = []
        for ne in raw.get("new", []):
            parts = split_couple(clean_name(ne.get("name", ""))) if ne.get("type") == "person" else None
            proposals += [{**ne, "name": part, "_couple": clean_name(ne["name"])} for part in parts] if parts else [ne]
        for i, ne in enumerate(proposals):
            proposed = clean_name(ne.get("name", ""))
            verdict, value = triage_name(ne.get("name", ""), ne.get("type"),
                                         lambda n: here(n) or self.match_known(n, ne.get("type")))
            if verdict == "drop":
                self.stats[f"dropped: {value}"] += 1
                continue
            name = value if verdict == "name" else proposed
            at = seq(ne.get("p"))
            if not name or len(name) > 120 or at is None:
                self.stats["dropped: bad entity"] += 1
                continue
            if verdict == "name" and self.find(ne.get("_couple") or name, chunk) is None:
                # A name the passage never uses comes from outside it (memory of the book or its
                # sequels): it could name someone before the text does.
                self.stats["dropped: not named in the passage"] += 1
                continue
            at = self.first_mention(name, chunk, at)
            if verdict == "known":
                # A description wrapped around someone known ("Celene's brother Gabriel"): that entity.
                eid = value if value in self.entities else self._localize(value, at)
                if not eid:
                    self.stats["dropped: no anchor"] += 1
                    continue
                self.stats["descriptions matched to a known entity"] += 1
            else:
                eid = self.resolve_name(name, ne.get("type"), chunk, at)
                if eid and eid not in self.entities:
                    eid = self._localize(eid, at)
            if verdict == "known":
                pass
            elif eid:
                self._add_name(eid, name, self._name_kind(eid, name), at)
            else:
                # Introduced where the text first uses the name, even if the model only picked it up
                # now: showing a name the reader has already read is never a spoiler.
                at = self.earliest_mention(name, at)
                snapped = self.snap_forward(at)
                if snapped is None:
                    self.stats["dropped: no anchor"] += 1
                    continue
                eid = self.new_entity_id(name)
                etype = ne.get("type") if ne.get("type") in TYPES else "concept"
                self.entities[eid] = {"id": eid, "type": etype, "from": self.anchor(snapped),
                                      "names": [{"value": name, "kind": "primary", "from": self.anchor(snapped)}],
                                      "facets": []}
                self.entity_from[eid] = snapped
                self.stats["entities"] += 1
                self.added.append({"kind": "entity", "id": eid, "name": name, "type": etype})
            new_by_name[self._core(name)] = new_by_name[self._core(proposed)] = eid   # updates may use either
            if ne.get("_couple"):
                couples.setdefault(self._core(ne["_couple"]), []).append(eid)
                new_by_name.setdefault(self._core(ne["_couple"]), eid)
            if str(ne.get("desc", "")).strip():
                desc_at = seq(ne.get("desc_p"))
                desc_at = max(at, desc_at if desc_at is not None else chunk.end)
                c = self._candidate("facet", f"n{i}", ne["desc"], desc_at, [], [eid], 280)
                if c:
                    c.data = {"entity": eid, "basis": "stated", "_desc": True}
                    cands.append(c)

        def resolve(ref: str, at: int | None) -> str | None:
            """An id or name → an entity of this book; returning entities join it at `at`."""
            ref = str(ref or "").strip()
            ref = self._redirect.get(ref, ref)
            eid = ref if (ref in self.entities or ref in self.prior) else (
                here(ref) or self.match_known(ref))
            if eid and eid not in self.entities:
                return self._localize(eid, at) if at is not None else None
            return eid

        def resolve_all(ref: str, at: int | None) -> list[str | None]:
            """Like resolve, but a couple named together ("Mr. and Mrs. Dursley") is both of them."""
            both = couples.get(self._core(str(ref or "")))
            return [self._redirect.get(x, x) for x in both] if both else [resolve(ref, at)]

        for nn in raw.get("names", []):
            at = seq(nn.get("p"))
            eid = resolve(nn.get("id", ""), at)
            if nn.get("name"):
                # The same rules as for a new entity's name; and a possessive ("Mina Harker's Journal") is
                # never another name for someone who is already in the companion.
                verdict, value = triage_name(nn["name"], None, lambda n: here(n) or self.match_known(n))
                if verdict == "drop" or (verdict == "known" and value == eid) or (
                        verdict == "name" and merge.POSSESSIVE.search(value)):
                    self.stats["dropped: description, not a name"] += 1
                    continue
                if verdict == "known":
                    self.stats["dropped: description, not a name"] += 1   # it is another entry, not an alias
                    continue
                nn = {**nn, "name": value}
            if nn.get("name") and self.find(nn["name"], chunk) is None:
                self.stats["dropped: not named in the passage"] += 1
                continue
            if eid and at is not None and nn.get("name"):
                # A claim like any other: the checker must find the passage using this name for this individual.
                at_name = self.first_mention(nn["name"], chunk, at)
                cands.append(Candidate("name", f"nm{len(cands)}", clean_name(nn["name"]), at_name, [at_name],
                                       {"entity": eid}))

        for i, u in enumerate(raw.get("updates", [])):
            at = seq(u.get("p"))
            if at is None:
                self.stats["dropped: citation outside passage"] += 1
                continue
            for k, eid in enumerate(resolve_all(u.get("id", ""), at)):
                c = self._candidate("facet", f"u{i}" + (f"-{k}" if k else ""), u.get("desc", ""), at, [], [eid] if eid else None, 280)
                if c:
                    c.data = {"entity": eid, "basis": "stated", "replaces": str(u.get("corrects") or "").strip("[] ")}
                    cands.append(c)
        chunk_text = " ".join(self.book.blocks[s].raw for s in range(chunk.start, chunk.end + 1))
        # Profiles: one per entity (the last offered), replacing its current profile, whichever
        # book that came from, so a series character keeps one description that grows.
        offered: dict[str, tuple[int, dict, int]] = {}
        for i, pr in enumerate(raw.get("profiles", []) if isinstance(raw.get("profiles"), list) else []):
            at = seq(pr.get("p"))
            if at is None:
                self.stats["dropped: citation outside passage"] += 1
                continue
            for eid in resolve_all(pr.get("id", ""), at):
                if eid is None:
                    self.stats["dropped: unknown entity"] += 1
                    continue
                offered[eid] = (i, pr, at)
        for eid, (i, pr, at) in offered.items():
            if MOMENTARY.search(str(pr.get("text", ""))):
                self.stats["dropped: profile describes the moment"] += 1   # the previous profile stays
                continue
            if unsupported_tie(str(pr.get("text", "")), chunk_text, *(x["text"] for x in self._current_facets(eid)),
                               (self.current_profile(eid) or {}).get("text", "")):
                self.stats["dropped: tie not in the text"] += 1
                continue
            c = self._candidate("facet", f"p{i}", pr.get("text", ""), at, [], [eid], 480)
            if c:
                prev = self.current_profile(eid)
                c.data = {"entity": eid, "basis": "stated", "profile": True, "replaces": prev["id"] if prev else "",
                          "known": [x["text"] for x in ([prev] if prev else [])
                                    + [f for f in self._current_facets(eid) if f.get("kind") != "profile"][-MAX_CONTEXT_FACTS:]]}
                cands.append(c)
        # A new entity's `desc` is a description: with a profile in the same response it is a
        # duplicate and goes; without one it becomes the entity's first profile, not a plain fact.
        profiled = {c.data["entity"] for c in cands if c.data.get("profile")}
        for c in [c for c in cands if c.data.get("_desc")]:
            eid = c.data["entity"]
            if eid in profiled:
                cands.remove(c)
                self.stats["dropped: description duplicates the profile"] += 1
            elif MOMENTARY.search(c.text):
                c.data.pop("_desc")
            elif (tie := unsupported_tie(c.text, chunk_text, *(x["text"] for x in self._current_facets(eid)),
                                         (self.current_profile(eid) or {}).get("text", ""))):
                cands.remove(c)
                self.stats["dropped: tie not in the text"] += 1
            else:
                prev = self.current_profile(eid)
                c.data = {"entity": eid, "basis": "stated", "profile": True, "replaces": prev["id"] if prev else "",
                          "known": [x["text"] for x in ([prev] if prev else [])
                                    + [f for f in self._current_facets(eid) if f.get("kind") != "profile"][-MAX_CONTEXT_FACTS:]]}
                profiled.add(eid)
        for i, r in enumerate(raw.get("relations", []) if depth["relations"] else []):
            at = seq(r.get("p"))
            a, b = resolve(r.get("a", ""), at), resolve(r.get("b", ""), at)
            if at is None:
                self.stats["dropped: citation outside passage"] += 1
                continue
            c = self._candidate("relation", f"r{i}", r.get("label", ""), at, [],
                                [a, b] if a and b and a != b else None, 80)
            if c:
                c.data = {"src": a, "dst": b, "basis": "stated"}
                cands.append(c)
        for i, ev in enumerate(raw.get("events", [])[:n_events]):
            at = seq(ev.get("p"))
            if at is None:
                self.stats["dropped: citation outside passage"] += 1
                continue
            ents = list(dict.fromkeys(e for x in ev.get("who", []) for e in resolve_all(x, at) if e))
            c = self._candidate("event", f"e{i}", ev.get("text", ""), at, [], ents or None, 280)
            if c:
                c.data = {"entities": ents}
                cands.append(c)
        summary = raw.get("summary") if isinstance(raw.get("summary"), list) else []
        for i, sentence in enumerate(summary[:14]):
            # Each sentence is checked on its own, so one bad detail doesn't cost the whole summary.
            c = self._candidate("summary", f"s{i}", sentence, chunk.end, [], [], 300)
            if c:
                cands.append(c)
        recap = " ".join(str(raw.get("recap", "")).split())
        if recap:
            # A recap covers the whole passage, so it is revealed at the passage's end.
            c = self._candidate("recap", "recap", recap, chunk.end, [], [], 1200)
            if c:
                cands.append(c)

        self._apply_redirect(cands)
        cands = self._dedupe(cands)
        cands = self._fix_copies(cands)
        if self.opts.verify and cands:
            self.on_phase("verify")
            cands = self._verify(cands, passage, chunk.start)
        self.summary_parts = []
        for c in cands:
            self._commit(c)
        self._commit_summary(chunk)
        self.done.append(chunk.key)

        delta = {k: v - before.get(k, 0) for k, v in self.stats.items() if v != before.get(k, 0)}
        kept = ", ".join(f"+{delta.get(k, 0)} {k}" for k in ("entities", "facets", "relations", "events"))
        dropped = sum(v for k, v in delta.items() if k.startswith("dropped"))
        self.log(f"  blocks {chunk.start}-{chunk.end}: {kept}; dropped {dropped} ({time.time() - t0:.0f}s)")
        return delta

    def _candidate(self, kind, cid, claim, at_seq, evidence, entities, max_len) -> Candidate | None:
        claim = " ".join(str(claim or "").split())
        if entities is None:
            self.stats["dropped: unknown entity"] += 1
            return None
        if not claim or len(claim) > max_len:
            self.stats["dropped: empty or too long"] += 1
            return None
        if HINDSIGHT.search(claim):
            self.stats["dropped: hindsight wording"] += 1
            return None
        cited = [at_seq, *evidence]
        # Reveal no earlier than the last cited paragraph and the entities involved.
        from_seq = max([*cited, *(self.entity_from[e] for e in entities)])
        return Candidate(kind, cid, claim, from_seq, sorted(set(cited)))

    def _dedupe(self, cands: list[Candidate]) -> list[Candidate]:
        seen = {(e["id"], f["text"].casefold()) for e in self.entities.values() for f in e["facets"]}
        seen |= {ev["summary"].casefold() for ev in self.events}
        seen |= {(r["src"], r["dst"], r["label"].casefold()) for r in self.relations}
        out = []
        for c in cands:
            if c.kind == "summary":   # a summary may restate an event: that's what summaries do
                out.append(c)
                continue
            key = (("name", c.data["entity"], c.text.casefold()) if c.kind == "name"
                   else (c.data["src"], c.data["dst"], c.text.casefold()) if c.kind == "relation"
                   else (c.data["entity"], c.text.casefold()) if c.kind == "facet" else c.text.casefold())
            if key in seen:
                self.stats["dropped: duplicate"] += 1
                continue
            seen.add(key)
            out.append(c)
        return out

    def _copied(self, s: str) -> bool:
        return bool(text.copied_runs(s, self.book_shingles, MAX_COPY))

    def _fix_copies(self, cands: list[Candidate]) -> list[Candidate]:
        bad = [c for c in cands if self._copied(c.text)]
        if bad:
            self.on_phase("rephrase")
            out = self.llm.complete(SYSTEM, REPHRASE.format(
                items="\n".join(f'{c.cid}: "{c.text}"' for c in bad)), REPHRASE_SCHEMA)
            fixes = {i["id"]: " ".join(i["text"].split()) for i in out.get("items", [])}
            for c in bad:
                c.text = fixes.get(c.cid, c.text)
        keep = []
        for c in cands:
            if self._copied(c.text) or HINDSIGHT.search(c.text):
                self.stats["dropped: copies the book"] += 1
            else:
                keep.append(c)
        return keep

    def _claim_text(self, c: Candidate, chunk_start: int) -> str:
        name = lambda eid: self._names(eid)[0]  # noqa: E731
        n = c.from_seq - chunk_start + 1
        where = f"({c.kind}) " if c.kind in ("recap", "summary") else f"({c.kind}, cited at paragraph {n}) "
        if c.kind == "name":
            prof = self.current_profile(c.data["entity"]) or next(iter(self._current_facets(c.data["entity"])), None)
            return (f'(name, cited at paragraph {n}) {name(c.data["entity"])} is also called "{c.text}" in this passage'
                    f'\n    known: {prof["text"] if prof else "nothing yet"}')
        if c.kind == "facet" and c.data.get("profile"):
            known = "; ".join(c.data.get("known") or []) or "nothing yet"
            return f"(profile, cited at paragraph {n}) {name(c.data['entity'])}: {c.text}\n    known: {known}"
        if c.kind == "facet":
            return f"(fact, cited at paragraph {n}) {name(c.data['entity'])}: {c.text}"
        if c.kind == "relation":
            return f"{where}{name(c.data['src'])}, {c.text}, {name(c.data['dst'])}"
        return where + c.text

    def _verify(self, cands: list[Candidate], passage: str, chunk_start: int) -> list[Candidate]:
        claims = "\n".join(f"{c.cid}: {self._claim_text(c, chunk_start)}" for c in cands)
        # Recaps and summaries legitimately draw on earlier chapters: show them, newest kept whole.
        chapters, used = [], 0
        for rc in reversed([r for r in self.recaps if r.get("scope") == "chapter"]):
            line = f"- {rc.get('title') or ''}: {rc['text']}"
            if used + len(line) > EARLIER_CHAPTERS:
                break
            chapters.append(line)
            used += len(line)
        checker = self.opts.check_llm or self.llm
        out = checker.complete(SYSTEM, VERIFY.format(recap=self.recap_text or "(none)", claims=claims,
                                                     chapters="\n".join(reversed(chapters)) or "(none)"),
                               VERIFY_SCHEMA, prefix=passage)
        verdict = out.get("unsupported") if isinstance(out, dict) else None
        if not isinstance(verdict, list):
            # No usable verdict (e.g. a declined request): keep nothing rather than keep unchecked claims.
            self.stats["dropped: not checked"] += len(cands)
            return []
        bad = {str(x).strip() for x in verdict}
        # Facts that narrate a moment are right, but belong among events.
        for cid in {str(x).strip() for x in out.get("moments") or []} - bad:
            c = next((c for c in cands if c.cid == cid and c.kind == "facet" and not c.data.get("profile")), None)
            if c:
                c.kind, c.data = "event", {"entities": [c.data["entity"]]}
                self.stats["facts moved to events"] += 1
        n = self._chunk_len
        for r in out.get("later") or []:
            c = next((c for c in cands if c.cid == str(r.get("id", "")).strip()), None)
            p = r.get("p")
            if c and c.cid not in bad and isinstance(p, int) and 1 <= p <= n:
                new_seq = chunk_start + p - 1
                if new_seq > c.from_seq:   # only ever later: moving a reveal later is spoiler-safe
                    c.from_seq = new_seq
                    c.evidence = sorted({*c.evidence, new_seq})
                    self.stats["moved later by fact-check"] += 1
        keep = [c for c in cands if c.cid not in bad]
        for c in cands:
            if c.cid in bad and len(self.rejected) < 500:
                self.rejected.append({"kind": c.kind, "claim": self._claim_text(c, chunk_start)})
        self.stats["dropped: not supported"] += len(cands) - len(keep)
        return keep

    def chapter_title(self, chunk: Chunk) -> str:
        return self.book.chapters[chunk.chapter].title if chunk.chapter >= 0 else "Opening"

    def _commit_summary(self, chunk: Chunk) -> None:
        """The checked summary sentences become this passage's chapter recap, revealed at its end."""
        parts, text_ = self.summary_parts, ""
        for part in parts:
            if len(text_) + len(part) + 1 > 2000:
                break
            text_ = f"{text_} {part}".strip()
        from_seq = self.snap_forward(chunk.end)
        if not text_ or from_seq is None:
            return
        self.recaps.append({"id": self._next_id(f"{self.key}.chapter"), "from": self.anchor(from_seq),
                            "text": text_, "evidence": [self.anchor(from_seq)],
                            "scope": "chapter", "title": self.chapter_title(chunk)[:120]})
        self.summary_text = text_
        self.stats["chapter recaps"] += 1

    def _add_name(self, eid: str, name: str, kind: str, seq: int) -> None:
        e = self.entities[eid]
        if any(n["value"].casefold() == name.casefold() for n in e["names"]):
            return
        mine = self._core(name)
        if any(o != eid and any(self._core(n) == mine and merge.compatible(name, n) for n in self._names(o))
               for o in self._ids() if self._type(o) == e["type"]):
            self.stats["dropped: name belongs to another entry"] += 1
            return
        words = text.words(name)
        if len(words) == 1 and e["type"] == "person" and words[0] in self._family():
            # "Harker" is Jonathan and Mina alike: a family name says nothing about who this is.
            self.stats["dropped: family surname, not a name"] += 1
            return
        snapped = self.snap_forward(max(seq, self.entity_from[eid]))
        if snapped is None:
            return
        e["names"].append({"value": name, "kind": kind if kind in ("alias", "title", "nickname", "primary")
                           else "alias", "from": self.anchor(snapped)})

    def _next_id(self, prefix: str) -> str:
        self.counters[prefix] += 1
        return f"{prefix}.{self.counters[prefix]}"

    def _commit(self, c: Candidate) -> None:
        from_seq = self.snap_forward(c.from_seq)
        if from_seq is None:
            self.stats["dropped: no anchor"] += 1
            return
        if c.kind == "name":
            e = c.data["entity"]
            if e in self.entities:
                self._add_name(e, c.text, self._name_kind(e, c.text), c.from_seq)
            return
        evidence = [self.anchor(s) for s in sorted({self.snap_forward(e) for e in c.evidence} - {None})]
        if c.kind == "summary":
            self.summary_parts.append(c.text)
            return
        if c.kind == "recap":
            self.recaps.append({"id": self._next_id(f"{self.key}.recap"), "from": self.anchor(from_seq),
                                "text": c.text, "evidence": evidence})
            self.recap_text = c.text
            self.stats["recaps"] += 1
            return
        item = {"from": self.anchor(from_seq), "basis": c.data.get("basis", "stated"), "evidence": evidence}
        replaces = c.data.get("replaces")
        if replaces and ((self.item_kind.get(replaces) == c.kind and self.item_seq[replaces] <= from_seq)
                         or self.prior_items.get(replaces) == c.kind):   # earlier books always come first
            item["replaces"] = [replaces]
        if c.kind == "facet":
            eid = c.data["entity"]
            profile = {"kind": "profile"} if c.data.get("profile") else {}
            item = {"id": self._next_id(f"{self.key}.{eid}"), **profile, **item, "text": c.text}
            self.entities[eid]["facets"].append(item)
            self.item_seq[item["id"]], self.item_kind[item["id"]] = from_seq, "facet"
            self.added.append({"kind": "facet", "id": item["id"], "entity": eid,
                               "name": self._names(eid)[0], "text": c.text})
            self.stats["profiles" if profile else "facets"] += 1
        elif c.kind == "relation":
            item = {"id": self._next_id(f"{self.key}.rel"), "src": c.data["src"], "dst": c.data["dst"],
                    "label": c.text, **item}
            self.relations.append(item)
            self.item_seq[item["id"]], self.item_kind[item["id"]] = from_seq, "relation"
            self.added.append({"kind": "relation", "id": item["id"], "src": self._names(c.data["src"])[0],
                               "dst": self._names(c.data["dst"])[0], "text": c.text})
            self.stats["relations"] += 1
        else:
            self.events.append({"id": self._next_id(f"{self.key}.ev"), "from": item["from"], "summary": c.text,
                                "entities": c.data["entities"], "evidence": item["evidence"]})
            self.added.append({"kind": "event", "id": self.events[-1]["id"], "text": c.text})
            self.stats["events"] += 1

    # ---- output ----------------------------------------------------------------------------

    def index(self, complete: bool, through_seq: int) -> dict:
        through = self.snap_back(through_seq)
        item_seqs = [self.seq_of[a] for a in self._all_from()]
        through = max([s for s in [through, *item_seqs] if s is not None])
        self.anchor(through)
        anchors = [{"id": self.anchors[s], "fp": self.book.blocks[s].fingerprint, "seq": s}
                   for s in sorted(self.anchors) if s <= through]
        epub_bytes = self.epub_path.read_bytes()
        doc = {
            "lorekeeper": "0.1",
            "kind": "index",
            "id": self.opts.index_id or (f"{self.opts.series_id}.{self.key}" if self.opts.series_id else self.key),
            "work": {"title": self.book.title or self.key, "authors": self.book.authors or ["Unknown"],
                     "language": self.book.language or "und"},
            "editions": [{"textHash": text.text_hash(b.words for b in self.book.blocks),
                          "fileHash": hashlib.sha256(epub_bytes).hexdigest(),
                          "label": self.epub_path.name, "builtFrom": True}],
            "coverage": {"complete": complete, "through": self.anchors[through]},
            "provenance": {
                "generator": {"name": "lorekeeper-generate", "version": GENERATOR_VERSION,
                              "model": self.llm.name, "promptSet": PROMPT_VERSION},
                "contributors": [{"name": f"{self.llm.name} via lorekeeper-generate", "role": "generator"}],
                "review": [{"by": "lorekeeper-generate", "date": self.created[:10], "status": "unreviewed"}],
                "created": self.created,
                "updated": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "license": self.opts.license,
            },
            "anchors": anchors,
            "entities": sorted(self._kept_entities(), key=lambda e: (self.entity_from[e["id"]], e["id"])),
            "relations": self.relations,
            "events": self.events,
            "recaps": self.recaps,
        }
        if self.opts.series_id:
            doc["series"] = {"id": self.opts.series_id, "book": self.key}
        return doc

    def _kept_entities(self) -> list[dict]:
        """Entities worth publishing: anything described or referenced; at deep depth, everything."""
        if self.opts.depth == "deep":
            return list(self.entities.values())
        used = {r[k] for r in self.relations for k in ("src", "dst")} | {x for ev in self.events for x in ev["entities"]}
        return [e for e in self.entities.values() if e["facets"] or e["id"] in used]

    def _all_from(self) -> list[str]:
        kept = self._kept_entities()
        out = [e["from"] for e in kept]
        out += [n["from"] for e in kept for n in e["names"]]
        out += [f["from"] for e in kept for f in e["facets"]]
        out += [x["from"] for x in [*self.relations, *self.events, *self.recaps]]
        return out

    # ---- resume ----------------------------------------------------------------------------

    def state(self) -> dict:
        return {"done": self.done, "recap": self.recap_text, "summary": self.summary_text, "stats": dict(self.stats),
                "counters": dict(self.counters), "created": self.created,
                "entity_from": self.entity_from, "item_seq": self.item_seq, "item_kind": self.item_kind}

    def restore(self, index: dict, state: dict) -> None:
        self.anchors = {a["seq"]: a["id"] for a in index["anchors"]}
        self.seq_of = {v: k for k, v in self.anchors.items()}
        self.entities = {e["id"]: e for e in index["entities"]}
        self.relations, self.events, self.recaps = index["relations"], index["events"], index["recaps"]
        self.done, self.recap_text = state["done"], state["recap"]
        self.summary_text = state.get("summary", "")
        self.stats, self.counters = Counter(state["stats"]), Counter(state["counters"])
        self.created, self.entity_from = state["created"], state["entity_from"]
        self.item_seq, self.item_kind = state["item_seq"], state["item_kind"]


def run(book: Book, llm: LLM, opts: Options, epub_path: str | Path, out: str | Path,
        resume: bool = False, log: Callable[[str], None] = print,
        on_event: Callable[[dict], None] | None = None,
        should_stop: Callable[[], bool] | None = None) -> dict:
    """Generate (or resume) an index, writing it and a sidecar state file after every chunk.

    on_event receives structured progress for UIs (start, chunk, phase, chunk_done, recap, stopped).
    should_stop is polled between chunks; a stopped run can be continued with resume=True.
    """
    emit = on_event or (lambda e: None)
    out = Path(out)
    state_path = out.with_suffix(out.suffix + ".state")
    gen = Generator(book, llm, opts, epub_path, log)
    gen.on_phase = lambda phase: emit({"type": "phase", "phase": phase})
    if should_stop is not None and hasattr(llm, "cancel"):
        llm.cancel = should_stop   # stop mid-call, not just between chunks
    if resume and out.exists() and state_path.exists():
        gen.restore(json.loads(out.read_text()), json.loads(state_path.read_text()))
        log(f"resuming: {len(gen.done)} chunks already done")

    chunks = gen.chunks()
    if not chunks:
        raise ValueError("nothing to process: check --chapters against `lorekeeper toc`")
    gen.scope_start = chunks[0].start   # names are introduced no earlier than the indexed text
    full_book = opts.chapters is None or opts.whole_story
    total_t = time.time()
    words = lambda c: sum(len(book.blocks[s].words) for s in range(c.start, c.end + 1))  # noqa: E731
    usage = getattr(llm, "usage", None)
    emit({"type": "start", "chunks": len(chunks), "done": sum(c.key in gen.done for c in chunks),
          "words": sum(words(c) for c in chunks), "words_done": sum(words(c) for c in chunks if c.key in gen.done)})
    stopped = False
    for i, chunk in enumerate(chunks):
        if chunk.key in gen.done:
            continue
        if should_stop and should_stop():
            stopped = True
            break
        title = book.chapters[chunk.chapter].title if chunk.chapter >= 0 else "front matter"
        log(f"[{i + 1}/{len(chunks)}] {title}")
        emit({"type": "chunk", "index": i, "chunks": len(chunks), "chapter": title,
              "start": chunk.start, "end": chunk.end, "words": words(chunk)})
        t0 = time.time()
        stats_before = (gen.stats["recaps"], gen.stats["chapter recaps"])
        try:
            delta = gen.process(chunk)
        except Cancelled:
            # Nothing from this chunk was written; resume redoes it from the last checkpoint.
            stopped = True
            break
        added = gen.added
        if (gen.stats["recaps"], gen.stats["chapter recaps"]) != stats_before:
            emit({"type": "recap", "chapter": title, "text": gen.recap_text,
                  "summary": gen.summary_text if gen.stats["chapter recaps"] > stats_before[1] else None})
        complete = full_book and i + 1 == len(chunks)
        doc = gen.index(complete, chunk.end)
        out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        state_path.write_text(json.dumps(gen.state(), indent=2))
        emit({"type": "chunk_done", "index": i, "chunks": len(chunks), "seconds": round(time.time() - t0, 1),
              "words": words(chunk), "delta": delta, "stats": dict(gen.stats), "added": added,
              "usage": dict(usage.__dict__) if usage else None})
    if stopped:
        log("stopped; run again with resume to continue")
        emit({"type": "stopped"})
    if not out.exists():   # stopped before the first chunk finished
        return {"model": llm.name, "chunks": len(chunks), "stopped": True, "seconds": 0,
                "stats": {}, "validation_errors": [], "usage": None}

    doc = json.loads(out.read_text())
    errors = [str(e) for e in validate.check_index(doc) + validate.check_against_book(doc, book.blocks)
              if e.level == "error"]
    usage = getattr(llm, "usage", None)
    report = {"model": llm.name, "chunks": len(chunks), "stopped": stopped,
              "seconds": round(time.time() - total_t),
              "stats": dict(gen.stats), "validation_errors": errors, "rejected": gen.rejected,
              "usage": usage.__dict__ if usage else None}
    out.with_suffix(out.suffix + ".report").write_text(json.dumps(report, indent=2) + "\n")
    return report
