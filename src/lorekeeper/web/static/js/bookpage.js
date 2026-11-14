// The book page and the series page: a breadcrumb, a compact head, label / value rows on hairlines, and tables.
import { api } from "./api.js";
import { parseHash } from "./app.js";
import { bookNo, buildDialog, builtWith, cover, NO_EPUB, numWord, startBuild, statusChip } from "./build.js";
import { clear, crumbs, fmtInt, h, icon, loadError, loading, STATUS_LABEL, toast } from "./dom.js";
import { rowAction } from "./library.js";

const when = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "");
const KIND_LABEL = { fiction: "Fiction", "non-fiction": "Non-fiction", "self-help": "Self-help" };
const row = (label, value, action = null) => h("div.set-row", h("span.set-lbl", label), h("div.set-val", value), action ? h("div.set-act", action) : null);

export function mountBook(root) {
  const key = decodeURIComponent(parseHash().parts[0] || "");
  const wrap = h("div.page.book-page");
  root.append(wrap);
  let timer, painted = false, building = false, disposed = false, sig = "";
  clear(wrap, loading("Opening the book…"));

  async function load() {
    let b;
    try { b = await api.book(key); } catch (e) {
      if (disposed) return;
      if (painted) {   // a failed background reload keeps what is shown, silently, and tries again while it is building
        if (building) { clearTimeout(timer); timer = setTimeout(load, 5000); }
        return;
      }
      if (e.status === 404) return clear(wrap, h("div.empty", h("h3", "Book not found"), h("a.btn", { href: "#/library" }, "Back to the library")));
      return clear(wrap, loadError("open the book", e, () => { clear(wrap, loading("Opening the book…")); load(); }));
    }
    if (disposed) return;
    painted = true; building = b.status === "building";
    // The 5 s reload while building repaints only when something changed, and only if no field is being edited.
    const next = JSON.stringify(b);
    if (next !== sig && !wrap.querySelector(".series-form")?.contains(document.activeElement)) { sig = next; paint(b); }
    clearTimeout(timer);
    if (b.status === "building") timer = setTimeout(load, 5000);
  }

  function paint(b) {
    const d = b.versions.find((v) => v.isDefault);
    const missing = b.preflight?.missingEarlier || [];
    const absent = b.preflight?.notInLibrary || [];
    const through = d && d.throughSeq != null ? d.throughSeq : -1;
    const chapters = b.chapters || [];
    const story = chapters.filter((c) => c.story !== false);
    const kind = d?.bookKind;

    const seriesForm = b.source === "upload" ? (() => {
      const name = h("input.input", { id: "s-name", value: b.series?.name || "", placeholder: "Series name, e.g. Sherlock Holmes", "aria-label": "Series name" });
      const index = h("input.input.w-90", { id: "s-index", value: b.series?.index || "", placeholder: "No.", inputmode: "decimal", "aria-label": "Number in the series" });
      return h("form.series-form", { on: { submit: async (e) => {
        e.preventDefault();
        try { await api.setSeries(b.key, name.value, index.value); toast(name.value ? "Series saved" : "Removed from series"); sig = ""; load(); }
        catch (ex) { toast(ex.message, "error"); }
      } } }, name, index, h("button.btn", { type: "submit" }, "Save"));
    })() : null;

    const seriesValue = b.seriesInfo
      ? h("ul.series-books", b.seriesInfo.books.map((x) => h(`li${x.key === b.key ? ".here" : ""}`,
        h("span.subtle.num", `${x.series.index ?? "?"}.`),
        h("a", { href: `#/book/${encodeURIComponent(x.key)}`, "aria-current": x.key === b.key ? "page" : null }, x.title),
        statusChip(x))))
      : null;

    const main = b.status === "building"
      ? h("a.btn", { href: `#/studio/job/${b.job.id}` }, "View progress")
      : h(`button.btn${d ? "" : ".primary"}`, { type: "button", disabled: !b.canBuild, on: { click: () => startBuild(b) } },
        b.versions.length ? "Rebuild index" : "Build index");

    clear(wrap,
      crumbs([{ label: "Library", href: "#/library" },
        ...(b.seriesInfo ? [{ label: b.seriesInfo.name, href: `#/series/${encodeURIComponent(b.seriesInfo.id)}` }] : []),
        { label: b.title }]),
      h("header.bk-head",
        cover(b, "sm"),
        h("div.bk-head-main",
          h("h1", b.title),
          h("p.by", `${b.authors.join(", ") || "Unknown author"}.${b.series ? ` ${b.series.name}${bookNo(b.series) ? `, ${bookNo(b.series)}` : ""}.` : ""}`),
          h("div.btns",
            d ? h("a.btn.primary", { href: `#/reader?i=${encodeURIComponent(d.key)}` }, "Read") : null,
            main,
            d ? h("a.btn.quiet", { href: `/api/library/${encodeURIComponent(d.key)}`, download: "" }, icon("download"), "Download index") : null))),
      missing.length ? h("div.notice.warn", h("div",
        `${missing.length === 1 ? `Book ${numWord(missing[0].index)}, ${missing[0].title},` : `${missing.length} earlier books`} ${missing.length === 1 ? "has" : "have"} no index yet. `,
        "Build ", missing.length === 1 ? "it" : "them", " first so this book can build on them.")) : null,
      absent.length ? h("div.notice.warn", h("div",
        `Book${absent.length === 1 ? "" : "s"} ${absent.map(numWord).join(", ")} of ${b.series.name} ${absent.length === 1 ? "isn't" : "aren't"} in your library, so this book can't build on ${absent.length === 1 ? "it" : "them"}. `,
        h("a", { href: "#/settings/bookorbit" }, "Connect Book Orbit"), " or add the EPUB.")) : null,
      !b.canBuild ? h("div.notice.info", h("div", NO_EPUB)) : null,

      h("div.set-rows",
        row("Index", h("span", statusChip(b), d?.model ? h("span.subtle", ` ${builtWith(d.model)[0].toUpperCase()}${builtWith(d.model).slice(1)}.`) : null)),
        story.length ? row("Coverage", h("div.coverage-wrap",
          h("div.coverage-strip", { role: "img", "aria-label": d ? (d.complete ? "The whole story is covered" : `Covered up to ${chapters.filter((c) => c.story !== false && c.start <= through).length} of ${story.length} chapters`) : "Nothing covered yet" },
            chapters.filter((c) => c.words > 0).map((c) => {
              if (c.story === false) return h("span.cov.extra", { title: `${c.title}: not part of the story` });
              const covered = d && (d.complete || c.start <= through);
              return h(`span.cov${covered ? ".on" : ""}`, { title: `${c.title}${covered ? ": covered" : ": not yet"}` });
            })),
          h("div.coverage-ends", h("span", story[0].title), h("span", story[story.length - 1].title)))) : null,
        row("Book", [b.words ? `${fmtInt(b.words)} words` : null, story.length ? `${fmtInt(story.length)} chapters` : null].filter(Boolean).join(", ") || "Unknown length"),
        kind ? row("Kind", KIND_LABEL[kind] || kind) : null,
        row("From", b.source === "bookorbit" ? "Book Orbit" : "Uploaded here"),
        row(b.seriesInfo ? "In this series" : "Series", seriesValue || seriesForm || "Standalone"),
        b.seriesInfo && seriesForm ? row("Change series", seriesForm) : null),

      h("section.page-sec", { "aria-labelledby": "versions-h" },
        h("h2#versions-h", "Versions"),
        b.versions.length ? h("div.table-wrap", h("table.data.versions",
          h("thead", h("tr", ["Version", "Covers", "Entries", "Details", "Updated"].map((t) => h("th", { scope: "col" }, t)),
            h("th", { scope: "col" }, h("span.sr-only", "Actions")))),
          h("tbody", b.versions.map((v) => h(`tr${v.isDefault ? ".selected" : ""}`,
            h("td", h("b", v.model || "Unknown"), h("span.subtle", v.isDefault ? ", used for reading" : v.source === "generated" ? ", built here" : `, from ${v.source}`)),
            h("td", v.complete ? "Whole book" : v.coverage != null ? `${v.coverage}%` : "Partial"),
            h("td.num", fmtInt(v.entities)), h("td.num", fmtInt(v.facts)), h("td", when(v.updated)),
            h("td.cell-actions",
              h("a", { href: `#/reader?i=${encodeURIComponent(v.key)}` }, "Read"),
              v.isDefault ? null : h("button.linkish", { type: "button", on: { click: async () => {
                try { await api.setDefault(b.key, v.key); toast("Default version changed"); sig = ""; load(); } catch (e) { toast(e.message, "error"); }
              } } }, "Make default"))))))) : h("p.muted", "No index yet."),
        b.versions.length > 1 ? h("p.hint", "The default version is what the Reader and connected apps receive.") : null),

      b.runs.length ? h("section.page-sec", { "aria-labelledby": "runs-h" },
        h("h2#runs-h", "Runs"),
        h("div.table-wrap", h("table.data",
          h("thead", h("tr", h("th", { scope: "col" }, "State"), h("th", { scope: "col" }, "Model"), h("th", { scope: "col" }, "Started"))),
          h("tbody", b.runs.map((j) => h("tr",
            h("td", h(`span.badge.${j.status}`, STATUS_LABEL[j.status] || j.status)),
            h("td", h("a", { href: `#/studio/job/${j.id}` }, j.config.model || "Default model")),
            h("td.subtle", new Date(j.created * 1000).toLocaleString()))))))) : null);
  }

  load();
  return () => { disposed = true; clearTimeout(timer); };
}

export function mountSeries(root) {
  const id = decodeURIComponent(parseHash().parts[0] || "");
  const wrap = h("div.page.series-page");
  root.append(wrap);
  let timer, painted = false, building = false, disposed = false, sig = "";
  clear(wrap, loading("Loading the series…"));

  async function load() {
    let data;
    try { data = await api.catalog(); } catch (e) {
      if (disposed) return;
      if (painted) {   // a failed background reload keeps what is shown, silently, and tries again while it is building
        if (building) { clearTimeout(timer); timer = setTimeout(load, 5000); }
        return;
      }
      return clear(wrap, loadError("load the series", e, () => { clear(wrap, loading("Loading the series…")); load(); }));
    }
    if (disposed) return;
    painted = true;
    const g = data.series.find((x) => x.id === id);
    if (!g) return clear(wrap, h("div.empty", h("h3", "Series not found"), h("a.btn", { href: "#/library" }, "Back to the library")));
    const byKey = Object.fromEntries(data.books.map((b) => [b.key, b]));
    const books = g.books.map((k) => byKey[k]).filter(Boolean);
    clearTimeout(timer);
    building = books.some((b) => b.status === "building");
    if (building) timer = setTimeout(load, 5000);
    const next = JSON.stringify([g, books]);
    if (next === sig) return;
    sig = next;
    paint(g, books);
  }

  function paint(g, books) {
    const firstGap = books.findIndex((b) => b.status === "missing");
    // Books after the first gap were built (or would be built) without everything before them.
    const todo = books.filter((b, i) => b.canBuild && (b.status === "missing" || (firstGap >= 0 && i > firstGap && b.status !== "building")));
    const indexed = books.filter((b) => b.status === "complete" || b.status === "partial").length;
    const fromBo = books.some((b) => b.source === "bookorbit");
    const focusKey = document.activeElement?.closest?.("[data-key]")?.dataset.key;

    clear(wrap,
      crumbs([{ label: "Library", href: "#/library" }, { label: g.name }]),
      h("header.bk-head",
        h("div.bk-head-main",
          h("h1", g.name),
          h("p.by", `${numWord(books.length)[0].toUpperCase()}${numWord(books.length).slice(1)} book${books.length === 1 ? "" : "s"}${g.authors.length ? ` by ${g.authors.join(", ")}` : ""}, ${indexed === books.length ? (books.length === 1 ? "indexed" : "all indexed") : indexed ? `${numWord(indexed)} indexed` : "none indexed"}. Order ${fromBo ? "from Book Orbit" : "set here"}.`))),
      todo.length ? h("div.notice.warn.series-cta",
        h("div", `Each book's index builds on the ones before it. Finishing the series in order queues ${todo.map((b) => b.title).join(", then ")}.`),
        h("button.btn.primary", { type: "button", on: { click: () => buildDialog(todo.map((b) => ({ key: b.key, title: b.title, words: b.words })),
          { title: `Build ${g.name} in order` }) } }, todo.length === 1 ? "Build 1 book" : `Build ${todo.length} in order`)) : null,
      h("div.table-wrap", h("table.data.books",
        h("caption.sr-only", `${g.name}, in reading order`),
        h("thead", h("tr", h("th.c-num", { scope: "col" }, "Book"), h("th.c-title", { scope: "col" }, "Title"),
          h("th.c-status", { scope: "col" }, "Index"), h("th.c-act", { scope: "col" }, h("span.sr-only", "Action")))),
        h("tbody", books.map((b, i) => h("tr.book", { dataset: { key: b.key } },
          h("td.c-num.subtle.num", String(b.series?.index ?? i + 1)),
          h("td.c-title", h("span.t", cover(b, "xs"), h("span.t-text",
            h("a.t-open", { href: `#/book/${encodeURIComponent(b.key)}`, dataset: { role: "open" } }, b.title),
            h("span.t-sub", b.status === "building" ? `Building with ${b.job?.model || "the default model"}`
              : b.status === "missing" ? (b.canBuild ? (b.authors.join(", ") || "No index yet") : "No EPUB to index")
                : [(() => { const m = builtWith(b.versions.find((v) => v.isDefault)?.model); return m ? m[0].toUpperCase() + m.slice(1) : b.authors.join(", "); })(),
                  firstGap >= 0 && i > firstGap ? ". Built without an earlier book" : ""].join(""))))),
          h("td.c-status", statusChip(b)),
          h("td.c-act", (() => { const a = rowAction(b); a.removeAttribute("tabindex"); return a; })())))))));
    if (focusKey) wrap.querySelector(`[data-key="${CSS.escape(focusKey)}"] a, [data-key="${CSS.escape(focusKey)}"] button`)?.focus({ preventScroll: true });
  }
  load();
  return () => { disposed = true; clearTimeout(timer); };
}
