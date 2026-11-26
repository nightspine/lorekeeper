"""The reading view's name lighting (static/js/readnames.js), run in Node against mentions.py: the same derived forms,
the same matches, the same title and particle lists, and a name never lit in a block before its own reveal."""
import json
import shutil
import subprocess

import pytest

from lorekeeper import merge, mentions

from .conftest import ROOT

# Visible entities of a gated view: titles, a particle, a surname two people share, a title-only name, a lowercase
# description, an accented title, a parenthetical note, a place (no derived forms), a creature.
ENTITIES = [
    {"id": "jonathan", "type": "person", "names": ["Mr. Jonathan Harker", "Jonathan"]},
    {"id": "mina", "type": "person", "names": ["Mrs. Mina Harker", "Mina Murray"]},
    {"id": "vanhelsing", "type": "person", "names": ["Abraham Van Helsing", "the Professor"]},
    {"id": "dracula", "type": "person", "names": ["Count Dracula"]},
    {"id": "seward", "type": "person", "names": ["Dr. John Seward"]},
    {"id": "quincey", "type": "person", "names": ["Quincey P. Morris"]},
    {"id": "brother", "type": "person", "names": ["Frère Jacques Martel"]},
    {"id": "chevalier", "type": "person", "names": ["Chevalier (Gabriel de León)"]},
    {"id": "stranger", "type": "person", "names": ["the stranger"]},
    {"id": "whitby", "type": "place", "names": ["Whitby", "Whitby Abbey"]},
    {"id": "wolf", "type": "creature", "names": ["Bersicker the Wolf"]},
]

TEXTS = [
    "Mr. Jonathan Harker met Mrs. Mina Harker at Whitby; Harker said nothing, and Jonathan smiled.",
    "Abraham Van Helsing, the Professor, and Van Helsing again; Helsing alone; Abraham wrote to Dr. Seward.",
    "Count Dracula! Dracula's castle. Seward and John and Quincey P. Morris and Morris, but not Quincey's hat.",
    "Jacques Martel and Martel, Frère Jacques; Bersicker the Wolf and Bersicker; the stranger, Chevalier.",
]


def cases():
    derived = mentions.derive(ENTITIES)
    names = {e["id"]: [*e["names"], *derived.get(e["id"], [])] for e in ENTITIES}
    matcher = mentions.Matcher(names)
    return {
        "entities": ENTITIES,
        "derived": derived,
        "honorifics": sorted(merge.HONORIFICS),
        "particles": sorted(merge.PARTICLES),
        "matches": [{"text": t, "spans": [[s, e, sorted(o)] for s, e, o in matcher.find(t)]} for t in TEXTS],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_read_names_js(tmp_path):
    f = tmp_path / "read_cases.json"
    f.write_text(json.dumps(cases()), encoding="utf-8")
    r = subprocess.run(["node", str(ROOT / "tests/js/run_read.mjs"), str(f)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
