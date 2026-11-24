"""Book storage and a one-at-a-time generation queue for the web UI.

A local Ollama on a laptop can run one model at a time, so jobs run sequentially on a single
worker thread. Every job's metadata and event stream are persisted under the data directory, so
history survives restarts; a job interrupted by a restart is marked "interrupted" and can be
resumed, because the generator checkpoints after every chunk.
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import art as artmod, arcs as book_arcs, bookkind, epub, generate, llm, portraits, text, validate

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
TERMINAL = {"done", "failed", "stopped", "interrupted"}


def _now() -> float:
    return round(time.time(), 3)


class BookStore:
    """Uploaded EPUBs, stored by content hash, with parsed metadata cached beside them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, book_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", book_id):
            raise KeyError(book_id)
        p = self.root / f"{book_id}.epub"
        if not p.exists():
            raise KeyError(book_id)
        return p

    def add(self, data: bytes, filename: str) -> dict:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("file is larger than 200 MB")
        book_id = hashlib.sha256(data).hexdigest()[:16]
        target = self.root / f"{book_id}.epub"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        try:
            with zipfile.ZipFile(tmp) as z:
                z.getinfo("META-INF/container.xml")
            meta = describe(tmp)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"not a readable EPUB: {e}") from e
        tmp.replace(target)
        meta.update({"id": book_id, "filename": Path(filename).name, "uploaded": _now()})
        meta["cover"] = self._save_cover(book_id)
        (self.root / f"{book_id}.json").write_text(json.dumps(meta))
        return meta

    def get(self, book_id: str) -> dict:
        self.path(book_id)
        return self._fresh(self.root / f"{book_id}.json")

    def list(self) -> list[dict]:
        out = []
        for p in self.root.glob("*.json"):
            try:
                out.append(self._fresh(p))
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
        return sorted(out, key=lambda b: -b.get("uploaded", 0))

    def _fresh(self, meta_path: Path) -> dict:
        """Book metadata, re-read from the EPUB when it predates a field we now extract (series)."""
        meta = json.loads(meta_path.read_text())
        if "cover" not in meta and meta_path.with_suffix(".epub").exists():
            meta["cover"] = self._save_cover(meta["id"])
            meta_path.write_text(json.dumps(meta))
        if "series" not in meta or "isbns" not in meta or "bookKind" not in meta or meta.get("storyRules") != STORY_RULES:
            epub_path = meta_path.with_suffix(".epub")
            if epub_path.exists():
                keep = {k: meta[k] for k in ("id", "filename", "uploaded", "cover") if k in meta}
                meta = {**describe(epub_path), **keep}
                meta_path.write_text(json.dumps(meta))
        return meta

    def delete(self, book_id: str) -> None:
        self.path(book_id).unlink()
        (self.root / f"{book_id}.json").unlink(missing_ok=True)
        for ext in epub.COVER_TYPES.values():
            (self.root / f"{book_id}.cover{ext}").unlink(missing_ok=True)

    def _save_cover(self, book_id: str) -> str | None:
        """Extract the EPUB's cover image; returns its file name, or None if the book has none."""
        try:
            found = epub.cover(self.root / f"{book_id}.epub")
        except Exception:
            found = None
        if not found:
            return None
        data, media_type = found
        name = f"{book_id}.cover{epub.COVER_TYPES[media_type]}"
        (self.root / name).write_bytes(data)
        return name

    def cover_path(self, book_id: str) -> Path | None:
        name = self.get(book_id).get("cover")
        return self.root / name if name and (self.root / name).exists() else None


# Sections that are not the story: front and back matter. They're skipped by default and don't
# count against "covers the whole story".
NOT_STORY = re.compile(
    r"contents|licen[cs]e|gutenberg|copyright|colophon|imprint|title ?page|endnotes|uncopyright|^index$|"
    r"acknowledg|dedication|about the (author|publisher)|also by|^praise|newsletter|sign up|preview|excerpt|"
    r"^maps?$|reading group|discussion questions|^cover$|^half title|"
    # An author's own introduction or note usually discusses the plot: never story by default.
    r"^introduction|author[’']?s note|^foreword|^preface|^afterword", re.IGNORECASE)


# Sections that close a book. Anything after the first of them, once the story has begun, belongs
# to the back of the book too: ebooks put a preview of the next title there ("Book Two – Chapter 1").
BACK_MATTER = re.compile(
    r"also by|about the (author|publisher)|acknowledg|newsletter|sign up|preview|excerpt|^praise|"
    r"reading group|discussion questions|coming soon|other books|more from|books by", re.IGNORECASE)
STORY_RULES = 2   # bump when story detection changes, so cached book metadata is recomputed


def is_story(title: str, words: int) -> bool:
    return words >= 60 and not NOT_STORY.search(title or "")


def mark_story(chapters: list[dict]) -> None:
    """Set each section's "story" flag: not front or back matter, and not after the back matter."""
    begun = ended = False
    for c in chapters:
        story = is_story(c["title"], c["words"])
        if begun and BACK_MATTER.search(c["title"] or ""):
            ended = True
        c["story"] = story and not ended
        begun = begun or story


def describe(path: Path, book=None) -> dict:
    """Metadata the UI needs: title, chapters with word counts, edition hash."""
    book = book or epub.read(path)   # a caller that already parsed the EPUB passes it
    ends = [c.start for c in book.chapters[1:]] + [len(book.blocks)]
    chapters = [{"n": i + 1, "title": c.title or f"Section {i + 1}", "start": c.start,
                 "words": sum(len(b.words) for b in book.blocks[c.start:end])}
                for i, (c, end) in enumerate(zip(book.chapters, ends))]
    mark_story(chapters)
    found = bookkind.infer(book)   # no model call here; a build may ask one (generate)
    return {"bookKind": {"detected": found.kind, "basis": found.basis, "why": found.why}, "storyRules": STORY_RULES, "title": book.title or path.stem, "authors": book.authors, "language": book.language,
            "series": book.series, "seriesIndex": book.series_index, "isbns": book.isbns,
            "blocks": len(book.blocks), "words": sum(len(b.words) for b in book.blocks),
            "chapters": chapters, "textHash": text.text_hash(b.words for b in book.blocks)}


@dataclass
class Job:
    id: str
    book_id: str
    book_title: str
    config: dict
    output: str
    status: str = "queued"          # queued | running | stopping | stopped | done | failed | interrupted
    created: float = field(default_factory=_now)
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    report: dict | None = None
    progress: dict = field(default_factory=dict)   # latest chunk / phase / stats snapshot
    resume: bool = False
    kind: str = "index"             # index: a build; art: pictures for an existing index (`output` is that index, never written)


def art_config(raw) -> dict:
    """The pictures part of a job config, checked: {style, level, portraits, quality, provider, model, cap}.
    Raises ValueError with a sentence for the person."""
    if not isinstance(raw, dict):
        raise ValueError("art must be an object")
    style = " ".join(str(raw.get("style") or "").split())
    if not style or len(style) > 200:
        raise ValueError("Choose a style, or write one of at most 200 characters.")
    level = raw.get("level", "main")
    if level not in artmod.LEVELS:
        raise ValueError("level must be \"main\" or \"all\".")
    provider = raw.get("provider")
    if provider is not None and provider not in llm.IMAGE_PROVIDERS:
        raise ValueError(f"No image provider {provider!r}.")
    quality = raw.get("quality", "standard")
    if quality not in ("draft", "standard", "best"):
        raise ValueError("quality must be draft, standard or best.")
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or (provider and not llm.image_model(provider, model))):
        raise ValueError(f"Unknown image model {model!r}.")
    cap = raw.get("cap")   # required: a run never draws without a spending limit (the form fills in $10)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= 500:
        raise ValueError("The cost cap must be a number of dollars above 0 and at most 500.")
    return {"style": style, "level": level, "portraits": bool(raw.get("portraits", True)), "quality": quality,
            "provider": provider, "model": model, "cap": cap}


class JobManager:
    def __init__(self, root: Path, books: BookStore, index_dir: Path, ollama_url: str,
                 keys=lambda provider: None, provider_config=lambda provider: {},
                 prior_for=lambda job: [], distinct_for=lambda job: set()) -> None:
        self.root, self.books, self.index_dir, self.ollama_url = root, books, index_dir, ollama_url
        self.prior_for = prior_for   # job → earlier books' index documents, for series context
        self.keys = keys   # provider id → API key or None; a callable, so a key saved in Settings applies to the next run
        self.provider_config = provider_config   # provider id → {base_url, model} (the custom server)
        self.distinct_for = distinct_for   # job → pairs of entity ids a person marked "not the same"
        self.after_run = None   # job → dict, run on a finished index (the app sets editor.reapply)
        self.art_store = None   # web.art.ArtStore, set by the app
        self.art_inputs = None  # job → (index doc, epub.Book, earlier books' docs), set by the app
        self.art_journal = None  # (job, text hash, slug, ids of the pictures drawn) → journal entry id, set by the app
        root.mkdir(parents=True, exist_ok=True)
        index_dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.events: dict[str, list[dict]] = {}
        self.stop_flags: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        self._resume_on_start: list[str] = []
        self._load()
        for job_id in sorted(self._resume_on_start, key=lambda i: self.jobs[i].created):
            self._save(self.jobs[job_id])
            self._emit(self.jobs[job_id], {"type": "queued", "resume": self.jobs[job_id].resume,
                                           "reason": "server restarted"})
            self.queue.put(job_id)
        threading.Thread(target=self._worker, daemon=True, name="lorekeeper-jobs").start()

    # ---- persistence -----------------------------------------------------------------------

    def _load(self) -> None:
        for p in self.root.glob("*.json"):
            try:
                job = Job(**json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            was = job.status
            if was not in TERMINAL:
                job.status = "interrupted"
            self.jobs[job.id] = job
            ev = self.root / f"{job.id}.events.jsonl"
            self.events[job.id] = [json.loads(line) for line in ev.read_text().splitlines()] if ev.exists() else []
            # A run the server was in the middle of when it stopped carries on where it left off
            # (the generator checkpoints every chunk). A run the user was stopping stays stopped.
            if was == "stopping":
                job.status, job.finished = "stopped", job.finished or _now()
            elif was in ("running", "queued"):
                job.status, job.resume, job.error, job.finished = "queued", was == "running", None, None
                self._resume_on_start.append(job.id)

    def _save(self, job: Job) -> None:
        (self.root / f"{job.id}.json").write_text(json.dumps(asdict(job)))

    def _emit(self, job: Job, event: dict) -> None:
        with self.lock:
            evs = self.events.setdefault(job.id, [])
            event = {"n": len(evs), "t": _now(), **event}
            evs.append(event)
        with open(self.root / f"{job.id}.events.jsonl", "a") as f:
            f.write(json.dumps(event) + "\n")

    # ---- API -------------------------------------------------------------------------------

    def submit(self, book_id: str, config: dict) -> Job:
        meta = self.books.get(book_id)
        job_id = uuid.uuid4().hex[:12]
        stem = generate.slug(meta["title"]) or "book"
        job = Job(id=job_id, book_id=book_id, book_title=meta["title"], config=config,
                  output=str(self.index_dir / f"{stem}-{job_id}.lore.json"))
        self.jobs[job_id] = job
        self.events[job_id] = []
        self._save(job)
        self._emit(job, {"type": "queued"})
        self.queue.put(job_id)
        return job

    def submit_art(self, key: str, config: dict, *, index_path: str, book_id: str, title: str, text_hash: str) -> Job:
        """Queue pictures for an existing index (library key `key`): a job of kind "art" on the same queue and events as a
        build. Refused (RuntimeError) while a run writes that index or pictures are already being drawn for that edition."""
        cfg = art_config(config)
        if self.writing(index_path):
            raise RuntimeError("A build is writing this index. Add pictures once it has finished.")
        if self.drawing(text_hash):
            raise RuntimeError("Pictures are already being drawn for this book.")
        job_id = uuid.uuid4().hex[:12]
        image = cfg["provider"] or next((p for p in llm.IMAGE_PROVIDERS if self.keys(p)), None)
        job = Job(id=job_id, book_id=book_id, book_title=title, output=index_path, kind="art",
                  config={"art": cfg, "index_key": key, "text_hash": text_hash,
                          "provider": self.text_provider(), "image_provider": image})
        self.jobs[job_id] = job
        self.events[job_id] = []
        self._save(job)
        self._emit(job, {"type": "queued"})
        self.queue.put(job_id)
        return job

    def active(self):
        return [j for j in list(self.jobs.values()) if j.status in ("queued", "running", "stopping")]

    def writing(self, index_path) -> bool:
        """A build (not a pictures run, which never writes the index) is writing or about to write this index file."""
        return any(j.kind != "art" and j.output == str(index_path) for j in self.active())

    def drawing(self, text_hash: str) -> bool:
        """Pictures are being drawn, or will be, for this edition: an art run, or a build that ends with the pictures stage."""
        for j in self.active():
            if j.kind == "art" and j.config.get("text_hash") == text_hash:
                return True
            if j.kind != "art" and j.config.get("art"):
                try:
                    if self.books.get(j.book_id).get("textHash") == text_hash:
                        return True
                except (KeyError, OSError, ValueError):
                    continue
        return False

    def stop(self, job_id: str) -> Job:
        job = self.jobs[job_id]
        if job.status == "queued":
            job.status, job.finished = "stopped", _now()
            self._emit(job, {"type": "stopped"})
        elif job.status == "running":
            job.status = "stopping"
            self.stop_flags[job_id].set()
            self._emit(job, {"type": "log", "line": "stop requested"})
        self._save(job)
        return job

    def resume(self, job_id: str) -> Job:
        job = self.jobs[job_id]
        if job.status not in ("stopped", "interrupted", "failed"):
            raise ValueError(f"cannot resume a job that is {job.status}")
        job.status, job.resume, job.error, job.finished = "queued", True, None, None
        self._save(job)
        self._emit(job, {"type": "queued", "resume": True})
        self.queue.put(job_id)
        return job

    def delete(self, job_id: str) -> None:
        job = self.jobs[job_id]
        if job.status in ("running", "stopping", "queued"):
            raise ValueError("stop the job before deleting it")
        del self.jobs[job_id]
        self.events.pop(job_id, None)
        for suffix in (".json", ".events.jsonl"):
            (self.root / f"{job_id}{suffix}").unlink(missing_ok=True)
        if job.kind == "art":
            return    # its output is the index it drew for, which belongs to the library, and its pictures stay
        out = Path(job.output)
        for p in (out, out.with_suffix(out.suffix + ".state"), out.with_suffix(out.suffix + ".report")):
            p.unlink(missing_ok=True)

    def events_after(self, job_id: str, n: int) -> list[dict]:
        with self.lock:
            return list(self.events.get(job_id, [])[n:])

    def throughput(self) -> dict[str, float]:
        """Observed words per second per model, from finished chunks of past jobs."""
        totals: dict[str, list[float]] = {}
        for job_id, evs in self.events.items():
            model = self.jobs[job_id].config.get("model") if job_id in self.jobs else None
            for e in evs:
                if e.get("type") == "chunk_done" and e.get("seconds"):
                    t = totals.setdefault(model, [0.0, 0.0])
                    t[0] += e["words"]
                    t[1] += e["seconds"]
        return {m: round(w / s, 2) for m, (w, s) in totals.items() if m and s}

    # ---- worker ----------------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued":
                continue
            if job.kind == "art":
                self._run_art(job)
            else:
                self._run(job)

    def configured(self) -> list[str]:
        """Cloud providers that can be used right now, in registry order."""
        return [p for p in llm.PROVIDERS
                if self.keys(p) or (p == "custom" and (self.provider_config(p) or {}).get("base_url"))]

    def cloud_model(self, provider: str, model: str | None = None):
        """A backend for a registry provider, with its saved key and (for a custom server) URL and model."""
        cfg = self.provider_config(provider) or {}
        return llm.make(provider, model or cfg.get("model"), api_key=self.keys(provider), base_url=cfg.get("base_url"))

    def text_provider(self, job_provider: str | None = None) -> str | None:
        """The provider `cheap_model` would use: the run's own cloud provider, else the first configured one."""
        order = ([job_provider] if job_provider in self.configured() else []) + self.configured()
        for p in order:
            if ((self.provider_config(p) or {}).get("model") if p == "custom" else llm.PROVIDERS[p]["cheap"]):
                return p
        return None

    def cheap_model(self, job_provider: str | None = None):
        """The cheap model for background writing (descriptions): the run's own cloud provider,
        else the first configured one; None when no cloud provider is set up."""
        order = ([job_provider] if job_provider in self.configured() else []) + self.configured()
        for p in order:
            spec = llm.PROVIDERS[p]
            name = (self.provider_config(p) or {}).get("model") if p == "custom" else spec["cheap"]
            if name:
                return self.cloud_model(p, name)
        return None

    def _portraits(self, job: Job, book, prior: list[dict], flag: threading.Event) -> dict:
        """Rewrite the finished index's profiles from every passage mentioning each entity
        (portraits.py). Needs a cloud model key; a failure here keeps the index as the run wrote it."""
        try:
            model = self.cheap_model(job.config.get("provider"))
            if model is None:
                self._emit(job, {"type": "log", "line": "descriptions: skipped (add a cloud model key in Settings to write them)"})
                return {"skipped": "no cloud key"}
            self._emit(job, {"type": "phase", "phase": "portraits"})
            model.cancel = flag.is_set
            out = Path(job.output)
            d = json.loads(out.read_text())
            portraits.clear(d)

            def progress(done: int, total: int) -> None:
                job.progress = {**job.progress, "portraits": [done, total], "last": "portraits"}
                self._emit(job, {"type": "portraits", "done": done, "total": total})
            stats = portraits.portraits([(d, book)], model, log=lambda m: None, prior=prior, progress=progress)
            errors = [e for e in validate.check_index(d) if e.level == "error"]
            if errors:
                raise ValueError(f"result would be invalid: {errors[0]}")
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
            tmp.replace(out)
        except llm.Cancelled:
            self._emit(job, {"type": "log", "line": "descriptions: stopped; the index keeps its earlier descriptions"})
            return {"skipped": "stopped"}
        except Exception as e:  # noqa: BLE001 - the index itself is done; don't fail the run over this
            self._emit(job, {"type": "log", "line": f"descriptions: failed ({type(e).__name__}: {e}); kept the earlier ones"})
            return {"failed": str(e)}
        cost = f"${model.usage.cost_usd:.2f}" if model.usage.cost_usd else "cost unknown"
        self._emit(job, {"type": "log", "line": f"descriptions: {stats.get('portraits', 0)} written ({cost})"})
        return {**stats, "cost_usd": round(model.usage.cost_usd, 4)}

    def _arcs(self, job: Job, book, prior: list[dict], flag: threading.Event) -> dict:
        """Write each person's short arc for this book from the finished index (arcs.py). Needs a cloud
        model key; a failure here keeps the index as the run wrote it."""
        if (job.config.get("provider") or "ollama") == "ollama":
            self._emit(job, {"type": "log", "line": "book arcs: skipped for a local run"})
            return {"skipped": "local run"}
        try:
            model = self.cheap_model(job.config.get("provider"))
            if model is None:
                self._emit(job, {"type": "log", "line": "book arcs: skipped (add a cloud model key in Settings to write them)"})
                return {"skipped": "no cloud key"}
            self._emit(job, {"type": "phase", "phase": "arcs"})
            model.cancel = flag.is_set
            out = Path(job.output)
            d = json.loads(out.read_text())
            before = {str(e) for e in validate.check_index(d) if e.level == "error"}
            book_arcs.clear(d)

            def progress(done: int, total: int) -> None:
                job.progress = {**job.progress, "arcs": [done, total], "last": "arcs"}
                self._emit(job, {"type": "arcs", "done": done, "total": total})
            stats = book_arcs.arcs([(d, book)], model, log=lambda m: None, prior=prior, progress=progress)
            errors = [e for e in validate.check_index(d) if e.level == "error" and str(e) not in before]
            if errors:
                raise ValueError(f"result would be invalid: {errors[0]}")
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
            tmp.replace(out)
        except llm.Cancelled:
            self._emit(job, {"type": "log", "line": "book arcs: stopped; this build has no book arcs"})
            return {"skipped": "stopped"}
        except Exception as e:  # noqa: BLE001 - the index itself is done; don't fail the run over this
            self._emit(job, {"type": "log", "line": f"book arcs: failed ({type(e).__name__}: {e}); the index is kept without book arcs"})
            return {"failed": str(e)}
        cost = f"${model.usage.cost_usd:.2f}" if model.usage.cost_usd else "cost unknown"
        self._emit(job, {"type": "log", "line": f"book arcs: {stats.get('arcs', 0)} written ({cost})"})
        return {**stats, "cost_usd": round(model.usage.cost_usd, 4)}

    def image_model(self, provider: str | None = None, model: str | None = None):
        """An image backend with its key (saved in Settings or from the environment, as text keys): the named provider,
        else the first image provider with a key. None when there is no key."""
        for p in [provider] if provider else list(llm.IMAGE_PROVIDERS):
            if p in llm.IMAGE_PROVIDERS and self.keys(p):
                return llm.make_image(p, model or None, api_key=self.keys(p))
        return None

    def _art(self, job: Job, doc: dict, book, prior: list[dict], flag: threading.Event) -> dict:
        """Draw the art set of an index (art.py): chapter pictures and portraits into the art store, keyed by edition and
        style. The index is never written. Needs an image key and a cloud text model for the prompts; a failure or a stop
        keeps the index and every picture drawn so far. The pictures it adds are one journal entry, so undo takes out
        exactly those."""
        from . import art as store_mod
        cfg = art_config(job.config.get("art"))
        h, added, slug = store_mod.text_hash(doc), [], None
        try:
            image = self.image_model(cfg["provider"], cfg["model"])
            if image is None:
                which = llm.PROVIDERS[cfg["provider"]]["label"] if cfg["provider"] else "Gemini or OpenAI"
                self._emit(job, {"type": "log", "line": f"pictures: skipped (add a {which} key in Settings to draw them)"})
                return {"skipped": "no image key"}
            text_model = self.cheap_model(job.config.get("provider"))
            if text_model is None:
                self._emit(job, {"type": "log", "line": "pictures: skipped (add a cloud model key in Settings to write the prompts)"})
                return {"skipped": "no cloud key"}
            if self.art_store is None:
                raise RuntimeError("no art store")
            self._emit(job, {"type": "phase", "phase": "art"})
            text_model.cancel = image.cancel = flag.is_set
            store = self.art_store
            slug, style = store.resolve(h, cfg["style"])    # one set per slug: "Ink" draws into the "ink" set
            books = []
            for d in prior:
                ph = store_mod.text_hash(d)
                if not store_mod.HASH.fullmatch(ph):
                    title = (d.get("work") or {}).get("title") or "an earlier book"
                    self._emit(job, {"type": "log", "line": f"pictures: {title} names no edition, so its pictures are not read"})
                    continue
                books.append(artmod.Book(d, None, store.sink(ph, store.resolve(ph, style)[1]), None, draw=False))
            books.append(artmod.Book(doc, book, store.sink(h, slug), cfg["level"]))
            box: dict = {}

            def progress(done: int, total: int) -> None:
                il = box.get("il")
                if flag.is_set() and il is not None and not il.stop:
                    il.stop = "stopped by request"
                cost = round(il.report.cost + il.text_spent(), 4) if il else 0.0
                job.progress = {**job.progress, "art": [done, total], "last": "art"}
                self._emit(job, {"type": "art", "done": done, "total": total, "cost": cost})

            def log(line: str) -> None:
                if flag.is_set() and box.get("il") is not None and not box["il"].stop:
                    box["il"].stop = "stopped by request"
                self._emit(job, {"type": "log", "line": f"pictures: {line.strip()}"})
            with store.lock(h, slug):
                had = {r.get("id") for r in (store.manifest(h, slug) or {}).get("pictures", [])}
                try:
                    il = box["il"] = artmod.Illustrator(books, text_model, image, style, portraits=cfg["portraits"],
                                                        quality=cfg["quality"], cap=cfg["cap"], log=log, progress=progress)
                    rep = il.run()
                finally:
                    added = [r["id"] for r in (store.manifest(h, slug) or {}).get("pictures", []) if r.get("id") not in had]
                    self._journal_art(job, h, slug, added)
        except llm.Cancelled:
            self._emit(job, {"type": "log", "line": "pictures: stopped; the pictures drawn so far are kept"})
            return {"skipped": "stopped", "drawn": len(added), "style": slug, "textHash": h}
        except Exception as e:  # noqa: BLE001 - the index itself is done; don't fail the run over this
            self._emit(job, {"type": "log", "line": f"pictures: failed ({type(e).__name__}: {e}); the pictures drawn so far are kept"})
            return {"failed": str(e), "drawn": len(added)}
        out = {"drawn": rep.drawn, "refused": rep.refused, "skipped": dict(rep.skipped), "cost_usd": round(rep.cost, 4),
               "text_cost_usd": round(rep.text_cost, 4), "stopped": rep.stopped, "held": rep.held,
               "reasons": rep.reasons[:200], "style": slug, "textHash": h}
        self._emit(job, {"type": "art", "done": il.done, "total": il.total, "cost": round(rep.cost + rep.text_cost, 4)})
        for line in rep.lines()[:40]:
            self._emit(job, {"type": "log", "line": f"pictures: {line.strip()}"})
        return out

    def _journal_art(self, job: Job, text_hash: str, slug: str, added: list[str]) -> None:
        if not added or self.art_journal is None:
            return
        try:
            entry = self.art_journal(job, text_hash, slug, added)
            if entry:
                job.progress = {**job.progress, "artUndo": entry}
        except Exception as e:  # noqa: BLE001 - the pictures are drawn; only their undo is missing
            self._emit(job, {"type": "log", "line": f"pictures: not added to the undo history ({type(e).__name__}: {e})"})

    @staticmethod
    def _art_stopped(got: dict) -> bool:
        return got.get("skipped") == "stopped" or str(got.get("stopped") or "").startswith("stopped by")

    def _finish_art(self, job: Job, report: dict, book, prior: list[dict], flag: threading.Event) -> None:
        """A build's last stage. A stop here leaves the build stopped and resumable from the pictures (`resumeFrom`), so a
        resume draws the rest without building the index again."""
        job.progress = {**job.progress, "resumeFrom": "art"}
        job.report = report
        self._save(job)
        report["art"] = got = self._art(job, json.loads(Path(job.output).read_text()), book, prior, flag)
        if flag.is_set() or self._art_stopped(got):
            report["stopped"] = True
        else:
            job.progress = {k: v for k, v in job.progress.items() if k != "resumeFrom"}
        job.report = report
        job.status = "stopped" if report.get("stopped") else "done"
        self._emit(job, {"type": job.status, "report": report})

    def _run_art(self, job: Job) -> None:
        """A pictures run on an existing index: no generator, the same events as a build's pictures stage."""
        flag = self.stop_flags[job.id] = threading.Event()
        job.status, job.started = "running", job.started or _now()
        self._save(job)
        self._emit(job, {"type": "running", "resume": job.resume})
        try:
            for _ in range(100):     # a run resumed at start-up can reach the worker before the app is wired
                if self.art_inputs is not None and self.art_store is not None:
                    break
                time.sleep(0.05)
            if self.art_inputs is None:
                raise RuntimeError("pictures are not available on this server")
            doc, book, prior = self.art_inputs(job)
            got = self._art(job, doc, book, prior, flag)
            report = {"art": got}
            if flag.is_set() or self._art_stopped(got):
                report["stopped"] = True
            if got.get("failed"):
                job.status, job.error = "failed", got["failed"]
            else:
                job.status = "stopped" if report.get("stopped") else "done"
            job.report = report
            self._emit(job, {"type": job.status, "report": report} if job.status != "failed" else
                         {"type": "failed", "error": job.error})
        except Exception as e:  # surface every failure to the UI rather than killing the worker
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "failed", "error": job.error, "trace": traceback.format_exc(limit=5)})
        finally:
            job.finished = _now()
            job.resume = False
            self._save(job)

    def _reapply_edits(self, job: Job) -> dict:
        """Put the person's recorded edits for this book back onto the fresh index (editor.reapply)."""
        try:
            got = self.after_run(job)
        except Exception as e:  # noqa: BLE001 - the index itself is done; don't fail the run over this
            self._emit(job, {"type": "log", "line": f"your edits: could not be applied ({type(e).__name__}: {e})"})
            return {"failed": str(e)}
        if got.get("failed"):
            self._emit(job, {"type": "log", "line": f"your edits: not applied, they would make the index invalid ({got['failed']})"})
        elif got.get("applied") or got.get("skipped"):
            gone = f", {got['skipped']} no longer apply" if got.get("skipped") else ""
            self._emit(job, {"type": "log", "line": f"your edits: {got.get('applied', 0)} applied again{gone}"})
        return got

    def _run(self, job: Job) -> None:
        flag = self.stop_flags[job.id] = threading.Event()
        job.status, job.started = "running", job.started or _now()
        self._save(job)
        self._emit(job, {"type": "running", "resume": job.resume})
        c = job.config
        try:
            book_path = self.books.path(job.book_id)
            book = epub.read(book_path)
            opts = generate.Options(
                chapters=c.get("chapters") or None, chunk_words=int(c.get("chunk_words") or generate.default_chunk_words(c.get("provider"))),
                chunk_mode=c.get("chunk_mode") if c.get("chunk_mode") in ("words", "chapter") else "words",  # the form always sends it; old configs keep their parts
                max_part_words=generate.max_part_words(c.get("provider"), int(c.get("num_ctx") or 16384)),
                depth=c.get("depth") if c.get("depth") in generate.DEPTHS else "standard",
                events=bool(c.get("events", True)),
                verify=bool(c.get("verify", True)), series_id=c.get("series_id") or None,
                book_key=c.get("book_key") or None, license=c.get("license") or "CC0-1.0",
                prior=self.prior_for(job) if c.get("series_id") else [],
                whole_story=bool(c.get("whole_story")), distinct=set(self.distinct_for(job)))
            kind = c.get("book_kind") or "auto"
            opts.book_kind = None if kind == "auto" else kind   # None: the generator infers it
            if opts.prior:
                self._emit(job, {"type": "log", "line": f"building on {len(opts.prior)} earlier book(s) in the series"})
            provider = c.get("provider") or "ollama"
            if provider in llm.PROVIDERS:
                if provider not in self.configured():
                    raise ValueError(f"No API key for {llm.PROVIDERS[provider]['label']}. Add one in Settings.")
                model = self.cloud_model(provider, c.get("model"))
                if c.get("check_model") and c["check_model"] != c.get("model"):
                    # A stronger model for the fact-check: it decides what's kept, so it matters most.
                    opts.check_llm = self.cloud_model(provider, c["check_model"])
            elif provider == "ollama":
                model = llm.make("ollama", c.get("model"), url=self.ollama_url, num_ctx=int(c.get("num_ctx", 16384)))
            else:
                raise ValueError(f"unknown provider {provider!r}")

            if job.resume and job.progress.get("resumeFrom") == "art" and c.get("art") and Path(job.output).exists():
                # The index and its stages were done when the run stopped in the pictures stage: carry on with the pictures.
                report = {k: v for k, v in (job.report or {}).items() if k not in ("stopped", "art")}
                self._emit(job, {"type": "log", "line": "the index is done; carrying on with the pictures"})
                self._finish_art(job, report, book, opts.prior, flag)
                return

            def on_event(e: dict) -> None:
                if e["type"] in ("chunk", "phase", "chunk_done", "start"):
                    job.progress = {**job.progress, **{k: v for k, v in e.items() if k != "added"},
                                    "last": e["type"]}
                    if e["type"] == "chunk_done":
                        self._save(job)
                self._emit(job, e)

            report = generate.run(book, model, opts, book_path, job.output, resume=job.resume,
                                  log=lambda line: self._emit(job, {"type": "log", "line": line}),
                                  on_event=on_event, should_stop=flag.is_set)
            if not report.get("stopped") and c.get("portraits", True):
                report["portraits"] = self._portraits(job, book, opts.prior, flag)
            if not report.get("stopped") and self.after_run is not None:
                report["edits"] = self._reapply_edits(job)
            if not report.get("stopped") and c.get("arcs", True):   # last: arcs tell the events as the person edited them
                report["arcs"] = self._arcs(job, book, opts.prior, flag)
            if not report.get("stopped") and c.get("art"):   # after arcs: pictures read the finished index
                self._finish_art(job, report, book, opts.prior, flag)
                return
            job.report = report
            job.status = "stopped" if report.get("stopped") else "done"
            self._emit(job, {"type": job.status, "report": report})
        except Exception as e:  # surface every failure to the UI rather than killing the worker
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
            self._emit(job, {"type": "failed", "error": job.error, "trace": traceback.format_exc(limit=5)})
        finally:
            job.finished = _now()
            job.resume = False
            self._save(job)
