"""The Reader's chapter steps (static/js/steps.js), run in Node: a chapter is named only once the position enters it."""
import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_steps_js():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_steps.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
