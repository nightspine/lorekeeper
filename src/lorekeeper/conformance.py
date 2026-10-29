"""Conformance cases (SPEC.md §10): inputs + the exact view a client must produce.

A case file looks like:

    {"description": "...",
     "series": "../examples/sherlock-holmes/series.json",   # optional, relative to the case
     "indexes": ["../examples/sherlock-holmes/study-in-scarlet.json"],
     "order": null, "finished": null,
     "position": {"book": "study-in-scarlet", "anchor": "ch2.para21"},
     "expect": { ...view... }}

Locator cases live in `<dir>/locators/` (SPEC §3.7). They need a real EPUB, so a case whose edition is
missing from this checkout is skipped, not failed:

    {"description": "...", "edition": "corpus/pg244/pg244.epub",
     "index": "examples/sherlock-holmes/study-in-scarlet.json",       # both relative to the repository root
     "locator": {"cfi": "epubcfi(/6/10!/4/2/14/1:12)"},
     "expect": {"seq": 36, "exact": true, "finished": "ch1.para05", "inclusive": "ch1.para06"}}

`finished` is the current anchor in the §3.7 "finished" mode (the block being read does not count);
`inclusive` counts it. Anchors come from fingerprint resolution against `edition`, never from seq hints.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import epub, gating, locators


def _compute(case: dict, base: Path) -> dict:
    load = lambda p: json.loads((base / p).read_text())  # noqa: E731
    series = load(case["series"]) if case.get("series") else None
    finished = set(case["finished"]) if case.get("finished") is not None else None
    pos = gating.Position(case["position"]["book"], case["position"]["anchor"])
    return gating.view([load(p) for p in case["indexes"]], pos, series, case.get("order"), finished)


def cases(directory: str | Path) -> list[Path]:
    return sorted(Path(directory).glob("*.json"))


def check(path: Path) -> tuple[bool, dict, dict]:
    case = json.loads(path.read_text())
    actual = _compute(case, path.parent)
    return actual == case["expect"], case["expect"], actual


def regenerate(path: Path) -> None:
    case = json.loads(path.read_text())
    case["expect"] = _compute(case, path.parent)
    path.write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n")


# ---- locator cases -----------------------------------------------------------------------------

def locator_cases(directory: str | Path) -> list[Path]:
    return sorted((Path(directory) / "locators").glob("*.json"))


@lru_cache(maxsize=8)
def _edition(path: str) -> epub.Book:
    return epub.read(path)


def _locator_compute(case: dict, root: Path) -> dict | None:
    """What a client must produce for the case, or None when its edition is not in this checkout."""
    edition = root / case["edition"]
    if not edition.is_file():
        return None
    book = _edition(str(edition))
    index = json.loads((root / case["index"]).read_text())
    resolved = locators.resolve_edition(index, book)
    found = locators.locate(book, case["locator"])
    return {"seq": found.seq, "exact": found.exact,
            "finished": locators.position(index["anchors"], resolved, found.seq),
            "inclusive": locators.position(index["anchors"], resolved, found.seq, inclusive=True)}


def check_locator(path: Path, root: Path | None = None) -> tuple[bool | None, dict, dict | None]:
    """(ok, expected, actual); ok is None when the case was skipped for a missing edition."""
    root = root or path.parent.parent.parent
    case = json.loads(path.read_text())
    actual = _locator_compute(case, root)
    return (None if actual is None else actual == case["expect"]), case["expect"], actual


def regenerate_locator(path: Path, root: Path | None = None) -> bool:
    root = root or path.parent.parent.parent
    case = json.loads(path.read_text())
    actual = _locator_compute(case, root)
    if actual is None:
        return False
    case["expect"] = actual
    path.write_text(json.dumps(case, indent=2, ensure_ascii=False) + "\n")
    return True
