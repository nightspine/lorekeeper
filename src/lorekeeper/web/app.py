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
from .catalog import Catalog, series_num, story_chapters
from .community import KEEP, Community, CommunityError
from .editor import EditError, Editor, WordingError
from .identity import Identity
from .jobs import BookStore, JobManager, art_config, describe
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
                "models": presets, "liveModels": live,
                "default": spec["default"] or cfg.get("model"), "cheap": spec["cheap"] or cfg.get("model"),
                "strong": spec["strong"] or cfg.get("model"), "images": images}

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

    # ---- pictures: art sets in the art store, never in the index (art.py, web/art.py, CLAUDE.md §8f) ----------------

    def art_error(status: int, code: str, detail: str, why: str = "") -> JSONResponse:
        return JSONResponse({"detail": detail, "code": code, **({"why": why} if why else {})}, status_code=status)

    class ArtRefusal(Exception):
        def __init__(self, status: int, code: str, detail: str, why: str = "") -> None:
            super().__init__(detail)
            self.status, self.code, self.detail, self.why = status, code, detail, why

    QUALITIES = ("draft", "standard", "best")

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

    def art_journal(job, h: str, slug_: str, added: list[str]) -> str | None:
        """A run's pictures as one journal entry: its undo takes out exactly those."""
        key = art_key_for(job)
        if not key or library.get(key) is None:
            return None
        return journal_art(key, [], {"op": "add", "textHash": h, "style": slug_, "added": added},
                           f"drew {len(added)} picture{'s' if len(added) != 1 else ''}")["id"]
    jobs.art_inputs, jobs.art_journal = art_inputs, art_journal

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
        slug_ = a["style"]
        with arts.lock(h, slug_):
            if a.get("added"):
                arts.take_out(h, slug_, a["added"])
            if a.get("removed"):
                arts.put_back(h, slug_, a["removed"], snap)
            if a.get("op") == "world":
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

    def illustrator(ctx: dict, style: str, quality: str = "standard", provider=None, model=None, text_needed=True,
                    portraits: str | bool | None = "main"):
        """(Illustrator, image model or None, slug) for one book and style. The set is found by its slug and the
        pipeline gets the set's stored style text, so a style written another way ("Ink") never starts the set over."""
        if provider is not None and provider not in llm.IMAGE_PROVIDERS:
            raise ArtRefusal(422, "bad-provider", f"No image provider {provider!r}.")
        if model is not None and (not isinstance(model, str) or (provider and not llm.image_model(provider, model))):
            raise ArtRefusal(422, "bad-model", f"Unknown image model {model!r}.")
        if quality not in QUALITIES:
            raise ArtRefusal(422, "bad-config", "quality must be draft, standard or best.")
        text_model = jobs.cheap_model(None) if text_needed else None
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
                books_.append(artmod.Book(d, None, arts.sink(ph, slug_), None, draw=False))
        books_.append(artmod.Book(ctx["doc"], art_epub(ctx), arts.sink(ctx["hash"], slug_), None))
        return artmod.Illustrator(books_, text_model, image, style, portraits=portraits, quality=quality,
                                  log=lambda m: None, pause=0), image, slug_

    def art_style(raw) -> str:
        style = " ".join(str(raw or "").split())
        if not style or len(style) > 200:
            raise ArtRefusal(422, "bad-style", "Choose a style, or write one of at most 200 characters.")
        return style

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

    def art_job(il, kind: str, target, given: bool, at: str | None, shape=None, finished: list[str] | None = None):
        if kind not in artmod.SHAPE_OF:
            raise ArtRefusal(422, "bad-kind", "kind must be plate, event, portrait, place or thing.")
        if not isinstance(target, str) or not target:
            raise ArtRefusal(422, "bad-target", "Say what the picture is of.")
        if shape is not None and shape not in ("square", "portrait", "landscape"):
            raise ArtRefusal(422, "bad-shape", "shape must be square, portrait or landscape.")
        if given and at is None:
            raise ArtRefusal(422, "not-visible", "You haven't reached that yet.")
        try:
            return il.job(kind, target, at=at, shape=shape, finished=None if finished is None else set(finished))
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

    WORLD_KEYS = ("world", "worldOwn")

    def prompt_for(il, ctx: dict, slug_: str, args: tuple):
        """(job, Prompt) for one picture, as the prompt and draw routes both need it. The world note this reader's pictures use
        (the book's own when an earlier book is unfinished) is written first, once, as a run writes it, and a world note
        written here is taken back when no prompt comes out (the picture is then held back, not visible or dropped). Appearance
        notes written on the way stay: they are the entity's, at their own anchors."""
        job = art_job(*args)                     # not visible even before a world note holds it: no call at all
        h, fin = ctx["hash"], args[-1]
        before = arts.manifest(h, slug_)
        try:
            il.world(len(il.books) - 1, finished=None if fin is None else set(fin))
            if arts.manifest(h, slug_) != before:
                job = art_job(*args)
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
        except ArtRefusal:
            untake_world(h, slug_, before)
            raise

    def untake_world(h: str, slug_: str, before: dict | None) -> None:
        """Put the set's world notes back as they were before this request; a set this request began and left with nothing
        else in it goes altogether."""
        m = arts.manifest(h, slug_)
        if m is None or all(m.get(k) == (before or {}).get(k) for k in WORLD_KEYS):
            return
        for k in WORLD_KEYS:
            if before is not None and k in before:
                m[k] = before[k]
            elif before is None and k == "world":
                m[k] = None
            else:
                m.pop(k, None)
        folder = arts.folder(h, slug_)
        if before is None and not m.get("pictures") and not m.get("appearance"):
            (folder / "art.json").unlink(missing_ok=True)
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        else:
            arts.save(h, slug_, m)

    def picture_json(h: str, slug_: str, rec: dict) -> dict:
        return {**rec, "url": f"/api/art/{h}/{slug_}/{rec['id']}"}

    def ref_json(il, refs, slug_: str) -> list[dict]:
        return [{"id": r["id"], "kind": r["kind"], "target": r["target"],
                 "url": f"/api/art/{artstore.text_hash(il.books[j].doc)}/{slug_}/{r['id']}"} for j, r in refs]

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
                    return art_error(e.status, e.code, e.detail, e.why)
                except llm.ProviderError as e:
                    return art_error(502, "model-failed", str(e)[:200])
            return await asyncio.get_running_loop().run_in_executor(None, work)
        return wrapped

    # The prompt route's Prompt, reused by draw when the job matches (an hour, the 32 latest), behind one lock.
    prompts: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
    prompts_lock = threading.Lock()

    def remember_prompt(k: tuple, pr) -> None:
        with prompts_lock:
            prompts[k] = (time.time(), pr)
            prompts.move_to_end(k)
            while len(prompts) > 32:
                prompts.popitem(last=False)

    def recall_prompt(k: tuple):
        with prompts_lock:
            hit = prompts.get(k)
        return copy.copy(hit[1]) if hit and time.time() - hit[0] < 3600 else None

    app.state.art_prompts = (remember_prompt, recall_prompt, prompts)   # for tests

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
        body["pictures"] = [picture_json(h, slug_, r) for r in body.get("pictures", [])] if m else []
        return {**body, "slug": slug_ if m else None, "styles": styles, "size": arts.size(h)}

    def chapter_events(ctx: dict, il) -> dict[str, int]:
        """Non-minor events per chapter title of the plan (the EPUB's chapters, a repeated title as "Title (2)")."""
        book, seen, out = art_epub(ctx), {}, {}
        ends = [c.start for c in book.chapters[1:]] + [len(book.blocks)]
        seq_of = {a["id"]: a.get("seq") for a in ctx["doc"]["anchors"]}
        for c, end in zip(book.chapters, ends):
            n = sum(1 for e in ctx["doc"].get("events", []) if e.get("kind") != "minor"
                    and seq_of.get(e["from"]) is not None and c.start <= seq_of[e["from"]] < end)
            seen[c.title] = seen.get(c.title, 0) + 1
            out[c.title if seen[c.title] == 1 else f"{c.title} ({seen[c.title]})"] = n
        return out

    @app.get("/api/art/estimate")
    @art_route
    def art_estimate(key: str, style: str = "ink", level: str = "main", portraits: str = "main",
                     provider: str | None = None, model: str | None = None, quality: str = "standard"):
        """How many pictures a run would draw and what they would cost, without a model call: the screened moments
        when the set has them, else the chapters' turning points (main) or non-minor events (all)."""
        ctx = art_index(key)
        if level not in artmod.LEVELS:
            raise ArtRefusal(422, "bad-config", "level must be main or all.")
        # main, all or none; true and false from an older page mean all and none
        cast = {"true": "all", "false": None, "none": None, "main": "main", "all": "all"}.get(portraits.lower(), "?")
        if cast == "?":
            raise ArtRefusal(422, "bad-config", "portraits must be main, all or none.")
        il, image, _ = illustrator(ctx, art_style(style), quality, provider, model, text_needed=False, portraits=cast)
        got = il.counts()
        # `drawable` is the screening's count, None before a chapter is screened; then a level "all" run draws up to the
        # chapter's non-minor events (read from the index, not from the pipeline's internals).
        events = chapter_events(ctx, il)
        chapters, plates = [], got["plates"]
        if level == "all":
            plates = sum(max(1, c["drawable"] if c["drawable"] is not None else events.get(c["title"], 0))
                         for c in got["chapters"])
        chapters = [{"title": c["title"], "drawable": c["drawable"], "screened": c["drawable"] is not None,
                     "events": events.get(c["title"], 0)} for c in got["chapters"]]
        ports = got["portraits"]
        prov = provider or getattr(image, "provider", None) or next(iter(llm.IMAGE_PROVIDERS))
        name = model or getattr(image, "name", None) or llm.IMAGE_PROVIDERS[prov]["default"]
        each = [llm.image_price(prov, name, quality, "landscape"), llm.image_price(prov, name, quality, "portrait")]
        price = None if (plates and each[0] is None) or (ports and each[1] is None) else \
            round(plates * (each[0] or 0) + ports * (each[1] or 0), 4)
        return {"pictures": plates + ports, "plates": plates, "portraits": ports, "price": price, "provider": prov,
                "model": name, "chapters": chapters}

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
                job = jobs.submit_art(key, body.get("art", body), index_path=str(ctx["item"]["path"]),
                                      book_id=(owner or {}).get("localId") or "", text_hash=ctx["hash"],
                                      title=ctx["doc"].get("work", {}).get("title") or key)
            except ValueError as e:
                raise ArtRefusal(422, "bad-config", str(e)) from e
            except RuntimeError as e:
                raise ArtRefusal(409, "busy", str(e)) from e
            return job_json(job)
        return await work()

    @app.post("/api/library/{key}/art/prompt")
    async def art_prompt(key: str, request: Request):
        """Write the prompt for one picture (one text call, nothing drawn): {prompt, anchor, references, price}."""
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
                il, image, slug_ = illustrator(ctx, style, quality, body.get("provider"), body.get("model"))
                given, at = art_position(ctx, body)
                args = (il, body.get("kind"), body.get("target"), given, at, body.get("shape"), fin)
                job, pr = prompt_for(il, ctx, slug_, args)
            # The price of the chosen provider and quality, also before its key is added.
            prov = body.get("provider") or getattr(image, "provider", None) or next(iter(llm.IMAGE_PROVIDERS))
            name = body.get("model") or getattr(image, "name", None) or llm.IMAGE_PROVIDERS[prov]["default"]
            out = {"prompt": pr.text, "scene": pr.scene, "anchor": pr.anchor, "shape": pr.shape, "kind": job.kind,
                   "target": job.target, "references": ref_json(il, pr.references, slug_), "provider": prov, "model": name,
                   "price": llm.image_price(prov, name, quality, pr.shape), "slug": slug_,
                   "inherits": list(pr.inherits), "partial": pr.partial, "finished": fin}
            remember_prompt((key, job.kind, job.target, slug_, job.shape, job.anchor, tuple(fin or ("*",))), pr)
            return out
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
            slug_ = arts.resolve(ctx["hash"], style)[0]
            with arts.lock(ctx["hash"], slug_):
                il, image, slug_ = illustrator(ctx, style, body.get("quality") or "standard", body.get("provider"),
                                               body.get("model"))
                if image is None:
                    raise ArtRefusal(409, "no-image-key", "Add a Gemini or OpenAI key in Settings to draw pictures.")
                given, at = art_position(ctx, body)
                args = (il, body.get("kind"), body.get("target"), given, at, body.get("shape"), fin)
                job = art_job(*args)
                pr = recall_prompt((key, job.kind, job.target, slug_, job.shape, job.anchor, tuple(fin or ("*",))))
                if pr is None:   # for its anchor and references (the person's text is drawn), with the prompt route's rollback
                    job, pr = prompt_for(il, ctx, slug_, args)
                rec = draw_given(il, job, pr, text_)
                entry = journal_art(key, [], {"op": "add", "textHash": ctx["hash"], "style": slug_, "added": [rec["id"]]},
                                    f"drew a picture ({rec['kind']})")
            return {**picture_json(ctx["hash"], slug_, rec), "undo": entry["id"]}
        return await work()

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

    @app.post("/api/library/{key}/art/{pid}/redraw")
    async def art_redraw(key: str, pid: str, request: Request):
        """Draw a picture again, with the person's note put first in its prompt; the new record `replaces` the old,
        which leaves the set and is kept for undo."""
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
                refs = [(j, r) for j in range(len(il.books)) for r in il.man[j]["pictures"]
                        if r["id"] in set(rec.get("references") or []) and r["id"] != pid]
                text_ = (note.rstrip(".") + ". " if note else "") + rec["prompt"]
                # The text is the old prompt's, so is the earlier-book content it carries: `inherits` (and `partial`) go
                # with it, or the Reader would show a prompt it had hidden until those books are finished. A record from
                # before `inherits` existed gets none on the new one either, so the Reader keeps treating it as unknown.
                pr = artmod.Prompt(text_, text_, rec["from"], rec.get("shape") or job.shape,
                                   [(rec["target"], rec["appearance"])] if rec.get("appearance") else [], refs,
                                   list(rec.get("sources") or [rec["from"]]),
                                   inherits=[k for k in rec.get("inherits") or [] if isinstance(k, str)],
                                   partial=bool(rec.get("partial")))
                new = draw_given(il, job, pr, text_)
                if "inherits" not in rec:
                    new.pop("inherits", None)
                old = arts.path(ctx["hash"], slug_, pid)
                entry = journal_art(key, [old] if old and old.name != new["file"] else [],
                                    {"op": "redraw", "textHash": ctx["hash"], "style": slug_, "removed": [rec],
                                     "added": [new["id"]]}, f"drew a picture again ({rec['kind']})")
                try:
                    man = arts.manifest(ctx["hash"], slug_)
                    for r in man["pictures"]:
                        if r["id"] == new["id"]:
                            r["replaces"] = pid
                            if "inherits" not in rec:
                                r.pop("inherits", None)
                    man["pictures"] = [r for r in man["pictures"] if r["id"] != pid or r["id"] == new["id"]]
                    man["cost"] = round(sum(r.get("cost") or 0 for r in man["pictures"]), 6)
                    arts.save(ctx["hash"], slug_, man)
                    if old is not None and new["file"] != old.name:
                        old.unlink(missing_ok=True)
                except Exception:
                    identity.discard(entry["id"])
                    raise
            return {**picture_json(ctx["hash"], slug_, {**new, "replaces": pid}), "undo": entry["id"]}
        return await work()

    @app.put("/api/library/{key}/art/world")
    async def art_world(key: str, request: Request):
        """The person's own world note for a style's set (`edited: true`: never rewritten by a run)."""
        body = await json_body(request)
        try:
            ctx = art_index(key)
            style = art_style(body.get("style"))
            text_ = " ".join(str(body.get("text") or "").split())
            if not text_ or len(text_) > artstore.MAX_WORLD:
                raise ArtRefusal(422, "bad-text", f"Write the world note in 1 to {artstore.MAX_WORLD} characters.")
            art_busy(ctx["hash"])
            slug_, style = arts.resolve(ctx["hash"], style)
            with arts.lock(ctx["hash"], slug_):
                m = arts.manifest(ctx["hash"], slug_)
                if m is None:   # a new set; an existing one is never replaced by an empty manifest
                    m = artmod.blank(next((e.get("textHash") for e in ctx["doc"].get("editions", []) if e.get("builtFrom")),
                                          ctx["hash"]), style)
                entry = journal_art(key, [], {"op": "world", "textHash": ctx["hash"], "style": slug_, "before": m.get("world")},
                                    "changed the world note")
                # The person's words come from no passage, so nothing is held back for them: the book's first anchor.
                m["world"] = {"text": text_, "edited": True, "from": ctx["doc"]["anchors"][0]["id"]}
                try:
                    arts.save(ctx["hash"], slug_, m)
                except Exception:
                    identity.discard(entry["id"])
                    raise
        except ArtRefusal as e:
            return art_error(e.status, e.code, e.detail)
        return {"world": m["world"], "undo": entry["id"]}

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
        elif jobs.drawing(last["art"]["textHash"]):
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
