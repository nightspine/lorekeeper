// artrefs.js: the rules of the person's own reference pictures (the picture sheet's row and the lightbox's list).
//   node tests/js/run_artrefs.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const R = await import(join(here, "../../src/lorekeeper/web/static/js/artrefs.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };
const sha = (c) => c.repeat(64);
const url = (c) => `/api/art/6211b791/refs/${sha(c)}`;

test("cleanNote collapses whitespace and keeps 120 characters", () => {
  assert.equal(R.cleanNote("  use this\n for   her dress "), "use this for her dress");
  assert.equal(R.cleanNote("x".repeat(200)).length, R.REF_NOTE_CHARS);
  assert.equal(R.cleanNote(null), "");
  assert.equal(R.cleanNote(42), "");
});
test("refUrl accepts only the refs route's own addresses", () => {
  assert.equal(R.refUrl(url("a")), url("a"));
  assert.equal(R.refUrl(`https://evil.example${url("a")}`), null);
  assert.equal(R.refUrl("/api/art/6211/ink/p0123abcd"), null);
  assert.equal(R.refUrl(`/api/art/../refs/${sha("a")}`), null);
  assert.equal(R.refUrl(`/api/art/x/refs/${sha("A")}`), null);   // a sha is lowercase hex
  assert.equal(R.refUrl("javascript:alert(1)"), null);
  assert.equal(R.refUrl({ url: url("a") }), null);
});
test("cleanUserRefs keeps well-formed, distinct items, at most four", () => {
  assert.deepEqual(R.cleanUserRefs(null), []);
  assert.deepEqual(R.cleanUserRefs({ sha: sha("a") }), []);
  const out = R.cleanUserRefs([
    { sha: sha("a"), ext: "png", note: " her  dress ", url: url("a"), description: "A long red dress." },
    { sha: sha("a"), ext: "png", note: "again" },          // the same picture twice
    { sha: "nope", ext: "png" },                             // not a sha
    { sha: sha("b"), ext: "gif", url: "http://x/y" },        // an odd ext and a foreign url are dropped, the item kept
    { sha: sha("c") }, { sha: sha("d") }, { sha: sha("e") },
  ]);
  assert.equal(out.length, R.MAX_REFS);
  assert.deepEqual(out[0], { sha: sha("a"), ext: "png", note: "her dress", description: "A long red dress.", url: url("a") });
  assert.deepEqual(out[1], { sha: sha("b"), ext: null, note: "", description: null, url: null });
  assert.deepEqual(out.map((x) => x.sha), [sha("a"), sha("b"), sha("c"), sha("d")]);
});
test("refsBody sends sha and note only, never an upload still in flight", () => {
  const items = [
    { sha: sha("a"), note: "the house", url: url("a"), description: "x" },
    { sha: sha("b"), note: "" },
    { sha: null, uploading: true },
    { sha: sha("c"), uploading: true },
  ];
  assert.deepEqual(R.refsBody(items), [{ sha: sha("a"), note: "the house" }, { sha: sha("b") }]);
  assert.deepEqual(R.refsBody([]), []);
  assert.deepEqual(R.refsBody(null), []);
});
test("sameRefs compares pictures, notes and order", () => {
  const a = [{ sha: sha("a"), note: "x" }, { sha: sha("b") }];
  assert.ok(R.sameRefs(a, [{ sha: sha("a"), note: " x " }, { sha: sha("b"), note: "" }]));
  assert.ok(!R.sameRefs(a, [{ sha: sha("b") }, { sha: sha("a"), note: "x" }]));
  assert.ok(!R.sameRefs(a, [{ sha: sha("a"), note: "y" }, { sha: sha("b") }]));
  assert.ok(!R.sameRefs(a, [{ sha: sha("a"), note: "x" }]));
  assert.ok(R.sameRefs([], []));
});
test("takesReferences reads the image model row, default yes", () => {
  const p = { images: { default: "m1", models: [{ id: "m1" }, { id: "m2", references: false }] } };
  assert.equal(R.takesReferences(p, "m1"), true);
  assert.equal(R.takesReferences(p, "m2"), false);
  assert.equal(R.takesReferences(p, null), true);                         // the provider's default
  assert.equal(R.takesReferences({ images: { default: "m2", models: p.images.models } }, null), false);
  assert.equal(R.takesReferences({ id: "gemini" }, null), true);          // an older server says nothing
});
test("describeModel picks a strong model that can see, else a cheap one, never Ollama", () => {
  const claude = { id: "claude", ok: true, strong: "s", cheap: "c", models: [{ id: "s", label: "Sonnet", price: [3, 15] }, { id: "c", label: "Haiku", price: [1, 5] }] };
  const groq = { id: "groq", ok: true, strong: "g", cheap: "g", models: [{ id: "g", label: "Llama", vision: false }] };
  const blindStrong = { id: "x", ok: true, strong: "s", cheap: "c", models: [{ id: "s", vision: false }, { id: "c", label: "Cheap", price: [1, 2] }] };
  assert.equal(R.describeModel([claude]).model.label, "Sonnet");
  assert.equal(R.describeModel([groq, claude]).provider.id, "claude");
  assert.equal(R.describeModel([groq]), null);
  assert.equal(R.describeModel([{ id: "ollama", ok: true, strong: "q", models: [{ id: "q" }] }]), null);
  assert.equal(R.describeModel([blindStrong]).model.label, "Cheap");
  assert.equal(R.describeModel([{ ...claude, ok: false }]), null);
  // a saved key is enough, as JobManager.describe_model reads configured()
  assert.equal(R.describeModel([{ ...claude, ok: false, configured: true }]).model.label, "Sonnet");
  assert.equal(R.describeModel([]), null);
  assert.equal(R.describeModel(null), null);
});
test("describeModel follows the server's own describe flag when it sends one", () => {
  const claude = { id: "claude", label: "Claude", ok: true, strong: "s", cheap: "c", models: [{ id: "s", label: "Sonnet", price: [2, 10] }, { id: "c", label: "Haiku", price: [1, 5] }] };
  const gemini = { id: "gemini", label: "Gemini", ok: true, strong: "gs", cheap: "gc", models: [{ id: "gs", label: "Gemini Pro" }, { id: "gc", label: "Flash" }] };
  // top level: the answer names the model, or says none
  assert.equal(R.describeModel([claude, gemini], { provider: "gemini", model: "gc" }).model.label, "Flash");
  assert.equal(R.describeModel([claude, gemini], null), null);
  assert.equal(R.describeModel([claude, gemini], false), null);
  assert.equal(R.describeModel([claude], { provider: "custom", model: "m" }).model.id, "m");   // a model the rows don't list
  // per row: true, a model id, or { model }; false on every flagged row is none
  assert.equal(R.describeModel([{ ...claude, describe: false }, { ...gemini, describe: true }]).model.label, "Gemini Pro");
  assert.equal(R.describeModel([{ ...claude, describe: "c" }]).model.label, "Haiku");
  assert.equal(R.describeModel([{ ...claude, describe: { model: "s" } }]).model.label, "Sonnet");
  assert.equal(R.describeModel([{ ...claude, describe: false }, { ...gemini, describe: false }]), null);
  // the flag wins over the guess: a working provider with no flag is not picked when others carry one
  assert.equal(R.describeModel([claude, { ...gemini, describe: false }]), null);
});
test("describePrice and describeCost never make up a number", () => {
  const m = { price: [3, 15] };
  const one = (R.DESCRIBE_TOKENS[0] * 3 + R.DESCRIBE_TOKENS[1] * 15) / 1e6;
  assert.ok(Math.abs(R.describePrice(m, 1) - one) < 1e-12);
  assert.ok(Math.abs(R.describePrice(m, 3) - 3 * one) < 1e-12);
  assert.equal(R.describePrice({ price: null }, 1), null);
  assert.equal(R.describePrice(m, 0), null);
  assert.equal(R.describeCost({ price: null }), null);
  assert.equal(R.describeCost({ price: [0.1, 0.5] }, 1), "less than 1 cent");
  assert.equal(R.describeCost({ price: [3, 15] }, 2), "1 cent");
  assert.equal(R.describeCost({ price: [3, 15] }, 4), "3 cents");
  assert.equal(R.describeCost({ price: [300, 1500] }, 4), "$2.70");
  // the bench: about $0.0045 for one picture on Sonnet ($2 in, $10 out)
  assert.ok(Math.abs(R.describePrice({ price: [2, 10] }, 1) - 0.0045) < 1e-9);
  assert.equal(R.describeCost({ price: [2, 10] }, 1), "less than 1 cent");
});
test("refPath and pathLine say which way the pictures go, and to whom", () => {
  assert.equal(R.refPath({ takes: true, describer: null }), "pictures");
  assert.equal(R.refPath({ takes: false, describer: {} }), "words");
  assert.equal(R.refPath({ takes: false, describer: null }), "none");
  assert.equal(R.pathLine({ path: "pictures", image: "Nano Banana 2.1", provider: "Gemini" }),
    "Sent to Nano Banana 2.1 as pictures. They go to Gemini with this picture.");
  const w = R.pathLine({ path: "words", image: "Paint 1", provider: "OpenAI", describer: "Sonnet", describerProvider: "Claude", price: "2 cents" });
  assert.equal(w, "Paint 1 can't take pictures, so they are described in words first (about 2 cents). They go to Claude to be described, and the words go to OpenAI with this picture.");
  assert.doesNotMatch(R.pathLine({ path: "words", image: "Paint 1", provider: "OpenAI", describerProvider: "Claude" }), /about|\(/);
  // "about" only for a priced amount
  assert.match(R.pathLine({ path: "words", image: "Paint 1", provider: "OpenAI", describerProvider: "Claude", price: "less than 1 cent" }), /first \(less than 1 cent\)\./);
  assert.equal(R.pathLine({ path: "none", image: "Paint 1", provider: "OpenAI" }),
    "Paint 1 can't take pictures, and no model that can see pictures is set up, so they can't be used. Draw goes without them.");
});
test("leftOutLine and withRefsPhrase", () => {
  assert.equal(R.leftOutLine(0), "");
  assert.equal(R.leftOutLine(undefined), "");
  assert.equal(R.leftOutLine("x"), "");
  assert.equal(R.leftOutLine(1), "1 character picture was left out to make room for yours.");
  assert.equal(R.leftOutLine(2), "2 character pictures were left out to make room for yours.");
  assert.equal(R.withRefsPhrase(0), "");
  assert.equal(R.withRefsPhrase(1), "with your reference picture");
  assert.equal(R.withRefsPhrase(2), "with your 2 reference pictures");
});
test("uploadError and earlyProblem name the rule in one sentence", () => {
  assert.equal(R.uploadError({ status: 413, code: "too-large", message: "x" }), "That picture is over 8 MB. Choose a smaller one.");
  assert.equal(R.uploadError({ status: 422, code: "bad-image", message: "Each side must be at least 256 pixels." }), "Each side must be at least 256 pixels.");
  assert.equal(R.uploadError({ status: 422, code: "bad-image", message: "" }), "That file is not a PNG, JPEG or WebP picture.");
  assert.match(R.uploadError({ status: 500, message: "boom" }), /^Couldn't add that picture: boom\.$/);
  assert.equal(R.earlyProblem({ kind: null, size: 10 }), "That file is not a PNG, JPEG or WebP picture.");
  assert.equal(R.earlyProblem({ kind: "png", size: 9 * 1024 * 1024 }), "That picture is over 8 MB. Choose a smaller one.");
  assert.equal(R.earlyProblem({ kind: "png", size: 10 }), "");
  assert.match(R.earlyProblem({ kind: "png", size: 10, width: 100, height: 900 }), /at least 256 pixels/);
  assert.equal(R.sniff(new Uint8Array([0xff, 0xd8, 0xff, 0, 0, 0, 0, 0, 0, 0, 0, 0])), "jpg");   // styleref.js's, shared
});
test("roomFor keeps the row at four", () => {
  assert.equal(R.roomFor([], 6), 4);
  assert.equal(R.roomFor([{}, {}, {}], 2), 1);
  assert.equal(R.roomFor([{}, {}, {}, {}], 1), 0);
  assert.equal(R.roomFor(null, 2), 2);
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
