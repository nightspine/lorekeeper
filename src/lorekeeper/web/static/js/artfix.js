// The Fix flow's rules, dependency-free so Node can test them (tests/js/run_artfix.mjs): the fault list a check answers,
// the body a Fix sends, and the lines "How this was drawn" shows for a picture's fixes. Faults come from a model and the
// person: plain text only, always shown through h() by the caller.

/** The route's limits (art.MAX_FIX_FAULTS, FAULT_CHARS, WHERE_CHARS, FIX_NOTE_CHARS). */
export const MAX_FAULTS = 8;
export const WHAT_CHARS = 200;
export const WHERE_CHARS = 120;
export const NOTE_CHARS = 300;

/** The cost of the automatic check, said once in Settings, from the 2026-11-27 bench (art.FIX_RATE, CHECK_TOKENS on Haiku). */
export const CHECK_COST = "Checking costs about 5 cents per 100 pictures. In a test about one picture in five had a fault worth fixing, and each fix costs one more picture.";

const text = (v, n) => (typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, n) : "");

/** A port of art._cut_words: whitespace collapsed, cut to at most `limit` characters at a word boundary (back to the last
 *  space when that is past half the limit, else where the limit falls), never keeping a trailing comma, dash or stop. */
export function cutWords(v, limit) {
  if (typeof v !== "string") return "";
  let t = v.split(/\s+/).filter(Boolean).join(" ");
  if (t.length > limit) {
    let head = t.slice(0, limit);
    if (t[limit] !== " ") {
      const space = head.lastIndexOf(" ");
      if (space > Math.floor(limit / 2)) head = head.slice(0, space);
    }
    t = head.replace(/[ ,;:\-\u2013\u2014]+$/, "");
  }
  return t.replace(/[ .]+$/, "");
}

/** A fault's grade: "minor" only when it says so; anything else (an older answer, a line the person typed) is "major",
 *  so it is fixed in full. */
export const severityOf = (f) => (f && f.severity === "minor" ? "minor" : "major");

/** A fault's kind: "drawing", "text" (letters, labels or writing drawn in the picture) or "style" (not in the chosen
 *  style); undefined for anything else, so the key is left out and the server defaults it to "drawing". */
export const kindOf = (f) => (f && ["drawing", "text", "style"].includes(f.kind) ? f.kind : undefined);

/** The faults of a check answer (or a record's `check`/`fixes` entry), cleaned: [{ what, where, severity, kind? }], an empty
 *  `what` dropped, at most MAX_FAULTS. Anything that is not a list is no faults. */
export function cleanFaults(list) {
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const f of list) {
    const what = cutWords(f?.what, WHAT_CHARS);
    if (!what) continue;
    const kind = kindOf(f);
    out.push({ what, where: cutWords(f?.where, WHERE_CHARS), severity: severityOf(f), ...(kind ? { kind } : {}) });
    if (out.length >= MAX_FAULTS) break;
  }
  return out;
}

/** The body of POST /art/{id}/fix from the rows as typed and the note: { faults, note }, or null when there is nothing
 *  to fix (no fault with words and no note). `p` and `finished` are added by the caller. */
export function fixBody(rows, note = "") {
  const faults = cleanFaults(rows);
  const n = text(note, NOTE_CHARS);
  if (!faults.length && !n) return null;
  return n ? { faults, note: n } : { faults };
}

const bare = (s) => s.replace(/[.;\s]+$/, "");
const count = (v) => (Number.isInteger(v) && v >= 0 ? v : null);

/** A fix's `verified` object, checked: { fixed, partly, of, results: [{ n, fixed, note? }], new: [fault], doubtful? }, or
 *  null when the fix was not checked (null, missing or malformed). `n` counts the items judged from 1: the faults sent, then
 *  the person's note as the last item, marked `note: true` (the counts include it). `doubtful: true` marks a hand fix the
 *  checker thinks put nothing right, kept because the picture did change. */
export function cleanVerified(v) {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const fixed = count(v.fixed), partly = count(v.partly), of = count(v.of);
  if (fixed == null || partly == null || of == null || fixed + partly > of) return null;
  const results = (Array.isArray(v.results) ? v.results : [])
    .filter((r) => r && Number.isInteger(r.n) && r.n >= 1 && r.n <= of && ["yes", "partly", "no"].includes(r.fixed))
    .map((r) => ({ n: r.n, fixed: r.fixed, ...(r.note === true ? { note: true } : {}) }));
  // one note item at most, the last one judged
  const notes = results.filter((r) => r.note);
  if (notes.length > 1 || (notes.length && notes[0].n !== of)) for (const r of notes) delete r.note;
  return { fixed, partly, of, results, new: cleanFaults(v.new).slice(0, 3), ...(v.doubtful === true ? { doubtful: true } : {}) };
}

/** The faults' share of a checked verdict, the note item taken out: { fixed, partly, of, results, note } where `note` is
 *  the note item's "yes" / "partly" / "no", or null when the note was not judged as an item. */
function split(v) {
  const n = v.results.find((r) => r.note);
  if (!n) return { fixed: v.fixed, partly: v.partly, of: v.of, results: v.results, note: null };
  return { fixed: v.fixed - (n.fixed === "yes" ? 1 : 0), partly: v.partly - (n.fixed === "partly" ? 1 : 0), of: v.of - 1,
    results: v.results.filter((r) => !r.note), note: n.fixed };
}
const DONE = { yes: "done", partly: "partly done", no: "not done" };

/** The sentence under the picture after a fix, from the reply's `verified` and the faults that were sent:
 *  "Fixed all 3." / "Fixed 2 of 3. Still wrong: the left hand has six fingers." / "Partly fixed: ..." / "Fixed. The
 *  result couldn't be checked.", then "Your change: done." for the note item and "The fix added: ..." when the check
 *  found something new. A doubtful fix says "The checker thinks nothing was fixed: <the faults>." */
export function verdictLine(verified, sent = []) {
  const v = cleanVerified(verified);
  if (!v) return "Fixed. The result couldn't be checked.";
  const faults = cleanFaults(sent);
  const f = split(v);
  const named = (kind) => f.results.filter((r) => r.fixed === kind).map((r) => faults[r.n - 1]).filter(Boolean).map((x) => bare(x.what));
  const out = [];
  if (v.doubtful) {
    // Doubtful: either nothing was judged put right, or something was but the checker says the picture did not visibly
    // change. Read the items: name as not fixed only those judged "no".
    const done = f.fixed + f.partly, noteDone = f.note === "yes" || f.note === "partly";
    const still = named("no");
    if (!done && !noteDone) {
      if (still.length) out.push(`The checker thinks nothing was fixed: ${still.join("; ")}.`);
      else if (!f.results.length && faults.length && f.of) out.push(`The checker thinks nothing was fixed: ${faults.slice(0, f.of).map((x) => bare(x.what)).join("; ")}.`);
      else if (f.of || !f.note) out.push("The checker thinks nothing was fixed.");
      else out.push(`The checker thinks your change was ${DONE[f.note]}.`);
    } else {
      const judged = done ? `${f.fixed ? `${f.fixed} of ${f.of} put right` : ""}${f.fixed && f.partly ? " and " : ""}${f.partly ? `${f.partly} partly` : ""}`
        : `your change ${DONE[f.note]}`;
      out.push(`The checker found little visible change, though it judged ${judged}.`);
      if (still.length) out.push(`Still wrong: ${still.join("; ")}.`);
    }
    // the note's result, unless a sentence above already said it ("your change was not done", "judged your change done")
    const said = !done && (noteDone || !f.of);
    if (f.note && !said) out.push(`Your change: ${DONE[f.note]}.`);
  } else {
    if (!f.of) { if (!f.note) out.push("Fixed."); }
    else if (f.fixed === f.of) out.push(f.of === 1 ? "Fixed it." : f.of === 2 ? "Fixed both." : `Fixed all ${f.of}.`);
    else {
      if (f.fixed) out.push(`Fixed ${f.fixed} of ${f.of}.`);
      const part = named("partly");
      if (part.length) out.push(`Partly fixed: ${part.join("; ")}.`);
      else if (f.partly) out.push(f.partly === 1 ? "One partly fixed." : `${f.partly} partly fixed.`);
      const still = named("no");
      const left = f.of - f.fixed - f.partly;
      if (still.length) out.push(`Still wrong: ${still.join("; ")}.`);
      else if (left > 0) out.push(left === 1 ? "One is still wrong." : `${left} are still wrong.`);
    }
    if (f.note) out.push(`Your change: ${DONE[f.note]}.`);
  }
  if (v.new.length) out.push(`The fix added: ${v.new.map((x) => bare(x.what)).join("; ")}.`);
  return out.join(" ");
}

/** Whether a fix reply's verdict is doubtful: the picture changed but the checker thinks nothing was put right. */
export const doubtful = (verified) => Boolean(cleanVerified(verified)?.doubtful);

/** " (2 of 3 put right, 1 partly)" over the faults only, or "" when unchecked or no fault was judged; and the note's
 *  " (done)" / " (partly done)" / " (not done)" when it was judged as an item. */
function tallies(verified) {
  const v = cleanVerified(verified);
  if (!v) return { faults: "", note: "", doubtful: false };
  const f = split(v);
  return { faults: f.of ? ` (${f.fixed} of ${f.of} put right${f.partly ? `, ${f.partly} partly` : ""})` : "",
    note: f.note ? ` (${DONE[f.note]})` : "", doubtful: Boolean(v.doubtful) };
}

/** "Fixed: the head is far too small for the body; the left hand has six fingers (1 of 2 put right). Also: brighter (done)."
 *  for one `fixes` entry, or null when it says nothing. The counts show only for a checked fix; a doubtful one reads
 *  "Tried to fix: ...". */
export function fixLine(fix) {
  if (Number.isInteger(fix?.panel) && fix.panel >= 1) return panelFixLine(fix);
  const whats = cleanFaults(fix?.faults).map((f) => bare(f.what));
  const note = text(fix?.note, NOTE_CHARS).replace(/[.\s]+$/, "");
  if (!whats.length && !note) return null;
  const t = tallies(fix?.verified);
  const verb = t.doubtful ? "Tried to fix" : "Fixed";
  const head = whats.length ? `${verb}: ${whats.join("; ")}${t.faults}.` : "";
  return [head, note ? `${whats.length ? "Also" : verb}: ${note}${t.note}.` : ""].filter(Boolean).join(" ");
}

/** A record's fixes, oldest first: [{ line, created }] (entries that say nothing are left out). */
export function fixLines(rec) {
  const list = Array.isArray(rec?.fixes) ? rec.fixes : [];
  return list.map((f) => ({ line: fixLine(f), created: typeof f?.created === "string" ? f.created : null })).filter((x) => x.line);
}

/** Whether the automatic check found a fault and the picture was fixed (the sheet's quiet line). */
export const autoFixed = (rec) => Boolean(rec && rec.check && typeof rec.check === "object" && rec.check.fixed === true);

const BEFORE_RE = /^\/api\/library\/[^/?#]+\/art\/[A-Za-z0-9_-]{1,64}\/before(\?[^#]*)?$/;
/** The "before" picture's address from a fix reply's `beforeUrl`, accepted only as the API's own route, with the reader's
 *  position added when the server left it out (`at` = { p, finished }, finished sent as "key,key"); null otherwise. */
export function beforeSrc(url, at = {}) {
  if (typeof url !== "string" || !BEFORE_RE.test(url)) return null;
  const [path, query = ""] = url.split("?");
  const q = new URLSearchParams(query);
  if (!q.has("p") && typeof at?.p === "string" && at.p) q.set("p", at.p);
  if (!q.has("finished") && Array.isArray(at?.finished)) q.set("finished", at.finished.join(","));
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

// ---- fixing one panel of a comic page ----------------------------------------------------------------

/** art.PANEL_TEXT_CHARS: the longest panel text the route takes. */
export const PANEL_TEXT_CHARS = 600;

/** The panel texts a record carries (`panelTexts`, served by the art route), or null unless there is exactly one string
 *  per panel: the box then starts empty. Never read out of the prompt. */
export function panelTextsOf(rec) {
  const t = rec?.panelTexts, n = Array.isArray(rec?.panels) ? rec.panels.length : 0;
  return Array.isArray(t) && n && t.length === n && t.every((x) => typeof x === "string") ? t : null;
}

/** The body of POST /art/{id}/panel: { k, text?, note? } or null when nothing changed (the text equals the one drawn, or
 *  none is given, and there is no note). `was` is the panel's text now ("" when not shown). */
export function panelBody(k, value, was = "", note = "") {
  const t = text(value, PANEL_TEXT_CHARS), w = text(was, 1e6), n = text(note, NOTE_CHARS);
  const changed = Boolean(t) && t !== w;
  if (!changed && !n) return null;
  return { k, ...(changed ? { text: t } : {}), ...(n ? { note: n } : {}) };
}

/** What the verdict names for a panel fix: the wish as one fault line. */
export const panelWish = (k, body) => ({ what: cutWords(body?.text ? `panel ${k} should show: ${body.text}` : (body?.note || `panel ${k}`), WHAT_CHARS) });

/** "Fixed panel 3: <new text, cut to 120 characters, or the note>." with the tally over the panel's own items when checked
 *  ("Tried to fix panel 3" when doubtful), then "Also: <note> (done)." for a note sent beside a new text, which is judged
 *  as its own last item. */
export function panelFixLine(fix) {
  const k = fix?.panel;
  const hasText = typeof fix?.text === "string" && fix.text.trim();
  const note = text(fix?.note, NOTE_CHARS).replace(/[.\s]+$/, "");
  const said = cutWords(hasText ? fix.text : note, 120);
  const t = tallies(fix?.verified);
  // the note is its own line only beside a new text (then it was judged as the last item); alone it is the panel's wish
  const also = hasText && note ? ` Also: ${note}${t.note}.` : "";
  return `${t.doubtful ? "Tried to fix" : "Fixed"} panel ${k}${said ? `: ${said}` : ""}${t.faults}.${also}`;
}
