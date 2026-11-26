// steps.js: the Reader's chapter steps and the entry's Age tile.
//   node tests/js/run_steps.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { nextName, statedAge, stepList, stepName, stepState, tickInfo, whereText } = await import(join(here, "../../src/lorekeeper/web/static/js/steps.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

// Ten blocks of ten words: block b starts at b/10 and ends at (b+1)/10.
const pos = { blockStart: (b) => b / 10, blockEnd: (b) => (b + 1) / 10 };
// Two blocks before the first contents entry ("Prologue: The Road" at block 2); the read route widens it to block 0.
const map = [{ title: "Prologue: The Road", start: 2, story: true }, { title: "The Body in the Library", start: 5, story: true }];
const toc = [{ n: 1, title: "Prologue: The Road", firstSeq: 0, lastSeq: 4 }, { n: 2, title: "The Body in the Library", firstSeq: 5, lastSeq: 9 }];

for (const [label, t] of [["with the table of contents", toc], ["with the chapter map only", null]]) {
  const list = stepList(map, t, pos);
  test(`${label}: a chapter is entered at the chapter map's start`, () => {
    assert.deepEqual(list.map((c) => [c.seq, c.from, c.to, c.k]), [[2, 0.2, 0.5, 1], [5, 0.5, 1, 2]]);
  });
  for (const p of [0, 0.1, 0.2 - 1e-6]) {
    test(`${label}: before the first chapter (p=${p}) its tick and the sentences name only its number`, () => {
      const st = stepState(list, p);
      assert.equal(st.cur, null);
      const info = tickInfo(list[0], st, p);
      assert.equal(info.state, "ahead");
      assert.equal(info.title, "");
      assert.ok(!/Prologue|Road/.test(info.label), info.label);
      assert.equal(nextName(st), "Chapter 1");
      assert.equal(whereText(st, p), "You haven't started Chapter 1 yet.");
      assert.ok(!/Body|Library/.test(tickInfo(list[1], st, p).label));
    });
  }
  test(`${label}: once entered the chapter is named, the next one still is not`, () => {
    const st = stepState(list, 0.3);
    assert.equal(st.cur, list[0]);
    assert.equal(tickInfo(list[0], st, 0.3).title, "Prologue: The Road");
    assert.equal(nextName(st), "Prologue");
    assert.equal(whereText(st, 0.3), "You're part way through Prologue.");
    assert.equal(tickInfo(list[1], st, 0.3).title, "");
  });
  test(`${label}: at the end of a chapter it is finished and the next is entered`, () => {
    const st = stepState(list, 0.5);
    assert.equal(tickInfo(list[0], st, 0.5).state, "done");
    assert.equal(st.last, list[0]);
    assert.equal(st.cur, list[1]);
    assert.equal(whereText(st, 0.5), "You're at the end of Prologue.");
    assert.equal(stepName(list[1]), "Chapter 2");
  });
  test(`${label}: the end of the book`, () => {
    const st = stepState(list, 1);
    assert.equal(st.next, null);
    assert.equal(nextName(st), null);
    assert.equal(whereText(st, 1), "You've finished the book.");
  });
}
test("front matter is not a step", () => {
  const list = stepList([{ title: "Contents", start: 0, story: false }, { title: "Chapter I", start: 1, story: true }], null, pos);
  assert.deepEqual(list.map((c) => [c.title, c.k]), [["Chapter I", 1]]);
});

test("age: the profile's first sentence about the entry", () => {
  assert.equal(statedAge("Lucy is a young woman aged nineteen. She lives in Whitby."), "aged nineteen");
  assert.equal(statedAge("Darrow was sixteen years old when he married."), "sixteen years old");
  assert.equal(statedAge("Nineteen years old, Lucy is engaged."), "Nineteen years old");
});
test("age: never someone else's", () => {
  assert.equal(statedAge("She nursed a child aged five."), null);
  assert.equal(statedAge("Arthur is the father of a boy aged five."), null);
  assert.equal(statedAge("Mina is a teacher, with a pupil aged ten."), null);
  assert.equal(statedAge("Mina is a teacher. Her pupil is aged ten."), null);
  assert.equal(statedAge("The house is two hundred years old."), null);
  assert.equal(statedAge(null, ["Lives with a sister aged six."]), null);
});
test("age: a detail that begins with it", () => {
  assert.equal(statedAge("A solicitor.", ["Aged twenty-two", "Lives in Exeter."]), "Aged twenty-two");
  assert.equal(statedAge("", ["Is thirty years old."]), "thirty years old");
  assert.equal(statedAge(undefined, []), null);
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
