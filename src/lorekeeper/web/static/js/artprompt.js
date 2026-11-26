// A picture prompt in its parts, and a style change made on the text without asking the server again.
// Dependency-free so Node can test it (tests/js/run_artprompt.mjs).
//
// The prompt route answers `body` (the writer's text, which carries no style words), `style` (the style sentence it was
// given) and `noText`; the prompt is the three joined by one space, the way art.py joins them. A style change then needs
// no call: an unedited prompt is rebuilt from `body`; an edited one has its old style sentence replaced where it still
// stands word for word, else it is left as the person wrote it and `missing` says the chosen style is not in the text.

/** Whitespace collapsed, as art.style_sentence does to a custom style. */
export const squash = (text) => String(text ?? "").split(/\s+/).filter(Boolean).join(" ");

/** The prompt from its parts. */
export function composePrompt(body, sentence, noText) {
  return [body, sentence, noText].map(squash).filter(Boolean).join(" ");
}

/** The prompt parts of a prompt-route reply, or null for an older server that sends only `prompt`. */
export function promptParts(reply) {
  if (!reply || typeof reply.body !== "string" || !reply.body.trim()) return null;
  return {
    body: reply.body,
    style: typeof reply.style === "string" ? reply.style : "",
    noText: typeof reply.noText === "string" ? reply.noText : "",
    styles: reply.styles && typeof reply.styles === "object" && !Array.isArray(reply.styles) ? reply.styles : {},
  };
}

/** The style sentence for a choice: a known style's from the reply's table, a custom one as the person wrote it. Null
 *  when it is not known (a known style the server did not list), "" for an empty custom style. */
export function sentenceFor(parts, style, custom = "") {
  if (style === "custom") return squash(custom);
  const s = parts?.styles?.[style];
  return typeof s === "string" && s.trim() ? squash(s) : null;
}

/** The style sentence of a choice made by slug or by words. `slug` null means the style is the loose `words` (squashed;
 *  "" when empty). A slug is looked up as a slug, never as the word "custom": the reply's table first, then
 *  `saved(slug)` (a saved style's own words), else null. */
export function choiceSentence(parts, slug, words = "", saved = () => null) {
  if (slug == null) return squash(words);
  const s = parts?.styles?.[slug];
  if (typeof s === "string" && s.trim()) return squash(s);
  const w = saved(slug);
  return typeof w === "string" && w.trim() ? squash(w) : null;
}

/**
 * Swap the style sentence. `prompt` is what is on screen, `written` what the sheet last put there itself (the two differ
 * when the person edited the prompt), `old` the sentence in it now. Returns { prompt, written, sentence, missing }.
 *
 * In an edited prompt the sentence is replaced only in its slot: right before the no-text sentence (the last one there),
 * or at the very end when the no-text sentence was taken out. Never anywhere in the body, where a short style ("ink")
 * may also stand as an ordinary word; anything else leaves the text as written and `missing` says so.
 */
export function restyle({ prompt, written, parts, old, next }) {
  const fresh = composePrompt(parts.body, next, parts.noText);
  const nx = squash(next);
  if (prompt === written) return { prompt: fresh, written: fresh, sentence: next, missing: false };
  const keep = (missing) => ({ prompt, written: fresh, sentence: next, missing });
  const done = (text) => ({ prompt: text, written: fresh, sentence: next, missing: false });
  const was = squash(old), nt = squash(parts.noText);
  if (nt) {
    // The slot before the no-text sentence: "<old> <noText>", or just "<noText>" when there was no sentence yet.
    const slot = was ? `${was} ${nt}` : nt;
    const k = prompt.lastIndexOf(slot);
    if (k >= 0 && (k === 0 || /\s$/.test(prompt.slice(0, k)) || !was)) {
      const before = prompt.slice(0, k), after = prompt.slice(k + slot.length);
      const head = !was && before && !/\s$/.test(before) ? `${before} ` : before;
      return done(`${head}${nx ? `${nx} ` : ""}${nt}${after}`);
    }
  }
  if (was) {
    // No no-text sentence any more: the style sentence still closes the prompt.
    const t = prompt.trimEnd();
    if (t.endsWith(was) && (t.length === was.length || /\s/.test(t[t.length - was.length - 1]))) {
      const base = t.slice(0, t.length - was.length);
      return done(nx ? `${base}${nx}` : base.trimEnd());
    }
  }
  return keep(nx !== "");
}
