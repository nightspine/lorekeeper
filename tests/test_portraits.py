import re

from lorekeeper import epub, portraits, text


def _book(paras_by_chapter):
    blocks, chapters = [], []
    for title, paras in paras_by_chapter:
        chapters.append(epub.Chapter(title=title, start=len(blocks)))
        for p in paras:
            blocks.append(epub.Block(seq=len(blocks), spine=0, words=text.words(p), raw=p))
    return epub.Book(blocks=blocks, chapters=chapters)


def _doc(book, entities):
    anchors = [{"id": f"b{b.seq}", "seq": b.seq, "fp": b.fingerprint} for b in book.blocks]
    return {"id": "bk", "work": {"title": "T"}, "anchors": anchors, "entities": entities,
            "coverage": {"through": anchors[-1]["id"]}}


CH1 = [f"Mira walked to the mill with her basket number {i} and waited by the gate." for i in range(5)]
CH2 = ["Mira learned that the miller Oren was her father all along, said the old woman.",
       "Mira kept the secret close and told no one at the market that day."]


class Fake:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, system, prompt, schema, prefix=None):
        self.prompts.append(prompt)
        return {"text": self.replies.pop(0)}


def _mira(**kw):
    return {"id": "mira", "type": "person", "from": "b0", "names": [{"value": "Mira", "kind": "primary", "from": "b0"}],
            "facets": kw.get("facets", [])}


def test_only_text_up_to_the_chapter_is_sent_and_revealed_after_it():
    book = _book([("One", CH1), ("Two", CH2)])
    d = _doc(book, [_mira()])
    model = Fake(["A young woman who carries baskets to the mill."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats["portraits"] == 1               # chapter two adds too few mentions for a rewrite
    assert "Oren" not in model.prompts[0]          # nothing past the end of chapter one
    p = [f for f in d["entities"][0]["facets"] if f["kind"] == "profile"]
    assert p[0]["from"] == "b4" and p[0]["evidence"] and all(int(a[1:]) <= 4 for a in p[0]["evidence"])


def test_names_the_book_has_not_used_yet_are_dropped():
    book = _book([("One", CH1), ("Two", CH2)])
    d = _doc(book, [_mira()])
    model = Fake(["Mira knows the miller Oren.", "Mira knows the miller Oren."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    # Dropped at the end of chapter one (no Oren yet); fine once chapter two has named him.
    assert stats["dropped"] == 1 and stats["portraits"] == 1
    assert int(d["entities"][0]["facets"][0]["from"][1:]) >= 5


def test_momentary_wording_gets_one_rewrite():
    book = _book([("One", CH1)])
    d = _doc(book, [_mira()])
    model = Fake(["Mira is now waiting at the mill.", "A young woman who carries baskets to the mill."])
    portraits.portraits([(d, book)], model, log=lambda m: None)
    assert len(model.prompts) == 2 and '"now"' in model.prompts[1]
    assert d["entities"][0]["facets"][0]["text"].startswith("A young woman")


def test_portraits_supersede_later_extraction_profiles_and_chain():
    book = _book([("One", CH1), ("Two", CH2 + CH1)])
    early = {"id": "x.p1", "kind": "profile", "from": "b0", "basis": "stated", "evidence": ["b0"], "text": "A girl."}
    late = {"id": "x.p2", "kind": "profile", "from": "b6", "basis": "stated", "evidence": ["b6"], "text": "Oren's daughter."}
    d = _doc(book, [_mira(facets=[early, late])])
    model = Fake(["A young woman who carries baskets to the mill.", "A young woman of the mill who keeps a secret."])
    portraits.portraits([(d, book)], model, log=lambda m: None)
    ps = [f for f in d["entities"][0]["facets"] if f["kind"] == "profile"]
    assert [f["id"] for f in ps] == ["x.p1", "bk.mira.portrait-1", "bk.mira.portrait-2"]
    assert ps[1]["replaces"] == ["x.p1"] and ps[2]["replaces"] == ["bk.mira.portrait-1"]
    assert "A young woman who carries" in model.prompts[1]      # the earlier portrait is built on


def test_shared_surnames_do_not_count_as_mentions():
    d = {"entities": [
        {"id": "a", "type": "person", "names": [{"value": "Lily Potter", "from": "b0"}]},
        {"id": "b", "type": "person", "names": [{"value": "Harry Potter", "from": "b0"}]}]}
    pats = portraits._patterns(d)
    assert {p.pattern for p, _ in pats["b"]} == {re.escape(x).join([r"(?<!\w)", r"(?!\w)"]) for x in ("Harry Potter", "Harry")}


def test_never_revealed_before_the_entity_even_if_its_name_appears_earlier():
    book = _book([("One", CH1 + ["A quiet day at the mill, nothing more to tell."])])
    ent = _mira()
    ent["from"] = "b5"
    ent["names"][0]["from"] = "b5"
    d = _doc(book, [ent])
    portraits.portraits([(d, book)], Fake(["A young woman who carries baskets to the mill."]), log=lambda m: None)
    assert d["entities"][0]["facets"][0]["from"] == "b5"


def test_a_whole_name_beats_a_surname_and_titles_are_not_first_names():
    d = {"entities": [
        {"id": "h", "type": "person", "names": [{"value": "Holden", "from": "b0"}]},
        {"id": "jh", "type": "person", "names": [{"value": "Jim Holden", "from": "b0"}]},
        {"id": "m", "type": "person", "names": [{"value": "Detective Miller", "from": "b0"}]}]}
    pats = {eid: {p.pattern for p, _ in ps} for eid, ps in portraits._patterns(d).items()}
    assert r"(?<!\w)Holden(?!\w)" in pats["h"]
    assert not any("Detective(?" in p for p in pats["m"])


def test_portraits_never_go_backwards(monkeypatch):
    book = _book([("One", CH1), ("Two", CH2 + CH1)])
    d = _doc(book, [_mira()])
    real = portraits._windows
    calls = []

    def short(text, hits, lo, hi):   # the second time, the budget leaves out the later passages
        calls.append(1)
        out, last = real(text, hits, lo, hi)
        return out, (last if len(calls) == 1 else 2)
    monkeypatch.setattr(portraits, "_windows", short)
    portraits.portraits([(d, book)], Fake(["A young woman of the mill.", "A young woman of the mill who keeps secrets."]),
                        log=lambda m: None)
    froms = [int(f["from"][1:]) for f in d["entities"][0]["facets"]]
    assert len(froms) == 2 and froms == sorted(froms)


def test_first_portrait_after_two_mentions_and_rewrite_at_four():
    ch1 = ["Mira waited by the gate of the mill for a long while that day.",
           "Nothing else happened in the quiet village on that grey morning."
           , "Mira came home again in the evening carrying her empty basket."]
    ch2 = ["Mira walked out to the mill again on the following morning early."]
    ch3 = ["Mira spoke with the miller about the price of flour for hours."]
    book = _book([("One", ch1), ("Two", ch2), ("Three", ch3)])
    d = _doc(book, [_mira()])
    model = Fake(["Mira is a young woman of the village.", "Mira is a villager who often visits the mill."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    ps = [f for f in d["entities"][0]["facets"] if f["kind"] == "profile"]
    assert stats["portraits"] == 2
    assert [f["id"] for f in ps] == ["bk.mira.portrait-1", "bk.mira.portrait-2"]
    assert ps[0]["from"] == "b2"          # two mentions at the end of chapter one
    assert ps[1]["from"] == "b4"          # four mentions at the end of chapter three


def test_one_mention_is_not_enough_and_the_prompt_allows_a_bare_tie():
    book = _book([("One", ["Mira waited by the gate of the mill for a long while that day."])])
    d = _doc(book, [_mira()])
    model = Fake(["unused"])
    assert portraits.portraits([(d, book)], model, log=lambda m: None).get("portraits", 0) == 0
    assert "ONLY if these passages say it" in portraits.PORTRAIT


def test_portrait_with_a_tie_not_in_the_passages_is_rewritten_once_then_dropped():
    book = _book([("One", CH1)])
    d = _doc(book, [_mira()])
    model = Fake(["Mira is the fiancée of the miller.", "Mira is still the fiancée of the miller."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert len(model.prompts) == 2 and "No passage says fiancée" in model.prompts[1]
    assert stats["dropped"] == 1 and stats.get("portraits", 0) == 0


def test_portrait_rewrite_without_the_tie_is_kept():
    book = _book([("One", CH1)])
    d = _doc(book, [_mira()])
    model = Fake(["Mira is the fiancée of the miller.", "A young woman who carries baskets to the mill."])
    assert portraits.portraits([(d, book)], model, log=lambda m: None)["portraits"] == 1


def test_a_portrait_that_copies_the_book_is_rewritten_once_then_dropped():
    """SPEC §6.3: no more than 8 consecutive words of the book, the rule the validator enforces."""
    long_ch = ["Mira walked to the mill with her basket and waited by the gate for the miller to open up."] * 4
    book = _book([("One", long_ch)])
    doc = _doc(book, [_mira()])
    copied = "Mira walked to the mill with her basket and waited by the gate for the miller."
    fake = Fake([copied, "A girl who carries a basket to the mill each morning and waits there."])
    stats = portraits.portraits([(doc, book)], fake, log=lambda m: None)
    assert stats.get("portraits") == 1 and "copied the book" in fake.prompts[1]
    assert doc["entities"][0]["facets"][0]["text"].startswith("A girl who carries")
    # Still copying after the rewrite: dropped.
    doc2 = _doc(book, [_mira()])
    stats2 = portraits.portraits([(doc2, book)], Fake([copied, copied]), log=lambda m: None)
    assert stats2.get("dropped") == 1 and not doc2["entities"][0]["facets"]
