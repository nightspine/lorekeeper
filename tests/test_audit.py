"""audit(): sampling, passages, verdict counting, with a scripted model."""

import re

from lorekeeper import audit, epub
from .test_generate import make_epub


def book(tmp_path):
    return epub.read(make_epub(tmp_path / "b.epub")).blocks


def index_of(blocks, n_facts=18):
    seqs = [b.seq for b in blocks if len(b.words) >= 6]
    anchors = [{"id": f"a{s}", "fp": blocks[s].fingerprint, "seq": s} for s in seqs]
    facets = [{"id": f"f{i}", "from": f"a{seqs[i % len(seqs)]}", "evidence": [f"a{seqs[i % len(seqs)]}"],
               "text": f"Fact number {i} about her.", "basis": "stated"} for i in range(n_facts)]
    return {
        "anchors": anchors,
        "entities": [{"id": "alice", "type": "person", "from": anchors[0]["id"],
                      "names": [{"value": "Alice Marlow", "kind": "primary", "from": anchors[0]["id"]}],
                      "facets": facets}],
        "relations": [{"id": "r1", "src": "alice", "dst": "alice", "label": "looks for", "from": anchors[1]["id"],
                       "evidence": [anchors[1]["id"]]}],
        "events": [{"id": "e1", "from": anchors[2]["id"], "summary": "Alice finds a hidden letter.", "entities": ["alice"],
                    "evidence": [anchors[2]["id"]]}],
        "recaps": [{"id": "s1", "from": anchors[3]["id"], "scope": "chapter",
                    "text": "Alice reaches the lighthouse. Yes. She finds a letter under the floor."},
                   {"id": "s2", "from": anchors[3]["id"], "scope": "story", "text": "Everything so far, in many words here."}],
    }


class Judge:
    name = "judge"

    def __init__(self, bad=(), unsure=(), omit=()):
        self.prompts, self.bad, self.unsure, self.omit = [], set(bad), set(unsure), set(omit)

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
        assert "verdicts" in schema["properties"]
        out = []
        for cid in re.findall(r"^(c\d+): \(", prompt, re.M):
            if cid in self.omit:
                continue
            v = "unsupported" if cid in self.bad else "unsure" if cid in self.unsure else "supported"
            out.append({"id": cid, "verdict": v, "because": "x"})
        return {"verdicts": out}


def test_all_claims_when_fewer_than_n(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks)
    j = Judge()
    r = audit.audit(idx, blocks, j, n=50)
    # 18 facts + 1 relation + 1 event + 2 chapter sentences ("Yes." is not a sentence; the story recap is skipped)
    assert r == {"sampled": 22, "supported": 22, "unsupported": 0, "unsure": 0, "model": "judge"}
    assert len(j.prompts) == 3


def test_counts_only_supported(tmp_path):
    blocks = book(tmp_path)
    r = audit.audit(index_of(blocks), blocks, Judge(bad={"c1"}, unsure={"c2"}, omit={"c3"}), n=50)
    assert (r["sampled"], r["supported"], r["unsupported"], r["unsure"]) == (22, 19, 1, 2)
    assert r["sampled"] == r["supported"] + r["unsupported"] + r["unsure"]


def test_profiles_are_not_sampled(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks)
    f = idx["entities"][0]["facets"]
    f.append({"id": "p1", "kind": "profile", "from": idx["anchors"][0]["id"], "text": "A long sum of everything.", "basis": "stated"})
    j = Judge()
    assert audit.audit(idx, blocks, j, n=50)["sampled"] == 22
    assert "A long sum of everything" not in j.prompts[0]


def test_detail_lists_verdicts_with_passages(tmp_path):
    blocks = book(tmp_path)
    d = []
    audit.audit(index_of(blocks), blocks, Judge(bad={"c1"}), n=50, detail=d)
    assert len(d) == 22 and d[0]["verdict"] == "unsupported" and d[0]["passage"].startswith("[")


def test_batches_of_ten_and_even_deterministic_sample(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks, n_facts=60)
    claims = audit.claims_of(idx, {a["id"]: a["seq"] for a in idx["anchors"]})
    a, b = audit.sample(claims, 25, seed=3), audit.sample(claims, 25, seed=3)
    assert [c.cid for c in a] == [c.cid for c in b]
    assert [c.cid for c in a] != [c.cid for c in audit.sample(claims, 25, seed=4)]
    # one claim from each of 25 equal strata of the ordinal-sorted list, so ordinals never go backwards
    assert [c.ordinal for c in a] == sorted(c.ordinal for c in a)
    j = Judge()
    r = audit.audit(idx, blocks, j, n=25, seed=3)
    assert r["sampled"] == 25 and len(j.prompts) == 3
    assert audit.audit(idx, blocks, Judge(), n=25, seed=3) == r


def test_passage_is_the_cited_block_and_its_neighbours(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks)
    j = Judge()
    audit.audit(idx, blocks, j, n=50)
    prompt = j.prompts[0]
    s = int(idx["anchors"][2]["seq"])           # the event's block
    assert f"[{s}] " in prompt and f"[{s - 1}] " in prompt and f"[{s + 1}] " in prompt and f"[{s + 2}] " in prompt
    last = len(blocks) - 1
    assert f"[{last + 1}]" not in prompt


def test_unresolved_anchors_are_not_sampled(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks)
    for a in idx["anchors"][:]:
        a["fp"] = "0" * 16          # a different edition: nothing resolves
    j = Judge()
    assert audit.audit(idx, blocks, j, n=50) == {"sampled": 0, "supported": 0, "unsupported": 0, "unsure": 0, "model": "judge"}
    assert j.prompts == []


def test_unusable_response_is_unsure(tmp_path):
    blocks = book(tmp_path)

    class Broken:
        name = "b"
        def complete(self, *a, **k):
            return {}
    r = audit.audit(index_of(blocks), blocks, Broken(), n=50)
    assert r["supported"] == r["unsupported"] == 0 and r["unsure"] == r["sampled"] == 22


def _claim(seqs, frm):
    return audit.Claim("c1", 0, "event", "x", seqs, frm=frm)


def _blocks(n=60):
    from lorekeeper.epub import Block
    return [Block(i, 0, ["w"], f"para {i}") for i in range(n)]


def _seqs(passage):
    return [int(x) for x in re.findall(r"^\[(\d+)\]", passage, re.M)]


def test_passage_runs_through_the_from_block():
    b = _blocks()
    # cited 10, from 15 (a later block): 8 .. 15, and two after the last cited (12) is inside it
    assert _seqs(audit.passage(_claim([10], 15), b)) == list(range(8, 16))
    # from at the cited block: two either side
    assert _seqs(audit.passage(_claim([10], 10), b)) == list(range(8, 13))
    # several cited blocks: from the first minus two, past the last by two
    assert _seqs(audit.passage(_claim([10, 12], 12), b)) == list(range(8, 15))
    # the edges of the book
    assert _seqs(audit.passage(_claim([0], 0), b)) == [0, 1, 2]


def test_passage_cap_keeps_cited_and_from_blocks():
    b = _blocks()
    got = _seqs(audit.passage(_claim([10, 12], 30), b))
    assert len(got) == 14 and got == sorted(got)
    assert {10, 12, 30} <= set(got)
    many = _seqs(audit.passage(_claim(list(range(5, 25)), 24), b))
    assert len(many) == 14 and 24 in many


def _summary(seqs, frm, start):
    return audit.Claim("c1", 0, "summary", "x", seqs, frm=frm, span_start=start)


def test_chapter_recap_gets_the_stretch_since_the_previous_recap(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks)
    seqs = [a["seq"] for a in idx["anchors"]]
    resolved = {a["id"]: a["seq"] for a in idx["anchors"]}
    idx["recaps"] = [
        {"id": "s1", "from": idx["anchors"][1]["id"], "scope": "chapter", "text": "Alice reaches the lighthouse at last."},
        {"id": "s2", "from": idx["anchors"][3]["id"], "scope": "chapter", "text": "She finds a letter under the floor."},
    ]
    cl = {c.text: c for c in audit.claims_of(idx, resolved) if c.kind == "summary"}
    assert cl["Alice reaches the lighthouse at last."].span_start == 0
    second = cl["She finds a letter under the floor."]
    assert second.span_start == seqs[1] + 1
    got = _seqs(audit.passage(second, blocks))
    assert got == list(range(seqs[1] + 1, seqs[3] + 1))      # the whole stretch, no trailing window


def test_long_stretch_keeps_the_end_and_cannot_be_unsupported():
    from lorekeeper.epub import Block
    b = [Block(i, 0, ["w"], "x" * 9_000) for i in range(30)]      # 8,000 chars sent per block
    c = _summary([29], 29, 0)
    got, cut = audit.summary_span(c, b)
    assert cut and got[-1] == 29 and got == list(range(got[0], 30))
    assert sum(len(audit._line(b, s)) + 1 for s in got) <= audit.SUMMARY_CHARS
    assert "start of the passage left out" in audit._prompt([c], b)
    short, cut = audit.summary_span(_summary([5], 5, 0), b)
    assert not cut and short == list(range(0, 6))

    class Strict:
        name = "s"
        def complete(self, system, prompt, schema, prefix=None):
            return {"verdicts": [{"id": "c1", "verdict": "unsupported", "because": "x"}]}
    idx = {"anchors": [{"id": "a", "fp": b[29].fingerprint if hasattr(b[29], "fingerprint") else "", "seq": 29}],
           "recaps": [{"id": "r", "from": "a", "scope": "chapter", "text": "A sentence that is long enough to count."}]}
    # via the claim directly: the verdict is turned into unsure
    import types
    orig = audit._picked
    audit._picked = lambda *a, **k: [c]
    try:
        r = audit.audit(idx, b, Strict())
    finally:
        audit._picked = orig
    assert (r["unsupported"], r["unsure"]) == (0, 1)


def test_recap_sentences_are_at_most_a_tenth_of_the_sample():
    cs = [audit.Claim(f"c{i}", i, "summary" if i % 2 else "fact", "t", [i]) for i in range(1, 201)]
    got = audit.sample(cs, 50, seed=1)
    assert len(got) == 50 and sum(c.kind == "summary" for c in got) == 5
    assert [c.ordinal for c in got] == sorted(c.ordinal for c in got)
    sm = [c.ordinal for c in got if c.kind == "summary"]
    assert sm[0] < 40 and sm[-1] > 160                         # spread across the book
    # few recap sentences: all of them up to the cap, the rest facts
    few = [audit.Claim(f"c{i}", i, "summary" if i < 4 else "fact", "t", [i]) for i in range(1, 100)]
    assert sum(c.kind == "summary" for c in audit.sample(few, 50)) == 3
    assert len(audit.sample(few, 50)) == 50


def test_recap_sentences_stay_a_tenth_when_fewer_than_n_are_sampled():
    cs = [audit.Claim(f"c{i}", i, "fact", "t", [i]) for i in range(1, 21)]
    cs += [audit.Claim(f"c{i}", i, "summary", "t", [i]) for i in range(21, 26)]
    got = audit.sample(cs, 50, seed=1)
    s = sum(c.kind == "summary" for c in got)
    assert len(got) < 50 and s * 10 <= len(got) and s == 2


def test_a_fact_is_named_as_the_book_had_revealed_it(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks, n_facts=3)
    ids = [a["id"] for a in idx["anchors"]]
    e = idx["entities"][0]
    e["names"] = [{"value": "Alice Marlow", "kind": "primary", "from": ids[4]},
                  {"value": "the stranger", "kind": "alias", "from": ids[0]}]
    e["facets"][0]["from"] = e["facets"][0]["evidence"][0] = ids[1]       # before the primary name arrives
    e["facets"][1]["from"] = e["facets"][1]["evidence"][0] = ids[5]       # after it
    resolved = {a["id"]: a["seq"] for a in idx["anchors"]}
    texts = [c.text for c in audit.claims_of(idx, resolved) if c.kind == "fact"]
    assert texts[0].startswith("the stranger: ") and any(t.startswith("Alice Marlow: ") for t in texts)
    # no name has arrived yet: the earliest name
    e["names"][1]["from"] = ids[2]
    e["facets"][0]["from"] = e["facets"][0]["evidence"][0] = ids[1]
    texts = [c.text for c in audit.claims_of(idx, resolved) if c.kind == "fact"]
    assert texts[0].startswith("the stranger: ")


def test_estimate_matches_what_audit_sends(tmp_path):
    blocks = book(tmp_path)
    idx = index_of(blocks, n_facts=30)
    j = Judge()
    est = audit.estimate(idx, blocks, n=25, seed=3)
    r = audit.audit(idx, blocks, j, n=25, seed=3)
    assert est["claims"] == r["sampled"] and est["calls"] == len(j.prompts) == 3
    assert est["input_chars"] == sum(len(audit.SYSTEM) + len(p) for p in j.prompts)
    assert audit.estimate({"anchors": [], "entities": []}, blocks) == {"claims": 0, "calls": 0, "input_chars": 0}
