"""kinship.js on its own: label parsing (good and noise labels from real indexes) and family trees."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_kinship_js_labels_and_families():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_kinship.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, "\n".join(line for line in r.stdout.splitlines() if not line.startswith("ok")) + r.stderr
