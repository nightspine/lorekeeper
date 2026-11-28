"""Try Ask on real indexes: the slice size, the answer, its cites and notes for each question.

    uv run python bench/ask_eval.py examples/sherlock-holmes/*-*.json --series examples/sherlock-holmes/series.json \
        --book sign-of-four --at 0.5 --provider ollama "Who is Watson?" "What happened in the first book?"

    uv run --extra claude python bench/ask_eval.py examples/dracula/dracula.json --epub corpus/pg345/pg345.epub \
        --at 0.5 --provider claude --model claude-haiku-5-5 --fresh --source all "What did Jonathan see the Count do?"

A cloud provider needs its extra: `uv run --extra claude python bench/ask_eval.py ... --provider claude --model claude-haiku-5-5`
(the key comes from ANTHROPIC_API_KEY or the provider's variable; never pass it on the command line).

Each question after the first is a follow-up of the previous one (its answer and cites are the history);
--fresh asks every question with no history. --at is a fraction of the way through the book: of its anchors without
--epub (default 1.0, the end), of its words with --epub (the Reader's `p`: the block holding word floor(p x words) and
everything after it are not searchable, and the view is gated at the anchor before that block). --epub is given once per
index, in the same order (an index without one gets no book text); an earlier book's text is searchable whole.
--source index|book|both|all chooses what Ask reads (`all` asks each question three times, one per source; the
history then follows the "both" answers). With no --provider only the slice is built and printed (no model call).
--json PATH also writes every answer as JSON lines.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lorekeeper import ask, chapters, epub, llm, locators
from lorekeeper.gating import Position, view


class _Recorder:
    """Wraps the model to print what the roster pick returned."""

    def __init__(self, inner):
        self.inner, self.picked = inner, None

    @property
    def usage(self):
        return getattr(self.inner, "usage", None)

    def complete(self, system, prompt, schema, prefix=None):
        r = self.inner.complete(system, prompt, schema, prefix)
        if schema == ask.PICK_SCHEMA:
            self.picked = (r or {}).get("ids")
        return r


def frontier_seq(book: epub.Book, p: float) -> int:
    """The first block that is not searchable: the block holding word floor(p x total words); every block at p >= 1."""
    if p >= 1:
        return len(book.blocks)
    total = sum(len(b.words) for b in book.blocks)
    done = 0
    for b in book.blocks:
        if done + len(b.words) > p * total:
            return b.seq
        done += len(b.words)
    return len(book.blocks)


def text_book(key: str | None, title: str | None, book: epub.Book, before: int) -> ask.TextBook:
    nums = chapters.numbering([c.title or f"Section {i + 1}" for i, c in enumerate(book.chapters)])
    return ask.TextBook(book=key, title=title, blocks=book.blocks, chapters=book.chapters, numbers=nums, before=before)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("index", nargs="+", type=Path)
    ap.add_argument("--series", type=Path)
    ap.add_argument("--book", help="book key to read in (default: the last index)")
    ap.add_argument("--at", type=float, default=1.0)
    ap.add_argument("--epub", type=Path, action="append", default=[], help="the EPUB of each index, in order")
    ap.add_argument("--source", choices=["index", "book", "both", "all"], default="index")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--show-slice", action="store_true")
    ap.add_argument("--show-prompt", action="store_true", help="print every prompt sent")
    ap.add_argument("--roster", type=int, help="roster size in characters for questions that name nobody "
                    "(default: ask.ROSTER_CHARS for a cloud provider, 0 for ollama)")
    ap.add_argument("--fresh", action="store_true", help="no history between questions")
    ap.add_argument("--json", type=Path, help="append every answer as a JSON line")
    ap.add_argument("questions", nargs="*")
    a = ap.parse_args()
    idx = [json.loads(p.read_text()) for p in a.index]
    series = json.loads(a.series.read_text()) if a.series else None
    keys = [i["id"].split(".")[-1] for i in idx]
    book = a.book or keys[-1]
    ci = next(k for k, i in enumerate(idx) if i["id"].split(".")[-1] == book or i["id"] == book)
    cur = idx[ci]
    epubs = {k: epub.read(p) for k, p in enumerate(a.epub)}
    p_at = None
    if ci in epubs:
        p_at = a.at
        eb = epubs[ci]
        stop = frontier_seq(eb, a.at)
        anchor = locators.position(cur["anchors"], locators.resolve_edition(cur, eb), stop) if stop < len(eb.blocks) \
            else cur["anchors"][-1]["id"]
    else:
        n = len(cur["anchors"])
        anchor = cur["anchors"][max(0, min(n - 1, round(a.at * n) - 1))]["id"] if a.at < 1.0 or n else None
    v = view(idx, Position(book if series else None, anchor), series)
    books = None
    if series:
        order = series["orders"][series["defaultOrder"]]
        titles = {b["key"]: b["title"] for b in series["books"]}
        books = {k: {"title": titles.get(k, k), "number": i + 1} for i, k in enumerate(order)}
    tbooks = []
    for k, eb in sorted(epubs.items()):
        if k > ci:
            continue                                   # a later book's text is never searchable
        key = keys[k] if series else None
        title = (books or {}).get(key, {}).get("title") if key else None
        tbooks.append(text_book(key, title, eb, frontier_seq(eb, a.at) if k == ci else len(eb.blocks)))
    bt = ask.BookText(tbooks) if tbooks else None
    model = None
    if a.provider:
        model = (llm.make("ollama", a.model) if a.provider == "ollama"
                 else llm.make(a.provider, a.model, api_key=llm.env_key(a.provider)))      # as cli._make_model
    budget = a.budget or (ask.OLLAMA_BUDGET if a.provider == "ollama" else ask.DEFAULT_BUDGET)
    where = f"p={p_at}, " if p_at is not None else ""
    print(f"view at {where}{anchor}: {len(v['entities'])} entries, {len(v['relations'])} connections, "
          f"{len(v['events'])} events, {len(v['chapters'])} chapter recaps"
          + (f", {sum(len(b.blocks) for b in bt.books)} blocks of book text" if bt else ""), file=sys.stderr)
    roster = a.roster if a.roster is not None else (0 if a.provider in (None, "ollama") else ask.ROSTER_CHARS)
    sources = ["index", "book", "both"] if a.source == "all" else [a.source]
    history: list[ask.Turn] = []
    total_cost = 0.0
    for q in a.questions:
        print(f"\n> {q}")
        if model is None:
            sl = ask.select(v, q, history, budget, books)
            print(f"  slice {sl.chars} chars, named {sl.named}, unknown {sl.unknown}, {len(sl.tags)} tags")
            if a.show_slice:
                print(sl.text)
            continue
        if a.fresh:
            history = []
        nxt = None
        for src in sources:
            rec = _Recorder(model)
            if a.show_prompt:
                inner = rec.complete

                def show(system, prompt, schema, prefix=None, inner=inner):
                    print("----- prompt -----\n" + prompt + "\n------------------")
                    return inner(system, prompt, schema, prefix)
                rec.complete = show
            r = ask.answer(v, q, rec, history, budget, books, roster, text=bt, source=src)
            u = r.usage or {}
            total_cost += u.get("cost_usd", 0) or 0
            print(f"  [{r.source}] {r.answer}")
            print(f"    found={r.found} note={r.note!r} chars={r.context_chars} passages={r.passages} "
                  f"calls={u.get('calls')} in={u.get('prompt_tokens')} out={u.get('output_tokens')} "
                  f"cost=${u.get('cost_usd', 0):.4f}" + (f" picked={rec.picked}" if rec.picked is not None else ""))
            for c in r.cites:
                if c["kind"] == "passage":
                    print(f"    [passage] {c['id']} seq {c['seq']}-{c['end']}: {c['chapter']}")
                else:
                    print(f"    [{c['kind']}] {c['id']}: {c['label']}")
            if a.json:
                with a.json.open("a") as fh:
                    fh.write(json.dumps({"q": q, "source": r.source, "answer": r.answer, "found": r.found,
                                         "note": r.note, "chars": r.context_chars, "passages": r.passages,
                                         "usage": r.usage, "cites": r.cites}) + "\n")
            if nxt is None or src == "both":
                nxt = ask.Turn(q, r.answer, [c["id"] for c in r.cites])
        history.append(nxt)
    if model is not None:
        print(f"\ntotal ${total_cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
