"""Ollama backend over a fake streaming server: normal replies, instant cancel, output cap."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lorekeeper import llm


class FakeOllama(BaseHTTPRequestHandler):
    reply = '{"a": [1, 2]}'
    done_reason = "stop"
    delay = 0.0
    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for ch in self.reply:
                self.wfile.write((json.dumps({"message": {"content": ch}, "done": False}) + "\n").encode())
                self.wfile.flush()
                time.sleep(self.delay)
            self.wfile.write((json.dumps({"message": {"content": ""}, "done": True, "done_reason": self.done_reason,
                                          "prompt_eval_count": 50, "eval_count": len(self.reply)}) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            FakeOllama.requests.append("client closed")

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    FakeOllama.requests, FakeOllama.reply, FakeOllama.done_reason, FakeOllama.delay = [], '{"a": [1, 2]}', "stop", 0.0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_streamed_reply_is_parsed(server):
    o = llm.Ollama(url=server)
    assert o.complete("s", "p", {"type": "object"}, prefix="PASSAGE ") == {"a": [1, 2]}
    body = FakeOllama.requests[0]
    assert body["stream"] is True and body["options"]["num_predict"] == llm.MAX_OUTPUT_TOKENS
    assert body["messages"][1]["content"].startswith("PASSAGE ")
    assert (o.usage.prompt_tokens, o.usage.output_tokens) == (50, len(FakeOllama.reply))


def test_cancel_stops_mid_stream(server):
    FakeOllama.reply, FakeOllama.delay = '{"a": [' + "1, " * 200 + "1]}", 0.01
    flag = threading.Event()
    o = llm.Ollama(url=server, cancel=flag.is_set)
    threading.Timer(0.15, flag.set).start()
    t = time.time()
    with pytest.raises(llm.Cancelled):
        o.complete("s", "p", {"type": "object"})
    assert time.time() - t < 2          # the full reply would take > 6 s


def test_truncated_reply_is_skipped_not_retried(server):
    FakeOllama.reply, FakeOllama.done_reason = '{"a": [1, 2', "length"
    o = llm.Ollama(url=server)
    assert o.complete("s", "p", {"type": "object"}) == {}
    assert o.usage.truncated == 1 and len(FakeOllama.requests) == 1


def test_run_stops_mid_chunk_and_resumes(tmp_path):
    """A cancel during a chunk leaves the last checkpoint intact; resume finishes the book."""
    from lorekeeper import epub, generate

    from .test_generate import FakeLLM, make_epub

    class Cancelling(FakeLLM):
        cancel = None

        def complete(self, system, prompt, schema, prefix=None):
            if "In the morning Pike admitted" in (prefix or "") and self.cancel and self.cancel():
                raise llm.Cancelled()
            return super().complete(system, prompt, schema, prefix)

    book_path = make_epub(tmp_path / "b.epub")
    out = tmp_path / "o.lore.json"
    fake = Cancelling()
    report = generate.run(epub.read(book_path), fake, generate.Options(), book_path, out,
                          log=lambda _: None, should_stop=lambda: len(fake.calls) >= 2)
    assert report["stopped"] is True
    assert len(json.loads(out.read_text())["recaps"]) == 2          # chapter one only: story + chapter
    report = generate.run(epub.read(book_path), FakeLLM(), generate.Options(), book_path, out,
                          resume=True, log=lambda _: None)
    assert not report["stopped"] and len(json.loads(out.read_text())["recaps"]) == 3
