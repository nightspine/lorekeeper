"""What kind of book is this? fiction, non-fiction or self-help, guessed from the EPUB.

Order: dc:subject terms, then chapter titles, then (when a model is given) one cheap call over
the table of contents and the opening, then the default, fiction. Python decides; the model only
answers a closed question and a bad answer falls through to the default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .epub import Book

KINDS = ("fiction", "non-fiction", "self-help")

# Lowercase substrings looked for in each dc:subject term. Extend these; do not special-case a book.
FICTION = ("fiction", "novel", "stories", "short stories", "fantasy", "science fiction", "horror", "romance",
           "mystery", "detective", "thriller", "fairy tale", "gothic", "adventure", "vampires", "ghost",
           "western", "humorous", "satire", "epistolary", "folklore", "legends", "plays", "drama", "poetry")
NON_FICTION = ("history", "biography", "autobiography", "memoir", "science", "philosophy", "politics",
               "economics", "religion", "essays", "natural selection", "evolution", "biology", "physics",
               "mathematics", "psychology", "sociology", "travel", "geography", "law", "education",
               "business", "reference", "criticism", "theology", "anthropology", "nature")
SELF_HELP = ("self-help", "self help", "self-improvement", "self improvement", "self-actualization",
             "personal development", "motivation", "success", "habits", "productivity", "conduct of life",
             "happiness", "character", "thrift", "mindfulness", "self-management", "self-culture")

# Chapter-title words (whole words, lowercase).
NON_FICTION_TITLES = ("preface", "introduction", "bibliography", "notes", "index", "references", "appendix",
                      "glossary", "foreword", "acknowledgements", "acknowledgments", "conclusion", "abstract")
SELF_HELP_TITLES = ("exercise", "exercises", "practice", "step", "habit", "habits", "principle", "principles",
                    "rule", "rules", "lesson", "lessons", "week", "tip", "tips", "action")

_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
                 "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
                 "thirty", "forty", "fifty", "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
                 "eighth", "ninth", "tenth", "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}

MODEL_WORDS = 1500
MODEL_SYSTEM = ("You classify books. Answer with the kind that fits best: fiction (a story: novel, tales, plays), "
                "non-fiction (history, science, biography, essays, argument) or self-help (advice for the reader's "
                "own life, habits, exercises). Give a short reason.")
MODEL_SCHEMA = {"type": "object", "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                                                 "why": {"type": "string"}}, "required": ["kind", "why"]}


@dataclass(frozen=True)
class Inference:
    kind: str
    basis: str       # subjects | structure | model | default | chosen
    why: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "basis": self.basis, "why": self.why}


def _hits(term: str, needles: tuple[str, ...]) -> list[str]:
    t = term.lower()
    return [n for n in needles if re.search(r"(?<![a-z])" + re.escape(n), t)]


def _from_subjects(subjects: list[str]) -> Inference | None:
    found: dict[str, list[str]] = {k: [] for k in KINDS}
    for s in subjects:
        for kind, words in (("fiction", FICTION), ("non-fiction", NON_FICTION), ("self-help", SELF_HELP)):
            if _hits(s, words):
                found[kind].append(s)
    # "History -- Fiction" is a novel; "Self-help" beats a generic subject such as "Success".
    for kind in ("fiction", "self-help", "non-fiction"):
        if found[kind]:
            return Inference(kind, "subjects", f"the book's subjects include {', '.join(repr(x) for x in found[kind][:3])}")
    return None


def _words(title: str) -> list[str]:
    return re.findall(r"[a-z]+", title.lower())


def _plain(title: str) -> bool:
    """"Chapter 4", "IV", "Chapter Twelve": a number and nothing else."""
    w = re.findall(r"[a-z0-9]+", title.lower())
    if w and w[0] in ("chapter", "part", "book", "section"):
        w = w[1:]
    return bool(w) and len(w) <= 2 and all(x.isdigit() or x in _NUMBER_WORDS or re.fullmatch(r"[ivxlc]+", x) for x in w)


def _from_titles(titles: list[str]) -> Inference | None:
    nf = [t for t in titles if set(_words(t)) & set(NON_FICTION_TITLES)]
    sh = [t for t in titles if set(_words(t)) & set(SELF_HELP_TITLES)]
    # Introductions and notes also frame novels, so a lean needs more than the usual front matter.
    if len(sh) >= 2 or (sh and len(sh) > len(nf)):
        return Inference("self-help", "structure", f"chapter titles such as {', '.join(repr(x) for x in sh[:3])}")
    if len(nf) >= 2:
        return Inference("non-fiction", "structure", f"chapter titles such as {', '.join(repr(x) for x in nf[:3])}")
    plain = [t for t in titles if _plain(t) and not set(_words(t)) & (set(NON_FICTION_TITLES) | set(SELF_HELP_TITLES))]
    if len(titles) >= 2 and len(plain) >= 0.8 * len(titles):
        return Inference("fiction", "structure", "numbered chapters with no headings of an argument or a course")
    return None


def _from_model(book: Book, llm) -> Inference | None:
    titles = [c.title for c in book.chapters][:60]
    story = [c for c in book.chapters if c.title][:2] or book.chapters[:2]
    ends = [c.start for c in book.chapters[1:]] + [len(book.blocks)]
    bounds = {c.start: e for c, e in zip(book.chapters, ends)}
    opening: list[str] = []
    for c in story:
        n = 0
        for b in book.blocks[c.start:bounds[c.start]]:
            if n >= MODEL_WORDS:
                break
            opening.append(b.raw)
            n += len(b.words)
    prompt = (f"Title: {book.title}\nAuthors: {', '.join(book.authors)}\n\nTable of contents:\n"
              + "\n".join(f"- {t}" for t in titles) + "\n\nOpening:\n" + "\n\n".join(opening))
    try:
        out = llm.complete(MODEL_SYSTEM, prompt, MODEL_SCHEMA)
    except Exception:  # noqa: BLE001  any backend failure means "no answer"
        return None
    kind = out.get("kind") if isinstance(out, dict) else None
    if kind not in KINDS:
        return None
    why = out.get("why")
    return Inference(kind, "model", why.strip() if isinstance(why, str) and why.strip() else "the model's reading of the opening")


def infer(book: Book, llm=None) -> Inference:
    """Subjects, then chapter titles, then one model call (if llm), then fiction."""
    found = _from_subjects(book.subjects) or _from_titles([c.title for c in book.chapters])
    if found:
        return found
    if llm is not None:
        found = _from_model(book, llm)
        if found:
            return found
    return Inference("fiction", "default", "nothing in the subjects or chapter titles says otherwise; the usual guess")
