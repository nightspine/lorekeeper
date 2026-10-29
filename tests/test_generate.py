"""Generator pipeline with a scripted model: structure, corrections and every drop rule."""

import json
import re
import zipfile

import pytest

from lorekeeper import epub, generate, validate
from lorekeeper.gating import Position, view

CH1 = [
    "Alice Marlow arrived at the old lighthouse on the northern cliffs one grey morning in March.",
    "She had come to find her brother, Tom Marlow, who had vanished from the village a year before.",
    "The keeper, a quiet man named Ezra Pike, said he had never heard of anyone called Tom.",
    "Yes.",
    "That night Alice found a letter hidden under the floorboards, signed with the initials T. M.",
]
CH2 = [
    "In the morning Pike admitted that Tom had stayed at the lighthouse all through the winter.",
    "He would not say where Tom had gone, only that he had left by boat before the storms.",
]


def make_epub(path, title="The Lighthouse", ch1=None, series_meta="", cover=None):
    ch1 = ch1 or CH1
    def doc(title, paras, cid):
        body = "".join(f"<p>{p}</p>" for p in paras)
        return (f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head>'
                f'<body><h2 id="{cid}">{title}</h2>{body}</body></html>')
    nav = ('<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
           '<nav epub:type="toc"><ol><li><a href="c1.xhtml#h1">Chapter One</a></li>'
           '<li><a href="c2.xhtml#h2">Chapter Two</a></li></ol></nav></body></html>')
    opf = ('<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata '
           f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title>'
           f'<dc:creator>A. Writer</dc:creator><dc:language>en</dc:language>{series_meta}</metadata><manifest>'
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
           '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>'
           + ('<item id="img" href="images/front.jpg" media-type="image/jpeg" properties="cover-image"/>' if cover else '')
           + '</manifest>'
           '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/c1.xhtml", doc("Chapter One", ch1, "h1"))
        z.writestr("OEBPS/c2.xhtml", doc("Chapter Two", CH2, "h2"))
        if cover:
            z.writestr("OEBPS/images/front.jpg", cover)
    return path


EXTRACT_CH1 = {
    # Local paragraph numbers: [1] is the chapter heading (seq 0), so seq = p - 1 in chapter one.
    "new": [
        {"name": "Alice Marlow", "type": "person", "p": 2, "desc_p": 3,
         "desc": "She has come to the lighthouse to look for her missing brother."},
        {"name": "Tom Marlow", "type": "person", "p": 3, "desc_p": 3,           # has a profile too: desc is a duplicate
         "desc": "Alice's missing brother."},
        {"name": "Ezra Pike", "type": "person", "p": 3, "desc": "", "desc_p": 4},
        {"name": "lighthouse", "type": "place", "p": 2, "desc": "", "desc_p": 2},  # kept: an event uses it
        {"name": "pipette", "type": "object", "p": 2, "desc": "", "desc_p": 2},    # pruned: nothing to say
    ],
    "profiles": [{"id": "Tom Marlow", "p": 3, "text": "Alice Marlow's missing brother, whom she has come to find."}],
    "updates": [
        {"id": "Ezra Pike", "p": 4, "desc": "The lighthouse keeper, who claims he has never heard of Tom."},
        {"id": "Alice Marlow", "p": 5, "desc": "She agrees with the keeper."},  # 1-word block → snaps
        {"id": "Ezra Pike", "p": 4, "desc": "He would later confess everything."},       # hindsight
        {"id": "Tom Marlow", "p": 99, "desc": "He was seen leaving by boat."},           # outside
        {"id": "Alice Marlow", "p": 6,
         "desc": "Alice found a letter hidden under the floorboards, signed with the initials"},  # copies
        {"id": "Tom Marlow", "p": 3, "desc": "He is a famous sailor."},                  # rejected
        {"id": "Nobody Known", "p": 3, "desc": "Does not exist."},                       # unknown
    ],
    "names": [],
    "relations": [{"a": "Alice Marlow", "label": "sister of", "b": "Tom Marlow", "p": 3}],
    "events": [{"p": 2, "text": "Alice arrives at the lighthouse.", "who": ["Alice Marlow", "lighthouse"]},
               {"p": 6, "text": "Alice finds a hidden letter.", "who": ["Alice Marlow"]}],
    "summary": ["Alice reaches the lighthouse in search of her brother Tom.",
                "Tom turns out to be a famous sailor.",        # hindsight wording: dropped before checking
                "Pike says Tom is a famous sailor.",           # rejected by the fact-check
                "Alice discovers a hidden letter."],
    "recap": "Alice comes to the lighthouse looking for her brother Tom.",
}


class FakeLLM:
    name = "fake"

    def __init__(self, verdicts=True):
        self.calls = []
        self.verdicts = verdicts

    def complete(self, system, prompt, schema, prefix=None):
        self.calls.append((prefix or "") + prompt)
        if "new" in schema.get("properties", {}):
            if "Alice Marlow arrived" in (prefix or ""):
                return EXTRACT_CH1
            # The keeper's denial was shown in context with its id; correct it.
            m = re.search(r"\[(\S+)\] The lighthouse keeper, who claims", prompt)
            denial = m.group(1) if m else ""
            return {"new": [{"name": "Pike", "type": "person", "p": 2, "desc": "", "desc_p": 2}],
                    "updates": [{"id": "ezra-pike", "p": 2, "corrects": f"[{denial}]",
                                 "desc": "He admits Tom spent the winter at the lighthouse."}],
                    "names": [], "relations": [],
                    "events": [{"p": 2, "text": "Pike admits Tom stayed.", "who": ["ezra-pike", "tom-marlow"]}],
                    "recap": "Pike admits that Tom stayed at the lighthouse through the winter."}
        props = schema.get("properties", {})
        if "items" in props:
            ids = re.findall(r"^(\S+): ", prompt, re.M)
            return {"items": [{"id": i, "text": "She discovers a concealed note marked T. M."} for i in ids]}
        if "unsupported" in props:
            if not self.verdicts:
                return {}
            bad = [line.split(":")[0] for line in prompt.splitlines() if "famous sailor" in line]
            # The relation is cited at paragraph 3 but (in this script) only shown by paragraph 6.
            later = [{"id": line.split(":")[0], "p": 6} for line in prompt.splitlines() if "sister of" in line]
            return {"unsupported": bad, "later": later}
        raise AssertionError("unexpected schema")


@pytest.fixture
def book_path(tmp_path):
    return make_epub(tmp_path / "lighthouse.epub")


def _run(book_path, tmp_path, opts=None, llm=None, **kw):
    llm = llm or FakeLLM()
    out = tmp_path / "out.lore.json"
    report = generate.run(epub.read(book_path), llm, opts or generate.Options(),
                          book_path, out, log=lambda m: None, **kw)
    return json.loads(out.read_text()), report, llm


def test_toc_and_metadata(book_path):
    book = epub.read(book_path)
    assert [c.title for c in book.chapters] == ["Chapter One", "Chapter Two"]
    assert [c.start for c in book.chapters] == [0, 6]
    assert (book.title, book.authors, book.language) == ("The Lighthouse", ["A. Writer"], "en")


def test_generated_index_is_valid(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path)
    blocks = epub.blocks(book_path)
    assert [str(e) for e in validate.check_index(ix) if e.level == "error"] == []
    assert [str(e) for e in validate.check_against_book(ix, blocks) if e.level == "error"] == []
    assert report["validation_errors"] == []
    assert ix["coverage"]["complete"] is True
    assert ix["provenance"]["generator"]["model"] == "fake"


def test_structure_and_corrections(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path)
    ents = {e["id"]: e for e in ix["entities"]}
    assert set(ents) == {"alice-marlow", "tom-marlow", "ezra-pike", "lighthouse"}  # "Pike" merged, pipette pruned
    assert ents["ezra-pike"]["from"] == "toc01.para03"            # first-mention corrected
    texts = [f["text"] for e in ix["entities"] for f in e["facets"]]
    assert "She discovers a concealed note marked T. M." in texts  # rephrased, not dropped
    agrees = next(f for f in ents["alice-marlow"]["facets"] if f["text"] == "She agrees with the keeper.")
    assert agrees["from"] == "toc01.para05"                        # 1-word block snapped forward
    s = report["stats"]
    assert s["dropped: hindsight wording"] == 2   # a facet and a summary sentence
    assert s["dropped: citation outside passage"] == 1
    assert s["dropped: not supported"] == 2   # the sailor facet and summary sentence
    assert s["dropped: unknown entity"] == 1


def test_desc_without_a_profile_becomes_the_first_profile(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path)
    ents = {e["id"]: e for e in ix["entities"]}
    alice = [f for f in ents["alice-marlow"]["facets"] if f.get("kind") == "profile"]
    assert [f["text"] for f in alice] == ["She has come to the lighthouse to look for her missing brother."]
    assert "She has come to the lighthouse to look for her missing brother." not in \
        [f["text"] for f in ents["alice-marlow"]["facets"] if f.get("kind") != "profile"]


def test_desc_is_dropped_when_the_response_has_a_profile(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path)
    tom = next(e for e in ix["entities"] if e["id"] == "tom-marlow")
    assert [f["text"] for f in tom["facets"]] == ["Alice Marlow's missing brother, whom she has come to find."]
    assert tom["facets"][0]["kind"] == "profile"
    assert report["stats"]["dropped: description duplicates the profile"] == 1


class _InventedTie(FakeLLM):
    """Extraction gives Alice a tie the passage never states; the scripted verifier rejects it."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["new"][0]["desc"] = "Tom's fiancée, at home in England."
        if "unsupported" in schema.get("properties", {}):
            out = dict(out)
            out["unsupported"] = out["unsupported"] + [
                line.split(":")[0] for line in prompt.splitlines() if "fiancée" in line]
        return out


def test_new_entity_desc_with_an_unstated_tie_is_rejected(book_path, tmp_path):
    ix, report, llm = _run(book_path, tmp_path, llm=_InventedTie())
    alice = next(e for e in ix["entities"] if e["id"] == "alice-marlow")
    assert all("fiancée" not in f["text"] for f in alice["facets"])
    assert report["stats"]["dropped: tie not in the text"] == 1   # stopped before the verifier saw it
    assert "unsupported" in generate.VERIFY and "commonly known" in generate.VERIFY


def test_tie_words_need_support_in_the_text():
    chunk = "Alice came to look for her brother Tom. Pike is the keeper."
    assert generate.unsupported_tie("Tom's wife, at home.", chunk) == "wife"
    assert generate.unsupported_tie("Tom's wife, at home.", chunk, "She is a wife") is None   # in the notes
    assert generate.unsupported_tie("Alice is Tom's sister.", chunk) == "sister"
    assert generate.unsupported_tie("Tom's brother Alice.", chunk) is None
    assert generate.unsupported_tie("Alice's fiancée.", "She met her Fiancé") is None          # accents and forms
    assert generate.unsupported_tie("Alice, a person in the season.", chunk) is None           # no substring matches


class _TieProfile(FakeLLM):
    def __init__(self, word):
        super().__init__()
        self.word = word

    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["profiles"] = [{"id": "Tom Marlow", "p": 3, "text": f"Alice Marlow's {self.word}, who went missing."}]
        return out


def test_extraction_profile_with_a_tie_the_chunk_never_says_is_dropped(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_TieProfile("wife"))
    tom = next(e for e in ix["entities"] if e["id"] == "tom-marlow")
    assert all("wife" not in f["text"] for f in tom["facets"])
    assert report["stats"]["dropped: tie not in the text"] >= 1


def test_extraction_profile_with_a_tie_the_chunk_says_is_kept(book_path, tmp_path):
    chunk_text = " ".join(b.raw for b in epub.read(book_path).blocks)
    word = "brother" if "brother" in chunk_text.lower() else None
    assert word, "fixture text should say brother"
    ix, report, _ = _run(book_path, tmp_path, llm=_TieProfile("brother"))
    tom = next(e for e in ix["entities"] if e["id"] == "tom-marlow")
    assert any("brother" in f["text"] for f in tom["facets"] if f.get("kind") == "profile")
    assert report["stats"].get("dropped: tie not in the text", 0) == 0


def test_prompt_asks_for_a_profile_for_every_new_entity_and_a_description_line():
    assert "EVERY new person, creature and group" in generate.EXTRACT
    assert "first line of a character sheet" in generate.EXTRACT
    assert "errands" in generate.EXTRACT


def test_replacement_and_gating(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path)
    pike = next(e for e in ix["entities"] if e["id"] == "ezra-pike")
    denial, admission = pike["facets"]
    assert admission["replaces"] == [denial["id"]]
    end_ch1 = view([ix], Position(None, "toc01.para05"))
    assert [f["id"] for f in next(e for e in end_ch1["entities"] if e["id"] == "ezra-pike")["facets"]] \
        == [denial["id"]]
    end = view([ix], Position(None, ix["coverage"]["through"]))
    pike_view = next(e for e in end["entities"] if e["id"] == "ezra-pike")
    assert [f["id"] for f in pike_view["history"]] == [denial["id"]]
    story = [r for r in ix["recaps"] if r.get("scope", "story") == "story"]
    assert len(story) == 2 and end["recap"]["id"] == story[-1]["id"]


def test_model_never_sees_later_text(book_path, tmp_path):
    _, _, llm = _run(book_path, tmp_path)
    first_extract = llm.calls[0]
    assert "Alice Marlow arrived" in first_extract
    assert "In the morning Pike admitted" not in first_extract


def test_resume_skips_finished_chunks(book_path, tmp_path):
    _run(book_path, tmp_path)
    _, _, llm = _run(book_path, tmp_path, resume=True)
    assert llm.calls == []


@pytest.mark.parametrize("name,expected", [
    ("hæmoglobin", "haemoglobin"), ("François Le Villard", "francois-le-villard"), ("Zoë", "zoe")])
def test_slug_is_ascii(name, expected):
    assert generate.slug(name) == expected


def test_slug_hashes_non_latin_names():
    s = generate.slug("東京")
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", s) and s != generate.slug("大阪")


def test_one_extraction_call_per_chunk(book_path, tmp_path):
    _, report, llm = _run(book_path, tmp_path)
    # 2 chunks: extract + verify each, plus one rephrase for the copied sentence.
    assert report["chunks"] == 2 and len(llm.calls) == 5


def test_fact_check_can_be_turned_off(book_path, tmp_path):
    ix, report, llm = _run(book_path, tmp_path, generate.Options(verify=False))
    assert "dropped: not supported" not in report["stats"]
    assert len(llm.calls) == 3
    assert any("famous sailor" in f["text"] for e in ix["entities"] for f in e["facets"])


def test_missing_verdict_drops_everything_it_covered(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=FakeLLM(verdicts=False))
    assert report["stats"]["dropped: not checked"] > 0
    assert all(not e["facets"] for e in ix["entities"]) and ix["recaps"] == []


def test_light_depth_skips_relations(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, generate.Options(depth="light"))
    assert ix["relations"] == []
    assert "relations" not in generate.extract_schema(False)["properties"]


def test_recaps_are_checked_and_revealed_at_chunk_end(book_path, tmp_path):
    ix, _, llm = _run(book_path, tmp_path)
    story = [r for r in ix["recaps"] if r.get("scope", "story") == "story"]
    assert [r["from"] for r in story] == ["toc01.para05", "toc02.para02"]
    assert any("(recap)" in c for c in llm.calls)


def test_chapter_recaps_keep_checked_sentences(book_path, tmp_path):
    ix, _, llm = _run(book_path, tmp_path)
    chapters = [r for r in ix["recaps"] if r.get("scope") == "chapter"]
    assert len(chapters) == 1 and chapters[0]["from"] == "toc01.para05"
    assert chapters[0]["title"] and chapters[0]["id"].endswith(".chapter.1")
    assert chapters[0]["text"] == "Alice reaches the lighthouse in search of her brother Tom. Alice discovers a hidden letter."
    assert any("(summary) Alice discovers" in c for c in llm.calls)
    v = view([ix], Position(None, "toc02.para02"))
    assert [c["id"] for c in v["chapters"]] == [chapters[0]["id"]]
    assert v["recap"]["text"].startswith("Pike admits")


def test_contained_place_names_merge():
    gen = generate.Generator.__new__(generate.Generator)
    gen.prior = {}
    gen.entities = {"baker-street": {"id": "baker-street", "type": "place",
                                     "names": [{"value": "Baker Street"}]}}
    assert gen.match_known("No. 221b, Baker Street", "place") == "baker-street"
    assert gen.match_known("Baker", "place") is None


def test_fact_check_moves_early_claims_later(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path)
    rel = ix["relations"][0]
    assert rel["label"] == "sister of" and rel["from"] == "toc01.para05"   # was paragraph 3 (seq 2)
    assert report["stats"]["moved later by fact-check"] == 1


def test_series_book_builds_on_earlier_books(book_path, tmp_path):
    """Book 2 reuses book 1's ids, corrects book 1's facts across books, and starts from its recap."""
    from lorekeeper import validate
    book1 = {
        "lorekeeper": "0.1", "kind": "index", "id": "s.one", "series": {"id": "s", "book": "one"},
        "anchors": [{"id": "a1", "fp": "0" * 16}],
        "entities": [{"id": "ezra-pike", "type": "person", "from": "a1",
                      "names": [{"value": "Ezra Pike", "kind": "primary", "from": "a1"}],
                      "facets": [{"id": "one.ezra-pike.1", "from": "a1", "text": "The lighthouse keeper, who claims he has never heard of Tom.",
                                  "basis": "stated", "evidence": ["a1"]}]}],
        "relations": [], "events": [],
        "recaps": [{"id": "one.recap.1", "from": "a1", "text": "Book one ends with Pike alone at the light.", "evidence": ["a1"]}],
    }
    llm = FakeLLM()
    opts = generate.Options(series_id="s", book_key="two", prior=[book1])
    ix, report, _ = _run(book_path, tmp_path, opts, llm)
    pike = next(e for e in ix["entities"] if e["id"] == "ezra-pike")   # same id as book one, no "-2"
    assert pike["names"][0]["value"] == "Ezra Pike"
    assert "Book one ends with Pike alone" in llm.calls[0]             # story so far carried over
    assert "[one.ezra-pike.1]" in llm.calls[0]                          # earlier facts shown as context
    assert report["stats"]["returning entities"] >= 1
    series = {"lorekeeper": "0.1", "kind": "series", "id": "s", "title": "S",
              "books": [{"key": "one", "title": "One"}, {"key": "two", "title": "Two"}],
              "orders": {"publication": ["one", "two"]}, "defaultOrder": "publication"}
    errors = [str(e) for e in validate.check_index(ix, [book1]) if e.level == "error"]
    assert errors == []



@pytest.mark.parametrize("meta,expected", [
    ('<meta property="belongs-to-collection" id="c1">Lights</meta>'
     '<meta refines="#c1" property="collection-type">series</meta><meta refines="#c1" property="group-position">2</meta>',
     ("Lights", "2")),
    ('<meta name="calibre:series" content="Lights"/><meta name="calibre:series_index" content="1.0"/>', ("Lights", "1")),
    ('<meta name="calibre:series" content="Lights"/><meta name="calibre:series_index" content="2.5"/>', ("Lights", "2.5")),
    ('<meta property="belongs-to-collection" id="c1">Box set</meta><meta refines="#c1" property="collection-type">set</meta>',
     (None, None)),
    ("", (None, None)),
])
def test_series_from_epub_metadata(tmp_path, meta, expected):
    book = epub.read(make_epub(tmp_path / "s.epub", series_meta=meta))
    assert (book.series, book.series_index) == expected


def test_whole_story_marks_the_index_complete(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, generate.Options(chapters=[1, 2], whole_story=True))
    assert ix["coverage"]["complete"] is True
    (tmp_path / "x").mkdir()
    ix, _, _ = _run(book_path, tmp_path / "x", generate.Options(chapters=[1, 2]))
    assert ix["coverage"]["complete"] is False
