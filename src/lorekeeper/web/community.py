"""Community indexes: look up ready-made indexes for a book in a static registry (SPEC.md §8.7), fetch one
to preview it, install it as a version of the book, and check an index built here before sharing it.

The server talks to the registry; the browser never does. Everything a registry returns is untrusted:
bodies are capped and parsed with the registry module's guards, validated against the schemas, an index
is checked against the book's own text, and a `href` is followed only inside the registry's own origin
and path. Nothing is fetched until the person has turned the feature on (`community.enabled` in
library.json); looking a book up tells the registry the person owns it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import math
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .. import anchors as anchor_mod
from .. import epub, registry, validate
from .catalog import model_label

DEFAULT_URL = "https://nightspine.github.io/lorekeeper-registry"
LOOKUP_TTL = 600           # seconds a lookup file is served without asking the registry again
MAX_BODY = 6 * 1024 * 1024
TIMEOUT = 10
AUDIT_CLAIMS = 50
KEEP = object()          # "leave it as it is" for configure()
CACHE_KEEP = 20        # fetched indexes (and their audits) kept in community/cache
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
PART_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")
HREF_RE = re.compile(r"^/v0/indexes/([A-Za-z0-9-]{1,128})/([A-Za-z0-9-]{1,128})/v([0-9]{1,6})\.lore\.json$")


class CommunityError(Exception):
    """A refusal with an HTTP status and, for the client to branch on, an optional code."""

    def __init__(self, status: int, detail: str, code: str | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code


class FetchError(Exception):
    """The registry could not be reached, answered with something we refuse to read, or sent too much."""


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the origin the request started at."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        a, b = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if (a.scheme, a.netloc) != (b.scheme, b.netloc):
            raise urllib.error.HTTPError(req.full_url, code, "redirect to another origin refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_fetch(url: str) -> tuple[int, bytes]:
    """GET `url`: (status, body). 10 s timeout, at most MAX_BODY bytes read, no redirect off the origin.
    Raises FetchError when nothing usable came back (a 404 is a status, not an error)."""
    opener = urllib.request.build_opener(_SameOrigin)
    req = urllib.request.Request(url, headers={"User-Agent": "lorekeeper", "Accept": "application/json"})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            if int(r.headers.get("Content-Length") or 0) > MAX_BODY:
                raise FetchError("the registry's answer is over the size limit")
            status = r.status
            deadline = time.monotonic() + TIMEOUT      # the whole download, not each read
            chunks, size = [], 0
            while size <= MAX_BODY:
                if time.monotonic() > deadline:
                    raise FetchError("the registry took too long to answer")
                read = getattr(r, "read1", None) or r.read     # one recv per call, so the deadline is seen after each
                chunk = read(min(65536, MAX_BODY + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b"".join(chunks)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (OSError, ValueError, http.client.HTTPException) as e:     # URLError, timeouts, bad URLs, BadStatusLine
        raise FetchError(str(e)) from e
    if len(body) > MAX_BODY:
        raise FetchError("the registry's answer is over the size limit")
    return status, body


def wilson_lower(supported: int, sampled: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson interval for supported/sampled: 48 of 50 beats 2 of 2. Never raises."""
    if not (isinstance(supported, int) and isinstance(sampled, int)) or sampled <= 0:
        return 0.0
    p = min(1.0, max(0.0, supported / sampled))
    d = 1 + z * z / sampled
    centre = p + z * z / (2 * sampled)
    spread = z * math.sqrt(max(0.0, p * (1 - p) / sampled + z * z / (4 * sampled * sampled)))
    return max(0.0, (centre - spread) / d)


def _count(a: dict, key: str) -> int:
    v = a.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def audit_counts_ok(a) -> bool:
    """An audit whose counts can be true: a non-empty sample that holds its three parts."""
    if not isinstance(a, dict):
        return False
    sampled = _count(a, "sampled")
    return sampled > 0 and sum(_count(a, k) for k in ("supported", "unsupported", "unsure")) <= sampled


def _judged(c: dict) -> tuple[int, int]:
    """(supported, supported + unsupported) of a checked measure: what the ranking and the lines use.
    A count the audit leaves out is the rest of the sample: sampled - supported - unsure."""
    supported = _count(c, "supported")
    if isinstance(c.get("unsupported"), int) and not isinstance(c.get("unsupported"), bool):
        unsupported = _count(c, "unsupported")
    else:
        unsupported = max(0, _count(c, "sampled") - supported - _count(c, "unsure"))
    return supported, supported + unsupported


def registry_slug(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return re.sub(r"[^a-z0-9]+", "-", f"{u.hostname or ''}{u.path}".lower()).strip("-")[:60] or "registry"


def _clip(value, n: int = 300) -> str:
    return str(value)[:n]


class Community:
    def __init__(self, data_dir: Path, catalog, library, *, fetch=None, reader=None, audit=None, model=None,
                 plan=None, now=time.time) -> None:
        self.dir = data_dir / "community"
        self.catalog, self.library = catalog, library
        self.fetch = fetch or default_fetch
        self.reader = reader or epub.read
        self._audit, self.model, self.plan, self.now = audit, model, plan, now
        self._lock = threading.Lock()

    @property
    def indexes(self) -> Path:
        return self.dir / "indexes"

    # ---- settings --------------------------------------------------------------------------

    def config(self) -> dict:
        c = self.catalog.state().get("community") or {}
        url = c.get("url") or DEFAULT_URL
        enabled = c.get("enabled")
        return {"enabled": enabled if isinstance(enabled, bool) else None, "url": url, "default": url == DEFAULT_URL}

    def configure(self, enabled=KEEP, url=None) -> dict:
        """Turn the feature on or off and/or set the address. Leaving `enabled` out leaves the opt-in answer
        alone: saving an address is not saying yes."""
        if enabled is KEEP:
            if url is None:
                raise CommunityError(400, "Nothing to change: send \"enabled\" and/or \"url\".")
        elif not isinstance(enabled, bool):
            raise CommunityError(400, "\"enabled\" must be true or false")
        before = self.config()["url"]
        if url is not None:
            url = str(url).strip().rstrip("/")
            u = urllib.parse.urlsplit(url)
            if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise CommunityError(400, "The registry address must be an https URL.")
            if u.scheme == "http" and u.hostname not in LOCAL_HOSTS:
                raise CommunityError(400, "The registry address must be an https URL (http works only for localhost).")
        else:
            url = before

        def apply(st):
            st["community"] = {**(st.get("community") or {}), "url": url,
                               **({} if enabled is KEEP else {"enabled": enabled})}
        self.catalog.update_state(apply)
        if url != before:
            shutil.rmtree(self.dir / "lookup", ignore_errors=True)
        return self.config()

    def _need_on(self) -> str:
        cfg = self.config()
        if cfg["enabled"] is not True:
            raise CommunityError(409, "Community indexes are turned off. Turn them on in Settings.", "off")
        return cfg["url"]

    # ---- the registry ----------------------------------------------------------------------

    @staticmethod
    def _resolve(url: str, href: str) -> tuple[str, str, str, int]:
        """The index URL for `href`, only when it points into the registry's own origin and path.
        Returns (url, publisher, slug, version)."""
        base = urllib.parse.urlsplit(url)
        prefix = base.path.rstrip("/")
        try:
            target = urllib.parse.urlsplit(urllib.parse.urljoin(url.rstrip("/") + "/", href))
        except ValueError as e:
            raise CommunityError(502, "The registry sent an address we won't follow.", "href") from e
        if (target.scheme, target.netloc) != (base.scheme, base.netloc) or target.query or target.fragment \
                or not target.path.startswith(prefix + "/"):
            raise CommunityError(502, "The registry sent an address outside itself, so it was not followed.", "href")
        m = HREF_RE.fullmatch(target.path[len(prefix):])
        if not m:
            raise CommunityError(502, "The registry sent an address we won't follow.", "href")
        return urllib.parse.urlunsplit(target), m.group(1), m.group(2), int(m.group(3))

    def _get(self, url: str) -> tuple[int, bytes]:
        try:
            status, body = self.fetch(url)
        except FetchError:
            raise
        except (OSError, ValueError, http.client.HTTPException) as e:
            raise FetchError(str(e)) from e
        if len(body) > MAX_BODY:
            raise FetchError("the registry's answer is over the size limit")
        return status, body

    def _lookup_file(self, text_hash: str) -> Path:
        return self.dir / "lookup" / f"{text_hash}.json"

    def _read_lookup_cache(self, text_hash: str, url: str):
        try:
            c = json.loads(self._lookup_file(text_hash).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(c, dict) or c.get("url") != url or not isinstance(c.get("results"), list):
            return None
        return c

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")     # unique: simultaneous writers never share one
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def lookup(self, text_hash: str) -> list[dict]:
        """Usable registry results for an edition (exact text, not a series set), fresh or stale.
        Raises FetchError when there is neither a good answer nor a cached one."""
        url = self.config()["url"]
        cached = self._read_lookup_cache(text_hash, url)
        if cached and self.now() - float(cached.get("at") or 0) < LOOKUP_TTL:
            return cached["results"]
        try:
            status, body = self._get(f"{url}/v0/lookup/text/{text_hash}.json")
            if status == 404:
                results: list[dict] = []
            elif status != 200:
                raise FetchError(f"the registry answered {status}")
            else:
                results = self._usable(url, text_hash, body)
        except FetchError:
            if cached:
                return cached["results"]
            raise
        self._write(self._lookup_file(text_hash),
                    json.dumps({"url": url, "at": self.now(), "results": results}).encode())
        return results

    def _usable(self, url: str, text_hash: str, body: bytes) -> list[dict]:
        doc, issues = registry.parse_json_capped(body, "lookup")
        if issues or validate.check_schema(doc, "api", "lookup"):
            raise FetchError("the registry's lookup file is not valid")
        out = []
        for r in doc["results"]:
            listing = r.get("listing")
            if r.get("series") or not isinstance(listing, dict) or listing.get("series"):
                continue     # a series set is not offered in this version
            if registry.listing_problems(listing) or listing["id"] != r["id"] or listing["digest"] != r["digest"]:
                continue
            if not any(e.get("textHash") == text_hash for e in listing["editions"]):
                continue     # exact text only
            try:
                href, pub, slug, version = self._resolve(url, r["href"])
            except CommunityError:
                continue
            if (pub, slug) != (listing["publisher"], listing["slug"]) or version != listing["version"]:
                continue
            out.append({"id": r["id"], "digest": r["digest"], "href": href, "listing": listing})
        return out

    # ---- results ----------------------------------------------------------------------------

    def _audit_file(self, digest: str) -> Path:
        return self.dir / "cache" / f"{digest.split(':')[-1]}.audit.json"

    @staticmethod
    def _newest(attestations: list[dict], kind: str, text_hash: str) -> dict | None:
        mine = [a for a in attestations if a.get("kind") == kind and a.get("textHash", text_hash) == text_hash]
        return max(mine, key=lambda a: a["at"], default=None)

    def _checked(self, listing: dict, text_hash: str, digest: str) -> dict:
        try:
            local = json.loads(self._audit_file(digest).read_text())
        except (OSError, ValueError):
            local = None
        if isinstance(local, dict) and local.get("textHash") == text_hash and audit_counts_ok(local):
            return {"kind": "audit", "by": "you", "sampled": local["sampled"], "supported": _count(local, "supported"),
                    **{k: local[k] for k in ("unsupported", "unsure") if isinstance(local.get(k), int)},
                    **({"model": _clip(model_label({"model": local["model"]}), 80)} if isinstance(local.get("model"), str) and local["model"] else {})}
        atts = listing.get("attestations", [])
        audit = self._newest([a for a in atts if a.get("kind") != "audit" or audit_counts_ok(a)], "audit", text_hash)
        if audit:
            return {"kind": "audit", "by": audit["by"], "sampled": audit["sampled"], "supported": _count(audit, "supported"),
                    **{k: audit[k] for k in ("unsupported", "unsure") if isinstance(audit.get(k), int)},
                    **({"model": _clip(model_label({"model": audit["model"]}), 80)} if isinstance(audit.get("model"), str) and audit["model"] else {})}
        checks = self._newest(atts, "book-checks", text_hash)
        if checks and checks.get("errors") == 0:
            return {"kind": "book-checks", "by": checks["by"]}
        return {"kind": None, "by": None}

    def _installed(self) -> dict[str, list[tuple[int, str]]]:
        """listing id → [(version, library key)] for installs from the registry in use whose file is still there."""
        have = set(self.library.scan())
        url = self.config()["url"]
        out: dict[str, list[tuple[int, str]]] = {}
        for key, rec in ((self.catalog.state().get("community") or {}).get("installed") or {}).items():
            if key in have and isinstance(rec, dict) and rec.get("registry") == url:
                out.setdefault(rec.get("listing"), []).append((int(rec.get("version") or 0), key))
        return out

    def results(self, text_hash: str) -> list[dict]:
        installed = self._installed()
        rows = []
        for r in self.lookup(text_hash):
            try:
                rows.append(self._row(r, text_hash, installed))
            except Exception:  # noqa: BLE001 - one bad listing is dropped, never the whole answer
                continue
        # best first: stable sorts from the least to the most important rule
        rows.sort(key=lambda x: x["updated"], reverse=True)
        rows.sort(key=self._audit_rank)
        rows.sort(key=lambda x: 0 if x["early"] == 0 else 1 if x["early"] is None else 2)
        rows.sort(key=lambda x: not x["complete"])
        return rows

    @staticmethod
    def _audit_rank(x: dict) -> tuple[int, float]:
        """Audits whose Wilson bound (supported / judged) is at least 0.5 first, best first; then listings
        with no audit (or one that judged nothing); then audits below 0.5."""
        c = x["checked"]
        if c["kind"] != "audit":
            return 1, 0.0
        supported, judged = _judged(c)
        w = wilson_lower(supported, judged) if judged else 0.0
        if not judged:
            return 1, 0.0
        return (0 if w >= 0.5 else 2), -w

    def _row(self, r: dict, text_hash: str, installed: dict) -> dict:
        listing = r["listing"]
        mine = max(installed.get(listing["id"], []), default=None)
        gate = self._newest(listing.get("attestations", []), "spoiler-gate", text_hash)
        prov = listing.get("generator")
        made = model_label(prov) if prov else "Hand-written"
        return {
            "id": listing["id"], "publisher": listing["publisher"], "title": _clip(listing["work"]["title"]),
            "version": listing["version"], "digest": listing["digest"], "complete": listing["complete"],
            "updated": listing["updated"], "license": listing["license"], "made": _clip(made),
            "edited": bool(listing.get("edited")), "bookKind": listing.get("bookKind"),
            "checked": self._checked(listing, text_hash, listing["digest"]),
            "early": gate["early"] if gate else None,
            "installed": mine[1] if mine else None,
            "update": bool(mine and mine[0] < listing["version"]),
        }

    def books(self, key: str) -> dict:
        cfg = self.config()
        entry = self.catalog.entry(key)
        if entry is None:
            raise CommunityError(404, "no such book")
        if cfg["enabled"] is False:
            return {"state": "off", "results": []}
        if not entry["textHash"]:
            return {"state": "no-edition", "results": []}
        if cfg["enabled"] is None:
            return {"state": "ask", "results": []}
        try:
            return {"state": "ok", "results": self.results(entry["textHash"])}
        except FetchError:
            return {"state": "unreachable", "results": []}

    # ---- fetch, preview, install -----------------------------------------------------------

    def _cached_result(self, key: str, listing_id: str, digest: str) -> tuple[dict, dict]:
        entry = self.catalog.entry(key)
        if entry is None:
            raise CommunityError(404, "no such book")
        if not entry["textHash"]:
            raise CommunityError(409, "This book's file isn't on this computer yet.", "no-edition")
        try:
            rows = self.lookup(entry["textHash"])
        except FetchError as e:
            raise CommunityError(502, "The registry can't be reached.", "unreachable") from e
        r = next((x for x in rows if x["id"] == listing_id and x["digest"] == digest), None)
        if r is None:
            raise CommunityError(404, "The registry doesn't list that index for this book.")
        return entry, r

    def _cache_file(self, digest: str) -> Path:
        return self.dir / "cache" / f"{digest.split(':')[-1]}.lore.json"

    @staticmethod
    def _digest(value) -> str:
        d = str(value or "")
        d = d if d.startswith("sha256:") else "sha256:" + d
        if not registry.DIGEST_RE.fullmatch(d):
            raise CommunityError(400, "a digest is sha256: and 64 hex digits")
        return d

    def fetch_index(self, key: str, listing_id: str, digest) -> dict:
        digest = self._digest(digest)
        url = self._need_on()
        entry, r = self._cached_result(key, str(listing_id), digest)
        index_url, _, _, _ = self._resolve(url, r["href"])
        have = self._cached_bytes(digest, parse=False)
        if have is not None:
            body = have[0]            # already downloaded and still hashing to the digest: not fetched twice
        else:
            try:
                status, body = self._get(index_url)
            except FetchError as e:
                raise CommunityError(502, f"The registry's index couldn't be downloaded: {e}", "unreachable") from e
            if status != 200:
                raise CommunityError(502, f"The registry answered {status} for that index.", "unreachable")
            if registry.digest_of(body) != digest:
                raise CommunityError(502, "The downloaded file does not match the digest the registry lists, so it "
                                     "was thrown away.", "digest")
        doc, issues = registry.parse_json_capped(body, "index")
        if issues:
            raise CommunityError(422, issues[0].message, "invalid")
        bad = registry.errors_of(validate.check_schema(doc, "index"))
        if not bad and doc.get("series"):
            raise CommunityError(422, "This index belongs to a series, which isn't supported yet.", "invalid")
        if not bad:
            bad = registry.errors_of(validate.check_index(doc, []))
        if bad:
            raise CommunityError(422, f"The index is not valid: {bad[0].message}", "invalid")
        th = entry["textHash"]
        if th not in {e["textHash"] for e in doc["editions"]}:
            raise CommunityError(422, "The index was not made for this copy of the book.", "invalid")
        path = self.library.edition_path(th)
        if path is None:
            raise CommunityError(409, "This book's file isn't on this computer yet.", "no-edition")
        blocks = self.reader(path).blocks
        book_issues = registry.errors_of(validate.check_against_book(doc, blocks))
        resolved = anchor_mod.resolve(doc["anchors"], blocks)
        early = registry.spoiler_gate(doc, blocks)["early"]
        with self._lock:
            if have is None:
                self._write(self._cache_file(digest), body)
            else:
                os.utime(self._cache_file(digest))     # used just now
            self._prune_cache()
        return {"digest": digest, "anchors": len(doc["anchors"]),
                "resolved": sum(1 for v in resolved.values() if v is not None), "early": early,
                "errors": [self._describe(i) for i in book_issues[:5]], "problems": len(book_issues)}

    def _cached_bytes(self, digest: str, parse: bool = True):
        """(bytes, document) of a cached index, only when the bytes still hash to `digest`; else None."""
        try:
            body = self._cache_file(digest).read_bytes()
        except OSError:
            return None
        if registry.digest_of(body) != digest:
            return None
        if not parse:
            return body, None
        doc, issues = registry.parse_json_capped(body, "index")
        return None if issues else (body, doc)

    def _prune_cache(self) -> None:
        """Keep the CACHE_KEEP most recently used fetched indexes and their audit files."""
        folder = self.dir / "cache"
        try:
            files = sorted(folder.glob("*.lore.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            return
        for f in files[CACHE_KEEP:]:
            f.unlink(missing_ok=True)
            (folder / (f.name[:-len(".lore.json")] + ".audit.json")).unlink(missing_ok=True)

    @staticmethod
    def _describe(issue) -> str:
        """Where a problem is and what kind, never the book text a quote-rule message carries."""
        what = "quotes too much of the book" if "consecutive words" in issue.message else _clip(issue.message, 200)
        return f"{_clip(issue.where, 120)}: {what}"

    def preview(self, digest_hex: str) -> dict:
        if not HEX_RE.fullmatch(digest_hex or ""):
            raise CommunityError(404, "no such preview")
        try:
            body = self._cache_file("sha256:" + digest_hex).read_bytes()
        except OSError as e:
            raise CommunityError(404, "That preview isn't downloaded. Open it from the book's page.") from e
        if registry.digest_of(body) != "sha256:" + digest_hex:
            raise CommunityError(409, "The downloaded copy was changed on disk, so it was not used.", "digest")
        doc, issues = registry.parse_json_capped(body, "index")
        if issues:
            raise CommunityError(409, "The downloaded copy can't be read.", "invalid")
        return doc

    @staticmethod
    def _file_name(registry_url: str, publisher: str, slug: str, version: int) -> str:
        if not (PART_RE.fullmatch(publisher) and PART_RE.fullmatch(slug) and isinstance(version, int) and version >= 1):
            raise CommunityError(400, "not a listing we can store")
        return f"{registry_slug(registry_url)}--{publisher}--{slug}--v{version}.lore.json"

    def install(self, key: str, listing_id: str, digest) -> dict:
        digest = self._digest(digest)
        url = self._need_on()
        entry, r = self._cached_result(key, str(listing_id), digest)
        _, pub, slug, version = self._resolve(url, r["href"])
        got = self._cached_bytes(digest)
        if got is None:
            raise CommunityError(409, "Preview the index first: it hasn't been downloaded and checked yet.", "not-fetched")
        body, doc = got
        if entry["textHash"] not in {e["textHash"] for e in doc["editions"]}:
            raise CommunityError(422, "The index was not made for this copy of the book.", "invalid")
        name = self._file_name(url, pub, slug, version)
        lib_key = f"community--{name[:-len('.json')]}"
        old = [k for _, k in self._installed().get(r["id"], [])]
        self.indexes.mkdir(parents=True, exist_ok=True)
        self._write(self.indexes / name, body)

        def apply(st):
            c = st.setdefault("community", {})
            c.setdefault("installed", {})[lib_key] = {
                "listing": r["id"], "version": version, "digest": digest, "registry": self.config()["url"],
                "installed": registry.now_iso()}
            # an explicit default only to carry over the one this replaces; a lone community version is the
            # default by ranking, and an index the person builds later wins
            if st["defaults"].get(key) in old and st["defaults"].get(key) != lib_key:
                st["defaults"][key] = lib_key
        self.catalog.update_state(apply)
        after = self.catalog.entry(key)
        return {"key": lib_key, "madeDefault": bool(after and after["default"] == lib_key)}

    def uninstall(self, lib_key: str) -> dict:
        rec = ((self.catalog.state().get("community") or {}).get("installed") or {}).get(lib_key)
        if not isinstance(rec, dict) or "/" not in str(rec.get("listing")):
            raise CommunityError(404, "That isn't an installed community index.")
        pub, slug = str(rec["listing"]).split("/", 1)
        name = self._file_name(str(rec.get("registry") or ""), pub, slug, rec.get("version"))
        if lib_key != f"community--{name[:-len('.json')]}":
            raise CommunityError(404, "That isn't an installed community index.")
        (self.indexes / name).unlink(missing_ok=True)

        def apply(st):
            st["community"]["installed"].pop(lib_key, None)
            for k in [k for k, v in st["defaults"].items() if v == lib_key]:
                st["defaults"].pop(k)       # the default falls back to the best remaining version
        self.catalog.update_state(apply)
        return {"ok": True}

    # ---- audits ----------------------------------------------------------------------------

    def _run_audit(self, index: dict, blocks) -> dict:
        model = self.model() if self.model else None
        if model is None:
            raise CommunityError(409, "Checking claims needs a cloud model key. Add one in Settings.", "needs-cloud-model")
        audit = self._audit
        if audit is None:
            try:
                from ..audit import audit as audit   # noqa: PLC0415 - written by another role; absent until it merges
            except ImportError as e:
                raise CommunityError(501, "Checking claims against the text isn't available in this version.",
                                     "unavailable") from e
        try:
            got = audit(index, blocks, model, n=AUDIT_CLAIMS, seed=0)
        except CommunityError:
            raise
        except Exception as e:  # noqa: BLE001 - a model failure is a refusal, not a crash
            raise CommunityError(502, f"The check failed: {e}", "audit-failed") from e
        sampled, supported = int(got["sampled"]), int(got["supported"])
        out = {"sampled": sampled, "supported": min(supported, sampled), "unsure": int(got.get("unsure", 0))}
        if got.get("unsupported") is not None:
            out["unsupported"] = int(got["unsupported"])
        return {**out, "model": str(got.get("model") or getattr(model, "name", "") or "model")}

    def audit_preview(self, digest) -> dict:
        digest = self._digest(digest)
        self._need_on()
        doc = self.preview(digest.split(":")[1])
        th, path = self._edition_of(doc)
        result = {**self._run_audit(doc, self.reader(path).blocks), "at": registry.now_iso(), "textHash": th,
                  "digest": digest}
        with self._lock:
            self._write(self._audit_file(digest), json.dumps(result).encode())
        return result

    def _edition_of(self, doc: dict) -> tuple[str, Path]:
        for ed in doc.get("editions", []):
            th = ed.get("textHash")
            p = self.library.edition_path(th) if th and ed.get("builtFrom", True) else None
            if p:
                return th, p
        raise CommunityError(409, "This book's file isn't on this computer.", "no-edition")

    # ---- sharing ---------------------------------------------------------------------------

    def _own(self, lib_key: str) -> dict:
        item = self.library.scan().get(lib_key)
        if item is None or item["doc"].get("kind") != "index":
            raise CommunityError(404, "no such index")
        if item["source"] != "generated":
            raise CommunityError(409, "Only an index built here can be shared.", "not-yours")
        if item["doc"].get("series"):
            raise CommunityError(409, "Series sharing comes next. Only single books can be shared for now.", "series")
        return item

    @staticmethod
    def _digest_of_index(doc: dict) -> str:
        """What an audit is keyed to: a hash of the audited content, so a licence or title change keeps it."""
        part = {k: doc.get(k) for k in ("anchors", "entities", "relations", "events", "recaps")}
        return "sha256:" + hashlib.sha256(json.dumps(part, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _sidecar(item: dict) -> Path:
        return item["path"].with_suffix(".audit.json")

    def stored_audit(self, item: dict) -> dict | None:
        """The audit saved beside the index, when it is for this index as it is now."""
        try:
            a = json.loads(self._sidecar(item).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(a, dict) or a.get("digest") != self._digest_of_index(item["doc"]):
            return None
        if not audit_counts_ok(a):
            return None
        return a

    def _estimate(self, doc: dict, blocks) -> str:
        plan = self.plan() if self.plan else None
        if not plan:
            return "Needs a cloud model key (Settings)"
        try:
            from ..audit import estimate    # noqa: PLC0415
            est = estimate(doc, blocks, n=AUDIT_CLAIMS, seed=0)
        except Exception:  # noqa: BLE001 - an estimate is a courtesy, never a failure
            return "Check claims against the text"
        n = est["claims"]
        price = plan.get("price")
        if not price:
            return f"Check {n} claims against the text, price unknown"
        usd = (est["input_chars"] / 4 * price[0] + 80 * n * price[1]) / 1_000_000
        return f"Check {n} claims against the text, about {max(1, round(usd * 100))} cents"

    @staticmethod
    def _audit_line(a: dict) -> str:
        supported, judged = _judged(a)
        line = f"{supported} of {judged} claims held up."
        unsure = _count(a, "unsure")
        return line + (f" {unsure} could not be judged from the passage cited." if unsure else "")

    def share_check(self, lib_key: str) -> dict:
        item = self._own(lib_key)
        doc = item["doc"]
        _, path = self._edition_of(doc)
        blocks = self.reader(path).blocks
        scrubbed = registry.scrub(doc)
        items: list[dict] = []

        def add(id_, state, text):
            items.append({"id": id_, "state": state, "text": text})

        bad = registry.errors_of(validate.check_index(scrubbed, []))
        add("validates", "fail" if bad else "pass",
            f"The index has a problem: {_clip(bad[0].message)}" if bad else "The index follows the format.")
        done = bool(doc.get("coverage", {}).get("complete"))
        add("complete", "pass" if done else "warn",
            "Covers the whole book." if done else "Covers part of the book. It will be listed as partial.")
        gate = {"early": 0}
        if bad:
            add("book-checks", "todo", "Checked once the index is valid.")
            add("spoiler-gate", "todo", "Checked once the index is valid.")
        else:
            problems = registry.errors_of(validate.check_against_book(scrubbed, blocks))
            resolved = anchor_mod.resolve(scrubbed["anchors"], blocks)
            found = sum(1 for v in resolved.values() if v is not None)
            add("book-checks", "fail" if problems else "pass",
                f"{len(problems)} problems found against the text, first at {_clip(problems[0].where, 120)}." if problems
                else f"All {len(scrubbed['anchors'])} anchors found in the text, nothing quoted at length."
                if found == len(scrubbed["anchors"]) else f"{found} of {len(scrubbed['anchors'])} anchors found in the text.")
            gate = registry.spoiler_gate(scrubbed, blocks)
            n = gate["early"]
            add("spoiler-gate", "warn" if n else "pass",
                f"{n} names are shown before the book uses them." if n > 1
                else "1 name is shown before the book uses it." if n
                else "No name is shown before the book uses it.")
        lic = doc.get("provenance", {}).get("license")
        add("license", "pass" if lic in registry.LICENSES else "fail",
            f"Licence {lic}." if lic in registry.LICENSES else "Choose a licence to share under: CC0-1.0 or CC-BY-4.0.")
        a = self.stored_audit(item)
        add("audit", "pass" if a else "todo", self._audit_line(a) if a else self._estimate(doc, blocks))
        bundle = None if any(i["state"] == "fail" for i in items) else self._bundle(item, blocks)
        return {"ok": not any(i["state"] == "fail" for i in items), "items": items, "license": lic,
                "licenses": list(registry.LICENSES),
                "bundleBytes": len(registry.dumps(bundle).encode("utf-8")) if bundle else None}

    def _bundle(self, item: dict, blocks) -> dict | None:
        doc = item["doc"]
        bundle, _ = registry.make_bundle(doc, blocks=blocks)
        if bundle is None:
            return None
        a = self.stored_audit(item)
        th = next(e["textHash"] for e in doc["editions"] if e.get("builtFrom", True))
        if a:
            att = {"kind": "audit", "by": "builder", "at": a.get("at") or registry.now_iso(),
                   "textHash": a.get("textHash", th), "sampled": a["sampled"], "supported": a["supported"],
                   "model": str(a.get("model") or "model")}
            att.update({k: a[k] for k in ("unsupported", "unsure") if isinstance(a.get(k), int)})
            bundle.setdefault("attestations", []).append(att)
            if validate.check_schema(bundle, "share"):       # a schema without the optional counts yet
                for k in ("unsupported", "unsure"):
                    att.pop(k, None)
                if validate.check_schema(bundle, "share"):
                    bundle["attestations"].pop()
        return bundle

    def share_audit(self, lib_key: str) -> dict:
        item = self._own(lib_key)
        th, path = self._edition_of(item["doc"])
        result = {**self._run_audit(item["doc"], self.reader(path).blocks), "at": registry.now_iso(), "textHash": th,
                  "digest": self._digest_of_index(item["doc"])}
        with self._lock:
            self._write(self._sidecar(item), json.dumps(result).encode())
        return result

    def share_bundle(self, lib_key: str) -> tuple[bytes, str]:
        item = self._own(lib_key)
        _, path = self._edition_of(item["doc"])
        bundle = self._bundle(item, self.reader(path).blocks)
        if bundle is None:
            raise CommunityError(422, "The index can't be shared yet. Run the check to see why.", "invalid")
        slug = registry.slugify(item["doc"]["id"]) or "index"
        return registry.dumps(bundle).encode("utf-8"), f"{slug}.lorekeeper-share.json"
