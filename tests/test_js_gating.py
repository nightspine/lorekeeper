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
