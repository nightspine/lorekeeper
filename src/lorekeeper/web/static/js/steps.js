// The Reader's chapter steps: which chapters are finished, which one the reader is in, and what each tick may say.
// Dependency-free so it can be tested in Node (tests/js/run_steps.mjs).
//
// The spoiler rule: a chapter is entered, and so named, only once the position reaches the start the Reader's chapter
// map gives it (the same start the ruler's chapterAt uses). The read route's table of contents can widen the first
// chapter back to block 0 (text before the first contents entry); that start is where the reading view's text begins,
// never a reason to name the chapter.

/** "Chapter XIV" out of "Chapter XIV Mina Harker's Journal", "Prologue" out of "Prologue: The Road". */
export const SELF_NAMED_SHORT = /^\s*(chapter|part|book|section|letter)\s+[\p{L}\p{N}]+|^\s*(prologue|epilogue|preface|introduction|foreword|afterword)\b/iu;

/**
 * The story chapters as steps, oldest first: { n (position in the map, 1-based), k (1..N among the steps), title, seq
 * (the map's start block, the key every per-chapter lookup uses), from, to } with `from`/`to` the reading positions where
 * the chapter is entered and where it ends.
 * @param map      the Reader's chapter map: [{ title, start, story }]
 * @param toc      the read route's chapters ([{ n, title, firstSeq, lastSeq }]) or null
 * @param pos      { blockStart(seq), blockEnd(seq) } of the Reader's position map
 * @param tidy     title clean-up (dom.js tidyTitle)
 */
export function stepList(map, toc, pos, tidy = (t) => t) {
  map = map || [];
  const src = toc && toc.length
    ? toc.map((c) => {
      const m = map[c.n - 1];
      // Entered at the map's start; the toc's firstSeq may sit earlier (text before the first contents entry).
      const seq = m && Number.isInteger(m.start) ? Math.max(m.start, c.firstSeq) : c.firstSeq;
      return { n: c.n, title: c.title || m?.title, seq, to: pos.blockEnd(c.lastSeq), story: m?.story !== false };
    })
    : map.map((c, i) => ({ n: i + 1, title: c.title, seq: c.start, to: map[i + 1] ? pos.blockStart(map[i + 1].start) : 1, story: c.story !== false }));
  const all = src.map((c) => ({ ...c, title: tidy(c.title) || `Section ${c.n}`, from: pos.blockStart(c.seq), to: Math.min(1, c.to) }));
  const story = all.filter((c) => c.story);
  return (story.length ? story : all).map((c, i) => ({ ...c, k: i + 1 }));
}

/** "Chapter XI" for a title that names itself, "Prologue", else "Chapter 11" by its place among the steps. */
export const stepName = (c) => (c.title.match(SELF_NAMED_SHORT)?.[0].trim().replace(/^\w/, (x) => x.toUpperCase())) || `Chapter ${c.k}`;

/** Where position `p` is: the chapter entered (`cur`), the one to finish next, the last finished. */
export function stepState(list, p) {
  const eps = 1e-9;
  const cur = list.find((c) => c.from <= p + eps && p < c.to - eps) || null;
  const next = cur || list.find((c) => c.to > p + eps) || null;
  const last = [...list].reverse().find((c) => c.to <= p + eps) || null;
  return { list, cur, next, last };
}

/** The name of the chapter "I finished …" would finish: its own wording once entered, else only its number. */
export const nextName = (st) => (!st.next ? null : st.next === st.cur ? stepName(st.next) : `Chapter ${st.next.k}`);

/** One sentence for where the reader is, naming only chapters entered or finished. */
export function whereText(st, p) {
  if (!st.list.length) return `You're ${Math.round(p * 100)}% of the way through.`;
  if (!st.next) return "You've finished the book.";
  if (st.cur && p > st.cur.from + 1e-6) return `You're part way through ${stepName(st.cur)}.`;
  if (st.last) return `You're at the end of ${stepName(st.last)}.`;
  return `You haven't started ${nextName(st)} yet.`;
}

/** What tick `c` shows: its state, its tooltip and its accessible name. Ahead of the position it names only a number. */
export function tickInfo(c, st, p) {
  const state = c.to <= p + 1e-9 ? "done" : c === st.cur ? "here" : "ahead";
  return {
    state,
    title: state === "ahead" ? "" : c.title,
    label: state === "done" ? `${c.title}, finished` : state === "here" ? `${c.title}, reading now. Mark it finished`
      : `Chapter ${c.k}, not read yet. Mark it finished`,
  };
}

// ---- the entry's Age tile ---------------------------------------------------------------------

const NUM = "(?:\\d{1,3}|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:-(?:one|two|three|four|five|six|seven|eight|nine))?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)";
const AGE = new RegExp(`\\b(?:aged\\s+${NUM}|${NUM}\\s+years\\s+old)\\b`, "i");
const AGE_START = new RegExp(`^(?:(?:is|was)\\s+)?(aged\\s+${NUM}|${NUM}\\s+years\\s+old)\\b`, "i");
/** Words that hand the age to someone else ("the father of a boy aged five", "with a son aged six"). */
const OTHER = /\b(of|with|has|had|have|her|his|their|its|whose|for|to|and|who|nursed|carries|carried)\b/i;

/**
 * The entry's age, only as its own text states it about the entry, else null. Conservative on purpose:
 * - the profile's first sentence, when the age phrase opens it, or follows "is"/"was" within four words that do not hand
 *   it to someone else ("Lucy is a young woman aged nineteen", not "She nursed a child aged five");
 * - or a detail that begins with the phrase ("Aged nineteen", "Is twenty years old").
 */
export function statedAge(profile, details = []) {
  const first = String(profile || "").trim().match(/^[^.!?]*[.!?]?/)?.[0] || "";
  const m = AGE.exec(first);
  if (m) {
    const before = first.slice(0, m.index);
    if (!before.trim()) return m[0];
    const verb = [...before.matchAll(/\b(?:is|was)\b/gi)].at(-1);
    if (verb) {
      const tail = before.slice(verb.index + verb[0].length).trim();
      const words = tail ? tail.split(/\s+/) : [];
      if (words.length <= 4 && !OTHER.test(tail) && !/[,;:]/.test(tail)) return m[0];
    }
  }
  for (const d of details) {
    const hit = AGE_START.exec(String(d || "").trim());
    if (hit) return hit[1];
  }
  return null;
}
