// Sign-in and password pages. Everything is built with h() and textContent; server text never reaches innerHTML.
import { api, safeNext } from "./api.js";
import { parseHash, refreshSession, state } from "./app.js";
import { h, icon, toast } from "./dom.js";

const MIN = 12, MAX = 128;

/** A password input with the same eye toggle as the key fields in Settings. */
function passwordField({ id, label, autocomplete, hint }) {
  let showing = false;
  const input = h("input.input", { id, type: "password", autocomplete, spellcheck: "false", autocapitalize: "off", required: true });
  const reveal = h("button.icon-btn", { type: "button", "aria-label": "Show password", "aria-pressed": "false", "aria-controls": id,
    on: { click: () => {
      showing = !showing;
      input.type = showing ? "text" : "password";
      reveal.setAttribute("aria-pressed", String(showing));
      reveal.setAttribute("aria-label", showing ? "Hide password" : "Show password");
    } } }, icon("eye"));
  const field = h("div.field", h("label", { for: id }, label), h("div.key-row", input, reveal), hint ? h("p.hint", hint) : null);
  return { input, field };
}

/** Disable a submit button while a request runs; returns a function that restores it. */
function busy(button, text) {
  const idle = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = text;
  return () => { button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = idle; };
}

/**
 * Change-password form, shared by the forced page and Settings.
 * onSaved runs after the server accepted the new password. requireCurrent=false hides the current-password field.
 */
export function passwordForm({ onSaved, requireCurrent = true, username = state.session.user?.name || "admin" } = {}) {
  const current = passwordField({ id: "pw-current", label: "Current password", autocomplete: "current-password" });
  const next = passwordField({ id: "pw-new", label: "New password", autocomplete: "new-password", hint: `${MIN} to ${MAX} characters.` });
  const confirm = passwordField({ id: "pw-confirm", label: "Confirm new password", autocomplete: "new-password" });
  const err = h("p.hint.field-error", { role: "alert", id: "pw-error" });
  const save = h("button.btn.primary", { type: "submit" }, "Save password");
  // Password managers file a new password under the account name; the field is there for them, not for people.
  const who = h("input.auth-username", { type: "text", name: "username", autocomplete: "username", value: username, readonly: true, tabindex: "-1", "aria-hidden": "true" });
  const inputs = [current, next, confirm].map((f) => f.input);
  const fail = (message, input) => {
    err.textContent = message;
    for (const i of inputs) i.removeAttribute("aria-invalid");
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", err.id);
    input.focus();
  };
  for (const i of inputs) i.addEventListener("input", () => { err.textContent = ""; i.removeAttribute("aria-invalid"); });

  const form = h("form.stack-12.auth-form", { novalidate: true, on: { submit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    if (requireCurrent && !current.input.value) return fail("Enter your current password.", current.input);
    if (next.input.value.length < MIN) return fail(`Use at least ${MIN} characters.`, next.input);
    if (next.input.value.length > MAX) return fail(`Use at most ${MAX} characters.`, next.input);
    if (next.input.value !== confirm.input.value) return fail("The passwords don't match.", confirm.input);
    const restore = busy(save, "Saving…");
    try {
      await api.changePassword(requireCurrent ? current.input.value : "", next.input.value);
    } catch (ex) {
      restore();
      return fail(ex.message, /current/i.test(ex.message) ? current.input : next.input);
    }
    restore();
    for (const i of inputs) i.value = "";
    toast("Password saved");
    await onSaved?.();
  } } },
    who, requireCurrent ? current.field : null, next.field, confirm.field, err, h("div.toolbar", save));
  return form;
}

function brand() {
  return h("div.auth-brand", h("span.brand-mark", { "aria-hidden": "true" }), h("span.brand-name", "Lorekeeper"));
}

export function mountLogin(root) {
  const next = safeNext(parseHash().params.get("next") || "");
  const user = h("input.input", { id: "login-user", name: "username", autocomplete: "username", autocapitalize: "off", spellcheck: "false", required: true, value: "admin" });
  const pass = passwordField({ id: "login-pass", label: "Password", autocomplete: "current-password" });
  const err = h("p.hint.field-error", { role: "alert", id: "login-error" });
  const go = h("button.btn.primary.block", { type: "submit" }, "Sign in");
  for (const i of [user, pass.input]) i.addEventListener("input", () => { err.textContent = ""; pass.input.removeAttribute("aria-invalid"); });

  const form = h("form.stack-12.auth-form", { novalidate: true, on: { submit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    if (!user.value.trim()) { err.textContent = "Enter your username."; user.focus(); return; }
    if (!pass.input.value) { err.textContent = "Enter your password."; pass.input.focus(); return; }
    const restore = busy(go, "Signing in…");
    try {
      await api.login(user.value.trim(), pass.input.value);
    } catch (ex) {
      restore();
      err.textContent = ex.message;
      pass.input.setAttribute("aria-invalid", "true");
      pass.input.setAttribute("aria-describedby", err.id);
      pass.input.focus();
      pass.input.select();
      return;
    }
    pass.input.value = "";
    await refreshSession(next);
  } } },
    h("div.field", h("label", { for: "login-user" }, "Username"), user),
    pass.field, go, err);

  const setup = state.session.setup;
  root.append(h("div.auth-page",
    h("div.auth-col",
      brand(),
      h("section.card.card-pad.auth-card", { "aria-labelledby": "login-title" },
        h("div.stack-12",
          h("div", h("h1#login-title", "Sign in"),
            h("p.muted", "Your library, runs and keys are behind this sign-in.")),
          setup ? h("div.notice.info", h("div", "First time here? Sign in with the password the server printed when it started (",
            h("span.mono", "docker compose logs lorekeeper"), "), then choose your own.")) : null,
          form)),
      h("p.hint.auth-foot", h("a", { href: "docs/self-hosting.html#sign-in" }, "Docs")))));
  (user.value ? pass.input : user).focus();
}

export function mountPassword(root) {
  const next = safeNext(parseHash().params.get("next") || "");
  const form = passwordForm({ onSaved: () => refreshSession(next) });
  root.append(h("div.auth-page",
    h("div.auth-col",
      brand(),
      h("section.card.card-pad.auth-card", { "aria-labelledby": "pw-title" },
        h("div.stack-12",
          h("div", h("h1#pw-title", "Choose a new password"),
            h("p.muted", "At least 12 characters. A sentence you will remember beats a short scramble.")),
          form)))));
  form.querySelector("#pw-current")?.focus();
}
