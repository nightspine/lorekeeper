"""The browser's gating module must pass the same conformance cases as the Python reference."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_gating_conformance():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_conformance.mjs"), str(ROOT / "conformance")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_view_copies_book_kind(scarlet, series):
    import json
    script = ("import {view} from %r; const [idx, ser] = JSON.parse(process.argv[1]);"
              "const pos = {book: 'study-in-scarlet', anchor: 'ch2.para51'};"
              "const a = view([idx], pos, ser), b = view([{...idx, bookKind: 'self-help'}], pos, ser);"
              "console.log(JSON.stringify([a.bookKind ?? null, b.bookKind]));"
              ) % (ROOT / "src/lorekeeper/web/static/js/gating.js").as_uri()
    r = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps([scarlet, series])],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [None, "self-help"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_view_matches_python_for_event_settings(scarlet, series):
    import copy
    import json

    from lorekeeper.gating import Position, view as pyview

    ix = copy.deepcopy(scarlet)
    for ev in ix["events"]:
        if ev["id"] == "sis.ev.meeting":
            ev["setting"] = {"text": "a laboratory", "place": "baker-street"}
        if ev["id"] == "sis.ev.maiwand":
            ev["setting"] = {"text": "the battlefield", "place": "maiwand"}
    script = ("import {view} from %r; const [idx, ser, anchor] = JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(view([idx], {book: 'study-in-scarlet', anchor}, ser)));"
              ) % (ROOT / "src/lorekeeper/web/static/js/gating.js").as_uri()
    for anchor in ("ch1.para05", "ch1.para35", "ch1.para52"):
        r = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps([ix, series, anchor])],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == pyview([ix], Position("study-in-scarlet", anchor), series)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_empty_setting_is_absent(scarlet, series):
    import copy
    import json

    from lorekeeper.gating import Position, view as pyview

    ix = copy.deepcopy(scarlet)
    for ev in ix["events"]:
        if ev["id"] == "sis.ev.meeting":
            ev["setting"] = {}
    script = ("import {view} from %r; const [idx, ser] = JSON.parse(process.argv[1]);"
              "console.log(JSON.stringify(view([idx], {book: 'study-in-scarlet', anchor: 'ch1.para52'}, ser)));"
              ) % (ROOT / "src/lorekeeper/web/static/js/gating.js").as_uri()
    r = subprocess.run(["node", "--input-type=module", "-e", script, json.dumps([ix, series])],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == pyview([ix], Position("study-in-scarlet", "ch1.para52"), series)
    assert all("setting" not in e for e in out["events"] if e["id"] == "sis.ev.meeting")
