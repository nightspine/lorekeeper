"""Compare a generated index with the hand-written example over the range the example covers.

    uv run python bench/compare_handwritten.py bench/runs/scarlet-ch1-2.gemma.lore.json

Matches entities by name, compares first appearances by block seq, and prints both sets of facts
side by side for a human to judge. Both indexes were built from the same Gutenberg EPUB, so block
seqs are directly comparable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lorekeeper import text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE = ROOT / "examples" / "sherlock-holmes" / "study-in-scarlet.json"


def seqs(ix: dict) -> dict[str, int]:
    return {a["id"]: a["seq"] for a in ix["anchors"]}


def name_keys(e: dict) -> set[tuple[str, ...]]:
    return {tuple(text.words(n["value"])) for n in e["names"]}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("generated")
    p.add_argument("--reference", default=str(DEFAULT_REFERENCE))
    args = p.parse_args()
    gen, ref = json.loads(Path(args.generated).read_text()), json.loads(Path(args.reference).read_text())
    gs, rs = seqs(gen), seqs(ref)
    limit = rs[ref["coverage"]["through"]]
    gen_ents = [e for e in gen["entities"] if gs[e["from"]] <= limit]

    matched, missed = [], []
    for r in ref["entities"]:
        keys = name_keys(r)
        g = next((e for e in gen_ents if name_keys(e) & keys
                  or any(set(k) <= set(gk) or set(gk) <= set(k) for k in keys for gk in name_keys(e)
                         if len(k) > 1 or len(gk) > 1)), None)
        (matched if g else missed).append((r, g))
    extra = [e for e in gen_ents if all(g is not e for _, g in matched)]

    gen_facts = sum(len(e["facets"]) for e in gen_ents)
    ref_facts = sum(len(e["facets"]) for e in ref["entities"])
    print(f"Reference entities found: {len(matched)}/{len(ref['entities'])}")
    print(f"Generated entities in range: {len(gen_ents)} ({len(extra)} not in reference)")
    print(f"Facts: generated {gen_facts}, reference {ref_facts}")
    print(f"Relations: generated {len(gen['relations'])}, reference {len(ref['relations'])}; "
          f"events: generated {len(gen['events'])}, reference {len(ref['events'])}")
    if missed:
        print("Missed:", ", ".join(r["names"][0]["value"] for r, _ in missed))
    print("\nFirst appearance (block seq): reference → generated")
    for r, g in matched:
        rseq, gseq = rs[r["from"]], gs[g["from"]]
        flag = "" if rseq == gseq else ("  LATER" if gseq > rseq else "  EARLIER")
        print(f"  {r['names'][0]['value']:30} {rseq:4} → {gseq:4}{flag}")

    print("\nFacts side by side")
    for r, g in matched:
        print(f"\n## {r['names'][0]['value']}  (generated id: {g['id']}, names: "
              f"{', '.join(n['value'] for n in g['names'])})")
        for f in r["facets"]:
            print(f"   REF [{rs[f['from']]:4}] {f['text']}")
        for f in g["facets"]:
            mark = " (replaces " + ", ".join(f["replaces"]) + ")" if f.get("replaces") else ""
            print(f"   GEN [{gs[f['from']]:4}] {f['text']}{mark}")
    print("\n## Generated entities not in the reference")
    for e in extra:
        print(f"   [{gs[e['from']]:4}] {e['type']:7} {e['names'][0]['value']}: "
              + " | ".join(f["text"] for f in e["facets"][:2]))
    print("\n## Generated recaps")
    for rc in gen["recaps"]:
        print(f"   [{gs[rc['from']]:4}] {rc['text']}")


if __name__ == "__main__":
    main()
