// The person's own reference pictures for a drawing they start: the rules the picture sheet and the lightbox share. Its
// one import is styleref.js (the style picture's checks, shared so both say the same), which has none, so Node can test
// this (tests/js/run_artrefs.mjs). The pictures are the person's own files, kept beside the picture they drew (`userRefs`
// on its record), never in an index. Notes and descriptions are plain text: the caller only ever shows them through h().
// Picture files are fetched only from /api/art/<textHash>/refs/<sha> addresses checked here.
import { refProblem, REF_ACCEPT, REF_MAX_BYTES, sniff } from "./styleref.js";

export { REF_ACCEPT, sniff };

/** At most this many pictures per drawing (the server answers 422 `bad-refs` past it). */
export const MAX_REFS = 4;
/** A note's length after collapsing whitespace (the server's limit). */
export const REF_NOTE_CHARS = 120;
/** What describing one picture in words costs, roughly: tokens in (the picture and the instructions) and out (one or two
 *  sentences). Set from the bench: $0.0045 for one picture on Claude Sonnet ($2 in, $10 out per million), one call. Only
 *  for the price in the path line; the server bills what it spends. */
export const DESCRIBE_TOKENS = [1750, 100];

const SHA_RE = /^[0-9a-f]{64}$/;
const EXTS = new Set(["png", "jpg", "webp"]);
const URL_RE = /^\/api\/art\/[A-Za-z0-9_-]{1,128}\/refs\/[0-9a-f]{64}$/;

/** A note as the server keeps it: whitespace collapsed, at most REF_NOTE_CHARS characters. */
export function cleanNote(v) {
  return typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, REF_NOTE_CHARS) : "";
}
/** A picture's address, only when it is one of the refs route's own; else null. */
export const refUrl = (v) => (typeof v === "string" && URL_RE.test(v) ? v : null);

/** A record's `userRefs` (or an upload reply) as the sheet and the lightbox use them: [{ sha, ext, note, description, url }],
 *  only well-formed items, distinct shas, at most MAX_REFS. Anything that is not a list is none. */
export function cleanUserRefs(list) {
  if (!Array.isArray(list)) return [];
  const out = [], seen = new Set();
  for (const r of list) {
    if (!r || typeof r !== "object" || !SHA_RE.test(r.sha || "") || seen.has(r.sha)) continue;
    seen.add(r.sha);
    const description = typeof r.description === "string" ? r.description.replace(/\s+/g, " ").trim().slice(0, 300) : "";
    out.push({ sha: r.sha, ext: EXTS.has(r.ext) ? r.ext : null, note: cleanNote(r.note), description: description || null, url: refUrl(r.url),
      appliesTo: cleanApplies(r.appliesTo) });
    if (out.length >= MAX_REFS) break;
  }
  return out;
}

/** The `refs` of a draw, redraw or `PUT .../refs` body from the row's items: [{ sha, note?, appliesTo? }], uploaded ones
 *  only. `appliesTo` goes only when it is not "scene" (missing means the whole scene, as an older server reads it too). */
export function refsBody(items) {
  return cleanUserRefs((items || []).filter((x) => x && !x.uploading && !x.error)).map((r) => ({
    sha: r.sha, ...(r.note ? { note: r.note } : {}), ...(r.appliesTo !== "scene" ? { appliesTo: r.appliesTo } : {}) }));
}

// ---- what each picture applies to ---------------------------------------------------------------

/** Picture kinds drawn of one subject (a portrait, a place, a thing): a reference may be for that subject. */
const SUBJECT_KINDS = new Set(["portrait", "place", "thing"]);
const LABEL_RE = /^[A-F]$/;
/** "scene", "subject" or a figure label "A".."F"; anything else (missing, null, a name, an id) is "scene". */
export function cleanApplies(v) {
  return v === "subject" || (typeof v === "string" && LABEL_RE.test(v)) ? v : "scene";
}
/** The subject in words when the view gives no name: "This place", "This thing", "The person drawn". */
const subjectWord = (kind) => (kind === "place" ? "This place" : kind === "thing" ? "This thing" : "The person drawn");

/**
 * The "Applies to" choices for one reference picture: [{ value, label }], the whole picture first.
 *   kind     the picture's kind (plate | event | comic | portrait | place | thing)
 *   figures  for a scene or comic: the labelled figures the prompt text uses now, [{ label, name }] where `name` comes from
 *            the reader's view only (null: not visible there, so just "Figure A"); the caller filters by the text
 *   subject  for a portrait, place or thing: the subject's name from the reader's view (null: "This place" and so on)
 */
export function appliesOptions({ kind, figures = [], subject = null } = {}) {
  if (SUBJECT_KINDS.has(kind)) {
    return [{ value: "scene", label: "The whole picture" }, { value: "subject", label: typeof subject === "string" && subject ? subject : subjectWord(kind) }];
  }
  const out = [{ value: "scene", label: "The whole scene" }], seen = new Set();
  for (const f of figures || []) {
    if (!f || !LABEL_RE.test(f.label || "") || seen.has(f.label)) continue;
    seen.add(f.label);
    out.push({ value: f.label, label: f.name ? `Figure ${f.label}, ${f.name}` : `Figure ${f.label}` });
  }
  return out.sort((a, b) => (a.value === "scene" ? -1 : b.value === "scene" ? 1 : a.value < b.value ? -1 : 1));
}

/** The sentence under a reference (and in the done view) when its figure is not in the prompt: "" for anything else. */
export function fallbackLine(wanted, kind = null) {
  const v = cleanApplies(wanted);
  if (LABEL_RE.test(v)) return `Figure ${v} is no longer in the prompt, so this picture now applies to the whole scene.`;
  if (v === "subject" && SUBJECT_KINDS.has(kind)) return "This picture now applies to the whole picture.";
  return "";
}

/**
 * What a reference applies to, given what the person chose (`wanted`) and the choices now (`options`, from appliesOptions;
 * null while no prompt is written yet). Returns { value, options, fallback }: `value` the one sent, `options` the select's
 * (while there is no prompt yet a chosen figure is kept as an option, so a redraw's choice waits for the new text),
 * `fallback` the quiet line when a chosen figure left the text ("" otherwise). "subject" on a scene is the whole scene,
 * silently (it only comes from an odd record).
 */
export function resolveApplies(wanted, options) {
  const v = cleanApplies(wanted);
  if (!options) {
    const opts = [{ value: "scene", label: "The whole scene" }];
    if (LABEL_RE.test(v)) opts.push({ value: v, label: `Figure ${v}` });
    return { value: LABEL_RE.test(v) ? v : "scene", options: opts, fallback: "" };
  }
  if (options.some((o) => o.value === v)) return { value: v, options, fallback: "" };
  return { value: "scene", options, fallback: LABEL_RE.test(v) ? fallbackLine(v) : "" };
}

/** The done view's sentence when the server used the whole scene for a picture tied to a figure (`appliesFallback`, the
 *  shas, on the reply's record): said once, naming the figures as they were sent. `sent` is the body's `refs`. "" for none. */
export function fallbackDoneLine(sent, shas, kind = null) {
  if (!Array.isArray(shas) || !shas.length) return "";
  const back = new Set(shas.filter((x) => typeof x === "string"));
  const labels = [...new Set((sent || []).filter((r) => r && back.has(r.sha)).map((r) => cleanApplies(r.appliesTo)).filter((v) => LABEL_RE.test(v)))].sort();
  if (!labels.length) return back.size ? (SUBJECT_KINDS.has(kind) ? "One of your pictures was used for the whole picture." : "One of your pictures was used for the whole scene.") : "";
  if (labels.length === 1) return fallbackLine(labels[0]);
  const names = `${labels.slice(0, -1).map((l) => `Figure ${l}`).join(", ")} and Figure ${labels[labels.length - 1]}`;
  return `${names} are no longer in the prompt, so those pictures now apply to the whole scene.`;
}

/**
 * The lightbox's line under a reference's note: "For figure A, Mina", "For figure A", "For Mina", "For this place"; "" for
 * the whole scene. The label maps to an entity only through the record's own `figures`; `name(entity)` is the reader's
 * view (null: not visible), so a figure not visible is only "For figure A". `rec` is the picture's record.
 */
export function appliesLine(appliesTo, rec, name = () => null) {
  const v = cleanApplies(appliesTo);
  if (v === "scene") return "";
  const nm = (id) => { try { const n = typeof id === "string" && id ? name(id) : null; return typeof n === "string" && n ? n : null; } catch { return null; } };
  if (v === "subject") {
    if (!SUBJECT_KINDS.has(rec?.kind)) return "";
    const n = nm(rec.target);
    return n ? `For ${n}` : `For ${subjectWord(rec.kind).replace(/^This/, "this").replace(/^The/, "the")}`;
  }
  const f = (Array.isArray(rec?.figures) ? rec.figures : []).find((x) => x && x.label === v);
  const n = f ? nm(f.entity) : null;
  return n ? `For figure ${v}, ${n}` : `For figure ${v}`;
}
/** Do two lists send the same pictures with the same notes, in the same order? */
export function sameRefs(a, b) {
  return JSON.stringify(refsBody(a)) === JSON.stringify(refsBody(b));
}

/** Does this image model take reference pictures? An image model row from /api/providers marked `references: false`
 *  does not; any other (an older server says nothing) does. */
export function takesReferences(provider, modelId) {
  const ms = provider?.images && Array.isArray(provider.images.models) ? provider.images.models : [];
  const row = ms.find((m) => m && m.id === modelId) || ms.find((m) => m && m.id === provider?.images?.default) || null;
  return !(row && row.references === false);
}

/** The model that would describe the pictures in words: { provider, model } (the rows, for names and prices) or null.
 *  `rows` are every provider row of /api/providers; `top` is the answer's own `describe` (undefined when it has none).
 *  The server says which, in that order:
 *   - a top-level `describe`: `{ provider, model }`, or null/false for none;
 *   - else a row's `describe`: true (its strong model, else its cheap one), a model id, or `{ model }`; false on every
 *     row that has the flag means none;
 *   - else (an older server) a guess as `JobManager.describe_model` picks: the strong model of the first configured cloud
 *     provider that can see pictures, else a cheap one; a preset marked `vision: false` cannot see. */
export function describeModel(rows, top = undefined) {
  const all = (rows || []).filter((p) => p && typeof p.id === "string" && p.id !== "ollama" && p.kind !== "ollama");
  const modelOf = (p, id) => {
    if (typeof id !== "string" || !id) return null;
    return [...(Array.isArray(p.models) ? p.models : []), ...(Array.isArray(p.liveModels) ? p.liveModels : [])].find((x) => x && x.id === id)
      || { id, label: id, price: null };
  };
  const sees = (p, id) => { const m = modelOf(p, id); return m && m.vision === false ? null : m; };
  const named = (pid, mid) => {
    const p = all.find((x) => x.id === pid) || (typeof pid === "string" && pid ? { id: pid, label: pid } : null);
    const m = p ? modelOf(p, mid) : null;
    return p && m ? { provider: p, model: m } : null;
  };
  if (top !== undefined) return top && typeof top === "object" ? named(top.provider, top.model) : null;
  const flagged = all.filter((p) => "describe" in p);
  if (flagged.length) {
    for (const p of flagged) {
      const d = p.describe;
      if (!d) continue;
      if (typeof d === "string") return named(p.id, d);
      if (typeof d === "object") return named(p.id, d.model);
      const m = sees(p, p.strong) || sees(p, p.cheap);
      if (m) return { provider: p, model: m };
    }
    return null;
  }
  const list = all.filter((p) => p.configured || p.ok);
  for (const tier of ["strong", "cheap"]) {
    for (const p of list) {
      const m = sees(p, p[tier]);
      if (m) return { provider: p, model: m };
    }
  }
  return null;
}
/** USD to describe `n` pictures on a model row ({ price: [in, out] per million tokens }), null when the price is unknown. */
export function describePrice(model, n = 1) {
  const pr = Array.isArray(model?.price) && model.price.length === 2 && model.price.every((x) => Number.isFinite(+x)) ? model.price : null;
  if (!pr || !(n > 0)) return null;
  return n * (DESCRIBE_TOKENS[0] * +pr[0] + DESCRIBE_TOKENS[1] * +pr[1]) / 1e6;
}

/** The description's cost as a phrase for the path line: "less than 1 cent", "2 cents", "$1.20"; null when unknown. */
export function describeCost(model, n = 1) {
  const usd = describePrice(model, n);
  if (usd == null || !(usd > 0)) return null;
  const c = Math.round(usd * 100);
  if (c < 1) return "less than 1 cent";
  return c < 100 ? `${c} cent${c === 1 ? "" : "s"}` : `$${usd.toFixed(2)}`;
}

/** Which way the pictures go, from the chosen image model: "pictures" (sent as image input), "words" (described by a
 *  model that can see pictures first) or "none" (neither: Draw goes without them). */
export function refPath({ takes, describer }) {
  return takes ? "pictures" : describer ? "words" : "none";
}
/** The one quiet line under the row. `image` is the image model's name and `provider` its provider's; `describer` the
 *  describing model's and provider's names; `price` the description's cost as a phrase ("less than 1 cent"). */
export function pathLine({ path, image, provider, describer = null, describerProvider = null, price = null }) {
  const who = image || "This model";
  if (path === "pictures") return `Sent to ${who} as pictures. They go to ${provider || "the image provider"} with this picture.`;
  if (path === "words") {
    // "about" only for a priced amount: "(about 3 cents)", but "(less than 1 cent)"; nothing when the price is unknown
    const bare = price ? price.replace(/^about /, "") : "";
    const cost = bare ? ` (${/^less than /.test(bare) ? bare : `about ${bare}`})` : "";
    return `${who} can't take pictures, so they are described in words first${cost}. They go to ${describerProvider || describer || "the describing model's provider"} to be described, and the words go to ${provider || "the image provider"} with this picture.`;
  }
  return `${who} can't take pictures, and no model that can see pictures is set up, so they can't be used. Draw goes without them.`;
}

/** The done view's line when figure pictures were left out to make room for the person's: "" for none. */
export function leftOutLine(n) {
  const k = Number.isInteger(n) ? n : Number.isFinite(+n) ? Math.floor(+n) : 0;
  if (k <= 0) return "";
  return k === 1 ? "1 character picture was left out to make room for yours." : `${k} character pictures were left out to make room for yours.`;
}
/** "with your 2 reference pictures" for the same-prompt redraw's line; "" for none. */
export function withRefsPhrase(n) {
  return n > 0 ? `with your ${n === 1 ? "reference picture" : `${n} reference pictures`}` : "";
}
/** An upload's error as one sentence for under the row. */
export function uploadError(e) {
  if (e?.status === 413 || e?.code === "too-large") return "That picture is over 8 MB. Choose a smaller one.";
  if (e?.code === "bad-image") return typeof e.message === "string" && e.message ? e.message : "That file is not a PNG, JPEG or WebP picture.";
  return `Couldn't add that picture: ${e?.message || "the upload failed"}.`;
}
/** What the early check says about a file before it is sent, as one sentence, else "" (the server checks again and is the
 *  one that counts). `kind` is `sniff`'s answer; `width` and `height` as `refProblem` takes them (left out until decoded,
 *  null when the file could not be decoded). */
export function earlyProblem({ kind, size, width, height } = {}) {
  if (!kind) return "That file is not a PNG, JPEG or WebP picture.";
  if (Number.isFinite(size) && size > REF_MAX_BYTES) return "That picture is over 8 MB. Choose a smaller one.";
  return refProblem({ kind, size, width, height });
}
/** How many more pictures may be added to a row holding `items`, at most `wanted`. */
export const roomFor = (items, wanted) => Math.max(0, Math.min(wanted, MAX_REFS - (items || []).length));
