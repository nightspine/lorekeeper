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

/**
 * Swap the style sentence. `prompt` is what is on screen, `written` what the sheet last put there itself (the two differ
 * when the person edited the prompt), `old` the sentence in it now. Returns { prompt, written, sentence, missing }.
 */
export function restyle({ prompt, written, parts, old, next }) {
  const fresh = composePrompt(parts.body, next, parts.noText);
  if (prompt === written) return { prompt: fresh, written: fresh, sentence: next, missing: false };
  const was = squash(old);
  if (!was) {
    // No sentence in it yet (an empty custom style): the new one goes before the no-text sentence when that still stands.
    const nt = squash(parts.noText), k = nt ? prompt.lastIndexOf(nt) : -1;
    if (k < 0 || !squash(next)) return { prompt, written: fresh, sentence: next, missing: squash(next) !== "" };
    return { prompt: `${prompt.slice(0, k)}${squash(next)} ${prompt.slice(k)}`, written: fresh, sentence: next, missing: false };
  }
  const at = prompt.indexOf(was);
  if (at < 0) return { prompt, written: fresh, sentence: next, missing: squash(next) !== "" && !prompt.includes(squash(next)) };
  return { prompt: prompt.slice(0, at) + squash(next) + prompt.slice(at + was.length), written: fresh, sentence: next, missing: false };
}
