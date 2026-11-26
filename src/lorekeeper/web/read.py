"""The book's own text, chapter by chapter, for the Reader's reading view.

Only text of the EPUB the person holds, in the chapter map the Reader already uses (`jobs.describe`: chapter `n`
is the 1-based position among the EPUB's table-of-contents entries). Nothing from the index goes in a reply: no
anchors, names or reveal points. The browser joins the text with the index it already holds and gates there.
"""

from __future__ import annotations

from .. import epub

HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


def chapter_spans(book: epub.Book) -> list[dict]:
    """`{n, title, firstSeq, lastSeq, words}` for every chapter, covering every block once.

    Blocks before the first table-of-contents entry (a title page the contents does not list) belong to chapter 1,
    so all of the book can be read; a book with no table of contents is one chapter titled after the book."""
    total = len(book.blocks)
    if not total:
        return []
    marks = [(c.title, c.start) for c in book.chapters] or [(book.title or "The book", 0)]
    out = []
    for i, (title, start) in enumerate(marks):
        first = 0 if i == 0 else start
        last = (marks[i + 1][1] if i + 1 < len(marks) else total) - 1
        out.append({"n": i + 1, "title": title or f"Section {i + 1}", "firstSeq": first, "lastSeq": last,
                    "words": sum(len(b.words) for b in book.blocks[first:last + 1])})
    return out


def chapter_of(spans: list[dict], seq: int) -> dict | None:
    return next((c for c in spans if c["firstSeq"] <= seq <= c["lastSeq"]), None)


def brief(c: dict | None) -> dict | None:
    return {"n": c["n"], "title": c["title"]} if c else None


def chapter_body(book: epub.Book, spans: list[dict], c: dict) -> dict:
    i = c["n"] - 1
    return {
        "chapter": {k: c[k] for k in ("n", "title", "firstSeq", "lastSeq")},
        "prev": brief(spans[i - 1]) if i > 0 else None,
        "next": brief(spans[i + 1]) if i + 1 < len(spans) else None,
        "blocks": [{"seq": b.seq, "text": b.raw, "heading": b.tag in HEADINGS}
                   for b in book.blocks[c["firstSeq"]:c["lastSeq"] + 1]],
    }
