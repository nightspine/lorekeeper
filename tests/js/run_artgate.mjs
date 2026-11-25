// artgate.js: which pictures and which picture text may show at a reading position.
//   node tests/js/run_artgate.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { visibleArt, visibleAt, visibleWorld, unfinishedSources, scopeAt, faceScopeAt, faceIndex } = await import(join(here, "../../src/lorekeeper/web/static/js/artgate.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

// Two books of a series whose anchor ids repeat (as the Sherlock indexes' ch1.para01 do), in a different order.
const ords = (ids) => new Map(ids.map((id, i) => [id, i]));
const book1 = ords(["ch1.para01", "ch1.para09", "ch2.para03", "ch3.para12"]);
const book2 = ords(["front.para01", "ch1.para01", "ch1.para20", "ch2.para07", "ch3.para02"]);
const pic = (id, from, kind = "portrait") => ({ id, kind, from, target: "t" });
const ids = (list) => list.map((r) => r.id).sort();
const set = { pictures: [pic("a", "ch1.para01"), pic("b", "ch1.para20", "event"), pic("c", "ch3.para02", "plate"), pic("d", "ch9.para99")] };

test("the frontier: at or before shows, after does not", () => {
  assert.deepEqual(ids(visibleArt(set, book2, book2.get("ch1.para20"), false)), ["a", "b"]);
  assert.deepEqual(ids(visibleArt(set, book2, book2.get("ch1.para01"), false)), ["a"]);
  assert.deepEqual(ids(visibleArt(set, book2, book2.get("ch3.para02"), false)), ["a", "b", "c"]);
});
test("before the first anchor nothing shows", () => {
  assert.deepEqual(visibleArt(set, book2, -1, false), []);
  assert.deepEqual(visibleArt(set, book2, null, false), []);
  assert.deepEqual(visibleArt(set, book2, undefined, false), []);
});
test("a finished earlier book shows whole", () => {
  assert.deepEqual(ids(visibleArt(set, book2, -1, true)), ["a", "b", "c"]);
});
test("an unknown anchor never shows, finished or not", () => {
  assert.ok(!ids(visibleArt(set, book2, 99, true)).includes("d"));
  assert.ok(!ids(visibleArt(set, book2, 99, false)).includes("d"));
});
test("colliding ids: a book is gated by its own anchors only", () => {
  // ch1.para01 is ordinal 0 in book 1 and 1 in book 2; book 1's map knows nothing of ch1.para20.
  const b1 = { pictures: [pic("x", "ch1.para01"), pic("y", "ch2.para03"), pic("z", "ch1.para20")] };
  assert.deepEqual(ids(visibleArt(b1, book1, book1.get("ch1.para01"), false)), ["x"]);
  assert.deepEqual(ids(visibleArt(b1, book1, -1, true)), ["x", "y"]);   // z is not an anchor of book 1
  assert.equal(visibleAt("ch1.para01", book2, 0, false), false);         // front.para01 only: book 2's ch1.para01 is ordinal 1
  assert.equal(visibleAt("ch1.para01", book1, 0, false), true);
});
test("malformed records are skipped", () => {
  const bad = { pictures: [null, { id: "n" }, { id: 3, kind: "plate", from: "ch1.para01" }, pic("ok", "ch1.para01")] };
  assert.deepEqual(ids(visibleArt(bad, book2, 4, false)), ["ok"]);
  assert.deepEqual(visibleArt(null, book2, 4, false), []);
  assert.deepEqual(visibleArt({ pictures: "x" }, book2, 4, false), []);
});
test("visibleAt follows the same rule", () => {
  assert.equal(visibleAt("ch2.para07", book2, 3, false), true);
  assert.equal(visibleAt("ch2.para07", book2, 2, false), false);
  assert.equal(visibleAt("ch2.para07", book2, -1, true), true);
  assert.equal(visibleAt("nope", book2, 9, true), false);
  assert.equal(visibleAt(undefined, book2, 9, true), false);
});

test("unfinishedSources: listed books that are not finished; the book being read never counts", () => {
  assert.deepEqual(unfinishedSources(["b1", "b2"], { book: "b3", finished: ["b1"] }), ["b2"]);
  assert.deepEqual(unfinishedSources(["b1", "b3"], { book: "b3", finished: ["b1"] }), []);
  assert.deepEqual(unfinishedSources([], { book: "b3", finished: [], earlier: ["b1"], fallback: true }), []);
  assert.deepEqual(unfinishedSources(undefined, { book: "b3", finished: ["b1"], earlier: ["b1", "b2"], fallback: true }), ["b2"]);
  assert.deepEqual(unfinishedSources(undefined, { book: "b3", finished: [], earlier: ["b1"] }), []);
});

const at = (id) => book2.get(id);
test("world note of this book: shown once its anchor is reached", () => {
  const w = { text: "Gaslit towns.", from: "ch2.para07" };
  assert.equal(visibleWorld(w, { ords: book2, position: at("ch1.para20") }), null);
  assert.deepEqual(visibleWorld(w, { ords: book2, position: at("ch2.para07") }), { text: "Gaslit towns.", own: true, from: "ch2.para07" });
});
test("an appended note waits for the earlier books it carries", () => {
  const w = { text: "Book one's world. And a new sea.", from: "ch1.para20", base: "Book one's world.", inherits: ["b1"] };
  const where = { ords: book2, position: at("ch3.para02"), book: "b2", earlier: ["b1"] };
  assert.equal(visibleWorld(w, { ...where, finished: [] }), null);
  assert.ok(visibleWorld(w, { ...where, finished: ["b1"] }));
  // No `inherits` on an appended note: every earlier book must be finished.
  const old = { ...w }; delete old.inherits;
  assert.equal(visibleWorld(old, { ...where, finished: [] }), null);
  assert.ok(visibleWorld(old, { ...where, finished: ["b1"] }));
});
test("an inherited note is never read as this book's, even when its anchor id collides", () => {
  // Its `from` is book 1's ch1.para01; book 2 also has a ch1.para01, already passed.
  const w = { text: "Book one's world.", from: "ch1.para01", inherited: true, inherits: ["b1"] };
  const where = { ords: book2, position: at("ch2.para07"), book: "b2", earlier: ["b1"] };
  assert.equal(visibleWorld(w, { ...where, finished: [] }), null);
  assert.deepEqual(visibleWorld(w, { ...where, finished: ["b1"] }), { text: "Book one's world.", own: false });
  const bare = { text: "Book one's world.", from: "ch1.para01", inherited: true };
  assert.equal(visibleWorld(bare, { ...where, finished: [] }), null);
});
test("no note, or an empty one, shows nothing", () => {
  assert.equal(visibleWorld(null, { ords: book2, position: 4 }), null);
  assert.equal(visibleWorld({ text: "  ", from: "ch1.para01" }, { ords: book2, position: 4 }), null);
});


// Story time: a frame story (s1) whose chapters tell a past strand (s2) after a cue paragraph.
const timeline = { runs: [{ seq: 0, scope: null }, { seq: 3, scope: "s1.1" }, { seq: 100, scope: null }, { seq: 112, scope: "s2.1" },
  { seq: 300, scope: null }, { seq: 300, scope: "s1.1" }] };
test("scopeAt: the run at or before the block, unknown before a chapter's cue", () => {
  assert.equal(scopeAt(timeline, 0), null);
  assert.equal(scopeAt(timeline, 2), null);
  assert.equal(scopeAt(timeline, 3), "s1.1");
  assert.equal(scopeAt(timeline, 99), "s1.1");
  assert.equal(scopeAt(timeline, 100), null);   // the new chapter has begun, its cue not reached: never switch early
  assert.equal(scopeAt(timeline, 111), null);
  assert.equal(scopeAt(timeline, 112), "s2.1");
  assert.equal(scopeAt(timeline, 300), "s1.1");   // a cue on the chapter's first block applies
});
test("scopeAt: at one seq a scope beats null, whichever is listed last", () => {
  const nullFirst = { runs: [{ seq: 0, scope: "s2.1" }, { seq: 300, scope: null }, { seq: 300, scope: "s1.1" }] };
  const nullLast = { runs: [{ seq: 0, scope: "s2.1" }, { seq: 300, scope: "s1.1" }, { seq: 300, scope: null }] };
  for (const tl of [nullFirst, nullLast]) {
    assert.equal(scopeAt(tl, 300), "s1.1");
    assert.equal(scopeAt(tl, 450), "s1.1");
    assert.equal(faceScopeAt(tl, 300), "s1.1");
  }
});
test("faceScopeAt: before a chapter's cue the face keeps the last known scope; a later run never applies", () => {
  assert.equal(faceScopeAt(timeline, 2), null);      // before the book's first cue: unknown, the latest picture
  assert.equal(faceScopeAt(timeline, 99), "s1.1");
  assert.equal(faceScopeAt(timeline, 100), "s1.1");  // the chapter has begun, its cue (112) not reached: no switch
  assert.equal(faceScopeAt(timeline, 111), "s1.1");
  assert.equal(faceScopeAt(timeline, 112), "s2.1");
  assert.equal(faceScopeAt(timeline, 299), "s2.1");
  assert.equal(faceScopeAt(timeline, 300), "s1.1");
  // The reviewer's case: youth, frame, then an unscoped picture; the face holds on the frame portrait until the cue.
  const pics = [{ rec: { id: "youth", scope: "s2.1" } }, { rec: { id: "frame", scope: "s1.1" } }, { rec: { id: "legacy" } }];
  for (const seq of [99, 100, 111]) assert.equal(pics[faceIndex(pics, faceScopeAt(timeline, seq))].rec.id, "frame");
  assert.equal(pics[faceIndex(pics, faceScopeAt(timeline, 112))].rec.id, "youth");
});
test("scopeAt: no timeline, no block, malformed runs", () => {
  assert.equal(scopeAt(null, 5), null);
  assert.equal(scopeAt({}, 5), null);
  assert.equal(scopeAt(timeline, null), null);
  assert.equal(scopeAt({ runs: [{ seq: "1", scope: "s1.1" }, { seq: 2, scope: 7 }, null, { seq: 3, scope: "" }] }, 9), null);
  // unsorted input still reads by seq
  assert.equal(scopeAt({ runs: [{ seq: 50, scope: "s2.1" }, { seq: 1, scope: "s1.1" }] }, 10), "s1.1");
});
test("faceIndex: the latest picture of the reader's scope, else the latest", () => {
  const pics = [{ rec: { id: "young", scope: "s2.1" } }, { rec: { id: "old", scope: "s1.1" } }, { rec: { id: "legacy" } }];
  assert.equal(faceIndex(pics, "s2.1"), 0);
  assert.equal(faceIndex(pics, "s1.1"), 1);
  assert.equal(faceIndex(pics, null), 2);       // no scope known yet (before the book's first cue): the latest, as today
  assert.equal(faceIndex(pics, "s3.1"), 2);     // no picture of that time: the latest
  assert.equal(faceIndex([pics[2]], "s1.1"), 0); // a single-scope figure with a legacy record: unchanged
  assert.equal(faceIndex([], "s1.1"), -1);
  // the choice is only among what is visible: a later scoped picture past the position is never passed in
  const visible = visibleArt({ pictures: [{ id: "a", kind: "portrait", from: "ch1.para01", target: "t", scope: "s1.1" },
    { id: "b", kind: "portrait", from: "ch3.para12", target: "t", scope: "s2.1" }] }, book1, book1.get("ch2.para03"), false).map((rec) => ({ rec }));
  assert.equal(visible[faceIndex(visible, "s2.1")].rec.id, "a");
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
