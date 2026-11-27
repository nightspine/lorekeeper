// styleref.js: the browser's checks on a style's reference picture, and the cost line of "Describe it for me".
//   node tests/js/run_styleref.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { sniff, refProblem, smallCost, describedLine, REF_MAX_BYTES } = await import(join(here, "../../src/lorekeeper/web/static/js/styleref.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };
const bytes = (...xs) => new Uint8Array([...xs, ...new Array(16).fill(0)]);

test("sniff reads the kind from the first bytes only", () => {
  assert.equal(sniff(bytes(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)), "png");
  assert.equal(sniff(bytes(0xff, 0xd8, 0xff, 0xe0)), "jpg");
  assert.equal(sniff(bytes(0x52, 0x49, 0x46, 0x46, 1, 2, 3, 4, 0x57, 0x45, 0x42, 0x50)), "webp");
  assert.equal(sniff(bytes(0x52, 0x49, 0x46, 0x46, 1, 2, 3, 4, 0x57, 0x41, 0x56, 0x45)), null);   // a RIFF WAVE
  assert.equal(sniff(bytes(0x47, 0x49, 0x46, 0x38)), null);   // GIF
  assert.equal(sniff(new Uint8Array([0x89, 0x50])), null);
  assert.equal(sniff(null), null);
});
test("refProblem names the rule that failed", () => {
  assert.equal(refProblem({ kind: null, size: 10 }), "Choose a PNG, JPEG or WebP picture.");
  assert.equal(refProblem({ kind: "png", size: REF_MAX_BYTES + 1, width: 512, height: 512 }), "This picture is over 8 MB. Choose a smaller one.");
  assert.equal(refProblem({ kind: "png", size: REF_MAX_BYTES, width: 512, height: 512 }), "");
  assert.equal(refProblem({ kind: "jpg", size: 1, width: null, height: null }), "This file could not be read as a picture.");
  assert.match(refProblem({ kind: "jpg", size: 1, width: 255, height: 900 }), /at least 256 pixels/);
  assert.equal(refProblem({ kind: "jpg", size: 1, width: 256, height: 8192 }), "");
  assert.match(refProblem({ kind: "webp", size: 1, width: 300, height: 8193 }), /at most 8192 pixels/);
  assert.equal(refProblem({ kind: "webp", size: 1 }), "");   // not decoded yet: only the kind and size so far
});
test("smallCost never makes up a number", () => {
  assert.equal(smallCost(null), "price unknown");
  assert.equal(smallCost(0), "price unknown");
  assert.equal(smallCost(0.0001), "less than 0.1 cent");
  assert.equal(smallCost(0.001), "0.1 cent");
  assert.equal(smallCost(0.0042), "0.4 cent");
  assert.equal(smallCost(0.01), "1 cent");
  assert.equal(smallCost(0.031), "3 cents");
  assert.equal(smallCost(1.2), "$1.20");
});
test("describedLine", () => {
  assert.equal(describedLine("Claude Haiku 5.5", 0.001), "Described by Claude Haiku 5.5, 0.1 cent.");
  assert.equal(describedLine("", 0), "Described by the model, price unknown.");
});
if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
