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
