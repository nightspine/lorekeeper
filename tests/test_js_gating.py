"""The browser's gating module must pass the same conformance cases as the Python reference."""

import shutil
import subprocess

import pytest

from .conftest import ROOT


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_gating_conformance():
    r = subprocess.run(["node", str(ROOT / "tests/js/run_conformance.mjs"), str(ROOT / "conformance")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
