// The Fix flow's rules, dependency-free so Node can test them (tests/js/run_artfix.mjs): the fault list a check answers,
// the body a Fix sends, and the lines "How this was drawn" shows for a picture's fixes. Faults come from a model and the
// person: plain text only, always shown through h() by the caller.

/** The route's limits (art.MAX_FIX_FAULTS, FAULT_CHARS, WHERE_CHARS, FIX_NOTE_CHARS). */
export const MAX_FAULTS = 8;
export const WHAT_CHARS = 200;
export const WHERE_CHARS = 120;
export const NOTE_CHARS = 300;

/** The cost of the automatic check, said once in Settings. A placeholder the lead sets from the bench. */
export const CHECK_COST = "Checking costs about 3 cents per 100 pictures, plus one fix for each picture with a fault, at the price of a picture.";

const text = (v, n) => (typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, n) : "");

/** The faults of a check answer (or a record's `check`/`fixes` entry), cleaned: [{ what, where }], an empty `what`
 *  dropped, at most MAX_FAULTS. Anything that is not a list is no faults. */
export function cleanFaults(list) {
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const f of list) {
    const what = text(f?.what, WHAT_CHARS);
    if (!what) continue;
    out.push({ what, where: text(f?.where, WHERE_CHARS) });
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

/** "Fixed: the head is far too small for the body; the left hand has six fingers. Also: brighter." for one `fixes`
 *  entry, or null when it says nothing. */
export function fixLine(fix) {
  const whats = cleanFaults(fix?.faults).map((f) => f.what.replace(/[.;\s]+$/, ""));
  const note = text(fix?.note, NOTE_CHARS).replace(/[.\s]+$/, "");
  if (!whats.length && !note) return null;
  const head = whats.length ? `Fixed: ${whats.join("; ")}.` : "";
  return [head, note ? `${whats.length ? "Also" : "Fixed"}: ${note}.` : ""].filter(Boolean).join(" ");
}

/** A record's fixes, oldest first: [{ line, created }] (entries that say nothing are left out). */
export function fixLines(rec) {
  const list = Array.isArray(rec?.fixes) ? rec.fixes : [];
  return list.map((f) => ({ line: fixLine(f), created: typeof f?.created === "string" ? f.created : null })).filter((x) => x.line);
}

/** Whether the automatic check found a fault and the picture was fixed (the sheet's quiet line). */
export const autoFixed = (rec) => Boolean(rec && rec.check && typeof rec.check === "object" && rec.check.fixed === true);
