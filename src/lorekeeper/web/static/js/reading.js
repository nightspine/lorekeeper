// The books opened most recently in the Reader, remembered per browser for the rail's "Reading" group and the phone
// tab bar. An entry holds only what the person set themselves: the book's title, the position they moved to, the
// chapter of the book that position falls in, and the address that reopens it there. Nothing from the index (no
// counts, no names, nothing past the position) is ever stored or shown.

const KEY = "lk-reading", MAX = 5, EVENT = "lk-reading";

const valid = (e) => e && typeof e.key === "string" && typeof e.title === "string" && Number.isFinite(e.p)
  && typeof e.hash === "string" && e.hash.startsWith("#/reader?");

/** Newest first, at most five. */
export function readingList() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "[]");
    return Array.isArray(v) ? v.filter(valid).slice(0, MAX) : [];
  } catch { return []; }
}

function save(list) {
  try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX))); } catch { return; }
  window.dispatchEvent(new CustomEvent(EVENT));
}

/** The Reader calls this whenever the position or the book changes. */
export function rememberReading({ key, title, p, chapter = null, hash }) {
  const entry = { key, title, p: Math.min(1, Math.max(0, p)), chapter: chapter || null, hash, at: Date.now() };
  if (!valid(entry)) return;
  const list = readingList();
  const same = list[0]?.key === key && list[0].p === entry.p && list[0].chapter === entry.chapter && list[0].hash === hash && list[0].title === title;
  if (same) return;
  save([entry, ...list.filter((e) => e.key !== key)]);
}

/** Drop entries whose index is no longer in the library. */
export function keepReading(keys) {
  const list = readingList();
  const kept = list.filter((e) => keys.has(e.key));
  if (kept.length !== list.length) save(kept);
}

/** "35%, in Chapter XI", or "Finished". The percentage reads like the Reader's. */
export function readingLine(e) {
  if (e.p >= 0.995) return "Finished";
  const pct = `${(e.p * 100).toFixed(e.p < 0.1 && e.p > 0 ? 1 : 0)}%`;
  return e.chapter ? `${pct}, in ${e.chapter}` : pct;
}

export function onReading(fn) {
  window.addEventListener(EVENT, fn);
  const other = (e) => { if (e.key === KEY) fn(); };   // another tab moved
  window.addEventListener("storage", other);
  return () => { window.removeEventListener(EVENT, fn); window.removeEventListener("storage", other); };
}
