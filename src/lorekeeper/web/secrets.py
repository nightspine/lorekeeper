"""Provider credentials entered in the web UI.

Stored in one JSON file under the data directory, readable only by the owner (0600), written
atomically. Values never leave the server: the API returns masked forms only.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def mask(value: str) -> str:
    if len(value) <= 12:
        return "•" * len(value)
    return f"{value[:7]}…{value[-4:]}"


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def get(self, name: str) -> str | None:
        return self._read().get(name) or None

    def set(self, name: str, value: str) -> None:
        with self._lock:
            data = self._read()
            data[name] = value
            self._write(data)

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(name, None)
            self._write(data)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


class Keys:
    """Provider API keys: saved in Settings (the secret store), else the provider's environment variable.

    Claude keeps its old stored name (`anthropic_api_key`) so existing installs keep working; every
    other provider is stored as `{id}_api_key`. The custom server also keeps its base URL and model.
    """

    STORED = {"claude": "anthropic_api_key"}

    def __init__(self, store: SecretStore) -> None:
        self.store = store

    @classmethod
    def name(cls, provider: str) -> str:
        return cls.STORED.get(provider, f"{provider}_api_key")

    def stored(self, provider: str) -> str | None:
        return self.store.get(self.name(provider))

    def get(self, provider: str) -> str | None:
        from .. import llm
        return self.stored(provider) or llm.env_key(provider)

    def source(self, provider: str) -> str | None:
        from .. import llm
        if self.stored(provider):
            return "settings"
        return "environment" if llm.env_key(provider) else None

    def masked(self, provider: str) -> str | None:
        key = self.get(provider)
        return mask(key) if key else None

    def set(self, provider: str, key: str) -> None:
        self.store.set(self.name(provider), key)

    def delete(self, provider: str) -> None:
        self.store.delete(self.name(provider))

    def config(self, provider: str) -> dict:
        """{base_url, model} for the custom server (saved, else CUSTOM_BASE_URL / CUSTOM_MODEL); empty otherwise."""
        if provider != "custom":
            return {}
        return {"base_url": self.store.get("custom_base_url") or os.environ.get("CUSTOM_BASE_URL") or None,
                "model": self.store.get("custom_model") or os.environ.get("CUSTOM_MODEL") or None}

    def set_config(self, provider: str, base_url: str | None, model: str | None) -> None:
        if provider == "custom":
            for name, v in (("custom_base_url", base_url), ("custom_model", model)):
                self.store.set(name, v) if v else self.store.delete(name)

    def delete_config(self, provider: str) -> None:
        if provider == "custom":
            self.store.delete("custom_base_url")
            self.store.delete("custom_model")
