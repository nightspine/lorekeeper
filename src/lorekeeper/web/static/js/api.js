/**
 * The query string of a mentions request. `fin` is the Reader's own value: null means its URL has no `fin` (the
 * default, earlier books finished), "" means none finished and is sent as `fin=`, never left out.
 */
export function mentionsQuery({ entity, seq, order = null, fin = null }) {
  return `entity=${encodeURIComponent(entity)}&seq=${seq}${order ? `&order=${encodeURIComponent(order)}` : ""}${fin != null ? `&fin=${encodeURIComponent(fin)}` : ""}`;
}
/** What a repaint is remembered by: a change in any part asks the server again. `rev` is the index's `provenance.updated` (an edit changes it; a merge does not, which is why `generation()` below also exists). */
export const mentionsSig = (key, id, seq, order = null, fin = null, rev = "") =>
  `${key}\n${id}\n${seq}\n${order ?? ""}\n${fin == null ? "-" : `fin:${fin}`}\n${rev ?? ""}`;

/** May the "Counting mentions…" line show? Only for the newest section whose request has not settled and whose box is still hidden. */
export const loadingMayShow = ({ current, settled, hidden }) => Boolean(current && !settled && hidden);

/**
 * Which mentions request is the newest. `next()` starts one; `invalidate()` (an edit or merge reloaded the index)
 * makes every request still pending or in flight stale, so its answer is neither painted nor remembered.
 */
export function generation() {
  let n = 0;
  return { next: () => ++n, invalidate: () => { n++; }, current: (g) => g === n };
}

async function request(method, url, body, opts = {}) {
  const res = await fetch(url, {
    method,
    // Required on every change; other websites can't send it (CSRF protection on the server).
    headers: { "X-Lorekeeper": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    signal: opts.signal,
  });
  if (!res.ok) {
    let detail = res.statusText, code = "", why = "";
    try { const j = await res.json(); detail = j.detail || detail; code = j.code || ""; why = typeof j.why === "string" ? j.why : ""; } catch {}
    if (!opts.auth) {
      if (res.status === 401) redirectToLogin();
      else if (res.status === 403 && code === "password-change-required") redirectTo("#/password");
    }
    const err = new Error(detail);
    err.status = res.status;
    err.code = code;
    if (why) err.why = why;
    throw err;
  }
  return res.json();
}

/** Only same-page hashes are ever followed after sign-in; never a URL, never the sign-in pages themselves. */
export function safeNext(next) {
  if (typeof next !== "string" || !next.startsWith("#/")) return "";
  const route = next.slice(2).split(/[/?]/)[0];
  return route === "login" || route === "password" ? "" : next;
}
function redirectTo(hash) {
  if (location.hash !== hash) location.hash = hash;
}
/** app.js sets `hooks.unauthorized` so a 401 on a stale cookie also marks the page as signed out. */
export const hooks = { unauthorized: null };
function redirectToLogin() {
  hooks.unauthorized?.();
  const here = location.hash;
  if (/^#\/login(\?|$)/.test(here)) return;
  const next = safeNext(here);
  redirectTo("#/login" + (next ? "?next=" + encodeURIComponent(next) : ""));
}

let catalogCache = null;
let aboutCache = null;
export const api = {
  /** The server's version ({version}), asked once per page load. Plain fetch, never redirects: signed out, a failed
   *  request or an older server without the route answer null (and a later call asks again). */
  about: () => {
    if (!aboutCache) {
      aboutCache = fetch("/api/about").then((res) => (res.ok ? res.json() : null)).catch(() => null)
        .then((d) => (d && typeof d.version === "string" && /^[\w.+-]{1,40}$/.test(d.version) ? d : null));
      aboutCache.then((d) => { if (!d) aboutCache = null; });
    }
    return aboutCache;
  },
  /** Who is signed in. Plain fetch, never redirects; a 404 or a failed request means an older server with no sign-in. */
  session: async () => {
    try {
      const res = await fetch("/api/auth/session");
      if (!res.ok) return { auth: false };
      return await res.json();
    } catch { return { auth: false }; }
  },
  login: (username, password) => request("POST", "/api/auth/login", { username, password }, { auth: true }),
  logout: () => request("POST", "/api/auth/logout", undefined, { auth: true }),
  changePassword: (current, next) => request("POST", "/api/auth/password", { current, new: next }, { auth: true }),
  ollama: () => request("GET", "/api/ollama"),
  // Cloud model providers (Claude, OpenAI, Gemini, ...): status, models and prices; keys are only ever returned masked.
  providers: (fresh = false) => request("GET", `/api/providers${fresh ? "?fresh=true" : ""}`),
  setProvider: (id, body) => request("PUT", `/api/providers/${encodeURIComponent(id)}`, body),
  deleteProvider: (id) => request("DELETE", `/api/providers/${encodeURIComponent(id)}`),
  claude: (fresh = false) => request("GET", `/api/claude${fresh ? "?fresh=true" : ""}`),   // legacy alias, kept for the older build dialog
  books: () => request("GET", "/api/books"),
  deleteBook: (id) => request("DELETE", `/api/books/${id}`),
  jobs: () => request("GET", "/api/jobs"),
  job: (id) => request("GET", `/api/jobs/${id}`),
  createJob: (book_id, config) => request("POST", "/api/jobs", { book_id, config }),
  stopJob: (id) => request("POST", `/api/jobs/${id}/stop`),
  resumeJob: (id) => request("POST", `/api/jobs/${id}/resume`),
  deleteJob: (id) => request("DELETE", `/api/jobs/${id}`),
  library: () => request("GET", "/api/library"),
  /** The catalog, fresh. Its answer also fills the shared copy below. */
  catalog: () => {
    const promise = request("GET", "/api/catalog");
    catalogCache = { at: Date.now(), promise };
    promise.catch(() => { if (catalogCache?.promise === promise) catalogCache = null; });
    return promise;
  },
  /** The catalog no older than `maxAge` ms: one request shared by the rail's count, the palette and a page opening
   *  at the same moment (an answer still on its way counts). */
  catalogShared: (maxAge = 60000) => (catalogCache && Date.now() - catalogCache.at < maxAge ? catalogCache.promise : api.catalog()),
  book: (key) => request("GET", `/api/catalog/books/${encodeURIComponent(key)}`),
  preflight: (key) => request("GET", `/api/catalog/books/${encodeURIComponent(key)}/preflight`),
  prepare: (key) => request("POST", `/api/catalog/books/${encodeURIComponent(key)}/prepare`),
  setDefault: (key, version) => request("PUT", `/api/catalog/books/${encodeURIComponent(key)}/default`, { version }),
  setSeries: (key, name, index) => request("PUT", `/api/catalog/books/${encodeURIComponent(key)}/series`, { name, index }),
  buildMany: (keys, config) => request("POST", "/api/catalog/build", { keys, config }),
  bookorbit: () => request("GET", "/api/sources/bookorbit"),
  connectBookOrbit: (url, username, password) => request("POST", "/api/sources/bookorbit", { url, username, password }),
  syncBookOrbit: () => request("POST", "/api/sources/bookorbit/sync"),
  bookorbitLibraries: (ids) => request("PUT", "/api/sources/bookorbit/libraries", { ids }),
  disconnectBookOrbit: () => request("DELETE", "/api/sources/bookorbit"),
  askStatus: () => request("GET", "/api/ask"),
  /** One question, answered from the index's gated view; `signal` lets the person cancel the wait. */
  ask: (key, body, signal) => request("POST", `/api/library/${encodeURIComponent(key)}/ask`, body, { signal }),
  index: (key) => request("GET", `/api/library/${encodeURIComponent(key)}`),
  duplicates: (key) => request("GET", `/api/library/${encodeURIComponent(key)}/duplicates`),
  checkDuplicates: (key) => request("POST", `/api/library/${encodeURIComponent(key)}/duplicates/check`),
  merge: (key, pairs) => request("POST", `/api/library/${encodeURIComponent(key)}/merge`, { pairs }),
  undoMerge: (key, id) => request("POST", `/api/library/${encodeURIComponent(key)}/merge/${encodeURIComponent(id)}/undo`),
  markDistinct: (key, a, b) => request("POST", `/api/library/${encodeURIComponent(key)}/distinct`, { a, b }),
  /** One change to an index built here (see editor.js); the response carries an undo id. */
  edit: (key, op) => request("POST", `/api/library/${encodeURIComponent(key)}/edits`, op),
  /** Undo the latest change, edit or merge. */
  undo: (key, id) => request("POST", `/api/library/${encodeURIComponent(key)}/undo/${encodeURIComponent(id)}`),
  /** Paragraphs around `seq` in an edition on disk, for choosing a reveal point. */
  blocks: (hash, seq, before = 2, after = 2, indexKey = null) =>
    request("GET", `/api/editions/${encodeURIComponent(hash)}/blocks?seq=${seq}&before=${before}&after=${after}${indexKey ? `&i=${encodeURIComponent(indexKey)}` : ""}`),
  /** Mentions of one entry per chapter, up to block `seq` (finished). Null when the server has nothing to count (no EPUB, older server, entry not visible). */
  mentions: async (key, entity, seq, { order = null, fin = null } = {}) => {
    // order and fin are the Reader's own URL values, so the server gates with the view the drawer shows.
    const res = await fetch(`/api/library/${encodeURIComponent(key)}/mentions?${mentionsQuery({ entity, seq, order, fin })}`);
    if (!res.ok) return null;
    return res.json();
  },
  /** Start an empty index for a library book, to write by hand. */
  handmade: (catalogKey, license = null) => request("POST", `/api/catalog/books/${encodeURIComponent(catalogKey)}/handmade`, license ? { license } : {}),
  /** Book Orbit's reading position for this index. A 404 means an older server: treated as "not synced", no message. */
  position: async (key) => {
    try {
      const res = await fetch(`/api/library/${encodeURIComponent(key)}/position`);
      if (res.status === 404) return { synced: false, reason: "no-route" };
      if (!res.ok) return { synced: false, reason: "unreachable" };
      return await res.json();
    } catch { return { synced: false, reason: "unreachable" }; }
  },
  // ---- community indexes (the server talks to the registry; the browser never does) ----
  communityConfig: () => request("GET", "/api/community"),
  setCommunity: (enabled, url) => request("PUT", "/api/community", url ? { enabled, url } : { enabled }),
  communityBook: (catalogKey) => request("GET", `/api/community/books/${encodeURIComponent(catalogKey)}`),
  communityFetch: (catalogKey, id, digest) => request("POST", "/api/community/fetch", { catalogKey, id, digest }),
  communityPreview: (hex) => request("GET", `/api/community/preview/${encodeURIComponent(hex)}`),
  communityInstall: (catalogKey, id, digest) => request("POST", "/api/community/install", { catalogKey, id, digest }),
  communityUninstall: (libraryKey) => request("DELETE", `/api/community/installed/${encodeURIComponent(libraryKey)}`),
  communityAudit: (digest) => request("POST", "/api/community/audit", { digest }),
  shareCheck: (libraryKey) => request("POST", `/api/community/share/${encodeURIComponent(libraryKey)}/check`),
  shareAudit: (libraryKey) => request("POST", `/api/community/share/${encodeURIComponent(libraryKey)}/audit`),
  shareBundleUrl: (libraryKey) => `/api/community/share/${encodeURIComponent(libraryKey)}/bundle`,
  // ---- pictures (art sets beside an index; the Reader gates them by `from`, see art.js) ----
  /** The art set of an index in one style (the server's default without one): the manifest, the styles present, `screened`.
   *  Null when the index has no art set or the server has no picture routes (an older server). */
  art: async (key, style = null) => {
    const res = await fetch(`/api/library/${encodeURIComponent(key)}/art${style ? `?style=${encodeURIComponent(style)}` : ""}`);
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(res.statusText);
    return res.json();
  },
  /** Write the prompt for one picture (one text call, nothing drawn). `p` is the reader's frontier anchor. */
  artPrompt: (key, body, signal) => request("POST", `/api/library/${encodeURIComponent(key)}/art/prompt`, body, { signal }),
  /** Draw a picture from the given (possibly edited) prompt and file it; a refusal is a 422 with code "refused". */
  artDraw: (key, body, signal) => request("POST", `/api/library/${encodeURIComponent(key)}/art/draw`, body, { signal }),
  artDelete: (key, id) => request("DELETE", `/api/library/${encodeURIComponent(key)}/art/${encodeURIComponent(id)}`),
  artRedraw: (key, id, note) => request("POST", `/api/library/${encodeURIComponent(key)}/art/${encodeURIComponent(id)}/redraw`, { note }),
  /** Edit the world note of one style's art set (the style's slug: each style has its own manifest). */
  artWorld: (key, text, style = null) => request("PUT", `/api/library/${encodeURIComponent(key)}/art/world`, style ? { text, style } : { text }),
  /** { pictures, price, chapters } for the Pictures choices; `key` may be empty before a first build (the server then counts roughly or 404s). */
  artEstimate: (q) => request("GET", `/api/art/estimate?${new URLSearchParams(Object.entries(q).filter(([, v]) => v != null && v !== "").map(([k, v]) => [k, String(v)]))}`),
  /** The standalone "Add pictures" run on an existing index: a job of kind "art". */
  artRun: (key, art) => request("POST", `/api/library/${encodeURIComponent(key)}/art/run`, { art }),
  /** Every art set on disk, per book: [{ key, title, textHash, styles: [{ style, slug, pictures, bytes }], bytes }]. */
  artSets: () => request("GET", "/api/art"),
  /** Remove a book's pictures (one style with `style`); 409 while a run draws them; undoable. */
  artRemove: (textHash, style = null) => request("DELETE", `/api/art/${encodeURIComponent(textHash)}${style ? `?style=${encodeURIComponent(style)}` : ""}`),
  edition: async (hash) => {
    const res = await fetch(`/api/editions/${hash}`);
    return res.ok ? res.json() : null;
  },
  /** Upload with progress (fetch has no upload progress events). */
  upload(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append("file", file);
      xhr.open("POST", "/api/books");
      xhr.setRequestHeader("X-Lorekeeper", "1");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error((data && data.detail) || `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error("Upload failed: network error"));
      xhr.send(form);
    });
  },
};
