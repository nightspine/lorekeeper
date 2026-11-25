"""artprompt.js: a style change in the picture sheet swaps the style sentence with no new prompt."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_picture_prompt_restyle():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_artprompt.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
