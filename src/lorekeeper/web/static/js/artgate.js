// The spoiler rules for pictures, with no imports so they can be tested in Node (tests/js/run_artgate.mjs).
// Pictures are gated by their own book's anchors, as names and events are. Text that comes with a picture (the world note,
// the prompt) may also carry content from earlier books of the series (`inherits`): it shows only when those are finished.

/**
 * The pictures of one book's art set that may show at the reader's position. `anchorsOrdinal` maps that book's anchor
 * ids to their ordinals; `position` is the ordinal of the reader's frontier in that book (-1 before the first anchor);
 * `finished` says the book is fully revealed (an earlier book the reader has finished). A picture shows only when its
 * `from` is an anchor of that book at or before the position, compared by ordinal as every `from` is; an unknown anchor
 * never shows. Everywhere a picture is placed, counted or stepped to goes through this. Anchor ids repeat across books
 * (ch1.para01 is in every book), so the caller passes the map of the picture's own book, never another's.
 */
export function visibleArt(manifest, anchorsOrdinal, position, finished) {
  const pics = Array.isArray(manifest?.pictures) ? manifest.pictures : [];
  const out = [];
  for (const r of pics) {
    if (!r || typeof r.id !== "string" || typeof r.from !== "string" || typeof r.kind !== "string") continue;
    const o = anchorsOrdinal.get(r.from);
    if (o == null) continue;
    if (finished || (Number.isInteger(position) && position >= 0 && o <= position)) out.push(r);
  }
  return out;
}

/** Is an item with a `from` anchor of that book visible? The same rule, for one item. */
export function visibleAt(from, anchorsOrdinal, position, finished) {
  const o = typeof from === "string" ? anchorsOrdinal.get(from) : null;
  return o != null && (finished === true || (Number.isInteger(position) && position >= 0 && o <= position));
}

/**
 * The earlier books whose content a text uses and the reader has not finished: [] means it may show. `inherits` is the
 * record's list of series book keys (the book being read itself never counts); without one, a text that says it carries
 * an earlier book's content (`fallback`: an inherited or appended world note, a prompt in a later book of a series) is
 * treated as using every earlier book.
 */
export function unfinishedSources(inherits, { book = null, finished = [], earlier = [], fallback = false } = {}) {
  const done = new Set(finished || []);
  const need = Array.isArray(inherits) ? inherits.filter((b) => typeof b === "string") : fallback ? earlier : [];
  return need.filter((b) => b !== book && !done.has(b));
}

/**
 * The world note a reader may see in the book being read, or null. `world` is that book's manifest note
 * ({ text, from, inherited?, base?, inherits? }); `ords` and `position` are the book being read's. An inherited note's
 * `from` is an anchor of an earlier book, so it is never compared with this book's anchors (ids repeat across books):
 * it shows once the books it comes from are finished. A note of this book shows once its anchor is reached and every
 * book it inherits from is finished.
 */
export function visibleWorld(world, { ords, position, book = null, finished = [], earlier = [] } = {}) {
  if (!world || typeof world.text !== "string" || !world.text.trim()) return null;
  const carries = world.inherited === true || typeof world.base === "string";
  if (unfinishedSources(world.inherits, { book, finished, earlier, fallback: carries }).length) return null;
  if (world.inherited === true) return { text: world.text, own: false };
  // `from` goes with a note of this book: text shown from it is dropped if the position moves back before it.
  return visibleAt(world.from, ords || new Map(), position, false) ? { text: world.text, own: true, from: world.from } : null;
}

/**
 * The background note the picture sheet shows, and whether the person may write one. Only the person's own note
 * (`edited: true`) is shown, and only as `visibleWorld` allows at the position; an automatic note is no longer used, so
 * it is never shown and may be replaced. A note of the person's own that is inherited from an earlier book, or hidden at
 * this position, cannot be changed here (saving would overwrite text the reader may not see).
 */
export function sheetWorld(world, gate = {}) {
  const seen = visibleWorld(world, gate);
  const own = world?.edited === true;
  const shown = seen && own ? seen : null;
  return { world: shown, canEditWorld: shown ? shown.own === true : !own };
}

const REF_ID = /^[A-Za-z0-9_-]{1,64}$/;
const REF_URL = /^\/api\/art\/([A-Za-z0-9:_-]{1,128})\/([a-z0-9-]{1,64})\/([A-Za-z0-9_-]{1,64})$/;

/**
 * A figure's reference in any of the shapes it comes in, checked: a plain id of the picture's own set or `{ set, id }` (as a
 * record stores it), the reference's URL (as the server answers `figures[].ref` on the prompt reply and on each picture), or
 * `{ id, set?, url }`. Gives `{ id, set, url }` (`set` null for a plain id; a URL's set is the one it is served from; `url`
 * null unless a URL was given, and then only an /api/art/<hash>/<set>/<id> address), or null for anything else.
 */
export function refParts(ref) {
  if (typeof ref === "string") {
    if (REF_ID.test(ref)) return { id: ref, set: null, url: null };
    const m = REF_URL.exec(ref);
    return m ? { id: m[3], set: m[2], url: ref } : null;
  }
  if (!ref || typeof ref !== "object") return null;
  const m = typeof ref.url === "string" ? REF_URL.exec(ref.url) : null;
  const id = typeof ref.id === "string" && REF_ID.test(ref.id) ? ref.id : (m ? m[3] : null);
  if (!id) return null;
  return { id, set: typeof ref.set === "string" ? ref.set : (m ? m[2] : null), url: m ? ref.url : null };
}

/** Whether an open lightbox must close: `ids` is its list, per picture it can step to, of the ids that stand for that
 *  picture (the id on screen now, and the one it opened with, since a fix or redraw files a new id that the page's art
 *  set knows only once fetched again); `shown` the visible ids. It closes when any picture has none of its ids shown. */
export const lightboxHides = (ids, shown) => (Array.isArray(ids) ? ids : []).some((alts) => !(Array.isArray(alts) ? alts : [alts]).some((x) => typeof x === "string" && shown.has(x)));

/** A comic's panels as the server sent them: event ids in panel order (strings only, at most 6, each once); [] otherwise. */
export function cleanPanels(list) {
  if (!Array.isArray(list)) return [];
  const out = [];
  for (const id of list) if (typeof id === "string" && id && !out.includes(id) && out.length < 6) out.push(id);
  return out;
}

/** The numbered captions of a comic's panels: each id's summary from the reader's view now (`text(id)`, null when it is
 *  not visible there), an id it has no summary for left out. The numbers are the panels' own, so a gap stays a gap. */
export function panelCaptions(panels, text) {
  const out = [];
  cleanPanels(panels).forEach((id, n) => { const t = text ? text(id) : null; if (typeof t === "string" && t) out.push({ n: n + 1, text: t }); });
  return out;
}
