async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    // Required on every change; other websites can't send it (CSRF protection on the server).
    headers: { "X-Lorekeeper": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
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
  catalog: () => request("GET", "/api/catalog"),
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
  index: (key) => request("GET", `/api/library/${encodeURIComponent(key)}`),
  duplicates: (key) => request("GET", `/api/library/${encodeURIComponent(key)}/duplicates`),
  checkDuplicates: (key) => request("POST", `/api/library/${encodeURIComponent(key)}/duplicates/check`),
  merge: (key, pairs) => request("POST", `/api/library/${encodeURIComponent(key)}/merge`, { pairs }),
  undoMerge: (key, id) => request("POST", `/api/library/${encodeURIComponent(key)}/merge/${encodeURIComponent(id)}/undo`),
  markDistinct: (key, a, b) => request("POST", `/api/library/${encodeURIComponent(key)}/distinct`, { a, b }),
  /** Book Orbit's reading position for this index. A 404 means an older server: treated as "not synced", no message. */
  position: async (key) => {
    try {
      const res = await fetch(`/api/library/${encodeURIComponent(key)}/position`);
      if (res.status === 404) return { synced: false, reason: "no-route" };
      if (!res.ok) return { synced: false, reason: "unreachable" };
      return await res.json();
    } catch { return { synced: false, reason: "unreachable" }; }
  },
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
