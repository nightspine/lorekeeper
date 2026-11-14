"""kinship.js and kinship.py are two implementations of one vocabulary: for every label they must agree on
{role, modifiers} and on normalize()."""

import itertools
import json
import re
import shutil
import subprocess

import pytest

from lorekeeper import kinship

from .conftest import ROOT

GOOD = """daughter of|sister of|son of|father of|brother of|husband of|wife of|cousin of|mother of|half-sister of|married to
aunt of|guardian of|nephew of|widow of|ex-husband of|niece of|younger brother of|ward of|sibling of|granddaughter of
grandson of|grandfather of|grandchild of|stepson of|uncle of|is niece of|blood-niece of|Mom of|his wife|the late husband of
mother-in-law of|brother-in-law of|step-son of|greatgrandmother of|great-grandmother of|adoptive mother of|twin of
twin brother of|second cousin of|godfather of|godson of|widower of|Her sister|foster-daughter of|dad of|a mum of
Wife Of|  wife   of |consort of|spouse of|goddaughter of|granny of|grandpa of|papa of|mama of|parent of|child of
older sister of|elder brother of|eldest son of|youngest daughter of|former wife of|identical twin of|first cousin of
half brother of|ex wife of|their daughter|an aunt of|godmother of|sister-in-law of|father-in-law of
mother in law of|brother in law of|sister in-law of|half–sister of|half—brother of
stepmother of|step mother of|stepfather of|stepparent of|stepdaughter of|stepchild of|stepbrother of|stepsister of
step brother of|grandmother of|granddaughter of|grandparent of|godfather of|godmother of
half-sibling of|half sibling of|half-brother of|ex-wife of|ex wife of|ex-spouse of|ex spouse of|exwife of
great-grandfather of|great grandfather of|great-grandson of|great-granddaughter of|great-grandchild of|great-aunt of
great aunt of|great-uncle of|great-niece of|great-nephew of|great great grandmother of|older half sister of
younger sister of|elder sister of|eldest brother of|youngest son of|adopted son of|late wife of|former husband of
blood sister of|first cousin of|second cousin of|twin sister of|identical twin brother of|foster mother of
older stepbrother of|eldest step son of|half-sister-in-law of
late elder brother of|step half brother of|half step sister of|younger late widow of|late widow of|ex step mother of
great step grandmother of""".replace("\n", "|").split("|")

NOISE = ["killed mother of", "barracks brother of", "brother-in-arms of", "guardian and friend of", "possible father of",
         "sibling of, fights", "calls cousin, rebuffed by", "grandson of the cast-out daughter of", "remembers late wife",
         "keeps secret from", "friend of", "", "servant of", "suspected brother of", "alleged son of", "rumoured wife of",
         "rumored husband of", "mother of the bride", "brother of Jonathan", "knows", "wife-beater of", "husband (estranged)",
         "wife of; widow of", "sister/lover of", "of", "is", "the", "married", "marries", "married to Jonathan", "cousin cousin of",
         "mother father of", "friend and brother of", "brother in arms of", "half", "step of"]


KIN_WORDS = ["mother", "son", "wife", "widow", "widower", "brother", "grandson", "aunt", "cousin", "ward"]


def _labels():
    words = ["mother", "son", "wife", "widow", "brother", "grandson", "aunt", "nephew", "cousin", "guardian", "ward", "twin"]
    mods = ["", "half ", "step-", "younger ", "ex-", "great ", "adoptive ", "late ", "blood-", "half-step ", "foster "]
    combos = [f"{m}{w}{s}" for m, w, s in itertools.product(mods, words, ["", " of", "-in-law of", " of Harry"])]
    # every pair of modifiers before every kin word, so the two orderings cannot drift apart
    pairs = [f"{a} {b} {w} of" for a, b, w in itertools.product(kinship.MODIFIERS, kinship.MODIFIERS, KIN_WORDS)]
    return [label for label in dict.fromkeys(GOOD + NOISE + combos + pairs)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_kinship_js_and_python_agree():
    labels = _labels()
    r = subprocess.run(["node", str(ROOT / "tests/js/kinship_dump.mjs")], input=json.dumps(labels),
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    assert len(js) == len(labels)
    diffs = []
    for label, j in zip(labels, js):
        p = kinship.parse(label)
        py = {"role": p["role"] if p else None, "modifiers": sorted(p["modifiers"]) if p else None,
              "normalize": kinship.normalize(label)}
        if py != j:
            diffs.append(f"{label!r}: py={py} js={j}")
    assert not diffs, "\n".join(diffs)


def test_labels_cover_the_data():
    # every label named in the brief's measurements is in the list, so a drop there is caught
    must = ["daughter of", "killed mother of", "grandson of the cast-out daughter of", "blood-niece of", "is niece of"]
    assert all(m in _labels() for m in must)
    assert sum(1 for g in GOOD if kinship.parse(g)) == len(GOOD)
    assert not [n for n in NOISE if kinship.parse(n)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_role_reverse_table_agrees():
    script = ("import(process.argv[1]).then(m => console.log(JSON.stringify(Object.fromEntries("
              "process.argv.slice(2).map(r => [r, m.reverse(r)])))))")
    mod = (ROOT / "src/lorekeeper/web/static/js/kinship.js").as_uri()
    r = subprocess.run(["node", "-e", script, mod, *kinship.ROLES], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == kinship.ROLES


def test_normalize_modifier_order():
    for label, want in [("late elder brother of", "late elder brother of"), ("step half brother of", "half-stepbrother of"),
                        ("half step sister of", "half-stepsister of"), ("great step grandmother of", "great-stepgrandmother of"),
                        ("ex step mother of", "ex-stepmother of"), ("younger late widow of", "younger widow of"),
                        ("late widow of", "widow of"), ("identical twin brother of", "identical twin brother of")]:
        assert kinship.normalize(label) == want, label
