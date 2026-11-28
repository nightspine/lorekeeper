"""styleref.js: the checks on a style reference picture and the describe cost line."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_style_reference_checks():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_styleref.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
