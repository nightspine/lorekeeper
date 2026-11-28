// Settings: sections on the left, the chosen one on the right, each at its own address (#/settings/<section>).
// Every section says at most one sentence up front; the rest of what it used to explain sits behind one
// "How this works" disclosure. Server text (provider errors, library names) only ever goes through h().
import { api } from "./api.js";
import { onOllama, parseHash, replaceHash, showHash, signOut, state } from "./app.js";
import { PASSWORD_RULE, passwordForm } from "./auth.js";
import { confirmDialog } from "./build.js";
import { clear, fmtInt, h, icon, relTime, toast } from "./dom.js";
import { CHECK_COST } from "./artfix.js";
import { artDefaults, drawsPictures, fitQuality, imageLine, imageModels, imagePrice, levelsOf, loadStyles, modelFor, modelProviders, money as aboutMoney, portraitImageSelect, QUALITIES, setArtDefaults, STYLES, styleLabel, stylePicker, writerSelect } from "./art.js";

const gb = (bytes) => (bytes ? `${(bytes / 1e9).toFixed(1)} GB` : "");
// A 100,000-word novel at standard depth, measured on a fact-checked run: 1.2 input and 0.58 output tokens per word.
const perBook = (m) => (m?.price ? (100000 * (1.2 * m.price[0] + 0.58 * m.price[1])) / 1e6 : null);
const money = (c) => `$${c.toFixed(c < 1 ? 3 : 2)}`;
const hostOf = (url) => { try { return new URL(url).host.replace(/^www\./, ""); } catch { return url; } };
/** "Claude", "Claude and OpenAI", "Claude, OpenAI and Gemini" */
const listing = (names) => (names.length < 3 ? names.join(" and ") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`);

/** One "How this works" disclosure per section, closed by default. */
/** Why a provider with a key is missing from the writer selects: "Claude's key didn't work, so its models are not offered
 *  here." (`others`: some other provider still writes). */
function brokenKeys(broken, others) {
  const names = broken.map((p) => p.label);
  const rest = others ? "not offered here" : "not offered, and nothing can write prompts yet";
  return names.length === 1 ? `${names[0]}'s key didn't work, so its models are ${rest}.`
    : `The keys for ${listing(names)} didn't work, so their models are ${rest}.`;
}
function howItWorks(...paragraphs) {
  return h("details.how", h("summary", icon("chevron"), "How this works"), h("div.how-body", paragraphs.map((p) => h("p", p))));
}
/** A label / value / action row on a hairline. */
function row(label, value, action = null, { top = false } = {}) {
  return h(`div.set-row${top ? ".top" : ""}`, h("span.set-lbl", label), h("div.set-val", value), action ? h("div.set-act", action) : null);
}
/** A state is a mark and a word, never colour alone. */
function state_(kind, text) {
  const mark = { ok: "check", bad: "alert", off: null, busy: null }[kind];
  return h(`span.st.st-${kind}`, mark ? icon(mark) : h("span.st-dot", { "aria-hidden": "true" }), text);
}

export function mountSettings(root) {
  const user = state.session.auth ? state.session.user : null;
  const SECTIONS = [
    user && { id: "account", name: "Account" },
    { id: "bookorbit", name: "Book Orbit" },
    { id: "cloud", name: "Cloud models" },
    { id: "local", name: "Local models" },
    { id: "pictures", name: "Pictures" },
    { id: "community", name: "Community indexes" },
  ].filter(Boolean);
  const ids = SECTIONS.map((x) => x.id);
  const asked = parseHash().parts[0];
  let explicit = ids.includes(asked) ? asked : null;
  let current = explicit || "cloud";
  let disposed = false;

  const lines = {}, links = {}, panes = {};
  const nav = h("nav.set-nav", { "aria-label": "Settings sections" }, SECTIONS.map((sec) => {
    lines[sec.id] = h("span.set-state", sec.id === "account" ? user.name : "Checking…");
    links[sec.id] = h("a.set-link", { href: `#/settings/${sec.id}`, "data-sec": sec.id,
      on: { click: (e) => { if (e.button || e.metaKey || e.ctrlKey || e.shiftKey) return; e.preventDefault(); go(sec.id); } } },
      h("span.set-name", sec.name), lines[sec.id]);
    return links[sec.id];
  }));
  // The server's version as one quiet line at the foot of the list; nothing when it can't be read (an older server).
  const version = h("p.set-version", { hidden: true });
  nav.append(version);
  api.about().then((d) => { if (d && !disposed) { version.textContent = `Lorekeeper ${d.version}`; version.hidden = false; } });
  // Arrow keys move between sections (and show them), Home and End jump to the ends.
  nav.addEventListener("keydown", (e) => {
    const i = ids.indexOf(e.target.dataset?.sec);
    if (i < 0) return;
    const to = { ArrowDown: i + 1, ArrowRight: i + 1, ArrowUp: i - 1, ArrowLeft: i - 1, Home: 0, End: ids.length - 1 }[e.key];
    if (to == null) return;
    e.preventDefault();
    const id = ids[(to + ids.length) % ids.length];
    go(id, { replace: true });   // stepping through the list does not stack history entries
    links[id].focus();
  });

  const back = h("a.set-back", { href: "#/settings", on: { click: (e) => { e.preventDefault(); explicit = null; showHash("#/settings"); show(); links[current]?.focus(); } } },
    icon("chevron-left"), "Settings");
  const body = h("div.set-body", back);
  for (const sec of SECTIONS) {
    panes[sec.id] = h("section.sec", { id: `sec-${sec.id}`, "aria-labelledby": `sec-h-${sec.id}`, hidden: true });
    body.append(panes[sec.id]);
  }
  const page = h("div.settings", h("header.page-head", h("h1", "Settings")), h("div.set", nav, body));
  root.append(page);

  function go(id, { replace = false } = {}) {
    const url = `#/settings/${id}`;
    if (replace) replaceHash(url); else if (location.hash !== url) showHash(url);
    explicit = id;
    current = id;
    show();
  }
  function show() {
    // On a phone the list is its own page: with no section in the address only the list shows.
    page.classList.toggle("picking", !explicit);
    for (const id of ids) {
      panes[id].hidden = id !== current;
      if (id === current) links[id].setAttribute("aria-current", "page"); else links[id].removeAttribute("aria-current");
    }
  }
  show();

  function head(id, title, sentence) {
    return h("div.sec-head", h("h2", { id: `sec-h-${id}` }, title), sentence ? h("p.sec-lede", sentence) : null);
  }

  // ---- Account ------------------------------------------------------------------------------
  if (user) {
    clear(panes.account,
      head("account", "Account"),
      h("div.set-rows",
        row("Signed in as", h("strong", user.name), h("button.btn", { type: "button", on: { click: () => signOut() } }, "Sign out")),
        row("Change password", h("div.account-form", passwordForm({ username: user.name, ruleHint: false })), null, { top: true })),
      howItWorks(`A password is ${PASSWORD_RULE}, and it must differ from the current one. Changing it signs you out everywhere else.`,
        "Forgot it? Run lorekeeper reset-password on the server: it prints a new one and ends every session."));
  }

  // ---- Community indexes ---------------------------------------------------------------------
  async function paintCommunity(focusId) {
    let c;
    try { c = await api.communityConfig(); } catch (e) {
      if (disposed) return;
      lines.community.textContent = "Couldn't check";
      return clear(panes.community, head("community", "Community indexes"), h("p.set-note", e.message));
    }
    if (disposed) return;
    lines.community.textContent = c.enabled === true ? (c.default ? "On" : "On, another registry") : c.enabled === false ? "Off" : "Not asked yet";
    const on = h("input", { type: "checkbox", role: "switch", id: "community-on", checked: c.enabled === true });
    on.addEventListener("change", async () => {
      on.disabled = true;
      try { await api.setCommunity(on.checked); toast(on.checked ? "Community indexes are on" : "Community indexes are off"); }
      catch (e) { toast(e.message, "error"); }
      paintCommunity("community-on");
    });
    const url = h("input.input", { id: "community-url", type: "url", value: c.url, spellcheck: "false", autocomplete: "off" });
    const save = h("button.btn", { type: "submit" }, "Save address");
    clear(panes.community,
      head("community", "Community indexes", "Ready-made indexes other people shared for the book you are looking at."),
      h("form.set-rows", { on: { submit: async (e) => {
        e.preventDefault();
        save.disabled = true;
        try { await api.setCommunity(undefined, url.value.trim()); toast("Registry address saved"); }
        catch (ex) { toast(ex.message, "error"); }
        paintCommunity("community-url");
      } } },
      row(h("label", { for: "community-on" }, "Look for them"), h("span.switch", on,
        h("span", c.enabled === true ? "On" : c.enabled === false ? "Off, nothing is sent" : "Not asked yet"))),
      h("div.set-row.form-row", h("label.set-lbl", { for: "community-url" }, "Registry address"), h("div.set-val", url,
        c.default ? null : h("p.hint", "This is not the project's registry. You are trusting its owner to list indexes.")), h("div.set-act", save))),
      howItWorks("Lorekeeper asks the registry about a book when you open its build page, and sends a fingerprint of the book's text, nothing else. Off means nothing is sent.",
        "An index you add is checked against your own copy first. Indexes you already added stay in your library when this is off."));
    if (focusId) document.getElementById(focusId)?.focus();
  }
  paintCommunity();

  // ---- Local models (Ollama) --------------------------------------------------------------------
  // Built once; each 15-second poll repaints only the status rows and the model table, so the open
  // "How this works" and the keyboard focus survive it.
  const ollamaLede = h("p.sec-lede", "Free and private, slower: a novel takes a few hours.");
  const ollamaUrl = document.createTextNode("http://localhost:11434");
  const ollamaBody = h("div.set-local", h("p.set-note", "Checking Ollama…"));
  clear(panes.local,
    h("div.sec-head", h("h2", { id: "sec-h-local" }, "Local models"), ollamaLede),
    ollamaBody,
    howItWorks(h("span", "Lorekeeper talks to the Ollama server at ", ollamaUrl, ". To use another one, start Lorekeeper with --ollama-url."),
      "Add a model with ollama pull. One model runs at a time; builds wait in a queue."));
  const paintOllama = (o) => {
    lines.local.textContent = o.ok ? "Ollama running" : "Ollama not running";
    const url = o.url || "http://localhost:11434";
    ollamaUrl.textContent = url;
    clear(ollamaBody,
      h("div.set-rows",
        row("Ollama", o.ok ? h("span", state_("ok", "Running"), " at ", h("span.num", url))
          : h("span", state_("bad", "Not running"), " at ", h("span.num", url))),
        o.ok ? null : row("To start it", h("span", h("span.mono", "brew services start ollama"), ", or install it from ollama.com."))),
      o.ok
        ? (o.models.length
          ? h("div.table-wrap.set-table", h("table.data",
            h("caption.sr-only", "Models installed in Ollama"),
            h("thead", h("tr", h("th", { scope: "col" }, "Model"), h("th.r", { scope: "col" }, "Parameters"), h("th.r", { scope: "col" }, "Size"), h("th", { scope: "col" }, "State"))),
            h("tbody", o.models.map((m) => h("tr",
              h("th.model-name", { scope: "row" }, m.name),
              h("td.r.muted", m.parameters || ""),
              h("td.r.muted", gb(m.size)),
              h("td", o.loaded.includes(m.name) ? state_("ok", "Loaded") : state_("off", "On disk")))))))
          : h("p.set-note", "No models installed yet. Try ", h("span.mono", "ollama pull gemma4:12b-it-qat"), "."))
        : null);
  };
  const off = onOllama((o) => { if (!disposed) paintOllama(o); });

  // ---- Cloud models: one table, a row per provider ------------------------------------------------
  const ui = {};        // per provider: { editing: bool, expanded: bool }
  const rows = {};      // per provider: the <tr> elements on screen
  const known = {};     // per provider: the last status from the server
  const tbody = h("tbody");
  let cloudLede = h("p.sec-lede", "Fast, on your own key, a few cents a book.");

  function cloudLine() {
    const all = Object.values(known);
    const ok = all.filter((p) => p.ok).map((p) => p.label);
    const broken = all.filter((p) => p.configured && !p.ok).map((p) => p.label);
    lines.cloud.textContent = ok.length ? listing(ok) : broken.length ? `${listing(broken)} not working` : "Not set up";
    cloudLede.textContent = `Fast, on your own key, a few cents a book: ${ok.length ? `${ok.length} of ${all.length}` : "none"} set up.`;
  }

  function providerRows(p) {
    const u = ui[p.id] ||= { editing: false, expanded: false };
    const custom = p.id === "custom";
    const def = p.models.find((m) => m.id === p.default);
    const cost = perBook(def);
    const where = p.source === "settings" ? "saved here" : p.source === "environment" ? `from ${p.key_env}` : "";
    const removable = p.source === "settings" || (custom && p.configured);
    const cells = {};

    // Status: a mark and a word.
    cells.status = u.editing ? state_("busy", p.configured ? "Replacing the key" : "Adding a key")
      : p.ok ? state_("ok", "Connected")
        : p.configured ? state_("bad", "Not working")
          : state_("off", "Not set up");

    // Key: the masked key and where it lives, or the field while editing.
    let first = null, actions;
    if (u.editing) {
      let showing = false;
      const formId = `prov-form-${p.id}`;
      const input = h("input.input", { id: `key-${p.id}`, type: "password", autocomplete: "off", spellcheck: "false", form: formId,
        placeholder: p.source ? "Paste a new key" : custom ? "Only if the server needs one" : "Paste your API key" });
      const urlInput = custom && h("input.input", { id: "key-custom-url", type: "url", value: p.base_url || "", placeholder: "http://localhost:1234/v1", form: formId });
      const modelInput = custom && h("input.input", { id: "key-custom-model", value: p.model || "", placeholder: "The model name the server uses", form: formId });
      const err = h("p.hint.field-error", { role: "alert", id: `err-${p.id}` });
      const reveal = h("button.icon-btn", { type: "button", "aria-label": "Show key", title: "Show key", "aria-pressed": "false", "aria-controls": `key-${p.id}`,
        on: { click: () => {
          showing = !showing;
          input.type = showing ? "text" : "password";
          reveal.setAttribute("aria-pressed", String(showing));
          reveal.setAttribute("aria-label", showing ? "Hide key" : "Show key");
          reveal.title = showing ? "Hide key" : "Show key";
        } } }, icon("eye"));
      const save = h("button.btn.primary", { type: "submit", form: formId }, "Save and test");
      const cancel = h("button.btn.quiet", { type: "button", on: { click: () => { u.editing = false; paintRow(p.id, "action"); } } }, "Cancel");
      const form = h("form.keyform", { id: formId, novalidate: true, on: { submit: async (e) => {
        e.preventDefault();
        err.textContent = "";
        input.removeAttribute("aria-invalid");
        if (!custom && !input.value.trim()) {
          err.textContent = "Paste your API key first.";
          input.setAttribute("aria-invalid", "true");
          input.focus();
          return;
        }
        save.disabled = true; save.setAttribute("aria-busy", "true"); save.textContent = "Testing…";
        try {
          await api.setProvider(p.id, { key: input.value.trim(), ...(custom ? { base_url: urlInput.value.trim(), model: modelInput.value.trim() } : {}) });
          toast(`${p.label} is connected`);
          u.editing = false;
          await paintProvider(p.id, "action");
        } catch (ex) {
          err.textContent = ex.message;
          input.setAttribute("aria-invalid", "true");
          save.disabled = false; save.removeAttribute("aria-busy"); save.textContent = "Save and test";
        }
      } } });
      const keyHint = p.key_url
        ? h("p.hint", "Get one at ", h("a", { href: p.key_url, target: "_blank", rel: "noopener noreferrer" }, hostOf(p.key_url)),
          p.source === "environment" ? `. A key saved here takes priority over ${p.key_env}.` : ". It is tested before it is saved.")
        : null;
      const describe = [keyHint && `hint-${p.id}`, `err-${p.id}`].filter(Boolean).join(" ");
      input.setAttribute("aria-describedby", describe);
      if (keyHint) keyHint.id = `hint-${p.id}`;
      first = custom ? urlInput : input;
      cells.key = h("div.keycell", form,
        custom ? h("div.keyfield-stack",
          h("label.label", { for: "key-custom-url" }, "Address"), urlInput,
          h("label.label", { for: "key-custom-model" }, "Model"), modelInput,
          h("label.label", { for: `key-${p.id}` }, "API key, optional")) : h("label.sr-only", { for: `key-${p.id}` }, `${p.label} API key`),
        h("div.keyfield", input, reveal),
        keyHint, err);
      actions = [cancel, save];
    } else {
      cells.key = custom && !p.configured ? h("span.subtle", "Address, model and an optional key")
        : p.configured ? h("div.keycell",
          h("span.key", custom ? h("code", p.base_url) : null, p.masked ? h("code", p.masked) : null, where ? h("span.subtle", where) : null),
          !p.ok && p.error ? h("p.prov-error", p.error) : null)
          : null;
      const label = custom && !p.configured ? "Set up" : "Add key";
      actions = p.configured
        ? [h("button.btn.quiet.prov-act", { type: "button", "aria-expanded": String(u.expanded), "aria-controls": `prov-more-${p.id}`,
          on: { click: () => { u.expanded = !u.expanded; paintRow(p.id, "action"); } } }, u.expanded ? "Hide models" : (custom ? "Show settings" : "Show models"))]
        : [h("button.btn.prov-act", { type: "button", "aria-label": `${label} for ${p.label}`,
          on: { click: () => { u.editing = true; paintRow(p.id, "field"); } } }, label)];
    }

    const modelText = custom ? (p.model || "Any model it serves") : (def?.label || p.default || "");
    const tr = h(`tr.prov${u.editing ? ".open" : ""}${u.expanded && !u.editing ? ".expanded" : ""}`, { "data-prov": p.id },
      h("th.pn", { scope: "row" }, p.label),
      h("td.ps", cells.status),
      h("td.pk", cells.key),
      h(`td.pm${p.ok ? "" : ".subtle"}`, modelText),
      // Images: the provider's image model and its price for one picture, or "Can't draw" (Claude and the rest).
      h(`td.pi${p.ok && drawsPictures(p) ? "" : ".subtle"}`, drawsPictures(p) ? imageLine(p, artDefaults().quality) : "Can't draw"),
      h(`td.pp.r${p.ok ? "" : ".subtle"}`, cost != null ? money(cost) : "Unknown"),
      h("td.pa", h("div.act", actions)));
    const out = [tr];

    if (p.configured && u.expanded && !u.editing) {
      const replace = h("button.btn", { type: "button", on: { click: () => { u.editing = true; paintRow(p.id, "field"); } } }, custom ? "Change settings" : "Replace key");
      const remove = removable && h("button.btn.danger", { type: "button", on: { click: async () => {
        if (!await confirmDialog({ title: `Remove the saved ${p.label} settings?`,
          message: "The key is deleted from lorekeeper-data/secrets.json on this machine. Runs on this provider stop working until you add one again.",
          confirmLabel: "Remove" })) return;
        try { await api.deleteProvider(p.id); toast("Removed"); u.expanded = false; await paintProvider(p.id, "action"); }
        catch (ex) { toast(ex.message, "error"); }
      } } }, custom ? "Remove settings" : "Remove key");
      const priced = p.models.filter((m) => m.price);
      out.push(h("tr.prov-more", { id: `prov-more-${p.id}` }, h("td", { colspan: "7" },
        priced.length ? h("table.data.subt",
          h("caption.sr-only", `${p.label} models and prices`),
          h("thead", h("tr", h("th", { scope: "col" }, "Model"), h("th.r", { scope: "col" }, "Input, per million tokens"),
            h("th.r", { scope: "col" }, "Output"), h("th.r", { scope: "col" }, "Per book"))),
          h("tbody", p.models.map((m) => h("tr",
            h("th", { scope: "row" }, m.label, m.id === p.default ? h("span.tag", "Default") : null),
            h("td.r", m.price ? `$${m.price[0].toFixed(2)}` : "Price unknown"),
            h("td.r", m.price ? `$${m.price[1].toFixed(2)}` : ""),
            h("td.r", perBook(m) != null ? money(perBook(m)) : ""))))) : null,
        h("div.btns", replace, remove))));
    }
    return { nodes: out, first };
  }

  /** Repaint one provider's rows in place; `focus` is "field" (into the key field) or "action" (back to the row's button). */
  function paintRow(id, focus = null) {
    const { nodes, first } = providerRows(known[id]);
    const old = rows[id] || [];
    if (old[0]?.parentNode) old[0].before(...nodes); else tbody.append(...nodes);
    for (const n of old) n.remove();
    rows[id] = nodes;
    if (focus === "field") first?.focus();
    if (focus === "action") nodes[0].querySelector(".pa .btn")?.focus();
    cloudLine();
  }
  async function paintProvider(id, focus = null) {
    let p;
    try { p = (await api.providers(true)).providers.find((x) => x.id === id); } catch (e) { toast(e.message, "error"); return; }
    if (disposed || !p) return;
    known[id] = p;
    paintRow(id, focus);
  }
  async function paintCloud() {
    clear(panes.cloud, head("cloud", "Cloud models"), h("p.set-note", "Checking your keys…"));
    let r;
    try { r = await api.providers(); }
    catch (e) {
      lines.cloud.textContent = "Couldn't check";
      clear(panes.cloud, head("cloud", "Cloud models"), h("div.load-error.inline", { role: "alert" }, h("p.muted", e.message),
        h("button.btn", { type: "button", on: { click: paintCloud } }, "Try again")));
      return;
    }
    if (disposed) return;
    tbody.replaceChildren();
    for (const p of r.providers) { known[p.id] = p; rows[p.id] = providerRows(p).nodes; tbody.append(...rows[p.id]); }
    cloudLede = h("p.sec-lede");
    cloudLine();
    clear(panes.cloud,
      h("div.sec-head", h("h2", { id: "sec-h-cloud" }, "Cloud models"), cloudLede),
      h("div.table-wrap.set-table", h("table.data.provs",
        h("caption.sr-only", "Cloud model providers"),
        h("thead", h("tr", h("th", { scope: "col" }, "Provider"), h("th", { scope: "col" }, "Status"), h("th", { scope: "col" }, "Key"),
          h("th", { scope: "col" }, "Default model"), h("th", { scope: "col" }, "Images"), h("th.r", { scope: "col" }, "Per book"), h("th.r", { scope: "col" }, h("span.sr-only", "Action")))),
        tbody)),
      howItWorks(
        "Book text is sent to the provider you build with, and only when you build.",
        "A key is tested before it is saved, then kept only on this machine in lorekeeper-data/secrets.json, readable by your user only, and never shown in full again. A key in the provider's environment variable (such as OPENAI_API_KEY) works too; a key saved here takes priority over it.",
        `Per book means a 100,000-word novel at standard depth: about ${fmtInt(1.2 * 100000)} full-price input and ${fmtInt(0.58 * 100000)} output tokens, measured on a fact-checked run, at each provider's list prices, which may have changed. The build form shows the estimate for the real book.`,
        "Custom server is anything that speaks the OpenAI chat API: LM Studio, vLLM, llama.cpp, a proxy. No price is known for it, so estimates say so, and book text is sent to that server.",
        "Images is the model that draws pictures with that key, and its price for one picture at your default quality. Only Gemini and OpenAI can draw; the same key serves both text and pictures."));
  }
  paintCloud();

  // ---- Pictures ------------------------------------------------------------------------------
  /** What a book's style previews cost: every style once per image provider, square, at the provider's default model's
   *  cheapest level (what the previews run draws at). "About 30 cents per book for Gemini." */
  function previewCost(provs, styles) {
    const each = provs.map((p) => {
      const m = modelFor(p);
      const q = levelsOf(p, m)[0]?.[0] || "draft";
      const one = imagePrice(p, q, "square", m);
      return { name: p.label || p.id, usd: one == null ? null : one * styles };
    });
    const one = (x) => (x.usd == null ? "price unknown" : aboutMoney(x.usd));
    if (each.length === 1) { const t = one(each[0]); return `${t[0].toUpperCase()}${t.slice(1)} per book with ${each[0].name}.`; }
    const parts = each.map((x, i) => `${one(x)}${i ? "" : " per book"} for ${x.name}`);
    const said = `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
    return `${said[0].toUpperCase()}${said.slice(1)}.`;
  }
  // The defaults new pictures start from (kept in this browser), and the art sets on this server with their size.
  async function paintPictures(focusId = null) {
    const d = artDefaults();
    const [{ image: provs, text: writers, broken }] = await Promise.all([modelProviders({ fresh: true }), loadStyles()]);
    if (disposed) return;
    lines.pictures.textContent = provs.length ? `${styleLabel(d.style)}, ${d.quality}` : "Needs a Gemini or OpenAI key";
    const save = (patch, what) => { setArtDefaults(patch); toast(`${what} saved`); paintPictures(document.activeElement?.id || null); };
    // The styles, inline: choosing a card makes it the style new pictures start in (no repaint, so focus stays on the
    // card); custom ones can be edited and deleted here. An older default typed as words is still shown, by its words.
    const known = STYLES.some(([k]) => k === d.style);
    const styles = await loadStyles();
    if (disposed) return;
    const legacy = !known && !styles.some((x) => x.slug === d.style) ? [{ slug: d.style, name: styleLabel(d.style), text: d.style }] : [];
    const summary = () => { const a = artDefaults(); lines.pictures.textContent = provs.length ? `${styleLabel(a.style)}, ${a.quality}${modelName ? `, ${modelName}` : ""}` : "Needs a Gemini or OpenAI key"; };
    const picker = stylePicker({ value: d.style, extra: legacy, manage: true, pickNew: false,
      onPick: (x) => { setArtDefaults({ style: x.slug }); summary(); toast(`${x.name} is the style for new pictures`); },
      onChanged: async (gone) => {
        // A deleted default goes back to the first built-in style.
        if (gone && artDefaults().style === gone.slug) { setArtDefaults({ style: "digital-art" }); picker.set("digital-art"); }
        summary();
      } });
    // The provider new pictures are drawn with, and its model: the saved one when that provider lists it, else its default.
    const current = provs.find((p) => p.id === d.provider) || provs[0] || null;
    const model = current ? modelFor(current, current.id === d.provider ? d.model : null) : null;
    const offered = current ? levelsOf(current, model).map(([k]) => k) : QUALITIES.map(([k]) => k);
    const quality = h("select.select#pic-quality", { on: { change: (e) => save({ quality: e.target.value }, "Quality") } },
      QUALITIES.map(([k, l]) => h("option", { value: k, selected: k === (current ? fitQuality(current, model, d.quality) : d.quality), disabled: !offered.includes(k) }, l)));
    const cap = h("input.input.w-90#pic-cap", { type: "number", min: "0.05", step: "0.05", inputmode: "decimal", value: String(d.cap),
      on: { change: (e) => save({ cap: +e.target.value > 0 ? Math.round(+e.target.value * 100) / 100 : 10 }, "Limit") } });
    // Another provider starts from its own default model.
    const prov = provs.length > 1 ? h("select.select#pic-prov", { on: { change: (e) => save({ provider: e.target.value, model: null }, "Provider") } },
      provs.map((p) => h("option", { value: p.id, selected: p.id === current?.id }, p.label))) : null;
    const models = current ? imageModels(current) : [];
    // A model without the default quality draws at the one it has, so the quality is saved with it.
    const modelSel = models.length ? h("select.select#pic-model", { on: { change: (e) => {
      const id = modelFor(current, e.target.value);
      save({ provider: current.id, model: id, quality: fitQuality(current, id, d.quality) }, "Image model"); } } },
    models.map((m) => h("option", { value: m.id, selected: m.id === model }, imageLine(current, d.quality, m.id)))) : null;
    const modelName = models.find((m) => m.id === model)?.label;
    if (modelName) lines.pictures.textContent = `${styleLabel(d.style)}, ${d.quality}, ${modelName}`;
    const sets = h("div.pic-sets", h("p.set-note", "Looking for pictures…"));
    const check = h("input", { type: "checkbox", role: "switch", id: "pic-check", checked: d.check,
      on: { change: (e) => save({ check: e.target.checked }, "Drawing fault check") } });
    const previews = h("input", { type: "checkbox", role: "switch", id: "pic-previews", checked: d.previews,
      on: { change: (e) => save({ previews: e.target.checked }, "Style previews") } });
    clear(panes.pictures,
      head("pictures", "Pictures", provs.length ? "What new pictures start from, and the pictures kept on this server." : null),
      provs.length ? null : h("p.set-note", "Pictures need a Gemini or OpenAI key. ", h("a", { href: "#/settings/cloud" }, "Add one in Cloud models")),
      h("div.set-rows",
        row(h("label", { for: "pic-quality" }, "Quality"), quality),
        row(h("label", { for: "pic-cap" }, "Stop a run at"), h("span.pic-cap", "$", cap)),
        prov ? row(h("label", { for: "pic-prov" }, "Draw with"), prov) : provs.length === 1 ? row("Draws with", modelSel ? provs[0].label : `${provs[0].label}, ${imageLine(provs[0], d.quality)}`) : null,
        modelSel ? row(h("label", { for: "pic-model" }, "Image model"), modelSel) : null,
        provs.length ? row(h("label", { for: "pic-pimage" }, "Portraits drawn with"), portraitImageSelect({ id: "pic-pimage", provs,
          provider: d.portraitProvider, model: d.portraitModel, quality: d.quality,
          onChange: (c) => save({ portraitProvider: c?.provider || null, portraitModel: c?.model || null }, "Portrait model") })) : null,
        provs.length ? row(h("label", { for: "pic-check" }, "Check pictures for drawing faults"), h("div.pic-check",
          h("span.switch", check, h("span", d.check ? "On" : "Off")),
          h("p.hint", `A cheap model looks at each new picture for drawing faults, such as an extra hand or a head far too small. When it finds one you would see at a glance, the picture is fixed once, and the original is kept if a second look finds the fix put nothing right. Smaller faults are left as drawn. ${CHECK_COST}`)), null, { top: true }) : null,
        provs.length ? row(h("label", { for: "pic-previews" }, "Draw style previews for new books"), h("div.pic-check",
          h("span.switch", previews, h("span", d.previews ? "On" : "Off")),
          h("p.hint", `After a book's first build, one scene from its opening is drawn in every style, once per image provider, so the style cards show that book. ${previewCost(provs, styles.length)}`)), null, { top: true }) : null),
      h("h3.pic-sets-h#pic-styles-h", "Styles"),
      h("p.set-note", "Every style shows the same scene; the one you choose is where new pictures start."),
      h("div.pic-styles", { role: "region", "aria-labelledby": "pic-styles-h" }, picker.el),
      // Shown whenever a text provider has a key, working or not, so a key that fails says so here instead of the rows
      // quietly going away.
      writers.length || broken.length ? [
        h("h3.pic-sets-h", "Who writes the prompts"),
        h("p.set-note", "Scene prompts are for chapter pictures and moments, portrait prompts for people, places and things, and choosing calls pick what gets drawn."),
        broken.length ? h("p.set-note.pic-writers-broken", brokenKeys(broken, writers.length > 0), " ", h("a", { href: "#/settings/cloud" }, broken.length > 1 ? "Check the keys in Cloud models" : "Check the key in Cloud models"), ".") : null,
        writers.length ? h("div.set-rows",
          row(h("label", { for: "pic-writer" }, "Scene prompts written by"), writerSelect({ id: "pic-writer", provs: writers, value: d.writer, tier: "cheap",
            onChange: (c) => save({ writer: c }, "Scene prompt model") })),
          row(h("label", { for: "pic-pwriter" }, "Portrait prompts written by"), writerSelect({ id: "pic-pwriter", provs: writers, value: d.portraitWriter, tier: "strong",
            onChange: (c) => save({ portraitWriter: c }, "Portrait prompt model") })),
          row(h("label", { for: "pic-picker" }, "Choosing calls by"), writerSelect({ id: "pic-picker", provs: writers, value: d.picker, tier: "cheap",
            onChange: (c) => save({ picker: c }, "Choosing model") }))) : null,
      ] : null,
      h("h3.pic-sets-h", "Pictures on this server"), sets,
      howItWorks(
        "These choices are kept in this browser and fill in the Pictures choices on the build form, on a book's page and in the Reader's Draw a picture sheet.",
        "A text model writes each picture's prompt from the passage and the index, as far as that picture's place in the book; the image provider sees only that prompt, never the book text. Pictures stay on this server, beside the index, and are never part of a shared index.",
        "Scene prompts describe a chapter's main moment or another moment worth drawing; by default the cheaper model of your first provider writes them. Portrait prompts describe a person, creature, group, place or thing as the book has shown it so far; they are few, and every scene uses the portraits as references, so by default the stronger model writes them, about a cent more each. Choosing calls are the small calls a run makes before drawing: which moments of a chapter are worth a picture, and which details say how someone looks. A prompt rewritten after a refusal is written by the model of its kind. In the Reader's Draw a picture sheet the stronger model writes by default, and you can choose another there.",
        "Portraits drawn with is the image model for portraits, places and things; Same as scenes uses the image model above. Each model's price is per million tokens of text, or per picture.",
        "A run stops drawing at the limit you set and keeps what it drew. Removing a book's pictures deletes its whole art folder, every style."));
    if (focusId) document.getElementById(focusId)?.focus();
    let r;
    try { r = await api.artSets(); } catch (e) {
      if (!disposed) clear(sets, h("p.set-note", e.status === 404 ? "This server cannot list pictures yet." : `Couldn't list the pictures: ${e.message}`));
      return;
    }
    if (disposed) return;
    // [{ key, title, textHash, styles: [{ style, slug, pictures, bytes }], bytes }] (an older shape { books } is read too).
    const books = (Array.isArray(r) ? r : Array.isArray(r?.books) ? r.books : []).filter((b) => b && typeof b.textHash === "string");
    const mb = (n) => (Number(n) >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round((Number(n) || 0) / 1e3))} KB`);
    clear(sets, books.length ? h("div.table-wrap", h("table.data.pic-table",
      h("caption.sr-only", "Pictures on this server, per book"),
      h("thead", h("tr", h("th", { scope: "col" }, "Book"), h("th", { scope: "col" }, "Styles"), h("th.r", { scope: "col" }, "Size"), h("th.r", { scope: "col" }, h("span.sr-only", "Action")))),
      h("tbody", books.map((b) => h("tr",
        h("th", { scope: "row" }, b.title || "Untitled"),
        h("td", (Array.isArray(b.styles) ? b.styles : []).map((x) => (typeof x === "string" ? styleLabel(x) : styleLabel(x?.slug, x?.style))).join(", ")),
        h("td.r.num", mb(b.bytes ?? b.size)),
        h("td.r", h("button.btn.quiet.danger", { type: "button", on: { click: async () => {
          if (!(await confirmDialog({ title: `Remove the pictures of ${b.title || "this book"}?`, message: "Every picture of this book, in every style, is deleted from this server. The index is not touched.", confirmLabel: "Remove pictures" }))) return;
          try {
            const res = await api.artRemove(b.textHash);
            const undo = res?.undo ?? null;
            toast("Pictures removed", "ok", { action: undo && b.key ? { label: "Undo", run: async () => {
              try { await api.undo(b.key, undo); toast("Undone"); paintPictures(); } catch (e) { toast(e.message, "error"); }
            } } : null });
            paintPictures();
          } catch (e) { toast(e.status === 409 ? "Pictures are being drawn for this book. Remove them once the run ends." : e.message, "error"); }
        } } }, "Remove pictures")))))))
      : h("p.set-note", "No pictures yet. Tick Pictures when you build an index, or use Add pictures on a book's page."));
  }
  paintPictures();

  // ---- Book Orbit -----------------------------------------------------------------------------
  const boHow = () => howItWorks(
    "Lorekeeper keeps only a session token, renewed as it is used, never your password: the password goes to your Book Orbit server once, to sign in, and is then forgotten.",
    "It reads titles, series and covers, and your reading position for the Reader, downloads an EPUB only when you build that book's index, and writes nothing to Book Orbit.",
    "Only books with an EPUB are listed. Comics, PDFs and audiobooks are skipped because they have no text to index.");
  async function paintBookOrbit(focusSel = null) {
    let st;
    try { st = await api.bookorbit(); }
    catch (e) {
      lines.bookorbit.textContent = "Couldn't check";
      clear(panes.bookorbit, head("bookorbit", "Book Orbit"), h("div.load-error.inline", { role: "alert" }, h("p.muted", e.message),
        h("button.btn", { type: "button", on: { click: () => paintBookOrbit() } }, "Try again")));
      return;
    }
    if (disposed) return;
    if (st.connected) {
      lines.bookorbit.textContent = st.librariesChosen ? `Connected, ${fmtInt(st.books)} book${st.books === 1 ? "" : "s"}` : "Choose libraries";
      const syncBtn = h("button.btn", { type: "button" }, icon("refresh"), "Sync now");
      syncBtn.addEventListener("click", async () => {
        syncBtn.disabled = true; syncBtn.setAttribute("aria-busy", "true"); syncBtn.lastChild.textContent = "Syncing…";
        try { await api.syncBookOrbit(); toast("Library synced"); } catch (e) { toast(e.message, "error"); }
        paintBookOrbit();
      });
      clear(panes.bookorbit,
        head("bookorbit", "Book Orbit", "Your Book Orbit books, series and numbering, in the Library."),
        h("div.set-rows",
          row("Status", h("span", state_("ok", "Connected"), " to ", h("span.num", hostOf(st.url)), ` as ${st.name || st.username}. Last synced ${relTime(st.synced)}.`)),
          row(st.librariesChosen ? "Libraries shown" : "Choose libraries", librariesPicker(st), null, { top: true }),
          row("In the Library", `${fmtInt(st.books)} EPUB${st.books === 1 ? "" : "s"}`)),
        h("div.btns.set-btns", syncBtn, h("a.btn", { href: "#/library?source=bookorbit" }, "Open library"),
          h("button.btn.danger", { type: "button", on: { click: async () => {
            if (!await confirmDialog({ title: "Disconnect Book Orbit?",
              message: "Its books leave the library list. Indexes you built are kept.", confirmLabel: "Disconnect" })) return;
            try { await api.disconnectBookOrbit(); toast("Disconnected"); paintBookOrbit("#bo-url"); } catch (e) { toast(e.message, "error"); }
          } } }, "Disconnect")),
        boHow());
      // Waiting for libraries to be chosen: that is the section to open on.
      if (!st.librariesChosen && !explicit && current !== "bookorbit") { current = "bookorbit"; show(); }
      if (focusSel) panes.bookorbit.querySelector(focusSel)?.focus();
      return;
    }
    lines.bookorbit.textContent = "Not connected";
    const url = h("input.input", { id: "bo-url", type: "url", placeholder: "https://books.example.com", autocomplete: "url" });
    const user_ = h("input.input", { id: "bo-user", autocomplete: "username" });
    const pass = h("input.input", { id: "bo-pass", type: "password", autocomplete: "current-password" });
    const err = h("p.hint.field-error#bo-error", { role: "alert" });
    const go_ = h("button.btn.primary", { type: "submit" }, "Sign in and sync");
    const fieldErr = (input, key) => {
      const p = h(`p.hint.field-error#bo-${key}-error`);
      input.addEventListener("input", () => { p.textContent = ""; input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); });
      return p;
    };
    const urlErr = fieldErr(url, "url"), userErr = fieldErr(user_, "user"), passErr = fieldErr(pass, "pass");
    const flag = (input, p, msg) => { p.textContent = msg; input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", p.id); };
    const validate = () => {
      for (const [i, p] of [[url, urlErr], [user_, userErr], [pass, passErr]]) { p.textContent = ""; i.removeAttribute("aria-invalid"); i.removeAttribute("aria-describedby"); }
      const bad = [];
      const u = url.value.trim();
      if (!u) bad.push([url, urlErr, "Enter your Book Orbit server address."]);
      else if (!/^https?:\/\//i.test(u)) bad.push([url, urlErr, "The address should start with https://"]);
      if (!user_.value.trim()) bad.push([user_, userErr, "Enter your username."]);
      if (!pass.value) bad.push([pass, passErr, "Enter your password."]);
      for (const [i, p, m] of bad) flag(i, p, m);
      bad[0]?.[0].focus();
      return !bad.length;
    };
    const field = (id, label, input, error) => h("div.set-row.form-row", h("label.set-lbl", { for: id }, label), h("div.set-val", input, error));
    clear(panes.bookorbit,
      head("bookorbit", "Book Orbit", "Show every book on your Book Orbit server in the Library, with its series and numbering, and build indexes for any of them."),
      h("form.set-rows", { novalidate: true, on: { submit: async (e) => {
        e.preventDefault();
        err.textContent = "";
        if (!validate()) return;
        go_.disabled = true; go_.setAttribute("aria-busy", "true"); go_.textContent = "Signing in…";
        try {
          await api.connectBookOrbit(url.value.trim(), user_.value.trim(), pass.value);
          pass.value = "";
          toast("Connected. Now choose which libraries to show.");
          paintBookOrbit(".bo-libs input");
        } catch (ex) {
          err.textContent = ex.message;
          go_.disabled = false; go_.removeAttribute("aria-busy"); go_.textContent = "Sign in and sync";
        }
      } } },
      field("bo-url", "Server address", url, urlErr),
      field("bo-user", "Username", user_, userErr),
      field("bo-pass", "Password", pass, passErr),
      h("div.set-row.form-row", h("span.set-lbl"), h("div.set-val", err, h("div.btns", go_)))),
      boHow());
    if (focusSel) panes.bookorbit.querySelector(focusSel)?.focus();
  }
  /** Which Book Orbit libraries feed the Lorekeeper library. Asked once after signing in. */
  function librariesPicker(st) {
    if (!st.libraries?.length) return h("p.muted", "Press Sync now to choose which Book Orbit libraries to show.");
    const boxes = st.libraries.map((lib) => {
      const box = h("input", { type: "checkbox", value: lib.id, checked: lib.enabled });
      box.addEventListener("change", changed);
      return h("label.bo-lib", box, h("span.bo-lib-name", lib.name),
        h("span.hint", `${fmtInt(lib.books)} EPUB${lib.books === 1 ? "" : "s"}`,
          lib.skipped ? `, ${fmtInt(lib.skipped)} other file${lib.skipped === 1 ? "" : "s"} skipped` : ""));
    });
    const save = h("button.btn.primary", { type: "button" }, st.librariesChosen ? "Save" : "Show these libraries");
    const note = h("p.hint", { role: "status" });
    function changed() {
      const n = boxes.filter((b) => b.querySelector("input").checked).length;
      save.disabled = st.librariesChosen && boxes.every((b) => b.querySelector("input").checked === st.libraries.find((l) => l.id === b.querySelector("input").value).enabled);
      note.textContent = n ? "" : "With no library chosen, no Book Orbit books are listed.";
    }
    save.addEventListener("click", async () => {
      const ids_ = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
      save.disabled = true;
      try { const r = await api.bookorbitLibraries(ids_); toast(`${fmtInt(r.books)} books in the library`); paintBookOrbit(); }
      catch (e) { toast(e.message, "error"); save.disabled = false; }
    });
    changed();
    return h(`fieldset.bo-libs${st.librariesChosen ? "" : ".ask"}`,
      h("legend.sr-only", st.librariesChosen ? "Libraries shown" : "Choose the libraries to show"),
      h("div.bo-lib-list", boxes), h("div.btns", save, note));
  }
  clear(panes.bookorbit, head("bookorbit", "Book Orbit"), h("p.set-note", "Checking Book Orbit…"));
  paintBookOrbit();

  return () => { disposed = true; off(); };
}
