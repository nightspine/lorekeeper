"""The library catalog: every book (uploads and Book Orbit), its series, and its index versions.

Books are the unit the UI browses; index files belong to books. A version is any index file in
the library directories whose edition text hash matches a book whose file is on this machine.
One version per book is the default: what the Reader and connected apps get.

State kept here (data/library.json): default versions, manual series for uploads, and which
local file a Book Orbit book was downloaded to. The Book Orbit book list is a synced cache.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import time
from pathlib import Path

from ..generate import slug
from ..text import words as text_words
from ..llm import CLAUDE_MODELS
from .jobs import is_story




def series_num(index) -> float | None:
    try:
        return float(index)
    except (TypeError, ValueError):
        return None


def _last_story_start(meta: dict) -> int | None:
    """Block where the last story section starts (an index that reaches it covers the story)."""
    story = set(story_chapters(meta))
    starts = [c["start"] for c in meta.get("chapters", []) if c["n"] in story]
    return max(starts) if starts else None


def story_chapters(meta: dict) -> list[int]:
    """Default sections to index: the story, without front and back matter."""
    return [c["n"] for c in meta.get("chapters", []) if c.get("story", is_story(c["title"], c["words"]))]


class Catalog:
    def __init__(self, data_dir: Path, books, jobs, library) -> None:
        self.books, self.jobs, self.library = books, jobs, library
        self.state_path = data_dir / "library.json"
        self.bo_path = data_dir / "bookorbit" / "books.json"
        self._lock = threading.Lock()

    # ---- persisted state -------------------------------------------------------------------

    def state(self) -> dict:
        try:
            s = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            s = {}
        for k in ("defaults", "series", "links"):
            s.setdefault(k, {})
        return s

    def update_state(self, fn) -> dict:
        with self._lock:
            s = self.state()
            fn(s)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(s, indent=2))
            tmp.replace(self.state_path)
            return s

    def bookorbit_cache(self) -> dict:
        try:
            return json.loads(self.bo_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"books": [], "synced": None}

    def save_bookorbit(self, books: list[dict], url: str, libraries: list[dict]) -> None:
        """Cache the books that have an EPUB (comics, PDFs and audiobooks have no text to index),
        with per-library counts of what was kept and skipped."""
        libs = [{**lib, "books": sum(b["libraryId"] == lib["id"] and bool(b.get("epubFileId")) for b in books),
                 "skipped": sum(b["libraryId"] == lib["id"] and not b.get("epubFileId") for b in books)}
                for lib in libraries]
        self.bo_path.parent.mkdir(parents=True, exist_ok=True)
        self.bo_path.write_text(json.dumps({"books": [b for b in books if b.get("epubFileId")], "libraries": libs,
                                            "synced": time.time(), "url": url}))

    def bookorbit_books(self) -> list[dict]:
        """Cached Book Orbit books with an EPUB, in the libraries the user enabled (all until they choose)."""
        on = self.state().get("bookorbitLibraries")
        return [b for b in self.bookorbit_cache()["books"]
                if b.get("epubFileId") and (on is None or b["libraryId"] in on)]

    def set_bookorbit_libraries(self, ids: list[str] | None) -> None:
        def apply(st):
            if ids is None:
                st.pop("bookorbitLibraries", None)
            else:
                st["bookorbitLibraries"] = ids
        self.update_state(apply)

    def clear_bookorbit(self) -> None:
        self.bo_path.unlink(missing_ok=True)

    # ---- assembling the catalog ------------------------------------------------------------

    def link_uploads(self, uploads: dict[str, dict], bo_books: list[dict]) -> None:
        """Recognise uploads that are also in Book Orbit, so each book is listed once.

        A match is an ISBN in common, or else the same title, an author in common and no
        conflicting series number. Only unambiguous matches are linked; the Book Orbit entry
        then uses the uploaded file, and the upload's default version moves with it.
        """
        st = self.state()
        taken = set(st["links"]) | set(st["links"].values())
        norm = lambda t: " ".join(text_words(t))  # noqa: E731
        surnames = lambda names: {text_words(n)[-1] for n in names if text_words(n)}  # noqa: E731
        new: dict[str, str] = {}
        for uid, meta in uploads.items():
            if uid in taken:
                continue
            isbns = set(meta.get("isbns") or [])
            hits = [b for b in bo_books if b.get("isbn13") and b["isbn13"] in isbns]
            if len(hits) != 1:
                up_index = meta.get("seriesIndex")
                hits = [b for b in bo_books if norm(b["title"]) == norm(meta["title"])
                        and surnames(b["authors"]) & surnames(meta.get("authors", []))
                        and not (up_index and b.get("series") and b["series"][0].get("index")
                                 and series_num(b["series"][0]["index"]) != series_num(up_index))]
            if len(hits) == 1 and f"bo:{hits[0]['id']}" not in taken and f"bo:{hits[0]['id']}" not in new:
                new[f"bo:{hits[0]['id']}"] = uid
        if not new:
            return

        def apply(st):
            for bo_key, uid in new.items():
                st["links"][bo_key] = uid
                if f"up:{uid}" in st["defaults"]:
                    st["defaults"][bo_key] = st["defaults"].pop(f"up:{uid}")
                st["series"].pop(f"up:{uid}", None)    # Book Orbit's series wins
        self.update_state(apply)

    def entries(self) -> list[dict]:
        uploads = {b["id"]: b for b in self.books.list()}
        bo_books = self.bookorbit_books()
        self.link_uploads(uploads, bo_books)
        st = self.state()
        linked = set(st["links"].values())
        out: list[dict] = []

        for b in bo_books:
            key = f"bo:{b['id']}"
            local = st["links"].get(key)
            meta = uploads.get(local) if local else None
            s = (b.get("series") or [None])[0]
            out.append(self._entry(key, "bookorbit", b["title"], b["authors"], meta,
                                   {"name": s["name"], "index": s["index"]} if s and s.get("name") else None,
                                   cover=f"/api/catalog/cover/{key}?v={urllib.parse.quote(str(b.get('coverVersion') or 0), safe='')}"
                                   if b.get("hasCover")
                                   else (f"/api/catalog/cover/up:{local}" if meta and meta.get("cover") else None),
                                   can_build=bool(b.get("epubFileId")) or bool(meta),
                                   extra={"bookorbitId": b["id"], "language": b.get("language")}))
        for bid, meta in uploads.items():
            if bid in linked:
                continue   # a downloaded copy of a Book Orbit book, already listed above
            key = f"up:{bid}"
            manual = st["series"].get(key)
            series = ({"name": manual["name"], "index": manual.get("index")} if manual
                      else {"name": meta["series"], "index": meta.get("seriesIndex")} if meta.get("series") else None)
            out.append(self._entry(key, "upload", meta["title"], meta.get("authors", []), meta, series,
                                   cover=f"/api/catalog/cover/{key}" if meta.get("cover") else None,
                                   can_build=True, extra={"filename": meta.get("filename")}))

        versions = self._versions()
        active = {}
        for j in self.jobs.jobs.values():
            if j.status in ("running", "queued", "stopping"):
                active[j.config.get("catalog_key") or f"up:{j.book_id}"] = j
                active.setdefault(f"local:{j.book_id}", j)
        for e in out:
            e["versions"] = versions.get(e["textHash"], []) if e["textHash"] else []
            story_end = e.pop("_storyStart", None)
            for v in e["versions"]:
                v["coverage"] = round(100 * min(1.0, (v["throughSeq"] + 1) / e["blocks"])) \
                    if e["blocks"] and v.get("throughSeq") is not None else None
                if not v["complete"] and story_end is not None and (v.get("throughSeq") or -1) >= story_end:
                    v["complete"] = True    # every story section is covered; only front/back matter is not
            default = st["defaults"].get(e["key"])
            if not any(v["key"] == default for v in e["versions"]):
                ranked = sorted(e["versions"], key=lambda v: (v["complete"], v.get("coverage") or 0, v.get("updated") or ""),
                                reverse=True)
                default = ranked[0]["key"] if ranked else None
            e["default"] = default
            for v in e["versions"]:
                v["isDefault"] = v["key"] == default
            job = active.get(e["key"]) or (active.get(f"local:{e['localId']}") if e["localId"] else None)
            d = next((v for v in e["versions"] if v["isDefault"]), None)
            e["job"] = {"id": job.id, "status": job.status, "progress": {k: job.progress.get(k) for k in ("index", "chunks")},
                        "model": job.config.get("model")} if job else None
            e["status"] = "building" if job else ("missing" if not d else "complete" if d["complete"] else "partial")
            e["coverage"] = 100 if d and d["complete"] else (d.get("coverage") if d else 0)
        return out

    def _entry(self, key, source, title, authors, meta, series, cover, can_build, extra) -> dict:
        sid = slug(series["name"]) if series else None
        return {
            "key": key, "source": source, "title": title or "Untitled", "authors": authors or [],
            "series": {"id": sid, "name": series["name"], "index": series.get("index"),
                       "num": series_num(series.get("index"))} if series else None,
            "cover": cover, "canBuild": can_build, "localId": meta["id"] if meta else None,
            "words": meta.get("words") if meta else None, "blocks": meta.get("blocks") if meta else None,
            "textHash": meta.get("textHash") if meta else None, **extra,
            "_storyStart": _last_story_start(meta) if meta else None,
        }

    def _versions(self) -> dict[str, list[dict]]:
        by_hash: dict[str, list[dict]] = {}
        for item in self.library.scan().values():
            doc = item["doc"]
            if doc.get("kind") != "index":
                continue
            seqs = {a["id"]: a.get("seq") for a in doc.get("anchors", [])}
            prov = doc.get("provenance", {})
            gen = prov.get("generator") or {}
            stats = {"entities": len(doc.get("entities", [])),
                     "facts": sum(len(e.get("facets", [])) for e in doc.get("entities", [])),
                     "events": len(doc.get("events", []))}
            model = gen.get("model")
            v = {"key": item["key"], "source": item["source"], "id": doc.get("id"),
                 "model": (CLAUDE_MODELS.get(model, {}).get("label") or model) if model
                 else ("Hand-written" if not gen else gen.get("name")),
                 "complete": bool(doc.get("coverage", {}).get("complete")),
                 "throughSeq": seqs.get(doc.get("coverage", {}).get("through")),
                 "updated": prov.get("updated") or prov.get("created"), **stats,
                 "review": ((prov.get("review") or [{}])[-1] or {}).get("status")}
            for ed in doc.get("editions", []):
                if ed.get("textHash") and ed.get("builtFrom", True):
                    by_hash.setdefault(ed["textHash"], []).append(dict(v))
        return by_hash

    def entry(self, key: str) -> dict | None:
        return next((e for e in self.entries() if e["key"] == key), None)

    def series(self, entries: list[dict] | None = None) -> list[dict]:
        entries = entries if entries is not None else self.entries()
        groups: dict[str, dict] = {}
        for e in entries:
            if not e["series"]:
                continue
            g = groups.setdefault(e["series"]["id"], {"id": e["series"]["id"], "name": e["series"]["name"], "books": []})
            g["books"].append(e)
        out = []
        for g in groups.values():
            g["books"].sort(key=lambda e: (e["series"]["num"] is None, e["series"]["num"] or 0, e["title"]))
            authors = {a for e in g["books"] for a in e["authors"]}
            out.append({"id": g["id"], "name": g["name"], "authors": sorted(authors),
                        "books": [e["key"] for e in g["books"]],
                        "indexed": sum(e["status"] != "missing" for e in g["books"])})
        return sorted(out, key=lambda g: g["name"].lower())

    # ---- series rules ----------------------------------------------------------------------

    def earlier(self, key: str, entries: list[dict] | None = None) -> list[dict]:
        """Books before this one in its series, in order."""
        entries = entries if entries is not None else self.entries()
        me = next((e for e in entries if e["key"] == key), None)
        if not me or not me["series"] or me["series"]["num"] is None:
            return []
        return sorted((e for e in entries if e["series"] and e["series"]["id"] == me["series"]["id"]
                       and e["series"]["num"] is not None and e["series"]["num"] < me["series"]["num"]),
                      key=lambda e: e["series"]["num"])

    def preflight(self, key: str) -> dict:
        entries = self.entries()
        before = self.earlier(key, entries)
        missing = [e for e in before if e["status"] == "missing"]
        me = next((e for e in entries if e["key"] == key), None)
        absent: list[int] = []
        if me and me["series"] and me["series"]["num"] is not None:
            # Whole-numbered books before this one that aren't in the library at all.
            have = {int(e["series"]["num"]) for e in before if e["series"]["num"] == int(e["series"]["num"])}
            absent = [n for n in range(1, int(me["series"]["num"])) if n not in have]
        return {"missingEarlier": [{"key": e["key"], "title": e["title"], "index": e["series"]["index"],
                                    "canBuild": e["canBuild"]} for e in missing],
                "notInLibrary": absent,
                "indexedEarlier": [e["series"]["index"] for e in before if e["status"] in ("complete", "partial")],
                "series": me["series"]["name"] if me and me["series"] else None,
                "buildingEarlier": [e["key"] for e in before if e["status"] == "building"]}

    def prior_indexes(self, key: str) -> list[dict]:
        """Default index documents of the earlier books, for building this one on top of them."""
        scan = self.library.scan()
        docs = []
        for e in self.earlier(key):
            if e["default"] and e["default"] in scan:
                docs.append(scan[e["default"]]["doc"])
        return docs

    def series_manifest(self, series_id: str) -> dict | None:
        entries = self.entries()
        g = next((s for s in self.series(entries) if s["id"] == series_id), None)
        if not g:
            return None
        by_key = {e["key"]: e for e in entries}
        books, seen = [], set()
        for k in g["books"]:
            bk = slug(by_key[k]["title"])   # the book key the generator writes into each index
            if bk not in seen:
                seen.add(bk)
                books.append({"key": bk, "title": by_key[k]["title"]})
        return {"lorekeeper": "0.1", "kind": "series", "id": series_id, "title": g["name"], "books": books,
                "orders": {"publication": [b["key"] for b in books]}, "defaultOrder": "publication"}
