// artsrc.js: picture variant URLs.   node tests/js/run_artsrc.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
const here = dirname(fileURLToPath(import.meta.url));
const { WIDTHS, fallbackWidth, isArtBase, srcsetFor, variantUrl } = await import(join(here, "../../src/lorekeeper/web/static/js/artsrc.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };
const base = "/api/art/6211b79150ee/ink/pa1b2c3d4";
test("widths are the contract's", () => assert.deepEqual(WIDTHS, [160, 320, 640, 1280]));
test("srcset lists every width", () => assert.equal(srcsetFor(base), WIDTHS.map((w) => `${base}?w=${w} ${w}w`).join(", ")));
test("only picture file urls get variants", () => {
  for (const u of [`${base}?w=160`, "/api/art/x/refs/abcdef", "https://x.test/a.png", "", null, "/api/art/a/b/c/d"]) { assert.equal(isArtBase(u), false, String(u)); assert.equal(srcsetFor(u), null); }
});
test("a width not served is refused", () => { assert.equal(variantUrl(base, 200), null); assert.equal(variantUrl(base, 320), `${base}?w=320`); });
test("fallback width covers 2x", () => { assert.equal(fallbackWidth(24), 160); assert.equal(fallbackWidth(80), 160); assert.equal(fallbackWidth(81), 320); assert.equal(fallbackWidth(5000), 1280); });
process.exit(failed ? 1 : 0);
