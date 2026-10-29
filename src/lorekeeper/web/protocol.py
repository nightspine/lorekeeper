"""The public read-only HTTP protocol (SPEC.md §8), served under /v0 and /.well-known/lorekeeper.

Unlike the UI's /api, these routes are meant for other programs: reader apps, plugins, other
servers. They are all GET, return JSON (errors as RFC 9457 problem+json), allow any origin
(CORS), and can be put behind a bearer token (env LOREKEEPER_API_TOKEN).

Indexes are addressed by their library key (stable on one server). Positions come either as an
anchor id of the index or as a locator (SPEC §3.8) into an edition the server holds; a locator
is mapped to a block with `lorekeeper.locators`, then to an anchor, then gated with
`gating.view`, so the answer equals what a client would compute itself.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import epub, gating

PROBLEM = "application/problem+json"
PROBLEM_BASE = "https://lorekeeper.dev/problems/"
CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization, If-None-Match",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Expose-Headers": "ETag",
        "Access-Control-Max-Age": "600"}
CACHE_CONTROL = "private, max-age=0, must-revalidate"
RANK = {"file": 0, "text": 1, "identifier": 2}
IDENTIFIERS = ("gutenberg", "openlibrary", "wikidata")


class Problem(Exception):
    def __init__(self, status: int, slug: str, title: str, detail: str | None = None,
                 headers: dict | None = None) -> None:
        super().__init__(title)
        self.status, self.slug, self.title, self.detail, self.headers = status, slug, title, detail, headers or {}

    def response(self) -> JSONResponse:
        body = {"type": PROBLEM_BASE + self.slug, "title": self.title, "status": self.status}
        if self.detail:
            body["detail"] = self.detail
        return JSONResponse(body, status_code=self.status, media_type=PROBLEM, headers=self.headers)


def _problems(*codes: int) -> dict:
    titles = {400: "Bad request: a parameter is missing, malformed or contradicts another.",
              401: "A bearer token is required.",
              404: "No such index or series.",
              409: "The server does not hold the reader's edition, so it cannot map a locator."}
    return {c: {"description": titles[c],
                "content": {PROBLEM: {"example": {"type": PROBLEM_BASE + "example", "title": titles[c].split(":")[0],
                                                  "status": c, "detail": "…"}}}} for c in codes}


def has_locators() -> bool:
    return importlib.util.find_spec("lorekeeper.locators") is not None


def api_token(configured: str | None = None) -> str | None:
    """The token passed to `install` (the `--api-token` flag) wins; the environment is the fallback."""
    return configured or os.environ.get("LOREKEEPER_API_TOKEN") or None


class Position:
    """Position query parameters shared by the view and entities routes (SPEC §8.4)."""

    def __init__(
        self,
        anchor: Annotated[str | None, Query(description="An anchor id of the index. Use this or one locator form.")] = None,
        block: Annotated[str | None, Query(description="Locator: block seq in the reader's edition.")] = None,
        cfi: Annotated[str | None, Query(description="Locator: EPUB CFI, e.g. epubcfi(/6/22!/4/108/1:0).")] = None,
        xpointer: Annotated[str | None, Query(description="Locator: CREngine XPointer.")] = None,
        spine: Annotated[str | None, Query(description="Locator: 0-based linear spine index, with word or progress.")] = None,
        word: Annotated[str | None, Query(description="Locator: 0-based word within the spine document.")] = None,
        progress: Annotated[str | None, Query(description="Locator: fraction 0..1 of the spine document (approximate).")] = None,
        textHash: Annotated[str | None, Query(description="Text hash of the reader's edition (required with a locator, unless fileHash).")] = None,
        fileHash: Annotated[str | None, Query(description="File hash of the reader's edition (required with a locator, unless textHash).")] = None,
        inclusive: Annotated[str | None, Query(description="true | false. Default false for view, true for entities.")] = None,
        order: Annotated[str | None, Query(description="Reading order key of the series.")] = None,
        finished: Annotated[str | None, Query(description="Comma-separated book keys the reader has finished.")] = None,
    ) -> None:
        self.anchor, self.block, self.cfi, self.xpointer = anchor, block, cfi, xpointer
        self.spine, self.word, self.progress = spine, word, progress
        self.textHash, self.fileHash, self.inclusive = textHash, fileHash, inclusive
        self.order, self.finished = order, finished


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _bad(detail: str) -> Problem:
    return Problem(400, "bad-request", "Bad request", detail)


def _int(name: str, value: str, low: int = 0) -> int:
    try:
        n = int(value)
    except ValueError:
        raise _bad(f"{name} must be an integer") from None
    if n < low:
        raise _bad(f"{name} must be at least {low}")
    return n


def parse_locator(p: Position) -> tuple[str | None, dict | None]:
    """(anchor, None) or (None, locator dict); anything else is a 400."""
    forms = []
    if p.anchor is not None:
        forms.append("anchor")
    for name in ("block", "cfi", "xpointer"):
        if getattr(p, name) is not None:
            forms.append(name)
    if p.spine is not None or p.word is not None or p.progress is not None:
        forms.append("spine")
    if not forms:
        raise _bad("give a position: anchor, block, cfi, xpointer, spine with word, or spine with progress")
    if len(forms) > 1:
        raise _bad(f"give exactly one position, not {', '.join(forms)}")
    form = forms[0]
    if form == "anchor":
        return p.anchor, None
    if not (p.textHash or p.fileHash):
        raise _bad("a locator needs textHash or fileHash, naming the reader's edition")
    if form == "block":
        return None, {"block": _int("block", p.block)}
    if form == "cfi":
        if not (p.cfi.startswith("epubcfi(") and p.cfi.endswith(")") and len(p.cfi) > 9):
            raise _bad("cfi must look like epubcfi(…)")
        return None, {"cfi": p.cfi}
    if form == "xpointer":
        if not p.xpointer.startswith("/"):
            raise _bad("xpointer must start with /")
        return None, {"xpointer": p.xpointer}
    if p.spine is None:
        raise _bad("word and progress need spine")
    if (p.word is None) == (p.progress is None):
        raise _bad("spine needs exactly one of word or progress")
    spine = _int("spine", p.spine)
    if p.word is not None:
        return None, {"spine": spine, "word": _int("word", p.word)}
    try:
        frac = float(p.progress)
    except ValueError:
        raise _bad("progress must be a number") from None
    if not 0 <= frac <= 1:
        raise _bad("progress must be between 0 and 1")
    return None, {"spine": spine, "progress": frac}


def _flag(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    if value.lower() in ("true", "1"):
        return True
    if value.lower() in ("false", "0"):
        return False
    raise _bad("inclusive must be true or false")


def _etag_matches(header: str | None, digest: str) -> bool:
    if not header:
        return False
    for tag in header.split(","):
        tag = tag.strip()
        if tag == "*" or tag.removeprefix("W/").strip('"') == digest:
            return True
    return False


def build_router(library, parsed: Callable[[Path], epub.Book], token: str | None = None) -> APIRouter:
    router = APIRouter(tags=["Protocol"])
    file_hashes: dict[Path, tuple[float, int, str]] = {}      # epub → (mtime, size, sha256)
    digests: dict[Path, tuple[float, int, str]] = {}          # index file → (mtime, size, digest)
    resolved_cache: dict[tuple, dict] = {}

    def digest_of(path: Path) -> str:
        st = path.stat()
        c = digests.get(path)
        if c is None or c[:2] != (st.st_mtime, st.st_size):
            c = digests[path] = (st.st_mtime, st.st_size, _digest(path.read_bytes()))
        return c[2]

    def file_hash(path: Path) -> str:
        st = path.stat()
        c = file_hashes.get(path)
        if c is None or c[:2] != (st.st_mtime, st.st_size):
            c = file_hashes[path] = (st.st_mtime, st.st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        return c[2]

    def edition_by_file(fh: str) -> Path | None:
        for d in library.epub_dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*.epub")):
                try:
                    if file_hash(p) == fh:
                        return p
                except OSError:
                    continue
        return None

    def held_edition(text_hash: str | None, fh: str | None) -> Path:
        path = library.edition_path(text_hash) if text_hash else None
        if path is None and fh:
            path = edition_by_file(fh)
        if path is None:
            raise Problem(409, "edition-not-held", "Edition not held",
                          "This server has no copy of the reader's edition, so it cannot map the locator. "
                          "Fingerprint the anchors locally and gate on the device instead (SPEC §3.6, §7).")
        return path

    def item_for(id: str, kind: str, scan: dict | None = None) -> dict:
        scan = scan if scan is not None else library.scan()
        if kind == "index":
            item = scan.get(id)
            if item is not None and item["doc"].get("kind") == "index":
                return item
        else:
            hit = [i for i in scan.values() if i["doc"].get("kind") == "series" and i["doc"].get("id") == id]
            if hit:
                return hit[0]
        raise Problem(404, "not-found", f"No such {kind}", id)

    def locate_anchor(item: dict, p: Position, default_inclusive: bool) -> tuple[str | None, dict | None]:
        """The anchor id the request is gated at, and the located block when a locator was used."""
        anchor, locator = parse_locator(p)
        inclusive = _flag(p.inclusive, default_inclusive)
        anchors = item["doc"]["anchors"]
        if locator is None:
            if anchor not in {a["id"] for a in anchors}:
                raise _bad(f"unknown anchor {anchor!r}")
            return anchor, None
        if not has_locators():
            raise Problem(501, "not-implemented", "Locators unavailable", "this server cannot map locators yet")
        from .. import locators
        path = held_edition(p.textHash, p.fileHash)
        book = parsed(path)
        try:
            located = locators.locate(book, locator)
        except locators.LocatorError as e:
            raise _bad(str(e)) from e
        key = (str(item["path"]), item["path"].stat().st_mtime, str(path), path.stat().st_mtime)
        if key not in resolved_cache:
            if len(resolved_cache) >= 8:
                resolved_cache.pop(next(iter(resolved_cache)))
            resolved_cache[key] = locators.resolve_edition(item["doc"], book)
        anchor = locators.position(anchors, resolved_cache[key], located.seq, inclusive=inclusive)
        return anchor, {"seq": located.seq, "basis": located.basis, "exact": bool(located.exact)}

    def gated_view(item: dict, anchor: str | None, p: Position) -> dict:
        doc = item["doc"]
        scan = library.scan()
        sref = doc.get("series")
        manifest = None
        if sref:
            mans = [i for i in scan.values() if i["doc"].get("kind") == "series" and i["doc"].get("id") == sref["id"]]
            manifest = next((m for m in mans if m["source"] == item["source"]), mans[0] if mans else None)
        indexes, series = [doc], None
        if manifest:
            series = manifest["doc"]
            for b in series["books"]:
                if b["key"] == sref["book"]:
                    continue
                cands = [i for i in scan.values() if i["doc"].get("kind") == "index"
                         and (i["doc"].get("series") or {}).get("id") == sref["id"]
                         and i["doc"]["series"]["book"] == b["key"]]
                choice = next((c for c in cands if c["source"] == item["source"]), cands[0] if cands else None)
                if choice:
                    indexes.append(choice["doc"])
            if p.order is not None and p.order not in series["orders"]:
                raise _bad(f"unknown reading order {p.order!r}")
        fin = None if p.finished is None else {k for k in p.finished.split(",") if k}
        try:
            return gating.view(indexes, gating.Position(sref["book"] if manifest else None, anchor),
                               series, p.order, fin)
        except ValueError as e:
            raise _bad(str(e)) from e

    # ---- discovery --------------------------------------------------------------------------

    @router.get("/.well-known/lorekeeper", summary="Discovery",
                responses={200: {"description": "What this server offers.", "content": {"application/json": {"example": {
                    "lorekeeper": "0.1", "name": "Lorekeeper", "api": "/v0",
                    "capabilities": ["lookup", "index", "series", "view", "entities"], "auth": {"type": "none"}}}}}})
    def discovery():
        """Always public. `auth.type` is `bearer` when the server was started with `LOREKEEPER_API_TOKEN`.
        `view-cfi` means `cfi` and `xpointer` locators are accepted, because this server holds editions."""
        caps = ["lookup", "index", "series", "view", "view-cfi", "entities"]
        return {"lorekeeper": "0.1", "name": os.environ.get("LOREKEEPER_SERVER_NAME") or "Lorekeeper",
                "api": "/v0", "capabilities": caps, "auth": {"type": "bearer" if api_token(token) else "none"}}

    # ---- lookup -----------------------------------------------------------------------------

    @router.get("/v0/lookup", summary="Find indexes for a book", responses={
        200: {"description": "Results, best match first.", "content": {"application/json": {"example": {"results": [{
            "id": "examples--sherlock-holmes--study-in-scarlet", "title": "A Study in Scarlet", "match": "file",
            "series": {"id": "sherlock-holmes", "book": "study-in-scarlet"}, "complete": False,
            "digest": "sha256:" + "0" * 64, "href": "/v0/indexes/examples--sherlock-holmes--study-in-scarlet"}]}}}},
        **_problems(401, 400)})
    def lookup(
        fileHash: Annotated[str | None, Query(description="sha256 of the EPUB file")] = None,
        textHash: Annotated[str | None, Query(description="text hash of the edition (SPEC §3.4)")] = None,
        isbn: Annotated[str | None, Query(description="ISBN-10 or -13, hyphens allowed")] = None,
        gutenberg: Annotated[str | None, Query(description="Project Gutenberg ebook number")] = None,
        openlibrary: Annotated[str | None, Query(description="Open Library work id")] = None,
        wikidata: Annotated[str | None, Query(description="Wikidata item id")] = None,
    ):
        """At least one parameter. `match` is `file` (same file), `text` (same text) or `identifier`
        (maybe another edition, so anchors resolve by fingerprint only)."""
        ids = {"gutenberg": gutenberg, "openlibrary": openlibrary, "wikidata": wikidata}
        if not (fileHash or textHash or isbn or any(ids.values())):
            raise _bad("give at least one of fileHash, textHash, isbn, gutenberg, openlibrary, wikidata")
        want_isbn = _isbn(isbn) if isbn else None
        results = []
        for item in library.scan().values():
            doc = item["doc"]
            if doc.get("kind") != "index":
                continue
            editions = doc.get("editions", [])
            work = doc.get("work", {})
            best = None

            def hit(kind: str) -> None:
                nonlocal best
                if best is None or RANK[kind] < RANK[best]:
                    best = kind
            if fileHash and any(e.get("fileHash") == fileHash for e in editions):
                hit("file")
            if textHash and any(e.get("textHash") == textHash for e in editions):
                hit("text")
            wids = {**work.get("ids", {})}
            for e in editions:
                for k, v in (e.get("ids") or {}).items():
                    wids.setdefault(k, v)
            for k, v in ids.items():
                if v and str(wids.get(k, "")).lower() == v.lower():
                    hit("identifier")
            if want_isbn:
                have = {_isbn(x) for x in [*work.get("isbns", []), wids.get("isbn", "")] if x}
                if want_isbn in have:
                    hit("identifier")
            if best is None:
                continue
            prov = doc.get("provenance", {})
            r = {"id": item["key"], "title": work.get("title") or doc.get("id") or item["key"], "match": best,
                 "complete": bool(doc.get("coverage", {}).get("complete")),
                 "digest": digest_of(item["path"]), "href": f"/v0/indexes/{item['key']}"}
            if doc.get("series"):
                r["series"] = {"id": doc["series"]["id"], "book": doc["series"]["book"]}
            if isinstance(prov.get("updated") or prov.get("created"), str):
                r["updated"] = prov.get("updated") or prov.get("created")
            results.append(r)
        results.sort(key=lambda r: (RANK[r["match"]], not r["complete"], r["id"]))
        return {"results": results}

    # ---- fetch ------------------------------------------------------------------------------

    def serve(request: Request, item: dict) -> Response:
        digest = digest_of(item["path"])
        headers = {"ETag": f'"{digest}"', "Cache-Control": CACHE_CONTROL}
        if _etag_matches(request.headers.get("if-none-match"), digest):
            return Response(status_code=304, headers=headers)
        return Response(item["path"].read_bytes(), media_type="application/json", headers=headers)

    @router.get("/v0/indexes/{id}", summary="Fetch an index document", responses={
        200: {"description": "The index (schema/index.schema.json). `ETag` is the digest from lookup."},
        304: {"description": "Unchanged since `If-None-Match`."}, **_problems(401, 404)})
    def get_index(id: str, request: Request):
        """Clients should cache indexes by `ETag` and gate locally (SPEC §7)."""
        return serve(request, item_for(id, "index"))

    @router.get("/v0/series/{id}", summary="Fetch a series manifest", responses={
        200: {"description": "The series manifest (schema/series.schema.json)."},
        304: {"description": "Unchanged since `If-None-Match`."}, **_problems(401, 404)})
    def get_series(id: str, request: Request):
        """`id` is the series id (for example `sherlock-holmes`), as in an index's `series.id`."""
        return serve(request, item_for(id, "series"))

    # ---- view and entities ------------------------------------------------------------------

    @router.get("/v0/indexes/{id}/view", summary="Gated view at a position", responses={
        200: {"description": "A view (schema/view.schema.json). When a locator was used it also carries "
                             "`locator`: {position, seq, basis, exact}."},
        **_problems(401, 400, 404, 409)})
    def get_view(id: str, p: Position = Depends()):
        """Exactly one position: `anchor`, or one locator form (`block`, `cfi`, `xpointer`,
        `spine`+`word`, `spine`+`progress`) with `textHash` or `fileHash`. `inclusive` defaults to false.
        Sends the reading position to the server."""
        item = item_for(id, "index")
        anchor, located = locate_anchor(item, p, default_inclusive=False)
        body = gated_view(item, anchor, p)
        if located is not None:
            body["locator"] = {"position": anchor, **located}
        return body

    @router.get("/v0/indexes/{id}/entities", summary="Who is this name?", responses={
        200: {"description": "Matching entities, as the view would render them, with their relations and events.",
              "content": {"application/json": {"example": {"position": "ch2.para14", "entities": []}}}},
        **_problems(401, 400, 404, 409)})
    def get_entities(id: str, name: Annotated[str | None, Query(description="A visible name or alias, or a unique surname.")] = None,
                     p: Position = Depends()):
        """Matches a visible name or alias case-insensitively, or a surname no other visible entity shares.
        `inclusive` defaults to true because the reader is looking at the name."""
        if not name or not name.strip():
            raise _bad("name is required")
        item = item_for(id, "index")
        anchor, located = locate_anchor(item, p, default_inclusive=True)
        view = gated_view(item, anchor, p)
        q = " ".join(name.split()).casefold()
        surname_owners: dict[str, int] = {}
        for e in view["entities"]:
            parts = e["name"].split()
            if len(parts) >= 2:
                surname_owners[parts[-1].casefold()] = surname_owners.get(parts[-1].casefold(), 0) + 1
        out = []
        for e in view["entities"]:
            names = [" ".join(n.split()).casefold() for n in [e["name"], *e["aliases"]]]
            parts = e["name"].split()
            surname = (len(q.split()) == 1 and len(parts) >= 2 and parts[-1].casefold() == q
                       and surname_owners.get(q) == 1)
            if q in names or surname:
                out.append({**e,
                            "relations": [r for r in view["relations"] if e["id"] in (r["src"], r["dst"])],
                            "events": [v for v in view["events"] if e["id"] in v["entities"]]})
        body = {"position": anchor, "entities": out}
        if located is not None:
            body["locator"] = {"position": anchor, **located}
        return body

    return router


def _isbn(value: str) -> str:
    return "".join(c for c in str(value) if c.isalnum()).upper()


def install(app: FastAPI, library, parsed: Callable[[Path], epub.Book], api_token_value: str | None = None) -> None:
    """Mount the protocol: routes, problem+json errors, CORS and the optional bearer token."""
    app.include_router(build_router(library, parsed, api_token_value))

    @app.exception_handler(Problem)
    async def problem_handler(request: Request, exc: Problem):
        return exc.response()

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/v0"):
            slug = {404: "not-found", 405: "method-not-allowed"}.get(exc.status_code, "error")
            return Problem(exc.status_code, slug, str(exc.detail) if exc.status_code != 404 else "Not found").response()
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.middleware("http")
    async def protocol_guard(request: Request, call_next):
        path = request.url.path
        discovery = path == "/.well-known/lorekeeper"
        if not (discovery or path == "/v0" or path.startswith("/v0/")):
            return await call_next(request)
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=CORS)
        token = api_token(api_token_value)
        if token and not discovery:
            supplied = request.headers.get("authorization", "")
            scheme, _, value = supplied.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip().encode(), token.encode()):
                resp = Problem(401, "unauthorized", "Unauthorized", "send Authorization: Bearer <token>").response()
                resp.headers["WWW-Authenticate"] = 'Bearer realm="lorekeeper"'
                resp.headers.update(CORS)
                return resp
        resp = await call_next(request)
        resp.headers.update(CORS)
        return resp
