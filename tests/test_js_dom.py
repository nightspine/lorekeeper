"""dom.js: h() never renders a function child as text."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_h_skips_function_children():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_dom.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
