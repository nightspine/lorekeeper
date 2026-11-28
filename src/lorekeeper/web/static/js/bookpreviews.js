// The book page's "Style previews" row: the book's own previews of every style (one scene from its first two story
// chapters, drawn per image provider; web/previews.py), what they cost to draw or redraw, and another opening scene.
//
// Everything said comes from the previews route: the scene's chapter by its place among the story chapters, and the
// opening moments' summaries in the scene sheet (index content, shown through h() only). The route only ever offers
// moments of the first two story chapters.
import { api } from "./api.js";
import { money } from "./artestimate.js";
import { confirmDialog } from "./build.js";
import { clear, h, sheet, toast } from "./dom.js";
import { bookPreviews, forgetBookPreviews, providerName, sceneChapter } from "./styles.js";

const ACTIVE = ["running", "queued", "stopping"];
const isPreviewsJob = (j, key) => j && (j.kind === "previews" || j.config?.kind === "previews") && (j.index === key || j.config?.index_key === key);
/** "Gemini and OpenAI", "Gemini, OpenAI and X". */
const listing = (names) => (names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`);
const cap1 = (t) => t.charAt(0).toUpperCase() + t.slice(1);

/**
 * The row for the index `key`: returns { el, dispose() }. `row(label, value, action)` builds the page's label / value row.
 * It fills in once the previews answer and keeps itself up to date while a previews run draws (every 5 s).
 */
export function previewsRow(key, row) {
  const value = h("div.pv-state", h("span.subtle", "Looking…"));
  const actions = h("div.pv-acts");
  const el = row("Style previews", value, actions);
  let timer = null, gone = false, sig = "";

  async function load(fresh = false) {
    clearTimeout(timer);
    const data = await bookPreviews(key, { fresh });
    if (gone) return;
    if (!data) { el.remove(); return; }   // an older server, or the index has none of this: the page says nothing
    const have = Object.keys(data.providers);
    // While a run draws: its progress, from the run itself.
    let job = null;
    if (data.drawing) {
      try { job = (await api.jobs()).find((j) => isPreviewsJob(j, key) && ACTIVE.includes(j.status)) || null; } catch {}
      if (gone) return;
    }
    const plain = data.drawing ? null : await api.previewsEstimate(key, { redraw: false }).catch(() => null);
    const again = data.drawing || !have.length ? null : await api.previewsEstimate(key, { redraw: true }).catch(() => null);
    if (gone) return;
    const next = JSON.stringify([data, job?.id, job?.progress?.previews, plain, again]);
    if (next !== sig) { sig = next; paint(data, have, job, plain, again); }
    if (data.drawing) timer = setTimeout(() => load(true), 5000);
  }

  function paint(data, have, job, plain, again) {
    const focus = el.contains(document.activeElement) ? document.activeElement.dataset.act : null;
    const pr = Array.isArray(job?.progress?.previews) ? job.progress.previews : null;
    const sentence = data.drawing
      ? (pr && +pr[1] ? `Drawing, ${+pr[0] || 0} of ${+pr[1]}.` : "Drawing.")
      : have.length && data.scene ? `Drawn for ${listing(have.map(providerName))} from a scene in ${sceneChapter(data.scene)}.`
        : "None yet.";
    const canDraw = (plain?.providers || []).length > 0;
    clear(value,
      h("span", sentence),
      data.drawing && job ? [" ", h("a", { href: `#/studio/job/${encodeURIComponent(job.id)}` }, "View progress")] : null,
      !data.drawing && !data.moments.length ? h("span.subtle.pv-note", "The first chapters have no moments to draw.") : null,
      !data.drawing && data.moments.length && plain && !canDraw ? h("span.subtle.pv-note", "Add a Gemini or OpenAI key in ",
        h("a", { href: "#/settings/cloud" }, "Settings"), " to draw them.") : null);
    const btn = (act, label, cls, on, title = null) => h(`button.${cls}`, { type: "button", dataset: { act }, title, on: { click: on } }, label);
    const acts = [];
    if (!data.drawing && data.moments.length && canDraw) {
      if (!have.length) acts.push(btn("draw", "Draw style previews", "btn", () => draw({})),
        h("span.subtle.pv-price", `${cap1(money(plain.price))}.`));
      else {
        acts.push(btn("redraw", "Redraw", "btn", () => draw({ redraw: true })), again ? h("span.subtle.pv-price", `${cap1(money(again.price))}.`) : null);
        if (plain.pictures > 0) acts.push(btn("missing", `Draw the ${plain.pictures} missing`, "linkish", () => draw({})));
      }
      acts.push(btn("scene", "Choose another scene", "linkish", () => chooseScene(data)));
    }
    if (!data.drawing && have.length) acts.push(btn("remove", "Remove previews", "linkish.danger", remove));
    clear(actions, acts);
    if (focus) (actions.querySelector(`[data-act="${focus}"]`) || actions.querySelector("button"))?.focus({ preventScroll: true });
  }

  async function draw(body) {
    try {
      await api.previewsDraw(key, body);
      toast("Drawing style previews");
      forgetBookPreviews(key);
      sig = "";
      load(true);
      return true;
    } catch (e) { toast(e.message, "error"); return false; }
  }

  async function remove() {
    if (!(await confirmDialog({ title: "Remove the style previews?", message: "The style cards for this book go back to the shared scene. You can draw them again later.",
      confirmLabel: "Remove previews" }))) return;
    try { await api.previewsRemove(key); toast("Style previews removed"); forgetBookPreviews(key); sig = ""; load(true); }
    catch (e) { toast(e.message, "error"); }
  }

  /** A sheet listing the opening moments (chapter and summary, the one drawn now marked) and Draw with its price. */
  function chooseScene(data) {
    const now = data.scene?.target || null;
    let pick = data.moments.some((m) => m.target === now) ? now : data.moments[0]?.target;
    const price = h("p.hint.pv-sheet-price", { role: "status" });
    const go = h("button.btn.primary", { type: "button" }, "Draw");
    const cancel = h("button.btn", { type: "button" }, "Cancel");
    let asked = 0;
    async function estimate() {
      const n = ++asked;
      price.textContent = "Working out the price…";
      const same = pick === now;
      const r = await api.previewsEstimate(key, { scene: pick, redraw: same }).catch((e) => ({ error: e.message }));
      if (n !== asked || !sh.el.open) return;
      if (r.error) { price.textContent = r.error; return; }
      price.textContent = `${r.pictures} picture${r.pictures === 1 ? "" : "s"}, ${money(r.price)}.`;
    }
    const rows = h("div.prows", { role: "radiogroup", "aria-label": "Scenes" }, data.moments.map((m, i) => {
      const r = h("input", { type: "radio", name: "pv-scene", value: m.target, id: `pv-scene-${i}`, checked: m.target === pick,
        on: { change: () => { if (!r.checked) return; pick = m.target; rows.querySelectorAll(".prow").forEach((x) => x.classList.toggle("on", x.contains(r))); estimate(); } } });
      return h(`label.prow${m.target === pick ? ".on" : ""}`, { for: `pv-scene-${i}` }, r,
        h("span.pr-text", h("span.pr-title", sceneChapter(m), m.target === now ? h("span.pv-now", "Drawn now") : null),
          m.summary ? h("span.pr-line", m.summary) : null));
    }));
    const sh = sheet({ title: "Choose a scene",
      content: [h("p.hint.pv-sheet-lede", "Scenes from the book's first two chapters, so the previews show nothing past its opening."), rows],
      footer: h("div.pv-sheet-foot", price, h("div.confirm-actions", cancel, go)), className: "pv-sheet" });
    cancel.addEventListener("click", () => sh.close());
    go.addEventListener("click", async () => {
      go.disabled = true;
      if (await draw(pick === now ? { scene: pick, redraw: true } : { scene: pick })) sh.close(true);
      else go.disabled = false;
    });
    estimate();
    requestAnimationFrame(() => rows.querySelector("input:checked")?.focus({ preventScroll: true }));
  }

  load();
  return { el, dispose: () => { gone = true; clearTimeout(timer); } };
}
