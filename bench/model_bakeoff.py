"""Compare local Ollama models on one extraction task: entities + facts with paragraph citations.

    uv run python bench/model_bakeoff.py qwen3.5:9b-q4_K_M gemma4:12b-it-qat --think off,on

Scores what can be scored mechanically (schema, first-appearance paragraph, verbatim copying,
recall against the hand-written example index, speed) and prints every claim for a human to
judge accuracy. Needs corpus/pg244/pg244.epub (see README) and a running Ollama.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

from lorekeeper import epub, text

ROOT = Path(__file__).resolve().parents[1]
FIRST, LAST = 31, 45  # Chapter I of A Study in Scarlet, first 15 paragraphs

# Entities the hand-written index introduces in this range (examples/sherlock-holmes).
EXPECTED = ["Watson", "Murray", "Maiwand", "Stamford", "Barts", "Criterion Bar", "Sherlock Holmes"]

TYPES = {
    "person": "an individual, named or clearly identified",
    "place": "a geographic location, building, room or establishment",
    "group": "a regiment, organization, institution, family or other collective",
    "object": "a ship, document, item or other physical thing",
    "concept": "an idea, method or discovery",
    "term": "a word or phrase with a special meaning in the story",
}

SCHEMA = {
    "type": "object",
    "required": ["entities"],
    "properties": {"entities": {"type": "array", "items": {
        "type": "object",
        "required": ["name", "type", "first_paragraph", "facts"],
        "properties": {
            "name": {"type": "string"},
            "type": {"enum": list(TYPES)},
            "first_paragraph": {"type": "integer"},
            "facts": {"type": "array", "items": {
                "type": "object",
                "required": ["paragraph", "text"],
                "properties": {"paragraph": {"type": "integer"}, "text": {"type": "string"}},
            }},
        }}}},
}

PROMPT = """You are building a spoiler-free reader's companion for a novel.

Read the numbered paragraphs below. List every named person, place, group, object, concept or term.

Types:
{types}

For each entity give:
- name: the name as written in the text
- type: one of the types above
- first_paragraph: the number of the paragraph where it is first named
- facts: 1 to 3 facts the passage states about it. Each fact is ONE short sentence (under 20 words)
  in your own words, plus the number of the paragraph that states it.

Rules:
- Use only what these paragraphs say. Do not use outside knowledge of the book.
- Do not copy phrases from the text; paraphrase.
- Only state things that happened. Do not turn plans, permissions or possibilities into events.
- Keep the order of events exactly as the text gives it.

{passage}
"""


def run(model: str, think: bool, passage: str) -> tuple[dict, dict]:
    prompt = PROMPT.format(types="\n".join(f"- {k}: {v}" for k, v in TYPES.items()), passage=passage)
    body = json.dumps({"model": model, "prompt": prompt, "format": SCHEMA, "stream": False,
                       "think": think, "options": {"temperature": 0, "num_ctx": 16384}}).encode()
    req = urllib.request.Request("http://localhost:11434/api/generate", body,
                                 {"Content-Type": "application/json"})
    t = time.time()
    resp = json.load(urllib.request.urlopen(req, timeout=1800))
    resp["_wall"] = time.time() - t
    return json.loads(resp["response"]), resp


def first_mention(name: str, blocks: list[epub.Block]) -> int | None:
    target = text.words(name)
    n = len(target)
    for b in blocks:
        if any(b.words[i:i + n] == target for i in range(len(b.words) - n + 1)):
            return b.seq
    return None


def score(out: dict, blocks: list[epub.Block]) -> dict:
    in_range = [b for b in blocks if FIRST <= b.seq <= LAST]
    by_seq = {b.seq: b for b in in_range}
    words = [w for b in in_range for w in b.words]
    ents = out["entities"]
    first_ok = sum(1 for e in ents if first_mention(e["name"], in_range) == e["first_paragraph"])
    facts = [f for e in ents for f in e["facts"]]
    copied = [f["text"] for f in facts if text.verbatim_overlap(f["text"], words, 8)]
    bad_cite = [f for f in facts if f["paragraph"] not in by_seq]
    found = {n for n in EXPECTED for e in ents
             if text.words(n) == text.words(e["name"]) or set(text.words(n)) <= set(text.words(e["name"]))}
    return {"entities": len(ents), "first_paragraph_correct": f"{first_ok}/{len(ents)}",
            "facts": len(facts), "copied_over_8_words": len(copied), "citations_out_of_range": len(bad_cite),
            "recall_vs_handwritten": f"{len(found)}/{len(EXPECTED)}",
            "missed": sorted(set(EXPECTED) - found)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("models", nargs="+")
    p.add_argument("--think", default="off", help="comma list of off/on")
    p.add_argument("--out", default=str(ROOT / "bench" / "results"))
    args = p.parse_args()

    blocks = epub.blocks(ROOT / "corpus" / "pg244" / "pg244.epub")
    passage = "\n\n".join(f"[{b.seq}] {b.raw}" for b in blocks[FIRST:LAST + 1])
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    for model in args.models:
        for mode in args.think.split(","):
            think = mode == "on"
            label = f"{model} think={mode}"
            print(f"\n=== {label}")
            try:
                out, resp = run(model, think, passage)
            except Exception as e:  # report and continue with the next model
                print(f"  failed: {e}")
                continue
            gen_s = resp.get("eval_duration", 0) / 1e9
            print(f"  {resp['_wall']:.0f}s wall, {resp.get('eval_count', 0)} output tokens "
                  f"({resp.get('eval_count', 0) / max(gen_s, 1e-9):.1f} tok/s)")
            for k, v in score(out, blocks).items():
                print(f"  {k}: {v}")
            for e in out["entities"]:
                print(f"  [{e['first_paragraph']}] {e['type']:7} {e['name']}")
                for f in e["facts"]:
                    print(f"        ({f['paragraph']}) {f['text']}")
            safe = label.replace(":", "_").replace(" ", "_").replace("=", "-")
            (outdir / f"{safe}.json").write_text(json.dumps(
                {"model": model, "think": think, "wall_seconds": resp["_wall"],
                 "eval_count": resp.get("eval_count"), "output": out}, indent=2))


if __name__ == "__main__":
    main()
