"""Local web UI: Studio (upload, configure, run and watch generation) and Reader (explore an index).

    uv run --extra web lorekeeper web            # http://127.0.0.1:8765

The server binds to localhost by default. Paths are never taken from requests: books are
addressed by content hash, indexes by keys from a server-side scan of known directories.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import epub, llm, text
from ..generate import slug
from . import bookorbit, position, protocol
from .catalog import Catalog, series_num, story_chapters
from .identity import Identity
from .jobs import BookStore, JobManager, describe
from .secrets import Keys, SecretStore, mask

STATIC = Path(__file__).parent / "static"
# Words per second with the support check on, measured on a 16 GB M4; replaced by observed rates.
DEFAULT_THROUGHPUT = {"gemma4:12b-it-qat": 9.9, "qwen3.5:9b-q4_K_M": 13, "granite4.2:8b": 11,
                      "claude-haiku-5-5": 300}


class Library:
    """Index and series files found in a set of directories, plus EPUBs to map positions onto."""

    def __init__(self, index_dirs: list[tuple[str, Path]], epub_dirs: list[Path]) -> None:
        self.index_dirs, self.epub_dirs = index_dirs, epub_dirs
        self._editions: dict[Path, tuple[float, str]] = {}   # epub path → (mtime, textHash)

    def scan(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for label, d in self.index_dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*.json")):
                if p.name.endswith((".state", ".report")) or ".events" in p.name:
                    continue
                try:
                    doc = json.loads(p.read_text())
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(doc, dict) or doc.get("kind") not in ("index", "series"):
                    continue
                key = f"{label}--{p.relative_to(d).with_suffix('').as_posix().replace('/', '--')}"
                found[key] = {"key": key, "path": p, "source": label, "doc": doc}
        return found

    def summary(self) -> dict:
        indexes, series = [], []
        for item in self.scan().values():
            doc = item["doc"]
            if doc["kind"] == "series":
                series.append({"key": item["key"], "id": doc["id"], "title": doc["title"],
                               "books": doc["books"], "orders": doc["orders"], "defaultOrder": doc["defaultOrder"]})
                continue
            prov = doc.get("provenance", {})
            indexes.append({
                "key": item["key"], "source": item["source"], "id": doc.get("id"),
                "title": doc.get("work", {}).get("title"), "authors": doc.get("work", {}).get("authors", []),
                "series": doc.get("series"), "complete": doc.get("coverage", {}).get("complete"),
                "entities": len(doc.get("entities", [])), "anchors": len(doc.get("anchors", [])),
                "model": (prov.get("generator") or {}).get("model"), "updated": prov.get("updated") or prov.get("created"),
                "textHashes": [e.get("textHash") for e in doc.get("editions", [])],
                "hasEdition": any(self.edition_path(e.get("textHash", "")) for e in doc.get("editions", [])),
            })
        return {"indexes": indexes, "series": series}

    def edition_path(self, text_hash: str) -> Path | None:
        if not text_hash:
            return None
        for d in self.epub_dirs:
            if not d.is_dir():
                continue
            for p in d.rglob("*.epub"):
                mtime = p.stat().st_mtime
                cached = self._editions.get(p)
                if cached is None or cached[0] != mtime:
                    try:
                        h = text.text_hash(b.words for b in epub.blocks(p))
                    except Exception:
                        h = ""
                    self._editions[p] = cached = (mtime, h)
                if cached[1] == text_hash:
                    return p
        return None


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def create_app(data_dir: Path, index_dirs: list[tuple[str, Path]], epub_dirs: list[Path],
               ollama_url: str = "http://localhost:11434",
               allowed_hosts: tuple[str, ...] | None = LOCAL_HOSTS,
               api_token: str | None = None) -> FastAPI:
    """allowed_hosts=None disables the Host check (only for deliberate remote access)."""
    secrets = SecretStore(data_dir / "secrets.json")
    keys = Keys(secrets)
    books = BookStore(data_dir / "books")
    jobs = JobManager(data_dir / "jobs", books, data_dir / "indexes", ollama_url,
                      keys=keys.get, provider_config=keys.config,
                      prior_for=lambda job: catalog.prior_indexes(job.config.get("catalog_key", "")),
                      distinct_for=lambda job: distinct_for(job))
    library = Library([("generated", data_dir / "indexes"), *index_dirs], [data_dir / "books", *epub_dirs])
    catalog = Catalog(data_dir, books, jobs, library)

    def distinct_for(job) -> set:
        """Pairs of entity ids a person marked "not the same", for the group this run writes into:
        the series (`series:<id>`), else the index file itself, so a build never re-merges them."""
        sid = job.config.get("series_id")
        if sid:
            gid = f"series:{sid}"
        else:
            rel = Path(job.output).relative_to(data_dir / "indexes").with_suffix("").as_posix()
            gid = f"generated--{rel.replace('/', '--')}"
        return identity.distinct(gid)

    def judge_model(tier: str = "cheap"):
        """For proposing pairs from the whole cast: the first configured cloud provider's cheap model
        (Claude first), else local Ollama if it's up."""
        for p in jobs.configured():
            spec = llm.PROVIDERS[p]
            name = keys.config(p).get("model") if p == "custom" else spec[tier]
            if name:
                return jobs.cloud_model(p, name)
        try:
            with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=2) as r:
                models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        except (OSError, ValueError):
            return None
        busy = any(j.status in ("running", "stopping") and j.config.get("provider") in (None, "ollama")
                   for j in jobs.jobs.values())
        if busy or llm.DEFAULT_MODEL not in models:
            return None   # one Ollama model at a time: don't compete with a running build
        return llm.make("ollama", llm.DEFAULT_MODEL, url=ollama_url)

    def strong_judge_model():
        """The strong model of the same provider judges the pairs (a few hundred short pairs cost cents
        and the verdicts decide merges); otherwise the same model as `judge_model`."""
        return judge_model("strong")

    identity = Identity(data_dir, library, catalog, jobs, checker=judge_model, judge_checker=strong_judge_model)
    covers = data_dir / "bookorbit" / "covers"

    # ---- Book Orbit connection (session = rotating refresh token, kept with the other secrets)
    bo: dict = {"client": None}

    def save_session(sess: bookorbit.Session) -> None:
        secrets.set("bookorbit", json.dumps(sess.__dict__))

    def bo_client() -> bookorbit.BookOrbit:
        if bo["client"] is None:
            raw = secrets.get("bookorbit")
            if not raw:
                raise HTTPException(409, "Book Orbit isn't connected. Connect it in Settings.")
            bo["client"] = bookorbit.BookOrbit(bookorbit.Session(**json.loads(raw)), save_session)
        return bo["client"]

    def bo_status() -> dict:
        raw = secrets.get("bookorbit")
        cache = catalog.bookorbit_cache()
        if not raw:
            return {"connected": False}
        sess = json.loads(raw)
        on = catalog.state().get("bookorbitLibraries")
        libs = [{**lib, "enabled": on is None or lib["id"] in on} for lib in cache.get("libraries", [])]
        return {"connected": True, "url": sess["url"], "username": sess["username"], "name": sess.get("name"),
                "sessionExpires": sess.get("refresh_expires_at"), "synced": cache.get("synced"),
                "libraries": libs, "librariesChosen": on is not None,
                "books": len(catalog.bookorbit_books()),
                "skipped": sum(lib.get("skipped", 0) for lib in libs if lib["enabled"])}

    def prepare(key: str) -> str:
        """Make sure the book's EPUB is on this machine; returns its local book id."""
        e = catalog.entry(key)
        if e is None:
            raise HTTPException(404, "no such book")
        if e["localId"]:
            return e["localId"]
        if e["source"] != "bookorbit":
            raise HTTPException(409, "this book has no file")
        b = next((x for x in catalog.bookorbit_books() if f"bo:{x['id']}" == key), None)
        if not b or not b.get("epubFileId"):
            raise HTTPException(409, "Book Orbit has no EPUB for this book, so there's no text to index.")
        try:
            data = bo_client().download_text_epub(b["epubFileId"])
            meta = books.add(data, f"{b['title']}.epub")
        except bookorbit.BookOrbitError as ex:
            raise HTTPException(502, str(ex)) from ex
        except ValueError as ex:
            raise HTTPException(422, f"Book Orbit's file couldn't be read as an EPUB: {ex}") from ex
        catalog.update_state(lambda st: st["links"].__setitem__(key, meta["id"]))
        return meta["id"]

    def submit_for(key: str, config: dict):
        """Queue a build for a catalog book, with series ids filled in from the catalog."""
        local = prepare(key)
        e = catalog.entry(key)
        config = {**config, "catalog_key": key}
        if e["series"] and not config.get("series_id"):
            config["series_id"] = e["series"]["id"]
            config["book_key"] = slug(e["title"])
            manifest = catalog.series_manifest(e["series"]["id"])
            if manifest:
                (data_dir / "indexes").mkdir(parents=True, exist_ok=True)
                (data_dir / "indexes" / f"{e['series']['id']}.series.json").write_text(json.dumps(manifest, indent=2))
        story = story_chapters(books.get(local))
        if not config.get("chapters"):
            config["chapters"] = story or None
        config["whole_story"] = bool(story) and set(story) <= set(config.get("chapters") or [])
        return jobs.submit(local, config)
    app = FastAPI(title="Lorekeeper", docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    # The docs site (static/docs) links ../css, ../fonts and ../vendor so that it works unchanged when
    # `lorekeeper docs export` copies it to a static host. Serve those folders at the root to match.
    app.mount("/docs", StaticFiles(directory=STATIC / "docs", html=True), name="docs")
    for folder in ("css", "fonts", "vendor"):
        app.mount(f"/{folder}", StaticFiles(directory=STATIC / folder), name=f"docs-{folder}")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS rebinding: a hostile site resolving its own name to 127.0.0.1 still sends its Host.
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        if allowed_hosts is not None and host not in allowed_hosts:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        # CSRF: other origins can't send a custom header without a CORS preflight, which we never allow.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-lorekeeper") != "1":
            return JSONResponse({"detail": "missing X-Lorekeeper header"}, status_code=403)
        return await call_next(request)

    def job_json(job) -> dict:
        return {**asdict(job), "output_name": Path(job.output).name}

    @app.get("/")
    def root():
        return FileResponse(STATIC / "index.html")

    def repo_markdown(name: str):
        """Read-only copy of a repository file for the docs site; absent in an installed wheel."""
        path = Path(__file__).resolve().parents[3] / name
        if not path.is_file():
            raise HTTPException(404, f"{name} is not part of this installation")
        return FileResponse(path, media_type="text/markdown; charset=utf-8")

    @app.get("/spec.md", include_in_schema=False)
    def spec_md():
        return repo_markdown("SPEC.md")

    @app.get("/readme.md", include_in_schema=False)
    def readme_md():
        return repo_markdown("README.md")

    @app.get("/api/ollama")
    def ollama_status():
        def get(path: str):
            with urllib.request.urlopen(f"{ollama_url}{path}", timeout=3) as r:
                return json.load(r)
        try:
            tags, ps = get("/api/tags"), get("/api/ps")
        except Exception as e:
            return {"ok": False, "error": str(e), "url": ollama_url, "models": [], "loaded": [],
                    "default": llm.DEFAULT_MODEL, "throughput": {**DEFAULT_THROUGHPUT, **jobs.throughput()}}
        models = [{"name": m["name"], "size": m.get("size"), "family": m.get("details", {}).get("family"),
                   "parameters": m.get("details", {}).get("parameter_size")} for m in tags.get("models", [])]
        return {"ok": True, "url": ollama_url, "models": models, "loaded": [m["name"] for m in ps.get("models", [])],
                "default": llm.DEFAULT_MODEL, "throughput": {**DEFAULT_THROUGHPUT, **jobs.throughput()}}

    check_cache: dict = {}

    def check_anthropic(key: str | None) -> tuple[bool, str | None]:
        """Try a free, read-only call with this key (None = the SDK's own credential lookup)."""
        try:
            import anthropic
        except ImportError:
            return False, "Install the Claude extra: uv sync --extra claude"
        try:
            anthropic.Anthropic(api_key=key, timeout=8, max_retries=0).models.list(limit=1)
            return True, None
        except anthropic.AuthenticationError:
            return False, "Anthropic rejected this API key."
        except anthropic.PermissionDeniedError:
            return False, "This API key doesn't have access to the Messages API."
        except anthropic.APIConnectionError:
            return False, "Couldn't reach the Anthropic API. Check your internet connection."
        except Exception as e:  # missing credentials and anything unexpected
            if "api_key" in str(e).lower() or "credential" in str(e).lower() or "auth" in str(e).lower():
                return False, "No Anthropic API key yet."
            return False, f"Claude check failed: {type(e).__name__}"

    def check_provider(pid: str, key: str | None, base_url: str | None = None) -> tuple[bool, str | None, list[str]]:
        """(ok, error, live model ids). A free call: models.list for Anthropic, GET /models otherwise."""
        spec = llm.PROVIDERS[pid]
        if spec["kind"] == "anthropic":
            ok, error = check_anthropic(key)
            return ok, error, []
        if pid != "custom" and not key:
            return False, f"No {spec['label']} API key yet.", []
        try:
            return True, None, llm.list_models(pid, key, base_url)
        except llm.ProviderError as e:
            if pid == "custom" and e.status in (404, 405):
                return True, None, []   # a server without /models: nothing to verify against
            return False, str(e), []

    def provider_status(pid: str, fresh: bool = False) -> dict:
        """Everything the UI needs about one provider. The key is only ever returned masked."""
        spec = llm.PROVIDERS[pid]
        cfg = keys.config(pid)
        key = keys.get(pid)
        configured = bool(key) or (pid == "custom" and bool(cfg.get("base_url")))
        sig = (key, cfg.get("base_url"))
        cached = check_cache.get(pid)
        if configured and (fresh or not cached or cached["sig"] != sig or cached["t"] < time.time() - 120):
            ok, error, live = check_provider(pid, key, cfg.get("base_url"))
            cached = check_cache[pid] = {"sig": sig, "t": time.time(), "ok": ok, "error": error, "live": live}
        elif not configured:
            cached = None
        presets = [{"id": m["id"], "label": m["label"], "price": m["price"], "tier": m["tier"]} for m in spec["models"]]
        known = {m["id"] for m in presets}
        live = [{"id": i, "label": i, "price": None, "tier": None} for i in (cached or {}).get("live", []) if i not in known]
        return {"id": pid, "label": spec["label"], "kind": spec["kind"], "key_url": spec["key_url"], "key_env": spec["key_env"],
                "configured": configured, "source": keys.source(pid), "masked": keys.masked(pid),
                "ok": bool(cached and cached["ok"]), "error": cached["error"] if cached else None,
                "base_url": cfg.get("base_url") or spec["base_url"], "model": cfg.get("model"),
                "models": presets, "liveModels": live,
                "default": spec["default"] or cfg.get("model"), "cheap": spec["cheap"] or cfg.get("model"),
                "strong": spec["strong"] or cfg.get("model")}

    def provider_id(pid: str) -> str:
        pid = "claude" if pid == "anthropic" else pid
        if pid not in llm.PROVIDERS:
            raise HTTPException(404, f"unknown provider {pid!r}")
        return pid

    @app.get("/api/providers")
    def list_providers(fresh: bool = False):
        return {"providers": [provider_status(p, fresh) for p in llm.PROVIDERS],
                "throughput": {**DEFAULT_THROUGHPUT, **jobs.throughput()}}

    @app.get("/api/claude")
    def claude_status(fresh: bool = False):
        """Legacy shape of the Claude status, kept for older clients."""
        st = provider_status("claude", fresh)
        models = [{"name": m["id"], "label": m["label"], "price_in": m["price"][0], "price_out": m["price"][1]}
                  for m in st["models"]]
        return {**st, "models": models, "default": llm.DEFAULT_CLAUDE_MODEL,
                "throughput": {**DEFAULT_THROUGHPUT, **jobs.throughput()}}

    @app.put("/api/providers/{pid}")
    async def set_provider(pid: str, request: Request):
        legacy = pid == "anthropic"
        pid = provider_id(pid)
        spec = llm.PROVIDERS[pid]
        body = await request.json()
        key = (body.get("key") or body.get("api_key") or "").strip()
        base_url = (body.get("base_url") or "").strip().rstrip("/") or None
        model = (body.get("model") or "").strip() or None
        if any(c.isspace() for c in key):
            raise HTTPException(400, "That doesn't look like an API key.")
        if pid == "claude" and (not key.startswith("sk-ant-") or len(key) < 20):
            raise HTTPException(400, "That doesn't look like an Anthropic API key (they start with sk-ant-).")
        if pid != "claude" and pid != "custom" and len(key) < 8:
            raise HTTPException(400, f"That doesn't look like a {spec['label']} API key.")
        if pid == "custom":
            if not base_url or not base_url.startswith(("http://", "https://")):
                raise HTTPException(400, "Enter the server's base URL, for example http://localhost:1234/v1.")
            if not model:
                raise HTTPException(400, "Enter the model name the server should use.")
            key = key or (keys.stored(pid) or "")
        elif not key:
            raise HTTPException(400, "Enter an API key.")
        ok, error, live = check_provider(pid, key or None, base_url)
        if not ok:
            raise HTTPException(400, error)
        if key:
            keys.set(pid, key)
        keys.set_config(pid, base_url, model)
        check_cache.pop(pid, None)
        return claude_status(fresh=True) if legacy else provider_status(pid, fresh=True)

    @app.delete("/api/providers/{pid}")
    def delete_provider(pid: str):
        legacy = pid == "anthropic"
        pid = provider_id(pid)
        keys.delete(pid)
        keys.delete_config(pid)
        check_cache.pop(pid, None)
        return claude_status(fresh=True) if legacy else provider_status(pid, fresh=True)

    @app.get("/api/books")
    def list_books():
        return books.list()

    @app.post("/api/books")
    async def upload_book(file: UploadFile = File(...)):
        data = await file.read()
        try:
            return books.add(data, file.filename or "book.epub")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/books/{book_id}")
    def delete_book(book_id: str):
        try:
            books.delete(book_id)
        except KeyError as e:
            raise HTTPException(404, "no such book") from e
        return {"ok": True}

    @app.get("/api/jobs")
    def list_jobs():
        return sorted((job_json(j) for j in jobs.jobs.values()), key=lambda j: -j["created"])

    @app.post("/api/jobs")
    async def create_job(request: Request):
        body = await request.json()
        config = body.get("config", {})
        if config.get("provider", "ollama") not in ("ollama", *llm.PROVIDERS):
            raise HTTPException(400, f"unknown provider {config.get('provider')!r}")
        try:
            job = (submit_for(config["catalog_key"], config) if config.get("catalog_key")
                   else jobs.submit(body["book_id"], config))
        except KeyError as e:
            raise HTTPException(400, f"unknown book or missing field: {e}") from e
        return job_json(job)

    # ---- catalog -------------------------------------------------------------------------------

    @app.get("/api/catalog")
    def catalog_all():
        entries = catalog.entries()
        return {"books": entries, "series": catalog.series(entries), "bookorbit": bo_status()}

    @app.get("/api/catalog/books/{key}")
    def catalog_book(key: str):
        entries = catalog.entries()
        e = next((x for x in entries if x["key"] == key), None)
        if e is None:
            raise HTTPException(404, "no such book")
        series = None
        if e["series"]:
            g = next(s for s in catalog.series(entries) if s["id"] == e["series"]["id"])
            by_key = {x["key"]: x for x in entries}
            series = {**g, "books": [{k: by_key[b][k] for k in ("key", "title", "series", "status", "coverage")}
                                     for b in g["books"]]}
        runs = [job_json(j) for j in jobs.jobs.values()
                if j.config.get("catalog_key") == key or (e["localId"] and j.book_id == e["localId"])]
        meta = books.get(e["localId"]) if e["localId"] else None
        return {**e, "seriesInfo": series, "runs": sorted(runs, key=lambda j: -j["created"]),
                "chapters": meta.get("chapters") if meta else None,
                "preflight": catalog.preflight(key)}

    @app.put("/api/catalog/books/{key}/default")
    async def set_default(key: str, request: Request):
        version = (await request.json()).get("version")
        e = catalog.entry(key)
        if e is None or not any(v["key"] == version for v in e["versions"]):
            raise HTTPException(404, "no such version for this book")
        catalog.update_state(lambda st: st["defaults"].__setitem__(key, version))
        return catalog.entry(key)

    @app.put("/api/catalog/books/{key}/series")
    async def set_series(key: str, request: Request):
        body = await request.json()
        if not key.startswith("up:"):
            raise HTTPException(409, "Series for Book Orbit books come from Book Orbit.")
        name = " ".join(str(body.get("name") or "").split())[:200]
        index = str(body.get("index") or "").strip()
        if index and series_num(index) is None:
            raise HTTPException(400, "Use a number for the position in the series, like 2 or 2.5.")
        catalog.update_state(lambda st: st["series"].__setitem__(key, {"name": name, "index": index})
                             if name else st["series"].pop(key, None))
        return catalog.entry(key)

    @app.get("/api/catalog/books/{key}/preflight")
    def preflight(key: str):
        return catalog.preflight(key)

    @app.post("/api/catalog/books/{key}/prepare")
    def prepare_book(key: str):
        return {"localId": prepare(key)}

    @app.post("/api/catalog/build")
    async def build_many(request: Request):
        """Queue several books in the given order (e.g. a series' missing books, earliest first)."""
        body = await request.json()
        out = []
        for key in body.get("keys", []):
            out.append(job_json(submit_for(key, dict(body.get("config", {})))))
        return {"jobs": out}

    @app.get("/api/catalog/cover/{key}")
    def cover(key: str, v: str = "0"):
        if key.startswith("up:"):
            try:
                path = books.cover_path(key[3:])   # validates the id; never a path from the request
            except KeyError:
                path = None
            if path is None:
                raise HTTPException(404, "no cover")
            return FileResponse(path, headers={"Cache-Control": "max-age=86400"})
        if not key.startswith("bo:") or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key[3:]) or len(v) > 300:
            raise HTTPException(404, "no cover")
        # Book Orbit's cover version is an opaque string (e.g. a timestamp pair); hash it for the cache name.
        path = covers / f"{key[3:]}-{hashlib.sha256(v.encode()).hexdigest()[:16]}.jpg"
        if not path.exists():
            try:
                data, _ = bo_client().thumbnail(key[3:])
            except bookorbit.BookOrbitError as ex:
                raise HTTPException(404, "no cover") from ex
            covers.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    # ---- Book Orbit source ----------------------------------------------------------------------

    def sync() -> dict:
        try:
            client = bo_client()
            catalog.save_bookorbit(client.books(), client.session.url, client.libraries())
        except bookorbit.BookOrbitError as ex:
            raise HTTPException(502, str(ex)) from ex
        return bo_status()

    @app.get("/api/sources/bookorbit")
    def bookorbit_status():
        return bo_status()

    @app.post("/api/sources/bookorbit")
    async def bookorbit_connect(request: Request):
        body = await request.json()
        try:
            bo["client"] = bookorbit.BookOrbit.sign_in(body.get("url", ""), str(body.get("username", "")).strip(),
                                                       str(body.get("password", "")), save_session)
        except bookorbit.BookOrbitError as ex:
            raise HTTPException(400, str(ex)) from ex
        catalog.set_bookorbit_libraries(None)   # a new connection: the user picks libraries again
        return sync()

    @app.put("/api/sources/bookorbit/libraries")
    async def bookorbit_libraries(request: Request):
        ids = (await request.json()).get("ids")
        known = {lib["id"] for lib in catalog.bookorbit_cache().get("libraries", [])}
        if not isinstance(ids, list) or not all(isinstance(i, str) and i in known for i in ids):
            raise HTTPException(400, "unknown library")
        catalog.set_bookorbit_libraries(ids)
        return bo_status()

    @app.post("/api/sources/bookorbit/sync")
    def bookorbit_sync():
        return sync()

    @app.delete("/api/sources/bookorbit")
    def bookorbit_disconnect():
        if bo["client"] or secrets.get("bookorbit"):
            try:
                bo_client().sign_out()
            except HTTPException:
                pass
        bo["client"] = None
        secrets.delete("bookorbit")
        catalog.clear_bookorbit()
        catalog.set_bookorbit_libraries(None)
        return bo_status()

    def _job(job_id: str):
        if job_id not in jobs.jobs:
            raise HTTPException(404, "no such job")
        return jobs.jobs[job_id]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return job_json(_job(job_id))

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str):
        return job_json(jobs.stop(_job(job_id).id))

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str):
        try:
            return job_json(jobs.resume(_job(job_id).id))
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        try:
            jobs.delete(_job(job_id).id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, after: int = 0):
        _job(job_id)

        async def stream():
            n = after
            idle = 0
            while not await request.is_disconnected():
                evs = jobs.events_after(job_id, n)
                for e in evs:
                    yield f"id: {e['n']}\ndata: {json.dumps(e)}\n\n"
                n += len(evs)
                idle = 0 if evs else idle + 1
                if idle and idle % 30 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str):
        out = Path(_job(job_id).output)
        if not out.exists():
            raise HTTPException(404, "no output yet")
        return FileResponse(out, media_type="application/json", filename=out.name)

    @app.get("/api/library")
    def library_summary():
        return library.summary()

    @app.get("/api/library/{key}")
    def library_doc(key: str):
        item = library.scan().get(key)
        if item is None:
            raise HTTPException(404, "no such index")
        return item["doc"]

    # ---- reading position from Book Orbit ---------------------------------------------------------

    parsed_books: dict[tuple[str, float], epub.Book] = {}     # (epub path, mtime) → parsed book; polled often

    def parsed(p: Path) -> epub.Book:
        k = (str(p), p.stat().st_mtime)
        if k not in parsed_books:
            if len(parsed_books) >= 4:
                parsed_books.pop(next(iter(parsed_books)))
            parsed_books[k] = epub.read(p)
        return parsed_books[k]

    @app.get("/api/library/{key}/position")
    def library_position(key: str):
        item = library.scan().get(key)
        if item is None or item["doc"].get("kind") != "index":
            raise HTTPException(404, "no such index")
        owner = next((e for e in catalog.entries() if any(v["key"] == key for v in e["versions"])), None)
        if owner is None or owner["source"] != "bookorbit":
            return {"synced": False, "reason": "not-bookorbit"}
        if not secrets.get("bookorbit"):
            return {"synced": False, "reason": "not-connected"}
        p = next((q for ed in item["doc"].get("editions", []) if ed.get("builtFrom", True)
                  for q in [library.edition_path(ed.get("textHash", ""))] if q), None)
        if p is None:
            return {"synced": False, "reason": "no-edition"}
        try:
            items = bo_client().progress(owner["bookorbitId"])
        except bookorbit.BookOrbitError as e:
            return {"synced": False, "reason": "unreachable", "detail": str(e)}
        file_id = next((b.get("epubFileId") for b in catalog.bookorbit_books() if b["id"] == owner["bookorbitId"]), None)
        entry = next((i for i in items if file_id and str(i.get("fileId")) == str(file_id)), None) \
            or max((i for i in items if i.get("updatedAt")), key=lambda i: i["updatedAt"], default=None)
        if entry is None:
            return {"synced": False, "reason": "no-progress"}
        try:
            got = position.resolve(parsed(p), entry)
        except Exception as e:   # an unreadable EPUB must not break the Reader's poll
            return {"synced": False, "reason": "no-edition", "detail": str(e)}
        if got["seq"] is None:
            return {"synced": False, "reason": "no-progress"}
        return {"synced": True, "source": "bookorbit", "updatedAt": entry.get("updatedAt"),
                "percentage": entry.get("percentage"), **got}

    # ---- duplicate entities ----------------------------------------------------------------------

    @app.get("/api/library/{key}/duplicates")
    def duplicates(key: str):
        try:
            return identity.duplicates(key)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e

    @app.post("/api/library/{key}/duplicates/check")
    def check_duplicates(key: str):
        try:
            return identity.check(key)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except LookupError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/library/{key}/merge")
    async def merge_entities(key: str, request: Request):
        body = await request.json()
        pairs = [(str(x["keep"]), str(x["drop"]), str(x["name"]) if x.get("name") else None)
                 for x in body.get("pairs", []) if isinstance(x, dict) and x.get("keep") and x.get("drop")]
        if not pairs:
            raise HTTPException(400, "nothing to merge")
        try:
            return identity.merge(key, pairs)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/library/{key}/tidy-names")
    def tidy_names(key: str):
        try:
            return identity.tidy_names(key)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/library/{key}/merge/{merge_id}/undo")
    def undo_merge(key: str, merge_id: str):
        try:
            return identity.undo(key, merge_id)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except (LookupError, RuntimeError) as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/library/{key}/distinct")
    async def mark_distinct(key: str, request: Request):
        body = await request.json()
        a, b = str(body.get("a", "")), str(body.get("b", ""))
        if not a or not b or a == b:
            raise HTTPException(400, "two different entity ids are needed")
        try:
            identity.mark_distinct(key, a, b)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        return {"ok": True}

    @app.get("/api/editions/{text_hash}")
    def edition(text_hash: str):
        p = library.edition_path(text_hash)
        if p is None:
            raise HTTPException(404, "book file for this edition not found")
        meta = describe(p)
        meta["blockWords"] = [len(b.words) for b in epub.blocks(p)]
        return meta

    protocol.install(app, library, parsed, api_token_value=api_token)   # /v0 and /.well-known/lorekeeper, see protocol.py
    return app
