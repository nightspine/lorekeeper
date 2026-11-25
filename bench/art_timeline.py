"""Measure the story-time pass of art sets (`Illustrator.timeline`) on real books, and what it does to picture prompts.

    uv run --extra claude python bench/art_timeline.py label [--runs 3] [--only eotv|controls]    # labels, agreement, cost
    uv run --extra claude python bench/art_timeline.py prompts [--chapters 12]                    # EotV 1 scene prompts, on vs off
    uv run --extra claude python bench/art_timeline.py ab [--n 10] [--quality draft]              # likeness reference A/B (Gemini)

The books, their indexes and the ground truth are bench data only (nothing in art.py knows them): Empire of the Vampire 1-3
(a frame and two strands of the past, alternating by part), and the controls Dracula and A Study in Scarlet / The Sign of the
Four. Indexes are read from `~/projects/lorekeeper/lorekeeper-data/indexes/`, EPUBs from `lorekeeper-data/books/`, never
written: every art set goes to a temporary folder. Keys come from the environment, else from the `.env` of the repository.
The text model is the provider's cheap one (Haiku by default). Results are written to `bench/results/art_timeline-*.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from itertools import combinations
from pathlib import Path

from lorekeeper import art, epub, llm

DATA = Path(os.environ.get("LOREKEEPER_DATA", Path.home() / "projects/lorekeeper/lorekeeper-data"))
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "results"

# (index file stem, book file stem) in series reading order
EOTV = [("empire-of-the-vampire-3ec7065b2283", "865fa20bac04f9dd"),
        ("empire-of-the-damned-92ff0054d954", "79f924c406c4a52a"),
        ("empire-of-the-dawn-5e8ee7cc3af3", "e299581e4470d8b6")]
CONTROLS = {"dracula": [("dracula-7fbd2cea3316", "4d40635636002720")],
            "sherlock": [("a-study-in-scarlet-e1f014b15b40", "d65612d1c87ec1f7"),
                         ("the-sign-of-the-four-0ae79430d682", "966338f996fb14b8")]}


def truth_eotv1(toc: int) -> str | None:
    """Empire of the Vampire book 1, by the `tocNN` of a chapter's anchors: the frame (the narrator in his cell), his youth,
    and the later past, alternating by part."""
    if toc in (5, 107):
        return "frame"
    if 6 <= toc <= 16 or 29 <= toc <= 41 or 59 <= toc <= 78:
        return "youth"
    if 17 <= toc <= 28 or 42 <= toc <= 58 or 79 <= toc <= 106:
        return "later"
    return None


# Bench data for the prompts measure: words of the frame's look of the narrator that his youth does not have (the inkwork
# and the aged face of the man in the cell). Only for scoring.
FRAME_LOOK = re.compile(r"\b(inkwork|ink-work|tattoo\w*|aegis|beard\w*|grey|gray|aged|weathered|lined|scar\w*|older)\b", re.I)


class NoImage:
    provider, name = "", "none"

    def draw(self, *a, **k):
        raise RuntimeError("the bench draws nothing here")


def load_env() -> None:
    for p in (ROOT / ".env", Path.home() / "projects/lorekeeper/.env"):
        if p.exists():
            for line in p.read_text().splitlines():
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$", line)
                if m and not os.environ.get(m.group(1)):
                    os.environ[m.group(1)] = m.group(2).strip().strip("'\"")


def text_model(provider: str, model: str | None):
    spec = llm.PROVIDERS[provider]
    return llm.make(provider, model or spec["cheap"], api_key=llm.env_key(provider))


def books(spec: list[tuple[str, str]], tmp: Path, level=None) -> list[art.Book]:
    out = []
    for n, (idx, bk) in enumerate(spec):
        doc = json.loads((DATA / "indexes" / f"{idx}.lore.json").read_text())
        out.append(art.Book(doc, epub.read(DATA / "books" / f"{bk}.epub"), art.FileSink(tmp / f"b{n}"), level))
    return out


def toc_of(b, ch: dict) -> int | None:
    for a in b.anchors:
        if ch["lo"] <= b.seq_of[a["id"]] <= ch["hi"]:
            m = re.match(r"toc(\d+)\.", a["id"])
            return int(m.group(1)) if m else None
    return None


def label(spec, text, workers=1) -> tuple[art.Illustrator, float, float]:
    tmp = Path(tempfile.mkdtemp(prefix="lk-tl-"))
    il = art.Illustrator(books(spec, tmp), text, NoImage(), "realism", portraits=None, workers=workers, log=lambda m: print(m, file=sys.stderr))
    t0 = time.time()
    il.timeline(len(il.books) - 1)
    return il, il.text_spent(), time.time() - t0


def chapters_of(il, j: int) -> list[tuple[str, dict | None, int | None]]:
    """(title, label as stored, toc) per chapter of book j."""
    b = il.books[j]
    got = (il.man[j].get("timeline") or {}).get("chapters", {})
    return [(c["title"], got.get(c["title"]), toc_of(b, c)) for c in b.chapters]


def strand_count(il) -> int:
    return len({s for j in range(len(il.books)) for _, lab, _ in chapters_of(il, j) if lab and (s := lab.get("strand"))})


def score_eotv1(il) -> dict:
    rows = [(t, lab, toc) for t, lab, toc in chapters_of(il, 0) if truth_eotv1(toc or 0)]
    by = Counter((lab or {}).get("strand") for _, lab, _ in rows)
    # each strand stands for the truth group most of its chapters are in
    group: dict = {}
    for s in by:
        group[s] = Counter(truth_eotv1(toc) for _, lab, toc in rows if (lab or {}).get("strand") == s).most_common(1)[0][0]
    ok = sum(1 for _, lab, toc in rows if lab and lab.get("strand") and group[lab["strand"]] == truth_eotv1(toc))
    wrong = [(toc, (lab or {}).get("strand"), truth_eotv1(toc)) for _, lab, toc in rows
             if not (lab and lab.get("strand") and group[lab["strand"]] == truth_eotv1(toc))]
    eras = Counter((lab or {}).get("era") for _, lab, _ in rows)
    groups = {s: g for s, g in group.items() if s}
    return {"chapters": len(rows), "correct": ok, "accuracy": round(ok / max(1, len(rows)), 3), "strands": len([s for s in by if s]),
            "groups": groups, "distinct": len(set(groups.values())) == len(groups), "wrong": wrong, "eras": dict(eras),
            "exceptions": sum(len((lab or {}).get("events", {})) for _, lab, _ in rows)}


def partition(il, j: int) -> list:
    return [(lab or {}).get("strand") and f"{lab['strand']}.{lab.get('era')}" for _, lab, _ in chapters_of(il, j)]


def agreement(a: list, b: list) -> float:
    """The share of chapters two runs put in the same scope, with the second run's ids mapped to the first's by majority."""
    m: dict = {}
    for x, y in zip(a, b):
        m.setdefault(y, Counter())[x] += 1
    best = {y: c.most_common(1)[0][0] for y, c in m.items()}
    return sum(1 for x, y in zip(a, b) if best[y] == x) / max(1, len(a))


def cmd_label(a) -> dict:
    text = text_model(a.provider, a.model)
    out: dict = {"provider": a.provider, "model": text.name, "runs": a.runs}
    sets = {"eotv": EOTV, **CONTROLS}
    if a.only == "eotv":
        sets = {"eotv": EOTV}
    elif a.only == "controls":
        sets = dict(CONTROLS)
    for name, spec in sets.items():
        runs = []
        for r in range(a.runs):
            il, cost, secs = label(spec, text)
            row = {"cost": round(cost, 4), "seconds": round(secs), "strands": strand_count(il),
                   "per_book": [len({lab.get("strand") for _, lab, _ in chapters_of(il, j) if lab and lab.get("strand")})
                                for j in range(len(il.books))],
                   "unknown": sum(1 for j in range(len(il.books)) for _, lab, _ in chapters_of(il, j) if not (lab and lab.get("strand"))),
                   "chapters": sum(len(il.books[j].chapters) for j in range(len(il.books))),
                   "partitions": [partition(il, j) for j in range(len(il.books))],
                   "labels": [{s["id"]: s["label"] for s in (il.man[j].get("timeline") or {}).get("strands", [])} for j in range(len(il.books))],
                   "other": sum(1 for j in range(len(il.books)) for _, lab, _ in chapters_of(il, j) for v in (lab or {}).get("events", {}).values() if v == "other"),
                   "exceptions": sum(len((lab or {}).get("events", {})) for j in range(len(il.books)) for _, lab, _ in chapters_of(il, j))}
            if name == "eotv":
                row["book1"] = score_eotv1(il)
            runs.append(row)
            print(f"{name} run {r + 1}: {row['strands']} strand(s) {row['per_book']}, {row['unknown']} unknown of {row['chapters']}, "
                  f"${row['cost']:.4f}, {row['seconds']}s" + (f", book 1 {row['book1']['accuracy']:.1%}" if name == "eotv" else ""))
        agree = [[round(agreement(x["partitions"][j], y["partitions"][j]), 3) for x, y in combinations(runs, 2)]
                 for j in range(len(spec))]
        out[name] = {"runs": [{k: v for k, v in r.items() if k != "partitions"} for r in runs], "agreement": agree,
                     "partitions": [r["partitions"] for r in runs]}
        print(f"{name}: agreement per book {agree}")
    return out


def cmd_prompts(a) -> dict:
    """Write (never draw) the scene prompts of EotV book 1's youth chapters, story time on and off, and count the prompts in
    which the narrator's frame look shows up, and the figure notes that came back empty."""
    text = text_model(a.provider, a.model)
    out: dict = {"provider": a.provider, "model": text.name}
    for mode in ("off", "on"):
        tmp = Path(tempfile.mkdtemp(prefix=f"lk-tlp-{mode}-"))
        bs = books(EOTV[:1], tmp, level="main")
        il = art.Illustrator(bs, text, NoImage(), "realism", portraits=None, workers=1, story_time=mode == "on",
                             log=lambda m: print(m, file=sys.stderr))
        il.world(0)
        il.timeline(0)
        b = il.books[0]
        lead = next(e["id"] for e in b.doc["entities"] if any(n["value"].lower().startswith("gabriel") for n in e["names"]))
        youth = [c for c in b.chapters if truth_eotv1(toc_of(b, c) or 0) == "youth" and any(lead in e.get("entities", []) for e in c["events"])]
        step = max(1, len(youth) // a.chapters)
        picked = youth[::step][:a.chapters]
        rows = []
        t0 = il.text_spent()
        for ch in picked:
            ev = next((e for e in ch["events"] if lead in e.get("entities", []) and e.get("kind") != "minor"), None)
            if not ev:
                continue
            job = art.Job("event", ev["id"], 0, ev["from"], event=ev, chapter=ch, shape="landscape")
            try:
                pr = il.make_prompt(job)
            except Exception as e:  # noqa: BLE001 - a dropped prompt is a result too
                rows.append({"chapter": ch["title"], "dropped": str(e)[:120]})
                continue
            note = dict(pr.notes).get(lead, "")
            rows.append({"chapter": ch["title"], "scope": pr.scope, "event_scope": il.event_scope(0, ev),
                         "lead_note": note, "frame_look": bool(FRAME_LOOK.search(note)), "figures": len(pr.notes),
                         "refs": len(pr.references)})
        notes = [n for v in il.man[0]["appearance"].values() for n in v]
        out[mode] = {"prompts": len([r for r in rows if "dropped" not in r]), "dropped": len([r for r in rows if "dropped" in r]),
                     "frame_look": sum(1 for r in rows if r.get("frame_look")),
                     "lead_empty": sum(1 for r in rows if "dropped" not in r and not r["lead_note"]),
                     "notes": len(notes), "empty_notes": sum(1 for n in notes if not n.get("text")),
                     "text_cost": round(il.text_spent(), 4), "prompt_cost": round(il.text_spent() - t0, 4), "rows": rows}
        o = out[mode]
        print(f"story time {mode}: {o['prompts']} prompts ({o['dropped']} dropped), frame look in {o['frame_look']}, "
              f"lead with no note {o['lead_empty']}, notes {o['notes']} ({o['empty_notes']} empty), text ${o['text_cost']:.4f}")
    return out


def cmd_ab(a) -> dict:
    """Draw about `n` portraits of EotV book 1 characters drawn in two times, the second time's first portrait with and without
    the likeness reference, at draft quality, and keep the files for a look."""
    text = text_model(a.provider, a.model)
    image = llm.make_image(a.image_provider, None, llm.env_key(a.image_provider))
    out: dict = {"provider": a.provider, "image": getattr(image, "name", ""), "quality": a.quality, "pairs": []}
    keep = OUT / "art_timeline_ab"
    keep.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="lk-tlab-"))
    bs = books(EOTV[:1], tmp, level=None)
    il = art.Illustrator(bs, text, image, "realism", portraits="main", quality=a.quality, workers=1, cap=a.cap,
                         log=lambda m: print(m, file=sys.stderr))
    il.world(0)
    jobs = il.plan(0)
    by: dict = {}
    for j in jobs:
        by.setdefault(j.target, []).append(j)
    pairs = [(eid, js) for eid, js in by.items() if len({j.scope for j in js}) >= 2][: max(1, a.n // 3)]
    for eid, js in pairs:
        js = sorted(js, key=lambda j: il.books[0].ord_of[j.anchor])
        first = il.draw(js[0])
        second = next((j for j in js if j.scope != js[0].scope and not j.change), None)
        if not first or not second:
            continue
        il.likeness = True
        with_ = il.draw(second)
        il.man[0]["pictures"] = [r for r in il.man[0]["pictures"] if r is not with_]
        il.likeness = False
        without = il.draw(second)
        il.likeness = True
        name = il.books[0].ents[eid]["names"][0]["value"]
        row = {"entity": name, "first": first and first["file"], "first_scope": js[0].scope, "second_scope": second.scope}
        for tag, rec in (("with", with_), ("without", without)):
            if rec:
                data = il.books[0].b.sink.read(rec["file"])
                (keep / f"{eid}-{tag}-{rec['file']}").write_bytes(data or b"")
                row[tag] = {"file": f"{eid}-{tag}-{rec['file']}", "appearance": rec.get("appearance"), "refs": rec["references"]}
        if first:
            (keep / f"{eid}-first-{first['file']}").write_bytes(il.books[0].b.sink.read(first["file"]) or b"")
            row["first_appearance"] = first.get("appearance")
        out["pairs"].append(row)
        print(json.dumps(row, ensure_ascii=False)[:400])
    out["cost"] = round(il.report.cost, 4)
    out["text_cost"] = round(il.text_spent(), 4)
    print(f"images ${out['cost']:.3f}, text ${out['text_cost']:.4f}; files in {keep}")
    return out


def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=("label", "prompts", "ab"))
    ap.add_argument("--provider", default="claude")
    ap.add_argument("--model")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", choices=("eotv", "controls"))
    ap.add_argument("--chapters", type=int, default=12)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--image-provider", default="gemini")
    ap.add_argument("--quality", default="draft")
    ap.add_argument("--cap", type=float, default=1.0)
    a = ap.parse_args(argv)
    got = {"label": cmd_label, "prompts": cmd_prompts, "ab": cmd_ab}[a.what](a)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"art_timeline-{a.what}{'-' + a.only if a.only else ''}.json"
    path.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n")
    print(f"written {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
