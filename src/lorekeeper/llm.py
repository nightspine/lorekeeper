"""Model backends for the generator: local Ollama, the Claude API and any OpenAI-compatible API
(OpenAI, Gemini, OpenRouter, Mistral, Groq, xAI, DeepSeek, or a server of your own).
Anything with `complete()` works. `PROVIDERS` is the one table of cloud providers."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol, Sequence

DEFAULT_MODEL = "gemma4:12b-it-qat"
MAX_OUTPUT_TOKENS = 4096   # per call; extraction deltas are far smaller, so hitting this means a runaway


class Cancelled(Exception):
    """Raised when the caller's cancel check fires mid-request."""


class NoVision(ValueError):
    """The model cannot take pictures as input: Ollama here, or a server that answered 400 naming the image input."""

    def __init__(self, message: str = "cannot see pictures") -> None:
        super().__init__(message)


class LLM(Protocol):
    name: str

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None,
                 images: Sequence[tuple[bytes, str]] = ()) -> dict:
        """prefix: shared leading text (the passage) that backends may cache across calls. images: (bytes, mime) pairs
        sent before the prompt, for a model that can see them (`vision`); their tokens are billed as input."""
        ...


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    failures: int = 0
    refusals: int = 0
    truncated: int = 0
    cost_usd: float = 0.0


@dataclass
class Ollama:
    name: str = DEFAULT_MODEL
    url: str = "http://localhost:11434"
    num_ctx: int = 16384
    timeout: float = 1800
    retries: int = 2
    max_output: int = MAX_OUTPUT_TOKENS
    usage: Usage = field(default_factory=Usage)
    cancel: object = None          # optional () -> bool, polled while tokens stream in

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None,
                 images: Sequence[tuple[bytes, str]] = ()) -> dict:
        """One schema-constrained chat call. Thinking is off: it costs time and did not help extraction.

        A shared prefix goes first in the user message, so Ollama reuses its cached computation for
        the passage on the follow-up call. num_ctx stays fixed: changing it forces a model reload.
        The reply is streamed so a cancel request takes effect immediately; closing the connection
        makes Ollama stop generating. A reply cut off at max_output is skipped (returns {}), not retried.
        Pictures are not sent to a local model: `images` raises NoVision.
        """
        if images:
            raise NoVision()
        body = json.dumps({
            "model": self.name,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": (prefix or "") + prompt}],
            "format": schema,
            "stream": True,
            "think": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": self.max_output},
        }).encode()
        last: Exception | None = None
        for _ in range(self.retries + 1):
            req = urllib.request.Request(f"{self.url}/api/chat", body, {"Content-Type": "application/json"})
            t = time.time()
            try:
                parts, final = [], {}
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    for line in resp:
                        if self.cancel and self.cancel():
                            raise Cancelled()
                        if not line.strip():
                            continue
                        msg = json.loads(line)
                        parts.append(msg.get("message", {}).get("content", ""))
                        if msg.get("done"):
                            final = msg
                            break
                self.usage.prompt_tokens += final.get("prompt_eval_count", 0)
                self.usage.output_tokens += final.get("eval_count", 0)
                if final.get("done_reason") == "length":
                    self.usage.truncated += 1
                    self.usage.calls += 1
                    return {}
                out = json.loads("".join(parts))
            except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError) as e:
                last = e
                self.usage.failures += 1
                continue
            finally:
                self.usage.seconds += time.time() - t
            self.usage.calls += 1
            return out
        raise RuntimeError(f"{self.name}: no valid response after {self.retries + 1} attempts: {last}")


# ---- Claude ---------------------------------------------------------------------------------

# USD per million tokens (input, output), Anthropic first-party rates.
CLAUDE_MODELS = {
    "claude-haiku-5-5": {"label": "Claude Haiku 5.5", "price": (0.10, 0.50)},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5", "price": (2.00, 10.00)},
    "claude-opus-5-5": {"label": "Claude Opus 5.5", "price": (4.00, 20.00)},
}
DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"


def strict_schema(schema: dict) -> dict:
    """Structured outputs require additionalProperties: false on every object."""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items()}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


@dataclass
class Claude:
    """Claude API backend with schema-constrained JSON output.

    Extraction is not a reasoning-heavy task, so thinking is kept off (or minimal where a model
    cannot turn it off): thinking tokens are billed as output. A refused passage returns an empty
    result, so the generator skips that chunk instead of failing the whole book.
    """

    name: str = DEFAULT_CLAUDE_MODEL
    max_tokens: int = 16000
    retries: int = 2
    usage: Usage = field(default_factory=Usage)
    client: object = None
    api_key: str | None = field(default=None, repr=False)
    cancel: object = None          # optional () -> bool, checked before each request

    def __post_init__(self) -> None:
        if self.client is None:
            import anthropic  # optional extra: uv sync --extra claude
            self.client = anthropic.Anthropic(api_key=self.api_key, max_retries=self.retries)

    def _model_params(self) -> dict:
        if self.name.startswith("claude-opus-5-5"):
            # Thinking cannot be disabled on Opus 5.5; low effort keeps it short.
            return {"output_config_extra": {"effort": "low"}, "fallbacks": True}
        if self.name.startswith("claude-sonnet-5-5"):
            return {"thinking": {"type": "between_tools"}, "fallbacks": True}
        if self.name.startswith("claude-haiku-5-5"):
            return {"thinking": {"type": "disabled"}}
        return {}

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None,
                 images: Sequence[tuple[bytes, str]] = ()) -> dict:
        if self.cancel and self.cancel():
            raise Cancelled()
        p = self._model_params()
        # Pictures go first, as base64 image blocks (their tokens are input tokens in `usage`). The passage is cached, so
        # the fact-check call over the same passage reads it at a tenth of the price.
        content = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
                   for data, mime in images] \
            + ([{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}] if prefix else []) \
            + [{"type": "text", "text": prompt}]
        kwargs = {
            "model": self.name,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": strict_schema(schema)},
                              **p.get("output_config_extra", {})},
        }
        if "thinking" in p:
            kwargs["thinking"] = p["thinking"]
        t = time.time()
        try:
            if p.get("fallbacks"):
                # On a policy decline, the API re-runs the request on a fallback model.
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except Exception:
            self.usage.failures += 1
            raise
        finally:
            self.usage.seconds += time.time() - t
        self.usage.calls += 1
        u = resp.usage
        fresh = u.input_tokens or 0
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        written = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.usage.prompt_tokens += fresh + read + written
        self.usage.output_tokens += u.output_tokens or 0
        price_in, price_out = CLAUDE_MODELS.get(self.name, {"price": (0, 0)})["price"]
        self.usage.cost_usd += ((fresh + 1.25 * written + 0.1 * read) * price_in
                                + (u.output_tokens or 0) * price_out) / 1e6
        if resp.stop_reason == "refusal":
            self.usage.refusals += 1
            return {}
        if resp.stop_reason == "max_tokens":
            # Cut off at the output limit: an empty result, as Ollama and the OpenAI-compatible backends
            # return, so the generator splits the part instead of failing the run.
            self.usage.truncated += 1
            return {}
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            self.usage.failures += 1
            raise RuntimeError(f"{self.name}: response was not valid JSON (stop_reason={resp.stop_reason})") from e


# ---- OpenAI-compatible APIs ------------------------------------------------------------------

class ProviderError(RuntimeError):
    """An HTTP-level failure from a provider. The message never contains the API key."""

    def __init__(self, message: str, status: int | None = None, sent: bool | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.sent = sent        # False: the request surely never reached the provider (safe to send again); None: unknown


def _http(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, dict | str]:
    """One request. Returns (status, parsed JSON or raw text); HTTP errors come back as a status, not an exception.
    Connection problems raise urllib.error.URLError / TimeoutError."""
    req = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
        e.close()
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def _error_text(data) -> str:
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        return str(err)[:300]
    if isinstance(data, list) and data and isinstance(data[0], dict):   # Gemini wraps errors in a list
        return _error_text(data[0])
    return str(data)[:300]


_IMAGE_INPUT = re.compile(r"image|vision|multimodal|modalit")


def _names_image_input(low: str) -> bool:
    """Whether a 400's message is about the picture sent ("image_url is only supported by certain models", "unknown variant
    `image_url`", "this model does not support vision")."""
    return bool(_IMAGE_INPUT.search(low))


def _parse_json(text: str) -> dict:
    """Model JSON, tolerating a code fence or chatter around the object."""
    text = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 < b:
            return json.loads(text[a:b + 1])
        raise


@dataclass
class OpenAICompatible:
    """Chat Completions backend for any OpenAI-compatible API (stdlib only).

    Asks for `response_format: json_schema`; a server that answers 400 gets one retry with plain
    `json_object` and the schema in the system prompt. Newer models that insist on
    `max_completion_tokens` or reject `temperature: 0` are handled the same way: the first 400
    says what to change, and the change is remembered for the rest of the run.
    """

    name: str
    base_url: str
    api_key: str | None = field(default=None, repr=False)
    provider: str = "custom"            # registry id, for the price lookup
    price: tuple | None = None          # (in, out) USD per million tokens; None = look up in PROVIDERS
    timeout: float = 600
    retries: int = 3
    backoff: float = 2.0                # seconds, doubled per retry
    max_output: int = 8192
    usage: Usage = field(default_factory=Usage)
    cancel: object = None               # optional () -> bool, checked before each request
    _mode: str = field(default="json_schema", repr=False)   # json_schema | json_object
    _drop: set = field(default_factory=set, repr=False)     # parameters this server rejected

    def _price(self) -> tuple | None:
        return self.price or model_price(self.provider, self.name)

    def _post(self, body: dict) -> tuple[int, dict | str]:
        headers = {"Content-Type": "application/json", "User-Agent": "lorekeeper"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body).encode()
        last: str = ""
        for attempt in range(self.retries + 1):
            if self.cancel and self.cancel():
                raise Cancelled()
            try:
                status, out = _http("POST", f"{self.base_url.rstrip('/')}/chat/completions", headers, data, self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                self.usage.failures += 1
                last = f"could not reach {self.base_url}: {getattr(e, 'reason', e)}"
            else:
                if status != 429 and status < 500:
                    return status, out
                self.usage.failures += 1
                last = f"HTTP {status}: {_error_text(out)}"
            if attempt < self.retries:
                time.sleep(self.backoff * 2 ** attempt)
        raise ProviderError(f"{self.name}: {last}")

    def _body(self, system: str, prompt: str, schema: dict, prefix: str | None,
              images: Sequence[tuple[bytes, str]] = ()) -> dict:
        if self._mode == "json_schema":
            fmt = {"type": "json_schema", "json_schema": {"name": "lorekeeper", "schema": schema, "strict": False}}
            sys_text = system
        else:
            fmt = {"type": "json_object"}
            sys_text = f"{system}\n\nAnswer with JSON matching this schema:\n{json.dumps(schema)}"
        user: str | list = (prefix or "") + prompt
        if images:      # data-URL parts before the text: OpenAI, Gemini's and OpenRouter's compatible APIs all take them
            user = [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
                    for data, mime in images] + [{"type": "text", "text": user}]
        body = {"model": self.name,
                "messages": [{"role": "system", "content": sys_text},
                             {"role": "user", "content": user}],
                "response_format": fmt}
        if "temperature" not in self._drop:
            body["temperature"] = 0
        body["max_completion_tokens" if "max_tokens" in self._drop else "max_tokens"] = self.max_output
        return body

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None,
                 images: Sequence[tuple[bytes, str]] = ()) -> dict:
        t = time.time()
        try:
            for _ in range(5):
                status, data = self._post(self._body(system, prompt, schema, prefix, images))
                if status == 200:
                    break
                msg = _error_text(data)
                low = msg.lower()
                if status == 400 and images and _names_image_input(low):
                    self.usage.failures += 1
                    raise NoVision(f"{self.name} cannot see pictures: {msg}")
                if status == 400 and "max_tokens" in low and "max_completion_tokens" in low and "max_tokens" not in self._drop:
                    self._drop.add("max_tokens")
                elif status == 400 and "temperature" in low and "temperature" not in self._drop:
                    self._drop.add("temperature")
                elif status == 400 and self._mode == "json_schema":
                    self._mode = "json_object"
                else:
                    self.usage.failures += 1
                    raise ProviderError(f"{self.name}: HTTP {status}: {msg}", status)
            else:
                raise ProviderError(f"{self.name}: the server kept rejecting the request")
        finally:
            self.usage.seconds += time.time() - t
        self.usage.calls += 1
        u = data.get("usage") or {}
        pin, pout = u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0
        self.usage.prompt_tokens += pin
        self.usage.output_tokens += pout
        price = self._price()
        if price:
            self.usage.cost_usd += (pin * price[0] + pout * price[1]) / 1e6
        choices = data.get("choices") or [{}]
        message = choices[0].get("message") or {}
        if choices[0].get("finish_reason") == "length":
            self.usage.truncated += 1
            return {}
        if message.get("refusal") or choices[0].get("finish_reason") == "content_filter":
            self.usage.refusals += 1
            return {}
        try:
            return _parse_json(message.get("content") or "")
        except json.JSONDecodeError as e:
            self.usage.failures += 1
            raise RuntimeError(f"{self.name}: response was not valid JSON") from e


# ---- Provider registry ------------------------------------------------------------------------
#
# THE ONLY PLACE to update model ids and prices. Every id and price below is the best known as of
# 2026-10-28 and must be verified against the provider's current list. A stale preset never blocks
# anything: the UI lists live models from GET {base_url}/models when the key works, and a model
# can be typed in. USD per million tokens (input, output); None = unknown ("price unknown" in the
# estimate, never $0).

def _models(*rows) -> list[dict]:
    """(id, label, price, tier[, extra]) rows; `extra` is a dict such as {"vision": False} for a text-only model."""
    return [{"id": r[0], "label": r[1], "price": r[2], "tier": r[3], **(r[4] if len(r) > 4 else {})} for r in rows]


# `vision: False` marks a preset that takes no picture input (the drawing-fault check needs one that does); every other
# preset sees pictures, and a live or custom model is tried (a 400 naming the image input raises NoVision).
_TEXT_ONLY = {"vision": False}

PROVIDERS: dict[str, dict] = {
    "claude": {
        "label": "Claude", "kind": "anthropic", "base_url": None, "key_env": "ANTHROPIC_API_KEY",
        "key_url": "https://console.anthropic.com/settings/keys",
        "models": [{"id": k, "label": v["label"], "price": v["price"],
                    "tier": "cheap" if "haiku" in k else "strong" if "sonnet" in k else "top"}
                   for k, v in CLAUDE_MODELS.items()],
        "default": "claude-haiku-5-5", "cheap": "claude-haiku-5-5", "strong": "claude-sonnet-5-5",
    },
    "openai": {
        "label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
        "models": _models(("gpt-5-mini", "GPT-5 mini", (0.25, 2.00), "cheap"),
                          ("gpt-5", "GPT-5", (1.25, 10.00), "strong"),
                          ("gpt-4.1-mini", "GPT-4.1 mini", (0.40, 1.60), "cheap"),
                          ("gpt-4.1", "GPT-4.1", (2.00, 8.00), "strong")),
        "default": "gpt-5-mini", "cheap": "gpt-5-mini", "strong": "gpt-5",
    },
    "gemini": {
        "label": "Gemini", "kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/apikey",
        "models": _models(("gemini-2.5-flash", "Gemini 2.5 Flash", (0.30, 2.50), "cheap"),
                          ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", (0.10, 0.40), "cheap"),
                          # gemini-2.5-pro answers 404 "no longer available to new users" (2026-11-25); prices for prompts
                          # up to 200K tokens (ai.google.dev, 2026-10-17)
                          ("gemini-3.1-pro-preview", "Gemini 3.1 Pro (preview)", (2.00, 12.00), "strong")),
        "default": "gemini-2.5-flash", "cheap": "gemini-2.5-flash", "strong": "gemini-3.1-pro-preview",
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY", "key_url": "https://openrouter.ai/keys",
        "models": _models(("google/gemini-2.5-flash", "Gemini 2.5 Flash", (0.30, 2.50), "cheap"),
                          ("openai/gpt-5-mini", "GPT-5 mini", (0.25, 2.00), "cheap"),
                          ("anthropic/claude-sonnet-5-5", "Claude Sonnet 5.5", (2.00, 10.00), "strong"),
                          ("deepseek/deepseek-chat", "DeepSeek chat", (0.28, 0.42), "cheap", _TEXT_ONLY)),
        "default": "google/gemini-2.5-flash", "cheap": "google/gemini-2.5-flash",
        "strong": "anthropic/claude-sonnet-5-5",
    },
    "mistral": {
        "label": "Mistral", "kind": "openai", "base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
        "key_url": "https://console.mistral.ai/api-keys",
        "models": _models(("mistral-small-latest", "Mistral Small", (0.20, 0.60), "cheap"),
                          ("mistral-large-latest", "Mistral Large", (2.00, 6.00), "strong")),
        "default": "mistral-small-latest", "cheap": "mistral-small-latest", "strong": "mistral-large-latest",
    },
    "groq": {
        "label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
        "key_url": "https://console.groq.com/keys",
        "models": _models(("llama-3.1-8b-instant", "Llama 3.1 8B", (0.05, 0.08), "cheap", _TEXT_ONLY),
                          ("llama-3.3-70b-versatile", "Llama 3.3 70B", (0.59, 0.79), "strong", _TEXT_ONLY)),
        "default": "llama-3.3-70b-versatile", "cheap": "llama-3.1-8b-instant", "strong": "llama-3.3-70b-versatile",
    },
    "xai": {
        "label": "xAI", "kind": "openai", "base_url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY",
        "key_url": "https://console.x.ai",
        "models": _models(("grok-4-fast", "Grok 4 Fast", (0.20, 0.50), "cheap"),
                          ("grok-4", "Grok 4", (3.00, 15.00), "strong")),
        "default": "grok-4-fast", "cheap": "grok-4-fast", "strong": "grok-4",
    },
    "deepseek": {
        "label": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY",
        "key_url": "https://platform.deepseek.com/api_keys",
        "models": _models(("deepseek-chat", "DeepSeek chat", (0.28, 0.42), "cheap", _TEXT_ONLY),
                          ("deepseek-reasoner", "DeepSeek reasoner", (0.28, 0.42), "strong", _TEXT_ONLY)),
        "default": "deepseek-chat", "cheap": "deepseek-chat", "strong": "deepseek-reasoner",
    },
    # Any OpenAI-compatible server (LM Studio, vLLM, llama.cpp server, a proxy): the user types the
    # base URL and model; the key is optional and no price is known.
    "custom": {
        "label": "Custom server", "kind": "openai", "base_url": None, "key_env": "CUSTOM_API_KEY", "key_url": None,
        "models": [], "default": None, "cheap": None, "strong": None,
    },
}


def vision(provider: str, model: str | None) -> bool | None:
    """Whether a text model can see pictures: False for Ollama and for a preset marked `vision: False`, True for any other
    preset, None for a model not in the table (a live or custom one: tried, and NoVision ends the check if it cannot)."""
    if provider == "ollama":
        return False
    for m in PROVIDERS.get(provider, {}).get("models", []):
        if m["id"] == model:
            return m.get("vision", True)
    return None


def model_price(provider: str, model: str) -> tuple | None:
    for m in PROVIDERS.get(provider, {}).get("models", []):
        if m["id"] == model:
            return m["price"]
    return None


def env_key(provider: str) -> str | None:
    """The provider's API key from its environment variable, if set."""
    var = PROVIDERS.get(provider, {}).get("key_env")
    return (os.environ.get(var) or "").strip() or None if var else None


def list_models(provider: str, api_key: str | None, base_url: str | None = None, timeout: float = 8) -> list[str]:
    """Live model ids from GET {base_url}/models. Raises ProviderError (auth, unreachable, unsupported)."""
    return sorted({mid for mid, _ in _model_rows(provider, api_key, base_url, timeout)})


def _model_rows(provider: str, api_key: str | None, base_url: str | None, timeout: float) -> list[tuple[str, dict]]:
    """(id without "models/", the row as sent) for every model GET {base_url}/models lists."""
    spec = PROVIDERS[provider]
    if spec["kind"] != "openai":
        raise ValueError("list_models is for OpenAI-compatible providers")
    url = (base_url or spec["base_url"] or "").rstrip("/")
    if not url:
        raise ProviderError("No base URL set.")
    headers = {"User-Agent": "lorekeeper"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        status, data = _http("GET", f"{url}/models", headers, None, timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
        raise ProviderError(f"Couldn't reach {url}: {getattr(e, 'reason', e)}") from e
    if status in (401, 403):
        raise ProviderError(f"{spec['label']} rejected this API key.", status)
    if status != 200:
        raise ProviderError(f"{spec['label']} answered HTTP {status}: {_error_text(data)}", status)
    rows = data.get("data") if isinstance(data, dict) else data
    if rows is None and isinstance(data, dict):
        rows = data.get("models")   # Gemini's native shape, just in case
    out = []
    for r in rows if isinstance(rows, list) else []:
        mid = r.get("id") or r.get("name") if isinstance(r, dict) else r
        if isinstance(mid, str):
            out.append((mid.removeprefix("models/"), r if isinstance(r, dict) else {}))
    return out


GEMINI_NATIVE = "https://generativelanguage.googleapis.com/v1beta"


def _gemini_rows(api_key: str | None, timeout: float) -> list[tuple[str, dict]]:
    """Gemini's native model list (it says what each model supports; the OpenAI-compatible one does not), every page."""
    out, token = [], ""
    for _ in range(10):
        q = "pageSize=1000" + (f"&pageToken={urllib.parse.quote(token)}" if token else "")
        try:
            status, data = _http("GET", f"{GEMINI_NATIVE}/models?{q}",
                                 {"User-Agent": "lorekeeper", "x-goog-api-key": api_key or ""}, None, timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
            raise ProviderError(f"Couldn't reach {GEMINI_NATIVE}: {getattr(e, 'reason', e)}") from e
        if status in (400, 401, 403) and not out:
            raise ProviderError("Gemini rejected this API key.", status)
        if status != 200 or not isinstance(data, dict):
            raise ProviderError(f"Gemini answered HTTP {status}: {_error_text(data)}", status)
        for r in data.get("models") or []:
            if isinstance(r, dict) and isinstance(r.get("name"), str):
                out.append((r["name"].removeprefix("models/"), r))
        token = data.get("nextPageToken") or ""
        if not isinstance(token, str) or not token:
            break
    return out


def _writes_text(provider: str, mid: str, row: dict, image_names: set[str]) -> bool:
    """Whether a listed model can write text, from what the provider's list says about it. Only where it says nothing does
    the id count, and then only to leave out the image models IMAGE_PROVIDERS lists for this provider."""
    if mid in {m["id"] for m in IMAGE_PROVIDERS.get(provider, {}).get("models", [])}:
        return False
    methods = row.get("supportedGenerationMethods")
    if isinstance(methods, list):        # Gemini's native list
        # generateContent is claimed by music, speech and image models too; the ones that think are the ones that write.
        # A model the provider names like a listed image model ("Nano Banana Pro") is that image model under another id.
        return "generateContent" in methods and row.get("thinking") is True and row.get("displayName") not in image_names
    arch = row.get("architecture")
    outputs = arch.get("output_modalities") if isinstance(arch, dict) else None   # OpenRouter
    if outputs is None:
        outputs = row.get("output_modalities", row.get("outputModalities"))
    if isinstance(outputs, list):
        return "text" in outputs
    caps = row.get("capabilities")
    if isinstance(caps, dict) and isinstance(caps.get("completion_chat"), bool):   # Mistral
        return caps["completion_chat"]
    return True


def live_models(provider: str, api_key: str | None, base_url: str | None = None, timeout: float = 8) -> list[dict]:
    """Every live model the provider lists, as {"id", "text"} sorted by id: `text` is whether it can write text (a prompt,
    an index), decided by `_writes_text`. Gemini is read from its native list. Raises ProviderError like list_models."""
    if provider == "gemini" and not base_url:
        rows = _gemini_rows(api_key, timeout)
    else:
        rows = _model_rows(provider, api_key, base_url, timeout)
    listed = {m["id"] for m in IMAGE_PROVIDERS.get(provider, {}).get("models", [])}
    image_names = {r.get("displayName") for mid, r in rows if mid in listed and r.get("displayName")}
    seen: dict[str, bool] = {}
    for mid, r in rows:
        seen[mid] = seen.get(mid, False) or _writes_text(provider, mid, r, image_names)
    return [{"id": mid, "text": seen[mid]} for mid in sorted(seen)]


def make(provider: str, model: str | None = None, api_key: str | None = None, base_url: str | None = None,
         **ollama_kwargs) -> LLM:
    """Build a backend by provider id: "ollama", or any key of PROVIDERS."""
    if provider == "ollama":
        return Ollama(name=model or DEFAULT_MODEL, **ollama_kwargs)
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"unknown provider {provider!r}")
    if spec["kind"] == "anthropic":
        return Claude(name=model or spec["default"], api_key=api_key)
    url = base_url or spec["base_url"]
    name = model or spec["default"]
    if not url:
        raise ValueError(f"{spec['label']} needs a base URL")
    if not name:
        raise ValueError(f"{spec['label']} needs a model name")
    return OpenAICompatible(name=name, base_url=url, api_key=api_key, provider=provider)


# ---- Image providers --------------------------------------------------------------------------
#
# The second table, beside PROVIDERS: who can draw. Claude cannot. The label, key variable and key
# page are PROVIDERS' (the same keys serve both), read through `image_spec`. `price` is USD per picture
# for each quality level: a number (every shape), a dict by shape where the provider's page gives one
# per shape, or None = not published ("price unknown", never $0 in an estimate). `levels` lists the
# quality levels a model offers (default all three); asking for another is a ValueError. `quality` and
# `shapes` map our levels (draft/standard/best) and shapes (square/portrait/landscape = 1:1, 3:4, 16:9)
# onto the provider's words, so a UI can show them; on Gemini draft and standard are both 1K on purpose.
# Verified 2026-11-24 against:
#   Gemini ids and prices   https://ai.google.dev/gemini-api/docs/pricing
#   Gemini request fields   https://ai.google.dev/gemini-api/docs/generate-content/image-generation
#   OpenAI ids and rates    https://developers.openai.com/api/docs/pricing and .../models/<id>
#   OpenAI sizes, quality   https://developers.openai.com/api/docs/guides/image-generation

def _images(*rows) -> list[dict]:
    return [{"id": i, "label": label, "price": price, "tier": tier, **extra} for i, label, price, tier, extra in rows]


_GEMINI_1K, _GEMINI_PRO = 0.0336, 0.134   # 1,120 image output tokens at $30/M; 1K and 2K both $0.134 on Pro


def _by_shape(square: float, tall: float, wide: float) -> dict:
    return {"square": square, "portrait": tall, "landscape": wide}


IMAGE_PROVIDERS: dict[str, dict] = {
    "gemini": {
        "kind": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "quality": {"draft": "1K", "standard": "1K", "best": "2K"},        # imageConfig.imageSize
        "shapes": {"square": "1:1", "portrait": "3:4", "landscape": "16:9"},   # imageConfig.aspectRatio
        "models": _images(
            ("gemini-nano-banana-2.1", "Nano Banana 2.1", {"draft": _GEMINI_1K, "standard": _GEMINI_1K, "best": 0.0504}, "cheap", {}),
            # the pricing page gives the 1K price only: no 2K level
            ("gemini-3.1-flash-lite-image", "Nano Banana 2 Lite", {"draft": _GEMINI_1K, "standard": _GEMINI_1K}, "cheap",
             {"levels": ("draft", "standard")}),
            ("gemini-3-pro-image", "Nano Banana Pro", {"draft": _GEMINI_PRO, "standard": _GEMINI_PRO, "best": _GEMINI_PRO}, "strong", {}),
        ),
        "default": "gemini-nano-banana-2.1", "cheap": "gemini-nano-banana-2.1", "strong": "gemini-3-pro-image",
    },
    "openai": {
        "kind": "openai", "base_url": "https://api.openai.com/v1",
        "quality": {"draft": "low", "standard": "medium", "best": "high"},
        "shapes": {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"},
        # gpt-image-2.6 is billed per token ($5/M text in, $8/M image in, $30/M image out; the docs publish no
        # per-picture table, only a calculator), so its price is None and the cost of a picture is worked out
        # from the response's usage with `rates`. Flare is the one the docs call "fast", hence the default.
        # gpt-image-2: the guide's per-picture table, one price per shape.
        "models": _images(
            ("gpt-image-2.6-flare", "GPT Image 2.6 Flare", {}, "cheap", {"rates": (5.0, 8.0, 30.0)}),
            ("gpt-image-2.6-sunburst", "GPT Image 2.6 Sunburst", {}, "strong", {"rates": (5.0, 8.0, 30.0)}),
            ("gpt-image-2", "GPT Image 2",
             {"draft": _by_shape(0.006, 0.005, 0.005), "standard": _by_shape(0.053, 0.041, 0.041),
              "best": _by_shape(0.211, 0.165, 0.165)}, "cheap", {}),
        ),
        "default": "gpt-image-2.6-flare", "cheap": "gpt-image-2.6-flare", "strong": "gpt-image-2.6-sunburst",
    },
}


def image_spec(provider: str) -> dict | None:
    """An image provider's entry with the label, key variable and key page it shares with PROVIDERS."""
    spec = IMAGE_PROVIDERS.get(provider)
    if spec is None:
        return None
    shared = {k: PROVIDERS[provider][k] for k in ("label", "key_env", "key_url")}
    return {**shared, **spec}


def image_model(provider: str, model: str) -> dict | None:
    """The table row for an image model, or None for an unknown provider or model."""
    for m in IMAGE_PROVIDERS.get(provider, {}).get("models", []):
        if m["id"] == model:
            return m
    return None


def image_price(provider: str, model: str, quality: str = "standard", shape: str | None = None) -> float | None:
    """USD for one picture, or None when the provider has not published it for that request
    (an unknown model, or a price given per shape when no shape is named)."""
    m = image_model(provider, model)
    price = m["price"].get(quality) if m else None
    if isinstance(price, dict):
        price = price.get(shape)
    return price


def nearest_quality(provider: str, model: str, quality: str) -> str:
    """The quality level an image model is drawn at when `quality` is asked for: `quality` when the model offers it (or the
    model is not in the table), else the nearest level it offers, a tie going up (draft becomes standard), and "best" the
    highest it has."""
    m = image_model(provider, model)
    order = ("draft", "standard", "best")
    if not m or quality not in order:
        return quality
    levels = [lv for lv in order if lv in (m.get("levels") or IMAGE_PROVIDERS[provider]["quality"])]
    if not levels or quality in levels:
        return quality
    if quality == "best":
        return levels[-1]
    i = order.index(quality)
    return min(levels, key=lambda lv: (abs(order.index(lv) - i), -order.index(lv)))


def make_image(provider: str, model: str | None = None, api_key: str | None = None, base_url: str | None = None):
    """An image backend (`images.ImageModel`) by provider id: a key of IMAGE_PROVIDERS."""
    if provider == "claude":
        raise ValueError("Claude cannot draw")
    spec = IMAGE_PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"no image provider {provider}")
    from . import images
    name = model or spec["default"]
    url = base_url or spec["base_url"]
    key = api_key or env_key(provider)
    cls = images.Gemini if spec["kind"] == "gemini" else images.OpenAIImages
    return cls(name=name, base_url=url, api_key=key, provider=provider)
