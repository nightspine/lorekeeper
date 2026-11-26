// artprompt.js: a picture prompt in its parts, and a style change made in the browser with no new prompt.
//   node tests/js/run_artprompt.mjs
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const { choiceSentence, composePrompt, promptParts, restyle, sentenceFor } = await import(join(here, "../../src/lorekeeper/web/static/js/artprompt.js"));
let failed = 0;
const test = (name, fn) => { try { fn(); console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n${e.message}`); } };

const reply = {
  prompt: "A tall woman in a grey coat by a window. Ink drawing, bold lines. No text anywhere.",
  body: "A tall woman in a grey coat by a window.", style: "Ink drawing, bold lines.", noText: "No text anywhere.",
  styles: { ink: "Ink drawing, bold lines.", watercolour: "Soft watercolour washes." },
};
const parts = promptParts(reply);

test("the parts join as art.py joins them, one space apart", () => {
  assert.equal(composePrompt(parts.body, parts.style, parts.noText), reply.prompt);
});
test("an older server's reply has no parts", () => {
  assert.equal(promptParts({ prompt: "x" }), null);
  assert.equal(promptParts({ prompt: "x", body: "  " }), null);
});
test("a style's sentence: a listed one from the table, a custom one as written, an unlisted one unknown", () => {
  assert.equal(sentenceFor(parts, "watercolour"), "Soft watercolour washes.");
  assert.equal(sentenceFor(parts, "custom", "  linocut,\n two colours "), "linocut, two colours");
  assert.equal(sentenceFor(parts, "custom", " "), "");
  assert.equal(sentenceFor(parts, "oil"), null);
});
test("a saved style is looked up by slug, even one whose slug is custom", () => {
  const saved = (slug) => ({ custom: "A custom-named style, cut paper.", linocut: "Linocut, two colours." })[slug] ?? null;
  // A saved style named "Custom" has the slug "custom": its own words, never the empty loose text.
  assert.equal(choiceSentence(parts, "custom", "", saved), "A custom-named style, cut paper.");
  assert.equal(choiceSentence({ ...parts, styles: { ...parts.styles, custom: "From the table." } }, "custom", "", saved), "From the table.");
  assert.equal(choiceSentence(parts, "linocut", "", saved), "Linocut, two colours.");
  assert.equal(choiceSentence(parts, "watercolour", "", saved), "Soft watercolour washes.");
  assert.equal(choiceSentence(parts, "oil", "", saved), null);
  // Loose words (no slug) are used as written.
  assert.equal(choiceSentence(parts, null, "  soft pastel\n drawing "), "soft pastel drawing");
  assert.equal(choiceSentence(parts, null, " "), "");
});
test("an unedited prompt is built again from its body", () => {
  const r = restyle({ prompt: reply.prompt, written: reply.prompt, parts, old: parts.style, next: "Soft watercolour washes." });
  assert.equal(r.prompt, "A tall woman in a grey coat by a window. Soft watercolour washes. No text anywhere.");
  assert.equal(r.written, r.prompt);
  assert.equal(r.missing, false);
});
test("an edited prompt keeps the edit and has its old style sentence replaced", () => {
  const edited = reply.prompt.replace("tall", "very tall");
  const r = restyle({ prompt: edited, written: reply.prompt, parts, old: parts.style, next: "Soft watercolour washes." });
  assert.equal(r.prompt, "A very tall woman in a grey coat by a window. Soft watercolour washes. No text anywhere.");
  assert.notEqual(r.prompt, r.written);   // still edited: closing the sheet asks first
  assert.equal(r.missing, false);
});
test("an edited prompt without its style sentence is left as written, and says so", () => {
  const edited = "A tall woman by a window, in pencil.";
  const r = restyle({ prompt: edited, written: reply.prompt, parts, old: parts.style, next: "Soft watercolour washes." });
  assert.equal(r.prompt, edited);
  assert.equal(r.missing, true);
  assert.equal(r.sentence, "Soft watercolour washes.");
});
test("an empty custom style and then a typed one", () => {
  const a = restyle({ prompt: reply.prompt, written: reply.prompt, parts, old: parts.style, next: "" });
  assert.equal(a.prompt, "A tall woman in a grey coat by a window. No text anywhere.");
  const edited = a.prompt.replace("tall", "short");
  const b = restyle({ prompt: edited, written: a.written, parts, old: a.sentence, next: "linocut, two colours" });
  assert.equal(b.prompt, "A short woman in a grey coat by a window. linocut, two colours No text anywhere.");
  assert.equal(b.missing, false);
});
test("a sentence with $ patterns is put in literally", () => {
  const edited = reply.prompt + " ";
  const r = restyle({ prompt: edited, written: reply.prompt, parts, old: parts.style, next: "Costs $& and $1." });
  assert.ok(r.prompt.includes("Costs $& and $1."));
});

test("the reviewer's case: an edited prompt, Custom, then typing letter by letter never touches the body", () => {
  const edited = reply.prompt.replace("grey", "dark");
  let st = { prompt: edited, written: reply.prompt, sentence: parts.style };
  for (const typed of ["", "i", "in", "ink"]) {
    st = { ...st, ...restyle({ prompt: st.prompt, written: st.written, parts, old: st.sentence, next: typed }) };
  }
  assert.equal(st.prompt, "A tall woman in a dark coat by a window. ink No text anywhere.");
  assert.equal(st.missing, false);
});
test("a short style that also stands in the body is swapped only in its slot", () => {
  const p2 = { ...parts, body: "A woman with ink-black hair, ink on her fingers." };
  const prompt = composePrompt(p2.body, "ink", p2.noText);
  const edited = prompt.replace("woman", "girl");
  const r = restyle({ prompt: edited, written: prompt, parts: p2, old: "ink", next: "Soft watercolour washes." });
  assert.equal(r.prompt, "A girl with ink-black hair, ink on her fingers. Soft watercolour washes. No text anywhere.");
});
test("a style word in the body while the slot is gone counts as missing, never replaced there", () => {
  const p2 = { ...parts, body: "A woman with ink-black hair, ink on her fingers." };
  const prompt = composePrompt(p2.body, "ink", p2.noText);
  const edited = "A girl with ink-black hair, ink on her fingers, by a fire.";
  const r = restyle({ prompt: edited, written: prompt, parts: p2, old: "ink", next: "Oil paint." });
  assert.equal(r.prompt, edited);
  assert.equal(r.missing, true);
});
test("with the no-text sentence taken out, the style sentence that closes the prompt is swapped", () => {
  const edited = "A very tall woman by a window. Ink drawing, bold lines.";
  const r = restyle({ prompt: edited, written: reply.prompt, parts, old: parts.style, next: "Soft watercolour washes." });
  assert.equal(r.prompt, "A very tall woman by a window. Soft watercolour washes.");
  assert.equal(r.missing, false);
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
