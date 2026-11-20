"""api.js builds the mentions query the Reader's view needs: order and fin always travel."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_mentions_query_and_memo_key():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_mentions_query.mjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
