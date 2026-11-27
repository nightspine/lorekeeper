"""Picture styles: the nine listed ones (`art.STYLES`, each with a preview shipped in `static/styles/<slug>.webp`) and the
ones a person saves, one list per server.

Saved styles live in `lorekeeper-data/styles.json` (`{"styles": [{slug, name, text, created, updated, preview}],
"retired": {slug: text}}`) and their previews in `lorekeeper-data/styles/<slug>.<ext>`. A saved style is passed around by
its slug, as a listed one is: `art.SAVED_STYLES` (kept in step here) gives `art.style_sentence` its words, so a job config,
the picture sheet and the art store's `resolve` need nothing else. A deleted style's words stay in `retired`, never listed,
so the art sets drawn in it keep drawing in its words. A preview is the shared scene, the style's stored words as written and the
no-text line (`art.preview_prompt`'s parts), drawn square; it is stale once the style's words change.

A saved style may carry one reference picture the person uploaded, `lorekeeper-data/styles/<slug>.ref.<ext>`, described in
its row as `reference: {ext, sha, bytes, width, height, created}` with `useReference`. Where `useReference` is true the
picture goes to the image model as the last reference of every picture drawn in that style (`art.SAVED_STYLE_REFS`, kept
in step here like `SAVED_STYLES`), the preview included. A deleted style keeps its picture in `retired_refs`, so its sets
keep drawing as they did; creating the name again takes it back. The picture is checked by its own bytes only (PNG, JPEG
or WebP magic bytes, at most `MAX_REF_BYTES`, each side `MIN_SIDE` to `MAX_SIDE` read from the header), never by its name
or Content-Type. `POST /api/styles/describe` asks a text model that sees pictures for the Description sentence, storing
nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .. import art, images, llm

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
MAX_NAME = 40
# slugs the picture sheet uses for itself ("custom" is its free-text style), so a saved style may never take one; a name
# with no letters or digits slugs to "custom" too
RESERVED = frozenset({"custom"})
MAX_TEXT = 300
NAMES = {"digital-art": "Digital art", "comic-strip": "Comic strip", "anime": "Anime", "realism": "Realism",
         "hand-drawn": "Hand-drawn", "oil-painting": "Oil painting", "epic-sci-fi": "Epic sci-fi",
         "watercolour": "Watercolour", "ink": "Ink"}
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MIME = {v: k for k, v in EXT.items()}
QUALITIES = ("draft", "standard", "best")
STATIC_STYLES = Path(__file__).parent / "static" / "styles"
MAX_REF_BYTES = 8 * 1024 * 1024
MIN_SIDE, MAX_SIDE = 256, 8192
# the note sent with the style picture when the pipeline has none of its own (it has the same words)
STYLE_REF_NOTE = ("The last reference picture shows only the drawing style to use: its medium, line, colour, texture and "
                  "light. Never take its subjects, figures, faces, composition or setting.")


def now_ms() -> int:
    return int(time.time() * 1000)


def sniff(data: bytes) -> tuple[str, int, int]:
    """(mime, width, height) of a PNG, JPEG or WebP from its own bytes, by stdlib code only. Raises StyleError 422
    `bad-image` saying which rule failed."""
    def bad(why: str):
        return StyleError(422, "bad-image", why)
    if not data:
        raise bad("The file is empty.")
    mime = w = h = None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise bad("The PNG file is damaged: its header can't be read.")
        mime, (w, h) = "image/png", struct.unpack(">II", data[16:24])
    elif data[:3] == b"\xff\xd8\xff":
        mime, i, n = "image/jpeg", 2, len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                break
            m = data[i + 1]
            if m == 0xFF:            # fill byte
                i += 1
                continue
            if m in (0x01, 0xD8) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            if m in (0xD9, 0xDA):    # end of image or start of scan before any frame header
                break
            length = int.from_bytes(data[i + 2:i + 4], "big")
            if length < 2:
                break
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 > n:
                    break
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                break
            i += 2 + length
        if w is None:
            raise bad("The JPEG file is damaged: its size can't be read.")
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        mime, chunk = "image/webp", data[12:16]
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            w, h = (int.from_bytes(data[26:28], "little") & 0x3FFF), (int.from_bytes(data[28:30], "little") & 0x3FFF)
        elif chunk == b"VP8L" and data[20] == 0x2F:
            b = int.from_bytes(data[21:25], "little")
            w, h = (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8X":
            w, h = int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
        else:
            raise bad("The WebP file is damaged: its size can't be read.")
    else:
        raise bad("Choose a PNG, JPEG or WebP picture.")
    if not (MIN_SIDE <= w <= MAX_SIDE and MIN_SIDE <= h <= MAX_SIDE):
        raise bad(f"The picture is {w} by {h} pixels; each side must be {MIN_SIDE} to {MAX_SIDE} pixels.")
    return mime, w, h


async def read_upload(file: UploadFile) -> bytes:
    """The uploaded bytes, refused past `MAX_REF_BYTES` (413 `too-large`) before they are all read."""
    data = await file.read(MAX_REF_BYTES + 1)
    if len(data) > MAX_REF_BYTES:
        raise StyleError(413, "too-large", "The picture is over 8 MB. Choose a smaller one.")
    return data


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


def valid_ref(ref) -> bool:
    return (isinstance(ref, dict) and ref.get("ext") in MIME and isinstance(ref.get("sha"), str)
            and re.fullmatch(r"[0-9a-f]{64}", ref["sha"]) is not None)


def stale(pv: dict, row: dict) -> bool:
    """A preview drawn from other words, or (for one that recorded it) with another picture sent or none, is stale."""
    if pv.get("text") != row["text"]:
        return True
    # a preview drawn before reference pictures existed recorded none, so it was drawn with no picture
    sent = row["reference"]["sha"] if row.get("reference") and row.get("useReference") else None
    return pv.get("reference") != sent


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
                and s["slug"] not in art.STYLES and s["slug"] not in RESERVED and isinstance(s.get("text"), str) and s["text"]]
        retired = {k: v for k, v in (data.get("retired") or {}).items()
                   if isinstance(k, str) and SLUG.fullmatch(k) and isinstance(v, str) and v}
        for row in rows:
            if not valid_ref(row.get("reference")):
                row["reference"] = None
            row["useReference"] = bool(row.get("useReference")) and row["reference"] is not None
        refs = {k: v for k, v in (data.get("retired_refs") or {}).items()
                if isinstance(k, str) and SLUG.fullmatch(k) and isinstance(v, dict) and valid_ref(v.get("reference"))}
        return {"styles": rows, "retired": retired, "retired_refs": refs}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"styles.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        self._sync(data)

    def _sync(self, data: dict) -> None:
        """Give `art.style_sentence` the saved styles' words (a deleted one's too, for its art sets), and the draw path
        the reference pictures sent with them (`useReference` only, live and retired)."""
        words = {**data["retired"], **{s["slug"]: s["text"] for s in data["styles"]}}
        art.SAVED_STYLES = words     # one assignment: a run reading it in another thread never sees it half filled
        refs = {}
        for slug, r in [*data["retired_refs"].items(), *((s["slug"], s) for s in data["styles"])]:
            ref = r.get("reference")
            if r.get("useReference") and ref:
                p = self.ref_file(slug, ref["ext"])
                if p.is_file():
                    refs[slug] = {"path": p, "mime": MIME[ref["ext"]], "sha": ref["sha"]}
        art.SAVED_STYLE_REFS = refs  # one assignment, as above

    def ref_file(self, slug: str, ext: str) -> Path:
        return self.dir / f"{slug}.ref.{ext}"

    # ---- reading

    def saved(self, slug: str) -> dict | None:
        return next((s for s in self._read()["styles"] if s["slug"] == slug), None)

    def public(self, s: dict) -> dict:
        pv = s.get("preview") if isinstance(s.get("preview"), dict) else None
        url = f"/api/styles/{s['slug']}/preview?v={pv['created']}" if pv and self.preview_path(s["slug"]) else None
        ref = s.get("reference")
        ref_url = f"/api/styles/{s['slug']}/reference?v={ref['sha'][:12]}" if ref and self.reference_path(s["slug"]) else None
        return {"slug": s["slug"], "name": s.get("name") or s["slug"], "text": s["text"], "builtin": False,
                "preview": url, "stale": bool(url and stale(pv, s)), "created": s.get("created"),
                "reference": ref_url, "useReference": bool(ref_url and s.get("useReference"))}

    def all(self) -> list[dict]:
        """Every style the picker offers: the listed ones first, in `art.STYLES` order, then the saved ones oldest first."""
        out = [{"slug": k, "name": NAMES.get(k, k.replace("-", " ").capitalize()), "text": v, "builtin": True,
                "preview": f"/static/styles/{k}.webp" if (STATIC_STYLES / f"{k}.webp").is_file() else None,
                "stale": False, "reference": None, "useReference": False} for k, v in art.STYLES.items()]
        return out + [self.public(s) for s in self._read()["styles"]]

    def preview_path(self, slug: str) -> Path | None:
        s = self.saved(slug) if SLUG.fullmatch(slug or "") else None
        pv = (s or {}).get("preview")
        if not isinstance(pv, dict) or pv.get("ext") not in MIME:
            return None
        p = self.dir / f"{slug}.{pv['ext']}"
        return p if p.is_file() else None

    def reference_path(self, slug: str) -> Path | None:
        s = self.saved(slug) if SLUG.fullmatch(slug or "") else None
        ref = (s or {}).get("reference")
        if not ref:
            return None
        p = self.ref_file(slug, ref["ext"])
        return p if p.is_file() else None

    def style_reference(self, slug: str) -> tuple[bytes, str, str] | None:
        """(bytes, mime, sha) of the picture sent with every drawing in this saved style, or None."""
        s = self.saved(slug) if SLUG.fullmatch(slug or "") else None
        if not s or not s.get("useReference"):
            return None
        p = self.reference_path(slug)
        try:
            return (p.read_bytes(), MIME[s["reference"]["ext"]], s["reference"]["sha"]) if p else None
        except OSError:
            return None

    # ---- changes

    def create(self, name, text) -> dict:
        name, text = clean(name, MAX_NAME, "name"), clean(text, MAX_TEXT, "description")
        slug = art.style_slug(name)
        with self._lock:
            data = self._read()
            if slug in art.STYLES or slug in RESERVED or any(s["slug"] == slug for s in data["styles"]):
                raise StyleError(409, "exists", "There is a style with that name already. Choose another name.")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            row = {"slug": slug, "name": name, "text": text, "created": now, "updated": now, "preview": None,
                   "reference": None, "useReference": False}
            old = data["retired_refs"].pop(slug, None)    # the picture a deleted style of this name kept
            if old and self.ref_file(slug, old["reference"]["ext"]).is_file():
                row["reference"], row["useReference"] = old["reference"], bool(old.get("useReference"))
            data["styles"].append(row)
            data["retired"].pop(slug, None)
            self._write(data)
        return self.public(row)

    def update(self, slug: str, name=None, text=None, use_reference=None) -> dict:
        """Rename it, change its words or whether its picture is sent; the slug (what configs and art sets name) never
        changes, and a preview of other words or another picture is stale."""
        name = None if name is None else clean(name, MAX_NAME, "name")
        text = None if text is None else clean(text, MAX_TEXT, "description")
        if use_reference is not None and not isinstance(use_reference, bool):
            raise StyleError(422, "bad-style", "useReference must be true or false.")
        with self._lock:
            data = self._read()
            row = self._row(data, slug)
            if use_reference and not row.get("reference"):
                raise StyleError(422, "bad-style", "Add a reference picture before sending it with each drawing.")
            if name is not None:
                row["name"] = name
            if text is not None:
                row["text"] = text
            if use_reference is not None:
                row["useReference"] = use_reference
            row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write(data)
        return self.public(row)

    def delete(self, slug: str) -> None:
        with self._lock:
            data = self._read()
            row = self._row(data, slug)
            data["styles"].remove(row)
            data["retired"][slug] = row["text"]     # its art sets stay and keep drawing in its words
            if row.get("reference"):                # and with its picture, as they did
                data["retired_refs"][slug] = {"reference": row["reference"], "useReference": bool(row.get("useReference"))}
            self._write(data)
            for f in self._previews(slug):
                f.unlink(missing_ok=True)

    def _previews(self, slug: str) -> list[Path]:
        """The preview files of a style (`<slug>.<ext>`), never its reference picture (`<slug>.ref.<ext>`)."""
        return [self.dir / f"{slug}.{ext}" for ext in MIME if (self.dir / f"{slug}.{ext}").is_file()]

    def set_reference(self, slug: str, data: bytes) -> dict:
        """File the person's reference picture of a saved style, replacing the old one. The first picture is sent with
        each drawing until the person says otherwise; a replaced one keeps that choice."""
        mime, w, h = sniff(data)
        ext = EXT[mime]
        with self._lock:
            store = self._read()
            row = self._row(store, slug)
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.ref_file(slug, ext)
            tmp = self.dir / f".{slug}.{uuid.uuid4().hex}.tmp"
            keep = self.dir / f".{slug}.{uuid.uuid4().hex}.old"
            tmp.write_bytes(data)
            had = target.is_file()
            if had:
                target.replace(keep)
            tmp.replace(target)
            first = not row.get("reference")
            row["reference"] = {"ext": ext, "sha": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                                "width": w, "height": h, "created": now_ms()}
            if first:
                row["useReference"] = True
            row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                self._write(store)
            except BaseException:
                if had:
                    keep.replace(target)
                else:
                    target.unlink(missing_ok=True)
                raise
            keep.unlink(missing_ok=True)
            for e in MIME:      # an old picture in another format, only once the list names the new one
                if e != ext:
                    self.ref_file(slug, e).unlink(missing_ok=True)
        return self.public(row)

    def remove_reference(self, slug: str) -> dict:
        with self._lock:
            store = self._read()
            row = self._row(store, slug)
            if not row.get("reference"):
                raise StyleError(404, "no-reference", "This style has no reference picture.")
            row["reference"], row["useReference"] = None, False
            row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write(store)
            for e in MIME:
                self.ref_file(slug, e).unlink(missing_ok=True)
        return self.public(row)

    def set_preview(self, slug: str, data: bytes, mime: str, text: str, meta: dict) -> dict:
        """File a drawn preview of the style as it read when it was drawn (`text`), replacing the old one."""
        ext = EXT.get(mime)
        if ext is None:
            raise StyleError(502, "model-failed", "The image provider sent a picture in a format this server can't keep.")
        with self._lock:
            store = self._read()
            row = self._row(store, slug)
            self.dir.mkdir(parents=True, exist_ok=True)
            target = self.dir / f"{slug}.{ext}"
            tmp = self.dir / f".{slug}.{uuid.uuid4().hex}.tmp"
            keep = self.dir / f".{slug}.{uuid.uuid4().hex}.old"
            tmp.write_bytes(data)
            had = target.is_file()
            if had:
                target.replace(keep)         # the old preview of the same format, put back if the list can't be written
            tmp.replace(target)
            row["preview"] = {"ext": ext, "text": text, "created": now_ms(), **meta}
            try:
                self._write(store)
            except BaseException:
                if had:
                    keep.replace(target)
                else:
                    target.unlink(missing_ok=True)
                raise
            keep.unlink(missing_ok=True)
            for f in self._previews(slug):  # an old preview in another format, only once the list names the new one
                if f.suffix[1:] != ext:
                    f.unlink(missing_ok=True)
        return self.public(row)

    @staticmethod
    def _row(data: dict, slug: str) -> dict:
        if slug in art.STYLES:
            raise StyleError(409, "builtin", "The listed styles can't be changed.")
        row = next((s for s in data["styles"] if s["slug"] == slug), None)
        if row is None:
            raise StyleError(404, "no-style", "There is no such style.")
        return row


def router(store: StyleStore, image_model: Callable, vision_model: Callable | None = None) -> APIRouter:
    """The /api/styles routes. Mutating ones sit behind the app's CSRF header and sign-in like every /api route.
    `vision_model()` is a text backend that can see pictures (or None), for "Describe it for me" (the app passes
    `JobManager.describe_model`, the strong tier first)."""
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
            return store.update(slug, body.get("name"), body.get("text"), body.get("useReference"))
        except StyleError as e:
            return err(e)

    @r.put("/api/styles/{slug}/reference")
    async def styles_reference_put(slug: str, file: UploadFile = File(...)):
        """The person's reference picture of a saved style (multipart `file`), checked by its bytes only."""
        try:
            if slug in art.STYLES:
                raise StyleError(409, "builtin", "The listed styles can't have a reference picture.")
            data = await read_upload(file)
            return await asyncio.get_running_loop().run_in_executor(None, store.set_reference, slug, data)
        except StyleError as e:
            return err(e)

    @r.delete("/api/styles/{slug}/reference")
    def styles_reference_delete(slug: str):
        try:
            return store.remove_reference(slug)
        except StyleError as e:
            return err(e)

    @r.get("/api/styles/{slug}/reference")
    def styles_reference_file(slug: str):
        p = store.reference_path(slug)
        if p is None:
            return JSONResponse({"detail": "no reference picture"}, status_code=404)
        return FileResponse(p, media_type=MIME[p.name.rsplit(".", 1)[1]], headers={"Cache-Control": "no-cache"})

    @r.post("/api/styles/describe")
    async def styles_describe(file: UploadFile = File(...)):
        """The Description sentence for a picture's drawing style, from one call on a text model that sees pictures
        (`art.describe_style`). Nothing is stored."""
        try:
            data = await read_upload(file)
            mime, _, _ = sniff(data)
        except StyleError as e:
            return err(e)

        def work():
            model = vision_model() if vision_model else None
            if model is None:
                raise StyleError(409, "no-vision-model",
                                 "Describing a picture needs a cloud model that can see pictures. Add a key in Settings, Cloud models.")
            before = art._cost_of(model)
            try:
                text = art.describe_style(model, data, mime)
            except llm.NoVision as e:
                raise StyleError(409, "no-vision-model",
                                 "The text model set up here can't see pictures. Add a key for one that can in Settings, Cloud models.") from e
            except llm.ProviderError as e:
                raise StyleError(502, "model-failed", str(e)[:200]) from e
            except ValueError as e:
                raise StyleError(502, "model-failed", "The model could not describe this picture. Write the description yourself.") from e
            except Exception as e:  # noqa: BLE001 - a provider's failure must not leak its body or key, as Ask has it
                raise StyleError(502, "model-failed", f"The model could not describe this picture ({type(e).__name__}).") from e
            ref = art.model_ref(model) or {}
            return {"text": text, "provider": ref.get("provider"), "model": ref.get("model"),
                    "cost": round(art._cost_of(model) - before, 6)}

        try:
            return await asyncio.get_running_loop().run_in_executor(None, work)
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
            if model is not None and provider is None and not llm.image_model(getattr(image, "provider", ""), model):
                # a model alone is one of the image provider that would draw it (the first set up)
                raise StyleError(422, "bad-model", f"Unknown image model {model!r}.")
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
                ref = store.style_reference(slug)     # the style's own picture, sent last as every drawing in it gets it
                refs = [(ref[0], ref[1])] if ref else []
                note = getattr(art, "STYLE_REF", STYLE_REF_NOTE) if ref else ""
                try:
                    got = image.draw(preview_prompt(text), shape=shape, quality=q, references=refs, note=note)
                except images.ImageRefused as e:
                    raise StyleError(422, "refused", f"refused: {e.reason[:160]}") from e
                except llm.ProviderError as e:
                    raise StyleError(502, "model-failed", str(e)[:200]) from e
                out = store.set_preview(slug, got.data, got.mime, text,
                                        {"provider": prov, "model": got.model or name, "quality": q,
                                         "cost": got.cost, "shape": shape, "reference": ref[2] if ref else None})
            finally:
                with drawing_lock:
                    drawing.discard(slug)
            return {**out, "price": price, "cost": got.cost, "provider": prov, "model": got.model or name, "quality": q}

        try:
            return await asyncio.get_running_loop().run_in_executor(None, work)
        except StyleError as e:
            return err(e)

    return r

