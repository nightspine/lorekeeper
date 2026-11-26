"""Write the main-cast portrait prompts of an index twice, with the old unknown-name rule and with the name-choosing call,
and draw nothing: how many prompts each drops as "dropped: unknown name in prompt", and over which words.

    set -a; . ./.env; set +a
    uv run --extra claude python bench/name_check.py examples/dracula/dracula.json --epub corpus/pg345/pg345.epub \
        [--provider claude] [--limit N]

The portrait prompts are written by the provider's strong tier and the choosing calls made by its cheap tier, as a run does.
"Before" is the same code with `_names_among` answering that every caught word is a name, which is the rule before the call.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from lorekeeper import art, epub, llm


class NoImage:
    provider, name = "", "none"

    def draw(self, *a, **k):
        raise RuntimeError("the bench draws nothing")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index")
    ap.add_argument("--epub", required=True)
    ap.add_argument("--provider", default="claude")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    doc = json.loads(Path(a.index).read_text())
    spec = llm.PROVIDERS[a.provider]
    key = llm.env_key(a.provider)
    cheap = llm.make(a.provider, spec["cheap"], api_key=key)
    strong = llm.make(a.provider, spec.get("strong") or spec["cheap"], api_key=key)
    bk = epub.read(a.epub)
    caught: list[str] = []
    il = art.Illustrator([art.Book(doc, bk, art.FileSink(Path(tempfile.mkdtemp(prefix="lk-names-")) / "art"), None)],
                         cheap, NoImage(), "realism", portraits="main", workers=1, cheap_text=cheap, portrait_text=strong,
                         log=lambda m: print("   log:", m, file=sys.stderr))
    asked = il._names_among

    def watched(text, flagged):
        names = asked(text, flagged)
        caught.extend(f"{w}{'' if w in names else ' (kept)'}" for w in flagged)
        return names

    jobs = il._portrait_jobs(0)[: a.limit]
    print(f"# {doc.get('work', {}).get('title')}: {len(jobs)} portrait jobs, writer {strong.name}, picker {cheap.name}\n")
    drops = {"before": 0, "after": 0}
    for job in jobs:
        name = il.books[0].ents[job.target]["names"][0]["value"]
        for mode in ("before", "after"):
            caught.clear()
            il._names_among = (lambda text, flagged: caught.extend(flagged) or list(flagged)) if mode == "before" else watched
            try:
                il.make_prompt(job)
                out = "written"
            except art.Dropped as e:
                out = f"{e} ({e.detail})"
                drops[mode] += "unknown name" in str(e)
            except art.TextFailed as e:
                out = f"{e} ({e.detail})"
            print(f"{name} at {job.anchor}, {mode}: {out}" + (f"; caught {', '.join(caught)}" if caught else ""))
    print(f"\nunknown-name drops: before {drops['before']}, after {drops['after']} of {len(jobs)}")
    print("report:", dict(il.report.dropped), f"text ${il.text_spent():.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
