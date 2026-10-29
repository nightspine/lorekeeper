import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "sherlock-holmes"
CORPUS = ROOT / "corpus"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


@pytest.fixture
def scarlet() -> dict:
    return copy.deepcopy(_load("study-in-scarlet.json"))


@pytest.fixture
def four() -> dict:
    return copy.deepcopy(_load("sign-of-four.json"))


@pytest.fixture
def series() -> dict:
    return _load("series.json")


def corpus_epub(rel: str) -> Path:
    """EPUBs are downloaded, not committed (see README). Skip tests that need a missing one."""
    path = CORPUS / rel
    if not path.exists():
        pytest.skip(f"corpus file {rel} not downloaded")
    return path
