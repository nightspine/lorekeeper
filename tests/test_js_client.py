"""The JS reference client must agree with the Python reference: words, fingerprints, text model,
anchor resolution, gating through LorekeeperIndex, and every locator form (SPEC §3, §3.8, §7)."""

import importlib.util
import shutil
import subprocess

import pytest

from .conftest import ROOT

EPUB = ROOT / "corpus" / "pg244" / "pg244.epub"
FIXTURES = ROOT / "tests" / "js" / "fixtures"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.skipif(not EPUB.is_file(), reason="corpus/pg244/pg244.epub missing (see README)")
def test_js_client_matches_python_reference():
    spec = importlib.util.spec_from_file_location("make_fixtures", ROOT / "tests" / "js" / "make_fixtures.py")
    make = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make)
    make.write(FIXTURES)
    r = subprocess.run(["node", str(ROOT / "tests/js/run_client.mjs"), str(FIXTURES)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, "\n".join(line for line in r.stdout.splitlines() if not line.startswith("ok")) + r.stderr
