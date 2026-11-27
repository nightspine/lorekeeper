// A custom style's reference picture: the checks the browser makes before sending one, and the cost line of "Describe it
// for me". No imports, so it can be tested in Node (tests/js/run_styleref.mjs). The server makes the same checks again
// and is the one that counts: these only say early, in the form, what it would refuse.

/** Largest file accepted, in bytes (the server answers 413 `too-large` past it). */
export const REF_MAX_BYTES = 8 * 1024 * 1024;
/** Each side of the picture, in pixels. */
export const REF_MIN_SIDE = 256, REF_MAX_SIDE = 8192;
/** What the file input offers. */
export const REF_ACCEPT = "image/png,image/jpeg,image/webp";

/** The picture's kind from its first bytes (never from the name or the type the browser guessed): "png", "jpg", "webp",
 *  else null. `head` is a Uint8Array of at least the first 12 bytes. */
export function sniff(head) {
  const b = head instanceof Uint8Array ? head : new Uint8Array(head || []);
  const at = (i, ...xs) => xs.every((x, k) => b[i + k] === x);
  if (b.length >= 8 && at(0, 0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)) return "png";
  if (b.length >= 3 && at(0, 0xff, 0xd8, 0xff)) return "jpg";
  if (b.length >= 12 && at(0, 0x52, 0x49, 0x46, 0x46) && at(8, 0x57, 0x45, 0x42, 0x50)) return "webp";
  return null;
}

/** What is wrong with a chosen picture, as one sentence, else "". `kind` is `sniff`'s answer; `width` and `height` are
 *  left out until the picture was decoded (null when it could not be). */
export function refProblem({ size, kind, width, height } = {}) {
  if (!kind) return "Choose a PNG, JPEG or WebP picture.";
  if (Number.isFinite(size) && size > REF_MAX_BYTES) return "This picture is over 8 MB. Choose a smaller one.";
  if (width === null || height === null) return "This file could not be read as a picture.";
  if (width === undefined || height === undefined) return "";
  if (Math.min(width, height) < REF_MIN_SIDE) return `This picture is too small: each side must be at least ${REF_MIN_SIDE} pixels.`;
  if (Math.max(width, height) > REF_MAX_SIDE) return `This picture is too large: each side must be at most ${REF_MAX_SIDE} pixels.`;
  return "";
}

/** A small text-model cost: "0.1 cent", "3 cents", "$1.20", "less than 0.1 cent"; "price unknown" for 0 or nothing (an
 *  unpriced model costs 0, never a made-up number). */
export function smallCost(usd) {
  const v = Number(usd);
  if (usd == null || !Number.isFinite(v) || v <= 0) return "price unknown";
  const c = v * 100;
  if (c < 0.05) return "less than 0.1 cent";
  if (c < 1) return `${c.toFixed(1)} cent`;
  if (c < 99.5) { const r = Math.round(c); return `${r} cent${r === 1 ? "" : "s"}`; }
  return `$${v.toFixed(2)}`;
}

/** "Described by Claude Haiku 5.5, 0.1 cent." from the describe reply and the model's name. */
export function describedLine(modelName, cost) {
  const who = typeof modelName === "string" && modelName.trim() ? modelName.trim() : "the model";
  return `Described by ${who}, ${smallCost(cost)}.`;
}

/** Which of several overlapping checks is the newest: `start()` gives a number, `current(n)` is true only for the
 *  last one started, so a slow check of an earlier choice never overwrites a newer one. */
export function latestOnly() {
  let n = 0;
  return { start: () => ++n, current: (k) => k === n };
}
