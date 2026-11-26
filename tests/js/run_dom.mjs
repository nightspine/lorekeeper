// dom.js: h() and clear() never render a function child (a component passed instead of what it returns would print its
// source as text, as the picture sheet's "Through the book so far" Style field did in 0.23.0).
//   node tests/js/run_dom.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

// Just enough DOM for h(): elements with classList, attributes, append and text nodes.
class FakeNode {}
class FakeText extends FakeNode { constructor(t) { super(); this.data = t; } get textContent() { return this.data; } }
class FakeElement extends FakeNode {
  constructor(tag) { super(); this.tagName = tag; this.childNodes = []; this.attrs = {}; this.dataset = {}; this.style = {}; this.className = "";
    this.classList = { add: (c) => { this.className = `${this.className} ${c}`.trim(); } }; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener() {}
  append(...cs) { this.childNodes.push(...cs); }
  replaceChildren() { this.childNodes = []; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
}
globalThis.Node = FakeNode;
globalThis.document = { createElement: (t) => new FakeElement(t), createTextNode: (t) => new FakeText(t) };

const here = dirname(fileURLToPath(import.meta.url));
const { h, clear } = await import(join(here, "../../src/lorekeeper/web/static/js/dom.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };
function styleRow() { return "source code that must never show"; }
const errors = [];
console.error = (m) => { errors.push(String(m)); };

test("text and elements render as before", () => {
  const el = h("div.a#b", h("span", "one"), " two", null, false, ["three"]);
  assert.equal(el.textContent, "one twothree");
  assert.equal(el.className, "a"); assert.equal(el.id, "b");
});
test("a function child is skipped with a console error", () => {
  errors.length = 0;
  const el = h("div.art-field", h("span.label", "Style"), styleRow, "after");
  assert.equal(el.textContent, "Styleafter");
  assert.equal(errors.length, 1); assert.match(errors[0], /styleRow/);
});
test("a function in the props place is a child, so it is skipped too", () => {
  errors.length = 0;
  assert.equal(h("div", styleRow, "x").textContent, "x");
  assert.equal(errors.length, 1);
});
test("clear() and nested arrays skip a function too", () => {
  errors.length = 0;
  const el = h("div");
  clear(el, "a", [[styleRow]], "b");
  assert.equal(el.textContent, "ab"); assert.equal(errors.length, 1);
});
test("with LK_STRICT_DOM a function child throws", () => {
  globalThis.LK_STRICT_DOM = true;
  try { assert.throws(() => h("div", "a", styleRow), /styleRow/); } finally { delete globalThis.LK_STRICT_DOM; }
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
