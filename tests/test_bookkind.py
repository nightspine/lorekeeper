"""Book kind inference: subjects, structure, model, default, and what generate records."""

import json
from types import SimpleNamespace

from lorekeeper import bookkind, epub, generate
from lorekeeper.epub import Chapter
from tests.test_generate import FakeLLM, make_epub


def fake_book(subjects=(), titles=("Chapter One", "Chapter Two")):
    return SimpleNamespace(subjects=list(subjects), chapters=[Chapter(t, i) for i, t in enumerate(titles)],
                           blocks=[SimpleNamespace(raw=f"Paragraph {i}.", words=["paragraph", str(i)]) for i in range(len(titles))],
                           title="T", authors=["A"])


class Scripted:
    name = "scripted"

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def complete(self, system, prompt, schema, prefix=None):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_subjects_decide():
    assert bookkind.infer(fake_book(["Horror tales"])).kind == "fiction"
    r = bookkind.infer(fake_book(["Natural selection", "Evolution (Biology)"]))
    assert (r.kind, r.basis) == ("non-fiction", "subjects")
    assert bookkind.infer(fake_book(["Conduct of life"])).kind == "self-help"


def test_fiction_wins_a_tie_with_non_fiction():
    assert bookkind.infer(fake_book(["History -- Fiction", "Great Britain -- History"])).kind == "fiction"


def test_structure_when_subjects_are_silent():
    nf = bookkind.infer(fake_book(titles=["Introduction", "Notes", "Bibliography", "Index"]))
    assert (nf.kind, nf.basis) == ("non-fiction", "structure")
    sh = bookkind.infer(fake_book(titles=["Habit 1: Begin", "Habit 2: Plan", "Exercises"]))
    assert (sh.kind, sh.basis) == ("self-help", "structure")
    fi = bookkind.infer(fake_book(titles=["Chapter 1", "Chapter II", "Chapter Three", "Four"]))
    assert (fi.kind, fi.basis) == ("fiction", "structure")


def test_model_when_everything_else_is_silent():
    llm = Scripted({"kind": "self-help", "why": "advice to the reader"})
    r = bookkind.infer(fake_book(titles=["Alpha", "Beta"]), llm)
    assert (r.kind, r.basis, r.why, llm.calls) == ("self-help", "model", "advice to the reader", 1)


def test_model_is_not_asked_when_subjects_answer():
    llm = Scripted({"kind": "self-help", "why": "x"})
    assert bookkind.infer(fake_book(["Horror tales"]), llm).basis == "subjects"
    assert llm.calls == 0


def test_malformed_model_answers_fall_back_to_default():
    for bad in ({"kind": "cookbook", "why": "x"}, {}, "nonsense", RuntimeError("down")):
        r = bookkind.infer(fake_book(titles=["Alpha", "Beta"]), Scripted(bad))
        assert (r.kind, r.basis) == ("fiction", "default")


def test_default_without_a_model():
    assert bookkind.infer(fake_book(titles=["Alpha", "Beta"])).basis == "default"


def _gen(tmp_path, opts=None, llm=None, **kw):
    p = make_epub(tmp_path / "b.epub")
    out = tmp_path / "out.lore.json"
    logs = []
    report = generate.run(epub.read(p), llm or FakeLLM(), opts or generate.Options(), p, out, log=logs.append, **kw)
    return json.loads(out.read_text()), report, logs


def test_generate_records_the_inferred_kind(tmp_path):
    ix, report, logs = _gen(tmp_path)
    assert ix["bookKind"] == "fiction"
    assert report["bookKind"]["basis"] == "structure"
    assert any(m.startswith("book kind: fiction (structure") for m in logs)
    assert not any("extraction comes in a later phase" in m for m in logs)


def test_a_chosen_kind_is_recorded_and_noticed_once(tmp_path):
    ix, report, logs = _gen(tmp_path, generate.Options(book_kind="non-fiction"))
    assert ix["bookKind"] == "non-fiction"
    assert report["bookKind"]["basis"] == "chosen"
    assert logs.count(generate.NON_STORY_NOTICE.format(kind="non-fiction")) == 1
    assert "building a story index for a non-fiction book; non-fiction extraction comes in a later phase" in logs


def test_resume_keeps_the_recorded_kind(tmp_path):
    _gen(tmp_path, generate.Options(book_kind="self-help"))
    ix, _r, _logs = _gen(tmp_path, resume=True)
    assert ix["bookKind"] == "self-help"
    assert _r["bookKind"]["basis"] == "resumed"


def test_model_step_skips_front_matter():
    book = fake_book(titles=["Contents", "Preface", "Alpha", "Beta"])
    book.blocks = [SimpleNamespace(raw=r, words=[r]) for r in ("TOC", "PREF", "OPENALPHA", "OPENBETA")]
    seen = []

    class Spy(Scripted):
        def complete(self, system, prompt, schema, prefix=None):
            seen.append(prompt)
            return super().complete(system, prompt, schema, prefix)

    bookkind.infer(book, Spy({"kind": "fiction", "why": "x"}))
    opening = seen[0].split("Opening:")[1]
    assert "OPENALPHA" in opening and "OPENBETA" in opening
    assert "TOC" not in opening and "PREF" not in opening


def test_model_reason_stays_out_of_the_run_log(tmp_path):
    llm = FakeLLM()
    logs = []
    gen = SimpleNamespace(kind=None)
    gen.kind = bookkind.Inference("fiction", "model", "secret plot hint")
    book = fake_book(titles=["Alpha", "Beta"])
    orig = bookkind.infer
    bookkind.infer = lambda b, l=None: gen.kind
    try:
        generate._settle_kind(gen, book, llm, generate.Options(), tmp_path / "o.json", False, logs.append)
    finally:
        bookkind.infer = orig
    assert logs == ["book kind: fiction (model)"]


def test_non_fiction_subject_is_not_fiction():
    for s in (["Non-fiction"], ["Nonfiction -- History"]):
        assert bookkind.infer(fake_book(s)).kind == "non-fiction"


def test_vague_self_help_words_lose_to_non_fiction():
    for s in (["Royal succession"], ["Characters and characteristics"], ["Animals -- Habits and behavior"]):
        assert bookkind.infer(fake_book(s)).kind != "self-help", s
    assert bookkind.infer(fake_book(["Animals -- Habits and behavior"])).kind == "non-fiction"
    assert bookkind.infer(fake_book(["Conduct of life", "Philosophy"])).kind == "self-help"
    assert bookkind.infer(fake_book(["Success"])).kind == "self-help"


def test_one_self_help_title_is_no_lean():
    r = bookkind.infer(fake_book(titles=["The Lesson", "The Return", "The End"]))
    assert r.basis != "structure" or r.kind != "self-help"
    assert bookkind._from_titles(["The Lesson", "The Return", "The End"]) is None


def test_model_step_lets_cancellation_through():
    import pytest
    from lorekeeper.llm import Cancelled
    with pytest.raises(Cancelled):
        bookkind.infer(fake_book(titles=['Alpha', 'Beta']), Scripted(Cancelled()))
    assert bookkind.infer(fake_book(titles=['Alpha', 'Beta']), Scripted(RuntimeError("x"))).basis == "default"


def test_numbered_chapters_beat_front_matter():
    t = ["Introduction", "Notes"] + [f"Chapter {i}" for i in range(1, 41)]
    r = bookkind.infer(fake_book(titles=t))
    assert (r.kind, r.basis) == ("fiction", "structure")
    r = bookkind.infer(fake_book(titles=["Preface", "Chapter One", "Chapter Two"]))
    assert (r.kind, r.basis) == ("fiction", "structure")
    assert bookkind._from_titles(["Preface", "Introduction", "Alpha", "Beta"]) is None


def test_topical_subjects_with_a_fiction_word_are_non_fiction():
    for s in ("Western Australia -- History", "English poetry -- History and criticism",
              "Romance languages -- Grammar", "Ghost towns -- Nevada",
              "Adventure and adventurers -- Biography", "Folklore -- Ireland"):
        assert bookkind.infer(fake_book([s])).kind == "non-fiction", s
    for s in ("Ghost stories", "Vampires -- Fiction", "Mystery", "Poetry -- Collections", "Horror tales"):
        assert bookkind.infer(fake_book([s])).kind == "fiction", s


def test_form_subdivisions_stay_fiction():
    for s in ("Tragedies (Drama)", "Hamlet (Legendary character) -- Drama", "Kings and rulers -- Succession -- Drama",
              "Revenge -- Drama", "Princes -- Drama", "Denmark -- Drama", "Love -- Poetry", "Vampires -- Drama",
              "English drama -- 17th century", "English poetry -- 19th century", "Satire, English -- 18th century"):
        assert bookkind.infer(fake_book([s])).kind == "fiction", s


def test_back_matter_switches_off_the_plain_chapter_shortcut():
    t = [f"Chapter {i}" for i in range(1, 21)] + ["Bibliography", "Index"]
    assert bookkind.infer(fake_book(titles=t)).kind == "non-fiction"
    t = ["Introduction", "Notes"] + [f"Chapter {i}" for i in range(1, 41)]
    assert bookkind.infer(fake_book(titles=t)).kind == "fiction"
