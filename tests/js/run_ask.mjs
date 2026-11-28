// The Ask tab's rule for when a thread must go: revealsLess(snapshot, view, position). Run by tests/test_js_ask.py.
import assert from "node:assert/strict";
import { anyRevealsLess, draftAfterInput, historyFor, passageBlocks, passageLabel, passageOpenable, readSource, revealsLess, snapshot } from "../../src/lorekeeper/web/static/js/ask.js";

const ent = (id, name, aliases = [], extra = {}) => ({ id, name, aliases, profile: null, facets: [], history: [], ...extra });
const view = (o = {}) => ({ entities: [ent("E1", "Holmes")], relations: [], relationHistory: [], events: [{ id: "V1" }], chapters: [{ id: "C1" }], recap: { id: "R1" }, ...o });
const pos = (o = {}) => ({ key: "k", book: "b1", rank: 1, ord: 5, order: "pub", finished: ["b0"], ...o });
const at = snapshot(view(), pos());
const t = (name, fn) => { fn(); console.log("ok", name); };

t("same view, same place keeps", () => assert.equal(revealsLess(at, view(), pos()), false));
t("forward with the story recap replaced keeps", () => assert.equal(revealsLess(at,
  view({ recap: { id: "R2" }, chapters: [{ id: "C1" }, { id: "C2" }], events: [{ id: "V1" }, { id: "V2" }] }), pos({ ord: 9 })), false));
t("a replaced connection (moved to relationHistory) keeps", () => {
  const was = snapshot(view({ relations: [{ id: "L1" }] }), pos());
  assert.equal(revealsLess(was, view({ relations: [{ id: "L2" }], relationHistory: [{ id: "L1" }] }), pos({ ord: 8 })), false);
});
t("a replaced detail (moved to history) keeps", () => {
  const was = snapshot(view({ entities: [ent("E1", "Holmes", [], { facets: [{ id: "F1" }] })] }), pos());
  assert.equal(revealsLess(was, view({ entities: [ent("E1", "Holmes", [], { facets: [{ id: "F2" }], history: [{ id: "F1" }] })] }), pos({ ord: 8 })), false);
});
t("a name gained going forward keeps", () => {
  assert.equal(revealsLess(at, view({ entities: [ent("E1", "Holmes", ["Sherlock"])] }), pos({ ord: 8 })), false);
});
t("back before an alias clears though every id is the same", () => {
  const was = snapshot(view({ entities: [ent("E1", "Holmes", ["Sherlock"])] }), pos());
  assert.equal(revealsLess(was, view(), pos({ ord: 4 })), true);
  // and even with the anchor not lower (a view alone that lost the alias)
  assert.equal(revealsLess(was, view(), pos()), true);
});
t("a display name that changed back clears", () => {
  const was = snapshot(view({ entities: [ent("E1", "Sherlock Holmes")] }), pos());
  assert.equal(revealsLess(was, view(), pos()), true);
});
t("back one anchor with no id change clears", () => assert.equal(revealsLess(at, view(), pos({ ord: 4 })), true));
t("an item gone clears", () => assert.equal(revealsLess(at, view({ events: [] }), pos()), true));
t("a finished box unticked clears", () => assert.equal(revealsLess(at, view(), pos({ finished: [] })), true));
t("a finished box ticked keeps", () => assert.equal(revealsLess(at, view(), pos({ finished: ["b0", "bx"] })), false));
t("another index clears", () => assert.equal(revealsLess(at, view(), pos({ key: "other" })), true));
t("another reading order clears", () => assert.equal(revealsLess(at, view(), pos({ order: "chron" })), true));
t("an earlier book clears, a later one keeps", () => {
  assert.equal(revealsLess(at, view(), pos({ rank: 0, book: "b0", ord: 99 })), true);
  assert.equal(revealsLess(at, view(), pos({ rank: 2, book: "b2", ord: 0 })), false);
});
t("nothing to compare keeps", () => assert.equal(revealsLess(null, view(), pos()), false));

// The draft: text typed by hand gets a snapshot when the field goes from empty to non-empty.
const take = (view_, pos_) => () => snapshot(view_, pos_);
t("typed, then moved back, is cleared", () => {
  const withAlias = view({ entities: [ent("E1", "Holmes", ["Sherlock"])] });
  const draft = draftAfterInput(null, "Who is Sherlock?", take(withAlias, pos()));
  assert.ok(draft);
  assert.equal(anyRevealsLess([draft], view(), pos({ ord: 4 })), true);
});
t("typed, then moved forward, is kept", () => {
  const draft = draftAfterInput(null, "Who is Holmes?", take(view(), pos()));
  assert.equal(anyRevealsLess([draft], view({ recap: { id: "R2" } }), pos({ ord: 9 })), false);
});
t("the snapshot is taken once, at the first character", () => {
  const first = draftAfterInput(null, "W", take(view(), pos()));
  assert.equal(draftAfterInput(first, "Wh", () => { throw new Error("taken again"); }), first);
});
t("emptied by hand leaves nothing to clear", () => {
  const draft = draftAfterInput(null, "Who", take(view(), pos()));
  assert.equal(draftAfterInput(draft, "  ", take(view(), pos())), null);
  assert.equal(anyRevealsLess([null], view(), pos({ ord: 0 })), false);
});

const turn = (q, note = null, state = "ok") => ({ q, state, a: { answer: `A ${q}`, cites: [{ id: "E1" }], note } });
t("history skips refused turns and keeps the rest", () => {
  const h = historyFor([turn("one"), turn("Does Moriarty come back?", "unknown name"), turn("two", "refused: outside knowledge"),
    turn("three", "rewritten: outside knowledge"), turn("bad", null, "error"), turn("four")]);
  assert.deepEqual(h.map((x) => x.question), ["one", "three", "four"]);
  assert.deepEqual(h[0], { question: "one", answer: "A one", cites: ["E1"] });
});
t("history keeps the last six", () => {
  assert.equal(historyFor(Array.from({ length: 9 }, (_, i) => turn(`q${i}`))).length, 6);
});

// Each book of a series has its own library key.
const bookView = (ids) => view({ events: ids.map((id) => ({ id })) });
const s2 = snapshot(bookView(["a1", "a2"]), pos({ key: "k2", series: "S", book: "b2", rank: 1, ord: 5, finished: ["b1"] }));
const p3 = (o = {}) => pos({ key: "k3", series: "S", book: "b3", rank: 2, ord: 0, finished: ["b1", "b2"], ...o });
t("book 2 to book 3 with book 2 finished keeps", () => assert.equal(revealsLess(s2, bookView(["a1", "a2", "c1"]), p3()), false));
t("book 3 back to book 2 clears", () => {
  const s3 = snapshot(bookView(["a1", "a2", "c1"]), p3());
  assert.equal(revealsLess(s3, bookView(["a1", "a2"]), pos({ key: "k2", series: "S", book: "b2", rank: 1, ord: 5, finished: ["b1"] })), true);
});
t("book 2 to book 3 with book 2 not finished clears (its items left the view)", () => assert.equal(revealsLess(s2, bookView(["c1"]), p3({ finished: ["b1"] })), true));
t("another series or a standalone index clears", () => {
  assert.equal(revealsLess(s2, bookView(["a1", "a2"]), p3({ series: "T" })), true);
  assert.equal(revealsLess(s2, bookView(["a1", "a2"]), pos({ key: "solo", series: null })), true);
});

// The position as a word fraction: two places inside one anchor's span differ in which paragraphs are before them.
t("back by p inside one anchor clears", () => {
  const was = snapshot(view(), pos({ p: 0.42 }));
  assert.equal(revealsLess(was, view(), pos({ p: 0.41 })), true);
  assert.equal(revealsLess(was, view(), pos({ p: 0.42 })), false);
  assert.equal(revealsLess(was, view(), pos({ p: 0.5 })), false);
});
t("p of another book is not compared", () => {
  const was = snapshot(view(), pos({ p: 0.9 }));
  assert.equal(revealsLess(was, view(), pos({ rank: 2, book: "b2", ord: 0, p: 0.01 })), false);
});
t("a snapshot without p keeps the anchor rule", () => {
  assert.equal(revealsLess(snapshot(view(), pos()), view(), pos({ p: 0 })), false);
});
t("a draft typed at a later p is cleared on any move back", () => {
  const d = draftAfterInput(null, "What did he see?", take(view(), pos({ p: 0.3 })));
  assert.equal(anyRevealsLess([d], view(), pos({ p: 0.2999 })), true);
});

// Passage cites: a link only while the passage lies wholly before the frontier block of the book being read.
const pc = (o = {}) => ({ kind: "passage", id: "p::40", book: null, seq: 40, end: 43, chapter: "CHAPTER VI", number: 6, ...o });
t("a passage before the frontier opens", () => assert.equal(passageOpenable(pc(), { book: null, frontier: 44 }), true));
t("a passage at or past the frontier never opens", () => {
  assert.equal(passageOpenable(pc(), { book: null, frontier: 43 }), false);   // its last block is the one being read
  assert.equal(passageOpenable(pc({ seq: 50, end: 52 }), { book: null, frontier: 44 }), false);
  assert.equal(passageOpenable(pc({ end: undefined, seq: 44 }), { book: null, frontier: 44 }), false);
  assert.equal(passageOpenable(pc(), { book: null, frontier: 40 }), false);
});
t("no frontier, another book or a bad seq never opens", () => {
  assert.equal(passageOpenable(pc(), { book: null, frontier: null }), false);
  assert.equal(passageOpenable(pc({ book: "b1" }), { book: "b2", frontier: 9999 }), false);
  assert.equal(passageOpenable(pc({ seq: "40" }), { book: null, frontier: 9999 }), false);
  assert.equal(passageOpenable(pc({ end: 39 }), { book: null, frontier: 9999 }), false);
  assert.equal(passageOpenable(pc({ end: 41.5 }), { book: null, frontier: 9999 }), false);
  assert.equal(passageOpenable({ ...pc(), kind: "event" }, { book: null, frontier: 9999 }), false);
});
t("a series passage of the book being read opens", () => assert.equal(passageOpenable(pc({ book: "b2" }), { book: "b2", frontier: 44 }), true));
t("passage labels", () => {
  const chapter = (c) => (c.number != null ? `Chapter ${c.number}` : null);
  assert.equal(passageLabel(pc(), { book: null, frontier: 44, chapter }), "Chapter 6");
  assert.equal(passageLabel(pc({ number: null }), { book: null, frontier: 44, chapter }), "CHAPTER VI");
  assert.equal(passageLabel(pc({ number: null, chapter: null }), { book: null, frontier: 44, chapter }), "The book");
  assert.equal(passageLabel(pc({ book: "b1", number: 3 }), { book: "b2", chapter, short: (k) => (k === "b1" ? "Empire of the Vampire" : k) }),
    "Empire of the Vampire, Chapter 3");
  assert.equal(passageLabel(pc({ book: "b1", number: null, chapter: "" }), { book: "b2", chapter, short: () => "Book One" }), "Book One");
});
t("history never carries passage cites", () => {
  const h = historyFor([{ q: "q", state: "ok", a: { answer: "a", cites: [{ id: "E1", kind: "entity" }, { id: "p::40", kind: "passage" }] } }]);
  assert.deepEqual(h[0].cites, ["E1"]);
});
t("the remembered source", () => {
  const store = (v) => ({ getItem: () => v });
  assert.equal(readSource(store(null)), "both");
  assert.equal(readSource(store("book")), "book");
  assert.equal(readSource(store("index")), "index");
  assert.equal(readSource(store("everything")), "both");
  assert.equal(readSource({ getItem: () => { throw new Error("denied"); } }), "both");
});

t("a passage of this book at or past the position is only 'The book'", () => {
  const chapter = () => "Chapter 27";
  assert.equal(passageLabel(pc(), { book: null, frontier: 43, chapter }), "The book");   // its last block is being read
  assert.equal(passageLabel(pc({ seq: 1900, end: 1901 }), { book: null, frontier: 44, chapter }), "The book");
  assert.equal(passageLabel(pc(), { book: null, frontier: null, chapter }), "The book");   // no exact map
  assert.equal(passageLabel(pc({ book: "b2" }), { book: "b2", frontier: 40, chapter }), "The book");
  assert.equal(passageLabel(pc({ book: "b2" }), { book: "b2", frontier: 44, chapter }), "Chapter 27");
});
t("the passage sheet shows only cited blocks before the frontier", () => {
  const blocks = [38, 39, 40, 41, 42, 43, 44].map((seq) => ({ seq, text: `b${seq}` }));
  const seqs = (fr, c = pc()) => passageBlocks(blocks, c, fr).map((b) => b.seq);
  assert.deepEqual(seqs(100), [40, 41, 42, 43]);
  assert.deepEqual(seqs(42), [40, 41]);       // the block being read and later never show
  assert.deepEqual(seqs(40), []);
  assert.deepEqual(seqs(null), []);
  assert.deepEqual(seqs(100, pc({ end: undefined })), [40]);
  assert.deepEqual(passageBlocks([{ seq: "41", text: "x" }, { seq: 41, text: null }, null], pc(), 100), []);
  assert.deepEqual(passageBlocks(undefined, pc(), 100), []);
});
