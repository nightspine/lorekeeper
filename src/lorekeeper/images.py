"""Image backends: one `draw` interface, Gemini and OpenAI behind it (stdlib only).

The model only draws what it is told; this layer sends a prompt (and up to `MAX_REFERENCES` pictures the
result should resemble), returns the bytes the provider sent back as they are (no conversion), and works
out the cost. A provider that declines for safety raises `ImageRefused` with its own wording; every other
failure is `llm.ProviderError`. Prices and ids live in `llm.IMAGE_PROVIDERS`.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from . import llm
from .llm import ProviderError

SHAPES = ("square", "portrait", "landscape")
QUALITIES = ("draft", "standard", "best")
MAX_REFERENCES = 6


@dataclass
class Drawn:
    data: bytes
    mime: str
    model: str
    cost: float                 # USD; 0 when the price is unknown
    width: int | None = None    # only when the provider reported them
    height: int | None = None


class ImageRefused(Exception):
    """The provider declined to draw this for safety reasons; `.reason` holds its wording."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ImageModel(Protocol):
    def draw(self, prompt: str, *, shape: str = "landscape", quality: str = "standard",
             references: Sequence[tuple[bytes, str]] = ()) -> Drawn: ...


def _check(shape: str, quality: str, references: Sequence) -> None:
    if shape not in SHAPES:
        raise ValueError(f"shape must be one of {', '.join(SHAPES)}")
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {', '.join(QUALITIES)}")
    if len(references) > MAX_REFERENCES:
        raise ValueError(f"at most {MAX_REFERENCES} reference pictures")


def multipart(fields: Sequence[tuple[str, str]], files: Sequence[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    """A multipart/form-data body: (name, value) fields, then (name, filename, mime, bytes) files.
    Returns (body, content type)."""
    boundary = "lorekeeper-" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields:
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    for name, filename, mime, data in files:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n").encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


@dataclass
class _Base:
    name: str
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    provider: str = ""
    timeout: float = 300
    retries: int = 2
    backoff: float = 2.0
    cancel: object = None       # optional () -> bool, checked before each request

    def _price(self, quality: str) -> float:
        return llm.image_price(self.provider, self.name, quality) or 0.0

    def _send(self, url: str, headers: dict, body: bytes) -> tuple[int, dict | str]:
        """POST with retries on connection errors, 429 and 5xx. Any other status is returned."""
        last = ""
        for attempt in range(self.retries + 1):
            if self.cancel and self.cancel():
                raise llm.Cancelled()
            try:
                status, out = llm._http("POST", url, headers, body, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = f"could not reach {self.base_url}: {getattr(e, 'reason', e)}"
            else:
                if status != 429 and status < 500:
                    return status, out
                last = f"HTTP {status}: {llm._error_text(out)}"
            if attempt < self.retries:
                time.sleep(self.backoff * 2 ** attempt)
        raise ProviderError(f"{self.name}: {last}")


# Reasons Gemini gives when it will not draw (finishReason on a candidate with no picture).
GEMINI_REFUSALS = {"IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "PROHIBITED_CONTENT", "SAFETY",
                   "BLOCKLIST", "SPII", "RECITATION", "IMAGE_OTHER"}


@dataclass
class Gemini(_Base):
    """`POST {base_url}/models/{model}:generateContent` with image output."""

    def draw(self, prompt: str, *, shape: str = "landscape", quality: str = "standard",
             references: Sequence[tuple[bytes, str]] = ()) -> Drawn:
        _check(shape, quality, references)
        spec = llm.IMAGE_PROVIDERS["gemini"]
        parts = [{"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}} for data, mime in references]
        parts.append({"text": prompt})
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"responseModalities": ["IMAGE"],
                                     "imageConfig": {"aspectRatio": spec["shapes"][shape],
                                                     "imageSize": spec["quality"][quality]}}}
        headers = {"Content-Type": "application/json", "User-Agent": "lorekeeper"}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        url = f"{self.base_url.rstrip('/')}/models/{self.name}:generateContent"
        status, data = self._send(url, headers, json.dumps(body).encode())
        if status != 200:
            raise ProviderError(f"{self.name}: HTTP {status}: {llm._error_text(data)}", status)
        if not isinstance(data, dict):
            raise ProviderError(f"{self.name}: the answer was not JSON")
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise ImageRefused(f"{block}: {(data['promptFeedback'].get('blockReasonMessage') or '').strip()}".rstrip(": "))
        cand = (data.get("candidates") or [{}])[0]
        said = []
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    raw = base64.b64decode(inline["data"], validate=False)
                except (binascii.Error, ValueError) as e:
                    raise ProviderError(f"{self.name}: the picture could not be decoded") from e
                # Reference images are billed as input tokens (about a cent each at most); the cost here is
                # the table's per-picture output price only.
                return Drawn(raw, inline.get("mimeType") or inline.get("mime_type") or "image/png", self.name,
                             self._price(quality))
            if part.get("text"):
                said.append(part["text"].strip())
        reason = cand.get("finishReason") or ""
        if reason in GEMINI_REFUSALS:
            raise ImageRefused(" ".join([reason, *said]).strip())
        raise ProviderError(f"{self.name}: no picture came back ({reason or 'no finish reason'}"
                            + (f": {' '.join(said)[:200]}" if said else "") + ")")


@dataclass
class OpenAIImages(_Base):
    """`POST {base_url}/images/generations` (JSON), or `/images/edits` (multipart) when references are given."""

    def draw(self, prompt: str, *, shape: str = "landscape", quality: str = "standard",
             references: Sequence[tuple[bytes, str]] = ()) -> Drawn:
        _check(shape, quality, references)
        spec = llm.IMAGE_PROVIDERS["openai"]
        size, level = spec["shapes"][shape], spec["quality"][quality]
        base = self.base_url.rstrip("/")
        headers = {"User-Agent": "lorekeeper"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if references:
            fields = [("model", self.name), ("prompt", prompt), ("quality", level), ("size", size),
                      ("output_format", "webp"), ("n", "1")]
            files = [("image[]", f"reference{i}{_ext(mime)}", mime, data) for i, (data, mime) in enumerate(references, 1)]
            body, headers["Content-Type"] = multipart(fields, files)
            url = f"{base}/images/edits"
        else:
            body = json.dumps({"model": self.name, "prompt": prompt, "quality": level, "size": size,
                               "output_format": "webp", "n": 1}).encode()
            headers["Content-Type"] = "application/json"
            url = f"{base}/images/generations"
        status, data = self._send(url, headers, body)
        if status != 200:
            err = data.get("error") if isinstance(data, dict) else None
            if status == 400 and isinstance(err, dict) and self._declined(err):
                raise ImageRefused(self._reason(err))
            raise ProviderError(f"{self.name}: HTTP {status}: {llm._error_text(data)}", status)
        rows = data.get("data") if isinstance(data, dict) else None
        b64 = rows[0].get("b64_json") if rows and isinstance(rows[0], dict) else None
        if not b64:
            raise ProviderError(f"{self.name}: no picture came back")
        try:
            raw = base64.b64decode(b64)
        except (binascii.Error, ValueError) as e:
            raise ProviderError(f"{self.name}: the picture could not be decoded") from e
        mime = "image/" + (data.get("output_format") or "webp")
        w = h = None
        m = re.fullmatch(r"(\d+)x(\d+)", str(data.get("size") or ""))
        if m:
            w, h = int(m[1]), int(m[2])
        return Drawn(raw, mime, self.name, self._cost(data.get("usage"), quality), w, h)

    @staticmethod
    def _declined(err: dict) -> bool:
        text = " ".join(str(err.get(k) or "") for k in ("type", "code", "message")).lower()
        return any(w in text for w in ("moderation", "safety", "content_policy", "content policy"))

    @staticmethod
    def _reason(err: dict) -> str:
        msg = str(err.get("message") or err.get("code") or "declined").strip()
        cats = (err.get("moderation_details") or {}).get("categories")
        return f"{msg} ({', '.join(cats)})" if cats else msg

    def _cost(self, usage, quality: str) -> float:
        """Token-billed models (a `rates` entry): worked out from the response's usage; else the table price."""
        rates = next((m.get("rates") for m in llm.IMAGE_PROVIDERS[self.provider or "openai"]["models"]
                      if m["id"] == self.name), None)
        if rates and isinstance(usage, dict):
            d = usage.get("input_tokens_details") or {}
            img_in, txt_in = d.get("image_tokens") or 0, d.get("text_tokens")
            if txt_in is None:
                txt_in = max(0, (usage.get("input_tokens") or 0) - img_in)
            out = usage.get("output_tokens") or 0
            if out:
                return (txt_in * rates[0] + img_in * rates[1] + out * rates[2]) / 1e6
        return self._price(quality)


def _ext(mime: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, "")
