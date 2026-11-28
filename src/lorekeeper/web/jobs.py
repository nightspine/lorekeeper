"""Book storage and a one-at-a-time generation queue for the web UI.

A local Ollama on a laptop can run one model at a time, so jobs run sequentially on a single
worker thread. Every job's metadata and event stream are persisted under the data directory, so
history survives restarts; a job interrupted by a restart is marked "interrupted" and can be
resumed, because the generator checkpoints after every chunk.
"""

from __future__ import annotations

import contextlib
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
from ..chapters import numbering

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
TERMINAL = {"done", "failed", "stopped", "interrupted"}
ENTITY_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")   # the index schema's slug
MAX_RUN_ENTITIES = 12      # entries one pictures run draws through the book


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
    # A list of the people in the book is front matter, however long.
    r"^dramatis person|^cast of characters|^list of characters|^(principal )?characters$|"
    # An author's own introduction or note usually discusses the plot: never story by default.
    r"^introduction|author[’']?s note|^foreword|^preface|^afterword", re.IGNORECASE)


# Sections that close a book. Anything after the first of them, once the story has begun, belongs
# to the back of the book too: ebooks put a preview of the next title there ("Book Two – Chapter 1").
BACK_MATTER = re.compile(
    r"also by|about the (author|publisher)|acknowledg|newsletter|sign up|preview|excerpt|^praise|"
    r"reading group|discussion questions|coming soon|other books|more from|books by", re.IGNORECASE)
STORY_RULES = 3   # bump when story detection changes, so cached book metadata is recomputed


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
    # the "Chapter N" the book states (`chapters.numbering` over the whole toc), for the Reader's chapter map
    for c, num in zip(chapters, numbering(chapters)):
        c.update(kind=num["kind"], number=num["number"])
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
    """The pictures part of a job config, checked: {style, level, chapters, portraits, quality, provider, model, cap,
    check}. check (missing: false) has every picture looked at for drawing faults and fixed once (`Illustrator(check=)`).
    level is "main" (missing), "all" or "none" (no chapter pictures); chapters is None (missing: every chapter) or the
    sorted plan chapter numbers `n` whose scenes are drawn (an empty list: none; portraits are never limited by it; that
    each `n` is one of the book's chapters is checked where the index is known, `chapter_choice`); portraits is "main",
    "all" or False. A config that draws nothing (no scenes and no portraits) is refused. Raises ValueError with a sentence
    for the person."""
    if not isinstance(raw, dict):
        raise ValueError("art must be an object")
    style = " ".join(str(raw.get("style") or "").split())
    if not style or len(style) > 200:
        raise ValueError("Choose a style, or write one of at most 200 characters.")
    level = raw.get("level", "main")
    if level not in artmod.LEVELS:
        raise ValueError("level must be \"main\", \"all\" or \"none\".")
    chapters = raw.get("chapters")
    if chapters is not None:
        if not isinstance(chapters, list) or len(chapters) > 10000 or \
                any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in chapters):
            raise ValueError("chapters must be a list of chapter numbers.")
        chapters = sorted(set(chapters))
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
    portraits = raw.get("portraits", "main")   # "main" (missing), "all" (or true, from an older page), or false for none
    if portraits is not False and portraits is not True and portraits not in artmod.CASTS:
        raise ValueError("portraits must be \"main\", \"all\" or false.")
    cast = artmod.portrait_cast(portraits) or False
    entities = raw.get("entities")      # one entry's portraits through the book (the picture sheet): only these, no cast rule
    if entities is not None:
        if not isinstance(entities, list) or not 1 <= len(entities) <= MAX_RUN_ENTITIES or \
                any(not isinstance(x, str) or not ENTITY_ID.fullmatch(x) for x in entities):
            raise ValueError("entities must be a list of entry ids.")
        entities = list(dict.fromkeys(entities))
    until = raw.get("until")            # the reader's position: no picture past it (checked against the index where it is known)
    if until is not None:
        p = until.get("p") if isinstance(until, dict) else None
        book = until.get("book") if isinstance(until, dict) else None
        if isinstance(p, bool) or not (isinstance(p, str) and 0 < len(p) <= 128 or isinstance(p, (int, float)) and 0 <= p <= 1) \
                or not (book is None or isinstance(book, str) and 0 < len(book) <= 128) or set(until) - {"p", "book"}:
            raise ValueError("until must be a position: {\"p\": an anchor id or a fraction, \"book\": a series book key}.")
        if level != "none":
            raise ValueError("A run up to a position draws portraits only (level \"none\").")
        until = {"p": p, "book": book}
    finished = raw.get("finished")      # the reader's finished earlier books (None: every one)
    if finished is not None:
        if not isinstance(finished, list) or len(finished) > 200 or \
                any(not isinstance(k, str) or not 0 < len(k) <= 128 for k in finished):
            raise ValueError("finished must be a list of book keys.")
        finished = sorted(set(finished))
    if (level == "none" or chapters == []) and not cast and not entities:
        raise ValueError("Choose chapters or portraits.")
    check = raw.get("check", False)     # look at every picture for drawing faults and fix them once (missing: off)
    if not isinstance(check, bool):
        raise ValueError("check must be true or false.")
    writers = {k: text_choice_config(raw.get(k), what) for k, what in TEXT_ROLES.items()}
    portrait_provider = raw.get("portraitProvider")
    if portrait_provider is not None and portrait_provider not in llm.IMAGE_PROVIDERS:
        raise ValueError("Claude cannot draw portraits." if portrait_provider in ("claude", "anthropic")
                         else f"No image provider {portrait_provider!r} for portraits.")
    portrait_model = raw.get("portraitModel")
    on = portrait_provider or provider      # the portrait image provider when it is named (else the first with a key)
    if portrait_model is not None and (not isinstance(portrait_model, str) or not portrait_model
                                       or (on and not llm.image_model(on, portrait_model))):
        raise ValueError(f"Unknown image model {portrait_model!r} for portraits.")
    return {"style": style, "level": level, "chapters": chapters, "portraits": cast, "quality": quality,
            "provider": provider, "model": model, "cap": cap, **writers,
            "portraitProvider": portrait_provider, "portraitModel": portrait_model,
            "entities": entities, "until": until, "finished": finished, "check": check}


def entity_run(cfg: dict, doc: dict) -> bool:
    """Whether a checked art config draws an earlier series book `doc` too: an entry's portraits through the series, for a
    reader who finished that book (`finished` None: every earlier book)."""
    if not cfg.get("entities"):
        return False
    key = (doc.get("series") or {}).get("book")
    return cfg.get("finished") is None or key in cfg["finished"]


def run_limits(cfg: dict, doc: dict, bi: int) -> dict:
    """The Illustrator arguments of an entity run (none for an ordinary one): `portraits` the entries, `until` the reader's
    position as (book index, anchor ordinal) in `doc`, the run's last book `bi`, and `finished` the reader's books. Raises
    ValueError when the position is no longer in the index (it was rebuilt since the run was queued): nothing is drawn."""
    out: dict = {}
    if cfg.get("entities"):
        out["portraits"] = frozenset(cfg["entities"])
    if cfg.get("until"):
        ids = [a["id"] for a in doc.get("anchors", [])]
        p = cfg["until"]["p"]
        if not isinstance(p, str) or p not in ids:
            raise ValueError("the reader's position is no longer in this index, so nothing was drawn")
        out["until"] = (bi, ids.index(p))
    if cfg.get("entities") or cfg.get("until"):
        out["finished"] = None if cfg.get("finished") is None else frozenset(cfg["finished"])
    return out


# The text responsibilities of a pictures run, by config key: who writes the scene prompts (plates and event pictures),
# the portrait prompts (portraits, places and things) and the choosing calls (screening, the look index and the look
# sentences). Each is {provider, model} or absent (the defaults: cheap tier, strong tier, cheap tier).
TEXT_ROLES = {"writer": "scene prompts", "portraitWriter": "portrait prompts", "picker": "choosing calls"}


def text_choice_config(raw, what: str = "prompts") -> dict | None:
    """A text model choice of an art config or request, checked for shape: None, or {provider, model} with a cloud
    provider of the registry (Ollama does not write picture prompts) and a model name. Whether the provider is set up
    and lists the model is `JobManager.check_art_models`. Raises ValueError with a sentence for the person."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"The model for the {what} must be a provider and a model.")
    provider, model = raw.get("provider"), raw.get("model")
    provider = "claude" if provider == "anthropic" else provider
    if provider == "ollama":
        raise ValueError(f"A local Ollama model can't write the {what}; choose a cloud model.")
    if not isinstance(provider, str) or provider not in llm.PROVIDERS:
        raise ValueError(f"No text provider {provider!r} for the {what}.")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError(f"Name the model for the {what}.")
    return {"provider": provider, "model": model.strip()}


def chapter_choice(cfg: dict, plan: list[dict]) -> None:
    """Check a checked art config's `chapters` against the book's plan (`art.Book.chapters`): every `n` must be one of
    them. Raises ValueError with a sentence for the person."""
    known = {c["n"] for c in plan}
    if cfg.get("chapters") and any(n not in known for n in cfg["chapters"]):
        raise ValueError("Some of the chosen chapters are not chapters of this book.")


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
        self.art_journal = None  # (job, text hash, slug, ids of the pictures drawn[, also]) → journal entry id, set by the app
        self.art_epub_for = None  # text hash → epub.Book or None: an earlier book's EPUB, for an entity run, set by the app
        # provider id → the text model ids it lists (presets and live), set by the app; checks a picture writer's choice
        self.text_models = lambda provider: {m["id"] for m in llm.PROVIDERS.get(provider, {}).get("models", [])}
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

    def submit_art(self, key: str, config: dict, *, index_path: str, book_id: str, title: str, text_hash: str,
                   also: list[str] = ()) -> Job:
        """Queue pictures for an existing index (library key `key`): a job of kind "art" on the same queue and events as a
        build. `also`: the editions of earlier series books it draws into too (an entry's portraits through the series).
        Refused (RuntimeError) while a run writes that index or pictures are already being drawn for one of those editions."""
        cfg = art_config(config)
        self.check_art_models(cfg)
        if self.writing(index_path):
            raise RuntimeError("A build is writing this index. Add pictures once it has finished.")
        if any(self.drawing(h) for h in (text_hash, *also)):
            raise RuntimeError("Pictures are already being drawn for this book.")
        job_id = uuid.uuid4().hex[:12]
        image = cfg["provider"] or next((p for p in llm.IMAGE_PROVIDERS if self.keys(p)), None)
        job = Job(id=job_id, book_id=book_id, book_title=title, output=index_path, kind="art",
                  config={"art": cfg, "index_key": key, "text_hash": text_hash,
                          **({"text_hashes": sorted(set(also) - {text_hash})} if also else {}),
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
            if j.kind == "art" and (j.config.get("text_hash") == text_hash or text_hash in j.config.get("text_hashes", [])):
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
        got = self.text_choice("cheap", job_provider)
        return self.cloud_model(*got) if got else None

    def strong_model(self, job_provider: str | None = None):
        """As `cheap_model`, the provider's strong tier (the picture sheet's prompt writer)."""
        got = self.text_choice("strong", job_provider)
        return self.cloud_model(*got) if got else None

    def text_choice(self, tier: str = "cheap", job_provider: str | None = None) -> tuple[str, str] | None:
        """(provider, model name) of the `tier` text model `cheap_model` / `strong_model` give, or None."""
        order = ([job_provider] if job_provider in self.configured() else []) + self.configured()
        for p in order:
            spec = llm.PROVIDERS[p]
            name = (self.provider_config(p) or {}).get("model") if p == "custom" else spec[tier]
            if name:
                return p, name
        return None

    def vision_model(self, choice: dict | None = None):
        """A text backend that can see pictures, for the drawing-fault check: the chosen {provider, model} (checked by
        `check_text_choice`) unless its preset is marked as unable to, else the cheap tier of the first configured provider
        whose cheap model is not (a live or custom model counts: it is tried). None when no configured model can."""
        if choice:
            if llm.vision(choice["provider"], choice["model"]) is False or choice["provider"] not in self.configured():
                return None
            return self.cloud_model(choice["provider"], choice["model"])
        for p in self.configured():
            name = (self.provider_config(p) or {}).get("model") if p == "custom" else llm.PROVIDERS[p]["cheap"]
            if name and llm.vision(p, name) is not False:
                return self.cloud_model(p, name)
        return None

    def describe_model(self):
        """A text backend that can see pictures, for describing a style's reference picture: the strong tier of the first
        configured provider whose strong model can (a cheap model misread colour and palette on the bench), else the
        cheap tier `vision_model` picks. None when no configured model can."""
        for p in self.configured():
            name = (self.provider_config(p) or {}).get("model") if p == "custom" else llm.PROVIDERS[p]["strong"]
            if name and llm.vision(p, name) is not False:
                return self.cloud_model(p, name)
        return self.vision_model()

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

    def check_text_choice(self, choice: dict | None, what: str = "prompts") -> None:
        """A checked text choice (`text_choice_config`) must name a provider set up here and a model it lists (its presets,
        its live models, or a custom server's model). Raises ValueError with a sentence for the person."""
        if choice is None:
            return
        p, m = choice["provider"], choice["model"]
        label = llm.PROVIDERS[p]["label"]
        if p not in self.configured():
            raise ValueError(f"{label} is not set up, so it can't write the {what}. Add its key in Settings.")
        listed = set(self.text_models(p) or ())
        if p == "custom" and (self.provider_config(p) or {}).get("model"):
            listed.add(self.provider_config(p)["model"])
        if m not in listed:
            raise ValueError(f"{label} has no model {m!r} for the {what}.")

    def check_art_models(self, cfg: dict) -> None:
        """The model choices of a checked art config against what is set up here: every text choice (`TEXT_ROLES`), and a
        named portrait image provider must have a key (its model is checked by `art_config`, or here when only the model is
        named and the portraits draw with the first image provider that has a key). Raises ValueError."""
        for k, what in TEXT_ROLES.items():
            self.check_text_choice(cfg.get(k), what)
        pp = cfg.get("portraitProvider")
        if pp is not None and not self.keys(pp):
            raise ValueError(f"{llm.PROVIDERS[pp]['label']} is not set up, so it can't draw the portraits. Add its key in Settings.")
        pm = cfg.get("portraitModel")
        on = pp or cfg.get("provider") or next((p for p in llm.IMAGE_PROVIDERS if self.keys(p)), None)
        if pm is not None and on and not llm.image_model(on, pm):
            raise ValueError(f"Unknown image model {pm!r} for portraits.")

    def writer_model(self, choice: dict | None, tier: str = "cheap", job_provider: str | None = None):
        """The text backend for one responsibility: the chosen {provider, model}, else the `tier` default (`cheap_model`
        or `strong_model`). None when the choice's provider is not set up any more, or there is no default."""
        if choice:
            return self.cloud_model(choice["provider"], choice["model"]) if choice["provider"] in self.configured() else None
        return self.strong_model(job_provider) if tier == "strong" else self.cheap_model(job_provider)

    def portrait_image_model(self, cfg: dict, scene_image):
        """The image backend for portraits, places and things: the config's `portraitProvider` / `portraitModel`, else the
        scene backend itself. With only a model named, it is a model of the scene provider. None when that provider has no key."""
        pp, pm = cfg.get("portraitProvider"), cfg.get("portraitModel")
        if pp is None and pm is None:
            return scene_image
        return self.image_model(pp or cfg.get("provider") or getattr(scene_image, "provider", None), pm)

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
            def instead(what: str, chosen: str, used) -> None:
                """One log line when a chosen model is no longer available and another stands in for it."""
                ref = artmod.model_ref(used)
                by = f"{llm.PROVIDERS.get(ref['provider'], {}).get('label', ref['provider'])} {ref['model']}" if ref else "the scene model"
                self._emit(job, {"type": "log", "line": f"pictures: {chosen} for the {what} is no longer set up; using {by} instead"})

            portrait_image = self.portrait_image_model(cfg, image)
            if portrait_image is None:      # its key was removed after the run was accepted: the scenes' model draws them
                pp = cfg["portraitProvider"] or cfg["provider"] or ""
                instead("portraits", " ".join(x for x in (llm.PROVIDERS.get(pp, {}).get("label"), cfg["portraitModel"]) if x)
                        or "The portrait model", image)
                portrait_image = image
            jp = job.config.get("provider")
            # Each responsibility its own model (TEXT_ROLES): scene prompts and choosing calls default to the cheap tier,
            # portrait prompts to the strong one (portraits are few, and every scene draws on them as references).
            text_model = self.writer_model(cfg["writer"], "cheap", jp)
            if text_model is None:
                self._emit(job, {"type": "log", "line": "pictures: skipped (add a cloud model key in Settings to write the prompts)"})
                return {"skipped": "no cloud key"}
            portrait_text = self.writer_model(cfg["portraitWriter"], "strong", jp)
            picker = self.writer_model(cfg["picker"], "cheap", jp)
            for key, what, got in (("portraitWriter", "portrait prompts", portrait_text), ("picker", "choosing calls", picker)):
                if got is None and cfg[key]:    # chosen and accepted, then its key was removed before this stage
                    instead(what, f"{llm.PROVIDERS[cfg[key]['provider']]['label']} {cfg[key]['model']}", text_model)
            portrait_text, picker = portrait_text or text_model, picker or text_model
            check = cfg["check"]
            pref = artmod.model_ref(picker) or {}
            if check and llm.vision(pref.get("provider") or "", pref.get("model")) is False:
                self._emit(job, {"type": "log", "line": f"pictures: not checked for drawing faults ({pref['model']}, which makes "
                                                        "the choosing calls, cannot see pictures)"})
                check = False
            if self.art_store is None:
                raise RuntimeError("no art store")
            self._emit(job, {"type": "phase", "phase": "art"})
            for m in (text_model, portrait_text, picker, image, portrait_image):
                m.cancel = flag.is_set
            store = self.art_store
            slug, style = store.resolve(h, cfg["style"])    # one set per slug: "Ink" draws into the "ink" set
            books, sets = [], []    # sets: (text hash, slug) of every set the run draws into, this book's last
            for d in prior:
                ph = store_mod.text_hash(d)
                if not store_mod.HASH.fullmatch(ph):
                    title = (d.get("work") or {}).get("title") or "an earlier book"
                    self._emit(job, {"type": "log", "line": f"pictures: {title} names no edition, so its pictures are not read"})
                    continue
                pslug, pstyle = store.resolve(ph, style)
                # an entry's portraits through the series: earlier books the reader finished are drawn too (their own sets)
                pbook = self.art_epub_for(ph) if self.art_epub_for and entity_run(cfg, d) else None
                books.append(store.book(d, pbook, ph, pstyle, None, draw=pbook is not None))
                if pbook is not None:
                    sets.append((ph, pslug))
            books.append(store.book(doc, book, h, style, cfg["level"]))
            sets.append((h, slug))
            extra = run_limits(cfg, doc, len(books) - 1)
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
            with contextlib.ExitStack() as held:
                for k in sorted(set(sets)):        # one order for every run, so two never wait on each other
                    held.enter_context(store.lock(*k))
                had = {k: {r.get("id") for r in (store.manifest(*k) or {}).get("pictures", [])} for k in sets}
                try:
                    # hook (picture-model-choices): an entity run draws portraits only; once there is a portrait writer and a
                    # portrait image model, they are the ones to use here
                    il = box["il"] = artmod.Illustrator(books, text_model, image, style, **{"portraits": cfg["portraits"], **extra},
                                                        scene_chapters=cfg["chapters"], quality=cfg["quality"],
                                                        cap=cfg["cap"], log=log, progress=progress, cheap_text=picker,
                                                        portrait_text=portrait_text, portrait_image=portrait_image,
                                                        check=check)
                    rep = il.run()
                finally:
                    got = {k: [r["id"] for r in (store.manifest(*k) or {}).get("pictures", []) if r.get("id") not in had[k]]
                           for k in sets}
                    added = got[(h, slug)]
                    self._journal_art(job, h, slug, added, [{"textHash": k[0], "style": k[1], "added": v}
                                                            for k, v in got.items() if k != (h, slug) and v])
        except llm.Cancelled:
            self._emit(job, {"type": "log", "line": "pictures: stopped; the pictures drawn so far are kept"})
            return {"skipped": "stopped", "drawn": len(added), "style": slug, "textHash": h}
        except Exception as e:  # noqa: BLE001 - the index itself is done; don't fail the run over this
            self._emit(job, {"type": "log", "line": f"pictures: failed ({type(e).__name__}: {e}); the pictures drawn so far are kept"})
            return {"failed": str(e), "drawn": len(added)}
        out = {"drawn": rep.drawn, "refused": rep.refused, "skipped": dict(rep.skipped), "cost_usd": round(rep.cost, 4),
               "text_cost_usd": round(rep.text_cost, 4), "text_costs": dict(rep.text_costs), "stopped": rep.stopped,
               "reasons": rep.reasons[:200], "style": slug, "textHash": h,
               "checked": rep.checked, "fixed": rep.fixed, "fix_refused": rep.fix_refused, "checks": dict(rep.checks)}
        self._emit(job, {"type": "art", "done": il.done, "total": il.total, "cost": round(rep.cost + rep.text_cost, 4)})
        for line in rep.lines()[:40]:
            self._emit(job, {"type": "log", "line": f"pictures: {line.strip()}"})
        return out

    def _journal_art(self, job: Job, text_hash: str, slug: str, added: list[str], also: list[dict] = ()) -> None:
        """The run's pictures as one journal entry; `also` the pictures it drew into earlier books' sets."""
        if not (added or also) or self.art_journal is None:
            return
        try:
            entry = self.art_journal(job, text_hash, slug, added, *([list(also)] if also else []))
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
