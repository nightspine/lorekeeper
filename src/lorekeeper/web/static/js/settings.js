import { api } from "./api.js";
import { onOllama } from "./app.js";
import { confirmDialog } from "./build.js";
import { clear, fmtInt, h, icon, relTime, toast } from "./dom.js";

const gb = (bytes) => (bytes ? `${(bytes / 1e9).toFixed(1)} GB` : "");

export function mountSettings(root) {
  const ollamaBody = h("div.stack-14");
  const boBody = h("div.stack-14");
  const cloudBody = h("div.stack-16.prov-list");
  // state = dot + word: beacon when working, rose when broken, hollow when nothing is set up yet
  const status = (ok, text, unset = false) => h(`span.badge.${ok ? "done" : unset ? "stopped" : "failed"}`, text);

  root.append(h("div.settings-page",
    h("div", h("span.eyebrow", "Settings"), h("h1", "Library and models"),
      h("p.muted.settings-lede",
        "Lorekeeper can read books with a local model through Ollama, or with a cloud model on your own key: Claude, OpenAI, Gemini and more, or any OpenAI-compatible server. Local is free and private; cloud models are much faster and cost a little per book.")),
    h("section.card.card-pad", { "aria-labelledby": "p-bo", id: "bookorbit" },
      h("div.section-title", h("h2#p-bo.with-icon", icon("link"), "Book Orbit")),
      boBody),
    h("div.settings-grid",
      h("section.card.card-pad", { "aria-labelledby": "p-cloud" },
        h("div.section-title", h("h2#p-cloud.with-icon", icon("sparkle"), "Cloud models")),
        cloudBody),
      h("section.card.card-pad", { "aria-labelledby": "p-ollama" },
        h("div.section-title", h("h2#p-ollama.with-icon", icon("cpu"), "Ollama (local)")),
        ollamaBody))));

  // ---- Ollama ----------------------------------------------------------------------------
  const paintOllama = (o) => {
    clear(ollamaBody,
      h("div", status(o.ok, o.ok ? "Running" : "Not running")),
      h("p.hint", "Server: ", h("span.num", o.url || "http://localhost:11434"),
        ". Change it by starting Lorekeeper with --ollama-url."),
      o.ok
        ? (o.models.length
          ? h("ul.list", o.models.map((m) => h("li.model-row",
            h("span", h("span.model-name", m.name), o.loaded.includes(m.name) ? h("span.chip.chip-after", "loaded") : null),
            h("span.subtle.num", [m.parameters, gb(m.size)].filter(Boolean).join(", ")))))
          : h("p.muted", "No models installed. Try: ollama pull gemma4:12b-it-qat"))
        : h("div.notice.warn", icon("alert"), h("div", "Start Ollama with ", h("span.mono", "brew services start ollama"),
          ", or install it from ollama.com.")));
  };
  clear(ollamaBody, h("p.muted", "Checking Ollama…"));
  const off = onOllama(paintOllama);

  // ---- Cloud models: one card per provider, on the user's own key -----------------------------
  const perBook = (m) => (m.price ? (100000 * (1.2 * m.price[0] + 0.58 * m.price[1])) / 1e6 : null);
  const money = (c) => `$${c.toFixed(c < 1 ? 3 : 2)}`;
  const cards = {};
  function providerCard(p) {
    const custom = p.id === "custom";
    const where = p.source === "settings" ? "saved in Settings" : p.source === "environment" ? `from ${p.key_env}` : "";
    let showing = false;
    const input = h("input.input", { id: `key-${p.id}`, type: "password", autocomplete: "off", spellcheck: "false",
      placeholder: p.source ? "Paste a new key to replace the current one" : custom ? "Optional: only if the server needs one" : "Paste your API key" });
    const urlInput = custom && h("input.input", { id: "key-custom-url", type: "url", value: p.base_url || "", placeholder: "http://localhost:1234/v1" });
    const modelInput = custom && h("input.input", { id: "key-custom-model", value: p.model || "", placeholder: "The model name the server uses" });
    const err = h("p.hint.field-error", { role: "alert", id: `err-${p.id}` });
    const reveal = h("button.icon-btn", { type: "button", "aria-label": "Show key", "aria-pressed": "false",
      on: { click: () => { showing = !showing; input.type = showing ? "text" : "password"; reveal.setAttribute("aria-pressed", String(showing)); reveal.setAttribute("aria-label", showing ? "Hide key" : "Show key"); } } }, icon("eye"));
    const save = h("button.btn.primary", { type: "submit" }, icon("check"), "Save and test");
    const form = h("form.stack-8", { on: { submit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      if (!custom && !input.value.trim()) { err.textContent = "Paste your API key first."; input.focus(); return; }
      save.disabled = true; save.setAttribute("aria-busy", "true"); save.lastChild.textContent = "Testing…";
      try {
        await api.setProvider(p.id, { key: input.value.trim(), ...(custom ? { base_url: urlInput.value.trim(), model: modelInput.value.trim() } : {}) });
        toast(`${p.label} is connected`);
        paintProvider(p.id);
      } catch (ex) {
        err.textContent = ex.message;
        save.disabled = false; save.removeAttribute("aria-busy"); save.lastChild.textContent = "Save and test";
      }
    } } },
      custom && h("label.label", { for: "key-custom-url" }, "Base URL"), urlInput,
      custom && h("label.label", { for: "key-custom-model" }, "Model"), modelInput,
      h("label.label", { for: `key-${p.id}` }, p.source === "settings" ? "Replace API key" : custom ? "API key (optional)" : "API key"),
      h("div.key-row", input, reveal),
      err,
      h("div.toolbar", save,
        (p.source === "settings" || (custom && p.configured)) && h("button.btn.danger", { type: "button", on: { click: async () => {
          if (!await confirmDialog({ title: `Remove the saved ${p.label} settings?`,
            message: "The key is deleted from lorekeeper-data/secrets.json on this machine. Runs on this provider stop working until you add one again.",
            confirmLabel: "Remove" })) return;
          try { await api.deleteProvider(p.id); toast("Removed"); paintProvider(p.id); } catch (ex) { toast(ex.message, "error"); }
        } } }, icon("trash"), "Remove")));

    const priced = p.models.filter((m) => m.price);
    return h("div.stack-14.prov", { id: `prov-${p.id}` },
      h("div.row-wrap",
        h("h3.prov-name", p.label),
        status(p.ok, p.ok ? "Connected" : p.configured ? "Not working" : "Not set up", !p.configured),
        p.masked && h("span.chip.num", p.masked), where && h("span.subtle", where),
        p.key_url && h("a.subtle", { href: p.key_url, target: "_blank", rel: "noopener noreferrer" }, "Get a key")),
      !p.ok && p.configured && p.error && h("div.notice.warn", icon("alert"), h("div", p.error)),
      p.source === "environment" && h("p.hint", `A key saved here takes priority over ${p.key_env}.`),
      // Nine providers on one page: only a connected one keeps its form open; the rest unfold on demand.
      p.configured ? form : h("details.prov-more", h("summary", custom ? "Set up a server" : "Add a key"), form),
      priced.length ? h("details.prov-more", h("summary", "Models and prices"), h("div.pricing", h("div.table-wrap", h("table.data",
        h("thead", h("tr", ["Model", "Input", "Output", "≈ per 100k-word book"].map((t) => h("th", t)))),
        h("tbody", p.models.map((m) => h("tr",
          h("td", m.label, m.id === p.default ? h("span.chip.chip-after", "recommended") : null),
          h("td.num", m.price ? `$${m.price[0]}/M` : "price unknown"),
          h("td.num", m.price ? `$${m.price[1]}/M` : ""),
          h("td.num", perBook(m) != null ? money(perBook(m)) : ""))))))))
        : null,
      h("p.hint.pricing-note", custom
        ? "Any server that speaks the OpenAI chat API: LM Studio, vLLM, llama.cpp, a proxy. No price is known, so the estimate says so. Book text is sent to that server."
        : `${priced.length ? `Estimated from a measured fact-checked run (about ${fmtInt(1.2 * 100000)} full-price input and ${fmtInt(0.58 * 100000)} output tokens per 100k words, standard depth). Prices are the provider's list prices and may have changed. ` : ""}Book text is sent to ${p.label}. The key is tested before saving, stored only on this machine in lorekeeper-data/secrets.json (readable by your user only), and never shown in full again.`));
  }
  async function paintProvider(id) {
    let p;
    try { p = (await api.providers(true)).providers.find((x) => x.id === id); } catch (e) { return toast(e.message, "error"); }
    const next = providerCard(p);
    cards[id]?.replaceWith(next);
    cards[id] = next;
  }
  async function paintCloud(fresh = false) {
    clear(cloudBody, h("p.muted", "Checking…"));
    let r;
    try { r = await api.providers(fresh); } catch (e) { return clear(cloudBody, h("p.muted", e.message)); }
    for (const k of Object.keys(cards)) delete cards[k];
    for (const p of r.providers) cards[p.id] = providerCard(p);
    clear(cloudBody, Object.values(cards));
  }
  paintCloud();

  // ---- Book Orbit ------------------------------------------------------------------------
  async function paintBookOrbit() {
    let st;
    try { st = await api.bookorbit(); } catch (e) { return clear(boBody, h("p.muted", e.message)); }
    if (st.connected) {
      const syncBtn = h("button.btn", { type: "button" }, icon("refresh"), "Sync now");
      syncBtn.addEventListener("click", async () => {
        syncBtn.disabled = true; syncBtn.setAttribute("aria-busy", "true"); syncBtn.lastChild.textContent = "Syncing…";
        try { await api.syncBookOrbit(); toast("Library synced"); } catch (e) { toast(e.message, "error"); }
        paintBookOrbit();
      });
      clear(boBody,
        h("div.row-wrap",
          status(true, "Connected"), h("span.chip.num", st.url), h("span.subtle", `as ${st.name || st.username}`)),
        librariesPicker(st),
        h("p.muted", `${fmtInt(st.books)} EPUBs in the library list. Last synced ${relTime(st.synced)}.`),
        h("p.hint", "Lorekeeper keeps only a session token, never your password. It reads titles, series and covers, and downloads an EPUB only when you build that book's index. It writes nothing to Book Orbit."),
        h("div.toolbar", syncBtn, h("a.btn", { href: "#/library?source=bookorbit" }, icon("book-open"), "Open library"),
          h("button.btn.danger", { type: "button", on: { click: async () => {
            if (!await confirmDialog({ title: "Disconnect Book Orbit?",
              message: "Its books leave the library list. Indexes you built are kept.", confirmLabel: "Disconnect" })) return;
            try { await api.disconnectBookOrbit(); toast("Disconnected"); paintBookOrbit(); } catch (e) { toast(e.message, "error"); }
          } } }, icon("x"), "Disconnect")));
      if (!st.librariesChosen) boBody.querySelector(".bo-libs")?.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    const url = h("input.input", { id: "bo-url", type: "url", placeholder: "https://books.example.com", autocomplete: "url" });
    const user = h("input.input", { id: "bo-user", autocomplete: "username" });
    const pass = h("input.input", { id: "bo-pass", type: "password", autocomplete: "current-password" });
    const err = h("p.hint.field-error#bo-error", { role: "alert" });
    const go = h("button.btn.primary", { type: "submit" }, icon("link"), "Sign in and sync");
    const fieldErr = (input, key) => {
      const p = h(`p.hint.field-error#bo-${key}-error`);
      input.addEventListener("input", () => { p.textContent = ""; input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); });
      return p;
    };
    const urlErr = fieldErr(url, "url"), userErr = fieldErr(user, "user"), passErr = fieldErr(pass, "pass");
    const flag = (input, p, msg) => { p.textContent = msg; input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", p.id); };
    const validate = () => {
      for (const [i, p] of [[url, urlErr], [user, userErr], [pass, passErr]]) { p.textContent = ""; i.removeAttribute("aria-invalid"); i.removeAttribute("aria-describedby"); }
      const bad = [];
      const u = url.value.trim();
      if (!u) bad.push([url, urlErr, "Enter your Book Orbit server address."]);
      else if (!/^https?:\/\//i.test(u)) bad.push([url, urlErr, "The address should start with https://"]);
      if (!user.value.trim()) bad.push([user, userErr, "Enter your username."]);
      if (!pass.value) bad.push([pass, passErr, "Enter your password."]);
      for (const [i, p, m] of bad) flag(i, p, m);
      bad[0]?.[0].focus();
      return !bad.length;
    };
    clear(boBody,
      h("p.muted", "Show every book on your Book Orbit server in the Lorekeeper library, with its series and numbering, and build indexes for any of them."),
      h("form.stack-12.form-520", { novalidate: true, on: { submit: async (e) => {
        e.preventDefault();
        err.textContent = "";
        if (!validate()) return;
        go.disabled = true; go.setAttribute("aria-busy", "true"); go.lastChild.textContent = "Signing in…";
        try {
          await api.connectBookOrbit(url.value.trim(), user.value.trim(), pass.value);
          pass.value = "";
          toast("Connected. Now choose which libraries to show.");
          paintBookOrbit();
        } catch (ex) {
          err.textContent = ex.message;
          go.disabled = false; go.removeAttribute("aria-busy"); go.lastChild.textContent = "Sign in and sync";
        }
      } } },
      h("div.field", h("label", { for: "bo-url" }, "Server address"), url, urlErr),
      h("div.field", h("label", { for: "bo-user" }, "Username"), user, userErr),
      h("div.field", h("label", { for: "bo-pass" }, "Password"), pass, passErr,
        h("p.hint", "Sent to your Book Orbit server to sign in, then forgotten. Lorekeeper keeps only a session token, renewed as it's used.")),
      err, h("div.toolbar", go)));
  }
  /** Which Book Orbit libraries feed the Lorekeeper library. Asked once after signing in. */
  function librariesPicker(st) {
    if (!st.libraries?.length) return h("p.notice.info", "Press Sync now to choose which Book Orbit libraries to show.");
    const boxes = st.libraries.map((lib) => {
      const box = h("input", { type: "checkbox", value: lib.id, checked: lib.enabled });
      box.addEventListener("change", changed);
      return h("label.bo-lib", box, h("span.bo-lib-name", lib.name),
        h("span.hint", `${fmtInt(lib.books)} EPUB${lib.books === 1 ? "" : "s"}`,
          lib.skipped ? `, ${fmtInt(lib.skipped)} other file${lib.skipped === 1 ? "" : "s"} skipped` : ""));
    });
    const save = h("button.btn.primary", { type: "button" }, icon("check"), st.librariesChosen ? "Save" : "Show these libraries");
    const note = h("p.hint", { role: "status" });
    function changed() {
      const n = boxes.filter((b) => b.querySelector("input").checked).length;
      save.disabled = st.librariesChosen && boxes.every((b) => b.querySelector("input").checked === st.libraries.find((l) => l.id === b.querySelector("input").value).enabled);
      note.textContent = n ? "" : "With no library chosen, no Book Orbit books are listed.";
    }
    save.addEventListener("click", async () => {
      const ids = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
      save.disabled = true;
      try { const r = await api.bookorbitLibraries(ids); toast(`${fmtInt(r.books)} books in the library`); paintBookOrbit(); }
      catch (e) { toast(e.message, "error"); save.disabled = false; }
    });
    changed();
    return h(`fieldset.bo-libs${st.librariesChosen ? "" : ".ask"}`,
      h("legend", st.librariesChosen ? "Libraries" : "Choose the libraries to show"),
      st.librariesChosen ? null : h("p.hint", "Only books with an EPUB are listed. Comics, PDFs and audiobooks are skipped because they have no text to index."),
      h("div.bo-lib-list", boxes), h("div.toolbar", save, note));
  }

  paintBookOrbit();

  return () => off();
}
