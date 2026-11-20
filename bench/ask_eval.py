"""Try Ask on real indexes: the slice size, the answer, its cites and notes for each question.

    uv run python bench/ask_eval.py examples/sherlock-holmes/*-*.json --series examples/sherlock-holmes/series.json \
        --book sign-of-four --at 0.5 --provider ollama "Who is Watson?" "What happened in the first book?"

A cloud provider needs its extra: `uv run --extra claude python bench/ask_eval.py ... --provider claude --model claude-haiku-5-5`
(the key comes from ANTHROPIC_API_KEY or the provider's variable; never pass it on the command line).

Each question after the first is a follow-up of the previous one (its answer and cites are the history);
--fresh asks every question with no history. --at is a fraction of the way through the book's anchors (default 1.0, the end). With no --provider
only the slice is built and printed (no model call). Keys come from the provider's environment variable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lorekeeper import ask, llm
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("index", nargs="+", type=Path)
    ap.add_argument("--series", type=Path)
    ap.add_argument("--book", help="book key to read in (default: the last index)")
    ap.add_argument("--at", type=float, default=1.0)
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--show-slice", action="store_true")
    ap.add_argument("--roster", type=int, help="roster size in characters for questions that name nobody "
                    "(default: ask.ROSTER_CHARS for a cloud provider, 0 for ollama)")
    ap.add_argument("--fresh", action="store_true", help="no history between questions")
    ap.add_argument("questions", nargs="*")
    a = ap.parse_args()
    idx = [json.loads(p.read_text()) for p in a.index]
    series = json.loads(a.series.read_text()) if a.series else None
    keys = [(i.get("work", {}).get("title"), i["id"].split(".")[-1]) for i in idx]
    book = a.book or keys[-1][1]
    cur = next(i for i in idx if i["id"].split(".")[-1] == book or i["id"] == book)
    n = len(cur["anchors"])
    anchor = cur["anchors"][max(0, min(n - 1, round(a.at * n) - 1))]["id"] if a.at < 1.0 or n else None
    v = view(idx, Position(book if series else None, anchor), series)
    books = None
    if series:
        order = series["orders"][series["defaultOrder"]]
        titles = {b["key"]: b["title"] for b in series["books"]}
        books = {k: {"title": titles.get(k, k), "number": i + 1} for i, k in enumerate(order)}
    model = None
    if a.provider:
        model = llm.make(a.provider, a.model, api_key=None)
    budget = a.budget or (ask.OLLAMA_BUDGET if a.provider == "ollama" else ask.DEFAULT_BUDGET)
    print(f"view at {anchor}: {len(v['entities'])} entries, {len(v['relations'])} connections, "
          f"{len(v['events'])} events, {len(v['chapters'])} chapter recaps", file=sys.stderr)
    roster = a.roster if a.roster is not None else (0 if a.provider in (None, "ollama") else ask.ROSTER_CHARS)
    history: list[ask.Turn] = []
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
        rec = _Recorder(model)
        r = ask.answer(v, q, rec, history, budget, books, roster)
        if rec.picked is not None:
            print(f"  picked: {rec.picked}")
        print(r.answer)
        print(f"  found={r.found} note={r.note!r} slice={r.context_chars} chars usage={r.usage}")
        for c in r.cites:
            print(f"  [{c['kind']}] {c['id']}: {c['label']}")
        history.append(ask.Turn(q, r.answer, [c["id"] for c in r.cites]))


if __name__ == "__main__":
    main()
