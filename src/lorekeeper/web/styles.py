"""Picture styles: the nine listed ones (`art.STYLES`, each with a preview shipped in `static/styles/<slug>.webp`) and the
ones a person saves, one list per server.

Saved styles live in `lorekeeper-data/styles.json` (`{"styles": [{slug, name, text, created, updated, preview}],
"retired": {slug: text}}`) and their previews in `lorekeeper-data/styles/<slug>.<ext>`. A saved style is passed around by
its slug, as a listed one is: `art.SAVED_STYLES` (kept in step here) gives `art.style_sentence` its words, so a job config,
the picture sheet and the art store's `resolve` need nothing else. A deleted style's words stay in `retired`, never listed,
so the art sets drawn in it keep drawing in its words. A preview is the shared scene, the style's stored words as written and the
no-text line (`art.preview_prompt`'s parts), drawn square; it is stale once the style's words change.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import art, images, llm

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MAX_NAME = 40
MAX_TEXT = 300
NAMES = {"digital-art": "Digital art", "comic-strip": "Comic strip", "anime": "Anime", "realism": "Realism",
         "hand-drawn": "Hand-drawn", "oil-painting": "Oil painting", "epic-sci-fi": "Epic sci-fi",
         "watercolour": "Watercolour", "ink": "Ink"}
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MIME = {v: k for k, v in EXT.items()}
QUALITIES = ("draft", "standard", "best")
STATIC_STYLES = Path(__file__).parent / "static" / "styles"
def preview_prompt(text: str) -> str:
    """The preview prompt of a saved style: its stored words as written, never looked up, so a description that reads like
    a listed slug ("ink") is still the person's own words. No book, so no names are replaced."""
    return art.Illustrator._assemble(art.PREVIEW_SCENE, style=" ".join(text.split()))


class StyleError(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def clean(value, most: int, what: str) -> str:
    text = " ".join(str(value or "").split()) if isinstance(value, str) else ""
    if not text or len(text) > most:
        raise StyleError(422, "bad-style", f"Write the {what} in 1 to {most} characters.")
    return text


class StyleStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "styles.json"
        self.dir = data_dir / "styles"
        self._lock = threading.Lock()
        self._sync(self._read())

    # ---- the file

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        rows = [s for s in data.get("styles") or [] if isinstance(s, dict) and SLUG.fullmatch(str(s.get("slug") or ""))
                and s["slug"] not in art.STYLES and isinstance(s.get("text"), str) and s["text"]]
        retired = {k: v for k, v in (data.get("retired") or {}).items()
                   if isinstance(k, str) and SLUG.fullmatch(k) and isinstance(v, str) and v}
        return {"styles": rows, "retired": retired}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"styles.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        self._sync(data)

    @staticmethod
    def _sync(data: dict) -> None:
        """Give `art.style_sentence` the saved styles' words (a deleted one's too, for its art sets)."""
        words = {**data["retired"], **{s["slug"]: s["text"] for s in data["styles"]}}
        art.SAVED_STYLES.clear()
        art.SAVED_STYLES.update(words)

    # ---- reading

    def saved(self, slug: str) -> dict | None:
        return next((s for s in self._read()["styles"] if s["slug"] == slug), None)

    def public(self, s: dict) -> dict:
        pv = s.get("preview") if isinstance(s.get("preview"), dict) else None
        url = f"/api/styles/{s['slug']}/preview?v={pv['created']}" if pv and self.preview_path(s["slug"]) else None
        return {"slug": s["slug"], "name": s.get("name") or s["slug"], "text": s["text"], "builtin": False,
                "preview": url, "stale": bool(url and pv.get("text") != s["text"]), "created": s.get("created")}

    def all(self) -> list[dict]:
        """Every style the picker offers: the listed ones first, in `art.STYLES` order, then the saved ones oldest first."""
        out = [{"slug": k, "name": NAMES.get(k, k.replace("-", " ").capitalize()), "text": v, "builtin": True,
                "preview": f"/static/styles/{k}.webp" if (STATIC_STYLES / f"{k}.webp").is_file() else None,
                "stale": False} for k, v in art.STYLES.items()]
        return out + [self.public(s) for s in self._read()["styles"]]

    def preview_path(self, slug: str) -> Path | None:
        s = self.saved(slug) if SLUG.fullmatch(slug or "") else None
        pv = (s or {}).get("preview")
        if not isinstance(pv, dict) or pv.get("ext") not in MIME:
            return None
        p = self.dir / f"{slug}.{pv['ext']}"
        return p if p.is_file() else None

    # ---- changes

    def create(self, name, text) -> dict:
        name, text = clean(name, MAX_NAME, "name"), clean(text, MAX_TEXT, "description")
        slug = art.style_slug(name)
        with self._lock:
            data = self._read()
            if slug in art.STYLES or any(s["slug"] == slug for s in data["styles"]):
                raise StyleError(409, "exists", "There is a style with that name already. Choose another name.")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            row = {"slug": slug, "name": name, "text": text, "created": now, "updated": now, "preview": None}
            data["styles"].append(row)
            data["retired"].pop(slug, None)
            self._write(data)
        return self.public(row)

    def update(self, slug: str, name=None, text=None) -> dict:
        """Rename it or change its words; the slug (what configs and art sets name) never changes, and a preview of other
        words is stale."""
        name = None if name is None else clean(name, MAX_NAME, "name")
        text = None if text is None else clean(text, MAX_TEXT, "description")
        with self._lock:
            data = self._read()
            row = self._row(data, slug)
            if name is not None:
                row["name"] = name
            if text is not None:
                row["text"] = text
            row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write(data)
        return self.public(row)

    def delete(self, slug: str) -> None:
        with self._lock:
            data = self._read()
            row = self._row(data, slug)
            data["styles"].remove(row)
            data["retired"][slug] = row["text"]     # its art sets stay and keep drawing in its words
            self._write(data)
            for f in self.dir.glob(f"{slug}.*") if self.dir.is_dir() else ():
                if f.suffix[1:] in MIME:
                    f.unlink(missing_ok=True)

    def set_preview(self, slug: str, data: bytes, mime: str, text: str, meta: dict) -> dict:
        """File a drawn preview of the style as it read when it was drawn (`text`), replacing the old one."""
        ext = EXT.get(mime)
        if ext is None:
            raise StyleError(502, "model-failed", "The image provider sent a picture in a format this server can't keep.")
        with self._lock:
            store = self._read()
            row = self._row(store, slug)
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / f".{slug}.{uuid.uuid4().hex}.tmp"
            tmp.write_bytes(data)
            tmp.replace(self.dir / f"{slug}.{ext}")
            for f in self.dir.glob(f"{slug}.*"):
                if f.suffix[1:] in MIME and f.suffix[1:] != ext:
                    f.unlink(missing_ok=True)
            row["preview"] = {"ext": ext, "text": text, "created": int(time.time() * 1000), **meta}
            self._write(store)
        return self.public(row)

    @staticmethod
    def _row(data: dict, slug: str) -> dict:
        if slug in art.STYLES:
            raise StyleError(409, "builtin", "The listed styles can't be changed.")
        row = next((s for s in data["styles"] if s["slug"] == slug), None)
        if row is None:
            raise StyleError(404, "no-style", "There is no such style.")
        return row


def router(store: StyleStore, image_model: Callable) -> APIRouter:
    """The /api/styles routes. Mutating ones sit behind the app's CSRF header and sign-in like every /api route."""
    r = APIRouter()
    drawing: set[str] = set()
    drawing_lock = threading.Lock()

    def err(e: StyleError) -> JSONResponse:
        return JSONResponse({"detail": e.detail, "code": e.code}, status_code=e.status)

    async def body_of(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise StyleError(400, "bad-body", "expected a JSON object")
        return body

    @r.get("/api/styles")
    def styles_list():
        return {"styles": store.all()}

    @r.post("/api/styles")
    async def styles_create(request: Request):
        try:
            body = await body_of(request)
            return store.create(body.get("name"), body.get("text"))
        except StyleError as e:
            return err(e)

    @r.put("/api/styles/{slug}")
    async def styles_update(slug: str, request: Request):
        try:
            body = await body_of(request)
            return store.update(slug, body.get("name"), body.get("text"))
        except StyleError as e:
            return err(e)

    @r.delete("/api/styles/{slug}")
    def styles_delete(slug: str):
        try:
            store.delete(slug)
        except StyleError as e:
            return err(e)
        return {"deleted": slug}

    @r.get("/api/styles/{slug}/preview")
    def styles_preview_file(slug: str):
        p = store.preview_path(slug)
        if p is None:
            return JSONResponse({"detail": "no preview"}, status_code=404)
        return FileResponse(p, media_type=MIME[p.suffix[1:]], headers={"Cache-Control": "no-cache"})

    @r.post("/api/styles/{slug}/preview")
    async def styles_preview_draw(slug: str, request: Request):
        """Draw the shared preview scene in a saved style with the image provider (the first set up, else `provider`, and
        `model`, `quality`), priced first; it replaces the old preview. A refusal is 422 `refused` with the reason."""
        try:
            body = await body_of(request)
        except StyleError as e:
            return err(e)

        def work():
            row = store.saved(slug) if SLUG.fullmatch(slug) else None
            if row is None:
                raise StyleError(409, "builtin", "The listed styles come with their previews.") if slug in art.STYLES \
                    else StyleError(404, "no-style", "There is no such style.")
            provider, model, quality = body.get("provider"), body.get("model"), body.get("quality") or "standard"
            if provider is not None and provider not in llm.IMAGE_PROVIDERS:
                raise StyleError(422, "bad-provider", f"No image provider {provider!r}.")
            if model is not None and (not isinstance(model, str) or (provider and not llm.image_model(provider, model))):
                raise StyleError(422, "bad-model", f"Unknown image model {model!r}.")
            if quality not in QUALITIES:
                raise StyleError(422, "bad-config", "quality must be draft, standard or best.")
            try:
                image = image_model(provider, model)
            except ValueError as e:
                raise StyleError(422, "bad-model", str(e)) from e
            if image is None:
                raise StyleError(409, "no-image-key", "Add a Gemini or OpenAI key in Settings to draw pictures.")
            prov, name = getattr(image, "provider", provider) or provider, getattr(image, "name", model) or model
            q = llm.nearest_quality(prov, name, quality) if prov and name else quality
            shape = art.PREVIEW_SHAPE
            price = llm.image_price(prov, name, q, shape) if prov and name else None
            with drawing_lock:
                if slug in drawing:
                    raise StyleError(409, "busy", "A preview of this style is being drawn.")
                drawing.add(slug)
            try:
                text = row["text"]
                try:
                    got = image.draw(preview_prompt(text), shape=shape, quality=q)
                except images.ImageRefused as e:
                    raise StyleError(422, "refused", f"refused: {e.reason[:160]}") from e
                except llm.ProviderError as e:
                    raise StyleError(502, "model-failed", str(e)[:200]) from e
                out = store.set_preview(slug, got.data, got.mime, text,
                                        {"provider": prov, "model": got.model or name, "quality": q,
                                         "cost": got.cost, "shape": shape})
            finally:
                with drawing_lock:
                    drawing.discard(slug)
            return {**out, "price": price, "cost": got.cost, "provider": prov, "model": got.model or name, "quality": q}

        try:
            return await asyncio.get_running_loop().run_in_executor(None, work)
        except StyleError as e:
            return err(e)

    return r

