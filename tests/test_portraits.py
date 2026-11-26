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
    """Scripted replies: a string is a description's text, a dict the whole reply. The look-picking call answers from
    `looks` (a list of number lists, one per call; none picked when it runs out) and is logged in `picks`."""

    def __init__(self, replies, looks=(), lost=()):
        self.replies, self.prompts, self.looks, self.picks = list(replies), [], list(looks), []
        self.lost, self.confirms = list(lost), []

    def complete(self, system, prompt, schema, prefix=None):
        if schema is portraits.LOOKS_SCHEMA:
            self.picks.append(prompt)
            return {"looks": self.looks.pop(0) if self.looks else []}
        if schema is portraits.LOST_SCHEMA:          # by default every flagged look is confirmed lost
            self.confirms.append(prompt)
            return {"lost": self.lost.pop(0) if self.lost else list(range(1, 20))}
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        return r if isinstance(r, dict) else {"text": r}


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

    def short(text, hits, lo, hi, since=None):   # the second time, the budget leaves out the later passages
        calls.append(1)
        out, last = real(text, hits, lo, hi, since)
        return out, (last if len(calls) == 1 else 2)
    monkeypatch.setattr(portraits, "_windows", short)
    portraits.portraits([(d, book)], Fake(["A young woman of the mill.", "A young woman of the mill who keeps secrets."]),
                        log=lambda m: None)
    facets = d["entities"][0]["facets"]
    froms = [int(f["from"][1:]) for f in facets]
    assert froms == sorted(froms)
    # Clamped to the first portrait's reveal point, the rewrite stands in for it rather than
    # sitting beside it: one description per reveal point, the later text.
    assert len(facets) == 1 and facets[0]["text"].endswith("keeps secrets.") and "replaces" not in facets[0]


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


def test_a_change_in_standing_earns_a_rewrite_and_the_notes_reach_the_model():
    ch1 = ["Mira waited by the gate of the mill for a long while that day.",
           "Mira came home again in the evening carrying her empty basket."]
    ch2 = ["The fever took the village that winter and many did not see spring.",
           "Mira lay in the cold room and did not wake on the third morning."]
    book = _book([("One", ch1), ("Two", ch2)])
    fact = {"id": "f1", "from": "b3", "basis": "stated", "evidence": ["b3"], "text": "Died of the fever in winter."}
    d = _doc(book, [_mira(facets=[fact])])
    model = Fake(["Mira is a young woman of the village.", "Dead, taken by the fever; a young woman of the village."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats["portraits"] == 2            # one new mention would not have earned a rewrite
    assert "Died of the fever" not in model.prompts[0]
    assert "What the companion has noted about Mira" in model.prompts[1] and "Died of the fever" in model.prompts[1]
    assert "FIRST sentence" in portraits.PORTRAIT
    ps = [f for f in d["entities"][0]["facets"] if f.get("kind") == "profile"]
    assert [f["from"] for f in ps] == ["b1", "b3"] and ps[1]["text"].startswith("Dead")


def test_an_extraction_profile_at_the_first_portrait_anchor_is_superseded():
    book = _book([("One", CH1)])
    early = {"id": "p-early", "kind": "profile", "from": "b1", "basis": "stated", "evidence": ["b1"], "text": "Someone."}
    same = {"id": "p-same", "kind": "profile", "from": "b4", "basis": "stated", "evidence": ["b4"], "text": "A girl."}
    d = _doc(book, [_mira(facets=[early, same])])
    portraits.portraits([(d, book)], Fake(["A young woman of the mill."]), log=lambda m: None)
    ps = [f for f in d["entities"][0]["facets"] if f.get("kind") == "profile"]
    assert [f["id"] for f in ps] == ["p-early", "bk.mira.portrait-1"]
    assert ps[1]["from"] == "b4" and ps[1]["replaces"] == ["p-early"]


def test_a_portrait_that_ignores_a_change_in_standing_is_rewritten_once():
    ch1 = ["Mira waited by the gate of the mill for a long while that day.",
           "Mira came home again in the evening carrying her empty basket."]
    ch2 = ["The fever took the village that winter and many did not see spring.",
           "Mira lay in the cold room and did not wake on the third morning."]
    book = _book([("One", ch1), ("Two", ch2)])
    fact = {"id": "f1", "from": "b3", "basis": "stated", "evidence": ["b3"], "text": "Died of the fever in winter."}
    d = _doc(book, [_mira(facets=[fact])])
    # Lucy in the Dracula example: told of her death, the model still described her as recovering.
    model = Fake(["Mira is a young woman of the village.", "Mira is a young woman of the village, kind to all.",
                  "Dead of the fever; a young woman of the village."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert '"Died of the fever in winter."' in model.prompts[1] and "change in standing" in model.prompts[1]
    assert "does not state the change" in model.prompts[2]
    assert stats["rewritten: change in standing"] == 1
    ps = [f for f in d["entities"][0]["facets"] if f.get("kind") == "profile"]
    assert ps[-1]["text"].startswith("Dead")


def test_a_tie_the_entrys_own_checked_notes_state_is_kept():
    book = _book([("One", CH1)])
    # Lucy in Dracula: "engaged" sat in letters the sampled passages missed, so every later portrait was dropped
    # and her description stayed from before her death.
    note = {"id": "f1", "from": "b0", "basis": "stated", "evidence": ["b0"], "text": "Is engaged to the miller."}
    d = _doc(book, [_mira(facets=[note])])
    model = Fake(["Mira is a young woman of the village, engaged to the miller."] * 3)
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats.get("portraits", 0) >= 1 and not stats.get("dropped")
    assert any("engaged" in f["text"] for f in d["entities"][0]["facets"] if f.get("kind") == "profile" and f["id"] != "f1")


def test_a_turning_point_records_a_change_in_standing_too():
    ch1 = ["Mira waited by the gate of the mill for a long while that day.",
           "Mira came home again in the evening carrying her empty basket."]
    ch2 = ["The fever took the village that winter and many did not see spring.",
           "Mira lay in the cold room and did not wake on the third morning."]
    book = _book([("One", ch1), ("Two", ch2)])
    d = _doc(book, [_mira()])
    # One Dracula build recorded Lucy's death only as the turning point "Lucy dies", never as a fact.
    d["events"] = [{"id": "e1", "from": "b3", "kind": "turn", "summary": "Mira dies of the fever.", "entities": ["mira"], "evidence": ["b3"]},
                   {"id": "e2", "from": "b2", "summary": "Mira is feared dead by the village.", "entities": ["mira"], "evidence": ["b2"]}]
    model = Fake(["Mira is a young woman of the village.", "Dead of the fever; a young woman of the village."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats["portraits"] == 2 and '"Mira dies of the fever."' in model.prompts[1]
    assert "feared dead" not in model.prompts[1]           # an ordinary event is not a change in standing


def test_a_hedged_near_death_is_not_a_change_in_standing():
    # "Van Helsing and Seward find Lucy near death" as a change made Haiku write her death chapters early.
    assert not portraits.changes_standing("Van Helsing and Seward find Lucy near death and pale.")
    assert not portraits.changes_standing("Seward fears Lucy is dead.")
    assert portraits.changes_standing("Lucy dies, and Seward leads Arthur away.")
    assert portraits.changes_standing("Married to Jonathan Harker.")


def test_a_portrait_told_of_a_change_is_not_revealed_before_it():
    ch1 = ["Mira waited by the gate of the mill for a long while that day.",
           "Mira came home again in the evening carrying her empty basket."]
    ch2 = ["Mira lay in the cold room through the long winter night alone.",
           "The fever took her on the third morning, and the whole village mourned."]
    book = _book([("One", ch1), ("Two", ch2)])
    d = _doc(book, [_mira()])
    d["events"] = [{"id": "e1", "from": "b3", "kind": "turn", "summary": "Mira dies of the fever.", "entities": ["mira"], "evidence": ["b3"]}]
    model = Fake(["Mira is a young woman of the village.", "Dead of the fever; a young woman of the village."])
    portraits.portraits([(d, book)], model, log=lambda m: None)
    ps = [f for f in d["entities"][0]["facets"] if f.get("kind") == "profile"]
    assert ps[-1]["text"].startswith("Dead") and ps[-1]["from"] == "b3"   # the last mention is b2


def test_a_rewrite_may_not_drop_the_standing_the_last_portrait_opened_with():
    ch1 = ["Mira waited by the gate of the mill for a long while that day.",
           "Mira came home again in the evening carrying her empty basket."]
    ch2 = [f"Mira was remembered at the mill by all who knew her, said the miller {i}." for i in range(6)]
    book = _book([("One", ch1), ("Two", ch2)])
    fact = {"id": "f1", "from": "b1", "basis": "stated", "evidence": ["b1"], "text": "Died of the fever in winter."}
    d = _doc(book, [_mira(facets=[fact])])
    # Lucy after the stake: "Dead, after a long decline" became "her health has lately failed".
    model = Fake(["Dead of the fever; a young woman of the village.", "Mira is a young woman whose health has failed.",
                  "Mira is a young woman whose health has failed."])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert 'began: "Dead of the fever;' in model.prompts[2]
    assert stats["dropped: lost the change in standing"] == 1
    ps = [f for f in d["entities"][0]["facets"] if f.get("kind") == "profile"]
    assert all(p["text"].startswith("Dead") for p in ps)


def test_engaged_does_not_stand_in_for_a_death():
    assert not portraits._opens_with("Lucy dies.", "Lucy is a young woman engaged to Arthur. She is sweet.")
    assert portraits._opens_with("Lucy dies.", "Dead, after a long decline. She was engaged to Arthur.")
    assert portraits._opens_with("Captured by the Count.", "Escaped from the castle; a solicitor.")


# ---- stated looks are kept

LOOK_BOOK = [("One", CH1), ("Two", CH2 + CH1)]
FIRST = "A tall woman with red hair who carries baskets to the mill."


def test_a_rewrite_is_told_the_looks_the_last_description_stated_and_keeps_them():
    book = _book(LOOK_BOOK)
    d = _doc(book, [_mira()])
    model = Fake([FIRST, "A tall red-haired woman of the mill who keeps a secret."], looks=[[1, 2]])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert len(model.picks) == 1 and "1. A tall woman\n2. red hair\n3. carries baskets" in model.picks[0]
    assert '1. A tall woman\n2. red hair\nKeep every one' in model.prompts[1] and len(model.prompts) == 2
    assert stats["portraits"] == 2 and not any(k.endswith("stated look") for k in stats)


def test_a_look_the_passages_changed_may_go_when_the_reply_says_so():
    book = _book(LOOK_BOOK)
    d = _doc(book, [_mira()])
    model = Fake([FIRST, {"text": "A tall woman of the mill with grey hair who keeps a secret.", "changed": [2]}],
                 looks=[[1, 2]])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats["portraits"] == 2 and len(model.prompts) == 2
    assert d["entities"][0]["facets"][-1]["text"].endswith("grey hair who keeps a secret.")


def test_a_change_claimed_for_something_else_entirely_does_not_count():
    book = _book(LOOK_BOOK)
    d = _doc(book, [_mira()])
    model = Fake([FIRST, {"text": "A tall woman of the mill who keeps a secret.", "changed": [2]},
                  {"text": "A tall woman of the mill who keeps a secret.", "changed": [2]}], looks=[[1, 2]])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats["dropped: lost a stated look"] == 1 and len(model.prompts) == 3


def test_a_lost_look_gets_one_rewrite():
    book = _book(LOOK_BOOK)
    d = _doc(book, [_mira()])
    model = Fake([FIRST, "A woman of the mill who keeps a secret.", "A tall woman of the mill with red hair who keeps a secret."],
                 looks=[[1, 2]])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert 'It no longer says how Mira looks: 1. "A tall woman"; 2. "red hair"' in model.prompts[2]
    assert stats["rewritten: kept a stated look"] == 1 and stats["portraits"] == 2
    assert d["entities"][0]["facets"][-1]["text"] == "A tall woman of the mill with red hair who keeps a secret."


def test_a_look_lost_twice_leaves_the_previous_description_standing():
    book = _book(LOOK_BOOK)
    d = _doc(book, [_mira()])
    model = Fake([FIRST, "A woman of the mill who keeps a secret.", "A tall woman of the mill who keeps a secret."],
                 looks=[[1, 2]])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats["dropped: lost a stated look"] == 1 and stats["portraits"] == 1 and len(model.confirms) == 2
    assert [f["text"] for f in d["entities"][0]["facets"] if f["kind"] == "profile"] == [FIRST]


def test_a_first_portrait_keeps_the_looks_of_the_extraction_profile_and_is_not_revealed_before_it():
    quiet = ["The rain fell on the long road past the old mill all through the night.",
             "By morning the river had risen over the stones of the ford below the bridge."]
    book = _book([("One", CH1 + quiet)])
    early = {"id": "x.p1", "kind": "profile", "from": "b6", "basis": "stated", "evidence": ["b6"], "text": FIRST}
    d = _doc(book, [_mira(facets=[early])])
    d["entities"][0]["names"].append({"value": "Mira of the mill", "kind": "alias", "from": "b0"})
    model = Fake(["A tall woman with red hair from the mill."], looks=[[2]])
    portraits.portraits([(d, book)], model, log=lambda m: None)
    assert "1. red hair\nKeep every one" in model.prompts[0]
    p = d["entities"][0]["facets"][-1]
    assert p["id"] == "bk.mira.portrait-1" and p["from"] == "b6"      # the passages end at b5, the profile it kept is at b6


def test_no_looks_picked_means_nothing_to_keep():
    book = _book(LOOK_BOOK)
    d = _doc(book, [_mira()])
    model = Fake([FIRST, "A woman of the mill who keeps a secret."], looks=[[]])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert stats["portraits"] == 2 and "Keep every one" not in model.prompts[1] and len(model.picks) == 1


def test_picks_outside_the_pieces_are_ignored():
    model = Fake([], looks=[[0, 2, 9, "x"]])
    assert portraits.look_pieces(model, "Mira", "a character", FIRST) == ["red hair"]
    assert portraits.kept("dark skin", "A dark-skinned smith.") and not portraits.kept("dark skin", "A dark smith.")


def test_mentions_since_the_last_description_get_half_the_windows_and_the_budget_first(monkeypatch):
    text = {s: f"Mira at the mill, paragraph {s}, " + "word " * 40 for s in range(400)}
    hits = list(range(0, 400, 4))                       # windows that never touch
    old, _ = portraits._windows(text, hits, 0, 399)
    assert sum(f"paragraph {s}," in old for s in range(361, 400)) <= 12     # evenly over the book: few recent ones
    monkeypatch.setattr(portraits, "BUDGET", 8000)                          # room for 11 windows of 3 paragraphs
    new, last = portraits._windows(text, hits, 0, 399, since=360)
    assert all(f"paragraph {s}," in new for h in range(364, 400, 4) for s in (h - 1, h, h + 1))   # every recent window
    assert last == 397 and "paragraph 0," in new and "paragraph 200," not in new                    # then the oldest first
    assert [int(x.split(",")[0]) for x in new.split("paragraph ")[1:]] == sorted(int(x.split(",")[0]) for x in new.split("paragraph ")[1:])


def test_a_look_kept_in_other_words_is_not_lost():
    book = _book(LOOK_BOOK)
    d = _doc(book, [_mira()])
    model = Fake([FIRST, "A long-legged woman of the mill with red hair who keeps a secret."], looks=[[1, 2]], lost=[[]])
    stats = portraits.portraits([(d, book)], model, log=lambda m: None)
    assert len(model.prompts) == 2 and stats["kept: look in other words"] == 1 and stats["portraits"] == 2
    assert "1. A tall woman" in model.confirms[0] and "red hair" not in model.confirms[0].split("New description:")[1].split("\n\n")[1]


def test_the_confirming_call_can_only_clear_what_code_flagged():
    model = Fake([], lost=[[2, 5]])
    assert portraits.confirm_lost(model, "Mira", "a character", ["a", "b", "c"], "text", [1, 3]) == [3]
    assert portraits.confirm_lost(model, "Mira", "a character", ["a"], "text", []) == [] and len(model.confirms) == 1
