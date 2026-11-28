"""Does every scene prompt call every figure it draws by its label? Writer calls only, no image calls.

    set -a; . ./.env; set +a
    git show main:src/lorekeeper/art.py > /tmp/art_main.py      # the code to compare against
    uv run --extra claude python bench/figure_labels.py INDEX --epub EPUB [INDEX --epub EPUB] --out DIR \
        [--old /tmp/art_main.py] [--scenes 10] [--plates 3] [--pictured 3] [--cap 0.50]

For each index the script picks `--scenes` scenes (`--plates` chapter pictures, the rest events), spread over the book,
whose moment names two or more people or creatures, and seeds an in-memory art set with a portrait record (no file, no
image call) for the `--pictured` people those scenes name most, at their first profile, so some figures have a portrait
and some do not. It writes each prompt once with the branch's `art.py` and, with `--old`, once with that file (main's),
with Claude's cheap model as writer and picker, and stops a side once its text cost passes `--cap`. DIR gets
`results.json` (per prompt: the figures shown, labelled and pictured, the word count, the text) and `summary.txt`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path

from lorekeeper import art as new_art, epub, llm


class Mem:
    def __init__(self, m):
        self.m, self.files = m, {}

    def manifest(self):
        return self.m

    def save(self, manifest):
        self.m = json.loads(json.dumps(manifest))

    def put(self, file, data):
        self.files[file] = data

    def read(self, file):
        return self.files.get(file)


def load_old(path: str):
    spec = importlib.util.spec_from_file_location("lorekeeper.art_main", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lorekeeper.art_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def pick(doc: dict, bk, n_scenes: int, n_plates: int) -> list[tuple[str, str]]:
    """(kind, target) of the scenes: plates of chapters whose main moment names 2+ figures, then events naming 2+, evenly."""
    types = {e["id"]: e["type"] for e in doc["entities"]}
    il = new_art.Illustrator([new_art.Book(doc, bk, Mem(new_art.blank(doc["editions"][0]["textHash"], "ink")))],
                             None, None, "ink", portraits=None, workers=1, log=lambda *_: None)
    b = il.books[0]
    many = lambda ev: sum(types.get(x) in new_art.PORTRAIT_TYPES for x in ev["entities"]) >= 2     # noqa: E731
    plates = []
    for ch in b.chapters:
        evs = [e for e in ch["events"] if e.get("kind") != "minor"]
        main = next((e for e in evs if e.get("kind") == "turn"), None) or (evs[0] if evs else None)
        if main and many(main):
            plates.append(("plate", ch["title"]))
    events = [e for e in sorted(doc["events"], key=lambda e: b.ord_of.get(e["from"], 10 ** 9))
              if e.get("kind") != "minor" and e["from"] in b.ord_of and many(e)]
    spread = lambda xs, k: [xs[round(i * (len(xs) - 1) / max(1, k - 1))] for i in range(min(k, len(xs)))] if xs else []  # noqa: E731
    out = spread(plates, n_plates)
    out += [("event", e["id"]) for e in spread(events, n_scenes - len(out))]
    return list(dict.fromkeys(out))


def seed(doc: dict, scenes, n: int) -> list[dict]:
    """Portrait records (no file) for the `n` people the chosen scenes name most, each at their first profile."""
    types = {e["id"]: e["type"] for e in doc["entities"]}
    evs = {e["id"]: e for e in doc["events"]}
    count = Counter(x for k, t in scenes if k == "event" for x in evs[t]["entities"] if types.get(x) in new_art.PORTRAIT_TYPES)
    recs = []
    for eid, _ in count.most_common(n):
        ent = next(e for e in doc["entities"] if e["id"] == eid)
        prof = [f for f in ent.get("facets", []) if f.get("kind") == "profile"] or ent.get("facets", [])
        at = prof[0]["from"] if prof else ent["from"]
        recs.append({"id": f"pseed{len(recs):03d}", "kind": "portrait", "target": eid, "from": at, "file": f"pseed{len(recs):03d}.png",
                     "mime": "image/png", "prompt": "seeded", "references": [], "sources": [at], "inherits": [], "cost": 0,
                     "notes": new_art.NOTE_VERSION, "created": "2026-11-27T00:00:00Z"})
    return recs


def side(mod, doc, bk, scenes, recs, text, cap: float) -> dict:
    sink = Mem({**mod.blank(doc["editions"][0]["textHash"], "ink"), "pictures": [dict(r) for r in recs]})
    il = mod.Illustrator([mod.Book(doc, bk, sink)], text, None, "ink", portraits=None, workers=1, log=lambda *_: None)
    calls = {"n": 0}
    orig = il._complete

    def counting(prompt, schema=mod.SCHEMA, system=mod.SYSTEM, model=None):
        if system in (mod.SCENE_WRITER, mod.WRITER, mod.PORTRAIT_WRITER):
            calls["n"] += 1
        return orig(prompt, schema, system, model)
    il._complete = counting
    pictured = {r["target"] for r in recs}
    rows = []
    for kind, target in scenes:
        if il.text_spent() > cap:
            rows.append({"kind": kind, "target": target, "skipped": "cap"})
            continue
        before = calls["n"]
        try:
            job = il.job(kind, target)
            pr = il.make_prompt(job)
        except Exception as e:                      # noqa: BLE001 (a bench: record and go on)
            rows.append({"kind": kind, "target": target, "skipped": str(e)[:200]})
            continue
        w = pr.writer
        tags = re.findall(r'<entry name="([^"]*)" type="(?:person|creature)"([^>]*)>', w)
        labelled = [f["entity"] for f in pr.figures or []]
        shown = list(dict.fromkeys(pr.shows))
        rows.append({"kind": kind, "target": target, "writer_calls": calls["n"] - before,
                     "entries": len(tags), "entry_labels": sum("figure=" in a for _, a in tags),
                     "shown": len(shown), "labelled_in_text": len(labelled),
                     "shown_unlabelled": len([x for x in shown if x not in labelled]),
                     "shown_pictured": len([x for x in shown if x in pictured]),
                     "references": len(pr.references), "words": len(pr.body.split()), "body": pr.body,
                     "figures": pr.figures})
    return {"rows": rows, "report": dict(il.report.dropped), "cost": round(il.text_spent(), 4)}


def summary(name: str, res: dict) -> list[str]:
    rows = [r for r in res["rows"] if "skipped" not in r]
    if not rows:
        return [f"{name}: nothing written"]
    tot = lambda k: sum(r[k] for r in rows)    # noqa: E731
    words = sorted(r["words"] for r in rows)
    return [f"{name}: {len(rows)} prompts ({sum('skipped' in r for r in res['rows'])} skipped), cost ${res['cost']:.4f}",
            f"  figure entries {tot('entries')}, labelled in the material {tot('entry_labels')}",
            f"  shown {tot('shown')}, called by a label {tot('labelled_in_text')}, shown unlabelled {tot('shown_unlabelled')}, "
            f"shown with a portrait {tot('shown_pictured')}, references {tot('references')}",
            f"  prompts with every shown figure labelled: {sum(r['shown_unlabelled'] == 0 for r in rows)} of {len(rows)}",
            f"  writer calls {tot('writer_calls')}; words min {words[0]}, median {words[len(words) // 2]}, max {words[-1]}",
            f"  report: {json.dumps(res['report'], sort_keys=True)}"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index", nargs="+")
    ap.add_argument("--epub", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--old")
    ap.add_argument("--scenes", type=int, default=10)
    ap.add_argument("--plates", type=int, default=3)
    ap.add_argument("--pictured", type=int, default=3)
    ap.add_argument("--cap", type=float, default=0.50)
    a = ap.parse_args()
    if len(a.epub) != len(a.index):
        ap.error("one --epub per index")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    text = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
    old = load_old(a.old) if a.old else None
    results, lines = {}, []
    for ix, ep in zip(a.index, a.epub):
        doc = json.loads(Path(ix).read_text())
        bk = epub.read(ep)
        scenes = pick(doc, bk, a.scenes, a.plates)
        recs = seed(doc, scenes, a.pictured)
        res = {"scenes": scenes, "seeded": [r["target"] for r in recs]}
        res["new"] = side(new_art, doc, bk, scenes, recs, text, a.cap)
        if old:
            res["old"] = side(old, doc, bk, scenes, recs, text, a.cap)
        results[ix] = res
        lines.append(f"== {Path(ix).name}: {len(scenes)} scenes, portraits seeded for {', '.join(res['seeded'])}")
        for k in ("old", "new"):
            if k in res:
                lines += summary(k, res[k])
        (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    (out / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
