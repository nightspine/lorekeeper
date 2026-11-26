"""artestimate.js: the pictures estimate line gives a range while some looks are not checked."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_pictures_estimate_line():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_artestimate.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
