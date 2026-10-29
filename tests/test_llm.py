"""Claude backend request shaping, cost accounting and refusal handling, with a stub client."""

import json
from types import SimpleNamespace as NS

import pytest

from lorekeeper import llm

SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "array", "items": {
    "type": "object", "properties": {"x": {"type": "string"}}}}}}


class StubMessages:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def create(self, **kw):
        self.calls.append(kw)
        return self.reply


def stub_client(text='{"a": []}', stop="end_turn", tokens=(1000, 200)):
    reply = NS(stop_reason=stop, content=[NS(type="text", text=text)],
               usage=NS(input_tokens=tokens[0], output_tokens=tokens[1],
                        cache_read_input_tokens=0, cache_creation_input_tokens=0))
    msgs = StubMessages(reply)
    return NS(messages=msgs, beta=NS(messages=msgs)), msgs


def test_strict_schema_closes_every_object():
    s = llm.strict_schema(SCHEMA)
    assert s["additionalProperties"] is False
    assert s["properties"]["a"]["items"]["additionalProperties"] is False


@pytest.mark.parametrize("model,thinking,beta", [
    ("claude-haiku-5-5", {"type": "disabled"}, False),
    ("claude-sonnet-5-5", {"type": "between_tools"}, True),
    ("claude-opus-5-5", None, True),
])
def test_request_shape_per_model(model, thinking, beta):
    client, msgs = stub_client()
    out = llm.Claude(name=model, client=client).complete("sys", "prompt", SCHEMA)
    assert out == {"a": []}
    kw = msgs.calls[0]
    assert kw["model"] == model and kw["system"] == "sys"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw.get("thinking") == thinking
    assert ("fallbacks" in kw) == beta
    if model == "claude-opus-5-5":
        assert kw["output_config"]["effort"] == "low"


def test_cost_accounting():
    client, _ = stub_client(tokens=(1_000_000, 100_000))
    c = llm.Claude(name="claude-sonnet-5-5", client=client)
    c.complete("s", "p", SCHEMA)
    assert c.usage.cost_usd == pytest.approx(2.0 + 1.0)
    assert (c.usage.prompt_tokens, c.usage.output_tokens, c.usage.calls) == (1_000_000, 100_000, 1)


def test_refusal_returns_empty_and_is_counted():
    client, _ = stub_client(text="", stop="refusal")
    c = llm.Claude(name="claude-haiku-5-5", client=client)
    assert c.complete("s", "p", SCHEMA) == {}
    assert c.usage.refusals == 1


def test_invalid_json_raises():
    client, _ = stub_client(text="not json")
    with pytest.raises(RuntimeError):
        llm.Claude(name="claude-haiku-5-5", client=client).complete("s", "p", SCHEMA)


def test_generator_runs_on_claude_backend(tmp_path):
    """The whole pipeline works unchanged on the Claude backend (scripted replies)."""
    from lorekeeper import epub, generate, validate
    from .test_generate import FakeLLM, make_epub

    script = FakeLLM()

    class ScriptedMessages:
        def create(self, **kw):
            schema = kw["output_config"]["format"]["schema"]
            blocks = kw["messages"][0]["content"]
            prefix = blocks[0]["text"] if len(blocks) > 1 else None
            assert len(blocks) == 1 or blocks[0]["cache_control"] == {"type": "ephemeral"}
            data = script.complete(kw["system"], blocks[-1]["text"], schema, prefix)
            return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(data))],
                      usage=NS(input_tokens=500, output_tokens=100, cache_read_input_tokens=0,
                               cache_creation_input_tokens=0))

    m = ScriptedMessages()
    backend = llm.Claude(name="claude-haiku-5-5", client=NS(messages=m, beta=NS(messages=m)))
    book_path = make_epub(tmp_path / "b.epub")
    out = tmp_path / "o.lore.json"
    report = generate.run(epub.read(book_path), backend, generate.Options(), book_path, out,
                          log=lambda _: None)
    assert report["validation_errors"] == []
    assert report["usage"]["cost_usd"] > 0
    assert not [e for e in validate.check_index(json.loads(out.read_text())) if e.level == "error"]


# ---- OpenAI-compatible backend, against an in-process fake server ------------------------------

import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class FakeOpenAI(BaseHTTPRequestHandler):
    """Scripted chat-completions server: `script` is a list of (status, body) answers, used in order."""
    script: list = []
    requests: list = []
    auth: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenAI.requests.append((self.path, body))
        FakeOpenAI.auth.append(self.headers.get("Authorization"))
        status, reply = FakeOpenAI.script.pop(0) if len(FakeOpenAI.script) > 1 else FakeOpenAI.script[0]
        raw = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        FakeOpenAI.auth.append(self.headers.get("Authorization"))
        status, reply = (200, {"data": [{"id": "m-b"}, {"id": "models/m-a"}]}) \
            if self.headers.get("Authorization") == "Bearer good" else (401, {"error": {"message": "bad key"}})
        raw = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def ok(content='{"a": []}', finish="stop", usage=(1000, 200), **message):
    return 200, {"choices": [{"message": {"content": content, **message}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


@pytest.fixture
def openai_server():
    FakeOpenAI.script, FakeOpenAI.requests, FakeOpenAI.auth = [ok()], [], []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


def compat(url, **kw):
    kw.setdefault("backoff", 0.01)
    return llm.OpenAICompatible(name="gpt-5-mini", base_url=url, api_key="k-secret", provider="openai", **kw)


def test_json_schema_request_and_cost_from_the_registry(openai_server):
    m = compat(openai_server)
    assert m.complete("sys", "question", SCHEMA, prefix="PASSAGE ") == {"a": []}
    path, body = FakeOpenAI.requests[0]
    assert path == "/v1/chat/completions" and FakeOpenAI.auth[0] == "Bearer k-secret"
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["temperature"] == 0 and body["max_tokens"] == m.max_output
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1]["content"] == "PASSAGE question"
    price = llm.model_price("openai", "gpt-5-mini")
    assert m.usage.cost_usd == pytest.approx((1000 * price[0] + 200 * price[1]) / 1e6)
    assert (m.usage.calls, m.usage.prompt_tokens, m.usage.output_tokens) == (1, 1000, 200)


def test_unpriced_model_costs_nothing_instead_of_a_wrong_number(openai_server):
    m = llm.OpenAICompatible(name="my-model", base_url=openai_server, provider="custom", backoff=0.01)
    m.complete("s", "p", SCHEMA)
    assert m.usage.cost_usd == 0 and llm.model_price("custom", "my-model") is None
    assert FakeOpenAI.auth[0] is None            # no key, no Authorization header


def test_bad_request_falls_back_to_json_object_with_the_schema_in_the_prompt(openai_server):
    FakeOpenAI.script = [(400, {"error": {"message": "response_format json_schema is not supported"}}), ok()]
    m = compat(openai_server)
    assert m.complete("sys", "p", SCHEMA) == {"a": []}
    first, second = FakeOpenAI.requests[0][1], FakeOpenAI.requests[1][1]
    assert first["response_format"]["type"] == "json_schema" and second["response_format"] == {"type": "json_object"}
    assert "JSON matching this schema" in second["messages"][0]["content"] and '"required"' in second["messages"][0]["content"]
    m.complete("sys", "p", SCHEMA)                 # remembered: no second probe
    assert FakeOpenAI.requests[2][1]["response_format"]["type"] == "json_object"


def test_newer_models_that_reject_max_tokens_and_temperature_are_adapted_to(openai_server):
    FakeOpenAI.script = [
        (400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}}),
        (400, {"error": {"message": "Unsupported value: 'temperature' does not support 0 with this model."}}),
        ok()]
    m = compat(openai_server)
    assert m.complete("s", "p", SCHEMA) == {"a": []}
    last = FakeOpenAI.requests[-1][1]
    assert "max_tokens" not in last and "temperature" not in last and last["max_completion_tokens"] == m.max_output
    assert last["response_format"]["type"] == "json_schema"


def test_finish_reason_length_is_a_truncated_empty_result(openai_server):
    FakeOpenAI.script = [ok('{"a": [', finish="length")]
    m = compat(openai_server)
    assert m.complete("s", "p", SCHEMA) == {}
    assert m.usage.truncated == 1 and len(FakeOpenAI.requests) == 1


def test_fenced_json_and_refusals(openai_server):
    FakeOpenAI.script = [ok('```json\n{"a": [{"x": "y"}]}\n```')]
    m = compat(openai_server)
    assert m.complete("s", "p", SCHEMA) == {"a": [{"x": "y"}]}
    FakeOpenAI.script = [ok(None, refusal="no")]
    assert m.complete("s", "p", SCHEMA) == {} and m.usage.refusals == 1


def test_rate_limit_is_retried_then_succeeds(openai_server):
    FakeOpenAI.script = [(429, {"error": {"message": "slow down"}}), (503, {"error": "busy"}), ok()]
    m = compat(openai_server)
    assert m.complete("s", "p", SCHEMA) == {"a": []}
    assert len(FakeOpenAI.requests) == 3 and m.usage.failures == 2


def test_persistent_failure_raises_without_leaking_the_key(openai_server):
    FakeOpenAI.script = [(429, {"error": {"message": "quota"}})]
    m = compat(openai_server, retries=1)
    with pytest.raises(llm.ProviderError) as e:
        m.complete("s", "p", SCHEMA)
    assert "quota" in str(e.value) and "k-secret" not in str(e.value)
    FakeOpenAI.script = [(401, {"error": {"message": "invalid key"}})]
    with pytest.raises(llm.ProviderError) as e:
        m.complete("s", "p", SCHEMA)
    assert e.value.status == 401 and len(FakeOpenAI.requests) == 3   # no retry on 401


def test_cancel_is_checked_before_each_request(openai_server):
    flag = threading.Event()
    m = compat(openai_server, cancel=flag.is_set)
    m.complete("s", "p", SCHEMA)
    flag.set()
    n = len(FakeOpenAI.requests)
    with pytest.raises(llm.Cancelled):
        m.complete("s", "p", SCHEMA)
    assert len(FakeOpenAI.requests) == n


def test_list_models_strips_prefixes_and_reports_bad_keys(openai_server, monkeypatch):
    assert llm.list_models("custom", "good", openai_server) == ["m-a", "m-b"]
    with pytest.raises(llm.ProviderError, match="rejected") as e:
        llm.list_models("openai", "bad", openai_server)
    assert e.value.status == 401


def test_registry_is_complete():
    assert list(llm.PROVIDERS)[:2] == ["claude", "openai"]
    for pid, spec in llm.PROVIDERS.items():
        assert spec["kind"] in ("anthropic", "openai") and spec["label"] and spec["key_env"]
        if pid == "custom":
            assert spec["models"] == [] and spec["base_url"] is None
            continue
        ids = {m["id"] for m in spec["models"]}
        assert {spec["default"], spec["cheap"], spec["strong"]} <= ids, pid
        if spec["kind"] == "openai":
            assert spec["base_url"].startswith("https://") and spec["key_url"]
    assert llm.PROVIDERS["gemini"]["key_env"] == "GEMINI_API_KEY"


def test_make_dispatches_on_the_registry(monkeypatch):
    m = llm.make("gemini", api_key="k")
    assert isinstance(m, llm.OpenAICompatible) and m.name == "gemini-2.5-flash" and "generativelanguage" in m.base_url
    c = llm.make("custom", "local-model", base_url="http://localhost:1234/v1")
    assert c.base_url == "http://localhost:1234/v1" and c.provider == "custom"
    assert isinstance(llm.make("ollama", "x"), llm.Ollama)
    with pytest.raises(ValueError):
        llm.make("custom", "m")                    # no base URL
    with pytest.raises(ValueError):
        llm.make("nope")
    monkeypatch.setenv("OPENAI_API_KEY", " sk-x ")
    assert llm.env_key("openai") == "sk-x" and llm.env_key("custom") is None


def test_cli_accepts_every_provider_and_reads_keys(tmp_path, monkeypatch, capsys):
    from lorekeeper import cli
    for pid in llm.PROVIDERS:
        monkeypatch.delenv(llm.PROVIDERS[pid]["key_env"], raising=False)
    args = argparse_for(["generate", "x.epub", "-o", "o.json", "--provider", "gemini"])
    assert args.provider == "gemini" and args.api_key_file is None and args.base_url is None
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        cli._make_model(args, None)
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    m = cli._make_model(args, None)
    assert isinstance(m, llm.OpenAICompatible) and m.api_key == "from-env" and m.name == "gemini-2.5-flash"
    keyfile = tmp_path / "key.txt"
    keyfile.write_text("from-file\n")
    args = argparse_for(["portraits", "i.json", "--provider", "openai", "--api-key-file", str(keyfile), "--model", "gpt-4.1"])
    assert cli._make_model(args, args.model).api_key == "from-file"
    args = argparse_for(["profiles", "i.json", "--provider", "custom", "--base-url", "http://localhost:1234/v1"])
    assert cli._make_model(args, "local").base_url == "http://localhost:1234/v1"
    with pytest.raises(SystemExit):
        argparse_for(["generate", "x.epub", "-o", "o", "--provider", "nope"])


def argparse_for(argv):
    from lorekeeper import cli
    captured = {}
    import argparse
    real = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        captured["a"] = real(self, args, namespace)
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = grab
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
    finally:
        argparse.ArgumentParser.parse_args = real
    if "a" not in captured:
        raise SystemExit(2)
    return captured["a"]
