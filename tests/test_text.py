from lorekeeper import text


def test_words_normalizes_case_punctuation_and_width():
    assert text.words("“Whatever have you been doing, WATSON?”") == [
        "whatever", "have", "you", "been", "doing", "watson"]
    assert text.words("ｆｕｌｌ　width") == ["full", "width"]  # NFKC


def test_joiners_make_spelling_variants_equal():
    assert text.words("wine-glass Good-bye don't don’t to-day") == \
        text.words("wineglass Goodbye dont don't today")
    assert text.words("soft­hyphen") == ["softhyphen"]


def test_dashes_and_digits():
    assert text.words("Nil.—2. Philosophy") == ["nil", "2", "philosophy"]


def test_non_latin_scripts_are_words():
    assert text.words("Москва, 東京!") == ["москва", "東京"]


def test_fingerprint_uses_first_twelve_words_only():
    a = text.words("one two three four five six seven eight nine ten eleven twelve thirteen")
    b = text.words("One two three four five six seven eight nine ten eleven twelve — changed")
    assert text.fingerprint(a) == text.fingerprint(b)
    assert len(text.fingerprint(a)) == 16


def test_fingerprint_is_stable():
    # Pinned so ports in other languages can check themselves against it.
    assert text.fingerprint(text.words("Sherlock Holmes.")) == "25e52f9270e3bdbf"


def test_verbatim_overlap():
    source = text.words("I have my eye on a suite in Baker Street which would suit us")
    assert text.verbatim_overlap("He has his eye on rooms in Baker Street", source, 8) == []
    assert text.verbatim_overlap("I have my eye on a suite in Baker Street", source, 8)
