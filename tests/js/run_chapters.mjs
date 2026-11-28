// chapters.js against the shared vectors (tests/fixtures/chapter_numbering.json) and, when a cases file is given,
// against titles chapters.py was run on (tests/test_js_chapters.py writes it).
//   node tests/js/run_chapters.mjs [cases.json]
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { classify, label, numbering, numbersByStart, ownNumber, recapChapter, recapNumbers, roman } = await import(join(here, "../../src/lorekeeper/web/static/js/chapters.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

const fixture = JSON.parse(readFileSync(join(here, "../fixtures/chapter_numbering.json"), "utf8"));
for (const c of fixture.cases) test(`vector ${c.name}`, () => assert.deepEqual(numbering(c.entries), c.expected));

if (process.argv[2]) {
  const extra = JSON.parse(readFileSync(process.argv[2], "utf8"));
  for (const t of extra.titles) {
    test(`title ${JSON.stringify(t.title)}`, () => assert.deepEqual([classify(t.title), ownNumber(t.title)], [t.kind, t.number]));
  }
  for (const c of extra.books || []) test(`book ${c.name}`, () => assert.deepEqual(numbering(c.entries), c.expected));
}

test("numbers never depend on which rows a view lists, when the book states them", () => {
  const toc = ["Prologue", "One", "Two", "Three"];
  assert.equal(numbering(toc)[3].number, 3);
  assert.equal(numbering([toc[0], toc[2], toc[3]])[2].number, 3);
});
test("a title-only book is a count over the whole list", () => {
  assert.deepEqual(numbering(["Prologue", "The Arrival", "A Long Night", "The Storm"]).map((x) => x.number), [null, 1, 2, 3]);
});
test("restart after a divider", () => {
  assert.deepEqual(numbering(["Part I", "Chapter I", "Chapter II", "Part II", "Chapter I"]).map((x) => x.number), [null, 1, 2, null, 1]);
});
test("strings, dicts, roman, label", () => {
  assert.deepEqual(numbering([{ title: "One" }, "Two"]), [{ kind: "chapter", number: 1 }, { kind: "chapter", number: 2 }]);
  assert.equal(roman("XIV"), 14); assert.equal(roman("IIII"), null); assert.equal(roman("IC"), null);
  assert.equal(label(3), "Chapter 3"); assert.equal(label(null), "");
});

// The Reader labels rows by these maps, keyed by chapter, so which rows a view lists never moves a number.
test("chapter map: a chapter with no recap or event does not shift the next one's number", () => {
  const map = [{ title: "Prologue", start: 0 }, { title: "The Arrival", start: 3 }, { title: "A Long Night", start: 9 },
    { title: "The Storm", start: 14 }];
  const nums = numbersByStart(map);
  // Rows listed: only the prologue and "The Storm" (nothing revealed for the two between). The Storm is still 3.
  assert.equal(nums.get(14), 3);
  assert.equal(nums.get(0), null);
});
test("chapter map: the server's numbers win, null means none", () => {
  const nums = numbersByStart([{ title: "The Company", start: 0, number: null }, { title: "One", start: 4, number: 1 }, { title: "X", start: 8, number: 9 }]);
  assert.deepEqual([...nums], [[0, null], [4, 1], [8, 9]]);
});
test("chapter map from an older server: worked out with the story flags", () => {
  const nums = numbersByStart([{ title: "Contents", start: 0, story: false }, { title: "The Start", start: 2 }, { title: "Then", start: 5 }]);
  assert.deepEqual([...nums], [[0, null], [2, 1], [5, 2]]);
});
test("recaps: every chapter recap in book order, parts of one chapter joined", () => {
  const doc = {
    anchors: [{ id: "a1" }, { id: "a2" }, { id: "a3" }, { id: "a4" }, { id: "a5" }],
    recaps: [
      { id: "story", scope: "story", from: "a5", title: "Story so far" },
      { id: "r3", scope: "chapter", from: "a4", title: "The Storm" },   // out of order in the file
      { id: "r0", scope: "chapter", from: "a1", title: "Prologue" },
      { id: "r1a", scope: "chapter", from: "a2", title: "The Arrival" },
      { id: "r1b", scope: "chapter", from: "a3", title: "The Arrival" },
    ],
  };
  const nums = recapNumbers(doc);
  assert.deepEqual(Object.fromEntries(nums), { r0: null, r1a: 1, r1b: 1, r3: 2 });
  assert.equal(nums.has("story"), false);
});
test("recaps: a book that numbers its chapters keeps the numbers whichever recaps exist", () => {
  // "One" has no recap; "Two" and "Four" keep their numbers.
  const doc = { anchors: [{ id: "a1" }, { id: "a2" }, { id: "a3" }],
    recaps: [{ id: "c", scope: "chapter", from: "a1", title: "The Company" }, { id: "t", scope: "chapter", from: "a2", title: "Two" },
      { id: "f", scope: "chapter", from: "a3", title: "Four" }] };
  assert.deepEqual(Object.fromEntries(recapNumbers(doc)), { c: null, t: 2, f: 4 });
  assert.equal(recapNumbers(null).size, 0);
});

// recapChapter: which map chapter a chapter recap sits in. Blocks: a short heading (2 words) opens each chapter.
{
  //            ch XIV: 10..19          ch XV: 20..29          Part Two: 30   ch XVI: 31..40
  const map = [{ title: "Chapter XIV. Father of Whispers", start: 10 }, { title: "Chapter XV. The Bridge", start: 20 },
    { title: "Part Two", start: 30 }, { title: "Chapter XVI", start: 31 }];
  const words = Array.from({ length: 41 }, (_, k) => (map.some((c) => c.start === k) ? 2 : 120));
  const at = (seq, title) => recapChapter(map, seq, title, words)?.start ?? null;
  test("recapChapter: anchored on the next chapter's second block, goes to its own chapter", () => {
    assert.equal(at(21, "Chapter XIV. Father of Whispers"), 10);
    assert.equal(at(21, "  chapter xiv.   father OF whispers "), 10);   // case and spacing do not matter
  });
  test("recapChapter: parts of one long chapter stay together", () => {
    assert.equal(at(14, "Chapter XIV. Father of Whispers"), 10);
    assert.equal(at(18, "Chapter XIV. Father of Whispers"), 10);
    assert.equal(at(21, "Chapter XIV. Father of Whispers"), 10);
  });
  test("recapChapter: anchored in a chapter's first long paragraph with no matching title, stays there", () => {
    assert.equal(at(22, "Something else"), 20);
    assert.equal(at(22, ""), 20);
  });
  test("recapChapter: a hand-titled recap after an all-short opening goes back one", () => {
    assert.equal(at(21, "What happened at the bridge"), 10);
    assert.equal(at(20, "What happened at the bridge"), 10);   // on the heading itself
  });
  test("recapChapter: a short divider between is passed over, never more than one real chapter back", () => {
    assert.equal(at(32, "Chapter XV. The Bridge"), 20);
    assert.equal(at(32, "My own title"), 20);
    assert.equal(at(32, "Chapter XIV. Father of Whispers"), 20);   // XIV is out of reach: rule 2, one back
  });
  test("recapChapter: never after the anchor's chapter, null before the first", () => {
    assert.equal(at(14, "Chapter XV. The Bridge"), 10);
    assert.equal(at(5, "Chapter XIV. Father of Whispers"), null);
    assert.equal(recapChapter([], 5, "x", words), null);
    assert.equal(recapChapter(map, null, "x", words), null);
  });
  test("recapChapter: the first chapter has nothing to go back to", () => {
    assert.equal(at(11, "Anything"), 10);
  });
  test("recapChapter: two chapters with one title, decided by where the anchor is", () => {
    const twin = [{ title: "Interlude", start: 0 }, { title: "Interlude", start: 10 }];
    const w = Array.from({ length: 20 }, (_, k) => (k === 0 || k === 10 ? 1 : 50));
    assert.equal(recapChapter(twin, 11, "Interlude", w).start, 0);    // snapped past the first one's end
    assert.equal(recapChapter(twin, 14, "Interlude", w).start, 10);   // its own text before the anchor
  });
  test("recapChapter: without word counts, the title alone decides and nothing goes back by rule 2", () => {
    assert.equal(recapChapter(map, 21, "Chapter XIV. Father of Whispers", null).start, 10);
    assert.equal(recapChapter(map, 21, "My own title", null).start, 20);
  });
  test("recapChapter: a tidier is applied to both titles", () => {
    const up = [{ title: "CHAPTER I", start: 0 }, { title: "CHAPTER II", start: 10 }];
    const w = Array.from({ length: 20 }, (_, k) => (k % 10 ? 50 : 2));
    assert.equal(recapChapter(up, 11, "Chapter i", w, (t) => t.toLowerCase()).start, 0);
  });
}

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
