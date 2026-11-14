// The Reader's edit mode: forms for entries, details, connections, events and recaps, and the
// paragraph picker that chooses a reveal point. Everything goes through POST /api/library/{key}/edits,
// one change per call, and each change can be undone from the toast it shows (the same journal as
// merges). Index content and book text are untrusted, so all text is rendered through h().
import { api } from "./api.js";
import { confirmDialog, modal } from "./build.js";
import { clear, foldList, h, icon, toast, TYPE_LABEL } from "./dom.js";

const TYPES = ["person", "creature", "place", "group", "object", "concept", "term"];
const NAME_KINDS = ["primary", "alias", "title", "nickname", "descriptive"];
const BASIS_HELP = "Stated: the text says it outright. Inferred: a careful reader would conclude it.";

/**
 * ctx: { key(), hash(), chapters(), seqAt(), docs(), view(), book(), reload() }
 *   key      the library key of the index being read
 *   hash     the text hash of the edition on disk (paragraphs are fetched by it)
 *   chapters the edition's chapters [{ n, title, start }]
 *   seqAt    the paragraph the reader is at now (the default reveal point for new items)
 *   docs     the raw index documents in view (this book and, in a series, the others)
 *   view     the gated view at the current position
 *   book     this book's series key, or null
 *   reload   re-reads the index after a change; given an anchor id, moves the position forward to it
 */
export function createEditor(ctx) {
  // ---- plumbing ------------------------------------------------------------------------------

  /** Where a toast goes: inside the entry drawer while it is open (a modal dialog makes the rest of
   *  the page inert, so a toast outside it could not be clicked), else the page's toast stack. */
  function toastHost() {
    const drawer = document.querySelector("dialog.drawer[open]");
    if (!drawer) return document.getElementById("toasts");
    let host = drawer.querySelector(".drawer-toasts");
    if (!host) { host = h("div.drawer-toasts"); drawer.append(host); }
    return host;
  }

  /** A toast that offers to undo the change it announces. Only the latest change can be undone, so
   *  an earlier toast's Undo is stale and goes away. */
  function savedToast(what, undoId) {
    for (const old of document.querySelectorAll(".toast .toast-undo")) old.closest(".toast")?.remove();
    const dismiss = h("button.icon-btn.small.toast-dismiss", { type: "button", "aria-label": "Dismiss", title: "Dismiss" }, icon("x"));
    const undo = h("button.linkish.toast-undo", { type: "button" }, "Undo");
    const el = h("div.toast.ok", { role: "status" }, icon("check-circle"), h("div.toast-msg", h("span", what), " ", undo), dismiss);
    dismiss.addEventListener("click", () => el.remove());
    undo.addEventListener("click", async () => {
      undo.disabled = true;
      try { await api.undo(ctx.key(), undoId); el.remove(); plainToast("Undone"); ctx.reload(); }
      catch (e) { el.remove(); plainToast(e.message, "error"); }
    });
    toastHost().append(el);
    setTimeout(() => el.remove(), 9000);
  }
  /** The page's toast, placed where it can be reached (see toastHost); errors stay until dismissed. */
  const plainToast = (message, kind = "ok") => toast(message, kind, { host: toastHost() });

  /** Send one change. A wording refusal (422) lands in `errorEl` and keeps the form open; anything else is a toast. */
  async function apply(op, errorEl = null) {
    if (errorEl) errorEl.textContent = "";
    try {
      const r = await api.edit(ctx.key(), op);
      await ctx.reload(op.op.startsWith("add-") ? r.anchor : null);   // a new item: read up to where it appears
      savedToast(r.what || "Saved", r.undo);   // after the reload, which redraws the drawer the toast may sit in
      return true;
    } catch (e) {
      if (e.status === 422 && errorEl) errorEl.textContent = e.message;
      else plainToast(e.message, "error");
      return false;
    }
  }

  /** The raw entity, or the raw item of a kind, by id, together with the document that holds it. */
  function findEntity(id) {
    for (const d of ctx.docs()) { const e = (d.entities || []).find((x) => x.id === id); if (e) return { item: e, doc: d }; }
    return null;
  }
  function findItem(kind, id) {
    for (const d of ctx.docs()) {
      let item = null;
      if (kind === "facet") for (const e of d.entities || []) { item = (e.facets || []).find((f) => f.id === id); if (item) break; }
      else item = (d[`${kind}s`] || []).find((x) => x.id === id);
      if (item) return { item, doc: d };
    }
    return null;
  }
  const seqOfAnchor = (doc, anchorId) => doc?.anchors?.find((a) => a.id === anchorId)?.seq ?? null;
  const seqOf = (found) => (found ? seqOfAnchor(found.doc, found.item.from) : null);
  /** Items from another book in the series are read-only here: their file is that book's. */
  const mine = (found) => found && (!found.doc.series || !ctx.book() || found.doc.series.book === ctx.book());

  function chapterOf(seq) {
    let ch = null;
    for (const c of ctx.chapters() || []) if (c.start <= seq) ch = c; else break;
    return ch;
  }
  function whereLabel(seq) {
    const ch = chapterOf(seq);
    return ch ? `${ch.title}, paragraph ${seq - ch.start + 1}` : `paragraph ${seq + 1}`;
  }
  const clip = (t, n = 90) => (t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t);

  // ---- the paragraph picker --------------------------------------------------------------------

  /** Browse the book around `seq` and choose a paragraph. Resolves with its seq, or null. */
  function pickParagraph(seq) {
    return new Promise((resolve) => {
      const m = modal("Choose the paragraph", { width: 640 });
      const real = m.close;
      let settled = false;
      m.close = () => { real(); if (!settled) { settled = true; resolve(null); } };
      let center = seq, chosen = seq, total = null;
      const list = h("div.para-list", { role: "listbox", "aria-label": "Paragraphs" });
      const where = h("p.hint.para-where");
      const err = h("p.field-error");
      const earlier = h("button.btn.small", { type: "button" }, icon("chevron-left"), "Earlier");
      const later = h("button.btn.small", { type: "button" }, "Later", icon("chevron"));
      const chapSel = h("select.select.para-chapter", { "aria-label": "Jump to a chapter" },
        h("option", { value: "" }, "Jump to a chapter…"),
        (ctx.chapters() || []).map((c) => h("option", { value: String(c.start) }, c.title)));
      chapSel.addEventListener("change", () => { if (chapSel.value !== "") { center = +chapSel.value; chosen = center; fill(); } chapSel.value = ""; });
      earlier.addEventListener("click", () => { center = Math.max(0, center - 8); fill(); });
      later.addEventListener("click", () => { center = total == null ? center + 8 : Math.min(total - 1, center + 8); fill(); });
      const use = h("button.btn.primary", { type: "button" }, "Use this paragraph");
      const cancel = h("button.btn", { type: "button" }, "Cancel");
      cancel.addEventListener("click", () => m.close());
      use.addEventListener("click", () => { settled = true; real(); resolve(chosen); });
      async function fill() {
        err.textContent = "";
        let r;
        try { r = await api.blocks(ctx.hash(), center, 4, 4, ctx.key()); }
        catch (e) { err.textContent = e.message; return; }
        total = r.total;
        earlier.disabled = center <= 0;
        later.disabled = center >= total - 1;
        where.textContent = r.chapter ? r.chapter.title : "Before the first chapter";
        clear(list, r.blocks.map((b) => {
          const ch = chapterOf(b.seq);   // numbered within its own chapter, so the previous chapter's tail doesn't go negative
          const row = h(`button.para-row${b.eligible ? "" : ".dim"}`, { type: "button", role: "option", "aria-pressed": String(b.seq === chosen), "aria-selected": String(b.seq === chosen),
            title: ch && r.chapter && ch.start !== r.chapter.start ? ch.title : null,
            on: { click: () => { chosen = b.seq; for (const x of list.children) { x.setAttribute("aria-pressed", String(x === row)); x.setAttribute("aria-selected", String(x === row)); } } } },
            h("span.para-n.num", String(b.seq - (ch ? ch.start : 0) + 1)),
            h("span.para-text", clip(b.text, 300),
              b.eligible ? null : h("span.hint", " Too short to anchor; the next paragraph will be used.")));
          return row;
        }));
      }
      clear(m.body,
        h("p.hint", "The entry appears once the reader has finished this paragraph. Pick the one that reveals it."),
        h("div.para-bar", chapSel, h("div.para-nav", earlier, later)),
        where, list, err,
        h("div.confirm-actions", cancel, use));
      fill();
    });
  }

  /** "Revealed at Chapter II, paragraph 14 “first words…”" with a Change link. `state.seq` is read and written. */
  function revealLine(state) {
    const text = h("span.reveal-text");
    const change = h("button.linkish", { type: "button" }, "Change");
    const line = h("p.hint.reveal-line", icon("book-open"), " ", text, " ", change);
    async function paint() {
      if (state.seq == null) { text.textContent = "No paragraph chosen yet."; return; }
      text.textContent = `Revealed at ${whereLabel(state.seq)}`;
      try {
        const r = await api.blocks(ctx.hash(), state.seq, 0, 0, ctx.key());
        const b = r.blocks[0];
        if (b) text.textContent = `Revealed at ${whereLabel(state.seq)}: “${clip(b.text, 70)}”`;
      } catch {}
    }
    change.addEventListener("click", async () => {
      const got = await pickParagraph(state.seq ?? ctx.seqAt() ?? 0);
      if (got != null) { state.seq = got; paint(); line.dispatchEvent(new Event("change", { bubbles: true })); }
    });
    paint();
    return line;
  }

  // ---- form pieces -----------------------------------------------------------------------------

  const textarea = (value, rows, label) => h("textarea.input", { rows: String(rows), "aria-label": label }, value || "");
  const input = (value, label, ph) => h("input.input", { value: value || "", "aria-label": label, placeholder: ph || "" });
  const select = (options, value, label) => h("select.select", { "aria-label": label }, options.map(([v, t]) => h("option", { value: v, selected: v === value }, t)));
  const basisSelect = (value) => select([["stated", "Stated in the text"], ["inferred", "Inferred by the reader"]], value || "stated", "Basis");
  const actions = (onSave, onCancel, saveLabel = "Save") => {
    const save = h("button.btn.primary.small", { type: "button" }, saveLabel);
    const cancel = h("button.btn.small", { type: "button" }, "Cancel");
    save.addEventListener("click", async () => { save.disabled = true; try { await onSave(); } finally { save.disabled = false; } });
    cancel.addEventListener("click", onCancel);
    return h("div.edit-actions", cancel, save);
  };
  const form = (...children) => h("div.edit-form", children);
  /** Replace a row's content with a form; cancel puts the old content back. */
  function inline(row, build) {
    const old = [...row.childNodes];
    const restore = () => clear(row, old);
    clear(row, build(restore));
    row.querySelector("textarea, input, select")?.focus();
  }
  const rowActions = (onEdit, onDelete, what) => h("span.row-actions",
    h("button.icon-btn.small", { type: "button", "aria-label": `Edit ${what}`, title: "Edit", on: { click: onEdit } }, icon("pen")),
    h("button.icon-btn.small", { type: "button", "aria-label": `Delete ${what}`, title: "Delete", on: { click: onDelete } }, icon("trash")));
  async function confirmDelete(what, message, op) {
    if (!await confirmDialog({ title: `Delete this ${what}?`, message, confirmLabel: "Delete" })) return;
    await apply(op);
  }

  // ---- details and descriptions ----------------------------------------------------------------

  function facetForm({ entity, facet = null, kind = null }, done) {
    const found = facet ? findItem("facet", facet.id) : null;
    const isProfile = kind === "profile" || found?.item?.kind === "profile";
    const state = { seq: found ? seqOf(found) : ctx.seqAt() };
    const text = textarea(facet?.text, isProfile ? 5 : 3, isProfile ? "Description" : "Detail");
    const basis = basisSelect(facet?.basis);
    const err = h("p.field-error");
    return form(
      h("label.label", isProfile ? "Who or what this is, as the reader knows it here" : "One thing the reader knows about them here"),
      text, err,
      h("div.edit-row", basis, h("span.hint", BASIS_HELP)),
      revealLine(state),
      actions(async () => {
        const op = facet
          ? { op: "edit-facet", id: facet.id, text: text.value.trim(), basis: basis.value, ...(state.seq !== seqOf(found) ? { seq: state.seq } : {}) }
          : { op: "add-facet", entity, text: text.value.trim(), basis: basis.value, seq: state.seq, ...(isProfile ? { kind: "profile" } : {}) };
        if (await apply(op, err)) done();
      }, done));
  }

  function factRow(entityId, f, node, isProfile) {
    const found = findItem("facet", f.id);
    if (!mine(found)) return null;
    return rowActions(
      () => inline(node, (restore) => facetForm({ entity: entityId, facet: f, kind: isProfile ? "profile" : null }, restore)),
      () => confirmDelete(isProfile ? "description" : "detail", `“${clip(f.text, 120)}” is removed from the index. You can undo this right after.`, { op: "delete-facet", id: f.id }),
      isProfile ? "description" : "detail");
  }

  // ---- names ---------------------------------------------------------------------------------

  function nameForm({ entity, index = null, name = null }, done) {
    const found = findEntity(entity);
    const state = { seq: name ? seqOfAnchor(found.doc, name.from) : ctx.seqAt() };
    const value = input(name?.value, "Name", "Name as the book writes it");
    const kind = select(NAME_KINDS.map((k) => [k, k[0].toUpperCase() + k.slice(1)]), name?.kind || "alias", "Kind of name");
    const err = h("p.field-error");
    return form(h("div.edit-row", value, kind), err, revealLine(state),
      actions(async () => {
        const op = name
          ? { op: "edit-name", entity, index, value: value.value.trim(), kind: kind.value, ...(state.seq !== seqOfAnchor(found.doc, name.from) ? { seq: state.seq } : {}) }
          : { op: "add-name", entity, value: value.value.trim(), kind: kind.value, seq: state.seq };
        if (await apply(op, err)) done();
      }, done));
  }

  function namesSection(entityId) {
    const found = findEntity(entityId);
    if (!mine(found)) return null;
    const names = found.item.names || [];
    const list = h("ul.name-list", names.map((n, i) => {
      const li = h("li");
      const seq = seqOfAnchor(found.doc, n.from);
      clear(li, h("span.nm", n.value), h("span.hint", `${n.kind}${seq != null ? `, ${whereLabel(seq)}` : ""}`),
        rowActions(() => inline(li, (restore) => nameForm({ entity: entityId, index: i, name: n }, restore)),
          () => confirmDelete("name", `“${n.value}” is removed from this entry's names.`, { op: "delete-name", entity: entityId, index: i }), "name"));
      return li;
    }));
    const add = h("button.btn.small", { type: "button" }, icon("sparkle"), "Add a name");
    const slot = h("div.add-slot");
    add.addEventListener("click", () => { add.hidden = true; clear(slot, nameForm({ entity: entityId }, () => { clear(slot); add.hidden = false; })); slot.querySelector("input")?.focus(); });
    return h("div.d-sec", h("span.eyebrow", "Names"), list, h("div.add-wrap", add, slot));
  }

  // ---- connections -----------------------------------------------------------------------------

  function relationForm({ entityId, rel = null }, done) {
    const v = ctx.view();
    const others = v.entities.filter((e) => e.id !== entityId).sort((a, b) => a.name.localeCompare(b.name));
    const me = v.entities.find((e) => e.id === entityId);
    const found = rel ? findItem("relation", rel.id) : null;
    const state = { seq: found ? seqOf(found) : ctx.seqAt() };
    const otherId = rel ? (rel.src === entityId ? rel.dst : rel.src) : (others[0]?.id || "");
    const other = select(others.map((e) => [e.id, `${e.name} (${TYPE_LABEL[e.type].toLowerCase()})`]), otherId, "The other entry");
    const dir = select([["out", `${me?.name || "This entry"} → the other`], ["in", `The other → ${me?.name || "this entry"}`]], rel && rel.dst === entityId ? "in" : "out", "Direction");
    const label = input(rel?.label, "How they are tied", "e.g. employer of, sister of, lives at");
    const kindSel = select([["", "Kind of tie"], ["family", "Family"], ["romance", "Romance"], ["friendship", "Friendship"],
      ["alliance", "Alliance"], ["service", "Service"], ["hostility", "Hostility"], ["acquaintance", "Acquaintance"], ["place", "Place"]],
      found?.item?.kind || "", "Kind of tie");
    const basis = basisSelect(rel ? found?.item?.basis : null);
    const err = h("p.field-error");
    if (!others.length) return form(h("p.hint", "No other entry is visible here to connect to."), actions(async () => done(), done, "OK"));
    return form(
      h("label.label", "Reads from the first entry to the second: Murray, orderly who saved the life of, Watson."),
      h("div.edit-row", dir, other), h("div.edit-row", label, kindSel), err,
      h("div.edit-row", basis, h("span.hint", BASIS_HELP)),
      revealLine(state),
      actions(async () => {
        const src = dir.value === "out" ? entityId : other.value, dst = dir.value === "out" ? other.value : entityId;
        const op = rel
          ? { op: "edit-relation", id: rel.id, label: label.value.trim(), basis: basis.value, kind: kindSel.value, src, dst, ...(state.seq !== seqOf(found) ? { seq: state.seq } : {}) }
          : { op: "add-relation", src, dst, label: label.value.trim(), basis: basis.value, kind: kindSel.value, seq: state.seq };
        if (await apply(op, err)) done();
      }, done));
  }

  function relationRowActions(entityId, r, node) {
    const found = findItem("relation", r.id);
    if (!mine(found)) return null;
    return rowActions(() => inline(node, (restore) => relationForm({ entityId, rel: r }, restore)),
      () => confirmDelete("connection", `The tie “${r.label}” is removed.`, { op: "delete-relation", id: r.id }), "connection");
  }

  // ---- events ----------------------------------------------------------------------------------

  function eventForm({ ev = null, preset = [] }, done) {
    const v = ctx.view();
    const found = ev ? findItem("event", ev.id) : null;
    const state = { seq: found ? seqOf(found) : ctx.seqAt() };
    const chosen = new Set(ev ? ev.entities : preset);
    const summary = textarea(ev?.summary, 3, "What happens");
    const kind = select([["", "A development of the story"], ["turn", "A turning point: a revelation, twist or decision that changes its course"],
      ["minor", "A small moment that changes nothing"]], ev?.kind || "", "How much it matters");
    const err = h("p.field-error");
    const chips = h("div.ents");
    const pick = h("select.select", { "aria-label": "Add an entry involved" });
    function paintChips() {
      clear(chips, [...chosen].map((id) => {
        const e = v.entities.find((x) => x.id === id);
        return h("button.chip", { type: "button", "aria-label": `Remove ${e?.name || id}`, on: { click: () => { chosen.delete(id); paintChips(); chips.dispatchEvent(new Event("change", { bubbles: true })); } } }, e?.name || id, icon("x"));
      }));
      clear(pick, h("option", { value: "" }, "Who is involved…"),
        v.entities.filter((e) => !chosen.has(e.id)).sort((a, b) => a.name.localeCompare(b.name)).map((e) => h("option", { value: e.id }, e.name)));
    }
    pick.addEventListener("change", () => { if (pick.value) { chosen.add(pick.value); paintChips(); } });
    paintChips();
    return form(h("label.label", "What happens, in your own words"), summary, err, chips, pick, kind, revealLine(state),
      actions(async () => {
        const op = ev
          ? { op: "edit-event", id: ev.id, summary: summary.value.trim(), entities: [...chosen], kind: kind.value, ...(state.seq !== seqOf(found) ? { seq: state.seq } : {}) }
          : { op: "add-event", summary: summary.value.trim(), entities: [...chosen], kind: kind.value, seq: state.seq };
        if (await apply(op, err)) done();
      }, done));
  }

  function eventRowActions(ev, node) {
    const found = findItem("event", ev.id);
    if (!mine(found)) return null;
    return rowActions(() => inline(node, (restore) => eventForm({ ev }, restore)),
      () => confirmDelete("event", `“${clip(ev.summary, 120)}” leaves the timeline.`, { op: "delete-event", id: ev.id }), "event");
  }

  // ---- recaps ----------------------------------------------------------------------------------

  function recapForm({ recap = null, scope = "story", title = "" }, done) {
    const found = recap ? findItem("recap", recap.id) : null;
    const sc = found?.item?.scope || scope;
    const state = { seq: found ? seqOf(found) : ctx.seqAt() };
    const scopeSel = select([["story", "The story so far"], ["chapter", "A chapter's summary"]], sc, "Kind of recap");
    const titleIn = input(found?.item?.title || title, "Chapter title", "Chapter title, as the book names it");
    const text = textarea(recap?.text, 6, "Recap");
    const err = h("p.field-error");
    const titleField = h("div.field", { hidden: scopeSel.value !== "chapter" }, h("label.label", "Chapter"), titleIn);
    scopeSel.addEventListener("change", () => { titleField.hidden = scopeSel.value !== "chapter"; });
    return form(
      recap ? null : scopeSel, titleField,
      h("label.label", sc === "chapter" || scopeSel.value === "chapter" ? "What happens in this chapter" : "The story so far, as a reader here knows it"),
      text, err, revealLine(state),
      actions(async () => {
        const op = recap
          ? { op: "edit-recap", id: recap.id, text: text.value.trim(), ...(found?.item?.scope === "chapter" ? { title: titleIn.value.trim() } : {}), ...(state.seq !== seqOf(found) ? { seq: state.seq } : {}) }
          : { op: "add-recap", text: text.value.trim(), scope: scopeSel.value, ...(scopeSel.value === "chapter" ? { title: titleIn.value.trim() } : {}), seq: state.seq };
        if (await apply(op, err)) done();
      }, done));
  }

  function recapRowActions(recap, node, { scope = "story", title = "" } = {}) {
    const found = findItem("recap", recap.id);
    if (!mine(found)) return null;
    return rowActions(() => inline(node, (restore) => recapForm({ recap, scope, title }, restore)),
      () => confirmDelete("recap", scope === "chapter" ? "This part of the chapter's summary is removed." : "This story-so-far recap is removed; the previous one shows instead.", { op: "delete-recap", id: recap.id }), "recap");
  }

  // ---- entries -------------------------------------------------------------------------------

  function entityForm(done) {
    const state = { seq: ctx.seqAt() };
    const name = input("", "Name", "Name, as the book first gives it");
    const type = select(TYPES.map((t) => [t, TYPE_LABEL[t]]), "person", "Kind of entry");
    const desc = textarea("", 3, "First description");
    const basis = basisSelect();
    const err = h("p.field-error");
    return form(
      h("div.edit-row", name, type), err,
      h("label.label", "Who or what this is, as the reader first meets them (optional)"), desc,
      h("div.edit-row", basis, h("span.hint", BASIS_HELP)),
      revealLine(state),
      actions(async () => {
        const op = { op: "add-entity", name: name.value.trim(), type: type.value, seq: state.seq, ...(desc.value.trim() ? { desc: desc.value.trim(), basis: basis.value } : {}) };
        if (await apply(op, err)) done();
      }, done, "Add"));
  }

  /** A button that opens a form in the slot after it. */
  function addButton(label, build) {
    const slot = h("div.add-slot");
    const btn = h("button.btn.small", { type: "button" }, icon("sparkle"), label);
    btn.addEventListener("click", () => {
      btn.hidden = true;
      clear(slot, build(() => { clear(slot); btn.hidden = false; }));
      slot.querySelector("input, textarea, select")?.focus();
    });
    return h("div.add-wrap", btn, slot);
  }
  const addEntityButton = () => addButton("Add an entry", (done) => entityForm(done));
  const addEventButton = (preset = []) => addButton("Add an event", (done) => eventForm({ preset }, done));
  const addRecapButton = () => addButton("Add a recap", (done) => recapForm({}, done));

  // ---- the drawer in edit mode ---------------------------------------------------------------

  /** The title block: an input to rename (the latest primary name) and a select for the kind. */
  function drawerTitle(e) {
    const found = findEntity(e.id);
    const name = input(e.name, "Name");
    const save = h("button.btn.small", { type: "button" }, "Rename");
    const err = h("p.field-error");
    const type = select(TYPES.map((t) => [t, TYPE_LABEL[t]]), e.type, "Kind of entry");
    type.addEventListener("change", () => apply({ op: "set-type", entity: e.id, type: type.value }));
    save.addEventListener("click", async () => {
      if (!found) return;
      const ords = new Map((found.doc.anchors || []).map((a, i) => [a.id, i]));
      let best = -1;
      found.item.names.forEach((n, i) => { if (n.kind === "primary" && (best < 0 || (ords.get(n.from) ?? -1) >= (ords.get(found.item.names[best].from) ?? -1))) best = i; });
      if (best < 0) best = 0;
      await apply({ op: "edit-name", entity: e.id, index: best, value: name.value.trim() }, err);
    });
    name.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); save.click(); } });
    return h("div.d-title.editing-title", h("div.edit-row", type), h("div.edit-row", name, save), err);
  }

  /** The body of the drawer with pen and trash on every row, add buttons, and the delete-entry foot. */
  function drawerBody(e, rels, evs, { entityChip, bookLabel }) {
    const found = findEntity(e.id);
    const facts = (list) => foldList(list.map((f) => {
      const li = h("li");
      clear(li, h("span.fact-text", f.text), f.basis === "inferred" ? h("span.inferred", "inferred") : null, bookLabel(f.book), factRow(e.id, f, li, false));
      return li;
    }), (nodes) => h("ul.fact-list", nodes), { show: 4, noun: "details" });
    const eyebrow = (label, n) => h("span.eyebrow", label, n > 1 ? h("span.n", String(n)) : null);
    const profileRow = e.profile ? (() => {
      const p = h("div.profile");
      clear(p, h("span.eyebrow", e.type === "person" || e.type === "group" ? "Who they are" : "What it is"),
        h("p", e.profile.text, e.profile.basis === "inferred" ? h("span.inferred", "inferred") : null), bookLabel(e.profile.book),
        factRow(e.id, e.profile, p, true));
      return p;
    })() : null;
    const addProfile = addButton(e.profile ? "Add a newer description" : "Add a description", (done) => facetForm({ entity: e.id, kind: "profile" }, done));
    const addFact = addButton("Add a detail", (done) => facetForm({ entity: e.id }, done));
    const addRel = addButton("Add a connection", (done) => relationForm({ entityId: e.id }, done));
    const addEv = addButton("Add an event", (done) => eventForm({ preset: [e.id] }, done));
    const del = h("button.btn.danger.small", { type: "button" }, icon("trash"), "Delete this entry");
    del.addEventListener("click", async () => {
      if (!await confirmDialog({ title: `Delete ${e.name}?`,
        message: `The entry, its names and details, and its connections are removed from every book in the series. Events keep their text. You can undo this right after.`,
        confirmLabel: "Delete" })) return;
      await apply({ op: "delete-entity", entity: e.id });
    });
    return h("div.body.editing",
      h("p.hint.edit-help", "Write what a reader knows at the reveal point, in your own words. The index refuses hindsight and long quotes."),
      profileRow, addProfile,
      namesSection(e.id),
      h("div.d-sec", eyebrow("Details", e.facets.length), e.facets.length ? facts(e.facets) : h("p.muted", "Nothing beyond the name yet."), addFact),
      e.history.length ? h("details.history", h("summary", `Earlier impressions (${e.history.length})`), facts(e.history)) : null,
      h("div.d-sec", eyebrow("Connections", rels.length),
        rels.length ? foldList(rels.map((r) => {
          const li = h("li");
          clear(li, entityChip(r.src), h("span.lbl", r.label), entityChip(r.dst), relationRowActions(e.id, r, li));
          return li;
        }), (nodes) => h("ul.relations", nodes), { show: 3, noun: "connections" }) : null, addRel),
      h("div.d-sec", eyebrow("Appears in", evs.length),
        evs.length ? foldList(evs.map((ev) => {
          const li = h(`li.tl-ev${ev.kind ? `.${ev.kind}` : ""}`);
          clear(li, ev.kind === "turn" ? h("span.tl-kind", "Turning point") : null, h("p.ev", ev.summary), bookLabel(ev.book), eventRowActions(ev, li));
          return li;
        }), (nodes) => h("ol.timeline", nodes), { show: 3, fromEnd: true, noun: "events" }) : null, addEv),
      mine(found) ? h("div.drawer-foot", del) : null);
  }

  return { apply, pickParagraph, drawerTitle, drawerBody, addEntityButton, addEventButton, addRecapButton, eventRowActions, recapRowActions };
}
