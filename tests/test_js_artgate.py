"""artgate.js: the Reader's spoiler rules for pictures (frontier, finished books, unknown and colliding anchors, world note)."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_picture_gating():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_artgate.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
