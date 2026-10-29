"""Model backends for the generator: local Ollama, the Claude API and any OpenAI-compatible API
(OpenAI, Gemini, OpenRouter, Mistral, Groq, xAI, DeepSeek, or a server of your own).
Anything with `complete()` works. `PROVIDERS` is the one table of cloud providers."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_MODEL = "gemma4:12b-it-qat"
MAX_OUTPUT_TOKENS = 4096   # per call; extraction deltas are far smaller, so hitting this means a runaway


class Cancelled(Exception):
    """Raised when the caller's cancel check fires mid-request."""


class LLM(Protocol):
    name: str

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None) -> dict:
        """prefix: shared leading text (the passage) that backends may cache across calls."""
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

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None) -> dict:
        """One schema-constrained chat call. Thinking is off: it costs time and did not help extraction.

        A shared prefix goes first in the user message, so Ollama reuses its cached computation for
        the passage on the follow-up call. num_ctx stays fixed: changing it forces a model reload.
        The reply is streamed so a cancel request takes effect immediately; closing the connection
        makes Ollama stop generating. A reply cut off at max_output is skipped (returns {}), not retried.
        """
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

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None) -> dict:
        if self.cancel and self.cancel():
            raise Cancelled()
        p = self._model_params()
        # The passage is cached, so the fact-check call over the same passage reads it at a tenth of the price.
        content = ([{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}] if prefix else []) \
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
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            self.usage.failures += 1
            raise RuntimeError(f"{self.name}: response was not valid JSON (stop_reason={resp.stop_reason})") from e


# ---- OpenAI-compatible APIs ------------------------------------------------------------------

class ProviderError(RuntimeError):
    """An HTTP-level failure from a provider. The message never contains the API key."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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

    def _body(self, system: str, prompt: str, schema: dict, prefix: str | None) -> dict:
        if self._mode == "json_schema":
            fmt = {"type": "json_schema", "json_schema": {"name": "lorekeeper", "schema": schema, "strict": False}}
            sys_text = system
        else:
            fmt = {"type": "json_object"}
            sys_text = f"{system}\n\nAnswer with JSON matching this schema:\n{json.dumps(schema)}"
        body = {"model": self.name,
                "messages": [{"role": "system", "content": sys_text},
                             {"role": "user", "content": (prefix or "") + prompt}],
                "response_format": fmt}
        if "temperature" not in self._drop:
            body["temperature"] = 0
        body["max_completion_tokens" if "max_tokens" in self._drop else "max_tokens"] = self.max_output
        return body

    def complete(self, system: str, prompt: str, schema: dict, prefix: str | None = None) -> dict:
        t = time.time()
        try:
            for _ in range(5):
                status, data = self._post(self._body(system, prompt, schema, prefix))
                if status == 200:
                    break
                msg = _error_text(data)
                low = msg.lower()
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
    return [{"id": i, "label": label, "price": price, "tier": tier} for i, label, price, tier in rows]


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
                          ("gemini-2.5-pro", "Gemini 2.5 Pro", (1.25, 10.00), "strong")),
        "default": "gemini-2.5-flash", "cheap": "gemini-2.5-flash", "strong": "gemini-2.5-pro",
    },
    "openrouter": {
        "label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY", "key_url": "https://openrouter.ai/keys",
        "models": _models(("google/gemini-2.5-flash", "Gemini 2.5 Flash", (0.30, 2.50), "cheap"),
                          ("openai/gpt-5-mini", "GPT-5 mini", (0.25, 2.00), "cheap"),
                          ("anthropic/claude-sonnet-5-5", "Claude Sonnet 5.5", (2.00, 10.00), "strong"),
                          ("deepseek/deepseek-chat", "DeepSeek chat", (0.28, 0.42), "cheap")),
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
        "models": _models(("llama-3.1-8b-instant", "Llama 3.1 8B", (0.05, 0.08), "cheap"),
                          ("llama-3.3-70b-versatile", "Llama 3.3 70B", (0.59, 0.79), "strong")),
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
        "models": _models(("deepseek-chat", "DeepSeek chat", (0.28, 0.42), "cheap"),
                          ("deepseek-reasoner", "DeepSeek reasoner", (0.28, 0.42), "strong")),
        "default": "deepseek-chat", "cheap": "deepseek-chat", "strong": "deepseek-reasoner",
    },
    # Any OpenAI-compatible server (LM Studio, vLLM, llama.cpp server, a proxy): the user types the
    # base URL and model; the key is optional and no price is known.
    "custom": {
        "label": "Custom server", "kind": "openai", "base_url": None, "key_env": "CUSTOM_API_KEY", "key_url": None,
        "models": [], "default": None, "cheap": None, "strong": None,
    },
}


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
    ids = []
    for r in rows or []:
        mid = r.get("id") or r.get("name") if isinstance(r, dict) else r
        if isinstance(mid, str):
            ids.append(mid.removeprefix("models/"))
    return sorted(set(ids))


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
