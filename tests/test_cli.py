"""CLI: the web command's API token and `docs export`."""

import pytest

from lorekeeper import cli


def _app(tmp_path, **kw):
    pytest.importorskip("fastapi")
    from lorekeeper.web import app as web_app
    return web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9",
                              allowed_hosts=("testserver",), auth=False, **kw)


def test_api_token_argument(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("LOREKEEPER_API_TOKEN", raising=False)
    with TestClient(_app(tmp_path, api_token="abc")) as c:
        assert c.get("/v0/index/x").status_code == 401
        assert c.get("/v0/index/x", headers={"Authorization": "Bearer abc"}).status_code != 401
        assert c.get("/v0/index/x", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/.well-known/lorekeeper").json()["auth"]["type"] == "bearer"


def test_api_token_env_fallback_and_flag_wins(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LOREKEEPER_API_TOKEN", "fromenv")
    with TestClient(_app(tmp_path)) as c:
        assert c.get("/v0/index/x").status_code == 401
        assert c.get("/v0/index/x", headers={"Authorization": "Bearer fromenv"}).status_code != 401
        assert c.get("/.well-known/lorekeeper").json()["auth"]["type"] == "bearer"
    with TestClient(_app(tmp_path, api_token="flag")) as c:
        assert c.get("/v0/index/x", headers={"Authorization": "Bearer fromenv"}).status_code == 401
        assert c.get("/v0/index/x", headers={"Authorization": "Bearer flag"}).status_code != 401


def test_no_token_is_open(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("LOREKEEPER_API_TOKEN", raising=False)
    with TestClient(_app(tmp_path)) as c:
        assert c.get("/.well-known/lorekeeper").json()["auth"]["type"] == "none"
        assert c.get("/v0/index/x").status_code != 401


def _fake_tree(tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / "docs" / "sub").mkdir(parents=True)
    (static / "docs" / "index.html").write_text("<html>")
    (static / "docs" / "sub" / "a.html").write_text("a")
    (static / "css").mkdir()
    (static / "css" / "base.css").write_text("x")
    (static / "css" / "other.css").write_text("y")
    (static / "fonts").mkdir()
    (static / "fonts" / "f.woff2").write_text("f")
    (static / "vendor").mkdir()
    (static / "vendor" / "marked.min.js").write_text("m")
    (static / "vendor" / "d3.min.js").write_text("d")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "SPEC.md").write_text("# spec")
    (repo / "README.md").write_text("# readme")
    monkeypatch.setattr(cli, "_static_root", lambda: static)
    monkeypatch.setattr(cli, "_repo_root", lambda: repo)


def test_docs_export_layout(tmp_path, monkeypatch, capsys):
    _fake_tree(tmp_path, monkeypatch)
    out = tmp_path / "site"
    assert cli.main(["docs", "export", str(out)]) == 0
    files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert files == {"docs/index.html", "docs/sub/a.html", "css/base.css", "fonts/f.woff2",
                     "vendor/marked.min.js", "spec.md", "readme.md"}
    assert "exported 7 files" in capsys.readouterr().out


def test_docs_export_refuses_nonempty_unless_forced(tmp_path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch)
    out = tmp_path / "site"
    out.mkdir()
    (out / "keep.txt").write_text("k")
    assert cli.main(["docs", "export", str(out)]) == 2
    assert not (out / "docs").exists()
    assert cli.main(["docs", "export", str(out), "--force"]) == 0
    assert (out / "docs" / "index.html").exists() and (out / "keep.txt").exists()


def test_docs_export_marked_optional(tmp_path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch)
    (tmp_path / "static" / "vendor" / "marked.min.js").unlink()
    out = tmp_path / "site"
    assert cli.main(["docs", "export", str(out)]) == 0
    assert not (out / "vendor").exists()


def test_docs_export_missing_docs(tmp_path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch)
    import shutil
    shutil.rmtree(tmp_path / "static" / "docs")
    assert cli.main(["docs", "export", str(tmp_path / "site")]) == 1
