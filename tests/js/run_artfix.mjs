// artfix.js: the Fix flow's fault list, the body a Fix sends and the lines "How this was drawn" shows.
//   node tests/js/run_artfix.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { autoFixed, cleanFaults, fixBody, fixLine, fixLines, MAX_FAULTS } = await import(join(here, "../../src/lorekeeper/web/static/js/artfix.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

test("a check answer is cleaned: empty what dropped, text trimmed and cut", () => {
  assert.deepEqual(cleanFaults([{ what: "  the head is far too small ", where: "figure B" }, { what: "", where: "x" }, { where: "y" }, null, "z"]),
    [{ what: "the head is far too small", where: "figure B" }]);
  assert.equal(cleanFaults([{ what: "a".repeat(500), where: "b".repeat(500) }])[0].what.length, 200);
  assert.equal(cleanFaults([{ what: "a", where: "b".repeat(500) }])[0].where.length, 120);
  assert.deepEqual(cleanFaults({ faults: [] }), []);
  assert.deepEqual(cleanFaults([{ what: 3, where: {} }]), []);
});
test("at most eight faults are kept", () => {
  assert.equal(cleanFaults(Array.from({ length: 12 }, (_, i) => ({ what: `f${i}` }))).length, MAX_FAULTS);
  assert.equal(MAX_FAULTS, 8);
});
test("fixBody needs a fault or a note", () => {
  assert.equal(fixBody([{ what: " ", where: "figure A" }], "  "), null);
  assert.deepEqual(fixBody([{ what: "six fingers", where: "figure A" }], ""), { faults: [{ what: "six fingers", where: "figure A" }] });
  assert.deepEqual(fixBody([], " brighter "), { faults: [], note: "brighter" });
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

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
