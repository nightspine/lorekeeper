import { api } from "./api.js";
import { parseHash } from "./app.js";
import { bookNo, buildDialog, cover, numWord, startBuild, STATUS, statusChip } from "./build.js";
import { clear, fmtInt, h, icon, STATUS_LABEL, toast } from "./dom.js";

const when = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "—");

const sep = () => h("span.sep", { "aria-hidden": "true" }, "/");

export function mountBook(root) {
  const key = decodeURIComponent(parseHash().parts[0] || "");
  const wrap = h("div.page");
  root.append(wrap);
  let timer;

  async function load() {
    let b;
    try { b = await api.book(key); } catch (e) {
      return clear(wrap, h("div.card.empty", h("h3", "Book not found"), h("a.btn", { href: "#/library" }, "Back to the library")));
    }
    paint(b);
    clearTimeout(timer);
    if (b.status === "building") timer = setTimeout(load, 5000);
  }

  function paint(b) {
    const d = b.versions.find((v) => v.isDefault);
    const missing = b.preflight?.missingEarlier || [];
    const through = d && d.throughSeq != null ? d.throughSeq : -1;
    const chapters = b.chapters || [];
    const story = chapters.filter((c) => c.story !== false);

    const seriesForm = b.source === "upload" ? (() => {
      const name = h("input.input", { id: "s-name", value: b.series?.name || "", placeholder: "e.g. Sherlock Holmes" });
      const index = h("input.input", { id: "s-index", value: b.series?.index || "", placeholder: "1", inputmode: "decimal", class: "w-90" });
      const save = h("button.btn", { type: "submit" }, "Save");
      return h("form.series-form", { on: { submit: async (e) => {
        e.preventDefault();
        try { await api.setSeries(b.key, name.value, index.value); toast(name.value ? "Series saved" : "Removed from series"); load(); }
        catch (ex) { toast(ex.message, "error"); }
      } } },
      h("div.field", h("label", { for: "s-name" }, "Series"), name),
      h("div.field", h("label", { for: "s-index" }, "Number"), index), save);
    })() : null;

    clear(wrap,
      h("nav.crumbs", { "aria-label": "Breadcrumb" }, h("a", { href: "#/library" }, "Library"),
        b.seriesInfo ? [sep(), h("a", { href: `#/series/${encodeURIComponent(b.seriesInfo.id)}` }, b.seriesInfo.name)] : null, sep(), h("span", { "aria-current": "page" }, b.title)),
      h("div.book-hero",
        cover(b, "lg"),
        h("div.book-hero-main",
          h("span.eyebrow", b.series ? `${b.series.name}${bookNo(b.series) ? `, ${bookNo(b.series)}` : ""}` : "Standalone book"),
          h("h1", b.title),
          h("p.by", [`${b.authors.join(", ") || "Unknown author"}.`,
            b.words || story.length ? ` ${[b.words ? `${fmtInt(b.words)} words` : null, story.length ? `${fmtInt(story.length)} chapters` : null].filter(Boolean).join(", ")}.` : ""].join("")),
          h("div.facts", statusChip(b), h("span.chip", b.source === "bookorbit" ? "From Book Orbit" : "Uploaded here"),
            h("span.chip", `${b.versions.length} version${b.versions.length === 1 ? "" : "s"}`)),
          missing.length ? h("div.notice.warn", icon("alert"), h("div",
            `${missing.length === 1 ? `Book ${numWord(missing[0].index)}, ${missing[0].title},` : `${missing.length} earlier books`} ${missing.length === 1 ? "has" : "have"} no index yet. `,
            "Build ", missing.length === 1 ? "it" : "them", " first so this book can build on them.")) : null,
          (b.preflight?.notInLibrary || []).length ? h("div.notice.warn", icon("alert"), h("div",
            `Book${b.preflight.notInLibrary.length === 1 ? "" : "s"} ${b.preflight.notInLibrary.map(numWord).join(", ")} of ${b.series.name} ${b.preflight.notInLibrary.length === 1 ? "isn't" : "aren't"} in your library, so this book can't build on ${b.preflight.notInLibrary.length === 1 ? "it" : "them"}. `,
            h("a", { href: "#/settings" }, "Connect Book Orbit"), " or add the EPUB.")) : null,
          h("div.toolbar",
            d ? h("a.btn.primary", { href: `#/reader?i=${encodeURIComponent(d.key)}` }, icon("book-open"), "Read") : null,
            b.status === "building" ? h("a.btn", { href: `#/studio/job/${b.job.id}` }, icon("eye"), "View progress")
              : h("button.btn", { type: "button", disabled: !b.canBuild, on: { click: () => startBuild(b) } }, icon("play"),
                b.versions.length ? "Rebuild index" : "Build index"),
            d ? h("a.btn.ghost", { href: `/api/library/${encodeURIComponent(d.key)}`, download: "" }, icon("download"), "Download index") : null),
          seriesForm),
        b.seriesInfo ? h("div.card.card-pad.series-mini", h("span.eyebrow", "In this series"),
          b.seriesInfo.books.map((x) => h(`a.series-row${x.key === b.key ? ".here" : ""}`, { href: `#/book/${encodeURIComponent(x.key)}` },
            h("span.subtle.num", `${x.series.index ?? "?"}.`), h("span", x.title), statusChip(x)))) : null),

      story.length ? h("section.card.card-pad", { "aria-label": "Coverage" },
        h("div.section-title", h("h2", "Coverage"), h("span.hint", d ? `Default version, built with ${d.model}.${d.complete ? " Covers the whole story." : ""}` : "No index yet.")),
        h("div.coverage-strip", chapters.filter((c) => c.words > 0).map((c) => {
          if (c.story === false) return h("span.cov.extra", { title: `${c.title}: not part of the story` });
          const covered = d && (d.complete || c.start <= through);
          return h(`span.cov${covered ? ".on" : ""}`, { title: `${c.title}${covered ? ": covered" : ": not yet"}` });
        })),
        h("div.coverage-ends", h("span", story[0].title), h("span", story[story.length - 1].title))) : null,

      h("section.card.card-pad", { "aria-label": "Versions" },
        h("div.section-title", h("h2", "Versions"), h("span.hint", "The default version is what the Reader and connected apps receive.")),
        b.versions.length ? h("div.table-wrap", h("table.data",
          h("thead", h("tr", ["Version", "Covers", "Entities", "Facts", "Updated", ""].map((t) => h("th", t)))),
          h("tbody", b.versions.map((v) => h(`tr${v.isDefault ? ".selected" : ""}`,
            h("td", h("b", v.model || "Unknown"), h("span.hint.block",
              v.isDefault ? "Default" : v.source === "generated" ? "Built here" : `From ${v.source}`)),
            h("td", v.complete ? "Whole book" : v.coverage != null ? `${v.coverage}%` : "Partial"),
            h("td.num", String(v.entities)), h("td.num", String(v.facts)), h("td", when(v.updated)),
            h("td.cell-actions",
              h("a", { href: `#/reader?i=${encodeURIComponent(v.key)}` }, "Read"),
              v.isDefault ? h("span.subtle", "Default") : h("button.linkish", { type: "button", on: { click: async () => {
                try { await api.setDefault(b.key, v.key); toast("Default version changed"); load(); } catch (e) { toast(e.message, "error"); }
              } } }, "Make default"))))))) : h("p.muted", "No index yet. Build one to read this book with a spoiler-free companion.")),

      b.runs.length ? h("section.card.card-pad", { "aria-label": "Runs" },
        h("div.section-title", h("h2", "Runs")),
        h("ul.run-list", b.runs.map((j) => h("li", h("a.run-row", { href: `#/studio/job/${j.id}` },
          h(`span.badge.${j.status}`, STATUS_LABEL[j.status] || j.status),
          h("span", (j.config.model || "default model")),
          h("span.hint", new Date(j.created * 1000).toLocaleString())))))) : null);
  }

  load();
  return () => clearTimeout(timer);
}

export function mountSeries(root) {
  const id = decodeURIComponent(parseHash().parts[0] || "");
  const wrap = h("div.page");
  root.append(wrap);
  let timer;

  async function load() {
    let data;
    try { data = await api.catalog(); } catch (e) { return toast(e.message, "error"); }
    const g = data.series.find((x) => x.id === id);
    if (!g) return clear(wrap, h("div.card.empty", h("h3", "Series not found"), h("a.btn", { href: "#/library" }, "Back to the library")));
    const byKey = Object.fromEntries(data.books.map((b) => [b.key, b]));
    const books = g.books.map((k) => byKey[k]);
    const firstGap = books.findIndex((b) => b.status === "missing");
    // Books after the first gap were built (or would be built) without everything before them.
    const todo = books.filter((b, i) => b.canBuild && (b.status === "missing" || (firstGap >= 0 && i > firstGap && b.status !== "building")));
    const sources = new Set(books.map((b) => b.source));

    clear(wrap,
      h("nav.crumbs", { "aria-label": "Breadcrumb" }, h("a", { href: "#/library" }, "Library"), sep(), h("span", { "aria-current": "page" }, "Series")),
      h("div.series-hero",
        h("div", h("span.eyebrow", `Series of ${numWord(books.length)} book${books.length === 1 ? "" : "s"}`), h("h1", g.name), g.authors.length ? h("p.by", `${g.authors.join(", ")}.`) : null),
        h("span.chip", sources.has("bookorbit") ? "Order from Book Orbit" : "Order set here")),
      h("section.card.card-pad", { "aria-label": "Indexes in reading order" },
        h("div.section-title", h("h2", "Indexes, in reading order"), h("span.hint", "Each book's index builds on the ones before it.")),
        h("div.series-track", books.map((b, i) => h(`a.track-book.${STATUS[b.status].cls}`, { href: `#/book/${encodeURIComponent(b.key)}` },
          cover(b, "sm"),
          h("span.track-info", h("span.eyebrow", `Book ${numWord(b.series.index ?? i + 1)}`), h("span.chain-title", b.title), statusChip(b),
            h("span.progress", h("i", { style: { width: `${b.coverage || 0}%` }, class: STATUS[b.status].cls })),
            h("span.hint", b.status === "building" ? `Building with ${b.job.model || "the default model"}`
              : b.status === "missing" ? (b.canBuild ? "No index yet" : "No EPUB to index")
                : [b.versions.find((v) => v.isDefault)?.model ? `Built with ${b.versions.find((v) => v.isDefault).model}.` : "",
                  firstGap >= 0 && i > firstGap ? "Built without an earlier book." : ""].filter(Boolean).join(" ")))))),
        todo.length ? h("div.notice.warn.series-cta", icon("layers"),
          h("div", h("b", "Finish the series in order. "),
            `Queues ${todo.map((b) => b.title).join(", then ")}, each building on the books before it.`),
          h("button.btn.primary", { type: "button", on: { click: () => buildDialog(todo.map((b) => ({ key: b.key, title: b.title, words: b.words })),
            { title: `Build ${g.name} in order` }) } }, icon("play"), todo.length === 1 ? "Build 1 book" : `Build ${todo.length} in order`)) : null));
    clearTimeout(timer);
    if (books.some((b) => b.status === "building")) timer = setTimeout(load, 5000);
  }
  load();
  return () => clearTimeout(timer);
}
