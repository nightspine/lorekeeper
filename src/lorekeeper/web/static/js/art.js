// Pictures: the gating helper, the manifest cache, the picture elements, the lightbox, the "Draw a picture" sheet and the
// Pictures group of the build form. An art set lives beside the index (never in it); the server sends its manifest whole,
// as it sends the index, and the Reader gates every picture by its `from` anchor exactly as it gates names and events.
// Captions, prompts and notes are untrusted text: they only ever go through h(). Picture files are fetched only from
// /api/art/... URLs built here from ids the server sent, never from a path or URL found in the manifest.
import { api } from "./api.js";
import { confirmDialog } from "./build.js";
import { clear, h, icon, loading, sheet, showModalLocked, closeLocked, toast } from "./dom.js";

/** The styles the pipeline knows (art.STYLES), in the order the person sees them. Anything else is a custom style. */
export const STYLES = [
  ["digital-art", "Digital art"], ["comic-strip", "Comic strip"], ["anime", "Anime"], ["realism", "Realism"],
  ["hand-drawn", "Hand-drawn"], ["oil-painting", "Oil painting"], ["epic-sci-fi", "Epic sci-fi"], ["watercolour", "Watercolour"], ["ink", "Ink"],
];
const STYLE_LABEL = Object.fromEntries(STYLES);
export const QUALITIES = [["draft", "Draft"], ["standard", "Standard"], ["best", "Best"]];
export const SHAPES = [["landscape", "Landscape"], ["portrait", "Portrait"], ["square", "Square"]];
/** The picture kind an entry type is drawn as; concepts and terms are not drawn. */
export const KIND_OF_TYPE = { person: "portrait", creature: "portrait", group: "portrait", place: "place", object: "thing" };
const SHAPE_OF = { plate: "landscape", event: "landscape", portrait: "portrait", place: "landscape", thing: "landscape" };
export const ENTITY_KINDS = new Set(["portrait", "place", "thing"]);

/** A style slug as words: the known ones by name, a custom one as the person wrote it (the manifest's `style`). */
export function styleLabel(slug, text = null) {
  if (STYLE_LABEL[slug]) return STYLE_LABEL[slug];
  const t = typeof text === "string" && text.trim() ? text.trim() : String(slug || "").replace(/-/g, " ");
  return t ? t[0].toUpperCase() + t.slice(1) : "Custom";
}
/** "about 3 cents", "about $1.75", "price unknown" (never a made-up number). */
export function money(usd) {
  if (usd == null || !Number.isFinite(+usd)) return "price unknown";
  const v = +usd;
  if (v < 0.995) { const c = Math.max(1, Math.round(v * 100)); return `about ${c} cent${c === 1 ? "" : "s"}`; }
  return `about $${v.toFixed(2)}`;
}

// ---- per-viewer preferences (localStorage) ----------------------------------------------------

const read = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } };
const write = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} };
export const artOn = () => read("lk-art-on", "1") !== "0";
export const setArtOn = (on) => write("lk-art-on", on ? "1" : "0");
export const artStyle = () => read("lk-art-style", null);
export const setArtStyle = (slug) => write("lk-art-style", slug || null);
/** The Settings "Pictures" defaults for new pictures: style, quality, the per-run cap, and the image provider. */
export function artDefaults() {
  let d = {};
  try { d = JSON.parse(read("lk-art-defaults", "{}")) || {}; } catch {}
  return { style: typeof d.style === "string" ? d.style : "digital-art", quality: ["draft", "standard", "best"].includes(d.quality) ? d.quality : "standard",
    cap: Number.isFinite(+d.cap) && +d.cap > 0 ? +d.cap : 10, provider: typeof d.provider === "string" ? d.provider : null };
}
export const setArtDefaults = (d) => write("lk-art-defaults", JSON.stringify({ ...artDefaults(), ...d }));

// ---- the gating helper: the one rule ------------------------------------------------------------

/**
 * The pictures of one book's art set that may show at the reader's position. `anchorsOrdinal` maps that book's anchor
 * ids to their ordinals; `position` is the ordinal of the reader's frontier in that book (-1 before the first anchor);
 * `finished` says the book is fully revealed (an earlier book the reader has finished). A picture shows only when its
 * `from` is an anchor of that book at or before the position, compared by ordinal as every `from` is; an unknown anchor
 * never shows. Everywhere a picture is placed, counted or stepped to goes through this.
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

/** Is a note with a `from` anchor (the world note) visible? Same rule, for one item. */
export function visibleAt(from, anchorsOrdinal, position, finished) {
  const o = typeof from === "string" ? anchorsOrdinal.get(from) : null;
  return o != null && (finished || (position >= 0 && o <= position));
}

// ---- the manifest cache --------------------------------------------------------------------------

const cache = new Map();   // `${key}\n${style}` → Promise of { manifest, styles, screened } (null when the server has none)
/** The art set of an index in one style (the server's default style when none is asked). Null without an art set, on an
 *  older server, or when the request fails: pictures are an extra, never a reason for the Reader to fail. */
export function loadArt(key, style = null) {
  const k = `${key}\n${style || ""}`;
  if (!cache.has(k)) {
    const p = api.art(key, style).then((r) => (r && typeof r === "object" ? normalise(r) : null)).catch(() => null);
    cache.set(k, p);
  }
  return cache.get(k);
}
function normalise(r) {
  const manifest = r.manifest && typeof r.manifest === "object" ? r.manifest : r;
  const styles = (Array.isArray(r.styles) ? r.styles : []).filter((x) => typeof x === "string" || (x && typeof x.slug === "string"))
    .map((x) => (typeof x === "string" ? { slug: x, text: null } : { slug: x.slug, text: typeof x.style === "string" ? x.style : typeof x.text === "string" ? x.text : null }));
  const screened = r.screened && typeof r.screened === "object" ? r.screened : manifest.screened && typeof manifest.screened === "object" ? manifest.screened : {};
  return { manifest: { ...manifest, pictures: Array.isArray(manifest.pictures) ? manifest.pictures : [] }, styles, screened, slug: typeof r.slug === "string" ? r.slug : null };
}
/** Forget what was fetched for an index (after a draw, a redraw, a delete or an undo). */
export function forgetArt(key = null) {
  for (const k of [...cache.keys()]) if (key == null || k.startsWith(`${key}\n`)) cache.delete(k);
}

/** The image providers that can draw now: configured cloud providers that have an image table. Cached for a minute. */
let provCache = null;
export function imageProviders({ fresh = false } = {}) {
  if (fresh || !provCache || Date.now() - provCache.at > 60000) {
    const promise = api.providers().then((r) => (r.providers || []).filter((p) => canDraw(p))).catch(() => []);
    provCache = { at: Date.now(), promise };
  }
  return provCache.promise;
}
/** A provider row from /api/providers that draws: its image table, or (older server) Gemini or OpenAI by id. */
export const drawsPictures = (p) => Boolean(p && (p.images || p.id === "gemini" || p.id === "openai"));
const canDraw = (p) => drawsPictures(p) && p.ok;
/** The quality levels a provider's default (or named) image model offers; all three when the server does not say. */
export function levelsOf(p, model = null) {
  const m = p?.images?.models?.find((x) => x.id === (model || p.images.default));
  const lv = Array.isArray(m?.levels) && m.levels.length ? m.levels : ["draft", "standard", "best"];
  return QUALITIES.filter(([k]) => lv.includes(k));
}
/** The image model's label and its price for one picture at a quality ("Nano Banana 2.1, about 3 cents a picture"). */
export function imageLine(p, quality = "standard") {
  const m = p?.images?.models?.find((x) => x.id === p.images.default);
  if (!m) return drawsPictures(p) ? "Can draw" : "Can't draw";
  const price = typeof m.price === "object" && m.price ? m.price[quality] : null;
  const one = typeof price === "number" ? price : price && typeof price === "object" ? price.landscape ?? null : null;
  return `${m.label || m.id}, ${one != null ? `${money(one)} a picture` : "price unknown"}`;
}

// ---- picture elements ----------------------------------------------------------------------------

const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
/** The file of one picture, from the API only. `textHash` and `style` are the art set's (checked by the caller against the
 *  index's own edition); the id must look like an id. Null when anything is off: no picture rather than a guessed URL. */
export function artUrl(rec, textHash, style, { thumb = false } = {}) {
  if (!rec || !ID_RE.test(rec.id || "") || typeof textHash !== "string" || !textHash || typeof style !== "string" || !style) return null;
  return `/api/art/${encodeURIComponent(textHash)}/${encodeURIComponent(style)}/${encodeURIComponent(rec.id)}${thumb ? "?thumb=1" : ""}`;
}
/** An <img> for a picture: alt is the caption (a chapter title or an entry name, never the prompt), lazy and async.
 *  A file that fails to load removes the element, so a broken picture leaves nothing on screen. */
export function artImg(src, alt, cls = "art-img", { eager = false } = {}) {
  if (!src) return null;
  const img = h(`img.${cls}`, { src, alt: alt || "", loading: eager ? "eager" : "lazy", decoding: "async", draggable: "false" });
  img.addEventListener("error", () => {
    img.closest(".has-face")?.classList.remove("has-face");   // a Cast row gets its type mark back
    const btn = img.closest(".art-thumb, .art-plate, .art-face-btn");
    (btn || img).remove();
  });
  return img;
}

// ---- the lightbox ----------------------------------------------------------------------------------

const when = (iso) => { const t = Date.parse(iso || ""); return Number.isFinite(t) ? new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : ""; };

/**
 * One picture at full size, with its caption and a closed "How this was drawn". `items` are
 * [{ rec, src, caption, key, canEdit }] in the order the arrows step through (only visible pictures are ever passed).
 * `onChanged()` runs after a redraw, a delete or an undo; `onRedraw(item)` is "Draw again" for an item without a picture.
 */
export function openLightbox(items, index = 0, { onChanged = null } = {}) {
  if (!items.length) return null;
  const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let i = Math.min(Math.max(0, index), items.length - 1);
  const stage = h("div.lb-stage");
  const caption = h("p.lb-caption");
  const how = h("div.lb-how");
  const acts = h("div.lb-acts");
  const count = h("span.lb-count.subtle");
  const prev = h("button.icon-btn.lb-nav.lb-prev", { type: "button", "aria-label": "Previous picture", title: "Previous picture" }, icon("chevron-left"));
  const next = h("button.icon-btn.lb-nav.lb-next", { type: "button", "aria-label": "Next picture", title: "Next picture" }, icon("chevron"));
  const closeBtn = h("button.icon-btn.lb-close", { type: "button", "aria-label": "Close", title: "Close" }, icon("x"));
  const title = h("h2#lb-title.sr-only");
  const toasts = h("div.toasts.lb-toasts", { "aria-live": "polite" });
  const dlg = h("dialog.lightbox", { "aria-labelledby": "lb-title" },
    h("div.lb-panel", h("header.lb-head", title, count, closeBtn), h("div.lb-main", prev, stage, next), h("div.lb-foot", caption, acts, how)), toasts);
  const close = () => { closeLocked(dlg); };
  dlg.addEventListener("close", () => { dlg.remove(); if (returnTo?.isConnected) returnTo.focus({ preventScroll: true }); });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) close(); });
  closeBtn.addEventListener("click", close);
  prev.addEventListener("click", () => go(i - 1));
  next.addEventListener("click", () => go(i + 1));
  dlg.addEventListener("keydown", (e) => {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); go(i - 1); }
    if (e.key === "ArrowRight") { e.preventDefault(); go(i + 1); }
  });
  function go(n) {
    if (n < 0 || n >= items.length || n === i) return;
    i = n; paint();
  }
  function paint() {
    const it = items[i], r = it.rec;
    title.textContent = it.caption || "Picture";
    clear(stage, artImg(it.src, it.caption, "lb-img", { eager: true }));
    caption.textContent = it.caption || "";
    count.textContent = items.length > 1 ? `${i + 1} of ${items.length}` : "";
    prev.hidden = next.hidden = items.length < 2;
    prev.disabled = i === 0; next.disabled = i === items.length - 1;
    const facts = [
      ["Style", styleLabel(it.style || r.style, it.styleText)],
      ["Model", [r.model, r.provider ? `on ${it.providerLabel || r.provider}` : null].filter(Boolean).join(" ")],
      ["Quality", r.quality ? `${r.quality[0].toUpperCase()}${r.quality.slice(1)}` : null],
      ["Cost", typeof r.cost === "number" ? (r.cost > 0 ? money(r.cost).replace(/^about /, "") : "Price unknown") : null],
      ["Drawn", when(r.created)],
    ].filter(([, v]) => v);
    clear(how, h("details.lb-details", h("summary", icon("chevron"), "How this was drawn"),
      h("div.lb-details-body",
        typeof r.prompt === "string" && r.prompt ? h("div.lb-prompt", h("span.label", "Prompt"), h("p", r.prompt)) : null,
        h("dl.lb-facts", facts.map(([k, v]) => [h("dt", k), h("dd", v)])))));
    clear(acts, it.canEdit ? [
      h("button.btn.quiet", { type: "button", on: { click: () => redraw(it) } }, icon("refresh"), "Redraw"),
      h("button.btn.quiet.danger", { type: "button", on: { click: () => remove(it) } }, icon("trash"), "Delete"),
    ] : null);
  }
  function undoable(it, r, done) {
    const id = r?.undo ?? r?.journal ?? null;
    toast(done, "ok", { host: dlg.open ? toasts : null, action: id ? { label: "Undo", run: async () => {
      try { await api.undo(it.key, id); forgetArt(it.key); toast("Undone"); onChanged?.(); } catch (e) { toast(e.message, "error"); }
    } } : null });
  }
  function redraw(it) {
    const note = h("textarea.input.lb-note", { rows: "2", placeholder: "What to change, for example: in daylight, closer to the face", "aria-label": "What to change" });
    const go_ = h("button.btn.primary", { type: "button" }, "Redraw");
    const cancel = h("button.btn.quiet", { type: "button", on: { click: () => paint() } }, "Cancel");
    const err = h("p.hint.field-error", { role: "alert" });
    go_.addEventListener("click", async () => {
      go_.disabled = true; cancel.disabled = true;
      clear(acts, loading("Drawing it again…"));
      try {
        const r = await api.artRedraw(it.key, it.rec.id, note.value.trim());
        forgetArt(it.key);
        const rec = r?.record || r;
        if (rec && ID_RE.test(rec.id || "")) { it.rec = rec; it.src = it.srcFor ? it.srcFor(rec) : it.src; }
        paint();
        undoable(it, r, "Picture redrawn");
        onChanged?.();
      } catch (e) {
        clear(acts, h("div.lb-redraw", note, err, h("div.btns", cancel, go_)));
        err.textContent = e.code === "refused" ? `The provider would not draw it: ${e.message}` : e.message;
        go_.disabled = false; cancel.disabled = false;
      }
    });
    clear(acts, h("div.lb-redraw", h("label.label", "Redraw with a note, optional"), note, err, h("div.btns", cancel, go_)));
    note.focus();
  }
  async function remove(it) {
    if (!(await confirmDialog({ title: "Delete this picture?", message: "It goes from this art set. Undo brings it back.", confirmLabel: "Delete" }))) return;
    try {
      const r = await api.artDelete(it.key, it.rec.id);
      forgetArt(it.key);
      items.splice(i, 1);
      close();
      undoable(it, r, "Picture deleted");
      onChanged?.();
    } catch (e) { toast(e.message, "error", { host: toasts }); }
  }
  document.body.append(dlg);
  showModalLocked(dlg);
  paint();
  closeBtn.focus({ preventScroll: true });
  return { close };
}

// ---- the picture sheet: on-demand drawing --------------------------------------------------------

/**
 * "Draw a picture" for one item the reader can see. `opts`:
 *   key          the index's library key
 *   kind         plate | event | portrait | place | thing
 *   target       the chapter title, event id or entity id the server knows it by
 *   title        what the sheet is about ("Chapter V", "Jonathan Harker"), shown as the title
 *   p            the reader's frontier anchor (the server refuses a target not yet visible)
 *   style        the art set's current style slug (selected first)
 *   styles       the styles the art set has [{ slug, text }]
 *   world        the world note as visible at the position ({ text } or null: not shown)
 *   moments      for a chapter: [{ kind, target, label }] the visible moments it may draw, the first being the chapter's main one
 *   drawnLine    for a chapter: "4 moments in this chapter, 1 drawn" (counted from visible content only)
 *   onDrawn(rec) after a picture was filed
 */
export async function openPictureSheet(opts) {
  const provs = await imageProviders({ fresh: true });
  if (!provs.length) {
    return sheet({ title: "Draw a picture", className: "art-sheet",
      content: h("div.art-sheet-body", h("p", "Pictures need a Gemini or OpenAI key."),
        h("p", h("a", { href: "#/settings/cloud" }, "Add one in Settings, Cloud models"))) });
  }
  const d = artDefaults();
  const st = {
    kind: opts.kind, target: opts.target,
    style: opts.style || d.style, custom: "", shape: SHAPE_OF[opts.kind] || "landscape",
    quality: d.quality, provider: provs.find((p) => p.id === d.provider) || provs[0],
    prompt: "", written: "", price: null, busy: false,
  };
  if (!STYLE_LABEL[st.style]) { st.custom = (opts.styles || []).find((x) => x.slug === st.style)?.text || st.style.replace(/-/g, " "); st.style = "custom"; }
  const body = h("div.art-sheet-body");
  const drawBtn = h("button.btn.primary", { type: "button", disabled: true }, icon("sparkle"), "Draw");
  const footNote = h("span.art-price.num", { role: "status" });
  const sh = sheet({ title: `Draw a picture${opts.title ? `: ${opts.title}` : ""}`, className: "art-sheet", content: body,
    footer: h("div.art-foot", footNote, drawBtn),
    beforeClose: () => {
      if (st.busy) return false;
      if (!st.prompt || st.prompt === st.written) return true;
      confirmDialog({ title: "Discard your prompt?", message: "The prompt you changed is not kept.", confirmLabel: "Discard", cancelLabel: "Keep editing" })
        .then((ok) => { if (ok) { st.prompt = st.written; sh.close(true); } });
      return false;
    } });
  const styleSlug = () => (st.style === "custom" ? st.custom.trim() : st.style);

  const textarea = h("textarea.input.art-prompt", { rows: "7", "aria-label": "Prompt", spellcheck: "true" });
  textarea.addEventListener("input", () => { st.prompt = textarea.value; paintFoot(); });
  const err = h("div.art-error", { role: "alert" });

  async function write() {
    st.busy = true;
    clear(body, loading("Writing a prompt from what you have read…"));
    drawBtn.disabled = true;
    try {
      const r = await api.artPrompt(opts.key, { kind: st.kind, target: st.target, style: styleSlug(), shape: st.shape, p: opts.p ?? null });
      st.prompt = st.written = typeof r.prompt === "string" ? r.prompt : "";
      st.price = typeof r.price === "number" ? r.price : null;
      st.busy = false;
      paint();
    } catch (e) {
      st.busy = false;
      clear(body, h("div.art-error", { role: "alert" },
        h("p", e.code === "not-visible" ? "This isn't revealed at your position yet." : `Couldn't write a prompt: ${e.message}`)),
        e.code === "not-visible" ? null : h("button.btn", { type: "button", on: { click: write } }, "Try again"));
    }
  }

  const chip = (label, pressed, on) => h("button.filter-chip", { type: "button", "aria-pressed": String(pressed), on: { click: on } }, label);
  function paint() {
    textarea.value = st.prompt;
    const customField = h("input.input.art-custom", { value: st.custom, placeholder: "Describe the style, for example: linocut, two colours", "aria-label": "Custom style",
      on: { input: (e) => { st.custom = e.target.value; paintFoot(); } } });
    const styleRow = h("div.art-chips", { role: "group", "aria-label": "Style" },
      STYLES.map(([slug, label]) => chip(label, st.style === slug, () => { st.style = slug; paint(); })),
      chip("Custom…", st.style === "custom", () => { st.style = "custom"; paint(); body.querySelector(".art-custom")?.focus(); }));
    const seg = (label, list, value, set) => h("div.seg", { role: "group", "aria-label": label },
      list.map(([k, l]) => h("button", { type: "button", "aria-pressed": String(value === k), on: { click: () => { set(k); paint(); } } }, l)));
    const levels = levelsOf(st.provider);
    if (!levels.some(([k]) => k === st.quality)) st.quality = levels[levels.length > 1 ? 1 : 0][0];
    const provSel = provs.length > 1 ? h("select.select.art-prov", { "aria-label": "Draw with", on: { change: (e) => { st.provider = provs.find((p) => p.id === e.target.value) || provs[0]; paint(); } } },
      provs.map((p) => h("option", { value: p.id, selected: p.id === st.provider.id }, p.label))) : null;
    const moments = (opts.moments || []).length > 1 ? h("div.art-field",
      h("span.label", "Moment"),
      h("div.art-moments", { role: "radiogroup", "aria-label": "Moment to draw" }, opts.moments.map((m, n) => {
        const radio = h("input", { type: "radio", name: "art-moment", checked: m.kind === st.kind && m.target === st.target,
          on: { change: () => { st.kind = m.kind; st.target = m.target; st.shape = SHAPE_OF[m.kind] || st.shape; write(); } } });
        return h(`label.art-moment${m.kind === st.kind && m.target === st.target ? ".on" : ""}`, radio, h("span", m.label || (n ? "A moment" : "The chapter's main moment")));
      }))) : null;
    clear(body,
      opts.drawnLine ? h("p.art-drawn.subtle", opts.drawnLine) : null,
      moments,
      h("div.art-field", h("label.label", { for: "art-prompt" }, "Prompt"), textarea,
        h("p.hint", "Written from what you have read so far. Change anything; the picture is drawn from this text only. ",
          h("button.linkish", { type: "button", on: { click: write } }, "Write it again"))),
      h("div.art-field", h("span.label", "Style"), styleRow, st.style === "custom" ? customField : null),
      h("div.art-row",
        h("div.art-field", h("span.label", "Shape"), seg("Shape", SHAPES, st.shape, (k) => { st.shape = k; })),
        h("div.art-field", h("span.label", "Quality"), seg("Quality", levels, st.quality, (k) => { st.quality = k; })),
        provSel ? h("div.art-field", h("label.label", "Draw with"), provSel) : null),
      opts.world ? worldLine() : null,
      err);
    textarea.id = "art-prompt";
    paintFoot();
  }
  function worldLine() {
    const text = h("span.art-world-text", opts.world.text);
    const line = h("p.art-world", h("span.subtle", "World: "), text, " ",
      opts.canEditWorld ? h("button.linkish", { type: "button", on: { click: edit } }, "Change") : null);
    function edit() {
      const field = h("textarea.input", { rows: "3", "aria-label": "World note", value: opts.world.text });
      field.value = opts.world.text;
      const ferr = h("p.hint.field-error", { role: "alert" });
      const save = h("button.btn", { type: "button", on: { click: async () => {
        save.disabled = true;
        try {
          await api.artWorld(opts.key, field.value.trim());
          opts.world = { text: field.value.trim() };
          forgetArt(opts.key);
          toast("World note saved", "ok", { host: sh.toasts });
          line.replaceWith(worldLine());
        } catch (e) { ferr.textContent = e.message; save.disabled = false; }
      } } }, "Save");
      const cancel = h("button.btn.quiet", { type: "button", on: { click: () => box.replaceWith(worldLine()) } }, "Cancel");
      const box = h("div.art-field.art-world-edit", h("span.label", "World"), field,
        h("p.hint", "Pasted into every picture of this book, so places and clothing keep to one world."), ferr, h("div.btns", cancel, save));
      line.replaceWith(box);
      field.focus();
    }
    return line;
  }
  function paintFoot() {
    const ok = st.prompt.trim().length > 0 && Boolean(styleSlug()) && !st.busy;
    drawBtn.disabled = !ok;
    footNote.textContent = st.busy ? "" : st.price != null ? `${money(st.price)[0].toUpperCase()}${money(st.price).slice(1)} for this picture` : "Price unknown";
  }

  drawBtn.addEventListener("click", async () => {
    if (st.busy) return;
    st.busy = true; drawBtn.disabled = true;
    const keep = [...body.childNodes];
    clear(body, loading("Drawing the picture. This takes a few seconds…"));
    footNote.textContent = "";
    try {
      const r = await api.artDraw(opts.key, { kind: st.kind, target: st.target, style: styleSlug(), shape: st.shape, quality: st.quality,
        prompt: st.prompt, provider: st.provider.id });
      st.busy = false;
      st.written = st.prompt;   // drawn: nothing to ask about on close
      forgetArt(opts.key);
      const rec = r?.record || r;
      const src = opts.srcFor ? opts.srcFor(rec, styleSlug()) : null;
      clear(body, h("div.art-done", src ? artImg(src, opts.title, "art-done-img", { eager: true }) : null,
        h("p", "Drawn and added to the art set."),
        h("div.btns", h("button.btn.primary", { type: "button", on: { click: () => sh.close(true) } }, "Done"))));
      drawBtn.hidden = true; footNote.textContent = typeof rec?.cost === "number" && rec.cost > 0 ? `Cost ${money(rec.cost).replace(/^about /, "")}` : "";
      opts.onDrawn?.(rec, styleSlug());
    } catch (e) {
      st.busy = false;
      clear(body, keep);
      clear(err, h("p", e.code === "refused" ? `The provider would not draw this: ${e.message}. Change the prompt and try again.` : `Couldn't draw it: ${e.message}`));
      paintFoot();
      textarea.focus();
    }
  });
  write();
  return sh;
}

// ---- the Pictures group of the build form and the book page --------------------------------------

/**
 * The "Pictures" choices, as one closed disclosure (`details`) or as a plain block for a dialog. Returns
 * { el, config() → the job's `art` object or null when "Draw pictures" is off, refresh() }. `estimate(params)` is
 * asked for the line "About 52 pictures, about $1.75"; `fallback()` gives a rough count when it cannot answer.
 */
export function picturesForm({ estimate, fallback = null, open = false, always = false, idPrefix = "art" } = {}) {
  const d = artDefaults();
  const st = { on: always, style: STYLE_LABEL[d.style] ? d.style : "custom", custom: STYLE_LABEL[d.style] ? "" : d.style, level: "main", portraits: true,
    quality: d.quality, provider: null, cap: d.cap };
  let provs = null;
  const line = h("p.art-est", { role: "status" });
  const body = h("div.art-form-body");
  const tick = h("input", { type: "checkbox", role: "switch", id: `${idPrefix}-on`, checked: st.on });
  tick.addEventListener("change", () => { st.on = tick.checked; paint(); });
  const sum = h("span.sum-hint");
  const el = always ? h("div.art-form", body, line)
    : h("details.advanced.art-group", { open: open || null }, h("summary", icon("chevron"), "Pictures", sum), h("div.advanced-body", body, line));
  imageProviders().then((list) => { provs = list; st.provider = list.find((p) => p.id === d.provider) || list[0] || null; paint(); });
  const slug = () => (st.style === "custom" ? st.custom.trim() : st.style);
  function paint() {
    sum.textContent = st.on ? `${styleLabel(slug())}, ${st.level === "main" ? "one per chapter" : "every moment"}${st.portraits ? ", portraits" : ""}` : "Off";
    if (provs && !provs.length) {
      clear(body, h("p.hint", "Pictures need a Gemini or OpenAI key. ", h("a", { href: "#/settings/cloud" }, "Add one in Settings")));
      line.textContent = "";
      return;
    }
    const chip = (label, pressed, on) => h("button.filter-chip", { type: "button", "aria-pressed": String(pressed), disabled: !st.on, on: { click: on } }, label);
    const radio = (name, value, label, hint) => {
      const r = h("input", { type: "radio", name: `${idPrefix}-${name}`, value, checked: st[name] === value, disabled: !st.on,
        on: { change: () => { st[name] = value; paint(); } } });
      return h(`label.prow${st[name] === value ? ".on" : ""}`, r, h("span.pr-text", h("span.pr-title", label), hint ? h("span.pr-line", hint) : null));
    };
    const custom = h("input.input.art-custom", { value: st.custom, disabled: !st.on, placeholder: "Describe the style", "aria-label": "Custom style",
      on: { change: (e) => { st.custom = e.target.value; refresh(); } } });
    const portraits = h("input", { type: "checkbox", id: `${idPrefix}-portraits`, checked: st.portraits, disabled: !st.on, on: { change: (e) => { st.portraits = e.target.checked; paint(); } } });
    const levels = st.provider ? levelsOf(st.provider) : QUALITIES;
    if (!levels.some(([k]) => k === st.quality)) st.quality = levels[0][0];
    const cap = h("input.input.w-90", { type: "number", min: "1", step: "1", id: `${idPrefix}-cap`, value: String(st.cap), disabled: !st.on, inputmode: "decimal",
      on: { change: (e) => { st.cap = Math.max(1, +e.target.value || 10); refresh(); } } });
    clear(body,
      always ? null : h("label.switch", { for: `${idPrefix}-on` }, tick, h("span", h("span.label.label-block", "Draw pictures"),
        h("span.hint", "Chapter pictures and portraits, drawn after the index is written."))),
      h("div.field", h("span.label", "Style"), h("div.art-chips", { role: "group", "aria-label": "Style" },
        STYLES.map(([k, l]) => chip(l, st.style === k, () => { st.style = k; paint(); })),
        chip("Custom…", st.style === "custom", () => { st.style = "custom"; paint(); el.querySelector(".art-custom")?.focus(); })),
        st.style === "custom" ? custom : null),
      h("div.field", h("span.label", "How many"), h("div.prows", { role: "radiogroup", "aria-label": "How many" },
        radio("level", "main", "The main moment of each chapter"),
        radio("level", "all", "Every moment worth drawing"))),
      h("label.switch", { for: `${idPrefix}-portraits` }, portraits, h("span", h("span.label.label-block", "Portraits of the characters"))),
      h("div.field", h("span.label", "Quality"), h("div.seg", { role: "group", "aria-label": "Quality" },
        levels.map(([k, l]) => h("button", { type: "button", "aria-pressed": String(st.quality === k), disabled: !st.on, on: { click: () => { st.quality = k; paint(); } } }, l)))),
      provs && provs.length > 1 ? h("div.field", h("label", { for: `${idPrefix}-prov` }, "Draw with"),
        h(`select.select#${idPrefix}-prov`, { disabled: !st.on, on: { change: (e) => { st.provider = provs.find((p) => p.id === e.target.value); paint(); } } },
          provs.map((p) => h("option", { value: p.id, selected: p.id === st.provider?.id }, `${p.label}, ${imageLine(p, st.quality)}`)))) : null,
      provs && provs.length === 1 ? h("p.hint", `Drawn with ${provs[0].label}: ${imageLine(provs[0], st.quality)}.`) : null,
      h("div.field.art-cap", h("label", { for: `${idPrefix}-cap` }, "Stop at"), h("span.art-cap-row", "$", cap),
        h("p.hint", "The run stops drawing there and keeps what it drew.")));
    refresh();
  }
  let seq = 0;
  async function refresh() {
    if (!st.on || !st.provider) { line.textContent = st.on && provs && !provs.length ? "" : ""; return; }
    const mine = ++seq;
    line.textContent = "Counting the pictures…";
    let r = null;
    try { r = await estimate?.({ style: slug(), level: st.level, portraits: st.portraits, quality: st.quality, provider: st.provider.id }); } catch { r = null; }
    if (mine !== seq) return;
    if (r && Number.isFinite(+r.pictures)) {
      const n = +r.pictures;
      line.textContent = `About ${n} picture${n === 1 ? "" : "s"}, ${money(r.price)}${r.price != null && +r.price > st.cap ? `. The run stops at $${st.cap}` : ""}.`;
    } else {
      const rough = fallback?.(st);
      line.textContent = rough || "The count is worked out when the index is written.";
    }
  }
  paint();
  return {
    el,
    config: () => (st.on && st.provider && slug() ? { style: slug(), level: st.level, portraits: st.portraits, quality: st.quality,
      provider: st.provider.id, model: st.provider.images?.default || null, cap: st.cap } : null),
    refresh,
    ready: () => Boolean(st.provider && slug()),
  };
}
