// chapters.js against the shared vectors (tests/fixtures/chapter_numbering.json, recap_chapter.json) and, when a cases file is given,
// against titles chapters.py was run on (tests/test_js_chapters.py writes it).
//   node tests/js/run_chapters.mjs [cases.json]
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { artChapterKeys, classify, label, numbering, numbersByStart, ownNumber, plateChapter, recapChapter, recapNumbers, roman } = await import(join(here, "../../src/lorekeeper/web/static/js/chapters.js"));
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

// recapChapter: which map chapter a chapter recap sits in, against the vectors chapters.py's recap_chapter also runs.
{
  const rc = JSON.parse(readFileSync(join(here, "../fixtures/recap_chapter.json"), "utf8"));
  const tidiers = { lower: (t) => t.toLowerCase() };
  for (const c of rc.cases) {
    test(`recapChapter: ${c.name}`, () => {
      const map = rc.maps[c.map];
      const words = c.words === null ? null : rc.blockWords[c.words];
      const got = recapChapter(map, c.seq, c.title, words, c.tidy ? tidiers[c.tidy] : undefined);
      assert.equal(got ? got.start : null, c.expected);
    });
  }
}

// plateChapter / artChapterKeys: a plate is anchored at its chapter's last anchor, often its own recap's, in the next
// chapter's first long paragraph; it goes in the row it was drawn for, keyed as art.py keys it.
{
  const rc = JSON.parse(readFileSync(join(here, "../fixtures/recap_chapter.json"), "utf8"));
  const upper = rc.maps.upper, uw = rc.blockWords.upper;   // CHAPTER I at 0, CHAPTER II at 10, each opening on a 2-word heading
  test("plateChapter: a plate anchored at its own recap in the next chapter's second block goes to its own chapter", () => {
    assert.equal(plateChapter(upper, 11, "CHAPTER I", uw).start, 0);
  });
  test("plateChapter: a plate anchored in its own chapter stays there", () => {
    assert.equal(plateChapter(upper, 15, "CHAPTER II", uw).start, 10);
    assert.equal(plateChapter(upper, 11, "CHAPTER II", uw).start, 10);   // an event in II's first long paragraph
  });
  test("plateChapter: never later than the anchor's chapter, null before the first", () => {
    assert.equal(plateChapter(upper, 5, "CHAPTER II", uw).start, 0);
    assert.equal(plateChapter(upper, -1, "CHAPTER I", uw), null);
    assert.equal(plateChapter([], 3, "CHAPTER I", uw), null);
  });
  test("plateChapter: without block words it falls back on the title near the anchor", () => {
    assert.equal(plateChapter(upper, 11, "CHAPTER I", null).start, 0);
  });
  const twin = rc.maps.twin, tw = rc.blockWords.twin;   // two "Interlude" chapters, at 0 and 10
  const keys = artChapterKeys(twin, [3, 13], [], tw);
  test("artChapterKeys: a repeated title is keyed \" (2)\"", () => {
    assert.deepEqual([...keys], [[0, "Interlude"], [10, "Interlude (2)"]]);
  });
  test("plateChapter: a repeated title is told apart by its key", () => {
    // The first Interlude's plate anchored in the second's first long paragraph (snapped): the first.
    assert.equal(plateChapter(twin, 11, "Interlude", tw, keys).start, 0);
    // The second Interlude's plate, anchored at an event in its own first long paragraph: the second, where the title
    // alone (both match, snapped) would have gone back one.
    assert.equal(plateChapter(twin, 11, "Interlude (2)", tw, keys).start, 10);
    assert.equal(plateChapter(twin, 11, "Interlude (2)", tw).start, 0);   // without keys: the recap rule on the bare title
  });
  test("artChapterKeys: a recap counts in the chapter it summarises, as art.py counts it", () => {
    // CHAPTER I has only its recap, anchored in II's second block; II has an event.
    assert.deepEqual([...artChapterKeys(upper, [15], [{ seq: 11, title: "CHAPTER I" }], uw)], [[0, "CHAPTER I"], [10, "CHAPTER II"]]);
    // A recap of II anchored in II, no events: only II is listed.
    assert.deepEqual([...artChapterKeys(upper, [], [{ seq: 15, title: "CHAPTER II" }], uw)], [[10, "CHAPTER II"]]);
    // Two Interludes, the first with only a recap anchored in the second's first long paragraph: both listed.
    assert.deepEqual([...artChapterKeys(twin, [13], [{ seq: 11, title: "Interlude" }], tw)], [[0, "Interlude"], [10, "Interlude (2)"]]);
  });
}

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
