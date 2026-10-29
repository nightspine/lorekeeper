// The Library: every book from uploads and Book Orbit, findable fast. A sticky toolbar holds search
// ("/" focuses it), sort, filters and the view; results come in letter sections with an A–Z rail, so a
// 500-book library is a jump away instead of a long scroll. Books are opened in the Reader from here.
import { api } from "./api.js";
import { parseHash, setParams } from "./app.js";
import { bookNo, buildDialog, cover, numWord, startBuild, STATUS, statusChip } from "./build.js";
import { clear, fmtInt, h, icon, relTime, toast } from "./dom.js";

const SORTS = [["title", "Title"], ["author", "Author"], ["series", "Series"], ["recent", "Recently indexed"]];
const SHOWS = [["all", "All books"], ["ready", "Ready to read"], ["missing", "No index"], ["building", "Building"]];
const NO_EPUB = "Book Orbit has no EPUB for this book, so there's no text to index.";
const LETTERS = [..."ABCDEFGHIJKLMNOPQRSTUVWXYZ", "#"];
const ARTICLE = /^(the|a|an)\s+/i;

const fold = (x) => (x || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const letterOf = (key) => { const c = fold(key).charAt(0).toUpperCase(); return c >= "A" && c <= "Z" ? c : "#"; };
const surname = (name) => (name || "").trim().split(/\s+/).pop() || "";
const isReady = (b) => b.status === "complete" || b.status === "partial";
const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);
const plural = (n, one, many = `${one}s`) => `${fmtInt(n)} ${n === 1 ? one : many}`;
const readerHref = (b) => {
  const d = b.versions.find((v) => v.isDefault);
  return d ? `#/reader?i=${encodeURIComponent(d.key)}` : null;
};

export function mountLibrary(root) {
  const { params } = parseHash();
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("lk-library") || "{}"); } catch {}
  const s = {
    data: null, demo: undefined, q: params.get("q") || "", selected: params.get("book"),
    sort: params.get("sort") || saved.sort || "title", view: (params.get("view") || saved.view) === "grid" ? "grid" : "list",
    show: params.get("show") || "all", source: params.get("source") || "all",
  };
  const side = h("aside.lib-side", { "aria-label": "Filters" });
  const bar = h("div.lib-bar");
  const body = h("div.lib-body");
  const rail = h("nav.az-rail", { "aria-label": "Jump to letter" });
  const panel = h("aside.lib-panel", { "aria-label": "Book details", hidden: true });
  root.append(h("div.library", side, h("section.lib-main", bar, h("div.lib-content", h("div.lib-list", body), rail, panel))));
  let timer, observer, searchTimer;

  async function load() {
    try { s.data = await api.catalog(); } catch (e) { return toast(`Could not load the library: ${e.message}`, "error"); }
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
    author: `${surname(b.authors[0])} ${b.authors[0] || ""}`,
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

  /** [{ letter, label, books }] in display order; "recent" is one ungrouped section. */
  function sections(list) {
    if (s.sort === "recent") {
      const updated = (b) => Math.max(0, ...b.versions.map((v) => Date.parse(v.updated) || 0));
      const done = list.filter((b) => b.versions.length).sort((a, b) => updated(b) - updated(a));
      const rest = list.filter((b) => !b.versions.length).sort((a, b) => fold(a.title).localeCompare(fold(b.title)));
      return [{ letter: null, label: "Indexed, newest first", books: done }, { letter: null, label: "Not indexed yet", books: rest }]
        .filter((x) => x.books.length);
    }
    const sorted = [...list].sort((a, b) => fold(sortKey(a)).localeCompare(fold(sortKey(b))));
    const out = [];
    for (const b of sorted) {
      const key = s.sort === "series" && !b.series ? "~" : sortKey(b);
      const letter = key === "~" ? "Standalone" : letterOf(key);
      if (!out.length || out[out.length - 1].letter !== letter) out.push({ letter, label: letter, books: [] });
      out[out.length - 1].books.push(b);
    }
    // Numbers and symbols after Z, like the rail; standalone books after every series.
    const rank = (x) => (x.letter === "Standalone" ? 2 : x.letter === "#" ? 1 : 0);
    return out.sort((a, b) => rank(a) - rank(b));
  }

  // ---- paint -------------------------------------------------------------------------------

  function paint() {
    paintSide();
    paintBar();
    paintBody();
    paintPanel();
    persist();
  }

  function paintSide() {
    const books = s.data.books;
    const n = (f) => books.filter(f).length;
    const row = (active, label, count, onClick) => h("button.filter-row", { type: "button", role: "radio", "aria-checked": String(active),
      tabindex: active ? "0" : "-1", on: { click: onClick } }, h("span", label), h("span.num", fmtInt(count)));
    // One exclusive choice: a radio group with a roving tabindex; arrows move and select, then focus follows the repaint.
    const radios = (id, title, rows) => {
      const group = h("div.side-group", { role: "radiogroup", "aria-labelledby": `side-${id}`, dataset: { group: id } },
        h("span.eyebrow", { id: `side-${id}` }, title), rows);
      group.addEventListener("keydown", (e) => {
        const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
        if (!step) return;
        const all = [...group.querySelectorAll("[role=radio]")];
        const at = all.indexOf(document.activeElement);
        if (at < 0) return;
        e.preventDefault();
        all[(at + step + all.length) % all.length].click();
        side.querySelector(`[data-group="${id}"] [aria-checked="true"]`)?.focus();
      });
      return group;
    };
    const bo = s.data.bookorbit;
    const showCount = { all: books.length, ready: n(isReady), missing: n((b) => b.status === "missing"), building: n((b) => b.status === "building") };
    clear(side,
      radios("show", "Show", SHOWS.map(([k, label]) => row(s.show === k, label, showCount[k], () => { s.show = k; paint(); }))),
      radios("source", "From", [
        row(s.source === "all", "Everywhere", books.length, () => { s.source = "all"; paint(); }),
        row(s.source === "bookorbit", "Book Orbit", n((b) => b.source === "bookorbit"), () => { s.source = "bookorbit"; paint(); }),
        row(s.source === "upload", "Added here", n((b) => b.source === "upload"), () => { s.source = "upload"; paint(); })]),
      h("div.side-group",
        bo.connected
          ? h("p.side-note", `Synced ${relTime(bo.synced)}. `, h("a", { href: "#/settings" }, "Settings"))
          : h("a.side-cta", { href: "#/settings" }, icon("link"), "Connect Book Orbit")),
      h("div.side-group", h("span.eyebrow", "Series"),
        h("button.filter-row", { type: "button", on: { click: () => { s.sort = "series"; s.q = ""; paint(); } } },
          h("span", "Browse by series"), h("span.num", fmtInt(s.data.series.length)))));
  }

  function paintBar() {
    const search = h("input.input.lib-search", { type: "search", placeholder: "Search titles, authors, series", value: s.q,
      "aria-label": "Search the library", "aria-keyshortcuts": "/" });
    search.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { s.q = search.value; paintBody(); persist(); }, 120);
    });
    search.addEventListener("keydown", (e) => { if (e.key === "Escape" && search.value) { search.value = ""; s.q = ""; paintBody(); persist(); } });
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
    const sort = h("select.select", { id: "lib-sort" }, SORTS.map(([k, label]) => h("option", { value: k, selected: s.sort === k }, label)));
    sort.addEventListener("change", () => { s.sort = sort.value; paint(); window.scrollTo({ top: 0 }); });
    clear(bar,
      h("div.lib-title", h("h1", "Library"), h("p.lib-count")),
      h("div.lib-tools",
        h("div.search.lib-search-wrap", icon("search"), search, h("kbd.kbd", { "aria-hidden": "true" }, "/")),
        h("label.inline-label", h("span", "Sort"), sort),
        h("div.seg", { role: "group", "aria-label": "View" }, [["list", "List"], ["grid", "Covers"]].map(([v, label]) =>
          h("button", { type: "button", "aria-pressed": String(s.view === v), on: { click: () => { s.view = v; paint(); } } }, label))),
        h("label.btn", { for: "lib-upload" }, "Add an EPUB"), upload));
  }

  /** The way in for a stranger: the finished example index opens in the Reader with no build. */
  function demoPanel(compact) {
    const d = s.demo;
    if (!d) return null;
    const entry = { title: d.title, authors: d.authors || [] };
    const href = `#/reader?i=${encodeURIComponent(d.key)}`;
    if (compact) {
      return h("section.demo-panel.compact", { "aria-label": "Try the companion" },
        cover(entry, "sm"),
        h("p.demo-line", `While your first index builds, try the companion on ${d.title}`),
        h("a.btn.primary.small", { href }, "Try it"));
    }
    return h("section.demo-first", { "aria-labelledby": "demo-head" },
      h("div.demo-panel",
        cover(entry, "lg"),
        h("div.demo-text",
          h("h2", { id: "demo-head" }, `Read ${d.title} with a companion that never spoils`),
          h("p", "Drag the position slider and watch the cast, places and events appear only as the book reveals them."),
          h("a.btn.primary.lg", { href }, "Try it now"))),
      h("p.demo-then", "Then bring your own books: ", h("label.demo-link", { for: "lib-upload" }, "add an EPUB"),
        ", or ", h("a", { href: "#/settings" }, "connect Book Orbit"), "."));
  }

  function readyStrip() {
    if (s.q || s.show !== "all" || s.sort === "recent") return null;
    const ready = s.data.books.filter(isReady);
    if (!ready.length) return demoPanel(true);
    return h("section.ready-strip", { "aria-label": "Ready to read" },
      h("div.ready-head", h("h2", "Ready to read"), h("span.hint", `${plural(ready.length, "book")} with an index`),
        ready.length > 6 ? h("button.linkish", { type: "button", on: { click: () => { s.show = "ready"; paint(); } } }, "Show all") : null),
      h("div.ready-row", ready.slice(0, 12).map((b) => h("a.ready-book", { href: readerHref(b), title: `Read ${b.title}`, "aria-label": `Read ${b.title}` },
        cover(b, "sm"), h("span.ready-title", b.title)))));
  }

  /** "Joe Abercrombie, book two"; outside a series block the series name joins: "Joe Abercrombie, The Age of Madness, book two". */
  function subLine(b, inSeries) {
    return [b.authors.join(", ") || "Unknown author", b.series && !inSeries ? b.series.name : null, bookNo(b.series) || null].filter(Boolean).join(", ");
  }

  function bookRow(b, inSeries) {
    const href = readerHref(b);
    const warn = warnFor(b);
    const select = () => { s.selected = b.key; paintPanel(); paintSelection(); persist(); };
    return h(`li.book-row${s.selected === b.key ? ".selected" : ""}`, { dataset: { key: b.key } },
      h("button.book-row-main", { type: "button", "aria-label": `${b.title}, ${STATUS[b.status].label}. Show details`, on: { click: select } },
        cover(b, "xs"),
        h("span.book-row-text", h("span.book-row-title", b.title),
          h("span.book-row-sub", subLine(b, inSeries), warn ? [". ", h("span.row-warn", `${warn}.`)] : null)),
        statusChip(b)),
      h("span.book-row-act",
        href ? h("a.btn.small.primary", { href, "aria-label": `Read ${b.title}` }, "Read")
          : b.status === "building" ? h("a.btn.small", { href: `#/studio/job/${b.job.id}` }, "View progress")
            : h("button.btn.small", { type: "button", "aria-disabled": b.canBuild ? null : "true",
              on: { click: () => (b.canBuild ? startBuild(b) : toast(NO_EPUB, "error")) } }, "Build")));
  }

  function bookTile(b, inSeries) {
    return h(`button.book-card${s.selected === b.key ? ".selected" : ""}`, { type: "button", "aria-label": `${b.title}, ${STATUS[b.status].label}`,
      on: { click: () => { s.selected = b.key; paintPanel(); paintSelection(); persist(); } }, dataset: { key: b.key } },
      h("span.cover-wrap", cover(b)),
      h("span.card-row", statusChip(b)),
      h("span.card-meta", inSeries ? bookNo(b.series) || b.authors[0] || "" : b.series ? `${b.series.name}, ${bookNo(b.series) || "in the series"}` : b.authors[0] || ""));
  }

  function seriesHead(g, books) {
    const missing = books.filter((b) => b.status === "missing" && b.canBuild).length;
    const indexed = books.filter(isReady).length;
    const count = plural(books.length, "book") + (indexed === 0 ? (books.length === 1 ? ", not indexed" : ", none indexed")
      : indexed === books.length ? (books.length === 1 ? ", indexed" : ", all indexed") : `, ${numWord(indexed)} indexed`);
    return h("div.series-head",
      h("a.series-name", { href: `#/series/${encodeURIComponent(g.id)}` }, g.name),
      h("span.series-count", count),
      missing && books.length > 1 ? h("button.linkish", { type: "button", on: { click: () => buildMissing(g) } },
        missing === books.length && missing > 1 ? `Build all ${numWord(missing)} in order` : `Build ${missing === 1 ? "the missing book" : `${numWord(missing)} missing`} in order`) : null);
  }

  function paintBody() {
    if (observer) observer.disconnect();
    const total = s.data.books.length;
    if (!total) {
      clear(rail);
      const first = demoPanel(false);
      if (first) return clear(body, first);
      return clear(body, h("div.card.empty", h("div.art", icon("book-open")), h("h3", "Your library is empty"),
        h("p", "Connect Book Orbit to see all your books, or add an EPUB."),
        h("div.toolbar", { style: { justifyContent: "center" } },
          h("a.btn.primary", { href: "#/settings" }, icon("link"), "Connect Book Orbit"),
          h("label.btn", { for: "lib-upload" }, "Add an EPUB"))));
    }
    const list = matches();
    const count = bar.querySelector(".lib-count");
    if (count) {
      const ready = s.data.books.filter(isReady).length;
      count.textContent = list.length === total
        ? `${plural(total, "book")} in ${plural(s.data.series.length, "series", "series")}.${ready ? ` ${fmtInt(ready)} ready to read.` : ""}`
        : `${fmtInt(list.length)} of ${plural(total, "book")}.`;
    }
    if (!list.length) {
      clear(rail);
      return clear(body, h("div.empty", h("div.art", icon("search")), h("h3", s.q ? `Nothing matches “${s.q}”` : "No books here"),
        h("p", s.q ? "Search looks in titles, authors and series names. Check the spelling, or try just a surname." : "Try another filter."),
        h("div.toolbar", { style: { justifyContent: "center" } },
          s.q ? h("button.btn", { type: "button", on: { click: () => { s.q = ""; paint(); } } }, "Clear search") : null,
          s.show !== "all" || s.source !== "all" ? h("button.btn", { type: "button", on: { click: () => { s.show = "all"; s.source = "all"; paint(); } } }, "Show all books") : null)));
    }
    const secs = sections(list);
    const byKey = Object.fromEntries(s.data.books.map((b) => [b.key, b]));
    const render = (books) => {
      if (s.sort === "series") {
        // Within a letter: one block per series, books in reading order.
        const out = [];
        for (const b of books) {
          const last = out[out.length - 1];
          if (b.series && last && last.series === b.series.id) last.books.push(b);
          else out.push({ series: b.series?.id || null, books: [b] });
        }
        return out.map((grp) => {
          const g = grp.series && s.data.series.find((x) => x.id === grp.series);
          return h("div.series-block", g ? seriesHead(g, g.books.map((k) => byKey[k]).filter((b) => grp.books.includes(b))) : null,
            s.view === "grid" ? h("div.shelf", grp.books.map((b) => bookTile(b, Boolean(g)))) : h("ul.book-rows", grp.books.map((b) => bookRow(b, Boolean(g)))));
        });
      }
      return s.view === "grid" ? h("div.shelf", books.map((b) => bookTile(b))) : h("ul.book-rows", books.map((b) => bookRow(b)));
    };
    clear(body, readyStrip(),
      secs.map((sec) => h("section.az-section", { id: sec.letter && sec.letter.length === 1 ? `az-${sec.letter === "#" ? "num" : sec.letter}` : null,
        "aria-label": sec.label },
        h("h2.az-head", sec.label, h("span.n", sectionCount(sec))), render(sec.books))));
    paintRail(secs);
  }

  /** "6 series" for a letter in series order, else "6 books". */
  function sectionCount(sec) {
    if (s.sort !== "series") return plural(sec.books.length, "book");
    const n = new Set(sec.books.map((b) => b.series?.id || b.key)).size;
    return sec.letter === "Standalone" ? plural(n, "book") : plural(n, "series", "series");
  }

  function paintRail(secs) {
    const present = new Set(secs.map((x) => x.letter));
    if (s.sort === "recent" || present.size < 2) { clear(rail); rail.hidden = true; return; }
    rail.hidden = false;
    clear(rail, LETTERS.map((L) => {
      const on = present.has(L);
      return h("button.az-letter", { type: "button", disabled: !on, "aria-label": on ? `Jump to ${L === "#" ? "numbers and symbols" : L}` : `${L}: none`,
        dataset: { letter: L }, on: { click: () => {
          const el = document.getElementById(`az-${L === "#" ? "num" : L}`);
          if (el) { el.scrollIntoView({ block: "start" }); el.querySelector("button, a")?.focus({ preventScroll: true }); }
        } } }, L);
    }));
    // Highlight the letter being read.
    observer = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const L = e.target.id.replace("az-", "").replace("num", "#");
        rail.querySelectorAll(".az-letter").forEach((b) => b.classList.toggle("here", b.dataset.letter === L));
      }
    }, { rootMargin: "-140px 0px -70% 0px" });
    body.querySelectorAll(".az-section[id]").forEach((el) => observer.observe(el));
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

  function paintSelection() {
    root.querySelectorAll("[data-key]").forEach((el) => el.classList.toggle("selected", el.dataset.key === s.selected));
  }

  function paintPanel() {
    const b = s.data.books.find((x) => x.key === s.selected);
    panel.hidden = !b;
    root.querySelector(".lib-content").classList.toggle("with-panel", Boolean(b));
    if (!b) return;
    const series = b.series ? s.data.series.find((g) => g.id === b.series.id) : null;
    const byKey = Object.fromEntries(s.data.books.map((x) => [x.key, x]));
    const d = b.versions.find((v) => v.isDefault);
    const warn = warnFor(b);
    const href = readerHref(b);
    const build = b.status === "building" ? h("a.btn", { href: `#/studio/job/${b.job.id}` }, icon("eye"), "View progress")
      : h(`button.btn${href ? "" : ".primary"}`, { type: "button", "aria-disabled": b.canBuild ? null : "true",
        on: { click: () => (b.canBuild ? startBuild(b) : toast(NO_EPUB, "error")) } }, icon("play"),
        b.status === "missing" ? "Build index" : "Rebuild index");
    clear(panel,
      h("div.panel-close", h("button.icon-btn.small", { type: "button", "aria-label": "Close details",
        on: { click: () => { s.selected = null; paintPanel(); paintSelection(); persist(); } } }, icon("x"))),
      h("div.panel-top", cover(b, "md"),
        h("div", h("span.eyebrow", b.series ? [b.series.name, bookNo(b.series) ? `, ${bookNo(b.series)}` : ""].join("") : "Standalone"),
          h("h2", b.title), h("p.by", b.authors.join(", ") || "Unknown author"),
          h("div.facts", statusChip(b), h("span.chip", b.source === "bookorbit" ? "Book Orbit" : "Uploaded"),
            b.words ? h("span.chip", `${fmtInt(b.words)} words`) : null))),
      h("div.panel-actions",
        href ? h("a.btn.primary", { href, "aria-label": `Read ${b.title}` }, icon("book-open"), "Read") : null,
        build,
        h("a.btn.ghost", { href: `#/book/${encodeURIComponent(b.key)}` }, "Versions and runs")),
      warn ? h("div.notice.warn", icon("alert"), h("div", `${warn}. `, /library/.test(warn)
        ? ["Add it (", h("a", { href: "#/settings" }, "connect Book Orbit"), " or add the EPUB) and build it first so characters and relationships carry over, or build this book on its own."]
        : "Build it first so characters and relationships carry over, or build this book on its own.")) : null,
      !b.canBuild ? h("div.notice.info", icon("info"), h("div", "Book Orbit has no EPUB for this book, so there's no text to index.")) : null,
      d ? h("p.hint", `Index built with ${d.model}.${d.coverage != null ? ` Covers ${d.complete ? "the whole book" : `${d.coverage}% of it`}.` : ""}`) : null,
      series ? h("div.panel-series", h("span.eyebrow", "In this series"),
        series.books.map((k) => byKey[k]).map((x) => h(`a.series-row${x.key === b.key ? ".here" : ""}`, { href: `#/book/${encodeURIComponent(x.key)}` },
          h("span.subtle.num", `${x.series.index ?? "?"}.`), h("span", x.title), statusChip(x)))) : null);
  }

  // "/" anywhere on the page focuses search (unless typing somewhere else).
  const onKey = (e) => {
    if (e.key !== "/" || e.metaKey || e.ctrlKey || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
    e.preventDefault();
    bar.querySelector(".lib-search")?.focus();
  };
  document.addEventListener("keydown", onKey);

  load();
  return () => { clearTimeout(timer); observer?.disconnect(); document.removeEventListener("keydown", onKey); };
}
