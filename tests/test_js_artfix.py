"""artfix.js: the Fix flow's fault list, the body it sends and the lines for a picture's fixes."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_fix_flow_rules():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_artfix.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
