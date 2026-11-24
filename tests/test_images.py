"""Image backends with the HTTP layer faked: request shapes, parsing, refusals, prices. No network."""

import base64
import json

import pytest

from lorekeeper import cli, images, llm

PNG = b"\x89PNG fake picture"
REF1, REF2 = (b"ref-one", "image/png"), (b"ref-two", "image/jpeg")


class Http:
    """Replaces llm._http: records each request, answers with the scripted replies."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.replies.pop(0)


def gemini_ok(mime="image/png"):
    return 200, {"candidates": [{"content": {"parts": [
        {"text": "here"}, {"inlineData": {"mimeType": mime, "data": base64.b64encode(PNG).decode()}}]},
        "finishReason": "STOP"}]}


def openai_ok(**extra):
    return 200, {"data": [{"b64_json": base64.b64encode(PNG).decode()}], **extra}


def gemini(monkeypatch, *replies, model="gemini-nano-banana-2.1"):
    http = Http(*replies)
    monkeypatch.setattr(llm, "_http", http)
    return llm.make_image("gemini", model, api_key="sekret"), http


def openai(monkeypatch, *replies, model="gpt-image-2.6-flare"):
    http = Http(*replies)
    monkeypatch.setattr(llm, "_http", http)
    return llm.make_image("openai", model, api_key="sekret"), http


# ---- Gemini ------------------------------------------------------------------------------------

def test_gemini_request_body_and_response(monkeypatch):
    m, http = gemini(monkeypatch, gemini_ok("image/jpeg"))
    pic = m.draw("an apple", shape="portrait", quality="best", references=[REF1, REF2])
    c = http.calls[0]
    assert c["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-nano-banana-2.1:generateContent"
    assert c["headers"]["x-goog-api-key"] == "sekret" and "sekret" not in c["url"]
    body = json.loads(c["body"])
    parts = body["contents"][0]["parts"]
    assert [list(p) for p in parts] == [["inlineData"], ["inlineData"], ["text"]]      # references first, prompt last
    assert parts[0]["inlineData"] == {"mimeType": "image/png", "data": base64.b64encode(b"ref-one").decode()}
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg"
    assert parts[2]["text"] == "an apple"
    cfg = body["generationConfig"]
    assert cfg["responseModalities"] == ["IMAGE"]
    assert cfg["imageConfig"] == {"aspectRatio": "3:4", "imageSize": "2K"}
    assert (pic.data, pic.mime, pic.model, pic.width) == (PNG, "image/jpeg", "gemini-nano-banana-2.1", None)
    assert pic.cost == pytest.approx(0.0504)


@pytest.mark.parametrize("shape,ratio", [("square", "1:1"), ("portrait", "3:4"), ("landscape", "16:9")])
@pytest.mark.parametrize("quality,size", [("draft", "1K"), ("standard", "1K"), ("best", "2K")])
def test_gemini_shape_and_quality(monkeypatch, shape, ratio, quality, size):
    m, http = gemini(monkeypatch, gemini_ok())
    m.draw("x", shape=shape, quality=quality)
    cfg = json.loads(http.calls[0]["body"])["generationConfig"]["imageConfig"]
    assert cfg == {"aspectRatio": ratio, "imageSize": size}


def test_gemini_refusals(monkeypatch):
    m, _ = gemini(monkeypatch, (200, {"candidates": [{"finishReason": "IMAGE_SAFETY"}]}))
    with pytest.raises(images.ImageRefused) as e:
        m.draw("x")
    assert "IMAGE_SAFETY" in e.value.reason
    m, _ = gemini(monkeypatch, (200, {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}))
    with pytest.raises(images.ImageRefused) as e:
        m.draw("x")
    assert "PROHIBITED_CONTENT" in e.value.reason
    m, _ = gemini(monkeypatch, (200, {"candidates": [{"content": {"parts": [{"text": "I can't."}]}, "finishReason": "SAFETY"}]}))
    with pytest.raises(images.ImageRefused) as e:
        m.draw("x")
    assert "SAFETY" in e.value.reason and "I can't." in e.value.reason


@pytest.mark.parametrize("reason", ["STOP", "IMAGE_OTHER", "RECITATION", "IMAGE_RECITATION"])
def test_gemini_no_picture_and_http_errors_are_provider_errors(monkeypatch, reason):
    m, _ = gemini(monkeypatch, (200, {"candidates": [{"finishReason": reason}]}))
    with pytest.raises(llm.ProviderError):
        m.draw("x")


def test_gemini_http_error_status(monkeypatch):
    m, _ = gemini(monkeypatch, (400, {"error": {"message": "bad key sekret"}}))
    with pytest.raises(llm.ProviderError) as e:
        m.draw("x")
    assert e.value.status == 400


def test_gemini_skips_thought_pictures(monkeypatch):
    def img(data, **kw):
        return {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(data).decode()}, **kw}
    reply = (200, {"candidates": [{"content": {"parts": [img(b"thought-1", thought=True), img(b"final one"),
                                                        img(b"thought-2", thought=True)]}}]})
    m, _ = gemini(monkeypatch, reply)
    assert m.draw("x").data == b"final one"
    m, _ = gemini(monkeypatch, (200, {"candidates": [{"content": {"parts": [img(b"t", thought=True)]},
                                                      "finishReason": "STOP"}]}))
    with pytest.raises(llm.ProviderError):
        m.draw("x")


def test_error_text_never_holds_the_key(monkeypatch):
    m, _ = gemini(monkeypatch, (403, {"error": {"message": "key sekret is not allowed"}}))
    with pytest.raises(llm.ProviderError) as e:
        m.draw("x")
    assert "sekret" not in str(e.value) and "***" in str(e.value)
    m, _ = openai(monkeypatch, (401, {"error": {"message": "Incorrect API key: sekret"}}))
    with pytest.raises(llm.ProviderError) as e:
        m.draw("x")
    assert "sekret" not in str(e.value)
    m, _ = openai(monkeypatch, (400, {"error": {"code": "moderation_blocked", "message": "sekret refused"}}))
    with pytest.raises(images.ImageRefused) as e:
        m.draw("x")
    assert "sekret" not in e.value.reason
    m, _ = gemini(monkeypatch, (200, {"promptFeedback": {"blockReason": "SAFETY", "blockReasonMessage": "sekret"}}))
    with pytest.raises(images.ImageRefused) as e:
        m.draw("x")
    assert "sekret" not in e.value.reason


def test_billed_request_is_not_sent_twice(monkeypatch):
    import socket
    import urllib.error
    monkeypatch.setattr(images.time, "sleep", lambda s: None)
    for failure in [(500, {"error": {"message": "boom"}}), TimeoutError("read timed out"),
                    ConnectionResetError("reset"), urllib.error.URLError(TimeoutError("timed out"))]:
        m, http = gemini(monkeypatch, failure, gemini_ok())
        if isinstance(failure, Exception):
            def raising(*a, _f=failure, **k):
                http.calls.append(a)
                raise _f
            monkeypatch.setattr(llm, "_http", raising)
        with pytest.raises(llm.ProviderError) as e:
            m.draw("x")
        assert len(http.calls) == 1 and e.value.sent is not False     # maybe drawn: no caller may send it again
    # never accepted: refused connection or unknown host is tried again
    calls = []

    def refused(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        if len(calls) == 2:
            raise urllib.error.URLError(socket.gaierror("no host"))
        return gemini_ok()
    monkeypatch.setattr(llm, "_http", refused)
    m = llm.make_image("gemini", api_key="k")
    assert m.draw("x").data == PNG and len(calls) == 3


def test_a_request_that_never_got_through_says_so(monkeypatch):
    import urllib.error
    monkeypatch.setattr(images.time, "sleep", lambda s: None)

    def refused(*a, **k):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    monkeypatch.setattr(llm, "_http", refused)
    with pytest.raises(llm.ProviderError) as e:
        llm.make_image("gemini", api_key="k").draw("x")
    assert e.value.sent is False
    m, http = gemini(monkeypatch, *[(429, {"error": {"message": "slow down"}})] * 3)
    with pytest.raises(llm.ProviderError) as e:
        m.draw("x")
    assert e.value.sent is False and e.value.status == 429


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(images.time, "sleep", lambda s: None)
    m, http = gemini(monkeypatch, (429, {"error": {"message": "slow down"}}), gemini_ok())
    assert m.draw("x").data == PNG and len(http.calls) == 2


def test_unpriced_model_costs_nothing(monkeypatch):
    m, _ = openai(monkeypatch, openai_ok())
    assert m.draw("x").cost == 0
    assert llm.image_price("openai", "gpt-image-2.6-flare", "standard", "square") is None
    assert llm.image_price("gemini", "gemini-3.1-flash-lite-image", "best") is None
    assert llm.image_price("nope", "x") is None
    assert llm.image_price("gemini", "made-up-model", "standard") is None


# ---- OpenAI ------------------------------------------------------------------------------------

def test_openai_generations_body(monkeypatch):
    m, http = openai(monkeypatch, openai_ok(size="1024x1536", output_format="webp"), model="gpt-image-2")
    pic = m.draw("an apple", shape="portrait", quality="draft")
    c = http.calls[0]
    assert c["url"] == "https://api.openai.com/v1/images/generations"
    assert c["headers"]["Authorization"] == "Bearer sekret" and c["headers"]["Content-Type"] == "application/json"
    assert json.loads(c["body"]) == {"model": "gpt-image-2", "prompt": "an apple", "quality": "low",
                                     "size": "1024x1536", "output_format": "webp", "n": 1}
    assert (pic.data, pic.mime, pic.width, pic.height) == (PNG, "image/webp", 1024, 1536)
    assert pic.cost == pytest.approx(0.005)           # the published portrait price, not the square one


@pytest.mark.parametrize("shape,quality,price", [("square", "draft", 0.006), ("landscape", "standard", 0.041),
                                                  ("portrait", "best", 0.165), ("square", "best", 0.211)])
def test_gpt_image_2_prices_follow_the_shape(shape, quality, price):
    assert llm.image_price("openai", "gpt-image-2", quality, shape) == pytest.approx(price)
    assert llm.image_price("openai", "gpt-image-2", quality) is None      # no shape named: not a published number


def test_openai_edits_multipart(monkeypatch):
    m, http = openai(monkeypatch, openai_ok())
    m.draw("the same man", shape="landscape", quality="best", references=[REF1, REF2])
    c = http.calls[0]
    assert c["url"] == "https://api.openai.com/v1/images/edits"
    ctype = c["headers"]["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1]
    body = c["body"]
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert body.count(f"--{boundary}\r\n".encode()) == 8            # 6 fields + 2 image parts
    assert body.count(b'name="image[]"') == 2
    assert b'name="image[]"; filename="reference1.png"\r\nContent-Type: image/png\r\n\r\nref-one\r\n' in body
    assert b'filename="reference2.jpg"\r\nContent-Type: image/jpeg\r\n\r\nref-two\r\n' in body
    for name, value in [("prompt", "the same man"), ("model", "gpt-image-2.6-flare"), ("quality", "high"),
                        ("size", "1536x1024"), ("output_format", "webp"), ("n", "1")]:
        assert f'name="{name}"\r\n\r\n{value}\r\n'.encode() in body


def test_openai_cost_from_token_usage_when_no_table_price(monkeypatch):
    usage = {"input_tokens": 1100, "input_tokens_details": {"text_tokens": 100, "image_tokens": 1000}, "output_tokens": 823}
    m, _ = openai(monkeypatch, openai_ok(usage=usage))
    pic = m.draw("x", references=[REF1])
    assert pic.cost == pytest.approx((100 * 5 + 1000 * 8 + 823 * 30) / 1e6)
    assert llm.image_price("openai", "gpt-image-2.6-flare", "standard") is None
    m, _ = openai(monkeypatch, openai_ok())                      # no usage in the answer: unknown, so 0
    assert m.draw("x").cost == 0


@pytest.mark.parametrize("err", [
    {"type": "image_generation_user_error", "code": "moderation_blocked", "message": "Your request was rejected by the safety system.",
     "moderation_details": {"moderation_stage": "input", "categories": ["violence"]}},
    {"type": "invalid_request_error", "code": "content_policy_violation", "message": "Blocked by content policy."},
])
def test_openai_refusals(monkeypatch, err):
    m, _ = openai(monkeypatch, (400, {"error": err}))
    with pytest.raises(images.ImageRefused) as e:
        m.draw("x")
    assert e.value.reason.startswith(err["message"][:20])
    if "moderation_details" in err:
        assert "violence" in e.value.reason


def test_openai_other_errors_are_provider_errors(monkeypatch):
    m, _ = openai(monkeypatch, (400, {"error": {"type": "invalid_request_error", "message": "Invalid size"}}))
    with pytest.raises(llm.ProviderError):
        m.draw("x")
    m, _ = openai(monkeypatch, (401, {"error": {"message": "bad key"}}))
    with pytest.raises(llm.ProviderError) as e:
        m.draw("x")
    assert e.value.status == 401


# ---- shared ------------------------------------------------------------------------------------

def test_argument_checks(monkeypatch):
    m, _ = gemini(monkeypatch)
    with pytest.raises(ValueError):
        m.draw("x", shape="wide")
    lite = llm.make_image("gemini", "gemini-3.1-flash-lite-image", api_key="k")
    with pytest.raises(ValueError, match="draft, standard"):
        lite.draw("x", quality="best")
    with pytest.raises(ValueError):
        m.draw("x", quality="ultra")
    with pytest.raises(ValueError):
        m.draw("x", references=[REF1] * 7)


def test_make_image_errors_and_dispatch(monkeypatch):
    with pytest.raises(ValueError, match="Claude cannot draw"):
        llm.make_image("claude")
    with pytest.raises(ValueError, match="no image provider mistral"):
        llm.make_image("mistral")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    m = llm.make_image("gemini")
    assert isinstance(m, images.Gemini) and m.name == "gemini-nano-banana-2.1" and m.api_key == "from-env"
    assert isinstance(llm.make_image("openai", "gpt-image-2", api_key="k"), images.OpenAIImages)


def test_table_is_consistent():
    for pid, spec in llm.IMAGE_PROVIDERS.items():
        ids = {m["id"] for m in spec["models"]}
        assert {spec["default"], spec["cheap"], spec["strong"]} <= ids
        assert not {"label", "key_env", "key_url"} & set(spec)            # shared fields come from PROVIDERS
        shared = llm.image_spec(pid)
        assert shared["key_env"] == llm.PROVIDERS[pid]["key_env"] and shared["label"] == llm.PROVIDERS[pid]["label"]
        for m in spec["models"]:
            assert set(m["price"]) <= {"draft", "standard", "best"}
            assert set(m.get("levels", m["price"])) >= set(m["price"])
    assert llm.image_price("gemini", "gemini-nano-banana-2.1", "draft") == pytest.approx(0.0336)
    assert llm.image_price("gemini", "gemini-3-pro-image", "best") == pytest.approx(0.134)


# ---- CLI ---------------------------------------------------------------------------------------

class FakeModel:
    def __init__(self, refuse=False):
        self.refuse, self.seen = refuse, None

    def draw(self, prompt, *, shape, quality, references):
        self.seen = (prompt, shape, quality, [d for d, _ in references])
        if self.refuse:
            raise images.ImageRefused("IMAGE_SAFETY")
        return images.Drawn(PNG, "image/png", "fake-model", 0.0336)


def test_cli_draw_writes_file(monkeypatch, tmp_path, capsys):
    fake = FakeModel()
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: fake)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(b"jpegbytes")
    out = tmp_path / "pic"
    assert cli.main(["draw", "an apple", "-o", str(out), "--shape", "square", "--quality", "draft", "--ref", str(ref)]) == 0
    assert (tmp_path / "pic.png").read_bytes() == PNG
    assert fake.seen == ("an apple", "square", "draft", [b"jpegbytes"])
    printed = capsys.readouterr().out
    assert "fake-model" in printed and "image/png" in printed and "$0.0336" in printed


def test_cli_draw_refusal_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: FakeModel(refuse=True))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert cli.main(["draw", "x", "-o", str(tmp_path / "p.png")]) == 2
    assert "IMAGE_SAFETY" in capsys.readouterr().err
    assert not (tmp_path / "p.png").exists()


def test_cli_draw_needs_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        cli.main(["draw", "x", "-o", str(tmp_path / "p.png"), "--provider", "openai"])


@pytest.mark.parametrize("name,mime,written", [
    ("castle.png", "image/webp", "castle.webp"), ("castle.jpg", "image/png", "castle.png"),
    ("castle.JPEG", "image/jpeg", "castle.JPEG"), ("castle.webp", "image/jpeg", "castle.jpg"),
    ("castle", "image/jpeg", "castle.jpg"), ("castle.v2", "image/png", "castle.v2.png"),
])
def test_cli_extension_follows_the_bytes(monkeypatch, tmp_path, capsys, name, mime, written):
    class M(FakeModel):
        def draw(self, *a, **k):
            return images.Drawn(PNG, mime, "fake-model", 0.0336)
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: M())
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert cli.main(["draw", "x", "-o", str(tmp_path / name)]) == 0
    assert (tmp_path / written).read_bytes() == PNG
    assert str(tmp_path / written) in capsys.readouterr().out


def test_cli_creates_the_output_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "make_image", lambda *a, **k: FakeModel())
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert cli.main(["draw", "x", "-o", str(tmp_path / "a" / "b" / "p.png")]) == 0
    assert (tmp_path / "a" / "b" / "p.png").exists()


def test_cli_file_errors_are_messages(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with pytest.raises(SystemExit, match="error:"):
        cli.main(["draw", "x", "-o", str(tmp_path / "p.png"), "--ref", str(tmp_path / "missing.png")])
    with pytest.raises(SystemExit, match="error:"):
        cli.main(["draw", "x", "-o", str(tmp_path / "p.png"), "--api-key-file", str(tmp_path / "nokey")])
