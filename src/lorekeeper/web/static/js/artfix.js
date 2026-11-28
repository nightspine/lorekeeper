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

/** The faults of a check answer (or a record's `check`/`fixes` entry), cleaned: [{ what, where, severity }], an empty
 *  `what` dropped, at most MAX_FAULTS. Anything that is not a list is no faults. */
export function cleanFaults(list) {
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const f of list) {
    const what = cutWords(f?.what, WHAT_CHARS);
    if (!what) continue;
    out.push({ what, where: cutWords(f?.where, WHERE_CHARS), severity: severityOf(f) });
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

/** A fix's `verified` object, checked: { fixed, partly, of, results: [{ n, fixed }], new: [fault] }, or null when the
 *  fix was not checked (null, missing or malformed). `n` counts the faults sent from 1. */
export function cleanVerified(v) {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const fixed = count(v.fixed), partly = count(v.partly), of = count(v.of);
  if (fixed == null || partly == null || of == null || fixed + partly > of) return null;
  const results = (Array.isArray(v.results) ? v.results : [])
    .filter((r) => r && Number.isInteger(r.n) && r.n >= 1 && r.n <= of && ["yes", "partly", "no"].includes(r.fixed))
    .map((r) => ({ n: r.n, fixed: r.fixed }));
  return { fixed, partly, of, results, new: cleanFaults(v.new).slice(0, 3) };
}

/** The sentence under the picture after a fix, from the reply's `verified` and the faults that were sent:
 *  "Fixed all 3." / "Fixed 2 of 3. Still wrong: the left hand has six fingers." / "Partly fixed: ..." / "Fixed. The
 *  result couldn't be checked.", then "The fix added: ..." when the check found something new. */
export function verdictLine(verified, sent = []) {
  const v = cleanVerified(verified);
  if (!v) return "Fixed. The result couldn't be checked.";
  const faults = cleanFaults(sent);
  const named = (kind) => v.results.filter((r) => r.fixed === kind).map((r) => faults[r.n - 1]).filter(Boolean).map((f) => bare(f.what));
  const out = [];
  if (!v.of) out.push("Fixed.");
  else if (v.fixed === v.of) out.push(v.of === 1 ? "Fixed it." : v.of === 2 ? "Fixed both." : `Fixed all ${v.of}.`);
  else {
    if (v.fixed) out.push(`Fixed ${v.fixed} of ${v.of}.`);
    const part = named("partly");
    if (part.length) out.push(`Partly fixed: ${part.join("; ")}.`);
    else if (v.partly) out.push(v.partly === 1 ? "One partly fixed." : `${v.partly} partly fixed.`);
    const still = named("no");
    const left = v.of - v.fixed - v.partly;
    if (still.length) out.push(`Still wrong: ${still.join("; ")}.`);
    else if (left > 0) out.push(left === 1 ? "One is still wrong." : `${left} are still wrong.`);
  }
  if (v.new.length) out.push(`The fix added: ${v.new.map((f) => bare(f.what)).join("; ")}.`);
  return out.join(" ");
}

/** "Fixed: the head is far too small for the body; the left hand has six fingers (1 of 2 put right). Also: brighter."
 *  for one `fixes` entry, or null when it says nothing. The count shows only for a checked fix. */
export function fixLine(fix) {
  const whats = cleanFaults(fix?.faults).map((f) => bare(f.what));
  const note = text(fix?.note, NOTE_CHARS).replace(/[.\s]+$/, "");
  if (!whats.length && !note) return null;
  const v = cleanVerified(fix?.verified);
  const tally = v && v.of ? ` (${v.fixed} of ${v.of} put right${v.partly ? `, ${v.partly} partly` : ""})` : "";
  const head = whats.length ? `Fixed: ${whats.join("; ")}${tally}.` : "";
  return [head, note ? `${whats.length ? "Also" : "Fixed"}: ${note}.` : ""].filter(Boolean).join(" ");
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
