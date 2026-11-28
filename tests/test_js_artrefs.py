"""artrefs.js: the rules of the person's own reference pictures for a drawing."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_artrefs_rules():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_artrefs.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
