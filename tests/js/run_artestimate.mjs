// artestimate.js: the pictures estimate line, one number or a range while some looks are not checked yet.
//   node tests/js/run_artestimate.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { estimateText, money } = await import(join(here, "../../src/lorekeeper/web/static/js/artestimate.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

test("money never makes up a number", () => {
  assert.equal(money(null), "price unknown");
  assert.equal(money(0.27), "about 27 cents");
  assert.equal(money(0.004), "about 1 cent");
  assert.equal(money(1.754), "about $1.75");
});
test("every look checked: one number, as before", () => {
  assert.equal(estimateText({ pictures: 8, price: 0.27, picturesMax: 8, priceMax: 0.27 }, 10), "About 8 pictures, about 27 cents.");
  assert.equal(estimateText({ pictures: 1, price: null }, 10), "About 1 picture, price unknown.");
});
test("an older server with no max keys: one number", () => {
  assert.equal(estimateText({ pictures: 52, price: 1.75 }, 10), "About 52 pictures, about $1.75.");
});
test("unchecked looks: a range with the higher price and why", () => {
  assert.equal(estimateText({ pictures: 8, price: 0.27, picturesMax: 24, priceMax: 0.81 }, 10),
    "8 to 24 pictures, up to about 81 cents. Characters whose look changes get more than one portrait.");
  assert.equal(estimateText({ pictures: 8, price: null, picturesMax: 24, priceMax: null }, 10),
    "8 to 24 pictures, price unknown. Characters whose look changes get more than one portrait.");
});
test("the cap note follows the price shown", () => {
  assert.equal(estimateText({ pictures: 8, price: 0.2, picturesMax: 24, priceMax: 0.6 }, 0.5),
    "8 to 24 pictures, up to about 60 cents. The run stops at $0.50. Characters whose look changes get more than one portrait.");
  assert.equal(estimateText({ pictures: 8, price: 12, picturesMax: 8, priceMax: 12 }, 10), "About 8 pictures, about $12.00. The run stops at $10.");
});
test("a max at or below the count is no range", () => {
  assert.equal(estimateText({ pictures: 8, price: 0.27, picturesMax: 3, priceMax: 0.1 }, 10), "About 8 pictures, about 27 cents.");
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
