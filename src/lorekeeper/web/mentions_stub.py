"""Stand-in for `lorekeeper.mentions` until the pipeline branch lands; delete it then.

Same contract the route relies on: `count(blocks, chapters, names, upto_seq)` returns, per entity id,
`{"chapters": {chapter index (0-based): count}, "matches": {seq: [(start, end), ...]}}` where `matches`
holds the current chapter's blocks only. Whole words, case-sensitive, longest name first, overlaps merged,
a match inside a longer name of another entity counts only for the longer one.
"""

from __future__ import annotations

import re


def count(blocks, chapters, names: dict[str, list[str]], upto_seq: int) -> dict[str, dict]:
    owners: dict[str, set[str]] = {}
    for eid, ns in names.items():
        for n in ns:
            if n.strip():
                owners.setdefault(n, set()).add(eid)
    if not owners:
        return {}
    rx = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(owners, key=lambda n: (-len(n), n))) + r")(?!\w)")
    starts = [c.start for c in chapters]
    cur = max((i for i, s in enumerate(starts) if s <= upto_seq), default=-1)
    out: dict[str, dict] = {}
    for b in blocks:
        if b.seq > upto_seq:
            break
        ch = max((i for i, s in enumerate(starts) if s <= b.seq), default=-1)
        if ch < 0:
            continue
        for m in rx.finditer(b.raw):
            for eid in owners[m.group(0)]:
                r = out.setdefault(eid, {"chapters": {}, "matches": {}})
                r["chapters"][ch] = r["chapters"].get(ch, 0) + 1
                if ch == cur:
                    r["matches"].setdefault(b.seq, []).append((m.start(), m.end()))
    return out
