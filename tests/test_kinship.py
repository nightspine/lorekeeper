from lorekeeper import kinship

GOOD = {
    "daughter of": ("child", []), "sister of": ("sibling", []), "son of": ("child", []), "father of": ("parent", []),
    "husband of": ("spouse", []), "wife of": ("spouse", []), "cousin of": ("cousin", []), "mother of": ("parent", []),
    "half-sister of": ("sibling", ["half"]), "married to": ("spouse", []), "aunt of": ("pibling", []),
    "guardian of": ("guardian", []), "nephew of": ("nibling", []), "widow of": ("spouse", ["late"]),
    "ex-husband of": ("spouse", ["ex"]), "niece of": ("nibling", []), "younger brother of": ("sibling", ["younger"]),
    "ward of": ("ward", []), "sibling of": ("sibling", []), "granddaughter of": ("grandchild", []),
    "grandson of": ("grandchild", []), "grandfather of": ("grandparent", []), "grandchild of": ("grandchild", []),
    "stepson of": ("child", ["step"]), "uncle of": ("pibling", []), "is niece of": ("nibling", []),
    "blood-niece of": ("nibling", ["blood"]), "mother-in-law of": ("parent", ["in-law"]),
    "great-grandmother of": ("grandparent", ["great"]), "greatgrandmother of": ("grandparent", ["great"]),
    "identical twin of": ("sibling", ["identical"]), "wife": ("spouse", []),
}
NOISE = ["killed mother of", "barracks brother of", "brother-in-arms of", "guardian and friend of", "possible father of",
         "sibling of, fights", "calls cousin, rebuffed by", "grandson of the cast-out daughter of",
         "remembers late wife", "keeps secret from", "estranged sister of", "", "of"]


def test_good_labels_parse():
    for label, (role, mods) in GOOD.items():
        p = kinship.parse(label)
        assert p and (p["role"], p["modifiers"]) == (role, mods), label


def test_noise_is_not_kinship():
    for label in NOISE:
        assert kinship.parse(label) is None and kinship.normalize(label) is None, label


def test_normalize():
    assert kinship.normalize("is niece of") == "niece of"
    assert kinship.normalize("Mom of") == "mother of"
    assert kinship.normalize("blood-niece of") == "blood niece of"
    assert kinship.normalize("married to") == "married to"
    assert kinship.normalize("Brother in law of") == "brother-in-law of"
    assert kinship.normalize("wife") == "wife of"
    for label in GOOD:   # canonical form parses to the same thing
        n = kinship.normalize(label)
        a, b = kinship.parse(n), kinship.parse(label)
        assert (a["role"], a["modifiers"]) == (b["role"], b["modifiers"]), label
        assert kinship.normalize(n) == n


def test_tag_and_ties_command(tmp_path, capsys):
    import json
    import shutil
    from pathlib import Path

    from lorekeeper import cli
    src = Path(__file__).parent.parent / "examples" / "sherlock-holmes" / "sign-of-four.json"
    doc = json.loads(src.read_text())
    ents = [e["id"] for e in doc["entities"]][:4]
    doc["relations"] += [
        {"id": "kx1", "src": ents[0], "dst": ents[1], "label": "Is Wife of", "from": doc["relations"][0]["from"], "basis": "stated", "kind": "romance"},
        {"id": "kx2", "src": ents[1], "dst": ents[2], "label": "possible father of", "from": doc["relations"][0]["from"], "basis": "stated"},
        {"id": "kx3", "src": ents[2], "dst": ents[3], "label": "half-brother of", "from": doc["relations"][0]["from"], "basis": "stated", "kind": "friendship"},
    ]
    path = tmp_path / "x.json"
    path.write_text(json.dumps(doc))
    n = kinship.tag(json.loads(path.read_text()))
    assert n["tagged"] >= 1 and n["left"] == 1 and n["normalised"] == 2
    assert cli.main(["ties", str(path)]) == 0
    assert json.loads(path.read_text()) == doc                      # dry run writes nothing
    assert cli.main(["ties", str(path), "--write"]) == 0
    out = {r["id"]: r for r in json.loads(path.read_text())["relations"]}
    assert (out["kx1"]["label"], out["kx1"]["kind"]) == ("wife of", "family")
    assert "kind" not in out["kx2"] and out["kx3"]["kind"] == "friendship"
    assert json.loads(path.with_name("x.json.bak").read_text()) == doc
