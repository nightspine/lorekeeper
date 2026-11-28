"""Local web UI: Studio (upload, configure, run and watch generation) and Reader (explore an index).

    uv run --extra web lorekeeper web            # http://127.0.0.1:8765

The server binds to localhost by default. Paths are never taken from requests: books are
addressed by content hash, indexes by keys from a server-side scan of known directories.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import art as artmod
from .. import ask as askmod
from .. import epub, gating, images, llm, text
from ..generate import slug
from . import art as artstore
from . import auth as authmod
from . import bookorbit, position, protocol
from . import read as readmod
from . import styles as stylesmod
from .catalog import Catalog, series_num, story_chapters
from .community import KEEP, Community, CommunityError
from .editor import EditError, Editor, WordingError
from .identity import Identity
from .jobs import (BookStore, JobManager, art_config, chapter_choice, describe, entity_run, is_story, run_limits,
                   text_choice_config)
from .secrets import Keys, SecretStore, mask

STATIC = Path(__file__).parent / "static"
# Words per second with the support check on, measured on a 16 GB M4; replaced by observed rates.
DEFAULT_THROUGHPUT = {"gemma4:12b-it-qat": 9.9, "qwen3.5:9b-q4_K_M": 13, "granite4.2:8b": 11,
                      "claude-haiku-5-5": 300}


class Library:
    """Index and series files found in a set of directories, plus EPUBs to map positions onto."""

    def __init__(self, index_dirs: list[tuple[str, Path]], epub_dirs: list[Path]) -> None:
        self.index_dirs, self.epub_dirs = index_dirs, epub_dirs
        self._described: dict[Path, tuple[tuple, dict]] = {}   # epub path → ((mtime_ns, size), edition meta)
        self._editions: dict[Path, tuple[float, str]] = {}   # epub path → (mtime, textHash)

    def _paths(self):
        """(key, label, path) of every candidate JSON file, in scan order; nothing is read."""
        for label, d in self.index_dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*.json")):
                if p.name.endswith((".state", ".report")) or ".events" in p.name:
                    continue
                yield f"{label}--{p.relative_to(d).with_suffix('').as_posix().replace('/', '--')}", label, p

    @staticmethod
    def _load(key: str, label: str, p: Path) -> dict | None:
        try:
            data = p.read_bytes()   # one read: the document and its digest describe the same bytes
            stamp = hashlib.blake2b(data, digest_size=16).hexdigest()
            doc = json.loads(data.decode())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(doc, dict) or doc.get("kind") not in ("index", "series"):
            return None
        return {"key": key, "path": p, "source": label, "doc": doc, "stamp": stamp}

    def scan(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for key, label, p in self._paths():
            if (item := self._load(key, label, p)) is not None:
                found[key] = item
        return found

    def get(self, key: str) -> dict | None:
        """One scanned item by key, reading only the files that carry that key (what `scan().get(key)`
        returns, without reading and hashing the rest)."""
        item = None
        for k, label, p in self._paths():
            if k == key and (found := self._load(k, label, p)) is not None:
                item = found
        return item

    def summary(self) -> dict:
        indexes, series = [], []
        found = self.scan()
        # Same membership rule as `Identity.group`: an index with a series is writable only when every
        # index of that series is generated here; one without a series, when it is generated itself.
        foreign = {(it["doc"].get("series") or {}).get("id") for it in found.values()
                   if it["doc"].get("kind") == "index" and it["source"] != "generated"}
        for item in found.values():
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
                "bookKind": doc.get("bookKind"),
                "writable": item["source"] == "generated" and ((doc.get("series") or {}).get("id") not in foreign
                                                               or not (doc.get("series") or {}).get("id")),
                "entities": len(doc.get("entities", [])), "anchors": len(doc.get("anchors", [])),
                "model": (prov.get("generator") or {}).get("model"), "updated": prov.get("updated") or prov.get("created"),
                "textHashes": [e.get("textHash") for e in doc.get("editions", [])],
                "hasEdition": any(self.edition_path(e.get("textHash", "")) for e in doc.get("editions", [])),
            })
        return {"indexes": indexes, "series": series}

    def describe_edition(self, p: Path) -> dict:
        """`describe(p)` plus `blockWords`, remembered per file (mtime, size): parsing a large EPUB takes
        half a second and the Reader asks every time it opens."""
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._described.get(p)
        if hit is None or hit[0] != stamp:
            book = epub.read(p)   # parsed once for both
            meta = describe(p, book)
            meta["blockWords"] = [len(b.words) for b in book.blocks]
            for gone in [q for q in self._described if q != p and not q.exists()]:
                del self._described[gone]
            hit = self._described[p] = (stamp, meta)
        return copy.deepcopy(hit[1])

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


def lorekeeper_version() -> str:
    """The installed package's version, else pyproject.toml's (a checkout run without installing), else "unknown"."""
    try:
        from importlib.metadata import version
        return version("lorekeeper")
    except Exception:
        pass
    try:
        import tomllib
        with open(Path(__file__).resolve().parents[3] / "pyproject.toml", "rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except Exception:
        return "unknown"


class Revalidated(StaticFiles):
    """Static files the browser must check before reuse (a 304 when unchanged). Without it a browser guesses a
    lifetime from Last-Modified and, after an upgrade, runs new scripts against old styles or the reverse."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(data_dir: Path, index_dirs: list[tuple[str, Path]], epub_dirs: list[Path],
               ollama_url: str = "http://localhost:11434",
               allowed_hosts: tuple[str, ...] | None = LOCAL_HOSTS,
               api_token: str | None = None, auth: bool = True, initial_password: str | None = None,
               log=authmod.say, auth_clock=time.monotonic) -> FastAPI:
    """allowed_hosts=None disables the Host check (only for deliberate remote access).

    auth=True puts an admin sign-in in front of /api (see web/auth.py); the first start creates the
    account and logs its initial password through `log`."""
    accounts = authmod.Auth(data_dir / "auth.json", auth_clock) if auth else None
    if accounts:
        accounts.ensure_admin(initial_password, log)
    secrets = SecretStore(data_dir / "secrets.json")
    keys = Keys(secrets)
    books = BookStore(data_dir / "books")
    jobs = JobManager(data_dir / "jobs", books, data_dir / "indexes", ollama_url,
                      keys=keys.get, provider_config=keys.config,
                      prior_for=lambda job: catalog.prior_indexes(job.config.get("catalog_key", "")),
                      distinct_for=lambda job: distinct_for(job))
    arts = artstore.ArtStore(data_dir)
    jobs.art_store = arts
    style_store = stylesmod.StyleStore(data_dir)     # saved picture styles; also gives art.style_sentence their words
    library = Library([("generated", data_dir / "indexes"), ("community", data_dir / "community" / "indexes"),
                       *index_dirs], [data_dir / "books", *epub_dirs])
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

    def pick_model(tier: str = "cheap") -> dict:
        """The model for small jobs over the whole cast (pairs, questions): the first configured cloud
        provider's `tier` model (Claude first), else local Ollama with the default model unless a build
        is using it. {"model": backend or None, "provider", "name", "local", "reason"}."""
        for p in jobs.configured():
            spec = llm.PROVIDERS[p]
            name = keys.config(p).get("model") if p == "custom" else spec[tier]
            if name:
                return {"model": jobs.cloud_model(p, name), "provider": p, "name": name, "local": False, "reason": None}
        none = {"model": None, "provider": None, "name": None, "local": False, "reason": "no-model"}
        try:
            with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=2) as r:
                models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        except (OSError, ValueError):
            return none
        if llm.DEFAULT_MODEL not in models:
            return none
        # A pictures run writes its prompts with a cloud model: it counts only when its text provider is Ollama.
        busy = any(j.status in ("running", "stopping")
                   and (j.config.get("provider") or ("" if j.kind == "art" else "ollama")) == "ollama"
                   for j in jobs.jobs.values())
        # one Ollama model at a time: don't compete with a running build
        return {"model": None if busy else llm.make("ollama", llm.DEFAULT_MODEL, url=ollama_url),
                "provider": "ollama", "name": llm.DEFAULT_MODEL, "local": True,
                "reason": "ollama-busy" if busy else None}

    def judge_model(tier: str = "cheap"):
        """For proposing pairs from the whole cast: see `pick_model`."""
        return pick_model(tier)["model"]

    def strong_judge_model():
        """The strong model of the same provider judges the pairs (a few hundred short pairs cost cents
        and the verdicts decide merges); otherwise the same model as `judge_model`."""
        return judge_model("strong")

    identity = Identity(data_dir, library, catalog, jobs, checker=judge_model, judge_checker=strong_judge_model)
    covers = data_dir / "bookorbit" / "covers"

    # ---- Book Orbit connection (session = rotating refresh token, kept with the other secrets)
    bo: dict = {"client": None}
    bo_live: dict[str, dict] = {}    # book id → last status Book Orbit itself told us; cleared on (dis)connect

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

    def write_manifest(series_id: str) -> None:
        manifest = catalog.series_manifest(series_id)
        if manifest:
            (data_dir / "indexes").mkdir(parents=True, exist_ok=True)
            (data_dir / "indexes" / f"{series_id}.series.json").write_text(json.dumps(manifest, indent=2))

    def submit_for(key: str, config: dict):
        """Queue a build for a catalog book, with series ids filled in from the catalog."""
        local = prepare(key)
        e = catalog.entry(key)
        config = {**config, "catalog_key": key}
        if e["series"] and not config.get("series_id"):
            config["series_id"] = e["series"]["id"]
            config["book_key"] = slug(e["title"])
            write_manifest(e["series"]["id"])
        story = story_chapters(books.get(local))
        if not config.get("chapters"):
            config["chapters"] = story or None
        config["whole_story"] = bool(story) and set(story) <= set(config.get("chapters") or [])
        return jobs.submit(local, config)
    app = FastAPI(title="Lorekeeper", docs_url="/api/docs", redoc_url=None)
    app.state.auth = accounts
    app.state.jobs = jobs
    app.mount("/static", Revalidated(directory=STATIC), name="static")
    app.include_router(stylesmod.router(style_store, jobs.image_model, lambda: jobs.describe_model()))   # /api/styles: the picker's styles and previews
    # The docs site (static/docs) links ../css, ../fonts and ../vendor so that it works unchanged when
    # `lorekeeper docs export` copies it to a static host. Serve those folders at the root to match.
    app.mount("/docs", Revalidated(directory=STATIC / "docs", html=True), name="docs")
    for folder in ("css", "fonts", "vendor"):
        app.mount(f"/{folder}", Revalidated(directory=STATIC / folder), name=f"docs-{folder}")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # DNS rebinding: a hostile site resolving its own name to 127.0.0.1 still sends its Host.
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        if allowed_hosts is not None and host not in allowed_hosts:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        # CSRF: other origins can't send a custom header without a CORS preflight, which we never allow.
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-lorekeeper") != "1":
            return JSONResponse({"detail": "missing X-Lorekeeper header"}, status_code=403)
        if accounts:
            path = request.url.path
            protected = (path.startswith("/api/") and not path.startswith("/api/auth/")) or path == "/openapi.json"
            user = accounts.session_for(request.cookies.get(authmod.COOKIE))
            request.state.user = user
            if protected:
                if not user:
                    return JSONResponse({"detail": "Sign in required"}, status_code=401)
                if accounts.user(user).get("mustChangePassword"):
                    return JSONResponse({"detail": "Choose a new password first",
                                         "code": "password-change-required"}, status_code=403)
        return await call_next(request)

    def secure_request(request: Request) -> bool:
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"

    def set_cookie(request: Request, response, token: str) -> None:
        response.set_cookie(authmod.COOKIE, token, max_age=30 * 24 * 3600, path="/", httponly=True,
                            samesite="lax", secure=secure_request(request))

    def need_auth() -> None:
        if not accounts:
            raise HTTPException(404, "Sign-in is off")

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        return body

    def throttled(key: tuple) -> JSONResponse | None:
        wait = accounts.throttle.wait(key)
        if wait:
            return JSONResponse({"detail": f"Too many attempts. Try again in {wait} seconds."},
                                status_code=429, headers={"Retry-After": str(wait)})
        return None

    @app.get("/api/auth/session")
    def auth_session(request: Request):
        if not accounts:
            return {"auth": False}
        user = getattr(request.state, "user", None)
        if not user:
            return {"auth": True, "user": None, "setup": accounts.setup()}
        return {"auth": True, "user": accounts.public_user(user)}

    @app.post("/api/auth/login")
    async def auth_login(request: Request):
        need_auth()
        body = await json_body(request)
        username, password = str(body.get("username") or ""), str(body.get("password") or "")
        key = (username.strip().lower(), request.client.host if request.client else "")
        if (blocked := throttled(key)):
            return blocked
        name = accounts.authenticate(username, password)
        if not name:
            accounts.throttle.fail(key)
            return JSONResponse({"detail": "Wrong username or password"}, status_code=401)
        accounts.throttle.clear(key)
        old = request.cookies.get(authmod.COOKIE)
        if old:
            accounts.delete_session(old)
        response = JSONResponse({"user": accounts.public_user(name)})
        set_cookie(request, response, accounts.create_session(name))
        return response

    @app.post("/api/auth/logout")
    def auth_logout(request: Request):
        need_auth()
        accounts.delete_session(request.cookies.get(authmod.COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(authmod.COOKIE, path="/", httponly=True, samesite="lax",
                               secure=secure_request(request))
        return response

    @app.post("/api/auth/password")
    async def auth_password(request: Request):
        need_auth()
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        body = await json_body(request)
        current, new = str(body.get("current") or ""), str(body.get("new") or "")
        key = (user, request.client.host if request.client else "")
        if (blocked := throttled(key)):
            return blocked
        if not accounts.authenticate(user, current):
            accounts.throttle.fail(key)
            return JSONResponse({"detail": "Wrong current password"}, status_code=401)
        accounts.throttle.clear(key)
        if (problem := authmod.check_password(new, current)):
            raise HTTPException(400, problem)
        accounts.set_password(user, new)
        old = request.cookies.get(authmod.COOKIE)
        accounts.delete_session(old)
        accounts.delete_other_sessions(user, None)
        response = JSONResponse({"user": accounts.public_user(user)})
        set_cookie(request, response, accounts.create_session(user))
        return response

    def job_json(job) -> dict:
        return {**asdict(job), "output_name": Path(job.output).name,
                **({"index": job.config.get("index_key")} if job.kind == "art" else {})}

    @app.get("/")
    def root():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

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

    @app.get("/api/about")
    def about():
        """The version of Lorekeeper this server runs (Settings shows it). Never fails: "unknown" when neither the installed
        package nor pyproject.toml says."""
        return {"version": lorekeeper_version()}

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

    def check_provider(pid: str, key: str | None, base_url: str | None = None) -> tuple[bool, str | None, list[dict]]:
        """(ok, error, live models as {id, text}). A free call: models.list for Anthropic, GET /models otherwise (Gemini's
        native list). `text` says whether the provider's list shows the model can write text (`llm.live_models`)."""
        spec = llm.PROVIDERS[pid]
        if spec["kind"] == "anthropic":
            ok, error = check_anthropic(key)
            return ok, error, []
        if pid != "custom" and not key:
            return False, f"No {spec['label']} API key yet.", []
        try:
            rows = llm.live_models(pid, key, base_url)
            return True, None, [r if isinstance(r, dict) else {"id": r, "text": True} for r in rows]
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
        rows = [r for r in (cached or {}).get("live", []) if r["id"] not in known]
        # liveModels: what can write text (every text model select, and the writer checks); otherModels: the rest the
        # provider lists (music, speech, images, embeddings), for a table that shows everything
        live = [{"id": r["id"], "label": r["id"], "price": None, "tier": None, "text": True} for r in rows if r["text"]]
        other = [{"id": r["id"], "label": r["id"], "price": None, "tier": None, "text": False} for r in rows if not r["text"]]
        img = llm.IMAGE_PROVIDERS.get(pid)
        # The image models the person may choose: names, prices per quality (a number, per shape, or absent = unknown)
        # and the quality levels each offers. None for a provider that cannot draw.
        images = {"default": img["default"], "cheap": img["cheap"], "strong": img["strong"],
                  "models": [{"id": m["id"], "label": m["label"], "tier": m["tier"], "price": m["price"],
                              "levels": list(m.get("levels") or img["quality"])} for m in img["models"]]} if img else None
        return {"id": pid, "label": spec["label"], "kind": spec["kind"], "key_url": spec["key_url"], "key_env": spec["key_env"],
                "configured": configured, "source": keys.source(pid), "masked": keys.masked(pid),
                "ok": bool(cached and cached["ok"]), "error": cached["error"] if cached else None,
                "base_url": cfg.get("base_url") or spec["base_url"], "model": cfg.get("model"),
                "models": presets, "liveModels": live, "otherModels": other,
                "default": spec["default"] or cfg.get("model"), "cheap": spec["cheap"] or cfg.get("model"),
                "strong": spec["strong"] or cfg.get("model"), "images": images}

    def provider_id(pid: str) -> str:
        pid = "claude" if pid == "anthropic" else pid
        if pid not in llm.PROVIDERS:
            raise HTTPException(404, f"unknown provider {pid!r}")
        return pid

    def text_models(pid: str) -> set[str]:
        """The text model ids a provider lists right now (presets and live, as Settings shows them)."""
        st = provider_status(pid)
        return {m["id"] for m in st["models"] + st["liveModels"]} | ({st["model"]} if st.get("model") else set())
    jobs.text_models = text_models

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

    def check_config(config) -> None:
        """Job config is accepted on two routes; both check it here before anything is queued."""
        if not isinstance(config, dict):
            raise HTTPException(400, "config must be an object")
        if config.get("provider", "ollama") not in ("ollama", *llm.PROVIDERS):
            raise HTTPException(400, f"unknown provider {config.get('provider')!r}")
        cm = config.get("chunk_mode")
        if cm is not None and cm not in ("words", "chapter"):
            raise HTTPException(422, "chunk_mode must be \"words\" or \"chapter\".")
        bk = config.get("book_kind")
        if bk is not None and (not isinstance(bk, str) or not bk.strip()):
            raise HTTPException(422, "book_kind must be \"auto\" or the name of a kind, like \"fiction\".")
        if bk is not None:
            config["book_kind"] = bk.strip()
        if config.get("art"):
            try:
                config["art"] = art_config(config["art"])
                jobs.check_art_models(config["art"])
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

    @app.post("/api/jobs")
    async def create_job(request: Request):
        body = await request.json()
        config = body.get("config", {})
        check_config(config)
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
        config = body.get("config", {})
        check_config(config)
        out = []
        for key in body.get("keys", []):
            out.append(job_json(submit_for(key, dict(config))))
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
            bo_live.clear()
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
        bo_live.clear()
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
        item = library.get(key)
        if item is None:
            raise HTTPException(404, "no such index")
        return item["doc"]

    # ---- Ask: questions answered from the gated view's notes (ask.py) -----------------------------

    ask_slots = {"n": 0}
    ask_lock = threading.Lock()
    MAX_ASKS = 2
    ASK_TIMEOUT = 120   # seconds a backend may wait on one call (Ollama and OpenAI-compatible have `timeout`)

    def ask_error(status: int, code: str, detail: str) -> JSONResponse:
        return JSONResponse({"detail": detail, "code": code}, status_code=status)

    def ask_info(c: dict) -> dict:
        spec = llm.PROVIDERS.get(c["provider"]) if c["provider"] != "ollama" else None
        known = next((m for m in spec["models"] if m["id"] == c["name"]), None) if spec else None
        return {"provider": c["provider"], "label": spec["label"] if spec else ("Ollama" if c["provider"] else None),
                "model": c["name"], "modelLabel": known["label"] if known else c["name"], "local": c["local"]}

    @app.get("/api/ask")
    def ask_status():
        c = pick_model("cheap")
        return {"available": c["model"] is not None, **ask_info(c), "reason": c["reason"]}

    @app.post("/api/library/{key}/ask")
    async def ask_book(key: str, request: Request):
        body = await json_body(request)
        question = body.get("question")
        question = " ".join(question.split()) if isinstance(question, str) else ""
        if not question or len(question) > 500:
            return ask_error(422, "bad-question", "Ask a question of 1 to 500 characters.")
        turns = []
        for t in (body.get("history") if isinstance(body.get("history"), list) else [])[-6:]:
            if not isinstance(t, dict):
                continue
            cites = t.get("cites") if isinstance(t.get("cites"), list) else []
            turns.append(askmod.Turn(str(t.get("question") or "")[:500], str(t.get("answer") or "")[:1500],
                                     [c for c in cites if isinstance(c, str)][:40]))
        anchor, order, finished, book = body.get("anchor"), body.get("order"), body.get("finished"), body.get("book")
        if (anchor is not None and not isinstance(anchor, str)) or (order is not None and not isinstance(order, str)) \
                or (book is not None and not isinstance(book, str)) \
                or (finished is not None and not (isinstance(finished, list) and all(isinstance(k, str) for k in finished))):
            return ask_error(422, "bad-position", "The position is not understood.")

        def work():
            """Everything that reads files, computes the view or waits on a model, off the event loop.
            The slot is taken and freed here, so only a job that is running holds one: a client that
            leaves while this is still queued cancels it before it takes a slot, and one that leaves
            while it runs does not free the slot until the model call really ends."""
            with ask_lock:
                if ask_slots["n"] >= MAX_ASKS:
                    return ask_error(429, "busy", "Two questions are already being answered. Try again in a moment.")
                ask_slots["n"] += 1
            try:
                item = (scan := library.scan()).get(key)
                if item is None or item["doc"].get("kind") != "index":
                    return ask_error(404, "not-found", "no such index")
                sref = item["doc"].get("series")
                manifest, siblings = proto.series_parts(item, scan)
                series = manifest["doc"] if manifest else None
                indexes = [item["doc"]] + [c["doc"] for c in siblings]
                if book is not None and sref and sref["book"] != book:
                    return ask_error(422, "bad-position", "That index is not the book being read.")
                if series and order is not None and order not in series["orders"]:
                    return ask_error(422, "bad-position", "That reading order does not exist.")
                try:
                    view = gating.view(indexes, gating.Position(sref["book"] if series else None, anchor),
                                       series, order, None if finished is None else set(finished))
                except ValueError as e:
                    return ask_error(422, "bad-position", str(e))
                titles = None
                if series:
                    names = {b["key"]: b.get("title") for b in series["books"]}
                    titles = {k: {"title": names.get(k) or k, "number": n}
                              for n, k in enumerate(series["orders"][order or series["defaultOrder"]], 1)}
                c = pick_model("cheap")
                if c["model"] is None:
                    return ask_error(409, c["reason"] or "no-model", "The local model is busy building an index."
                                     if c["reason"] == "ollama-busy" else "No model is set up to answer. Add a key in Settings.")
                if hasattr(c["model"], "timeout"):
                    c["model"].timeout = ASK_TIMEOUT   # a question must not hold a slot for half an hour
                budget = askmod.OLLAMA_BUDGET if c["local"] else askmod.DEFAULT_BUDGET
                try:
                    got = askmod.answer(view, question, c["model"], turns, budget, titles,
                                        askmod.ROSTER_CHARS if not c["local"] else 0)
                except llm.ProviderError as e:
                    return ask_error(502, "model-failed", str(e)[:200])
                except Exception as e:  # noqa: BLE001 - a provider's failure must not leak its body or key
                    return ask_error(502, "model-failed", f"The model could not answer ({type(e).__name__}).")
                info = ask_info(c)
                return {"answer": got.answer, "found": got.found, "cites": got.cites, "note": got.note,
                        "provider": info["provider"], "model": info["model"], "local": info["local"],
                        "usage": got.usage}
            finally:
                with ask_lock:
                    ask_slots["n"] -= 1

        return await asyncio.get_running_loop().run_in_executor(None, work)

    # ---- reading position from Book Orbit ---------------------------------------------------------

    parsed_books: dict[tuple[str, float], epub.Book] = {}     # (epub path, mtime) → parsed book; polled often

    def parsed(p: Path) -> epub.Book:
        k = (str(p), p.stat().st_mtime)
        if k not in parsed_books:
            if len(parsed_books) >= 4:
                parsed_books.pop(next(iter(parsed_books)))
            parsed_books[k] = epub.read(p)
        return parsed_books[k]

    editor = Editor(identity, library, data_dir, reader=parsed)
    def after_run(job):
        got = editor.reapply(Path(job.output))   # a rebuild keeps the person's edits
        if job.config.get("series_id"):
            try:
                write_manifest(job.config["series_id"])   # the new index may settle the series' bookKind
            except Exception as ex:  # noqa: BLE001 - never turns into "your edits could not be applied"
                line = f"series manifest: not refreshed ({type(ex).__name__}: {ex})"
                try:
                    jobs._emit(job, {"type": "log", "line": line})
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).warning(line)
        return got
    jobs.after_run = after_run

    def who(request: Request) -> str:
        return getattr(request.state, "user", None) or "admin"

    @app.get("/api/library/{key}/position")
    def library_position(key: str):
        """Where the person is in a Book Orbit book. With `finished: true` the whole book counts as read:
        `p` is 1 and `seq` (the last block) is informational."""
        item = library.get(key)
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
        cached = next((b for b in catalog.bookorbit_books() if b["id"] == owner["bookorbitId"]), None) or {}
        bid = owner["bookorbitId"]
        # the last live answer beats the sync cache, so a failing call can't bounce the Reader
        last = bo_live.get(bid) or {"status": cached.get("readStatus"), "finishedAt": cached.get("finishedAt")}
        status, finished_at = last["status"], last["finishedAt"]
        items, error, down = [], None, False
        try:
            live = bo_client().read_status(owner["bookorbitId"])
            if live["known"]:                    # null means no status; only a missing field keeps the fallback
                status, finished_at = live["status"], live["finishedAt"]
                bo_live[bid] = {"status": status, "finishedAt": finished_at}
        except bookorbit.BookOrbitUnreachable as e:
            down, error = True, e                # no second wait for the progress call
        except bookorbit.BookOrbitError:
            pass                                 # fall back to the cached status
        done = bookorbit.is_finished(status) and bool(finished_at)   # progress could add nothing
        if not down and not done:
            try:
                items = bo_client().progress(owner["bookorbitId"])
            except bookorbit.BookOrbitError as e:
                error = e
        newest = max((i["updatedAt"] for i in items if i.get("updatedAt")), default=None)
        if bookorbit.is_finished(status):
            try:
                blocks = parsed(p).blocks
            except Exception as e:
                return {"synced": False, "reason": "no-edition", "detail": str(e)}
            if not blocks:
                return {"synced": False, "reason": "no-edition"}
            return {"synced": True, "source": "bookorbit", "finished": True, "percentage": 100, "p": 1.0,
                    "seq": blocks[-1].seq, "basis": "finished", "updatedAt": finished_at or newest}
        if error is not None:
            return {"synced": False, "reason": "unreachable", "detail": str(error)}
        file_id = cached.get("epubFileId")
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
        return {"synced": True, "source": "bookorbit", "finished": False, "updatedAt": entry.get("updatedAt"),
                "percentage": entry.get("percentage"), **got}

    # ---- mentions of an entry, per chapter, gated at the position ----------------------------------

    mention_views: dict[tuple, tuple[float, dict]] = {}   # (key, anchor, order, fin, mtime) → (time, the gated view); a minute
    story_flags: dict[tuple, list[bool]] = {}              # (epub path, mtime) → story flag per section, for books with no stored meta

    def sections_story(p: Path, book) -> list[bool]:
        """Which of the book's sections are story: the stored book meta's flags (`jobs.describe`, what the build form
        shows), else worked out once for an EPUB the store does not hold (Book Orbit's)."""
        if p.parent == books.root:
            try:
                meta = books.get(p.stem)
            except (OSError, ValueError, KeyError):
                meta = None
            if meta and len(meta.get("chapters", [])) == len(book.chapters):
                return [bool(c.get("story")) for c in meta["chapters"]]
        k = (str(p), p.stat().st_mtime)
        if k not in story_flags:
            from .jobs import mark_story
            ends = [c.start for c in book.chapters[1:]] + [len(book.blocks)]
            secs = [{"title": c.title or f"Section {i + 1}",
                     "words": sum(len(b.words) for b in book.blocks[c.start:end])}
                    for i, (c, end) in enumerate(zip(book.chapters, ends))]
            mark_story(secs)
            story_flags[k] = [c["story"] for c in secs]
        return story_flags[k]

    def mentions_body(book, flags: list[bool], m, eid: str) -> dict:
        """Chapters are the story chapters, numbered 1..N in reading order. A section between two story chapters that
        is not story (a part title, an interlude) is counted in the column of the story chapter before it."""
        from .. import mentions
        story = [i for i, f in enumerate(flags) if f]                 # section index of story chapter n = story[n - 1]
        body = {"total": len(story), "chapters": [], "current": None,
                "first": None, "busiest": None, "latest": None}
        cur = m.current
        if cur is None or not story or cur < story[0]:                # front matter: nothing to chart yet
            return body
        counts = m.counts.get(eid) or [0] * (cur + 1)
        chapters = []
        for n, i in enumerate(story):
            if i > cur:
                break
            # up to the next story chapter; after the last one, only its own section (back matter is not folded in)
            end = story[n + 1] if n + 1 < len(story) else i + 1
            chapters.append({"n": n + 1, "title": book.chapters[i].title or f"Section {i + 1}",
                             "count": sum(counts[i:min(end, cur + 1)])})
        body["chapters"] = chapters
        seen = [c for c in chapters if c["count"]]
        if not seen:
            return body
        busiest = max(seen, key=lambda c: (c["count"], -c["n"]))
        body["first"] = {"n": seen[0]["n"], "title": seen[0]["title"]}
        body["busiest"] = {"n": busiest["n"], "title": busiest["title"], "count": busiest["count"]}
        body["latest"] = {"n": seen[-1]["n"], "title": seen[-1]["title"]}
        if flags[cur]:                                                # an interlude or back matter has no current chapter
            first = book.chapters[cur].start
            u16 = lambda t, i: len(t[:i].encode("utf-16-le")) // 2   # noqa: E731
            sn = mentions.snippets(book.blocks, m.spans.get(eid, []))
            body["current"] = {"n": story.index(cur) + 1, "count": counts[cur],
                               "snippets": [{**x, "para": x["seq"] - first + 1,
                                             "start": u16(x["text"], x["start"]), "end": u16(x["text"], x["end"])}
                                            for x in sn]}
        return body

    @app.get("/api/library/{key}/mentions")
    def library_mentions(key: str, entity: str, seq: int, order: str | None = None, fin: str | None = None):
        """Where an entry comes up, chapter by chapter, counted from the book's text and only up to block `seq`
        (the last finished block, counted: names are gated inclusively there).
        Names are the ones the Reader's view shows at that position, gated by the same helpers as `/v0` and the
        Reader (`protocol.locate_anchor`, `gated_view`) under its reading order `order` and the comma-separated
        finished books `fin` (absent: the Reader's defaults), and chapters after it are not even titled.
        Snippets carry `para` (1-based paragraph within the chapter) and `start`/`end` as UTF-16 code-unit
        offsets into `text`, since the consumer is JavaScript."""
        from .. import mentions
        scan = library.scan()
        item = scan.get(key)
        if item is None or item["doc"].get("kind") != "index":
            raise HTTPException(404, "no such index")
        ed = next((ed for ed in item["doc"].get("editions", []) if ed.get("builtFrom", True)
                   and library.edition_path(ed.get("textHash", ""))), None)
        if ed is None:
            raise HTTPException(404, "book file for this index not found")
        p = library.edition_path(ed["textHash"])
        book = parsed(p)
        if not 0 <= seq < len(book.blocks):
            raise HTTPException(422, "seq is outside the book")
        pos = protocol.make_position(block=str(seq), textHash=ed["textHash"], inclusive="true",
                                     order=order, finished=fin)
        try:
            anchor, _ = proto.locate_anchor(item, pos, True)
            manifest, siblings = proto.series_parts(item, scan)   # every document the view reads is in the key
            deps = tuple((str(i["path"]), i["path"].stat().st_mtime) for i in [item, *([manifest] if manifest else []), *siblings])
            vk = (key, anchor, order, fin, deps)
            hit = mention_views.get(vk)
            if hit is None or time.time() - hit[0] > 60:
                if len(mention_views) > 64:
                    mention_views.clear()
                mention_views[vk] = hit = (time.time(), proto.gated_view(item, anchor, pos, scan, lenient=True))
        except protocol.Problem as e:
            raise HTTPException(e.status, e.detail or e.title) from e
        names = mentions.visible_names(hit[1])
        if entity not in names:
            raise HTTPException(404, "no such entry at this position")
        return mentions_body(book, sections_story(p, book), mentions.count(book.blocks, book.chapters, names, seq), entity)

    # ---- reading view: the book's own text by chapter (web/read.py) ---------------------------------------------

    def read_book(key: str):
        """The EPUB an index was built from, parsed, with its path; a JSONResponse refusal otherwise."""
        item = library.get(key)
        if item is None or item["doc"].get("kind") != "index":
            return None, None, JSONResponse({"detail": "no such index", "code": "not-found"}, status_code=404)
        p = next((q for ed in item["doc"].get("editions", []) if ed.get("builtFrom", True)
                  for q in [library.edition_path(ed.get("textHash", ""))] if q), None)
        if p is None:
            return None, None, JSONResponse({"detail": "The book file for this index is not here.",
                                             "code": "no-book-file"}, status_code=404)
        return p, parsed(p), None

    @app.get("/api/library/{key}/read/toc")
    def read_toc(key: str):
        """Every chapter of the index's own edition, numbered as the Reader's chapter map (`n`), with the blocks it
        spans and its words. `story` is the build form's front and back matter rule (absent for a book read with no
        table of contents)."""
        p, book, refusal = read_book(key)
        if refusal:
            return refusal
        spans = readmod.chapter_spans(book)
        if book.chapters:
            for c, flag in zip(spans, sections_story(p, book)):
                c["story"] = flag
        return {"chapters": spans, "blocks": len(book.blocks), "words": sum(c["words"] for c in spans)}

    @app.get("/api/library/{key}/read")
    def read_chapter(key: str, chapter: int | None = None, seq: int | None = None):
        """One chapter's blocks in order, short ones included: by `chapter` (n of the toc) or by the `seq` of a block
        in it. Book text only, nothing from the index."""
        p, book, refusal = read_book(key)
        if refusal:
            return refusal
        spans = readmod.chapter_spans(book)
        if chapter is not None:
            c = spans[chapter - 1] if 1 <= chapter <= len(spans) else None
        elif seq is not None:
            if not 0 <= seq < len(book.blocks):
                return JSONResponse({"detail": "seq is outside the book", "code": "bad-seq"}, status_code=422)
            c = readmod.chapter_of(spans, seq)
        else:
            return JSONResponse({"detail": "chapter or seq is needed", "code": "bad-request"}, status_code=422)
        if c is None:
            return JSONResponse({"detail": "no such chapter", "code": "no-chapter"}, status_code=404)
        return readmod.chapter_body(book, spans, c)

    # ---- pictures: art sets in the art store, never in the index (art.py, web/art.py, CLAUDE.md §8f) ----------------

    def art_error(status: int, code: str, detail: str, why: str = "", extra: dict | None = None) -> JSONResponse:
        return JSONResponse({**(extra or {}), "detail": detail, "code": code, **({"why": why} if why else {})},
                            status_code=status)

    class ArtRefusal(Exception):
        def __init__(self, status: int, code: str, detail: str, why: str = "", extra: dict | None = None) -> None:
            super().__init__(detail)
            self.status, self.code, self.detail, self.why, self.extra = status, code, detail, why, extra

    QUALITIES = ("draft", "standard", "best")
    PORTRAIT_KINDS = ("portrait", "place", "thing")     # drawn with the portrait image model when one is chosen

    def art_index(key: str) -> dict:
        """The one index a pictures route works on (no library scan: only the series prior needs one)."""
        item = library.get(key)
        if item is None or item["doc"].get("kind") != "index":
            raise ArtRefusal(404, "not-found", "no such index")
        h = artstore.text_hash(item["doc"])
        if not artstore.HASH.fullmatch(h):
            raise ArtRefusal(409, "no-edition", "This index names no edition, so it can't have pictures.")
        return {"item": item, "doc": item["doc"], "hash": h}

    def art_epub(ctx: dict) -> epub.Book:
        p = library.edition_path(ctx["hash"])
        if p is None:
            raise ArtRefusal(404, "no-edition", "The book file for this index is not here, so pictures can't be made for it.")
        return parsed(p)

    def art_prior(ctx: dict) -> list[dict]:
        """The earlier books of the series, in its default reading order, whose indexes are in the library."""
        sref = ctx["doc"].get("series")
        if not sref:
            return []
        manifest, siblings = proto.series_parts(ctx["item"], library.scan())
        if not manifest:
            return []
        order = manifest["doc"]["orders"].get(manifest["doc"].get("defaultOrder"), [])
        earlier = order[:order.index(sref["book"])] if sref["book"] in order else []
        by = {c["doc"]["series"]["book"]: c["doc"] for c in siblings}
        return [by[k] for k in earlier if k in by]

    def art_inputs(job):
        ctx = art_index(job.config["index_key"])
        return ctx["doc"], art_epub(ctx), art_prior(ctx)

    def art_key_for(job) -> str | None:
        """The library key of the index a job draws for: the pictures run's own, else the build's output."""
        if job.kind == "art":
            return job.config.get("index_key")
        try:
            rel = Path(job.output).relative_to(data_dir / "indexes").with_suffix("").as_posix()
        except ValueError:
            return None
        return f"generated--{rel.replace('/', '--')}"

    def art_journal(job, h: str, slug_: str, added: list[str], also: list[dict] | None = None) -> str | None:
        """A run's pictures as one journal entry: its undo takes out exactly those (`also`: those it drew into earlier
        books' sets, each {textHash, style, added})."""
        key = art_key_for(job)
        if not key or library.get(key) is None:
            return None
        n = len(added) + sum(len(a["added"]) for a in also or [])
        return journal_art(key, [], {"op": "add", "textHash": h, "style": slug_, "added": added,
                                     **({"also": also} if also else {})},
                           f"drew {n} picture{'s' if n != 1 else ''}")["id"]

    def prior_epub(h: str) -> epub.Book | None:
        """An earlier series book's EPUB by its edition hash, when it is here (an entry's portraits are drawn into it)."""
        p = library.edition_path(h) if artstore.HASH.fullmatch(h or "") else None
        return parsed(p) if p is not None else None
    jobs.art_inputs, jobs.art_journal, jobs.art_epub_for = art_inputs, art_journal, prior_epub

    def journal_art(key: str, keep: list[Path], art: dict, what: str) -> dict:
        return identity.snapshot(key, keep, {"kind": "art", "what": what, "art": art})

    def art_revert(entry: dict, snap: Path) -> None:
        """Undo one pictures entry, touching only the records it names: what it added is taken out, what it removed is put
        back with its file from the snapshot, a world note goes back to what it was. Pictures drawn since stay."""
        a = entry["art"]
        h = a["textHash"]
        if a.get("op") == "remove-sets":
            for slug_ in a["sets"]:
                src = snap / slug_
                with arts.lock(h, slug_):
                    if arts.manifest(h, slug_) is None:
                        dst = arts.folder(h, slug_)
                        dst.mkdir(parents=True, exist_ok=True)
                        for f in src.iterdir():
                            if not (dst / f.name).exists():
                                shutil.copy2(f, dst / f.name)
                    else:
                        arts.put_back(h, slug_, json.loads((src / "art.json").read_text()).get("pictures", []), src)
            return
        if a.get("op") == "world" and "sets" in a:
            for sl, was in a["sets"].items():
                with arts.lock(h, sl):
                    m = arts.manifest(h, sl)
                    if m is not None:
                        m["world"] = was
                        arts.save(h, sl, m)
            if "background" in a:
                with arts.lock(h, "background"):
                    arts.set_background(h, a["background"])
            return
        for more in a.get("also", []):          # a run's pictures in earlier books' sets
            with arts.lock(more["textHash"], more["style"]):
                arts.take_out(more["textHash"], more["style"], more["added"])
        slug_ = a["style"]
        with arts.lock(h, slug_):
            if a.get("added"):
                arts.take_out(h, slug_, a["added"])
            if a.get("removed"):
                arts.put_back(h, slug_, a["removed"], snap)
            if a.get("op") == "world" and "sets" not in a:     # an entry from before notes were per edition
                m = arts.manifest(h, slug_)
                if m is not None:
                    m["world"] = a.get("before")
                    arts.save(h, slug_, m)

    def art_finished(ctx: dict, body: dict) -> list[str] | None:
        """The reader's finished books (`finished`: series book keys, each an earlier book of this one); None = the Reader's
        default, every earlier book."""
        fin = body.get("finished")
        if fin is None:
            return None
        if not isinstance(fin, list) or not all(isinstance(k, str) for k in fin):
            raise ArtRefusal(422, "bad-position", "finished must be a list of book keys.")
        earlier = [(d.get("series") or {}).get("book") for d in art_prior(ctx)]
        if any(k not in earlier for k in fin):
            raise ArtRefusal(422, "bad-position", "Only earlier books of this series can be finished.")
        return sorted(set(fin))

    def art_writer(body: dict) -> dict | None:
        """The request's `writer` ({provider, model}, the sheet's "Prompt written by"), checked against what is set up and
        listed here; None means the default, the strong tier."""
        try:
            choice = text_choice_config(body.get("writer"), "prompt")
            jobs.check_text_choice(choice, "prompt")
        except ValueError as e:
            raise ArtRefusal(422, "bad-writer", str(e)) from e
        return choice

    def art_picker(body: dict):
        """The text backend that looks at a picture for drawing faults: the request's `picker` ({provider, model}, checked
        as a writer is), else the first configured cloud model that can see pictures (`JobManager.vision_model`). None
        when none can."""
        try:
            choice = text_choice_config(body.get("picker"), "choosing calls")
            jobs.check_text_choice(choice, "choosing calls")
        except ValueError as e:
            raise ArtRefusal(422, "bad-picker", str(e)) from e
        return jobs.vision_model(choice)

    def art_checker(body: dict):
        """The text backend that judges a picture's drawing faults and whether a fix put them right (the lightbox's check
        and fix): the request's `picker` when it sends one (`art_picker`, kept for API callers), else the strong tier of
        the first configured provider whose strong model can see pictures, falling back to the cheap tier
        (`JobManager.vision_model(tier="strong")`). None when no configured model can see pictures."""
        if body.get("picker") is not None:
            return art_picker(body)
        return jobs.vision_model(None, tier="strong")

    def art_image_choice(body: dict, kind) -> tuple[str | None, str | None]:
        """(provider, model) of the image model a sheet request draws with: for a portrait, place or thing the request's
        `portraitProvider` / `portraitModel` when it sends either (a model alone is one of `provider`'s), else `provider` /
        `model` as for every picture."""
        prov, mod = body.get("provider"), body.get("model")
        if kind in PORTRAIT_KINDS and (body.get("portraitProvider") is not None or body.get("portraitModel") is not None):
            pp = body.get("portraitProvider")
            return pp if pp is not None else prov, body.get("portraitModel")
        return prov, mod

    def illustrator(ctx: dict, style: str, quality: str = "standard", provider=None, model=None, text_needed=True,
                    portraits: str | bool | None = "main", tier: str = "cheap", writer: dict | None = None,
                    cfg: dict | None = None, picker=None, check: bool = False, checker=None):
        """(Illustrator, image model or None, slug) for one book and style. The set is found by its slug and the
        pipeline gets the set's stored style text, so a style written another way ("Ink") never starts the set over.
        `writer` (checked, `art_writer`) writes every kind's prompt; else the `tier` model does. `cfg`, an entity run's
        checked config (`entity_choice`): the earlier books it draws are given with their EPUB, and the pipeline gets its
        entries, position and finished books (`jobs.run_limits`). `picker`, a text backend, makes the choosing calls (the
        drawing-fault check among them) instead of the cheap tier; `check` has every picture drawn checked and fixed once, by
        `checker` when given (a model that sees pictures; every other choosing call stays with the picker)."""
        if provider is not None and provider not in llm.IMAGE_PROVIDERS:
            raise ArtRefusal(422, "bad-provider", f"No image provider {provider!r}.")
        if model is not None and (not isinstance(model, str) or (provider and not llm.image_model(provider, model))):
            raise ArtRefusal(422, "bad-model", f"Unknown image model {model!r}.")
        if quality not in QUALITIES:
            raise ArtRefusal(422, "bad-config", "quality must be draft, standard or best.")
        # One picture asked for in the sheet is written by the chosen writer, else the strong tier (`tier`); runs keep
        # their own choices (`JobManager._art`).
        text_model = jobs.writer_model(writer, tier) if text_needed else None
        if text_model is None and text_needed:
            raise ArtRefusal(409, "no-text-model", "Pictures need a cloud model to write their prompts. Add a key in Settings.")
        try:
            image = jobs.image_model(provider, model)
        except ValueError as e:
            raise ArtRefusal(422, "bad-model", str(e)) from e
        slug_, style = arts.resolve(ctx["hash"], style)
        books_ = []
        for d in art_prior(ctx):   # every earlier book; what a reader has not finished is kept out by `finished` on the job
            ph = artstore.text_hash(d)
            if artstore.HASH.fullmatch(ph):
                pb = prior_epub(ph) if cfg is not None and entity_run(cfg, d) else None
                books_.append(arts.book(d, pb, ph, arts.resolve(ph, style)[1], None, draw=pb is not None))
        books_.append(arts.book(ctx["doc"], art_epub(ctx), ctx["hash"], style, None))
        extra = run_limits(cfg, ctx["doc"], len(books_) - 1) if cfg is not None else {}
        if "portraits" in extra:
            portraits = extra.pop("portraits")
        # The sheet's writer writes every kind, and the sheet already chose the image model for the kind it draws.
        return artmod.Illustrator(books_, text_model, image, style, portraits=portraits, quality=quality,
                                  log=lambda m: None, pause=0,
                                  cheap_text=picker or (jobs.cheap_model(None) if text_needed else None),
                                  portrait_text=text_model, portrait_image=image, check=check, checker=checker,
                                  **extra), image, slug_

    def entity_choice(ctx: dict, cfg: dict) -> tuple[dict, list[str]]:
        """Check a checked art config's entries, position and finished books against the index, as the reader stands:
        `until` is resolved to an anchor of this book (`art_position`, finished mode), every entry must be visible there
        to a reader who finished `finished` and be a person or creature. Returns the config with `until` as
        {"p": anchor id, "book": series book key} and `finished` checked, and the editions of the earlier books the run
        draws into (finished ones with their EPUB here). An unknown entry and one past the position get the same answer."""
        if not (cfg.get("entities") or cfg.get("until")):
            return cfg, []
        fin = art_finished(ctx, {"finished": cfg.get("finished")})
        sbook = (ctx["doc"].get("series") or {}).get("book")
        until = cfg.get("until")
        if until:
            if until.get("book") is not None and until["book"] != sbook:
                raise ArtRefusal(422, "bad-position", "The position is in another book.")
            _, at = art_position(ctx, {"p": until["p"]})
            if at is None:
                raise ArtRefusal(422, "not-visible", "You haven't reached that yet.")
        else:
            at = ctx["doc"]["anchors"][-1]["id"]
        cfg = {**cfg, "finished": fin, "until": {"p": at, "book": sbook} if until else None}
        il = illustrator(ctx, cfg["style"], cfg["quality"], cfg["provider"], cfg["model"], text_needed=False)[0]
        v = il.view(len(il.books) - 1, at, None if fin is None else frozenset(fin))
        types = {e["id"]: e["type"] for e in v["entities"]}
        for eid in cfg.get("entities") or []:
            if eid not in types:
                raise ArtRefusal(422, "not-visible", "You haven't reached that yet.")
            if types[eid] not in artmod.PORTRAIT_TYPES:
                raise ArtRefusal(422, "bad-entity", "Only a person or a creature has portraits through the book.")
        also = [artstore.text_hash(d) for d in art_prior(ctx) if entity_run(cfg, d)]
        return cfg, [ph for ph in also if artstore.HASH.fullmatch(ph) and library.edition_path(ph) is not None]

    def art_style(raw) -> str:
        style = " ".join(str(raw or "").split())
        if not style or len(style) > 200:
            raise ArtRefusal(422, "bad-style", "Choose a style, or write one of at most 200 characters.")
        return style

    def query_position(ctx: dict, raw: str) -> str | float:
        """A position from a query string: an anchor id of the index when it is one (an id may look like a number, "1"),
        else the fraction of the book read when it reads as one, else the string as given (`art_position` refuses it)."""
        if any(a.get("id") == raw for a in ctx["doc"].get("anchors", [])):
            return raw
        return float(raw) if re.fullmatch(r"(0|1)(\.[0-9]{1,12})?", raw) else raw

    def art_position(ctx: dict, body: dict) -> tuple[bool, str | None]:
        """(given, anchor) of the reader's position `p`: an anchor id, or the fraction of the book's words read (as the
        Reader's address), gated in "finished" mode like `/v0` with a `block` locator."""
        p = body.get("p", body.get("anchor"))
        if p is None:
            return False, None
        if isinstance(p, str):
            if p not in {a["id"] for a in ctx["doc"]["anchors"]}:
                raise ArtRefusal(422, "bad-position", "The position is not understood.")
            return True, p
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 1:
            raise ArtRefusal(422, "bad-position", "The position is not understood.")
        book = art_epub(ctx)
        total = sum(len(b.words) for b in book.blocks)
        seq, done = len(book.blocks) - 1, 0
        for b in book.blocks:
            if done + len(b.words) > p * total:
                seq = b.seq
                break
            done += len(b.words)
        # The block being read does not count, unless the whole book is read.
        pos = protocol.make_position(block=str(seq), textHash=ctx["hash"], inclusive="true" if p >= 1 else "false")
        try:
            anchor, _ = proto.locate_anchor(ctx["item"], pos, False)
        except protocol.Problem as e:
            raise ArtRefusal(422, "bad-position", e.detail or e.title) from e
        return True, anchor

    def art_job(il, kind: str, target, given: bool, at: str | None, shape=None, finished: list[str] | None = None,
                looks: str = "make"):
        if kind not in artmod.SHAPE_OF:
            raise ArtRefusal(422, "bad-kind", "kind must be plate, event, portrait, place or thing.")
        if not isinstance(target, str) or not target:
            raise ArtRefusal(422, "bad-target", "Say what the picture is of.")
        if shape is not None and shape not in ("square", "portrait", "landscape"):
            raise ArtRefusal(422, "bad-shape", "shape must be square, portrait or landscape.")
        if given and at is None:
            raise ArtRefusal(422, "not-visible", "You haven't reached that yet.")
        try:
            fin_ = None if finished is None else set(finished)
            if looks == "make":
                return il.job(kind, target, at=at, shape=shape, finished=fin_)
            return il.job(kind, target, at=at, shape=shape, finished=fin_, looks=looks)
        except artmod.NotVisible as e:
            raise ArtRefusal(422, "not-visible", "You haven't reached that yet.") from e
        except artmod.TextFailed as e:      # the on-demand plate's screening call failed twice
            raise ArtRefusal(502, "model-failed", str(e)[:200] or "The text model failed.") from e
        except (KeyError, StopIteration, ValueError) as e:
            raise ArtRefusal(404, "no-target", "There is no such chapter, event or entry in this index.") from e

    def draw_given(il, job, pr, text_: str) -> dict:
        """Draw the prompt as given (the person's own or edited text, or an old prompt): with `text` the pipeline never
        softens it, and a refusal is raised with the provider's reason and recorded as no skip, so the person may try again."""
        try:
            rec = il.draw(job, prompt=pr, text=text_)
        except images.ImageRefused as e:
            raise ArtRefusal(422, "refused", f"refused: {e.reason[:160]}") from e
        if rec is None:
            why = il.report.reasons[-1]["reason"] if il.report.reasons else next(iter(il.report.skipped), "skipped")
            raise ArtRefusal(422, "refused" if why.startswith("refused") else "skipped", why)
        return rec

    def prompt_for(il, args: tuple):
        """(job, Prompt) for one picture, as the prompt and draw routes both need it: one writer call (two with its rewrite).
        Nothing else is written on the way: no world note, no appearance note (the person's own background note is read by
        the pipeline from the set, as saved by `PUT .../art/world`)."""
        job = art_job(*args)                     # not visible: no call at all
        try:
            return job, il.make_prompt(job)
        except artmod.NotVisible as e:
            raise ArtRefusal(422, "not-visible", "You haven't reached that yet.") from e
        except artmod.TextFailed as e:
            raise ArtRefusal(502, "model-failed", str(e)[:200] or "The text model failed.") from e
        except artmod.Dropped as e:   # the check and the word it caught, for the person to act on
            raise ArtRefusal(422, "dropped", str(e), e.say) from e
        except ValueError as e:
            raise ArtRefusal(422, "dropped", str(e)) from e

    def prompt_parts(il, pr, style: str) -> tuple[str, str, str]:
        """(body, style sentence, no-text sentence) of a Prompt: the writer's text carries no style words, so a style is
        changed by swapping the sentence, with no new call."""
        return pr.body, il.style_sentence(style), pr.no_text

    def style_sentences(il, h: str) -> dict[str, str]:
        """slug -> style sentence for every listed style and every set this edition has (a custom one by its stored text)."""
        out = {k: il.style_sentence(k) for k in [*artmod.STYLES, *(st["slug"] for st in style_store.all())]}
        for st in arts.styles(h):
            if st.get("style") and st["slug"] not in out:
                out[st["slug"]] = il.style_sentence(st["style"])
        return out

    def prompt_key(key: str, job, fin) -> tuple:
        """What a cached Prompt is for: never the style or the shape, which change no word the writer wrote. `finished`
        None (every earlier book read) and [] (none read) are two keys: ("*",) and (). The reader's frontier (`job.reach`) is
        part of it: a scene's figure may take its earliest picture after the moment up to there, so a prompt written further on
        is never drawn for a reader who has not reached that picture."""
        return (key, job.kind, job.target, job.anchor, ("*",) if fin is None else tuple(fin), getattr(job, "reach", None))

    def picked_references(il, job, pr, fin) -> list:
        """The references the pipeline would attach in this set to a picture showing `pr.shows`: a scene's figures most tied
        first (at most `art.MAX_REFERENCES`), a look-change portrait its previous portrait, nothing for anything else."""
        pick = getattr(il, "references", None)
        if pick is not None:
            return pick(job, list(pr.shows), fin, figures=pr.figures) if pr.figures is not None else pick(job, list(pr.shows), fin)
        # until `Illustrator.references` lands: the same rule from the pipeline's own parts
        pos = (job.bi, il.books[job.bi].ord_of[pr.anchor])
        if job.kind in ("plate", "event"):
            return il._references(list(pr.shows), pos, il.view(job.bi, pr.anchor, fin), fin)
        if job.kind == "portrait" and job.change:
            prev = il.latest_picture(job.target, pos, ("portrait",), strict=True, finished=fin, current=True)
            return [prev] if prev else []
        return []

    def earlier_keys(ctx: dict) -> list[str]:
        """Every earlier book of the series in its default reading order (in the library or not)."""
        sref = ctx["doc"].get("series")
        if not sref:
            return []
        manifest, _ = proto.series_parts(ctx["item"], library.scan())
        if not manifest:
            return []
        order = manifest["doc"]["orders"].get(manifest["doc"].get("defaultOrder"), [])
        return order[:order.index(sref["book"])] if sref["book"] in order else []

    def restyled(il, job, pr, slug_: str, made_in: str):
        """A cached Prompt made for one set, ready to draw into this one: the chosen shape, and when the set differs its
        references taken again from this set's pictures, of the same entities (a set's pictures are the only references
        it sends). `inherits` keeps what the text carries and adds what the new references carry."""
        pr.shape = job.shape or artmod.SHAPE_OF[job.kind]
        if made_in == slug_:
            return pr
        fin = None if job.finished is None else frozenset(job.finished)
        refs = picked_references(il, job, pr, fin)
        def book_key(j: int) -> str:   # as the pipeline stores book keys: the series book key, else the index id
            d = il.books[j].doc
            return str((d.get("series") or {}).get("book") or d.get("id") or j)
        inherits = set(pr.inherits)
        for j, r in refs:
            inherits |= set(r.get("inherits", [])) | ({book_key(j)} if j != job.bi else set())
        pr.references, pr.inherits = refs, sorted(inherits - {book_key(job.bi)})
        return pr

    def edition_hashes(ctx: dict) -> list[str]:
        """The editions a reference of this index's pictures may live in: this one, then the earlier books of its series
        (a reference picture is stored as an id with no book, and `latest_picture` walks the earlier books)."""
        out = [ctx["hash"]]
        try:
            for d in art_prior(ctx):
                ph = artstore.text_hash(d)
                if artstore.HASH.fullmatch(ph) and ph not in out:
                    out.append(ph)
        except Exception:
            pass
        return out

    def figure_ref_url(hashes: list[str], slug_: str, ref) -> str | None:
        """The URL of a stored reference (a plain id of this style's set, or {set, id} of another style's set), served from the
        edition that holds that picture (this one, else an earlier book's: the search `redraw_references` makes); None for
        none, anything malformed, or a picture no edition holds. Built from checked ids only."""
        if isinstance(ref, str):
            pid_, sl = ref, slug_
        elif isinstance(ref, dict) and isinstance(ref.get("set"), str) and isinstance(ref.get("id"), str):
            pid_, sl = ref["id"], ref["set"]
        else:
            return None
        if not (artstore.PID.fullmatch(pid_) and artstore.SLUG.fullmatch(sl)):
            return None
        for h in hashes:
            m = arts.manifest(h, sl)
            if m and any(r.get("id") == pid_ for r in m.get("pictures", [])):
                return f"/api/art/{h}/{sl}/{pid_}"
        return None

    def picture_json(ctx: dict, slug_: str, rec: dict, hashes: list[str] | None = None) -> dict:
        out = {**rec, "url": f"/api/art/{ctx['hash']}/{slug_}/{rec['id']}"}
        if isinstance(rec.get("figures"), list):    # each reference as a URL (or null), as the prompt route answers
            hashes = hashes if hashes is not None else edition_hashes(ctx)
            out["figures"] = [{"label": f.get("label"), "entity": f.get("entity"), "ref": figure_ref_url(hashes, slug_, f.get("ref"))}
                              for f in rec["figures"] if isinstance(f, dict)]
        return out

    def ref_json(il, refs, slug_: str) -> list[dict]:
        """The references as the sheet lists them, each served from its own set: a likeness from another style's set
        carries that set's slug (`set`)."""
        return [{"id": r["id"], "kind": r["kind"], "target": r["target"], **({"set": r["set"]} if r.get("set") else {}),
                 "url": f"/api/art/{artstore.text_hash(il.books[j].doc)}/{r.get('set') or slug_}/{r['id']}"}
                for j, r in refs]

    def figures_json(il, pr, slug_: str) -> list[dict] | None:
        """A scene prompt's labelled figures for the sheet: label, entity id and the reference's URL (null when none)."""
        if pr.figures is None:
            return None
        return [{"label": f["label"], "entity": f["entity"],
                 "ref": (ref_json(il, [hit], slug_)[0]["url"] if hit else None)}
                for f, hit in artmod.Illustrator.pair_figures(pr.figures, pr.references)]

    def art_busy(h: str) -> None:
        if jobs.drawing(h):
            raise ArtRefusal(409, "busy", "Pictures are being drawn for this book. Try again once that run has finished.")

    def art_route(fn):
        """Run a pictures route off the event loop (they read books and wait on models), answering refusals as
        {detail, code}."""
        import functools

        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            def work():
                try:
                    return fn(*args, **kwargs)
                except ArtRefusal as e:
                    return art_error(e.status, e.code, e.detail, e.why, e.extra)
                except llm.ProviderError as e:
                    return art_error(502, "model-failed", str(e)[:200])
            return await asyncio.get_running_loop().run_in_executor(None, work)
        return wrapped

    # The prompt route's Prompt, reused by draw when the job matches (an hour, the 32 latest), behind one lock.
    prompts: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
    prompts_lock = threading.Lock()

    def remember_prompt(k: tuple, pr, slug_: str = "") -> None:
        with prompts_lock:
            prompts[k] = (time.time(), pr, slug_)
            prompts.move_to_end(k)
            while len(prompts) > 32:
                prompts.popitem(last=False)

    def recall_prompt(k: tuple, with_slug: bool = False):
        """The cached Prompt (a copy), or None; `with_slug`: (Prompt, the slug of the set it was made for)."""
        with prompts_lock:
            hit = prompts.get(k)
        pr = copy.copy(hit[1]) if hit and time.time() - hit[0] < 3600 else None
        return ((pr, hit[2]) if pr else (None, "")) if with_slug else pr

    app.state.art_prompts = (remember_prompt, recall_prompt, prompts)   # for tests

    # The soften replies written for a job (by `prompt_key`) from a refused text the server itself wrote at that job's
    # anchor, with the prompt cache's lifetime: a draw of one of them is server text written at that anchor too.
    softs: OrderedDict[tuple, tuple[float, list]] = OrderedDict()

    def remember_soft(k: tuple, pr) -> None:
        with prompts_lock:
            old = softs.get(k)
            kept = [x for x in (old[1] if old and time.time() - old[0] < 3600 else [])][-7:]
            softs[k] = (time.time(), [*kept, copy.copy(pr)])
            softs.move_to_end(k)
            while len(softs) > 32:
                softs.popitem(last=False)

    def recall_softs(k: tuple) -> list:
        with prompts_lock:
            hit = softs.get(k)
        return list(hit[1]) if hit and time.time() - hit[0] < 3600 else []

    def written_as(il, h: str, style: str, pr, text_: str) -> bool:
        """Whether `text_` is the server's own text of Prompt `pr`: its body, a style sentence (the request's, the prompt's
        own, any listed style or set of the edition) and its no-text line, joined as the sheet joins them, so a style change
        alone keeps it the server's."""
        if pr is None or not getattr(pr, "body", None):
            return pr is not None and " ".join(text_.split()) == " ".join(pr.text.split())
        sentences = {il.style_sentence(style), pr.style, *style_sentences(il, h).values()}
        squash = lambda x: " ".join(str(x or "").split())    # noqa: E731
        want = squash(text_)
        return any(want == " ".join(p for p in (squash(pr.body), squash(st), squash(pr.no_text)) if p) for st in sentences)

    def server_written(il, h: str, style: str, k: tuple, text_: str, fresh=None) -> bool:
        """Whether `text_` was written by the server from the material at the job's anchor (`k` its `prompt_key`): the
        prompt cached for it (or `fresh`, the one just written), or one of its remembered soften replies."""
        return any(written_as(il, h, style, pr, text_) for pr in [fresh or recall_prompt(k), *recall_softs(k)] if pr)

    def at_position(il, job, at: str | None) -> bool:
        """File an entity picture of the book being read at the reader's position `at` (text the server did not write at
        the job's anchor is bounded only by what the reader has read). True when the anchor moved."""
        if job.kind not in artmod.ENTITY_KINDS or at is None or job.bi != len(il.books) - 1:
            return False
        ords = il.books[job.bi].ord_of
        if ords[at] <= ords[job.anchor]:
            return False
        job.anchor = at
        return True

    @app.get("/api/art")
    def art_sets():
        """Every art set on this server, per book, for Settings: the index it belongs to, its styles and their size."""
        found: dict[str, dict] = {}
        for it in library.scan().values():
            if it["doc"].get("kind") == "index":
                h = artstore.text_hash(it["doc"])
                if h not in found or (it["source"] == "generated" and found[h]["source"] != "generated"):
                    found[h] = it
        out = []
        for h in arts.editions():
            it = found.get(h)
            styles = [{k: s[k] for k in ("style", "slug", "pictures", "bytes")} for s in arts.styles(h)]
            out.append({"key": it["key"] if it else None, "title": (it["doc"].get("work") or {}).get("title") if it else None,
                        "textHash": h, "styles": styles, "bytes": sum(s["bytes"] for s in styles)})
        return out

    @app.delete("/api/art/{text_hash}")
    def art_remove_sets(text_hash: str, style: str | None = None):
        """Remove a book's pictures (one style with `?style=`), kept for undo in the journal of an index of that edition."""
        try:
            if not artstore.HASH.fullmatch(text_hash):
                raise ArtRefusal(404, "no-set", "no pictures for that book")
            slugs = [s["slug"] for s in arts.styles(text_hash) if style is None or s["slug"] == artmod.style_slug(style)]
            if not slugs:
                raise ArtRefusal(404, "no-set", "no pictures for that book")
            art_busy(text_hash)
            key = next((it["key"] for it in library.scan().values() if it["doc"].get("kind") == "index"
                        and artstore.text_hash(it["doc"]) == text_hash), None)
            entry = None
            if key:
                entry = journal_art(key, [], {"op": "remove-sets", "textHash": text_hash, "sets": slugs},
                                    f"removed the pictures ({', '.join(slugs)})")
            for slug_ in slugs:
                with arts.lock(text_hash, slug_):
                    folder = arts.folder(text_hash, slug_)
                    if entry:
                        shutil.copytree(folder, identity.dir / entry["id"] / slug_)
                    shutil.rmtree(folder)
            parent = arts.root / text_hash
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except ArtRefusal as e:
            return art_error(e.status, e.code, e.detail)
        return {"removed": slugs, "undo": entry["id"] if entry else None}

    @app.get("/api/library/{key}/art")
    def library_art(key: str, style: str | None = None):
        """The art set of one style, whole (the Reader gates every picture by its `from`), with the styles present."""
        try:
            ctx = art_index(key)
        except ArtRefusal as e:
            return art_error(e.status, e.code, e.detail)
        h = ctx["hash"]
        styles = arts.styles(h)
        slug_ = artmod.style_slug(style) if style else (styles[0]["slug"] if styles else None)
        m = arts.manifest(h, slug_) if slug_ else None
        body = copy.deepcopy(m) if m else artmod.blank(h, style or "")
        bg = arts.background(h)                 # the person's note is the edition's, in every style
        if bg:
            body["world"] = artstore.world_of(bg)
        hashes = edition_hashes(ctx) if m and any(isinstance(r.get("figures"), list) for r in body.get("pictures", [])) else None
        body["pictures"] = [picture_json(ctx, slug_, r, hashes) for r in body.get("pictures", [])] if m else []
        return {**body, "slug": slug_ if m else None, "styles": styles, "size": arts.size(h)}

    def story_rows(il, rows: list[dict]) -> list[dict]:
        """The estimate's chapter rows a person can choose from: every chapter that can get a picture, and every story
        chapter (`is_story`) even with nothing to draw; front and back matter with no events ("Contents") is left out."""
        words = {c["n"]: c["words"] for c in il.books[-1].chapters}
        return [r for r in rows if r["events"] or r["pictures"] or is_story(r["title"], words.get(r["n"], 0))]

    @app.get("/api/art/estimate")
    @art_route
    def art_estimate(key: str, style: str = "ink", level: str = "main", portraits: str = "main",
                     provider: str | None = None, model: str | None = None, quality: str = "standard",
                     chapters: str | None = None, portraitProvider: str | None = None, portraitModel: str | None = None,
                     entities: str | None = None, until: str | None = None, finished: str | None = None,
                     check: bool = False, pickerProvider: str | None = None, pickerModel: str | None = None):
        """How many pictures a run would draw and what they would cost, without a model call: the screened moments
        when the set has them, else the chapters' turning points (main) or non-minor events (all). `level` main, all or
        none; `chapters` the plan numbers chosen ("1,3,4"; absent: every chapter, empty: none). Each chapter row carries
        what it would get at the level whether chosen or not (`pictures`) and how many of those are drawn (`drawn`). The
        totals (`pictures`, `plates`, `portraits`, `price`) count what a run would still draw: the chosen chapters'
        pictures not drawn yet, plus the new portraits, which the chapter choice never limits; `planned` has the same
        three counts with what is already drawn included. `entities` ("id,id"), `until` (an anchor id or the fraction of
        the book read) and `finished` ("key,key"; absent: every earlier book) estimate an entry's portraits through the book
        as the run route would check them: the portraits of those entries only, at or before the position, in this book and
        the earlier books the reader finished, without what is drawn. `portraitsMax`, `picturesMax` and `priceMax` are the
        top of the range: an entity whose looks no run has checked yet counts one portrait in `portraits` but up to
        `art.MAX_VERSIONS` here (the pipeline's `portraits_max`); all three equal the plain numbers when every counted entity
        is checked. With `check`, every price adds the drawing-fault check: one call per picture on the model that would
        make it and, for the share of pictures the bench found a major fault in (`art.FIX_RATE`), a fix and its verification
        (`art.verify_price`, else `art.VERIFY_FALLBACK`), answered as `check` {each, verify, fixRate, price, priceMax, provider, model}. The model is the one the run checks with, its choosing-calls model
        (`pickerProvider` / `pickerModel`, else the cheap tier), priced by `art.check_price`, else `art.CHECK_FALLBACK`; a
        model that cannot see pictures leaves the run unchecked, so nothing is added and there is no `check` key."""
        ctx = art_index(key)
        if level not in artmod.LEVELS:
            raise ArtRefusal(422, "bad-config", "level must be main, all or none.")
        # main, all or none; true and false from an older page mean all and none
        cast = {"true": "all", "false": None, "none": None, "main": "main", "all": "all"}.get(portraits.lower(), "?")
        if cast == "?":
            raise ArtRefusal(422, "bad-config", "portraits must be main, all or none.")
        chosen = None
        if chapters is not None:
            parts = [x.strip() for x in chapters.split(",") if x.strip()]
            if len(parts) > 10000 or not all(re.fullmatch(r"[0-9]{1,9}", x) for x in parts):   # ASCII only
                raise ArtRefusal(422, "bad-config", "chapters must be chapter numbers separated by commas.")
            chosen = {int(x) for x in parts}
        ecfg = None
        if entities is not None or until is not None or finished is not None:
            def listed(raw: str | None) -> list[str] | None:
                return None if raw is None else [x.strip() for x in raw.split(",") if x.strip()]
            u = None
            if until is not None:
                u = {"p": query_position(ctx, until)}
            try:
                ecfg = art_config({"style": style, "level": level, "chapters": None, "portraits": cast or False,
                                   "quality": quality, "provider": provider, "model": model, "cap": 1,
                                   "entities": listed(entities), "until": u, "finished": listed(finished)})
            except ValueError as e:
                raise ArtRefusal(422, "bad-config", str(e)) from e
            ecfg = entity_choice(ctx, ecfg)[0]
        il, image, _ = illustrator(ctx, art_style(style), quality, provider, model, text_needed=False, portraits=cast,
                                   cfg=ecfg)
        if chosen and any(n not in {c["n"] for c in il.books[-1].chapters} for n in chosen):
            raise ArtRefusal(422, "bad-config", "Some of the chosen chapters are not chapters of this book.")
        il.scene_chapters = chosen     # the chapter choice limits scenes only (art.Illustrator's `scene_chapters`)
        got = il.counts(level=level)
        # an upper bound while a look index is unchecked (the pipeline's `portraits_max`, when it gives one)
        # (`portraits_max` is what `new_portraits` could grow to, so the range's extra is measured from that)
        extra_max = max(0, got.get("portraits_max", got["new_portraits"]) - got["new_portraits"])
        for bi in range(len(il.books) - 1):     # an entity run's earlier books the reader finished: their portraits too
            b = il.books[bi]
            if b.b.draw and b.epub is not None:
                g = il.counts(bi, level=None)
                for k in ("portraits", "new_portraits"):
                    got[k] += g[k]
                extra_max += max(0, g.get("portraits_max", g["new_portraits"]) - g["new_portraits"])
        rows = story_rows(il, got["chapters"])
        # the totals and the price are what a run would still draw (it skips what the set holds); `planned` the full plan
        plates = sum(c["pictures"] - c["drawn"] for c in got["chapters"] if c["chosen"])
        chapters_ = [{"n": c["n"], "title": c["title"], "drawable": c["drawable"], "screened": c["drawable"] is not None,
                      "events": c["events"], "pictures": c["pictures"], "drawn": c["drawn"], "chosen": c["chosen"]}
                     for c in rows]
        ports = got["new_portraits"]
        prov = provider or getattr(image, "provider", None) or next(iter(llm.IMAGE_PROVIDERS))
        name = model or getattr(image, "name", None) or llm.IMAGE_PROVIDERS[prov]["default"]
        # Portraits are priced with their own image model (`portraitProvider` / `portraitModel`; a model alone is one of
        # the scene provider's), else the scenes' one.
        pprov = portraitProvider if portraitProvider is not None else prov
        if pprov not in llm.IMAGE_PROVIDERS:
            raise ArtRefusal(422, "bad-provider", f"No image provider {pprov!r} for portraits.")
        pname = portraitModel if portraitModel is not None else \
            (name if pprov == prov else llm.IMAGE_PROVIDERS[pprov]["default"])
        if not llm.image_model(pprov, pname):
            raise ArtRefusal(422, "bad-model", f"Unknown image model {pname!r} for portraits.")
        # each at the level its model is drawn at: the one asked for, else the nearest it offers (`llm.nearest_quality`)
        squal, pqual = llm.nearest_quality(prov, name, quality), llm.nearest_quality(pprov, pname, quality)
        each = [llm.image_price(prov, name, squal, "landscape"), llm.image_price(pprov, pname, pqual, "portrait")]
        price = None if (plates and each[0] is None) or (ports and each[1] is None) else \
            round(plates * (each[0] or 0) + ports * (each[1] or 0), 4)
        high = ports + extra_max
        price_max = None if price is None or (high and each[1] is None) else round(price + extra_max * (each[1] or 0), 4)
        check_ = None
        if check and (pickerProvider is None) != (pickerModel is None):
            raise ArtRefusal(422, "bad-picker", "Give both pickerProvider and pickerModel, or neither.")
        if check and pickerProvider is not None:
            try:
                choice = text_choice_config({"provider": pickerProvider, "model": pickerModel}, "choosing calls")
                jobs.check_text_choice(choice, "choosing calls")
            except ValueError as e:
                raise ArtRefusal(422, "bad-picker", str(e)) from e
            ref = choice
        else:
            got_ = jobs.text_choice("cheap") if check else None    # the run's picker by default (`JobManager._art`)
            ref = {"provider": got_[0], "model": got_[1]} if got_ else None
        if check and ref and llm.vision(ref["provider"], ref["model"]) is not False:
            one = artmod.check_price(ref.get("provider"), ref.get("model"))
            one = artmod.CHECK_FALLBACK if one is None else one
            verify = artmod.verify_price(ref.get("provider"), ref.get("model"))
            verify = artmod.VERIFY_FALLBACK if verify is None else verify

            def with_check(total, n_plates: int, n_ports: int):
                if total is None:
                    return None
                # a check per picture, and for the share expected to have a major fault one fix and its verification
                return round(total + (n_plates + n_ports) * (one + artmod.FIX_RATE * verify)
                             + artmod.FIX_RATE * (n_plates * (each[0] or 0) + n_ports * (each[1] or 0)), 4)
            check_ = {"each": round(one, 6), "verify": round(verify, 6), "fixRate": artmod.FIX_RATE,
                      "price": None if price is None else round(with_check(price, plates, ports) - price, 4),
                      "priceMax": None if price_max is None else round(with_check(price_max, plates, high) - price_max, 4),
                      "provider": ref["provider"], "model": ref["model"]}
            price, price_max = with_check(price, plates, ports), with_check(price_max, plates, high)
        return {**({"check": check_} if check_ else {}), "pictures": plates + ports, "plates": plates, "portraits": ports, "price": price, "provider": prov,
                "picturesMax": plates + high, "portraitsMax": high, "priceMax": price_max,
                **({"entities": ecfg["entities"], "until": ecfg["until"], "finished": ecfg["finished"]} if ecfg else {}),
                "model": name, "quality": squal, "portraitProvider": pprov, "portraitModel": pname, "portraitQuality": pqual,
                "level": level, "chapters": chapters_,
                "planned": {"pictures": got["plates"] + got["portraits"], "plates": got["plates"], "portraits": got["portraits"]}}

    @app.get("/api/art/{text_hash}/{style}/{pid}")
    def art_file(text_hash: str, style: str, pid: str):
        try:
            p = arts.path(text_hash, style, pid)
        except KeyError:
            p = None
        if p is None:
            raise HTTPException(404, "no such picture")
        media = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(p.suffix, "application/octet-stream")
        return FileResponse(p, media_type=media, headers={"Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/library/{key}/art/run")
    async def art_run(key: str, request: Request):
        """Pictures for this index as a run on the queue (the build form's pictures stage, on an existing index)."""
        body = await json_body(request)

        @art_route
        def work():
            ctx = art_index(key)
            art_epub(ctx)
            owner = next((e for e in catalog.entries() if any(v["key"] == key for v in e["versions"])), None)
            try:
                cfg = art_config(body.get("art", body))
                if cfg["chapters"]:     # each chosen chapter must be one of this book's (plan numbers, as the estimate lists)
                    il = illustrator(ctx, cfg["style"], cfg["quality"], cfg["provider"], cfg["model"], text_needed=False,
                                     portraits=cfg["portraits"])[0]
                    chapter_choice(cfg, il.books[-1].chapters)
                cfg, also = entity_choice(ctx, cfg)     # an entry's portraits through the book, up to the reader's position
                job = jobs.submit_art(key, cfg, index_path=str(ctx["item"]["path"]),
                                      book_id=(owner or {}).get("localId") or "", text_hash=ctx["hash"],
                                      title=ctx["doc"].get("work", {}).get("title") or key, also=also)
            except ValueError as e:
                raise ArtRefusal(422, "bad-config", str(e)) from e
            except RuntimeError as e:
                raise ArtRefusal(409, "busy", str(e)) from e
            return job_json(job)
        return await work()

    @app.post("/api/library/{key}/art/prompt")
    async def art_prompt(key: str, request: Request):
        """Write the prompt for one picture (one writer call, nothing drawn). The answer has the prompt whole and in its parts
        (`body`, the `style` sentence, `noText`) with the sentence of every style (`styles`, by slug), so the sheet changes
        style, shape, quality, provider or model without asking again; plus anchor, references, price, slug, inherits."""
        body = await json_body(request)

        @art_route
        def work():
            ctx = art_index(key)
            style = art_style(body.get("style"))
            art_busy(ctx["hash"])
            quality = body.get("quality") or "standard"
            fin = art_finished(ctx, body)
            slug_ = arts.resolve(ctx["hash"], style)[0]
            with arts.lock(ctx["hash"], slug_):
                il, image, slug_ = illustrator(ctx, style, quality, *art_image_choice(body, body.get("kind")),
                                               tier="strong", writer=art_writer(body))
                given, at = art_position(ctx, body)
                args = (il, body.get("kind"), body.get("target"), given, at, body.get("shape"), fin)
                job, pr = prompt_for(il, args)
            out = prompt_answer(ctx, il, image, job, pr, style, quality, body, fin)
            remember_prompt(prompt_key(key, job, fin), pr, slug_)
            return out
        return await work()

    def prompt_answer(ctx: dict, il, image, job, pr, style: str, quality: str, body: dict, fin) -> dict:
        """The prompt route's answer for one written Prompt (the soften route answers the same shape)."""
        slug_, stored = arts.resolve(ctx["hash"], style)
        body_, sentence, no_text = prompt_parts(il, pr, stored)
        # The price of the chosen provider and quality, also before its key is added.
        ip, im = art_image_choice(body, job.kind)
        prov = ip or getattr(image, "provider", None) or next(iter(llm.IMAGE_PROVIDERS))
        name = im or getattr(image, "name", None) or llm.IMAGE_PROVIDERS[prov]["default"]
        # `model` stays the image model (the sheet's Model choice, the portrait one for a portrait, place or thing when
        # sent); `writer` names the text model that wrote the prompt: the request's choice, else the strong tier
        chosen = text_choice_config(body.get("writer")) if body.get("writer") is not None else None
        wp, wn = (chosen["provider"], chosen["model"]) if chosen else (jobs.text_choice("strong") or (None, None))
        if isinstance(getattr(pr, "written_by", None), dict):     # the model that did write it, when it has a name
            wp, wn = pr.written_by.get("provider"), pr.written_by.get("model")
        out = {"prompt": pr.text, "body": body_, "writer": {"provider": wp, "model": wn}, "style": sentence, "noText": no_text,
               "styles": style_sentences(il, ctx["hash"]), "scene": pr.scene, "anchor": pr.anchor, "shape": pr.shape,
               "kind": job.kind, "target": job.target, "references": ref_json(il, pr.references, slug_),
               "figures": figures_json(il, pr, slug_), "provider": prov, "model": name, "quality": llm.nearest_quality(prov, name, quality),
               "price": llm.image_price(prov, name, llm.nearest_quality(prov, name, quality), pr.shape), "slug": slug_,
               "inherits": list(pr.inherits), "partial": pr.partial, "finished": fin}
        return out

    @app.post("/api/library/{key}/art/soften")
    async def art_soften(key: str, request: Request):
        """Rewrite a prompt the image model refused so the same moment can be drawn (one writer call, strong tier, nothing
        drawn): the prompt route's body plus `text` (what was refused) and `reason` (the provider's words). Gated like the
        prompt route and answered in its shape. Never cached as the job's prompt. When the refused text was the server's own
        at the job's anchor (`server_written`), the reply is remembered beside it (`remember_soft`), so a draw of it counts as
        server text and an entity picture keeps its first-look anchor; a refused text the person wrote is rewritten from the
        material at the reader's position (`at_position`), answers that anchor, and is not remembered, so its draw is filed
        there too."""
        body = await json_body(request)

        @art_route
        def work():
            ctx = art_index(key)
            style = art_style(body.get("style"))
            refused = " ".join(str(body.get("text") or "").split())
            reason = " ".join(str(body.get("reason") or "").split())
            if not refused or len(refused) > 4000:
                raise ArtRefusal(422, "bad-prompt", "Give the refused prompt, at most 4,000 characters.")
            if len(reason) > 300:
                raise ArtRefusal(422, "bad-reason", "Keep the reason to 300 characters.")
            art_busy(ctx["hash"])
            quality = body.get("quality") or "standard"
            fin = art_finished(ctx, body)
            slug_ = arts.resolve(ctx["hash"], style)[0]
            with arts.lock(ctx["hash"], slug_):
                il, image, slug_ = illustrator(ctx, style, quality, *art_image_choice(body, body.get("kind")),
                                               tier="strong", writer=art_writer(body))
                given, at = art_position(ctx, body)
                job = art_job(il, body.get("kind"), body.get("target"), given, at, body.get("shape"), fin)
                if not hasattr(il, "soften"):
                    raise ArtRefusal(501, "unavailable", "This server can't rewrite a refused prompt yet.")
                # An entity's picture shows from its first look only for text the server wrote from there; a refused text
                # it did not write (the person's edit) may use anything read up to the position, so the rewrite is written
                # from the material at the position and answers that anchor.
                k = prompt_key(key, job, fin)
                own = server_written(il, ctx["hash"], style, k, refused)
                if not own:
                    at_position(il, job, at)
                try:
                    pr = il.soften(job, refused, reason, finished=None if fin is None else set(fin))
                except artmod.NotVisible as e:
                    raise ArtRefusal(422, "not-visible", "You haven't reached that yet.") from e
                except artmod.TextFailed as e:
                    raise ArtRefusal(502, "model-failed", str(e)[:200] or "The text model failed.") from e
                except artmod.Dropped as e:
                    raise ArtRefusal(422, "dropped", str(e), e.say) from e
                except ValueError as e:
                    raise ArtRefusal(422, "dropped", str(e)) from e
                if own:
                    remember_soft(k, pr)
            return prompt_answer(ctx, il, image, job, pr, style, quality, body, fin)
        return await work()

    @app.post("/api/library/{key}/art/draw")
    async def art_draw(key: str, request: Request):
        """Draw one picture with the given prompt (the person may have edited it) and file it: the new record, undone like an edit."""
        body = await json_body(request)

        @art_route
        def work():
            ctx = art_index(key)
            style = art_style(body.get("style"))
            text_ = " ".join(str(body.get("prompt") or "").split())
            if not text_ or len(text_) > 4000:
                raise ArtRefusal(422, "bad-prompt", "Give the prompt to draw, at most 4,000 characters.")
            art_busy(ctx["hash"])
            fin = art_finished(ctx, body)
            check = body.get("check", False)
            if not isinstance(check, bool):
                raise ArtRefusal(422, "bad-config", "check must be true or false.")
            # with `check`, the picture is looked at for drawing faults by a model that sees pictures (`picker`, else the
            # first configured one) and fixed once; none that can: drawn unchecked, as the pipeline does. That model makes
            # the check only: every other choosing call stays with the usual picker.
            checker = art_picker(body) if check else None
            slug_ = arts.resolve(ctx["hash"], style)[0]
            with arts.lock(ctx["hash"], slug_):
                il, image, slug_ = illustrator(ctx, style, body.get("quality") or "standard",
                                               *art_image_choice(body, body.get("kind")), tier="strong",
                                               writer=art_writer(body),   # a cache miss writes as the sheet does
                                               checker=checker, check=check and checker is not None)
                if image is None:
                    raise ArtRefusal(409, "no-image-key", "Add a Gemini or OpenAI key in Settings to draw pictures.")
                given, at = art_position(ctx, body)
                args = (il, body.get("kind"), body.get("target"), given, at, body.get("shape"), fin)
                job = art_job(*args)
                # The prompt route's Prompt, whatever style it was asked in, for its anchor, sources and references (the
                # person's text is drawn); a miss writes it once.
                pr, made_in = recall_prompt(prompt_key(key, job, fin), with_slug=True)
                missed = pr is None
                if missed:
                    job, pr = prompt_for(il, args)
                    remember_prompt(prompt_key(key, job, fin), copy.copy(pr), slug_)
                    made_in = slug_
                k = prompt_key(key, job, fin)
                own = server_written(il, ctx["hash"], style, k, text_, fresh=copy.copy(pr))
                pr = restyled(il, job, pr, slug_, made_in)
                if missed and text_ != " ".join(pr.text.split()):
                    # The text drawn is not the prompt just written for this reader, so it may come from one written for a
                    # reader with more books finished: it is hidden until every earlier book is (`partial` is this request's).
                    pr.inherits = sorted(set(pr.inherits) | set(earlier_keys(ctx)))
                # An entity's picture shows from its first look only when the text drawn is the server's own, written from
                # the material there (the prompt, in any style, or a soften reply of it); any other text (the person's
                # edit) may use whatever the reader has read, so it is filed at the reader's position.
                if not own and at_position(il, job, at):
                    pr.anchor = job.anchor
                old_rec = replaced_record(ctx, il, job, slug_, at, fin, body.get("replaces"))
                rec = draw_given(il, job, pr, text_)
                if old_rec is None:
                    entry = journal_art(key, [], {"op": "add", "textHash": ctx["hash"], "style": slug_,
                                                  "added": [rec["id"]]}, f"drew a picture ({rec['kind']})")
                else:
                    entry = take_place_of(key, ctx["hash"], slug_, old_rec, rec)
                    rec = {**rec, "replaces": old_rec["id"]}
            return {**picture_json(ctx, slug_, rec), "slug": slug_, "undo": entry["id"]}
        return await work()

    def replaced_record(ctx: dict, il, job, slug_: str, at: str | None, fin, pid) -> dict | None:
        """The picture a draw from the sheet takes the place of (`replaces`), or None: one of the same set, kind and target,
        that the reader can see by the same gate as the target (its anchor at or before the position, and nothing from an
        earlier book the reader has not finished). Checked before anything is drawn."""
        if pid is None:
            return None
        if not isinstance(pid, str) or not pid:
            raise ArtRefusal(422, "bad-replaces", "replaces must be a picture id.")
        rec = next((r for r in (arts.manifest(ctx["hash"], slug_) or {}).get("pictures", []) if r.get("id") == pid), None)
        if rec is None:
            raise ArtRefusal(422, "bad-replaces", "That picture is not in this style's pictures.")
        if rec.get("kind") != job.kind or rec.get("target") != job.target:
            raise ArtRefusal(422, "bad-replaces", "That picture is of something else.")
        ords = il.books[job.bi].ord_of
        unfinished = set(earlier_keys(ctx)) - set(fin) if fin is not None else set()
        if (rec.get("from") not in ords or (at is not None and ords[rec["from"]] > ords[at])
                or unfinished & set(rec.get("inherits") or [])):
            raise ArtRefusal(422, "not-visible", "You haven't reached that picture yet.")
        return rec

    def take_place_of(key: str, h: str, slug_: str, old_rec: dict, new: dict, what: str = "drew a picture again") -> dict:
        """File `new` (already in the set) in the place of `old_rec`: the new record `replaces` it and the old leaves the set
        with its file, both kept in one journal entry (op `redraw`) whose undo puts the old one back. Under the set's lock."""
        pid = old_rec["id"]
        old = arts.path(h, slug_, pid)
        entry = journal_art(key, [old] if old and old.name != new["file"] else [],
                            {"op": "redraw", "textHash": h, "style": slug_, "removed": [old_rec], "added": [new["id"]]},
                            f"{what} ({old_rec['kind']})")
        try:
            man = arts.manifest(h, slug_)
            for r in man["pictures"]:
                if r["id"] == new["id"]:
                    r["replaces"] = pid
                    if "inherits" not in old_rec and "inherits" not in new:
                        r.pop("inherits", None)
            man["pictures"] = [r for r in man["pictures"] if r["id"] != pid or r["id"] == new["id"]]
            man["cost"] = round(sum(r.get("cost") or 0 for r in man["pictures"]), 6)
            arts.save(h, slug_, man)
            if old is not None and new["file"] != old.name:
                old.unlink(missing_ok=True)
        except Exception:
            identity.discard(entry["id"])
            raise
        return entry

    def art_record(ctx: dict, pid: str) -> tuple[str, dict]:
        hit = arts.find(ctx["hash"], pid)
        if hit is None:
            raise ArtRefusal(404, "no-picture", "no such picture")
        return hit

    @app.delete("/api/library/{key}/art/{pid}")
    def art_delete(key: str, pid: str):
        """Remove a picture; undone like an edit (`POST /api/library/{key}/undo/{id}`)."""
        try:
            ctx = art_index(key)
            slug_, rec = art_record(ctx, pid)
            art_busy(ctx["hash"])
            with arts.lock(ctx["hash"], slug_):
                f = arts.path(ctx["hash"], slug_, pid)
                entry = journal_art(key, [f] if f else [], {"op": "delete", "textHash": ctx["hash"], "style": slug_,
                                                            "removed": [rec]}, f"removed a picture ({rec['kind']})")
                try:
                    arts.remove(ctx["hash"], slug_, pid)
                except Exception:
                    identity.discard(entry["id"])
                    raise
        except ArtRefusal as e:
            return art_error(e.status, e.code, e.detail)
        return {"removed": pid, "undo": entry["id"]}

    def redraw_references(il, rec: dict, pid: str) -> list[tuple[int, dict]]:
        """The references a record was drawn with, found again: a plain id in this set (any book of the run), a `{set, id}`
        likeness in that other style's set of the same book's edition (read through it by `Illustrator.reference_bytes`).
        One that is gone, or malformed, is left out."""
        out = []
        for ref in rec.get("references") or []:
            if isinstance(ref, str):
                if ref != pid:
                    out += [(j, r) for j in range(len(il.books)) for r in il.man[j]["pictures"] if r["id"] == ref][:1]
            elif isinstance(ref, dict) and isinstance(ref.get("set"), str) and isinstance(ref.get("id"), str):
                for j in range(len(il.books)):
                    hit = next((r for slug, _, lm in il.like[j] if slug == ref["set"]
                                for r in lm.get("pictures", []) if r.get("id") == ref["id"]), None)
                    if hit is not None:
                        out.append((j, {**hit, "set": ref["set"]}))
                        break
        return out

    @app.post("/api/library/{key}/art/{pid}/redraw")
    async def art_redraw(key: str, pid: str, request: Request):
        """Draw a picture again, with the person's note put first in its prompt; the new record `replaces` the old,
        which leaves the set and is kept for undo. An optional `p` is the reader's position (as the prompt route's): a scene's
        reference from after the scene is sent again only when the reader has reached it with no recorded look change
        between (`Illustrator.reference_reached`); one left out is drawn without, as a reference that is gone. No `p`: every
        stored reference, as before."""
        body = await json_body(request) if int(request.headers.get("content-length") or 0) else {}
        note = " ".join(str(body.get("note") or "").split())

        @art_route
        def work():
            if len(note) > 300:
                raise ArtRefusal(422, "bad-note", "Keep the note to 300 characters.")
            ctx = art_index(key)
            slug_, rec = art_record(ctx, pid)
            art_busy(ctx["hash"])
            with arts.lock(ctx["hash"], slug_):
                prov = rec.get("provider") if rec.get("provider") in llm.IMAGE_PROVIDERS else None
                mod = rec.get("model") if prov and llm.image_model(prov, rec.get("model") or "") else None
                qual = rec.get("quality") if rec.get("quality") in QUALITIES else "standard"
                il, image, slug_ = illustrator(ctx, slug_, qual, prov, mod)
                if image is None:
                    raise ArtRefusal(409, "no-image-key", "Add a Gemini or OpenAI key in Settings to draw pictures.")
                if rec.get("kind") not in artmod.SHAPE_OF or rec.get("from") not in il.books[-1].ord_of:
                    raise ArtRefusal(409, "stale", "This picture no longer fits the index, so it can't be drawn again.")
                job = artmod.Job(rec["kind"], rec["target"], len(il.books) - 1, rec["from"], shape=rec.get("shape") or "")
                refs = redraw_references(il, rec, pid)
                if body.get("p", body.get("anchor")) is not None:
                    given, at = art_position(ctx, body)
                    job.reach = (job.bi, il.books[job.bi].ord_of[at] if at is not None else -1)
                    refs = [(j, r) for j, r in refs if il.reference_reached(job, j, r)]
                figures = None      # older records and non-scenes: no labels, drawn as before
                if rec.get("kind") in ("plate", "event") and isinstance(rec.get("figures"), list):
                    figures = [{"label": f["label"], "entity": f["entity"]} for f in rec["figures"]
                               if isinstance(f, dict) and isinstance(f.get("label"), str) and isinstance(f.get("entity"), str)]
                # a portrait drawn with the older half-length frame is drawn again with the mid-chest one
                text_ = (note.rstrip(".") + ". " if note else "") + artmod.reframed(rec["kind"], rec["prompt"], il.style_sentence())
                # The text is the old prompt's, so is the earlier-book content it carries: `inherits` (and `partial`) go
                # with it, or the Reader would show a prompt it had hidden until those books are finished. A record from
                # before `inherits` existed gets none on the new one either, so the Reader keeps treating it as unknown.
                pr = artmod.Prompt(text=text_, scene=text_, anchor=rec["from"], shape=rec.get("shape") or job.shape,
                                   references=refs, sources=list(rec.get("sources") or [rec["from"]]),
                                   inherits=[k for k in rec.get("inherits") or [] if isinstance(k, str)],
                                   partial=bool(rec.get("partial")),
                                   notes=[(rec["target"], rec["appearance"])] if rec.get("appearance") else [],
                                   version=rec.get("notes"), body=text_, figures=figures,
                                   written_by=dict(rec["writer"]) if isinstance(rec.get("writer"), dict) else None)
                new = draw_given(il, job, pr, text_)
                if "inherits" not in rec:
                    new.pop("inherits", None)
                entry = take_place_of(key, ctx["hash"], slug_, rec, new)
            return {**picture_json(ctx, slug_, {**new, "replaces": pid}), "slug": slug_, "undo": entry["id"]}
        return await work()

    def art_gate(ctx: dict, il, rec: dict, body: dict, fin: list[str] | None) -> None:
        """A picture asked about by the reader must be one they can see: its anchor at or before `p` (when given) and
        nothing from an earlier book they have not finished (`inherits`, the gate the Reader applies)."""
        ords = il.books[-1].ord_of
        if rec.get("kind") not in artmod.SHAPE_OF or rec.get("from") not in ords:
            raise ArtRefusal(409, "stale", "This picture no longer fits the index.")
        if body.get("p", body.get("anchor")) is not None:
            _, at = art_position(ctx, body)
            if at is None or ords[rec["from"]] > ords[at]:
                raise ArtRefusal(422, "not-visible", "You haven't reached that picture yet.")
        # A record with no `inherits` list (drawn before it existed) in a later series book waits for every earlier book,
        # as the Reader's `unfinishedSources` has it.
        inh = rec.get("inherits")
        need = {k for k in inh if isinstance(k, str)} if isinstance(inh, list) else set(earlier_keys(ctx))
        unfinished = set(earlier_keys(ctx)) - set(fin) if fin is not None else set()
        if unfinished & need:
            raise ArtRefusal(422, "not-visible", "You haven't reached that picture yet.")

    SEVERITIES = ("major", "minor")

    def fault_json(f: dict) -> dict:
        """A fault as the routes answer it: {what, where, severity}, a missing or unknown severity read as major (an older
        pipeline's list, a person's own line)."""
        sev = f.get("severity")
        return {"what": f.get("what", ""), "where": f.get("where", ""), "severity": sev if sev in SEVERITIES else "major"}

    @app.post("/api/library/{key}/art/{pid}/check")
    async def art_check(key: str, pid: str, request: Request):
        """Look at one picture for drawing faults (an extra hand, a melted face, a sword through a hand...): one call on a
        text model that can see pictures (`picker` when the body sends one, else the strong tier, `art_checker`), with the
        picture's stored prompt as context. Answers {faults: [{what, where, severity}], cost, model: {provider, model}};
        nothing is written."""
        body = await json_body(request) if int(request.headers.get("content-length") or 0) else {}

        @art_route
        def work():
            ctx = art_index(key)
            slug_, rec = art_record(ctx, pid)
            art_busy(ctx["hash"])
            fin = art_finished(ctx, body)
            checker = art_checker(body)
            if checker is None:
                raise ArtRefusal(409, "no-vision", "None of the models set up here can see pictures. Add a Claude, OpenAI "
                                                   "or Gemini key in Settings to check them.")
            il, _, slug_ = illustrator(ctx, slug_, text_needed=False, picker=checker)
            art_gate(ctx, il, rec, body, fin)
            got = il.check_picture(rec, bi=len(il.books) - 1)
            if not got.checked:
                if il.no_vision:
                    raise ArtRefusal(409, "no-vision", f"The model can't see pictures: {il.no_vision}"[:300])
                if got.why == "the picture file is missing":
                    raise ArtRefusal(409, "stale", "The picture file is missing.")
                raise ArtRefusal(502, "model-failed", (got.why or "The check failed.")[:200])
            return {"faults": [fault_json(f) for f in got.faults], "cost": round(got.cost, 6),
                    "model": artmod.model_ref(checker)}
        return await work()

    def fix_request(body: dict) -> tuple[list[dict], str]:
        """The faults and note of a fix request, checked: 0 to 8 faults, each {what: 1 to 200 characters, where: 0 to
        120, severity: major or minor (missing: major)}, a note of at most 300, and at least one fault or a note. Raises
        422 `bad-fix`."""
        raw = body.get("faults", [])
        note = body.get("note") or ""
        if not isinstance(raw, list) or len(raw) > artmod.MAX_FIX_FAULTS or not isinstance(note, str):
            raise ArtRefusal(422, "bad-fix", "List at most 8 things to fix.")
        faults = []
        for f in raw:
            what = f.get("what") if isinstance(f, dict) else None
            where = f.get("where", "") if isinstance(f, dict) else None
            if not isinstance(what, str) or not isinstance(where if where is not None else "", str):
                raise ArtRefusal(422, "bad-fix", "Each thing to fix needs what is wrong.")
            sev = f.get("severity", "major")
            if sev is None:
                sev = "major"
            if sev not in SEVERITIES:
                raise ArtRefusal(422, "bad-fix", "A fault's severity is major or minor.")
            what, where = " ".join(what.split()), " ".join((where or "").split())
            if not 1 <= len(what) <= artmod.FAULT_CHARS or len(where) > artmod.WHERE_CHARS:
                raise ArtRefusal(422, "bad-fix", "Say what is wrong in at most 200 characters, and where in at most 120.")
            faults.append({"what": what, "where": where, "severity": sev})
        note = " ".join(note.split())
        if len(note) > artmod.FIX_NOTE_CHARS:
            raise ArtRefusal(422, "bad-fix", "Keep the note to 300 characters.")
        if not faults and not note:
            raise ArtRefusal(422, "bad-fix", "Say what to fix.")
        return faults, note

    def verified_json(verdict, of: int) -> dict:
        """A fix verdict (`art.Verdict`) as `fixes[-1].verified` holds it: counts of the faults put right and partly put
        right, out of `of`, each fault's result and the new faults the fix added."""
        results = [r for r in getattr(verdict, "results", None) or [] if isinstance(r, dict)]
        return {"fixed": sum(r.get("fixed") == "yes" for r in results),
                "partly": sum(r.get("fixed") == "partly" for r in results), "of": of,
                "results": results, "new": [fault_json(f) for f in getattr(verdict, "new", None) or [] if isinstance(f, dict)]}

    UNCHANGED = "The model sent the picture back without the fix, so your picture is kept."

    @app.post("/api/library/{key}/art/{pid}/fix")
    async def art_fix(key: str, pid: str, request: Request):
        """Fix the drawing faults of a picture: one edit by the image model that drew it (its provider, model, quality and
        shape), the picture itself the only reference, everything but the listed faults kept; then one call on the checker
        (`art_checker`, as the check route's) comparing the two pictures. An edit that changed nothing, or put none of the
        faults right, is 422 `unchanged` with its `verified` and `fixCost`, and nothing is filed. Otherwise the new record
        takes the old one's place as a redraw does (one journal entry, undone like an edit; the stored prompt is
        unchanged) and the reply carries `verified` (null when the verdict failed or no model can see pictures) and
        `beforeUrl`, the replaced picture while the entry is live (`/before`)."""
        body = await json_body(request)

        @art_route
        def work():
            faults, note = fix_request(body)
            ctx = art_index(key)
            slug_, rec = art_record(ctx, pid)
            art_busy(ctx["hash"])
            fin = art_finished(ctx, body)
            checker = art_checker(body)
            with arts.lock(ctx["hash"], slug_):
                slug_, rec = art_record(ctx, pid)       # read again under the lock
                prov = rec.get("provider") if rec.get("provider") in llm.IMAGE_PROVIDERS else None
                mod = rec.get("model") if prov and llm.image_model(prov, rec.get("model") or "") else None
                qual = rec.get("quality") if rec.get("quality") in QUALITIES else "standard"
                il, image, slug_ = illustrator(ctx, slug_, qual, prov, mod, text_needed=False)
                if image is None:
                    which = llm.PROVIDERS[prov]["label"] if prov else "Gemini or OpenAI"
                    raise ArtRefusal(409, "no-image-key", f"Add a {which} key in Settings to fix this picture.")
                art_gate(ctx, il, rec, body, fin)
                try:
                    new = il.fix(rec, faults, note, bi=len(il.books) - 1, verify=checker is not None, checker=checker)
                except images.ImageRefused as e:
                    raise ArtRefusal(422, "refused", f"refused: {e.reason[:160]}") from e
                except ValueError as e:
                    if "missing" in str(e):
                        raise ArtRefusal(409, "stale", "The picture file is missing.") from e
                    raise ArtRefusal(422, "bad-fix", str(e)) from e
                except artmod.FixUnchanged as e:
                    v = getattr(e, "verdict", None)
                    raise ArtRefusal(422, "unchanged", UNCHANGED, extra={
                        "verified": verified_json(v, len(faults)) if v is not None else None,
                        "fixCost": round(getattr(e, "cost", 0) or 0, 6)}) from e
                if "inherits" not in rec:
                    new.pop("inherits", None)
                kept = arts.path(ctx["hash"], slug_, pid)
                before = kept is not None and kept.name != new["file"]
                entry = take_place_of(key, ctx["hash"], slug_, rec, new, "fixed a picture")
            last = (new.get("fixes") or [{}])[-1]
            verified = last.get("verified")
            return {**picture_json(ctx, slug_, {**new, "replaces": pid}), "slug": slug_, "undo": entry["id"],
                    "fixCost": last.get("cost") or 0,
                    "verified": ({**verified, "new": [fault_json(f) for f in verified.get("new") or []]}
                                 if isinstance(verified, dict) else None),
                    "beforeUrl": f"/api/library/{key}/art/{new['id']}/before" if before else None}
        return await work()

    @app.get("/api/library/{key}/art/{pid}/before")
    def art_before(key: str, pid: str, p: str | None = None, finished: str | None = None):
        """The picture a fix (or a redraw) replaced with `pid`: the file the journal entry that filed `pid` kept for its
        undo, served only while that entry is live (not undone) and with the gate of the picture itself (`p`, the reader's
        position, and `finished`, "key,key" of the earlier books they finished; both optional, as the check route's).
        404 `no-before` when there is none."""
        try:
            ctx = art_index(key)
            slug_, rec = art_record(ctx, pid)
            fin = art_finished(ctx, {"finished": None if finished is None else
                                     [k for k in finished.split(",") if k.strip()]})
            gate = {} if p is None else {"p": query_position(ctx, p)}
            hit = identity.art_entry_filing(key, ctx["hash"], slug_, pid)
            if hit is None:
                raise ArtRefusal(404, "no-before", "There is no earlier picture to show.")
            entry, snap = hit
            old = next((r for r in entry["art"].get("removed") or [] if isinstance(r, dict)), None)
            name = old.get("file") if old else None
            # only a picture file as the store names one (`<id>.<ext>` of the replaced record), inside the snapshot
            f = None
            if isinstance(name, str) and artstore.FILE.fullmatch(name) and name.startswith(f"{old.get('id')}."):
                f = (snap / name).resolve()
                if not f.is_relative_to(snap.resolve()) or not f.is_file():
                    f = None
            if f is None:
                raise ArtRefusal(404, "no-before", "There is no earlier picture to show.")
            il, _, slug_ = illustrator(ctx, slug_, text_needed=False)
            art_gate(ctx, il, rec, gate, fin)
            art_gate(ctx, il, old, gate, fin)
        except ArtRefusal as e:
            return art_error(e.status, e.code, e.detail, e.why)
        mime = old.get("mime") if old.get("mime") in ("image/png", "image/jpeg", "image/webp") else "image/png"
        return FileResponse(f, media_type=mime, headers={"Cache-Control": "no-cache"})

    @app.put("/api/library/{key}/art/world")
    async def art_world(key: str, request: Request):
        """The person's own note on the book's world, the writer's background note. It is the edition's, not a style's: kept
        in `art/<textHash>/background.json` and read for every set (`ArtStore.sink`), each set's own `world` written from it
        too (what an older server reads). All or nothing: a failure puts back what was written, and only then is there a
        journal entry, whose undo puts the file and every set back. Nothing writes a world note automatically any more."""
        body = await json_body(request)
        try:
            ctx = art_index(key)
            art_style(body.get("style"))     # the sheet names its style; the note is the book's whatever it is
            text_ = " ".join(str(body.get("text") or "").split())
            if not text_ or len(text_) > artstore.MAX_WORLD:
                raise ArtRefusal(422, "bad-text", f"Write the world note in 1 to {artstore.MAX_WORLD} characters.")
            art_busy(ctx["hash"])
            h = ctx["hash"]
            # The person's words come from no passage, so nothing is held back for them: the book's first anchor.
            bg = {"text": text_, "from": ctx["doc"]["anchors"][0]["id"], "edited": True,
                  "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            world = artstore.world_of(bg)
            with arts.lock(h, "background"):
                was_bg = arts.background(h)          # an older per-style note becomes the file first, so undo returns to it
                written: dict[str, object] = {}       # slug -> the stored world it had

                def put_back() -> None:
                    for sl, old in written.items():
                        with arts.lock(h, sl):
                            m = arts.manifest(h, sl)
                            if m is not None:
                                m["world"] = old
                                arts.save(h, sl, m)
                    arts.set_background(h, was_bg)
                try:
                    for st in arts.styles(h):
                        sl = st["slug"]
                        with arts.lock(h, sl):
                            m = arts.manifest(h, sl)
                            if m is None:
                                continue
                            old = m.get("world")
                            m["world"] = world
                            arts.save(h, sl, m)
                            written[sl] = old
                    arts.set_background(h, bg)
                    entry = journal_art(key, [], {"op": "world", "textHash": h, "background": was_bg, "sets": written},
                                        "changed the world note")
                except Exception:
                    put_back()
                    raise
        except ArtRefusal as e:
            return art_error(e.status, e.code, e.detail)
        return {"world": world, "undo": entry["id"]}

    # ---- duplicate entities ----------------------------------------------------------------------

    def not_community(key: str) -> None:
        """A community index is read-only: it is somebody else's work, replaced as a whole by a newer version."""
        item = library.get(key)
        if item is not None and item["source"] == "community":
            raise HTTPException(409, "A community index can't be changed here. It is read-only.")

    @app.get("/api/library/{key}/duplicates")
    def duplicates(key: str):
        try:
            return identity.duplicates(key)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e

    @app.post("/api/library/{key}/duplicates/check")
    def check_duplicates(key: str):
        not_community(key)
        try:
            return identity.check(key)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except LookupError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/library/{key}/merge")
    async def merge_entities(key: str, request: Request):
        not_community(key)
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
        not_community(key)
        try:
            return identity.tidy_names(key)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/library/{key}/merge/{merge_id}/undo")
    @app.post("/api/library/{key}/undo/{merge_id}")
    def undo_change(key: str, merge_id: str):
        """Undo the latest journal entry, a merge, a hand edit or a change to the pictures (both routes do the same).
        Pictures are not part of the index, so their changes undo on a community index too."""
        try:
            last = identity.latest(key)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        art_entry = (last or {}).get("kind") == "art" and last["id"] == merge_id
        if not art_entry:
            not_community(key)
        elif any(jobs.drawing(h_) for h_ in [last["art"]["textHash"], *(a["textHash"] for a in last["art"].get("also", []))]):
            raise HTTPException(409, "Pictures are being drawn for this book. Undo once that run has finished.")
        try:
            if art_entry:   # only the records the entry touched: pictures drawn since stay
                return identity.undo(key, merge_id, revert=art_revert)
            return editor.undo(key, merge_id)
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except (LookupError, RuntimeError) as e:
            raise HTTPException(409, str(e)) from e

    # ---- editing by hand ---------------------------------------------------------------------

    @app.post("/api/library/{key}/edits")
    async def edit_index(key: str, request: Request):
        """Apply one edit (see editor.py for the operations) as one undoable step."""
        not_community(key)
        op = await request.json()
        try:
            return editor.apply(key, op, user=who(request))
        except KeyError as e:
            raise HTTPException(404, "no such index") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except WordingError as e:
            return JSONResponse({"detail": str(e), "code": "wording"}, status_code=422)
        except EditError as e:
            raise HTTPException(400, str(e)) from e
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/catalog/books/{key}/handmade")
    async def handmade(key: str, request: Request):
        """An empty index for a library book, to be written by hand in the Reader's edit mode."""
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        local = prepare(key)
        e = catalog.entry(key)
        path = books.path(local)
        series_id = e["series"]["id"] if e["series"] else None
        try:
            doc = editor.blank(parsed(path), path, license=str(body.get("license") or "CC0-1.0"), user=who(request),
                               series_id=series_id, book_key=slug(e["title"]) if series_id else None)
        except EditError as ex:
            raise HTTPException(409, str(ex)) from ex
        if series_id:
            write_manifest(series_id)
        stem = f"{slug(e['title']) or 'book'}-{uuid.uuid4().hex[:12]}"
        out = data_dir / "indexes" / f"{stem}.lore.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return {"key": f"generated--{stem}.lore"}

    @app.post("/api/library/{key}/distinct")
    async def mark_distinct(key: str, request: Request):
        not_community(key)
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
        return library.describe_edition(p)

    @app.get("/api/editions/{text_hash}/blocks")
    def edition_blocks(text_hash: str, seq: int = 0, before: int = 2, after: int = 2, i: str | None = None):
        """A passage of the book around paragraph `seq`, for choosing a reveal point by hand."""
        p = library.edition_path(text_hash)
        if p is None:
            raise HTTPException(404, "book file for this edition not found")
        doc = (library.get(i) or {}).get("doc") if i else None
        return editor.passage(parsed(p), seq, before, after, doc=doc)

    # ---- community indexes ------------------------------------------------------------------------

    def audit_plan():
        """The model that checks claims: the first configured cloud provider's strong model (Claude first),
        with its price. None without a cloud key: a small local judge gives numbers that aren't comparable."""
        for p in jobs.configured():
            spec = llm.PROVIDERS[p]
            name = keys.config(p).get("model") if p == "custom" else spec["strong"]
            if name:
                known = next((m for m in spec["models"] if m["id"] == name), None)
                return {"provider": p, "name": name, "label": known["label"] if known else name,
                        "price": known["price"] if known else None}
        return None

    def audit_model():
        plan = audit_plan()
        return jobs.cloud_model(plan["provider"], plan["name"]) if plan else None

    community = Community(data_dir, catalog, library, reader=parsed, model=audit_model, plan=audit_plan)
    app.state.community = community

    @app.exception_handler(CommunityError)
    async def community_error(request: Request, exc: CommunityError):
        body = {"detail": exc.detail}
        if exc.code:
            body["code"] = exc.code
        return JSONResponse(body, status_code=exc.status)

    @app.get("/api/community")
    def community_config():
        return community.config()

    @app.put("/api/community")
    async def community_set(request: Request):
        body = await json_body(request)
        return community.configure(body["enabled"] if "enabled" in body else KEEP, body.get("url"))

    @app.get("/api/community/books/{key}")
    def community_books(key: str):
        return community.books(key)

    @app.post("/api/community/fetch")
    async def community_fetch(request: Request):
        body = await json_body(request)
        return await asyncio.to_thread(community.fetch_index, str(body.get("catalogKey") or ""),
                                       str(body.get("id") or ""), body.get("digest"))

    @app.get("/api/community/preview/{digest_hex}")
    def community_preview(digest_hex: str):
        return community.preview(digest_hex)

    @app.post("/api/community/install")
    async def community_install(request: Request):
        body = await json_body(request)
        return await asyncio.to_thread(community.install, str(body.get("catalogKey") or ""),
                                       str(body.get("id") or ""), body.get("digest"))

    @app.delete("/api/community/installed/{key}")
    def community_uninstall(key: str):
        return community.uninstall(key)

    @app.post("/api/community/audit")
    async def community_audit(request: Request):
        body = await json_body(request)
        return await asyncio.to_thread(community.audit_preview, body.get("digest"))

    @app.post("/api/community/share/{key}/check")
    def community_share_check(key: str):
        return community.share_check(key)

    @app.post("/api/community/share/{key}/audit")
    async def community_share_audit(key: str):
        return await asyncio.to_thread(community.share_audit, key)

    @app.get("/api/community/share/{key}/bundle")
    def community_share_bundle(key: str):
        data, name = community.share_bundle(key)
        return Response(data, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    proto = protocol.install(app, library, parsed, api_token_value=api_token)   # /v0 and /.well-known/lorekeeper, see protocol.py
    return app
