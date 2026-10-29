"""Admin sign-in: bootstrap, sessions, password rules, the gate, the throttle and the reset command."""

import json
import stat

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from lorekeeper import cli  # noqa: E402
from lorekeeper.web import app as web_app  # noqa: E402
from lorekeeper.web import auth  # noqa: E402

NEW = "a much better password"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 10)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, **kw):
    out: list[str] = []
    app = web_app.create_app(tmp_path / "data", [], [], ollama_url="http://127.0.0.1:9",
                             allowed_hosts=("testserver",), log=out.append, **kw)
    return app, out


def client(app):
    return TestClient(app, headers={"X-Lorekeeper": "1"})


def printed_password(out):
    return out[out.index("First start. Sign in as  admin  with the password") + 1].strip()


@pytest.fixture
def env(tmp_path):
    app, out = make(tmp_path)
    return app, out, printed_password(out)


def sign_in(c, password, username="admin"):
    return c.post("/api/auth/login", json={"username": username, "password": password})


def test_bootstrap_prints_once_and_stores_only_a_hash(tmp_path):
    app, out = make(tmp_path)
    pw = printed_password(out)
    path = tmp_path / "data" / "auth.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert pw not in raw
    user = json.loads(raw)["users"]["admin"]
    assert user["mustChangePassword"] is True and user["hash"].startswith("scrypt$1024$8$3$")
    assert out
    app2, out2 = make(tmp_path)
    assert out2 == []


def test_initial_password_from_environment(tmp_path):
    app, out = make(tmp_path, initial_password="from the compose file")
    assert printed_password(out) == "from the compose file"
    with client(app) as c:
        assert sign_in(c, "from the compose file").status_code == 200
        assert c.get("/api/auth/session").json()["user"]["mustChangePassword"] is True


def test_session_shapes(tmp_path, env):
    app, out, pw = env
    with client(app) as c:
        assert c.get("/api/auth/session").json() == {"auth": True, "user": None, "setup": True}
        sign_in(c, pw)
        assert c.get("/api/auth/session").json() == {
            "auth": True, "user": {"name": "admin", "mustChangePassword": True}}
    off = web_app.create_app(tmp_path / "off", [], [], allowed_hosts=("testserver",), auth=False)
    with client(off) as c:
        assert c.get("/api/auth/session").json() == {"auth": False}
        assert c.post("/api/auth/login", json={}).status_code == 404
        assert c.get("/api/library").status_code == 200


def test_public_and_protected_routes(env):
    app, out, pw = env
    with client(app) as c:
        for path in ("/", "/.well-known/lorekeeper", "/docs/", "/static/css/base.css", "/api/auth/session"):
            assert c.get(path).status_code == 200, path
        for path in ("/api/library", "/api/ollama", "/api/docs", "/openapi.json"):
            r = c.get(path)
            assert r.status_code == 401 and r.json() == {"detail": "Sign in required"}, path


def test_login_and_cookie_flags(env):
    app, out, pw = env
    with client(app) as c:
        assert sign_in(c, "wrong password!").status_code == 401
        assert sign_in(c, "nobody", "ghost").json() == {"detail": "Wrong username or password"}
        r = sign_in(c, pw, " Admin ")
        assert r.status_code == 200 and r.json()["user"]["name"] == "admin"
        cookie = r.headers["set-cookie"]
        assert "lorekeeper_session=" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
        assert "samesite=lax" in cookie.lower() and "Max-Age=2592000" in cookie
        assert "secure" not in cookie.lower()
        r = c.post("/api/auth/login", json={"username": "admin", "password": pw},
                   headers={"X-Forwarded-Proto": "https"})
        assert "secure" in r.headers["set-cookie"].lower()
        assert pw not in json.dumps(json.loads(app.state.auth.path.read_text())["sessions"])


def test_must_change_gate_and_policy(env):
    app, out, pw = env
    with client(app) as c:
        sign_in(c, pw)
        r = c.get("/api/library")
        assert r.status_code == 403 and r.json()["code"] == "password-change-required"
        assert c.get("/api/auth/session").status_code == 200
        assert c.post("/api/auth/password", json={"current": pw, "new": "short"}).json() == {
            "detail": "Use at least 12 characters."}
        assert c.post("/api/auth/password", json={"current": pw, "new": "x" * 129}).json() == {
            "detail": "Use at most 128 characters."}
        assert c.post("/api/auth/password", json={"current": pw, "new": pw}).status_code == 400
        r = c.post("/api/auth/password", json={"current": "not the current one", "new": NEW})
        assert r.status_code == 401 and r.json() == {"detail": "Wrong current password"}
        r = c.post("/api/auth/password", json={"current": pw, "new": NEW})
        assert r.status_code == 200 and r.json()["user"]["mustChangePassword"] is False
        assert c.get("/api/library").status_code == 200
    with client(app) as c2:
        assert c2.get("/api/auth/session").json()["setup"] is False
        assert sign_in(c2, pw).status_code == 401
        assert sign_in(c2, NEW).status_code == 200


def test_same_password_message(env):
    app, out, pw = env
    with client(app) as c:
        sign_in(c, pw)
        r = c.post("/api/auth/password", json={"current": pw, "new": pw + "!!!!!!!!!!!!"})
        assert r.status_code == 200
        pw2 = pw + "!!!!!!!!!!!!"
        r = c.post("/api/auth/password", json={"current": pw2, "new": pw2})
        assert r.status_code == 400 and r.json() == {"detail": "Choose a different password."}


def test_password_change_signs_out_other_sessions_and_rotates(env):
    app, out, pw = env
    with client(app) as a, client(app) as b:
        sign_in(a, pw)
        sign_in(b, pw)
        old = a.cookies.get("lorekeeper_session")
        assert a.post("/api/auth/password", json={"current": pw, "new": NEW}).status_code == 200
        assert a.cookies.get("lorekeeper_session") != old
        assert a.get("/api/library").status_code == 200
        assert b.get("/api/library").status_code == 401


def test_logout(env):
    app, out, pw = env
    with client(app) as c:
        sign_in(c, pw)
        c.post("/api/auth/password", json={"current": pw, "new": NEW})
        token = c.cookies.get("lorekeeper_session")
        assert c.post("/api/auth/logout").json() == {"ok": True}
        assert c.get("/api/library").status_code == 401
        c.cookies.set("lorekeeper_session", token)
        assert c.get("/api/library").status_code == 401


def test_login_throttle(tmp_path):
    clock = Clock()
    app, out = make(tmp_path, auth_clock=clock)
    pw = printed_password(out)
    with client(app) as c:
        for _ in range(5):
            assert sign_in(c, "bad password").status_code == 401
        r = sign_in(c, pw)
        assert r.status_code == 429 and r.headers["retry-after"] == "1"
        assert r.json()["detail"] == "Too many attempts. Try again in 1 seconds."
        clock.t += 2
        assert sign_in(c, "bad password").status_code == 401   # sixth failure: 2 s lock
        assert sign_in(c, pw).status_code == 429
        clock.t += 3
        assert sign_in(c, pw).status_code == 200               # success clears the counter
        for _ in range(5):
            sign_in(c, "bad password")
        clock.t += 2
        assert sign_in(c, pw).status_code == 200


def test_unknown_user_does_not_raise(env):
    app, out, pw = env
    with client(app) as c:
        assert sign_in(c, "x", "").status_code == 401
        assert c.post("/api/auth/login", json={}).status_code == 401
        assert c.post("/api/auth/login", content=b"nope").status_code == 400


def test_idle_session_expires(env):
    app, out, pw = env
    with client(app) as c:
        sign_in(c, pw)
        path = app.state.auth.path
        data = json.loads(path.read_text())
        for s in data["sessions"].values():
            s["seen"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(data))
        assert c.get("/api/auth/session").json()["user"] is None


def test_reset_password_cli(tmp_path, capsys):
    app, out = make(tmp_path)
    pw = printed_password(out)
    data = str(tmp_path / "data")
    with client(app) as c:
        sign_in(c, pw)
        c.post("/api/auth/password", json={"current": pw, "new": NEW})
        assert c.get("/api/library").status_code == 200
        assert cli.main(["reset-password", "--data", data]) == 0
        shown = capsys.readouterr().out
        new_pw = [ln.strip() for ln in shown.splitlines()][2]
        assert new_pw and new_pw != NEW and "You will be asked" in shown
        assert c.get("/api/library").status_code == 401
        assert sign_in(c, NEW).status_code == 401
        assert sign_in(c, new_pw).json()["user"]["mustChangePassword"] is True


def test_reset_password_without_admin(tmp_path, capsys):
    assert cli.main(["reset-password", "--data", str(tmp_path / "empty")]) == 2
    assert "no admin yet" in capsys.readouterr().err


def test_hash_roundtrip_and_format():
    h = auth.hash_password("correct horse battery")
    scheme, n, r, p, salt, digest = h.split("$")
    assert (scheme, n, r, p) == ("scrypt", "1024", "8", "3") and salt and digest
    assert auth.verify_password("correct horse battery", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")
    assert h != auth.hash_password("correct horse battery")   # salted
