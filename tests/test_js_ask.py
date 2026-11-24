"""The Ask tab clears its thread exactly when the view reveals less (tests/js/run_ask.mjs)."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_ask_reveals_less():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_ask.mjs")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
