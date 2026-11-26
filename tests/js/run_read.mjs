// readnames.js: the reading view's name lighting, against cases made by mentions.py (tests/test_js_read.py).
//   node tests/js/run_read.mjs CASES.json
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { ALWAYS, HONORIFICS, PARTICLES, derive, litForms, litSpans, matcher } = await import(join(here, "../../src/lorekeeper/web/static/js/readnames.js"));
const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

test("the title and particle lists are merge.py's", () => {
  assert.deepEqual([...HONORIFICS].sort(), cases.honorifics);
  assert.deepEqual([...PARTICLES].sort(), cases.particles);
});

test("derived forms are mentions.derive's", () => {
  const got = Object.fromEntries([...derive(cases.entities)].map(([id, forms]) => [id, [...new Set(forms.map(([f]) => f))]]));
  assert.deepEqual(got, cases.derived);
});

// A view as gating.js gives it: the display name, then the other names as aliases.
const view = { entities: cases.entities.map((e) => ({ id: e.id, type: e.type, name: e.names[0], aliases: e.names.slice(1) })) };
const everyType = new Set([...ALWAYS, "place", "group", "object", "concept", "term"]);

test("matches are mentions.Matcher's (every name revealed)", () => {
  const forms = litForms(view, everyType, () => -Infinity);
  const re = matcher(forms);
  const owners = new Map(forms);
  for (const { text, spans } of cases.matches) {
    const got = litSpans(text, re, owners, 0).map((x) => [x.start, x.end, [...x.ids].sort()]);
    assert.deepEqual(got, spans, text);
  }
});

test("people and creatures only, without the toggle", () => {
  const forms = litForms(view, ALWAYS, () => -Infinity);
  assert.ok(!forms.some(([f]) => f.startsWith("Whitby")));
  assert.ok(forms.some(([f]) => f === "Bersicker"));
});

// The reveal rule: block b ends at (b + 1) / 10. "Mina Murray" is revealed at the end of block 3, "Mrs. Mina Harker" at
// the end of block 6, and the forms derived from each ("Mina") at their source's reveal, the earliest one winning.
const reveal = { "Mrs. Mina Harker": 0.7, "Mina Murray": 0.4 };
const nameAt = (id, value) => (id === "mina" ? reveal[value] : -Infinity);
const forms = litForms(view, ALWAYS, nameAt);
const re = matcher(forms);
const owners = new Map(forms);
const lit = (text, b) => litSpans(text, re, owners, (b + 1) / 10).filter((x) => x.ids.includes("mina")).map((x) => text.slice(x.start, x.end));

test("a name is never lit in a block before its own reveal", () => {
  for (let b = 0; b < 10; b++) {
    const got = lit("Mina Murray wrote; Mrs. Mina Harker answered; Mina slept.", b);
    const want = [...(b >= 3 ? ["Mina Murray"] : []), ...(b >= 6 ? ["Mrs. Mina Harker"] : []), ...(b >= 3 ? ["Mina"] : [])];
    assert.deepEqual(got, want, `block ${b}`);
  }
});

test("a longer name not yet revealed is left unlit, never searched for a shorter one", () => {
  assert.deepEqual(lit("Mrs. Mina Harker", 4), []);
});

test("a name with an unresolved anchor (never revealed) is never lit", () => {
  const f = litForms(view, ALWAYS, (id) => (id === "dracula" ? Infinity : -Infinity));
  assert.ok(!f.some(([, o]) => o.has("dracula")));
});

console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
