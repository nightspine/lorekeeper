// Community indexes: the "Ready-made" block on the build page, the sheet of results, the preview
// strip, adding an index, the share dialog and the Settings card. The server talks to the registry;
// nothing here fetches anything but this app's own /api. Registry text is untrusted: it only ever
// goes through h() (textContent).
import { api } from "./api.js";
import { announce, clear, fmtInt, h, icon, toast } from "./dom.js";
import { modal } from "./build.js";

const REPORT = "https://github.com/nightspine/lorekeeper-registry/issues/new?title=Report:+";
const SHARE_FORM = "https://github.com/nightspine/lorekeeper-registry/issues/new?template=share.yml&title=Share:+";

const when = (iso) => {
  const d = Date.parse(iso);
  return Number.isFinite(d) ? new Date(d).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
};
const plural = (n, one, many) => `${fmtInt(n)} ${n === 1 ? one : many}`;
const WHO = { builder: "its builder", registry: "the registry", reader: "a reader", you: "you" };

// ---- what a result says about itself ------------------------------------------------------------

/** "Whole book" or "Partial": the headline never claims a check. The audit count goes in the sentence under it. */
export function headline(r) {
  return r.complete ? "Whole book" : "Partial";
}

/** The Checked measure: a main sentence, an optional quiet second one, and who vouches. Never a percentage. */
export function checkedOf(r) {
  const c = r.checked || {};
  const who = c.by ? `checked by ${WHO[c.by] || "someone"}${c.kind === "audit" && c.model ? ` with ${c.model}` : ""}` : null;
  if (c.kind === "audit") {
    const unsure = c.unsure || 0;
    const judged = c.unsupported != null ? c.supported + c.unsupported : Math.max(0, c.sampled - unsure);
    return {
      main: judged ? `${c.supported} of ${judged} claims held up` : `${plural(unsure || c.sampled, "claim", "claims")} could not be judged`,
      note: unsure && judged ? `${unsure} could not be judged from the passage cited.` : null, who };
  }
  if (c.kind === "book-checks") return { main: "Anchors and quoting checked", note: null, who };
  return { main: "Not checked yet", note: null, who: null };
}

/** One ember line, counts only, when a builder's check found names shown before the book uses them. */
export function earlyLine(n, whose = "Its builder's check") {
  if (!(n > 0)) return null;
  return `${whose} found ${n === 1 ? "1 name" : `${fmtInt(n)} names`} shown before the book uses ${n === 1 ? "it" : "them"}.`;
}

const madeLine = (r) => `${r.made || "Unknown"}${r.edited ? ", edited by hand" : ""}`;

/** The measures as a description list, in the same order on every row. */
export function measures(r) {
  const k = checkedOf(r);
  const row = (term, ...bits) => [h("dt", term), h("dd", ...bits)];
  return h("dl.measures",
    row("Checked", h("span", k.main), k.note ? h("span.m-sub", k.note) : null, k.who ? h("span.m-sub", k.who) : null),
    row("Covers", r.complete ? "Whole book" : "Part of the book"),
    row("Made", madeLine(r)),
    row("Licence", r.license));
}

// ---- adding and previewing ------------------------------------------------------------------------

const hexOf = (digest) => String(digest).replace(/^sha256:/, "");
export const previewHref = (catalogKey, r) => `#/preview/${hexOf(r.digest)}?catalog=${encodeURIComponent(catalogKey)}&listing=${encodeURIComponent(r.id)}`;

/** Download, check and file one result as a version of the book, then open it in the Reader. */
export async function useIndex(catalogKey, r, { busy = null } = {}) {
  busy?.(true);
  try {
    await api.communityFetch(catalogKey, r.id, r.digest);
    const done = await api.communityInstall(catalogKey, r.id, r.digest);
    location.hash = `#/reader?i=${encodeURIComponent(done.key)}`;
    toast(`Added ${r.publisher}'s index.`, "ok", { action: { label: "Undo", run: async () => {
      try { await api.communityUninstall(done.key); toast("Removed"); location.hash = `#/book/${encodeURIComponent(catalogKey)}`; }
      catch (e) { toast(e.message, "error"); }
    } } });
    return true;
  } catch (e) {
    toast(e.message, "error");
    busy?.(false);
    return false;
  }
}

// ---- the sheet -------------------------------------------------------------------------------------

/** A native dialog that is a side sheet on a desktop and a bottom sheet on a phone (css). */
function sheet(title) {
  const opener = document.activeElement;
  const closeBtn = h("button.icon-btn.small", { type: "button", "aria-label": "Close", title: "Close" }, icon("x"));
  const body = h("div.sheet-body");
  const dlg = h("dialog.sheet", { "aria-labelledby": "sheet-title" },
    h("header.sheet-head", h("h2#sheet-title", title), closeBtn), body);
  const ctl = { dialog: dlg, body, close() {
    window.removeEventListener("hashchange", ctl.close);
    if (dlg.open) dlg.close();
    dlg.remove();
    if (opener && document.contains(opener)) opener.focus();
  } };
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); ctl.close(); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) ctl.close(); });
  closeBtn.addEventListener("click", ctl.close);
  window.addEventListener("hashchange", ctl.close);
  document.body.append(dlg);
  dlg.showModal();
  closeBtn.focus();
  return ctl;
}

export function openSheet(catalogKey, results) {
  const sh = sheet("Community indexes for this book");
  const live = h("p.sr-only", { "aria-live": "polite" });
  let sort = "best";
  const list = h("ul.sheet-list");
  const order = () => (sort === "new" ? [...results].sort((a, b) => Date.parse(b.updated) - Date.parse(a.updated)) : results);
  const sorts = [["best", "Best match"], ["new", "Newest"]];
  const bar = h("div.sheet-sort", { role: "group", "aria-label": "Sort by" });
  const paintSort = () => clear(bar, sorts.map(([k, label]) => h("button.sort-btn", { type: "button", "aria-pressed": String(sort === k),
    on: { click: () => { sort = k; paintSort(); paintList(); announce(`Sorted by ${label.toLowerCase()}`); } } }, label)));
  const row = (r, i) => {
    const id = `sheet-row-${i}`;
    const use = h("button.btn.primary", { type: "button" }, r.update ? "Update" : "Use this index");
    use.addEventListener("click", async () => {
      if (await useIndex(catalogKey, r, { busy: (b) => { use.disabled = b; } })) sh.close();
    });
    const more = h("details.row-more", h("summary", { "aria-label": "More for this index" }, icon("chevron")),
      h("a", { href: REPORT + encodeURIComponent(r.id), target: "_blank", rel: "noopener noreferrer" }, "Report a problem"));
    return h("li.sheet-row", { "aria-labelledby": id },
      h("h3", { id }, `${r.publisher}'s index`, r.installed && !r.update ? h("span.chip.chip-after", "In your library") : null),
      h("p.sheet-sub", headline(r)),
      h("p.sheet-ver", `Version ${r.version}, shared ${when(r.updated)}.`),
      measures(r),
      r.early > 0 ? h("p.early-line", earlyLine(r.early)) : null,
      h("div.sheet-actions",
        h("a.btn", { href: previewHref(catalogKey, r), on: { click: () => sh.close() } }, "Preview"),
        r.installed && !r.update ? h("a.btn", { href: `#/reader?i=${encodeURIComponent(r.installed)}`, on: { click: () => sh.close() } }, "Read") : use,
        more));
  };
  const paintList = () => clear(list, order().map(row));
  paintSort();
  paintList();
  clear(sh.body, h("div.sheet-top", h("p.hint", "Every one of these matches your copy of the book exactly."), bar), list);
  live.textContent = `${results.length} community ${results.length === 1 ? "index" : "indexes"} found`;
  sh.body.prepend(live);
  return sh;
}

// ---- the Ready-made block on the build page --------------------------------------------------------

/** Returns { el, offered(): bool, onOffer(fn) }. The block keeps its height while loading so the form below never jumps. */
export function readyMade(catalogKey) {
  const el = h("section.ready", { "aria-labelledby": "ready-h", hidden: true });
  const listeners = new Set();
  const ctl = { el, offering: false, onOffer: (fn) => { listeners.add(fn); fn(ctl.offering); }, refresh: load };
  const set = (offering) => { if (ctl.offering !== offering) { ctl.offering = offering; listeners.forEach((fn) => fn(offering)); } };
  let gone = false;

  const frame = (...kids) => { el.hidden = false; clear(el, h("h2#ready-h.build-q", "Ready-made"), ...kids); };

  function asking() {
    set(false);
    frame(h("div.ready-card.ask",
      h("p", "See if someone has already shared an index for this book?"),
      h("p.hint", "Lorekeeper asks the community registry about a book when you open its page. It sends a fingerprint of the book's text, nothing else."),
      h("div.ready-actions",
        h("button.btn", { type: "button", on: { click: () => choose(true) } }, "Turn on"),
        h("button.linkish", { type: "button", on: { click: () => choose(false) } }, "Not now"))));
  }
  async function choose(on) {
    try { await api.setCommunity(on); } catch (e) { return toast(e.message, "error"); }
    if (on) load(); else { el.hidden = true; clear(el); }
  }

  function skeleton() {
    frame(h("div.ready-card.skeleton", { role: "status", "aria-label": "Looking for a ready-made index" },
      h("span.sk.w60"), h("span.sk.w40"), h("div.ready-actions", h("span.sk.btn-sk"))));
  }

  function found(results) {
    const upd = results.find((r) => r.update);
    const best = upd || results[0];
    set(true);
    const use = h("button.btn.primary", { type: "button" }, upd ? "Update" : "Use this index");
    use.addEventListener("click", async () => { await useIndex(catalogKey, best, { busy: (b) => { use.disabled = b; } }); });
    const already = best.installed && !best.update;
    if (already) set(false);
    const more = results.length > 1 ? h("button.linkish", { type: "button", on: { click: () => {
      openSheet(catalogKey, results);
    } } }, `See all ${results.length}`) : null;
    frame(h("div.ready-card.found",
      h("p.ready-line", upd ? "A newer version of the index you use is available." : already ? "You already use this index." : headline(best)),
      h("p.hint", upd || already ? `Shared by ${best.publisher}.`
        : `Shared by ${best.publisher}.${best.checked?.kind === "audit" ? ` ${checkedOf(best).main}.` : ""} Matches your copy exactly.`),
      best.early > 0 ? h("p.early-line", earlyLine(best.early)) : null,
      h("div.ready-actions",
        already ? h("a.btn", { href: `#/reader?i=${encodeURIComponent(best.installed)}` }, "Read") : use,
        h("a.btn", { href: previewHref(catalogKey, best) }, "Preview"),
        more)));
  }

  async function load() {
    skeleton();
    let r;
    try { r = await api.communityBook(catalogKey); } catch { if (!gone) { el.hidden = true; set(false); } return; }
    if (gone) return;
    if (r.state === "ask") return asking();
    if (r.state !== "ok") { el.hidden = true; set(false); return; }   // off, no text on disk, or the registry is unreachable
    if (!r.results.length) { set(false); return frame(h("p.hint.ready-none", "No one has shared an index for this book yet.")); }
    found(r.results);
  }
  load();
  ctl.stop = () => { gone = true; };
  return ctl;
}

// ---- the preview ------------------------------------------------------------------------------------

/** Finds the listing a preview route names, downloads and checks it. Resolves { result, fit } or throws. */
export async function fetchPreview(catalogKey, listingId, hex) {
  const digest = `sha256:${hex}`;
  const books = await api.communityBook(catalogKey);
  const result = (books.results || []).find((r) => r.id === listingId && r.digest === digest);
  if (!result) {
    const e = new Error("That index is no longer listed for this book. Pick another one.");
    e.code = "gone";
    throw e;
  }
  const fit = await api.communityFetch(catalogKey, listingId, digest);
  return { result, fit };
}

/** The strip above the position panel in preview mode. `info` is { catalog, result, fit }. */
export function previewStrip(info) {
  const { catalog, result: r, fit } = info;
  const add = h("button.btn.primary", { type: "button" }, "Add to library");
  add.addEventListener("click", () => useIndex(catalog, r, { busy: (b) => { add.disabled = b; } }));
  const back = h("button.btn", { type: "button", on: { click: () => {
    if (history.length > 1) history.back(); else location.hash = `#/book/${encodeURIComponent(catalog)}`;
  } } }, "Back");
  const lines = [h("p.fit", `${fit.resolved === fit.anchors ? `${fmtInt(fit.anchors)} of ${fmtInt(fit.anchors)}` : `${fmtInt(fit.resolved)} of ${fmtInt(fit.anchors)}`} anchors found in your copy.`
    + (fit.problems ? ` ${plural(fit.problems, "problem", "problems")} found against your copy.` : ""))];
  if (fit.early > 0) lines.push(h("p.early-line", earlyLine(fit.early, "Your copy's check")));
  const result = h("p.checked-line", { "aria-live": "polite" });
  const k = checkedOf(r);
  if (r.checked?.kind) result.textContent = `${k.main}. ${k.who[0].toUpperCase()}${k.who.slice(1)}.`;
  const check = h("button.linkish", { type: "button" }, "Check it yourself");
  const paintCheck = (c) => {
    const judged = c.unsupported != null ? c.supported + c.unsupported : c.sampled - (c.unsure || 0);
    result.textContent = `${c.supported} of ${judged} claims held up, checked by you.${c.unsure ? ` ${c.unsure} could not be judged from the passage cited.` : ""}`;
  };
  if (r.checked?.kind === "audit" && r.checked.by === "you") paintCheck(r.checked);
  check.addEventListener("click", async () => {
    check.disabled = true;
    result.textContent = "Checking a sample of its claims against the book…";
    try { paintCheck(await api.communityAudit(r.digest)); check.remove(); }
    catch (e) { result.textContent = e.message; check.disabled = false; }
  });
  // "Check it yourself" needs a cloud model's key: absent without one.
  api.providers().then((p) => { if (!p.providers.some((x) => x.configured && x.id !== "ollama")) check.remove(); }).catch(() => check.remove());
  return h("section.preview-strip", { "aria-label": "Preview" },
    h("div.ps-main",
      h("p.ps-title", h("b", `Preview of ${r.publisher}'s index.`), " Not in your library yet."),
      h("div.ps-actions", add, back)),
    h("div.ps-facts", lines, result, check));
}

/** The page shown while a preview downloads, or when it can't be used. */
export function previewError(error, catalog) {
  return h("div.load-error", { role: "alert" },
    h("h2", "Couldn't open the preview"),
    h("p.muted", error.message || "Something went wrong."),
    h("a.btn.primary", { href: catalog ? `#/book/${encodeURIComponent(catalog)}` : "#/library" }, "Try another"));
}

// ---- sharing -----------------------------------------------------------------------------------------

const STATE_WORD = { pass: "Passed", warn: "Heads up", fail: "Needs fixing", todo: "Optional" };
const STATE_ICON = { pass: "check-circle", warn: "alert", fail: "x", todo: "clock" };

/** The share dialog for one index built here. */
export function openShare(libraryKey, title) {
  const m = modal(`Share ${title}`, { width: 560 });
  const body = m.body;
  clear(body, h("p.muted", { role: "status" }, "Checking the index against the book…"));
  const bundleLink = h("a.btn", { href: api.shareBundleUrl(libraryKey), download: "" }, icon("download"), "Save the bundle");
  async function check() {
    let r;
    try { r = await api.shareCheck(libraryKey); } catch (e) {
      return clear(body, h("div.notice.warn", icon("alert"), h("div", e.message)));
    }
    const items = r.items.map((i) => h("li.check-row", { class: i.state },
      icon(STATE_ICON[i.state] || "info"), h("span.check-state", STATE_WORD[i.state] || ""),
      h("span.check-text", i.id === "audit" && i.state === "todo" ? auditText(i) : i.text),
      i.id === "license" && i.state === "fail" ? licenseFix(r) : null,
      i.id === "audit" && i.state === "todo" ? auditAction(i) : null));
    clear(body,
      h("ul.checklist", items),
      h("p.hint", "Sent: the index file and the results above. Never the book."),
      h("ol.share-steps",
        h("li", h("b", "1. Save the bundle"), h("p.hint", r.bundleBytes ? `A file of about ${fmtInt(Math.max(1, Math.round(r.bundleBytes / 1024)))} KB.` : "Available once the problems above are fixed."),
          r.ok ? bundleLink : h("span.btn", { "aria-disabled": "true" }, icon("download"), "Save the bundle")),
        h("li", h("b", "2. Open the submission form"),
          h("p.hint", "Drag the saved file into the form. It is filed under your GitHub name."),
          h("a.btn.primary", { href: SHARE_FORM + encodeURIComponent(title), target: "_blank", rel: "noopener noreferrer" }, "Open the form"))));
  }
  function licenseFix(r) {
    const sel = h("select.select", { "aria-label": "Licence" }, (r.licenses || ["CC0-1.0", "CC-BY-4.0"]).map((l) => h("option", { value: l }, l)));
    const set = h("button.btn", { type: "button" }, "Use this licence");
    set.addEventListener("click", async () => {
      set.disabled = true;
      try { await api.edit(libraryKey, { op: "set-license", license: sel.value }); check(); }
      catch (e) { toast(e.message, "error"); set.disabled = false; }
    });
    return h("span.check-fix", sel, set);
  }
  const needsKey = (item) => /cloud model/i.test(item.text);
  const auditText = (item) => `Check 50 claims against the text, so the listing can say how many held up.${needsKey(item) ? " Needs a cloud model key." : ""}`;
  function auditAction(item) {
    if (needsKey(item)) return h("span.check-fix", h("a", { href: "#/settings/cloud", on: { click: () => m.close() } }, "Add a key in Settings"));
    const run = h("button.btn", { type: "button" }, item.text);
    run.addEventListener("click", async () => {
      run.disabled = true;
      run.replaceChildren(h("span.spinner", { "aria-hidden": "true" }), "Checking claims against the book…");
      try { await api.shareAudit(libraryKey); check(); }
      catch (e) { toast(e.message, "error"); run.disabled = false; run.textContent = item.text; }
    });
    return h("span.check-fix", run);
  }
  check();
  return m;
}
