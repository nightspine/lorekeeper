// The Library: every book from uploads and Book Orbit as one table (or a grid of covers), with the chosen
// book in a details pane beside it. The header row (title, count, search, Add an EPUB) and the filter row
// are built once; a reload (every 5 s while something builds) repaints only the rows and the pane, and
// only when their data changed, so search typing, focus, the selection and scroll positions survive it.
import { api } from "./api.js";
import { parseHash, setParams } from "./app.js";
import { bookNo, buildDialog, builtWith, cover, NO_EPUB, numWord, partLine, startBuild, statusChip } from "./build.js";
import { clear, fmtInt, h, icon, loadError, loading, relTime, toast } from "./dom.js";

// Column sorts: every sort the old select offered. Recently indexed runs newest first.
const SORTS = { title: "Title", author: "Author", series: "Series", recent: "Last indexed" };
const SHOWS = [["all", "All books"], ["ready", "Ready to read"], ["missing", "No index"], ["building", "Building"]];
const SOURCES = [["all", "Everywhere"], ["bookorbit", "Book Orbit"], ["upload", "Added here"]];
const ARTICLE = /^(the|a|an)\s+/i;

const fold = (x) => (x || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const surname = (name) => (name || "").trim().split(/\s+/).pop() || "";
const isReady = (b) => b.status === "complete" || b.status === "partial";
const KIND_LABEL = { fiction: "Fiction", "non-fiction": "Non-fiction", "self-help": "Self-help" };
const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
/** "Non-fiction" or "Self-help" for a book whose default index is not fiction; nothing for fiction or no index. */
const kindOf = (b) => { const k = b.versions.find((v) => v.isDefault)?.bookKind; return k && k !== "fiction" ? KIND_LABEL[k] || k : null; };
const plural = (n, one, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;
const updatedAt = (b) => Math.max(0, ...b.versions.map((v) => Date.parse(v.updated) || 0));
const shortDate = (t) => (t ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "");
const readerHref = (b) => {
  const d = b.versions.find((v) => v.isDefault);
  return d ? `#/reader?i=${encodeURIComponent(d.key)}` : null;
};
/** "Sherlock Holmes 2", or the name alone. */
const seriesText = (b) => (b.series ? `${b.series.name}${b.series.index != null && b.series.index !== "" ? ` ${b.series.index}` : ""}` : "");

/** The row's one action: Read, View progress, or Build (refused with a reason when there is no EPUB). */
export function rowAction(b, { small = true } = {}) {
  const href = readerHref(b);
  const cls = small ? ".small" : "";
  if (href) return h(`a.btn${cls}`, { href, "aria-label": `Read ${b.title}`, tabindex: "-1", dataset: { role: "act" } }, "Read");
  if (b.status === "building" && b.job) return h(`a.btn${cls}`, { href: `#/studio/job/${b.job.id}`, "aria-label": `${b.title}: view progress`, tabindex: "-1", dataset: { role: "act" } }, "Progress");
  return h(`button.btn${cls}`, { type: "button", tabindex: "-1", dataset: { role: "act" }, "aria-label": `Build ${b.title}`, "aria-disabled": b.canBuild ? null : "true",
    on: { click: (e) => { e.stopPropagation(); b.canBuild ? startBuild(b) : toast(NO_EPUB, "error"); } } }, "Build");
}

export function mountLibrary(root) {
  const { params } = parseHash();
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("lk-library") || "{}"); } catch {}
  const s = {
    data: null, demo: undefined, q: params.get("q") || "", selected: params.get("book"),
    sort: SORTS[params.get("sort") || saved.sort] ? params.get("sort") || saved.sort : "title",
    view: (params.get("view") || saved.view) === "grid" ? "grid" : "list",
    show: params.get("show") || "all", source: params.get("source") || "all",
  };
  let timer, searchTimer, disposed = false, listSig = "", paneSig = "";

  // ---- the frame: built once ---------------------------------------------------------------
  const count = h("p.lib-count");
  const search = h("input.input.lib-search", { type: "search", placeholder: "Search titles, authors, series", value: s.q,
    "aria-label": "Search the library", "aria-keyshortcuts": "/" });
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { s.q = search.value; paintList(); persist(); }, 120);
  });
  search.addEventListener("keydown", (e) => { if (e.key === "Escape" && search.value) { e.preventDefault(); search.value = ""; s.q = ""; paintList(); persist(); } });
  const upload = h("input", { type: "file", accept: ".epub,application/epub+zip", class: "sr-only", id: "lib-upload" });
  upload.addEventListener("change", async () => {
    const f = upload.files[0];
    if (!f) return;
    const done = toast(`Uploading “${f.name}”… 0%`, "busy");
    const msg = document.querySelector("#toasts .toast:last-child .toast-msg");
    try {
      const b = await api.upload(f, (p) => { if (msg) msg.textContent = `Uploading “${f.name}”… ${Math.round(p * 100)}%`; });
      done(); toast(`Added “${b.title}”`); s.selected = `up:${b.id}`; load();
    } catch (e) { done(); toast(e.message, "error"); }
    finally { upload.value = ""; }
  });
  const showGroup = h("div.lib-show", { role: "group", "aria-label": "Show" });
  const sourceSel = h("select.select.lib-source", { id: "lib-source" });
  sourceSel.addEventListener("change", () => { s.source = sourceSel.value; paintFilters(); paintList(); persist(); });
  const boNote = h("span.lib-bo");
  // The same sorts as the column headers, for where a header isn't shown (Covers, narrow windows, the pane open, a phone).
  const sortSel = h("select.select", { id: "lib-sort", "aria-label": "Sort books" },
    Object.entries(SORTS).map(([k, label]) => h("option", { value: k, selected: s.sort === k }, `${label}${k === "recent" ? ", newest first" : ""}`)));
  sortSel.addEventListener("change", () => { s.sort = sortSel.value; paintList(); persist(); });
  const viewBtns = [["list", "List"], ["grid", "Covers"]].map(([v, label]) =>
    h("button", { type: "button", dataset: { v }, "aria-pressed": String(s.view === v), on: { click: () => {
      s.view = v; viewBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v))); listSig = ""; paintList(); persist();
    } } }, label));
  let toolbar;
  const list = h("div.lib-list");
  const pane = h("aside.lib-pane", { "aria-label": "Book details", hidden: true });
  const split = h("div.lib-split", list, pane);
  root.append(h("div.library",
    h("header.page-head.lib-head",
      h("div.lib-title", h("h1", "Library"), count),
      h("div.lib-head-end",
        h("div.search.lib-search-wrap", icon("search"), search, h("kbd.kbd", { "aria-hidden": "true" }, "/")),
        h("label.btn.primary", { for: "lib-upload" }, icon("upload"), "Add an EPUB"), upload)),
    toolbar = h("div.lib-toolbar", { hidden: true },   // shown once there are books to filter
      showGroup,
      h("label.lib-from", h("span", "From"), sourceSel),
      h("label.lib-from.lib-sort", h("span", "Sort"), sortSel),
      boNote,
      h("div.seg.lib-view", { role: "group", "aria-label": "View" }, viewBtns)),
    split));
  clear(list, loading("Loading your library…"));

  async function load() {
    let data;
    try { data = await (s.data ? api.catalog() : api.catalogShared(5000)); } catch (e) {
      if (disposed) return;
      // A failed first load says so in the page; a failed background reload keeps what is shown.
      if (!s.data) clear(list, loadError("load the library", e, () => { clear(list, loading("Loading your library…")); load(); }));
      else if (s.data.books.some((b) => b.status === "building")) { clearTimeout(timer); timer = setTimeout(load, 5000); }
      return;
    }
    if (disposed) return;
    s.data = data;
    // Screenshot flags: ?demo=empty pretends there are no books, ?demo=none-ready that no book has an index yet.
    if (params.get("demo") === "empty") s.data = { ...s.data, books: [], series: [] };
    else if (params.get("demo") === "none-ready") s.data = { ...s.data, books: s.data.books.map((b) => (isReady(b) ? { ...b, status: "missing", versions: [] } : b)) };
    if (s.demo === undefined) {
      try {
        const lib = await api.library();
        const ex = (lib.indexes || []).filter((i) => i.source === "examples");
        // The whole-novel Dracula example first; the two-chapter Sherlock ones are hand-written and short.
        s.demo = ex.find((i) => i.key.includes("dracula")) || ex.find((i) => i.key.includes("study-in-scarlet")) || ex[0] || null;
      } catch { s.demo = null; }
    }
    if (disposed) return;   // left while the example was being looked up: no paint, no address rewrite, no timer
    paint();
    clearTimeout(timer);
    if (s.data.books.some((b) => b.status === "building")) timer = setTimeout(load, 5000);
  }

  function persist() {
    try { localStorage.setItem("lk-library", JSON.stringify({ view: s.view, sort: s.sort })); } catch {}
    const q = {};
    if (s.q) q.q = s.q;
    if (s.sort !== "title") q.sort = s.sort;
    if (s.view !== "list") q.view = s.view;
    if (s.show !== "all") q.show = s.show;
    if (s.source !== "all") q.source = s.source;
    if (s.selected) q.book = s.selected;
    setParams(q);
  }

  // ---- what to show, in what order ---------------------------------------------------------

  const sortKey = (b) => ({
    title: b.title.replace(ARTICLE, ""),
    author: `${surname(b.authors[0])} ${b.authors[0] || ""} ${b.title}`,
    series: b.series ? `${b.series.name.replace(ARTICLE, "")} ${String(b.series.num ?? 999).padStart(5, "0")}` : `~${b.title}`,
  })[s.sort] || b.title;

  function matches() {
    const words = fold(s.q).split(/\s+/).filter(Boolean);
    return s.data.books.filter((b) => {
      if (s.source !== "all" && b.source !== s.source) return false;
      if (s.show === "ready" && !isReady(b)) return false;
      if (s.show === "missing" && b.status !== "missing") return false;
      if (s.show === "building" && b.status !== "building") return false;
      if (!words.length) return true;
      const hay = fold([b.title, ...b.authors, b.series?.name || ""].join(" "));
      return words.every((w) => hay.includes(w));
    });
  }

  /** [{ group, books }] in display order. Series order: one group per series, then the standalone books.
   *  Last indexed: newest first, then the books with no index. Title and author: one ungrouped run. */
  function groups(books) {
    if (s.sort === "recent") {
      const done = books.filter((b) => b.versions.length).sort((a, b) => updatedAt(b) - updatedAt(a));
      const rest = books.filter((b) => !b.versions.length).sort((a, b) => fold(a.title).localeCompare(fold(b.title)));
      return [{ group: null, books: done }, { group: rest.length && done.length ? { label: "Not indexed yet" } : null, books: rest }].filter((g) => g.books.length);
    }
    const sorted = [...books].sort((a, b) => fold(sortKey(a)).localeCompare(fold(sortKey(b))));
    if (s.sort !== "series") return [{ group: null, books: sorted }];
    const out = [];
    for (const b of sorted) {
      const id = b.series?.id || "~";
      if (!out.length || out[out.length - 1].id !== id) {
        const g = b.series && s.data.series.find((x) => x.id === b.series.id);
        out.push({ id, group: g ? { series: g } : { label: "Standalone" }, books: [] });
      }
      out[out.length - 1].books.push(b);
    }
    return out.sort((a, b) => (a.id === "~") - (b.id === "~"));   // standalone books after every series
  }

  // ---- paint -------------------------------------------------------------------------------

  function paint() {
    paintFilters();
    paintList();
    paintPane();
    persist();
  }

  function paintFilters() {
    if (!s.data) return;   // the frame exists before the catalog arrives (slow load, or the "Try again" state)
    const books = s.data.books;
    const n = (f) => books.filter(f).length;
    const showCount = { all: books.length, ready: n(isReady), missing: n((b) => b.status === "missing"), building: n((b) => b.status === "building") };
    const srcCount = { all: books.length, bookorbit: n((b) => b.source === "bookorbit"), upload: n((b) => b.source === "upload") };
    // Repainted in place: the pressed button keeps focus across a reload.
    const had = showGroup.contains(document.activeElement) ? document.activeElement.dataset.k : null;
    clear(showGroup, SHOWS.map(([k, label]) => h("button.lib-filter", { type: "button", dataset: { k }, "aria-pressed": String(s.show === k),
      on: { click: () => { s.show = k; paintFilters(); paintList(); persist(); } } }, label, h("span.n", fmtInt(showCount[k])))));
    if (had) showGroup.querySelector(`[data-k="${had}"]`)?.focus();
    const sel = sourceSel.value || s.source;
    clear(sourceSel, SOURCES.map(([k, label]) => h("option", { value: k, selected: s.source === k }, `${label}, ${fmtInt(srcCount[k])}`)));
    sourceSel.value = s.source || sel;
    const bo = s.data.bookorbit || {};
    clear(boNote, bo.connected ? [`Book Orbit synced ${relTime(bo.synced)}`]
      : h("a", { href: "#/settings/bookorbit" }, icon("link"), "Connect Book Orbit"));
  }

  /** Remember what had focus in the list, by book key and role, so a repaint can put it back. */
  function focusMark() {
    const a = document.activeElement;
    if (!a || !list.contains(a)) return null;
    const host = a.closest("[data-key]");
    return host ? { key: host.dataset.key, role: a.dataset.role || "" } : { other: a.dataset.sort || null };
  }
  function focusRestore(m) {
    if (!m) return;
    if (m.key) {
      const host = list.querySelector(`[data-key="${CSS.escape(m.key)}"]`);
      (host?.querySelector(`[data-role="${m.role}"]`) || host?.querySelector("[data-role=open]"))?.focus({ preventScroll: true });
    } else if (m.other) list.querySelector(`[data-sort="${m.other}"]`)?.focus({ preventScroll: true });
  }

  function paintList() {
    if (!s.data) return;
    sortSel.value = s.sort;
    root.querySelector(".library")?.classList.toggle("covers", s.view === "grid");
    const total = s.data.books.length;
    const books = matches();
    const ready = s.data.books.filter(isReady).length;
    const buildingN = s.data.books.filter((b) => b.status === "building").length;
    const extra = [ready ? `${fmtInt(ready)} ready to read` : null, buildingN ? `${fmtInt(buildingN)} building` : null].filter(Boolean).join(", ");
    count.textContent = !total ? "No books yet."
      : books.length === total ? `${plural(total, "book")} in ${plural(s.data.series.length, "series", "series")}.${extra ? ` ${cap(extra)}.` : ""}`
        : `${fmtInt(books.length)} of ${plural(total, "book")}.`;
    // With no books there is nothing to filter.
    toolbar.hidden = !total;
    // Skip the repaint when nothing on screen would change (the 5 s reload while building).
    const sig = JSON.stringify([s.view, s.sort, s.show, s.source, s.q, total ? books : 0, s.data.series, Boolean(s.demo),
      // a series group row counts the indexed books of the whole series, including books the filter hides
      s.data.books.map((b) => [b.key, b.status, b.coverage, b.canBuild])]);
    if (sig === listSig) { paintSelection(); return; }
    listSig = sig;
    const mark = focusMark();

    if (!total) {
      const first = demoPanel(false);
      clear(list, first || h("div.empty", h("div.art", icon("book-open")), h("h3", "Your library is empty"),
        h("p", "Connect Book Orbit to see all your books, or add an EPUB."),
        h("div.btns", { style: { justifyContent: "center" } },
          h("a.btn.primary", { href: "#/settings/bookorbit" }, icon("link"), "Connect Book Orbit"),
          h("label.btn", { for: "lib-upload" }, "Add an EPUB"))));
      return;
    }
    if (!books.length) {
      clear(list, h("div.empty", h("div.art", icon("search")), h("h3", s.q ? `Nothing matches “${s.q}”` : "No books here"),
        h("p", s.q ? "Search looks in titles, authors and series names. Check the spelling, or try just a surname." : "Try another filter."),
        h("div.btns", { style: { justifyContent: "center" } },
          s.q ? h("button.btn", { type: "button", on: { click: () => { s.q = ""; search.value = ""; paintList(); persist(); } } }, "Clear search") : null,
          s.show !== "all" || s.source !== "all" ? h("button.btn", { type: "button", on: { click: () => { s.show = "all"; s.source = "all"; paintFilters(); paintList(); persist(); } } }, "Show all books") : null)));
      return;
    }
    const gs = groups(books);
    // No book has an index yet: the example still offers a way in.
    const hint = !ready && s.show === "all" && !s.q ? demoPanel(true) : null;
    clear(list, hint, s.view === "grid" ? gridView(gs) : tableView(gs));
    paintSelection();
    focusRestore(mark);
  }

  // ---- the table ---------------------------------------------------------------------------

  function sortHeader(cls, keys) {
    // One <th> may hold two sorts (title and author share the first column); aria-sort names the active one.
    const active = keys.find((k) => k === s.sort);
    return h(`th${cls}`, { scope: "col", "aria-sort": active ? (active === "recent" ? "descending" : "ascending") : null },
      keys.map((k, i) => [i ? h("span.sort-sep", { "aria-hidden": "true" }, " and ") : null,
        h("button.sort", { type: "button", dataset: { sort: k }, "aria-pressed": String(k === s.sort),
          "aria-label": `Sort by ${SORTS[k].toLowerCase()}${k === s.sort ? ", sorted" : ""}`,
          on: { click: () => { s.sort = k; paintList(); persist(); list.querySelector(`[data-sort="${k}"]`)?.focus(); } } },
        SORTS[k], k === s.sort ? h("span.sort-mark", { "aria-hidden": "true" }, k === "recent" ? "↓" : "↑") : null)]));
  }

  function tableView(gs) {
    const table = h("table.data.books",
      h("caption.sr-only", "Books. Up and down arrows move between books, Enter shows one's details, Tab reaches its action."),
      h("thead", h("tr",
        sortHeader(".c-title", ["title", "author"]),
        sortHeader(".c-series", ["series"]),
        h("th.c-status", { scope: "col" }, "Index"),
        sortHeader(".c-date", ["recent"]),
        h("th.c-act", { scope: "col" }, h("span.sr-only", "Action")))),
      gs.map((g) => h("tbody", g.group ? groupRow(g) : null, g.books.map((b) => bookRow(b, Boolean(g.group?.series))))));
    table.addEventListener("keydown", onTableKey);
    return h("div.table-wrap.books-wrap", table);
  }

  function groupRow(g) {
    if (!g.group.series) return h("tr.group-row", h("th", { colspan: "5", scope: "colgroup" }, g.group.label));
    const sg = g.group.series;
    const byKey = Object.fromEntries(s.data.books.map((b) => [b.key, b]));
    const all = sg.books.map((k) => byKey[k]).filter(Boolean);
    const missing = all.filter((b) => b.status === "missing" && b.canBuild).length;
    const indexed = all.filter(isReady).length;
    const cnt = plural(all.length, "book") + (indexed === 0 ? (all.length === 1 ? ", not indexed" : ", none indexed")
      : indexed === all.length ? (all.length === 1 ? ", indexed" : ", all indexed") : `, ${numWord(indexed)} indexed`);
    return h("tr.group-row", h("th", { colspan: "5", scope: "colgroup" },
      h("a.group-name", { href: `#/series/${encodeURIComponent(sg.id)}` }, sg.name),
      h("span.group-count", cnt),
      missing && all.length > 1 ? h("button.linkish.group-act", { type: "button", on: { click: () => buildMissing(sg) } },
        missing === all.length && missing > 1 ? `Build all ${numWord(missing)} in order` : `Build ${missing === 1 ? "the missing book" : `${numWord(missing)} missing`} in order`) : null));
  }

  function bookRow(b, inSeries) {
    const warn = warnFor(b);
    const last = b.status === "building" ? "Now" : shortDate(updatedAt(b));
    const tr = h("tr.book", { dataset: { key: b.key }, on: { click: (e) => { if (!e.target.closest("a, button:not([data-role=open]), input")) select(b.key, floating()); } } },
      h("td.c-title",
        h("span.t", cover(b, "xs"),
          h("span.t-text",
            h("button.t-open", { type: "button", dataset: { role: "open" }, tabindex: "-1",
              "aria-label": `${b.title}${b.authors.length ? `, by ${b.authors.join(", ")}` : ""}, ${chipText(b)}. Show details`,
              on: { click: (e) => { e.stopPropagation(); select(b.key, floating()); } } }, b.title),
            h("span.t-sub", b.authors.join(", ") || "Unknown author",
              // The series repeats here only when the Series column is hidden (narrow, or the pane is open).
              inSeries ? null : b.series ? h("span.t-series", `, ${seriesText(b)}`) : null,
              kindOf(b) ? h("span.t-kind", `, ${kindOf(b)}`) : null,
              warn ? h("span.row-warn", `. ${warn}.`) : null)))),
      h("td.c-series.muted", inSeries ? bookNo(b.series) ? cap(bookNo(b.series)) : "" : seriesText(b)),
      h("td.c-status", statusChip(b)),
      h("td.c-date.subtle.num", last),
      h("td.c-act", rowAction(b)));
    return tr;
  }

  const chipText = (b) => statusChip(b).textContent;

  /** Up and down move between books (the selection follows when the pane is open), Enter shows the details,
   *  Home and End jump to the ends. One title button at a time is in the tab order. */
  function onTableKey(e) {
    const btn = e.target.closest?.("[data-role=open]");
    if (!btn) return;
    const all = [...list.querySelectorAll("[data-role=open]")];
    const i = all.indexOf(btn);
    const to = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: all.length - 1 }[e.key];
    if (to == null) return;
    e.preventDefault();
    const next = all[Math.max(0, Math.min(all.length - 1, to))];
    if (!next || next === btn) return;
    const key = next.closest("[data-key]").dataset.key;
    if (s.selected) select(key, false); else rove(key);
    next.focus();
    next.closest("tr")?.scrollIntoView({ block: "nearest" });
  }
  /** One book at a time is in the tab order: its title button and, in the table, its row action (Read, Progress, Build). */
  let roveKey = null;   // the row in the tab order, kept across reloads when nothing is selected
  function rove(key) {
    const opens = [...list.querySelectorAll("[data-role=open]")];
    const target = opens.find((x) => x.closest("[data-key]").dataset.key === key) || opens[0];
    roveKey = target?.closest("[data-key]").dataset.key ?? null;
    opens.forEach((x) => {
      const on = x === target ? "0" : "-1";
      x.setAttribute("tabindex", on);
      x.closest("tr")?.querySelector(".c-act .btn")?.setAttribute("tabindex", on);
    });
  }

  // ---- covers ------------------------------------------------------------------------------

  function gridView(gs) {
    const grid = h("div.lib-grid");
    for (const g of gs) {
      if (g.group) {
        const sg = g.group.series;
        grid.append(h("h2.grid-group", sg ? h("a", { href: `#/series/${encodeURIComponent(sg.id)}` }, sg.name) : g.group.label));
      }
      grid.append(h("div.shelf", g.books.map((b) => bookTile(b, Boolean(g.group?.series)))));
    }
    grid.addEventListener("keydown", (e) => {
      const btn = e.target.closest?.("[data-role=open]");
      if (!btn) return;
      const all = [...grid.querySelectorAll("[data-role=open]")];
      const i = all.indexOf(btn);
      const to = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: all.length - 1 }[e.key];
      if (to == null) return;
      e.preventDefault();
      const next = all[Math.max(0, Math.min(all.length - 1, to))];
      if (s.selected) select(next.closest("[data-key]").dataset.key, false); else rove(next.closest("[data-key]").dataset.key);
      next.focus();
    });
    return grid;
  }

  function bookTile(b, inSeries) {
    return h("div.book-card", { dataset: { key: b.key } },
      h("button.tile-open", { type: "button", dataset: { role: "open" }, tabindex: "-1", "aria-label": `${b.title}, ${chipText(b)}. Show details`,
        on: { click: () => select(b.key, floating()) } },
        h("span.cover-wrap", cover(b)),
        h("span.card-title", b.title),
        h("span.card-meta", inSeries ? cap(bookNo(b.series) || b.authors[0] || "") : b.authors[0] || "", kindOf(b) ? `, ${kindOf(b)}` : "")),
      h("span.card-row", statusChip(b)));
  }

  // ---- selection and the details pane ------------------------------------------------------

  // Below 1100px the pane floats over the list, so choosing a book moves focus into it (Escape or Close returns it).
  const floating = () => matchMedia("(max-width: 1100px)").matches;
  function select(key, focusPane) {
    s.selected = key;
    paintPane();
    paintSelection();
    persist();
    if (focusPane) pane.querySelector("h2")?.focus();
  }

  /** Clear the selection; with `refocus`, hand focus back to the row or tile it came from. */
  function deselect(refocus) {
    const key = s.selected;
    s.selected = null; paintPane(); paintSelection(); persist();
    if (!refocus || !key) return;
    list.querySelector(`[data-key="${CSS.escape(key)}"] [data-role=open]`)?.focus();
  }

  function paintSelection() {
    list.querySelectorAll("[data-key]").forEach((el) => {
      const on = el.dataset.key === s.selected;
      el.classList.toggle("selected", on);
      const open = el.querySelector("[data-role=open]");
      if (open) { if (on) open.setAttribute("aria-current", "true"); else open.removeAttribute("aria-current"); }
    });
    rove(s.selected ?? roveKey);
  }

  function paintPane() {
    const b = s.data?.books.find((x) => x.key === s.selected);
    const open = Boolean(b);
    pane.hidden = !open;
    split.classList.toggle("with-pane", open);
    if (!b) { paneSig = ""; clear(pane); return; }
    // The pane also shows the rest of the series (its list and the earlier-book warning), so their state is in the signature too.
    const sg = b.series && s.data.series.find((g) => g.id === b.series.id);
    const siblings = sg ? sg.books.map((k) => s.data.books.find((x) => x.key === k)).filter(Boolean)
      .map((x) => [x.key, x.status, x.coverage, x.canBuild, x.job?.progress]) : null;
    const sig = JSON.stringify([b, sg, siblings, b.series && s.data.books.filter((x) => x.series?.id === b.series.id).map((x) => [x.key, x.status])]);
    if (sig === paneSig) return;   // a reload that changed nothing about this book leaves the pane, its focus and scroll alone
    paneSig = sig;
    const keep = pane.contains(document.activeElement) ? document.activeElement.dataset.act : null;
    const scroll = pane.scrollTop;
    const series = b.series ? s.data.series.find((g) => g.id === b.series.id) : null;
    const byKey = Object.fromEntries(s.data.books.map((x) => [x.key, x]));
    const d = b.versions.find((v) => v.isDefault);
    const warn = warnFor(b);
    const href = readerHref(b);
    const kind = d?.bookKind;
    const build = b.status === "building"
      ? h("a.btn", { href: `#/studio/job/${b.job.id}`, dataset: { act: "progress" } }, "View progress")
      : h(`button.btn${href ? "" : ".primary"}`, { type: "button", dataset: { act: "build" }, "aria-disabled": b.canBuild ? null : "true",
        on: { click: () => (b.canBuild ? startBuild(b) : toast(NO_EPUB, "error")) } }, b.status === "missing" ? "Build index" : "Rebuild index");
    clear(pane,
      h("button.icon-btn.pane-close", { type: "button", "aria-label": "Close details", title: "Close details", dataset: { act: "close" },
        on: { click: () => deselect(true) } }, icon("x")),
      cover(b, "md"),
      h("h2", { tabindex: "-1" }, b.title),
      h("p.by", b.authors.join(", ") || "Unknown author"),
      h("p.pane-state", stateSentence(b, d)),
      h("div.btns.pane-actions",
        href ? h("a.btn.primary", { href, dataset: { act: "read" }, "aria-label": `Read ${b.title}` }, "Read") : null,
        build),
      warn ? h("div.notice.warn", h("div", `${warn}. `, /library/.test(warn)
        ? ["Add it (", h("a", { href: "#/settings/bookorbit" }, "connect Book Orbit"), " or add the EPUB) and build it first so characters and relationships carry over, or build this book on its own."]
        : "Build it first so characters and relationships carry over, or build this book on its own.")) : null,
      !b.canBuild ? h("div.notice.info", h("div", NO_EPUB)) : null,
      h("dl.kv",
        b.words ? [h("dt", "Words"), h("dd", fmtInt(b.words))] : null,
        kind ? [h("dt", "Kind"), h("dd", KIND_LABEL[kind] || kind)] : null,
        h("dt", "From"), h("dd", b.source === "bookorbit" ? "Book Orbit" : "Uploaded here"),
        h("dt", "Series"), h("dd", series ? h("a", { href: `#/series/${encodeURIComponent(series.id)}` }, `${series.name}${bookNo(b.series) ? `, ${bookNo(b.series)}` : ""}`) : "Standalone")),
      b.versions.length ? h("section.pane-versions", { "aria-labelledby": "pane-ver" },
        h("h3#pane-ver", "Versions"),
        h("ul.pane-list", b.versions.map((v) => h("li",
          h("span", h("b", v.model || "Unknown model"),
            h("small", [v.complete ? "Whole book" : v.coverage != null ? `${v.coverage}% of the book` : "Part of the book",
              v.updated ? shortDate(Date.parse(v.updated)) : null].filter(Boolean).join(", ") + (v.isDefault ? ". Used for reading." : "."))))))) : null,
      series && series.books.length > 1 ? h("section.pane-series", { "aria-labelledby": "pane-ser" },
        h("h3#pane-ser", "In this series"),
        h("ul.pane-list", series.books.map((k) => byKey[k]).filter(Boolean).map((x) => h(`li${x.key === b.key ? ".here" : ""}`,
          h("a", { href: `#/book/${encodeURIComponent(x.key)}`, "aria-current": x.key === b.key ? "true" : null },
            h("span.subtle.num", `${x.series.index ?? "?"}.`), " ", x.title), statusChip(x))))) : null,
      h("p.pane-more", h("a", { href: `#/book/${encodeURIComponent(b.key)}`, dataset: { act: "page" } }, "Versions, runs and book settings")));
    pane.scrollTop = scroll;
    if (keep) pane.querySelector(`[data-act="${keep}"]`)?.focus({ preventScroll: true });
  }

  /** The state in one sentence: "A complete index of the whole book.", "Building, part 3 of 12." */
  function stateSentence(b, d) {
    const prog = b.job?.progress;
    if (b.status === "building") return prog?.chunks ? `Building now, ${partLine(prog, "building")}.` : "Building now.";
    if (b.status === "complete") return `A complete index of the whole book${d?.model ? `, ${builtWith(d.model)}` : ""}.`;
    if (b.status === "partial") return `An index of ${b.coverage != null ? `${b.coverage}% of the book` : "part of the book"}${d?.model ? `, ${builtWith(d.model)}` : ""}.`;
    return b.canBuild ? "No index yet." : "No index yet, and no EPUB to build one from.";
  }

  /** A short sentence (no full stop) about earlier books this one would build on, or null. */
  function warnFor(b) {
    if (!b.series || b.series.num == null || b.status !== "missing") return null;
    const prev = s.data.books.filter((x) => x.series && x.series.id === b.series.id && x.series.num != null && x.series.num < b.series.num);
    const gap = prev.filter((x) => x.status === "missing");
    if (gap.length) return gap.length === 1 ? `Book ${numWord(gap[0].series.index)} has no index yet` : `${cap(numWord(gap.length))} earlier books have no index yet`;
    const have = new Set(prev.map((x) => x.series.num));
    const absent = [];
    for (let n = 1; n < Math.floor(b.series.num); n++) if (!have.has(n)) absent.push(n);
    return absent.length ? (absent.length === 1 ? `Book ${numWord(absent[0])} isn't in your library` : `Books ${absent.map(numWord).join(", ")} aren't in your library`) : null;
  }

  function buildMissing(g) {
    const byKey = Object.fromEntries(s.data.books.map((b) => [b.key, b]));
    const todo = g.books.map((k) => byKey[k]).filter((b) => b && b.status === "missing" && b.canBuild);
    if (!todo.length) return toast("None of the missing books has an EPUB to index.", "error");
    buildDialog(todo.map((b) => ({ key: b.key, title: b.title, words: b.words })), { title: `Build ${g.name} in order` });
  }

  /** The way in for a stranger: the finished example index opens in the Reader with no build. */
  function demoPanel(compact) {
    const d = s.demo;
    if (!d) return null;
    const entry = { title: d.title, authors: d.authors || [] };
    const href = `#/reader?i=${encodeURIComponent(d.key)}`;
    if (compact) {
      return h("section.demo-panel.compact", { "aria-label": "Try the companion" },
        cover(entry, "xs"),
        h("p.demo-line", `While your first index builds, try the companion on ${d.title}.`),
        h("a.btn.small", { href }, "Try it"));
    }
    return h("section.demo-first", { "aria-labelledby": "demo-head" },
      h("div.demo-panel",
        cover(entry, "lg"),
        h("div.demo-text",
          h("h2", { id: "demo-head" }, `Read ${d.title} with a companion that never spoils`),
          h("p", "Drag the position slider and watch the cast, places and events appear only as the book reveals them."),
          h("a.btn.primary.lg", { href }, "Try it now"))),
      h("p.demo-then", "Then bring your own books: ", h("label.demo-link", { for: "lib-upload" }, "add an EPUB"),
        ", or ", h("a", { href: "#/settings/bookorbit" }, "connect Book Orbit"), "."));
  }

  // "/" focuses search; Escape clears the selection (unless a field, dialog or drawer has it).
  const onKey = (e) => {
    if (e.key === "Escape" && s.selected && !e.defaultPrevented && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)
        && !document.querySelector("dialog[open], .modal-backdrop")) { deselect(true); return; }
    if (e.key !== "/" || e.metaKey || e.ctrlKey || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
    e.preventDefault();
    search.focus();
  };
  document.addEventListener("keydown", onKey);

  load();
  return () => { disposed = true; clearTimeout(timer); document.removeEventListener("keydown", onKey); };
}
