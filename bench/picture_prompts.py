"""Write picture prompts for some portraits and chapter plates of an index, on a fresh art set each, and draw nothing.

    set -a; . ./.env; set +a
    uv run python bench/picture_prompts.py INDEX [--epub EPUB] --pick portrait:Darrow@0.17 --pick plate:3 \
        [--provider gemini] [--tier cheap|strong] [--style realism]

`--pick KIND:WHAT[@FRACTION]`: KIND is portrait, place, thing, plate or event; WHAT an entity name (portrait, place,
thing), a chapter number in the index's chapter list (plate, 1 for the first) or an event id (event); FRACTION the position
as a fraction of the index's anchors (default the end). Each pick is written on a fresh, empty art set, as the prompt route
does for a style it has never drawn in, so the seconds, the text calls and the text cost printed per prompt are what a
reader waits for. The same command runs on an `art.py` from before the simplification (it calls `world` first, as that
route did), so it measures before and after.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from lorekeeper import art, epub, llm


class NoImage:
    provider, name = "", "none"

    def draw(self, *a, **k):
        raise RuntimeError("the bench draws nothing")


class Counting:
    """The text model, counting its calls."""

    def __init__(self, model):
        self.model, self.calls, self.lock = model, 0, threading.Lock()

    def complete(self, *a, **k):
        with self.lock:
            self.calls += 1
        return self.model.complete(*a, **k)

    def __getattr__(self, name):
        return getattr(self.model, name)


def entity(doc: dict, want: str, kind: str) -> str | None:
    """The entity named `want` (an exact name first, then a part of one), of a type the kind draws first ("Dracula" for a
    portrait is Count Dracula, not Castle Dracula)."""
    w = want.lower()
    types = {"portrait": ("person", "creature"), "place": ("place",), "thing": ("object",)}.get(kind, ())
    for test in (lambda v: v.lower() == w, lambda v: w in v.lower()):
        for fits in (lambda e: e["type"] in types, lambda e: True):
            hit = next((e["id"] for e in doc["entities"] if fits(e) and any(test(x["value"]) for x in e["names"])), None)
            if hit:
                return hit
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index")
    ap.add_argument("--epub")
    ap.add_argument("--pick", action="append", default=[], required=True)
    ap.add_argument("--provider", default="gemini")
    ap.add_argument("--tier", default="cheap", choices=("cheap", "strong"))
    ap.add_argument("--model")
    ap.add_argument("--style", default="realism")
    a = ap.parse_args(argv)
    doc = json.loads(Path(a.index).read_text())
    spec = llm.PROVIDERS[a.provider]
    model = llm.make(a.provider, a.model or spec[a.tier], api_key=llm.env_key(a.provider))
    bk = epub.read(a.epub) if a.epub else None
    title = doc.get("work", {}).get("title", a.index)
    print(f"# {title}: text model {a.provider}/{model.name}, style {a.style}\n")
    total_s = total_calls = 0
    total_cost = 0.0
    for pick in a.pick:
        kind, _, rest = pick.partition(":")
        what, _, frac = rest.partition("@")
        text = Counting(model)
        tmp = Path(tempfile.mkdtemp(prefix="lk-bench-"))
        il = art.Illustrator([art.Book(doc, bk, art.FileSink(tmp / "art"), None)], text, NoImage(), a.style,
                             log=lambda m: print("   log:", m, file=sys.stderr), workers=1, portraits="all")
        b = il.books[0]
        at = b.anchors[min(len(b.anchors) - 1, max(0, round(float(frac or 1) * (len(b.anchors) - 1))))]["id"]
        if kind == "plate":
            target = b.chapters[int(what) - 1]["title"]
        elif kind == "event":
            target = what
        else:
            target = entity(doc, what, kind) or what
        label = f"{kind} {what}" + (f" at {frac}" if frac else "")
        u = getattr(model, "usage", None)
        tok0 = (getattr(u, "prompt_tokens", 0), getattr(u, "output_tokens", 0))
        c0, t0 = il.text_spent(), time.monotonic()
        try:
            if hasattr(il, "_world_inputs"):        # before: the prompt route wrote the world note first
                il.world(0)
            job = il.job(kind, target, at=at)
            pr = il.make_prompt(job)
        except Exception as e:  # noqa: BLE001 - a bench prints every outcome
            secs = time.monotonic() - t0
            print(f"## {label}: no prompt ({type(e).__name__}: {e}{': ' + e.detail if getattr(e, 'detail', '') else ''}), "
                  f"{secs:.1f} s, {text.calls} calls, ${il.text_spent() - c0:.4f}\n")
            continue
        secs, cost = time.monotonic() - t0, il.text_spent() - c0
        toks = (getattr(u, "prompt_tokens", 0) - tok0[0], getattr(u, "output_tokens", 0) - tok0[1])
        total_s, total_calls, total_cost = total_s + secs, total_calls + text.calls, total_cost + cost
        print(f"## {label} (from {pr.anchor}): {secs:.1f} s, {text.calls} calls, ${cost:.4f} "
              f"({toks[0]} tokens in, {toks[1]} out)")
        print(f"{pr.text}\n")
    print(f"total {total_s:.1f} s, {total_calls} calls, text ${total_cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
