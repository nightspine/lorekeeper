// Lorekeeper gating algorithm (SPEC.md §7): a line-by-line port of lorekeeper/gating.py.
// It must produce exactly the views in conformance/; tests/test_js_gating.py checks that with Node.

export const SPEC_VERSION = "0.1";

const bookKey = (ix) => (ix.series ? ix.series.book : null);
const ordinals = (ix) => new Map(ix.anchors.map((a, i) => [a.id, i]));

function cmp(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return 0;
}

/**
 * @param {object[]} indexes  index documents
 * @param {{book: string|null, anchor: string|null}} position
 * @param {object|null} series  series manifest
 * @param {string|null} order   reading order name (default: series.defaultOrder)
 * @param {string[]|null} finished  finished book keys (default: books before position.book)
 */
export function view(indexes, position, series = null, order = null, finished = null) {
  let readingOrder, current;
  if (!series) {
    if (indexes.length !== 1) throw new Error("multiple indexes require a series manifest");
    readingOrder = [bookKey(indexes[0])];
    current = readingOrder[0];
  } else {
    readingOrder = series.orders[order || series.defaultOrder];
    current = position.book;
    if (!readingOrder.includes(current)) throw new Error(`book ${current} is not in the reading order`);
  }
  let done = new Set(finished ?? readingOrder.slice(0, readingOrder.indexOf(current)));
  done.delete(current);
  const rank = new Map(readingOrder.map((k, i) => [k, i]));

  const byBook = new Map(indexes.map((ix) => [bookKey(ix), ix]));
  if (!byBook.has(current)) throw new Error(`no index supplied for current book ${current}`);
  const curIndex = byBook.get(current);
  const curOrds = ordinals(curIndex);
  if (position.anchor !== null && !curOrds.has(position.anchor)) throw new Error(`unknown anchor ${position.anchor}`);
  const limit = position.anchor !== null ? curOrds.get(position.anchor) : -1;

  const visibleKey = (book, ords, anchor, n) => {
    if (book === current) {
      if (anchor === null || anchor === undefined) return null;
      const o = ords.get(anchor);
      return o <= limit ? [rank.get(book), o, n] : null;
    }
    if (done.has(book)) {
      const o = anchor === null || anchor === undefined ? -1 : ords.get(anchor);
      return [rank.has(book) ? rank.get(book) : rank.size, o, n];
    }
    return null;
  };

  const entities = new Map();
  const names = new Map();
  const facets = [], relations = [], events = [], recaps = [];
  const replaced = new Set();
  let counter = 0;
  const take = (book, ords, item) => { counter += 1; return visibleKey(book, ords, item.from, counter); };

  const sources = indexes.map((ix) => [bookKey(ix), ordinals(ix), ix, false]);
  for (const entry of curIndex.carried || []) {
    if (!byBook.has(entry.origin) && done.has(entry.origin)) {
      sources.push([entry.origin, new Map(), { entities: [entry] }, true]);
    }
  }

  for (const [book, ords, ix, carried] of sources) {
    for (const e of ix.entities || []) {
      const k = visibleKey(book, ords, carried ? null : e.from, counter);
      if (k === null) continue;
      if (!entities.has(e.id)) entities.set(e.id, { id: e.id, type: e.type, key: k });
      const ent = entities.get(e.id);
      if (cmp(k, ent.key) < 0) ent.key = k;
      for (const nm of e.names || []) {
        const nk = take(book, ords, nm);
        if (nk !== null) {
          if (!names.has(e.id)) names.set(e.id, []);
          names.get(e.id).push([nk, nm]);
        }
      }
      for (const f of e.facets || []) {
        const fk = take(book, ords, f);
        if (fk !== null) {
          facets.push([fk, e.id, f, book]);
          (f.replaces || []).forEach((r) => replaced.add(r));
        }
      }
    }
    for (const r of ix.relations || []) {
      const rk = take(book, ords, r);
      if (rk !== null) { relations.push([rk, r, book]); (r.replaces || []).forEach((x) => replaced.add(x)); }
    }
    for (const ev of ix.events || []) {
      const ek = take(book, ords, ev);
      if (ek !== null) events.push([ek, ev, book]);
    }
    for (const rc of ix.recaps || []) {
      const rck = take(book, ords, rc);
      if (rck !== null) recaps.push([rck, rc, book]);
    }
  }

  const facetOut = (f, book) => ({ id: f.id, text: f.text, basis: f.basis, book });
  const relOut = (r, book) => ({ id: r.id, src: r.src, dst: r.dst, label: r.label, book });

  const outEntities = [...entities.values()]
    .sort((a, b) => cmp(a.key, b.key) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .map((ent) => {
      const visibleNames = (names.get(ent.id) || []).sort((a, b) => cmp(a[0], b[0])).map((t) => t[1]);
      const primaries = visibleNames.filter((n) => n.kind === "primary").map((n) => n.value);
      const display = primaries.length ? primaries[primaries.length - 1]
        : visibleNames.length ? visibleNames[0].value : ent.id;
      const aliases = [];
      for (const n of visibleNames) if (n.value !== display && !aliases.includes(n.value)) aliases.push(n.value);
      const mine = facets.filter((t) => t[1] === ent.id).sort((a, b) => cmp(a[0], b[0]));
      // The last visible profile describes the entity, inherited from earlier books until a
      // later one is visible; earlier profiles are history.
      const profiles = mine.filter((t) => t[2].kind === "profile");
      const profile = profiles.length ? profiles[profiles.length - 1] : null;
      return {
        id: ent.id, type: ent.type, name: display, aliases,
        profile: profile ? facetOut(profile[2], profile[3]) : null,
        facets: mine.filter((t) => !replaced.has(t[2].id) && t[2].kind !== "profile").map((t) => facetOut(t[2], t[3])),
        history: mine.filter((t) => t !== profile && (replaced.has(t[2].id) || t[2].kind === "profile")).map((t) => facetOut(t[2], t[3])),
      };
    });

  const visibleIds = new Set(outEntities.map((e) => e.id));
  const relSorted = relations.sort((a, b) => cmp(a[0], b[0]))
    .filter(([, r]) => visibleIds.has(r.src) && visibleIds.has(r.dst));
  let latest = null;
  for (const t of recaps) if ((t[1].scope || "story") === "story" && (!latest || cmp(t[0], latest[0]) > 0)) latest = t;

  const coverage = curIndex.coverage;
  const exhausted = !coverage.complete && position.anchor !== null && limit >= curOrds.get(coverage.through);

  return {
    lorekeeper: SPEC_VERSION,
    kind: "view",
    position: { book: series ? position.book : null, anchor: position.anchor },
    coverage: { complete: coverage.complete, through: coverage.through, exhausted },
    entities: outEntities,
    relations: relSorted.filter(([, r]) => !replaced.has(r.id)).map(([, r, b]) => relOut(r, b)),
    relationHistory: relSorted.filter(([, r]) => replaced.has(r.id)).map(([, r, b]) => relOut(r, b)),
    events: events.sort((a, b) => cmp(a[0], b[0]))
      .map(([, ev, b]) => ({ id: ev.id, summary: ev.summary, entities: ev.entities, book: b })),
    recap: latest ? { id: latest[1].id, text: latest[1].text, book: latest[2] } : null,
    chapters: recaps.filter((t) => t[1].scope === "chapter").sort((a, b) => cmp(a[0], b[0]))
      .map(([, rc, b]) => ({ id: rc.id, title: rc.title ?? null, text: rc.text, book: b })),
  };
}
