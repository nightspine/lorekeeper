"""Book Orbit client: sign in, list the user's books with series, download EPUBs and covers.

Book Orbit has no API keys, so Lorekeeper signs in once with username and password as a
"native" client and keeps only the rotating refresh token (7 days, renewed on every use). The
password is never stored. Access tokens (15 minutes) live in memory and are refreshed on demand.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

API = "/api/v1"
PAGE_SIZE = 200
MAX_BOOKS = 50_000
RANGE_BLOCK = 1 << 20          # read-ahead for partial downloads
MAX_SLIM_BYTES = 200 * 1024 * 1024
# Read-along EPUBs can carry a whole audiobook. Indexing needs only the text, so media stays behind.
MEDIA_EXTENSIONS = (".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac", ".mp4", ".m4v",
                    ".webm", ".mov")


class BookOrbitError(Exception):
    """A failure worth showing the user as is."""


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise BookOrbitError("Enter your Book Orbit address.")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise BookOrbitError("That doesn't look like a web address.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _request(method: str, url: str, body: dict | None = None, token: str | None = None,
             timeout: float = 20, raw: bool = False):
    headers = {"Accept": "application/json", "User-Agent": "Lorekeeper"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
        return (payload, resp.headers.get("Content-Type", "")) if raw else (json.loads(payload) if payload else {})


def _error_message(e: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(e.read() or b"{}")
        msg = detail.get("message")
        if isinstance(msg, list):
            msg = "; ".join(map(str, msg))
        return str(msg or e.reason)
    except (json.JSONDecodeError, ValueError):
        return str(e.reason)


def probe(url: str) -> dict:
    """Confirm the address is a Book Orbit server (public identity endpoint)."""
    url = normalize_url(url)
    try:
        ident = _request("GET", f"{url}{API}/server-identity", timeout=8)
    except urllib.error.HTTPError as e:
        raise BookOrbitError(f"No Book Orbit server answered at {url} ({e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BookOrbitError(f"Couldn't reach {url}. Check the address and that the server is running.") from e
    except json.JSONDecodeError as e:
        raise BookOrbitError(f"{url} answered, but not like a Book Orbit server.") from e
    if ident.get("product") != "bookorbit":
        raise BookOrbitError(f"{url} answered, but not like a Book Orbit server.")
    return {"url": url, "instanceId": ident.get("instanceId"), "protocolVersion": ident.get("protocolVersion")}


@dataclass
class Session:
    url: str
    refresh_token: str
    refresh_expires_at: str | None
    username: str
    name: str | None = None


class BookOrbit:
    """A signed-in connection. `save` persists the session whenever the refresh token rotates."""

    def __init__(self, session: Session, save) -> None:
        self.session, self._save = session, save
        self._access: str | None = None
        self._access_until = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def sign_in(url: str, username: str, password: str, save) -> "BookOrbit":
        url = probe(url)["url"]
        try:
            r = _request("POST", f"{url}{API}/auth/login", {
                "username": username, "password": password, "clientKind": "native", "deviceLabel": "Lorekeeper"})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                raise BookOrbitError("Book Orbit didn't accept that username and password.") from e
            if e.code == 429:
                raise BookOrbitError("Too many sign-in attempts. Wait a minute and try again.") from e
            raise BookOrbitError(f"Sign-in failed: {_error_message(e)}") from e
        if not r.get("refreshToken"):
            raise BookOrbitError("This Book Orbit server didn't return a session for apps. It may be too old.")
        user = r.get("user") or {}
        if user.get("isDefaultPassword"):
            raise BookOrbitError("Change this account's default password in Book Orbit first.")
        bo = BookOrbit(Session(url, r["refreshToken"], r.get("refreshTokenExpiresAt"),
                               user.get("username") or username, user.get("name")), save)
        bo._access, bo._access_until = r["accessToken"], time.time() + 13 * 60
        save(bo.session)
        return bo

    # ---- tokens ----------------------------------------------------------------------------

    def _token(self, force: bool = False) -> str:
        with self._lock:
            if self._access and not force and time.time() < self._access_until:
                return self._access
            try:
                r = _request("POST", f"{self.session.url}{API}/auth/refresh",
                             {"refreshToken": self.session.refresh_token})
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403):
                    raise BookOrbitError("Your Book Orbit session has expired. Sign in again in Settings.") from e
                raise BookOrbitError(f"Book Orbit refresh failed: {_error_message(e)}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise BookOrbitError(f"Couldn't reach Book Orbit at {self.session.url}.") from e
            self._access, self._access_until = r["accessToken"], time.time() + 13 * 60
            if r.get("refreshToken"):   # rotates on every refresh: persist the new one
                self.session.refresh_token = r["refreshToken"]
                self.session.refresh_expires_at = r.get("refreshTokenExpiresAt")
                self._save(self.session)
            return self._access

    # Lorekeeper only ever reads from Book Orbit. The one POST is a query (a read with a body). Anything
    # that could change the user's library, progress or annotations is refused before a request is made.
    READ_ONLY_POSTS = frozenset({"/books/query"})

    def _call(self, method: str, path: str, body: dict | None = None, raw: bool = False, timeout: float = 30):
        if method != "GET" and not (method == "POST" and path.split("?")[0] in self.READ_ONLY_POSTS):
            raise BookOrbitError(f"Lorekeeper never writes to Book Orbit (refused {method} {path}).")
        for attempt in (0, 1):
            try:
                return _request(method, f"{self.session.url}{API}{path}", body, self._token(force=attempt == 1),
                                timeout=timeout, raw=raw)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                if e.code == 403:
                    raise BookOrbitError(f"Book Orbit refused: {_error_message(e)}") from e
                if e.code == 404:
                    raise BookOrbitError("Book Orbit couldn't find that item.") from e
                raise BookOrbitError(f"Book Orbit error {e.code}: {_error_message(e)}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise BookOrbitError(f"Couldn't reach Book Orbit at {self.session.url}.") from e
        raise BookOrbitError("Book Orbit rejected the session.")

    # ---- data ------------------------------------------------------------------------------

    def me(self) -> dict:
        return self._call("GET", "/auth/me")

    def libraries(self) -> list[dict]:
        """The user's book libraries (podcast libraries have nothing to index)."""
        r = self._call("GET", "/libraries")
        items = r if isinstance(r, list) else r.get("items", [])
        return [{"id": str(lib.get("id")), "name": lib.get("name") or f"Library {lib.get('id')}"}
                for lib in sorted(items, key=lambda lib: lib.get("displayOrder") or 0)
                if lib.get("type", "books") == "books"]

    def books(self) -> list[dict]:
        """Every book the user can see, trimmed to what Lorekeeper needs."""
        out: list[dict] = []
        page = 0
        while len(out) < MAX_BOOKS:
            r = self._call("POST", "/books/query", {
                "sort": [{"field": "title", "dir": "asc"}],
                "pagination": {"page": page, "size": PAGE_SIZE}, "collapseSeries": False})
            items = r.get("items", [])
            out += [trim_book(b) for b in items]
            if not items or len(out) >= r.get("total", 0):
                break
            page += 1
        return out

    def _open(self, path: str, headers: dict, timeout: float = 120):
        """A raw authenticated GET (for partial and streamed downloads); retries once on 401."""
        for attempt in (0, 1):
            req = urllib.request.Request(f"{self.session.url}{API}{path}", headers={
                "Authorization": f"Bearer {self._token(force=attempt == 1)}", "User-Agent": "Lorekeeper", **headers})
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                if e.code == 403:
                    raise BookOrbitError("Book Orbit won't let this account download books (it needs the "
                                         "download permission).") from e
                raise BookOrbitError(f"Book Orbit error {e.code}: {_error_message(e)}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise BookOrbitError(f"Couldn't reach Book Orbit at {self.session.url}.") from e
        raise BookOrbitError("Book Orbit rejected the session.")

    def download_text_epub(self, file_id) -> bytes:
        """The EPUB without audio or video, fetched with HTTP range requests where possible.

        An EPUB is a zip: reading its directory and then only the members we keep means a 900 MB
        read-along book costs a few megabytes. Servers without range support are streamed to disk.
        """
        path = f"/books/files/{urllib.parse.quote(str(file_id))}/download"
        probe = self._open(path, {"Range": "bytes=0-0"})
        total = None
        if probe.status == 206:
            m = re.match(r"bytes \d+-\d+/(\d+)", probe.headers.get("Content-Range", ""))
            total = int(m.group(1)) if m else None
        if total:
            probe.close()
            source = _RangeFile(lambda a, b: self._open(path, {"Range": f"bytes={a}-{b}"}).read(), total)
            return _slim(source)
        with tempfile.TemporaryFile() as tmp:   # no range support: stream the whole file to disk
            with probe:
                while chunk := probe.read(1 << 20):
                    tmp.write(chunk)
            tmp.seek(0)
            return _slim(tmp)

    def progress(self, book_id) -> list[dict]:
        """Reading progress per file of a book, trimmed to what position mapping needs.

        Newer servers answer `GET /reading-progress?bookId=` with grouped entries; older ones only
        have `GET /books/{id}/progress`, a flat row per file. Both are normalized to the grouped shape.
        """
        q = urllib.parse.quote(str(book_id))
        try:
            r = self._call("GET", f"/reading-progress?bookId={q}")
            items = (r.get("items") if isinstance(r, dict) else None) or []
        except BookOrbitError as e:
            if "find" not in str(e):
                raise
            r = self._call("GET", f"/books/{q}/progress")
            items = r if isinstance(r, list) else []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            if "text" in it or "kobo" in it:   # grouped shape
                text, kobo = it.get("text") or {}, it.get("kobo") or {}
                cfi, source, pct = text.get("cfi"), kobo.get("source"), kobo.get("contentPercent")
                koreader = it.get("koreader")
            else:                               # flat shape
                cfi, source, pct = it.get("cfi"), it.get("koboLocationSource"), it.get("koboContentSourceProgressPercent")
                koreader = it.get("koreaderProgress")
            out.append({"fileId": it.get("fileId"), "percentage": it.get("percentage"), "updatedAt": it.get("updatedAt"),
                        "text": {"cfi": cfi}, "kobo": {"source": source, "contentPercent": pct}, "koreader": koreader})
        return out

    def thumbnail(self, book_id) -> tuple[bytes, str]:
        return self._call("GET", f"/books/{urllib.parse.quote(str(book_id))}/thumbnail", raw=True, timeout=30)

    def sign_out(self) -> None:
        try:
            _request("POST", f"{self.session.url}{API}/auth/logout", {"refreshToken": self.session.refresh_token},
                     token=self._access, timeout=8)
        except Exception:
            pass   # the local copy is deleted either way


def trim_book(b: dict) -> dict:
    epub = next((f for f in b.get("files") or [] if str(f.get("format", "")).lower() == "epub"), None)
    memberships = b.get("seriesMemberships") or []
    series = []
    if memberships:
        for m in sorted(memberships, key=lambda m: m.get("displayOrder") or 0):
            series.append({"id": str(m.get("seriesId")), "name": m.get("seriesName"), "index": m.get("seriesIndex"),
                           "expected": m.get("expectedBookCount")})
    elif b.get("seriesId") or b.get("seriesName"):
        series.append({"id": str(b.get("seriesId") or b.get("seriesName")), "name": b.get("seriesName"),
                       "index": b.get("seriesIndex"), "expected": None})
    return {
        "id": str(b.get("id")), "libraryId": str(b.get("libraryId")), "title": b.get("title") or "Untitled",
        "authors": [a if isinstance(a, str) else a.get("name", "") for a in b.get("authors") or []],
        "series": series, "epubFileId": str(epub["id"]) if epub else None,
        "epubSize": epub.get("sizeBytes") if epub else None, "hasCover": bool(b.get("hasCover")),
        "coverVersion": b.get("coverVersion"), "language": b.get("language"), "isbn13": b.get("isbn13"),
        "updatedAt": b.get("updatedAt"),
    }


class _RangeFile(io.RawIOBase):
    """A read-only, seekable view of a remote file, fetched in blocks with HTTP range requests."""

    def __init__(self, fetch, size: int) -> None:
        self._fetch, self._size, self._pos = fetch, size, 0
        self._cache: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _block(self, n: int) -> bytes:
        if n not in self._cache:
            if len(self._cache) > 32:
                self._cache.pop(next(iter(self._cache)))
            start = n * RANGE_BLOCK
            self._cache[n] = self._fetch(start, min(self._size, start + RANGE_BLOCK) - 1)
        return self._cache[n]

    def readinto(self, b) -> int:
        if self._pos >= self._size:
            return 0
        want = min(len(b), self._size - self._pos)
        out = bytearray()
        while len(out) < want:
            n, off = divmod(self._pos + len(out), RANGE_BLOCK)
            out += self._block(n)[off: off + want - len(out)]
        b[:len(out)] = out
        self._pos += len(out)
        return len(out)

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._size - self._pos
        buf = bytearray(max(0, min(size, self._size - self._pos)))
        n = self.readinto(buf)
        return bytes(buf[:n])


def _slim(source) -> bytes:
    """Re-pack an EPUB without audio and video members (the package still lists them; readers don't care)."""
    out = io.BytesIO()
    try:
        with zipfile.ZipFile(source) as src, zipfile.ZipFile(out, "w") as dst:
            names = [i for i in src.infolist() if not i.filename.lower().endswith(MEDIA_EXTENSIONS)]
            names.sort(key=lambda i: i.filename != "mimetype")    # mimetype first, stored, per OCF
            for info in names:
                data = src.read(info.filename)
                if out.tell() + len(data) > MAX_SLIM_BYTES:
                    raise BookOrbitError("This book is too large to index, even without its audio.")
                dst.writestr(info.filename, data,
                             compress_type=zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED)
    except zipfile.BadZipFile as e:
        raise BookOrbitError("Book Orbit's file isn't a readable EPUB.") from e
    return out.getvalue()
