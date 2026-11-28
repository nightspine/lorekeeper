// artfix.js: the Fix flow's fault list, the body a Fix sends and the lines "How this was drawn" shows.
//   node tests/js/run_artfix.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { autoFixed, cutWords, beforeSrc, cleanFaults, cleanVerified, fixBody, fixLine, fixLines, MAX_FAULTS, panelTextsOf, panelBody, panelFixLine, panelWish, severityOf, verdictLine } = await import(join(here, "../../src/lorekeeper/web/static/js/artfix.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

test("a check answer is cleaned: empty what dropped, text trimmed and cut", () => {
  assert.deepEqual(cleanFaults([{ what: "  the head is far too small ", where: "figure B" }, { what: "", where: "x" }, { where: "y" }, null, "z"]),
    [{ what: "the head is far too small", where: "figure B", severity: "major" }]);
  assert.equal(cleanFaults([{ what: "a".repeat(500), where: "b".repeat(500) }])[0].what.length, 200);
  assert.equal(cleanFaults([{ what: "a", where: "b".repeat(500) }])[0].where.length, 120);
  assert.deepEqual(cleanFaults({ faults: [] }), []);
  assert.deepEqual(cleanFaults([{ what: 3, where: {} }]), []);
});
test("faults are cut at a word boundary, as art._cut_words cuts them", () => {
  const long = "the left hand of the man in the dark coat has six fingers and the thumb grows out of the wrist ".repeat(4);
  const what = cleanFaults([{ what: long }])[0].what;
  assert.ok(what.length <= 200 && long.startsWith(what) && long[what.length] === " ", what);
  const where = cleanFaults([{ what: "a", where: long }])[0].where;
  assert.ok(where.length <= 120 && long.startsWith(where) && long[where.length] === " ", where);
  // the same cases as the Python
  assert.equal(cutWords("one two three", 7), "one two");
  assert.equal(cutWords("one two, three", 8), "one two");
  assert.equal(cutWords("abcdefghij klm", 8), "abcdefgh");      // the only space is before half the limit: a hard cut
  assert.equal(cutWords("ab cdefghijkl", 8), "ab cdefg");
  assert.equal(cutWords("  a   sword \n through the hand. ", 200), "a sword through the hand");
  assert.equal(cutWords("one two \u2014 three", 9), "one two");
  assert.equal(cutWords(3, 10), "");
});
test("at most eight faults are kept", () => {
  assert.equal(cleanFaults(Array.from({ length: 12 }, (_, i) => ({ what: `f${i}` }))).length, MAX_FAULTS);
  assert.equal(MAX_FAULTS, 8);
});
test("fixBody needs a fault or a note", () => {
  assert.equal(fixBody([{ what: " ", where: "figure A" }], "  "), null);
  assert.deepEqual(fixBody([{ what: "six fingers", where: "figure A" }], ""), { faults: [{ what: "six fingers", where: "figure A", severity: "major" }] });
  assert.deepEqual(fixBody([], " brighter "), { faults: [], note: "brighter" });
  assert.deepEqual(fixBody([{ what: "a chair leg missing", where: "", severity: "minor" }]).faults[0].severity, "minor");
  assert.equal(fixBody([], "n".repeat(400)).note.length, 300);
});
test("a fix reads as one line", () => {
  assert.equal(fixLine({ faults: [{ what: "the head is far too small." }, { what: "the left hand has six fingers" }] }),
    "Fixed: the head is far too small; the left hand has six fingers.");
  assert.equal(fixLine({ faults: [{ what: "an extra arm" }], note: "keep the candle lit." }), "Fixed: an extra arm. Also: keep the candle lit.");
  assert.equal(fixLine({ faults: [], note: "the eyes open" }), "Fixed: the eyes open.");
  assert.equal(fixLine({ faults: [] }), null);
  assert.equal(fixLine(null), null);
});
test("a record's fixes, oldest first, the empty ones left out", () => {
  const rec = { fixes: [{ faults: [{ what: "a" }], created: "2026-11-26T10:00:00Z" }, { faults: [] }, { faults: [{ what: "b" }] }] };
  assert.deepEqual(fixLines(rec), [{ line: "Fixed: a.", created: "2026-11-26T10:00:00Z" }, { line: "Fixed: b.", created: null }]);
  assert.deepEqual(fixLines({}), []);
  assert.deepEqual(fixLines({ fixes: "x" }), []);
});
test("the sheet's line shows only after an automatic fix", () => {
  assert.equal(autoFixed({ check: { faults: [{ what: "a" }], fixed: true } }), true);
  assert.equal(autoFixed({ check: { faults: [{ what: "a" }], fixed: false } }), false);
  assert.equal(autoFixed({ fixes: [{ faults: [{ what: "a" }] }] }), false);
  assert.equal(autoFixed(null), false);
});

test("severity: minor only when it says so, else major", () => {
  assert.equal(severityOf({ severity: "minor" }), "minor");
  assert.equal(severityOf({ severity: "major" }), "major");
  assert.equal(severityOf({ severity: "MINOR" }), "major");
  assert.equal(severityOf({}), "major");
  assert.equal(severityOf(null), "major");
  assert.deepEqual(cleanFaults([{ what: "a", severity: "minor" }, { what: "b", severity: "huge" }]).map((f) => f.severity), ["minor", "major"]);
});
const sent = [{ what: "the left hand has six fingers." }, { what: "the chair has three legs" }, { what: "the eyes are at different heights" }];
const res = (...k) => k.map((fixed, j) => ({ n: j + 1, fixed }));
test("the verdict sentence after a fix", () => {
  assert.equal(verdictLine({ fixed: 3, partly: 0, of: 3, results: res("yes", "yes", "yes"), new: [] }, sent), "Fixed all 3.");
  assert.equal(verdictLine({ fixed: 1, partly: 0, of: 1, results: res("yes"), new: [] }, sent.slice(0, 1)), "Fixed it.");
  assert.equal(verdictLine({ fixed: 2, partly: 0, of: 2, results: res("yes", "yes"), new: [] }, sent.slice(0, 2)), "Fixed both.");
  assert.equal(verdictLine({ fixed: 2, partly: 0, of: 3, results: res("no", "yes", "yes"), new: [] }, sent),
    "Fixed 2 of 3. Still wrong: the left hand has six fingers.");
  assert.equal(verdictLine({ fixed: 0, partly: 1, of: 1, results: res("partly"), new: [] }, sent.slice(0, 1)),
    "Partly fixed: the left hand has six fingers.");
  assert.equal(verdictLine({ fixed: 1, partly: 1, of: 3, results: res("yes", "partly", "no"), new: [] }, sent),
    "Fixed 1 of 3. Partly fixed: the chair has three legs. Still wrong: the eyes are at different heights.");
  assert.equal(verdictLine({ fixed: 3, partly: 0, of: 3, results: res("yes", "yes", "yes"), new: [{ what: "a second sword in the hand." }] }, sent),
    "Fixed all 3. The fix added: a second sword in the hand.");
});
test("an unchecked fix, a note-only fix and a malformed verdict", () => {
  assert.equal(verdictLine(null, sent), "Fixed. The result couldn't be checked.");
  assert.equal(verdictLine(undefined), "Fixed. The result couldn't be checked.");
  assert.equal(verdictLine({ fixed: 4, partly: 0, of: 3 }, sent), "Fixed. The result couldn't be checked.");
  assert.equal(verdictLine({ fixed: "2", partly: 0, of: 3 }, sent), "Fixed. The result couldn't be checked.");
  assert.equal(verdictLine({ fixed: 0, partly: 0, of: 0, results: [], new: [] }, []), "Fixed.");
  // results with no fault to name still give the counts
  assert.equal(verdictLine({ fixed: 1, partly: 0, of: 3, results: [], new: [] }, []), "Fixed 1 of 3. 2 are still wrong.");
  assert.deepEqual(cleanVerified({ fixed: 1, partly: 0, of: 2, results: [{ n: 0, fixed: "yes" }, { n: 2, fixed: "maybe" }, { n: 1, fixed: "yes" }] }).results, [{ n: 1, fixed: "yes" }]);
  assert.equal(cleanVerified({ fixed: 0, partly: 0, of: 1, new: Array.from({ length: 5 }, (_, j) => ({ what: `n${j}` })) }).new.length, 3);
});
test("How this was drawn: the fix line carries its tally when checked", () => {
  assert.equal(fixLine({ faults: [{ what: "a" }, { what: "b" }, { what: "c" }], verified: { fixed: 2, partly: 0, of: 3 } }), "Fixed: a; b; c (2 of 3 put right).");
  assert.equal(fixLine({ faults: [{ what: "a" }, { what: "b" }], verified: { fixed: 1, partly: 1, of: 2 } }), "Fixed: a; b (1 of 2 put right, 1 partly).");
  assert.equal(fixLine({ faults: [{ what: "a" }], verified: null }), "Fixed: a.");
});
test("the before picture's address: the API's own route only, the position added", () => {
  assert.equal(beforeSrc("/api/library/k1/art/p0a1b2c3/before", { p: "toc4.para2", finished: ["b1", "b2"] }),
    "/api/library/k1/art/p0a1b2c3/before?p=toc4.para2&finished=b1%2Cb2");
  assert.equal(beforeSrc("/api/library/k1/art/p0a1b2c3/before?p=x", { p: "y" }), "/api/library/k1/art/p0a1b2c3/before?p=x");
  assert.equal(beforeSrc("/api/library/k1/art/p0a1b2c3/before", {}), "/api/library/k1/art/p0a1b2c3/before");
  assert.equal(beforeSrc("/api/library/k1/art/p0a1b2c3/before", { finished: [] }), "/api/library/k1/art/p0a1b2c3/before?finished=");
  for (const bad of ["https://evil.test/api/library/k/art/p1/before", "//evil.test/x", "/api/art/h/s/p1", "javascript:alert(1)", null, 3,
    "/api/library/k/art/../before", "/api/library/k/art/p1/before#x"]) assert.equal(beforeSrc(bad, { p: "a" }), null, String(bad));
});

test("panelTextsOf: one text per panel from the record, else none", () => {
  assert.deepEqual(panelTextsOf({ panels: ["a", "b"], panelTexts: ["x", "y"] }), ["x", "y"]);
  assert.equal(panelTextsOf({ panels: ["a", "b"], panelTexts: ["x"] }), null);
  assert.equal(panelTextsOf({ panels: ["a"], panelTexts: [1] }), null);
  assert.equal(panelTextsOf({ panels: ["a"] }), null);
  assert.equal(panelTextsOf(null), null);
});
test("panelBody: only a changed text or a note is something to fix", () => {
  assert.equal(panelBody(2, "same", "same", ""), null);
  assert.equal(panelBody(2, "  ", "was", ""), null);
  assert.deepEqual(panelBody(2, "new  text", "was", ""), { k: 2, text: "new text" });
  assert.deepEqual(panelBody(2, "same", "same", "brighter"), { k: 2, note: "brighter" });
  assert.deepEqual(panelBody(1, "x", "", "n"), { k: 1, text: "x", note: "n" });
  assert.equal(panelBody(1, "x".repeat(700), "", "").text.length, 600);
  assert.equal(panelWish(3, { text: "a boy awake" }).what, "panel 3 should show: a boy awake");
  assert.equal(panelWish(3, { note: "brighter" }).what, "brighter");
});
test("How this was drawn names the fixed panel", () => {
  assert.equal(panelFixLine({ panel: 3, text: "The boy sits up.", verified: { fixed: 1, partly: 0, of: 1 } }), "Fixed panel 3: The boy sits up (1 of 1 put right).");
  assert.equal(panelFixLine({ panel: 2, note: "brighter" }), "Fixed panel 2: brighter.");
  assert.equal(panelFixLine({ panel: 1, text: "w ".repeat(100) }).length < 140, true);
  assert.deepEqual(fixLines({ fixes: [{ panel: 3, text: "x y", created: "2026-11-28T10:00:00Z" }] }), [{ line: "Fixed panel 3: x y.", created: "2026-11-28T10:00:00Z" }]);
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
