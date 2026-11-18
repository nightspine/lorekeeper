"""Generator pipeline with a scripted model: structure, corrections and every drop rule."""

import dataclasses
import json
import re
from pathlib import Path
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
    "relations": [{"a": "Alice Marlow", "label": "sister of", "b": "Tom Marlow", "p": 3, "kind": "family"}],
    "events": [{"p": 2, "text": "Alice arrives at the lighthouse.", "who": ["Alice Marlow", "lighthouse"], "weight": "plot"},
               {"p": 6, "text": "Alice finds a hidden letter.", "who": ["Alice Marlow"], "weight": "turn"}],
    "summary": ["Alice reaches the lighthouse in search of her brother Tom.",
                "Tom turns out to be a famous sailor.",        # hindsight wording: dropped before checking
                "Pike says Tom is a famous sailor.",           # rejected by the fact-check
                "Alice discovers a hidden letter."],
    "recap": "Alice comes to the lighthouse looking for her brother Tom.",
}


def verdicts(unsupported=(), unsure=(), later=(), moments=()):
    """A fact-check answer in the checker's shape: every listed claim gets a verdict and a reason."""
    out = [{"id": i, "verdict": "unsupported", "because": "the passage says otherwise"} for i in unsupported]
    out += [{"id": i, "verdict": "unsure", "because": "the passage does not say"} for i in unsure]
    return {"verdicts": out, "later": list(later), "moments": list(moments)}


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
        if "verdicts" in props:
            if not self.verdicts:
                return {}
            bad = [line.split(":")[0] for line in prompt.splitlines() if "famous sailor" in line]
            # The relation is cited at paragraph 3 but (in this script) only shown by paragraph 6.
            later = [{"id": line.split(":")[0], "p": 6} for line in prompt.splitlines() if "sister of" in line]
            return verdicts(bad, later=later)
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
        if "verdicts" in schema.get("properties", {}):
            out = dict(out)
            out["verdicts"] = out["verdicts"] + [
                {"id": line.split(":")[0], "verdict": "unsupported", "because": "no passage says fiancée"}
                for line in prompt.splitlines() if "fiancée" in line]
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
    assert [c["from"] for c in chapters] == ["toc01.para05", "toc02.para02"]
    assert chapters[0]["title"] and chapters[0]["id"].endswith(".chapter.1")
    assert chapters[0]["text"] == "Alice reaches the lighthouse in search of her brother Tom. Alice discovers a hidden letter."
    assert any("(summary) Alice discovers" in c for c in llm.calls)
    # The scripted model gives chapter two no summary at all: its checked event stands in for one.
    assert chapters[1]["text"] == "Pike admits Tom stayed."
    v = view([ix], Position(None, "toc02.para02"))
    assert [c["id"] for c in v["chapters"]] == [c["id"] for c in chapters]
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


class _Unsure(FakeLLM):
    """The checker cannot confirm the hidden-letter claims: they are kept and reported."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "verdicts" in schema.get("properties", {}):
            out = dict(out)
            out["verdicts"] = out["verdicts"] + [
                {"id": line.split(":")[0], "verdict": "unsure", "because": "the letter is only implied"}
                for line in prompt.splitlines() if "hidden letter" in line]
        return out


def test_unsure_claims_are_kept_and_reported_with_reasons(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Unsure())
    assert any("hidden letter" in ev["summary"] for ev in ix["events"])
    assert report["stats"]["kept: checker unsure"] == len(report["unsure"]) == 2   # the event and a summary sentence
    assert {u["because"] for u in report["unsure"]} == {"the letter is only implied"}
    assert report["rejected"] and all(r["because"] for r in report["rejected"])


class _Descriptive(FakeLLM):
    """A description the checker calls a moment stays a fact: an event is something that happens."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["updates"].append({"id": "Ezra Pike", "p": 4, "desc": "A weathered keeper in oilskins who meets every boat."})
        if "verdicts" in schema.get("properties", {}):
            out = dict(out)
            out["moments"] = list(out.get("moments", [])) + [
                line.split(":")[0] for line in prompt.splitlines() if "oilskins" in line]
        return out


def test_a_description_is_not_moved_to_events_even_when_called_a_moment(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Descriptive())
    pike = next(e for e in ix["entities"] if e["id"] == "ezra-pike")
    assert any("oilskins" in f["text"] for f in pike["facets"])
    assert not any("oilskins" in ev["summary"] for ev in ix["events"])
    assert "facts moved to events" not in report["stats"]


class _AboutTheText(FakeLLM):
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = {**out, "recap": "In this passage Alice comes to the lighthouse looking for Tom."}
        return out


def test_a_recap_about_the_text_is_dropped(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_AboutTheText())
    assert report["stats"]["dropped: talks about the text"] == 1
    assert all("passage" not in r["text"] for r in ix["recaps"])


class _PikeTie(FakeLLM):
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["relations"].append({"a": "Ezra Pike", "label": "denies knowing", "b": "Tom Marlow", "p": 4})
        return out


def test_the_prompt_names_the_chapter_and_lists_existing_ties(book_path, tmp_path):
    _, _, llm = _run(book_path, tmp_path, llm=_PikeTie())
    extracts = [c for c in llm.calls if "Update the companion" in c]
    assert "This passage is from: Chapter One" in extracts[0]
    assert "ties:" not in extracts[0]
    assert "ties: denies knowing Tom Marlow" in extracts[1]   # Pike is named in chapter two
    assert "only if this passage changes it" in extracts[1]


class _ManyEvents(FakeLLM):
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["events"] = [{"p": 2 + i % 5, "text": f"Step {i + 1} of the day passes at the lighthouse.",
                              "who": ["Alice Marlow"]} for i in range(6)]
        return out


def test_every_development_is_kept_within_the_budget(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_ManyEvents())
    assert sum(1 for ev in ix["events"] if ev["summary"].startswith("Step ")) == 6   # the old cap was 2
    assert "every development" in generate.EXTRACT and "never a description" in generate.EXTRACT


def test_events_are_weighed_and_turning_points_counted(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path)
    by = {ev["summary"]: ev for ev in ix["events"]}
    assert by["Alice finds a hidden letter."]["kind"] == "turn"
    assert "kind" not in by["Alice arrives at the lighthouse."]          # "plot" is the ordinary development: no kind
    assert report["stats"]["turning points"] == 1
    assert '"weight"' in generate.EXTRACT and "complete sentences" in generate.EXTRACT and "never leave the" in generate.EXTRACT
    assert generate.EXTRACT_SCHEMA["properties"]["events"]["items"]["properties"]["weight"]["enum"] == ["turn", "plot", "minor"]
    assert generate.PROMPT_VERSION == "extract-v11"


class _Where(FakeLLM):
    """Events with a setting: one names a known place, one an unsupported cellar, one is too long, one unsure."""
    def __init__(self, wheres, unsure=()):
        super().__init__()
        self.wheres, self.unsure = wheres, unsure

    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        props = schema.get("properties", {})
        if "new" in props and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["events"] = [{**ev, "where": w} for ev, w in zip(out["events"], self.wheres)]
        if "verdicts" in props:
            out = dict(out)
            out["verdicts"] = out["verdicts"] + [
                {"id": line.split(":")[0], "verdict": "unsupported", "because": "the passage never goes there"}
                for line in prompt.splitlines() if "(setting" in line and "the cellar" in line] + [
                {"id": line.split(":")[0], "verdict": "unsure", "because": "only implied"}
                for line in prompt.splitlines() if "(setting" in line and any(u in line for u in self.unsure)]
        return out


def test_an_event_setting_is_normalised_and_linked_to_a_known_place(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Where(["Inside the lighthouse,", "on the Cliffs"]))
    by = {ev["summary"]: ev for ev in ix["events"]}
    place = next(e["id"] for e in ix["entities"] if e["type"] == "place")
    assert by["Alice arrives at the lighthouse."]["setting"] == {"text": "the lighthouse", "place": place}
    assert by["Alice finds a hidden letter."]["setting"] == {"text": "the Cliffs"}   # no place known: text only
    assert report["stats"]["event settings"] == 2 and report["stats"]["settings linked to a place"] == 1
    assert generate.MAX_SETTING_WORDS == 8
    assert '"where"' in generate.EXTRACT and "from elsewhere" in generate.EXTRACT
    assert "where" in generate.EXTRACT_SCHEMA["properties"]["events"]["items"]["required"]


def test_a_proper_noun_keeps_its_capital_and_a_common_one_loses_it(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_Where(["The lighthouse", "Alice Marlow's room by the sea"]))
    texts = [ev["setting"]["text"] for ev in ix["events"] if ev.get("setting")]
    assert texts == ["the lighthouse", "Alice Marlow's room by the sea"]


def test_a_setting_the_checker_rejects_is_dropped_and_the_event_stays(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Where(["the lighthouse", "the cellar"]))
    by = {ev["summary"]: ev for ev in ix["events"]}
    assert "setting" in by["Alice arrives at the lighthouse."]
    assert "setting" not in by["Alice finds a hidden letter."]            # the event stays without it
    assert report["stats"]["dropped: setting not in the text"] == 1
    assert any(r["kind"] == "setting" and r["because"] for r in report["rejected"])
    assert report["stats"]["dropped: not supported"] == 2                 # only the usual summary/fact drops


def test_an_unsure_setting_is_kept_and_listed(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Where(["the lighthouse", "a boat"], unsure=("a boat",)))
    assert any(ev.get("setting", {}).get("text") == "a boat" for ev in ix["events"])
    assert any(u["kind"] == "setting" and "a boat" in u["claim"] for u in report["unsure"])


def test_a_setting_longer_than_eight_words_or_empty_is_not_written(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Where(["in the long low room at the top of the old tower", "none"]))
    assert not any("setting" in ev for ev in ix["events"])
    assert report["stats"]["dropped: setting too long"] == 1


def test_settings_are_kept_unchecked_when_the_fact_check_is_off(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_Where(["the lighthouse", "the cellar"]), opts=generate.Options(verify=False))
    assert sum(1 for ev in ix["events"] if ev.get("setting")) == 2


class _EventGone(_Where):
    """The checker rejects the event itself: its setting must not outlive it."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "verdicts" in schema.get("properties", {}):
            out["verdicts"] += [{"id": line.split(":")[0], "verdict": "unsupported", "because": "no"}
                                for line in prompt.splitlines() if "Alice finds a hidden letter" in line and "(event" in line]
        return out


def test_a_setting_goes_with_its_event_when_the_event_is_dropped(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_EventGone(["the lighthouse", "a boat"]))
    assert not any("hidden letter" in ev["summary"] for ev in ix["events"])
    assert [ev["setting"]["text"] for ev in ix["events"] if ev.get("setting")] == ["the lighthouse"]
    assert report["stats"]["event settings"] == 1


class _Labels(FakeLLM):
    """A model that answers the summary with a heading and an event with one word."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["summary"] = ["unresolved", *out["summary"]]
            out["events"].append({"p": 2, "text": "Arrival.", "who": ["Alice Marlow"], "weight": "plot"})
        return out


def test_a_label_is_not_a_summary_sentence_or_an_event(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Labels())
    assert report["stats"]["dropped: not a sentence"] == 2
    assert not any(ev["summary"] == "Arrival." for ev in ix["events"])
    assert not any(r["text"].startswith("unresolved") for r in ix["recaps"])


class _NoSummary(FakeLLM):
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["summary"] = []
            out["events"].append({"p": 2, "text": "Alice eats bread and cheese by the fire.", "who": ["Alice Marlow"], "weight": "minor"})
        return out


def test_a_chapter_with_no_summary_is_recapped_from_its_events(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_NoSummary())
    first = next(r for r in ix["recaps"] if r.get("scope") == "chapter" and r["title"] == "Chapter One")
    assert "Alice arrives at the lighthouse." in first["text"] and "Alice finds a hidden letter." in first["text"]
    assert "bread and cheese" not in first["text"]          # small moments stay out of the recap
    assert report["stats"]["chapter recap from events"] == 2   # chapter two's scripted answer has no summary either


class _Ties(FakeLLM):
    """Chapter one: a second tie for the same pair, a sentence as a label, and an action as a tie.
    Chapter two: a newer family tie between the same two, which replaces the first."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        props = schema.get("properties", {})
        if "new" in props and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["relations"] += [
                {"a": "Tom Marlow", "label": "brother of", "b": "Alice Marlow", "p": 2, "kind": "family"},     # same kind, shown earlier
                {"a": "Ezra Pike", "label": "suspicious of", "b": "Alice Marlow", "p": 4, "kind": "hostility"},  # another kind: kept
                {"a": "Ezra Pike", "label": "keeper who says he has never once heard the name of", "b": "Tom Marlow", "p": 4, "kind": "acquaintance"},
                {"a": "Alice Marlow", "label": "questions", "b": "Ezra Pike", "p": 4, "kind": "acquaintance"},   # an action
            ]
        elif "new" in props:
            out = json.loads(json.dumps(out))
            out["relations"] = [{"a": "alice-marlow", "label": "estranged sister of", "b": "tom-marlow", "p": 2, "kind": "family"}]
        if "verdicts" in props:
            acts = [line.split(":")[0] for line in prompt.splitlines() if ", questions, " in line]
            out = {**out, "moments": [*out.get("moments", []), *acts]}
        return out


def test_ties_are_kinds_one_per_pair_and_a_newer_one_replaces_the_old(book_path, tmp_path):
    ix, report, llm = _run(book_path, tmp_path, llm=_Ties())
    rels = ix["relations"]
    assert [(r["label"], r.get("kind")) for r in rels] == [("sister of", "family"), ("suspicious of", "hostility"), ("estranged sister of", "family")]
    assert rels[2]["replaces"] == [rels[0]["id"]] and "replaces" not in rels[1]
    st = report["stats"]
    assert st["dropped: second tie for a pair"] == 1 and st["dropped: not a tie"] == 1
    assert st["dropped: an action, not a tie"] == 1 and st["ties replaced by a newer one"] == 1
    assert any("Alice Marlow, sister of, Tom Marlow (family tie)" in c for c in llm.calls)
    v = view([ix], Position(None, "toc02.para02"))
    assert [r["label"] for r in v["relations"]] == ["suspicious of", "estranged sister of"] and v["relations"][1]["kind"] == "family"
    assert [r["label"] for r in v["relationHistory"]] == ["sister of"]
    assert not [i for i in validate.check_index(ix) if i.level == "error"]


def test_default_chunk_size_follows_the_provider():
    assert generate.default_chunk_words("ollama") == generate.default_chunk_words(None) == 2500
    assert generate.default_chunk_words("claude") == generate.default_chunk_words("openai") == 8000


def test_a_name_the_text_gives_a_relative_or_another_person_is_not_an_alias():
    # Dracula: "Mrs. Westenra was naturally anxious concerning Lucy" gave Lucy her mother's name.
    assert generate.names_two_people("Mrs. Westenra", ["Lucy Westenra"],
                                     ["Mrs. Westenra was naturally anxious concerning Lucy, and has consulted me."])
    assert not generate.names_two_people("Mrs. Harker", ["Mina Murray", "Mina Harker"], ["I said to Mina, Mrs. Harker, that we go."])
    assert not generate.names_two_people("Mrs. Harker", ["Mina Harker"], ["Mrs. Harker smiled at me. Mina was tired."])
    assert not generate.names_two_people("Lucy", ["Lucy Westenra"], ["Lucy came with Lucy Westenra."])   # untitled: not judged
    assert generate.names_a_relative("Mrs. Westenra", ["Lucy and her mother, Mrs. Westenra, walked out."])
    assert generate.names_a_relative("Mrs. Westenra", ["A young woman, daughter of Mrs. Westenra."])
    assert generate.names_a_relative("Mrs. Westenra", ["Mrs. Westenra's daughter is unwell."])
    assert not generate.names_a_relative("Mrs. Harker", ["She is Mrs. Harker now, married in Buda-Pesth."])


class _Kin(FakeLLM):
    """Kinship labels as models write them: other casings and forms, a wrong kind, and noise that must stay as is."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["relations"] = [
                {"a": "Alice Marlow", "label": "Is Sister of", "b": "Tom Marlow", "p": 3, "kind": "friendship"},
                {"a": "Ezra Pike", "label": "possible brother of", "b": "Tom Marlow", "p": 4, "kind": "acquaintance"},
            ]
        return out


def test_kinship_labels_are_normalised_and_tagged_family(book_path, tmp_path):
    ix, report, llm = _run(book_path, tmp_path, llm=_Kin())
    rels = {(r["label"], r.get("kind")) for r in ix["relations"]}
    assert ("sister of", "family") in rels                    # parsed: canonical label, family whatever the model said
    assert ("possible brother of", "acquaintance") in rels    # uncertain: left alone
    assert not [i for i in validate.check_index(ix) if i.level == "error"]


def test_the_prompt_names_the_canonical_family_labels(book_path, tmp_path):
    _, _, llm = _run(book_path, tmp_path, llm=_Kin())
    assert any("husband of, wife of, married to, grandmother of" in c for c in llm.calls)


def test_a_wife_tie_ends_a_courtship_tie(book_path, tmp_path):
    class Wed(FakeLLM):
        def complete(self, system, prompt, schema, prefix=None):
            out = super().complete(system, prompt, schema, prefix)
            if "new" in schema.get("properties", {}):
                out = json.loads(json.dumps(out))
                first = "Alice Marlow arrived" in (prefix or "")
                out["relations"] = [{"a": "Alice Marlow", "label": "engaged to" if first else "Wife of", "b": "Ezra Pike",
                                     "p": 4 if first else 2, "kind": "romance" if first else "friendship"}]
            return out
    ix, _, _ = _run(book_path, tmp_path, llm=Wed())
    first, second = ix["relations"][0], ix["relations"][-1]
    assert (second["label"], second["kind"]) == ("wife of", "family") and second["replaces"] == [first["id"]]


def test_a_courtship_and_a_wife_tie_in_one_passage_keep_the_wife(book_path, tmp_path):
    class Both(FakeLLM):
        def complete(self, system, prompt, schema, prefix=None):
            out = super().complete(system, prompt, schema, prefix)
            if "new" in schema.get("properties", {}):
                out = json.loads(json.dumps(out))
                out["relations"] = [
                    {"a": "Alice Marlow", "label": "engaged to", "b": "Ezra Pike", "p": 4, "kind": "romance"},
                    {"a": "Alice Marlow", "label": "wife of", "b": "Ezra Pike", "p": 2, "kind": "family"}]
            return out
    ix, _, report = _run(book_path, tmp_path, llm=Both())
    labels = [r["label"] for r in ix["relations"]]
    assert "wife of" in labels and "engaged to" not in labels


def _family_chain(*chapters):
    """Family ties per chapter (the fake model's two chapters): each a list of (a, label, b, kind)."""
    class Chain(FakeLLM):
        def complete(self, system, prompt, schema, prefix=None):
            out = super().complete(system, prompt, schema, prefix)
            if "new" in schema.get("properties", {}):
                out = json.loads(json.dumps(out))
                first = "Alice Marlow arrived" in (prefix or "")
                ch = chapters[0 if first else 1]
                ident = (lambda n: n) if first else (lambda n: n.lower().replace(" ", "-"))   # later chapters name ids
                out["relations"] = [{"a": ident(a), "label": label, "b": ident(b), "p": 2 if i or not first else 4, "kind": kind}
                                    for i, (a, label, b, kind) in enumerate(ch)]
            return out
    return Chain()


def _current_labels(ix):
    gone = {x for r in ix["relations"] for x in r.get("replaces", [])}
    return [r["label"] for r in ix["relations"] if r["id"] not in gone]


def test_cousins_who_marry_keep_both_ties(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_family_chain(
        [("Alice Marlow", "cousin of", "Ezra Pike", "family")], [("Alice Marlow", "wife of", "Ezra Pike", "family")]))
    assert _current_labels(ix) == ["cousin of", "wife of"]
    assert all("replaces" not in r for r in ix["relations"])


def test_a_wife_tie_retires_the_engagement(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_family_chain(
        [("Alice Marlow", "engaged to", "Ezra Pike", "romance")], [("Alice Marlow", "wife of", "Ezra Pike", "family")]))
    assert _current_labels(ix) == ["wife of"]


def test_a_half_brother_tie_replaces_a_brother_tie(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_family_chain(
        [("Alice Marlow", "brother of", "Tom Marlow", "family")], [("Alice Marlow", "half-brother of", "Tom Marlow", "friendship")]))
    assert _current_labels(ix) == ["half-brother of"]


def test_one_passage_keeps_a_tie_per_role(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_family_chain(
        [("Alice Marlow", "cousin of", "Ezra Pike", "family"), ("Alice Marlow", "wife of", "Ezra Pike", "family")], []))
    assert sorted(_current_labels(ix)) == ["cousin of", "wife of"]


class _Late(_Where):
    """The checker accepts the setting but says the passage shows it only at paragraph 5."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "verdicts" in schema.get("properties", {}):
            out["later"] = out.get("later", []) + [{"id": line.split(":")[0], "p": 5}
                                                   for line in prompt.splitlines() if "(setting" in line and "the lighthouse" in line]
        return out


def test_a_setting_the_checker_moves_later_than_its_event_is_dropped(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Late(["the lighthouse", "a boat"]))
    by = {ev["summary"]: ev for ev in ix["events"]}
    assert "setting" not in by["Alice arrives at the lighthouse."]       # would have shown it before the text did
    assert by["Alice finds a hidden letter."]["setting"] == {"text": "a boat"}
    assert report["stats"]["dropped: setting revealed later than its event"] == 1


class _OnlyASetting(_Where):
    """The place is named by no event's `who`: only a setting uses it."""
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            for ev in out["events"]:
                ev["who"] = [w for w in ev["who"] if w != "lighthouse"]
        return out


def test_a_place_used_only_as_a_setting_is_not_kept_and_the_link_goes(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_OnlyASetting(["the lighthouse", "a boat"]))
    assert not any(e["type"] == "place" for e in ix["entities"])
    by = {ev["summary"]: ev for ev in ix["events"]}
    assert by["Alice arrives at the lighthouse."]["setting"] == {"text": "the lighthouse"}   # text stays, no place
    assert [str(e) for e in validate.check_index(ix) if e.level == "error"] == []
    assert validate.check_schema(ix, "index") == []


def test_a_setting_of_nine_whitespace_tokens_is_refused(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Where(["the hall & the stairs of the old house", "a boat"]))
    assert not any(ev.get("setting", {}).get("text", "").startswith("the hall") for ev in ix["events"])
    assert report["stats"]["dropped: setting too long"] == 1


class _LatePlace(_Where):
    def complete(self, system, prompt, schema, prefix=None):
        out = super().complete(system, prompt, schema, prefix)
        if "new" in schema.get("properties", {}) and "Alice Marlow arrived" in (prefix or ""):
            out = json.loads(json.dumps(out))
            out["new"].append({"name": "floorboards", "type": "place", "p": 6, "desc": "A bare spot.", "desc_p": 6})
        return out


def test_a_place_first_named_after_the_event_is_not_linked(book_path, tmp_path):
    ix, _, _ = _run(book_path, tmp_path, llm=_LatePlace(["the floorboards", "a boat"]))
    first = next(ev for ev in ix["events"] if ev["summary"] == "Alice arrives at the lighthouse.")
    assert first["setting"] == {"text": "the floorboards"}
    assert [str(e) for e in validate.check_index(ix) if e.level == "error"] == []


def test_a_setting_naming_a_word_the_book_has_not_used_is_dropped(book_path, tmp_path):
    ix, report, _ = _run(book_path, tmp_path, llm=_Where(["Castle Dracula", "Alice Marlow's room"]))
    texts = [ev["setting"]["text"] for ev in ix["events"] if ev.get("setting")]
    assert texts == ["Alice Marlow's room"]
    assert report["stats"]["dropped: setting names a word the book hasn't used"] == 1


def _bare_generator():
    from collections import Counter
    g = generate.Generator.__new__(generate.Generator)
    g.stats, g.entities, g.events, g.relations = Counter(), {}, [], []
    return g


def test_a_one_letter_article_is_lowercased_in_a_setting():
    g = _bare_generator()
    ev = generate.Candidate("event", "e1", "x", 1, [], {})
    got = {w: g._setting_candidate(ev, w, "").text for w in ["A carriage", "The carriage", "Castle Dracula"]}
    assert got == {"A carriage": "a carriage", "The carriage": "the carriage", "Castle Dracula": "Castle Dracula"}


def test_a_repeated_events_setting_never_reaches_the_checker():
    g = _bare_generator()
    e1 = generate.Candidate("event", "e1", "Alice leaves.", 1, [], {})
    e2 = generate.Candidate("event", "e2", "Alice leaves.", 1, [], {})
    s1 = generate.Candidate("setting", "s1", "the hall", 1, [], {"event": "e1", "event_text": "Alice leaves."})
    s2 = generate.Candidate("setting", "s2", "the cellar", 1, [], {"event": "e2", "event_text": "Alice leaves."})
    out = g._dedupe([e1, e2, s1, s2])
    assert [c.cid for c in out] == ["e1", "s1"]


# ---- chapter mode ----------------------------------------------------------------------------

def _chapter_gen(book_path, **kw):
    return generate.Generator(epub.read(book_path), FakeLLM(), generate.Options(chunk_mode="chapter", **kw),
                              book_path, lambda m: None)


def test_a_chapter_under_the_cap_is_one_whole_part_with_a_bridge(book_path):
    parts = _chapter_gen(book_path).chunks()
    assert len(parts) == 2 and all(p.whole for p in parts)
    assert parts[0].bridge is None and parts[1].bridge == (parts[0].end - 1, parts[0].end)


def test_a_chapter_over_the_cap_splits_at_a_scene_break(tmp_path):
    long = [f"Paragraph {i} of the long chapter keeps going with plenty of words to count here today." for i in range(8)]
    long[5] = "Later That Night"          # heading-like: a scene break
    path = make_epub(tmp_path / "long.epub", ch1=long)
    g = _chapter_gen(path, max_part_words=60)
    parts = [p for p in g.chunks() if p.chapter == 0]
    assert len(parts) > 1 and not any(p.whole for p in parts)
    heading = next(b.seq for b in g.book.blocks if b.raw == "Later That Night")
    assert any(p.start == heading for p in parts)
    assert all(a.end + 1 == b.start for a, b in zip(parts, parts[1:]))


def test_scene_break_detection():
    for t in ("III", "Chapter Two", "MINA HARKER'S JOURNAL", "Later That Night"):
        assert generate.is_scene_break(t, len(t.split())), t
    for t in ("Then\u2014", "Yours ever, Jonathan Harker", "\u201cYes.\u201d", "Yes.", "later that night", "* * *"):
        assert not generate.is_scene_break(t, len(t.split()) if t != "* * *" else 0), t


def test_ollama_cap_leaves_room_for_the_prompt_and_the_output():
    assert generate.max_part_words("ollama", 8192) == 1500
    assert generate.max_part_words("ollama", 16384) == 6501
    assert generate.max_part_words("ollama", 32768) == 17970
    assert generate.max_part_words("ollama", 1_000_000) == generate.MAX_PART_WORDS == 20000
    assert generate.max_part_words("claude") == generate.MAX_PART_WORDS
    assert generate.default_chunk_mode("claude") == "chapter" and generate.default_chunk_mode("ollama") == "words"


def test_no_bridge_from_front_matter(tmp_path):
    path = make_epub(tmp_path / "fm.epub")
    g = _chapter_gen(path)
    parts = g.chunks()
    assert parts[0].chapter == -1 or parts[0].bridge is None
    assert all(p.bridge is None for p in parts if p.chapter == -1)
    first_real = next((i for i, p in enumerate(parts) if p.chapter >= 0), 0)
    if first_real > 0:
        assert parts[first_real].bridge is None or parts[first_real - 1].chapter >= 0


class CutOffLLM(FakeLLM):
    """The first extraction reply is empty (cut off at the limit), the next ones come from the script."""
    def __init__(self, cut=1, always=False):
        super().__init__()
        self.cut, self.always, self.extractions = cut, always, []

    def complete(self, system, prompt, schema, prefix=None):
        if "new" in schema.get("properties", {}):
            self.extractions.append(prefix or "")
            if self.always or len(self.extractions) <= self.cut:
                return {}
        return super().complete(system, prompt, schema, prefix)


def _cutoff_run(book_path, tmp_path, llm):
    logs = []
    out = tmp_path / "o.lore.json"
    generate.run(epub.read(book_path), llm, generate.Options(chapters={1}), book_path, out, log=logs.append)
    return logs


def test_a_cut_off_reply_splits_the_part_and_logs_it(book_path, tmp_path):
    llm = CutOffLLM(cut=1)
    logs = _cutoff_run(book_path, tmp_path, llm)
    assert sum("extraction reply was cut off; splitting the part in two" in m for m in logs) == 1
    assert len(llm.extractions) == 3          # the whole part, then both halves
    assert len(set(llm.extractions[1:])) == 2


def test_a_half_that_fails_again_is_logged_and_done(book_path, tmp_path):
    llm = CutOffLLM(always=True)
    logs = _cutoff_run(book_path, tmp_path, llm)
    assert any("cut off again" in m for m in logs)
    state = json.loads((tmp_path / "o.lore.json.state").read_text())
    keys = [k for k in state["done"]]
    assert len(keys) == 3 and len(set(keys)) == 3          # both halves and the parent
    parent = max(keys, key=lambda k: int(k.split("-")[1]) - int(k.split("-")[0]))
    assert parent == keys[-1]
    report = json.loads((tmp_path / "o.lore.json.report").read_text())
    assert report["stats"]["dropped: reply cut off"] == 2


def test_a_split_part_reports_both_halves_additions(book_path, tmp_path):
    g = generate.Generator(epub.read(book_path), CutOffLLM(cut=1), generate.Options(chapters={1}), book_path, lambda m: None)
    seen = []
    orig = g._process_part

    def spy(chunk):
        out = orig(chunk)
        seen.append(list(g.added))
        return out
    g._process_part = spy
    g.process(g.chunks()[0])
    assert len(seen) == 2 and seen[0] and seen[1]
    assert g.added == seen[0] + seen[1]


def test_a_cut_off_claude_reply_is_split_not_raised(book_path, tmp_path):
    """Through the real Claude backend: stop_reason max_tokens on the first extraction splits the part."""
    from types import SimpleNamespace as NS
    from lorekeeper import llm as llmmod
    fake, calls = FakeLLM(), {"extractions": 0}

    class Messages:
        def create(self, **kw):
            schema = kw["output_config"]["format"]["schema"]
            usage = NS(input_tokens=10, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            if "new" in schema.get("properties", {}):
                calls["extractions"] += 1
                if calls["extractions"] == 1:
                    return NS(stop_reason="max_tokens", content=[NS(type="text", text='{"new": [{"na')], usage=usage)
            text = kw["messages"][0]["content"]
            text = text if isinstance(text, str) else text[-1]["text"]
            return NS(stop_reason="end_turn", usage=usage,
                      content=[NS(type="text", text=json.dumps(fake.complete(kw["system"], text, schema)))])
    client = NS(messages=Messages(), beta=NS(messages=Messages()))
    backend = llmmod.Claude(name="claude-haiku-5-5", client=client)
    logs = _cutoff_run(book_path, tmp_path, backend)
    assert any("splitting the part in two" in m for m in logs)
    assert backend.usage.truncated == 1


def test_cli_parts_default_follows_the_provider():
    from argparse import Namespace
    from lorekeeper import cli
    def mode(provider="ollama", parts=None, whole=False):
        return cli._chunk_mode(Namespace(provider=provider, parts=parts, whole_chapters=whole,
                                         chunk_words=None, resume=False, output="x"))
    assert mode() == "words"
    assert mode("claude") == "chapter"
    assert mode("claude", parts="words") == "words"
    assert mode(whole=True) == "chapter"
    assert mode(parts="chapter") == "chapter"


def _cli_mode(monkeypatch, *argv):
    from lorekeeper import cli
    seen = {}
    monkeypatch.setattr(generate, "run", lambda book, model, opts, *a, **k: seen.update(mode=opts.chunk_mode) or {"validation_errors": 0})
    monkeypatch.setattr(cli, "_make_model", lambda *a, **k: object())
    assert cli.main(["generate", *argv]) == 0
    return seen["mode"]


def test_cli_chunk_words_alone_means_words(book_path, tmp_path, monkeypatch):
    base = [str(book_path), "-o", str(tmp_path / "o.json"), "--provider", "claude"]
    assert _cli_mode(monkeypatch, *base) == "chapter"
    assert _cli_mode(monkeypatch, *base, "--chunk-words", "3000") == "words"


def test_cli_whole_chapters_and_parts_words_conflict(book_path, tmp_path):
    from lorekeeper import cli
    with pytest.raises(SystemExit):
        cli.main(["generate", str(book_path), "-o", str(tmp_path / "o.json"), "--whole-chapters", "--parts", "words"])


def test_resume_keeps_the_recorded_mode(book_path, tmp_path, monkeypatch):
    out = tmp_path / "o.json"
    _run_to(book_path, out, generate.Options(chunk_mode="words", chunk_words=60))
    assert json.loads(Path(str(out) + ".state").read_text())["chunk_mode"] == "words"
    base = [str(book_path), "-o", str(out), "--provider", "claude", "--resume"]
    assert _cli_mode(monkeypatch, *base) == "words"
    assert _cli_mode(monkeypatch, *base, "--parts", "chapter") == "chapter"
    # a state from before the mode was recorded was a words run
    st = Path(str(out) + ".state"); d = json.loads(st.read_text()); d.pop("chunk_mode"); st.write_text(json.dumps(d))
    assert _cli_mode(monkeypatch, *base) == "words"


def test_resume_in_the_recorded_mode_rereads_nothing(book_path, tmp_path):
    out = tmp_path / "o.json"
    _run_to(book_path, out, generate.Options(chunk_mode="words", chunk_words=60))
    n = len(json.loads(out.read_text())["recaps"])
    llm = FakeLLM()
    generate.run(epub.read(book_path), llm, generate.Options(chunk_mode="words", chunk_words=60), book_path, out,
                 resume=True, log=lambda m: None)
    assert llm.calls == [] and len(json.loads(out.read_text())["recaps"]) == n


def _run_to(book_path, out, opts):
    generate.run(epub.read(book_path), FakeLLM(), opts, book_path, out, log=lambda m: None)


def test_requests_per_part_are_capped(book_path, tmp_path):
    llm = FakeLLM()
    big = " ".join(["word"] * 40000)
    g = generate.Generator(epub.read(book_path), llm, generate.Options(), book_path, lambda m: None)
    chunk = g.chunks()[0]
    g.book.blocks[chunk.start] = dataclasses.replace(g.book.blocks[chunk.start], words=big.split())   # 40,000 words
    g.process(chunk)
    prompt = next(c for c in llm.calls if "events" in c)
    assert f"{generate.MAX_EVENTS}" in prompt and "at most 8 per entity" in prompt



def test_whole_chapter_prompt_has_an_uncited_bridge_and_one_recap_each(book_path, tmp_path):
    ix, report, llm = _run(book_path, tmp_path, generate.Options(chunk_mode="chapter"))
    assert report["chunks"] == 2
    first, second = [c for c in llm.calls if "Update the companion" in c][:2]
    assert "This passage is the whole of: Chapter One" in first and "Where the previous part left off" not in first
    assert "Where the previous part left off" in second and "That night Alice found a letter" in second
    assert "never cite" in second
    chapter_recaps = [r for r in ix["recaps"] if r.get("scope") == "chapter"]
    assert len(chapter_recaps) == len({r["title"] for r in chapter_recaps})
    assert any("do not" in c or "support nothing" in c for c in llm.calls if "Claims:" in c)


def test_words_mode_has_no_bridge(book_path, tmp_path):
    _, _, llm = _run(book_path, tmp_path)
    assert not any("Where the previous part left off" in c or "the whole of" in c for c in llm.calls)


def test_a_family_tie_with_an_unresolved_end_has_no_role():
    assert generate.Generator._family_role("family", "sister of", "alice", None) is None
    assert generate.Generator._family_role("family", "sister of", None, None) is None
