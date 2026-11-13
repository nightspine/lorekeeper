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
    nf = bookkind.infer(fake_book(titles=["Preface", "Introduction", "Part 1", "Notes"]))
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
    assert generate.NON_STORY_NOTICE not in logs


def test_a_chosen_kind_is_recorded_and_noticed_once(tmp_path):
    ix, report, logs = _gen(tmp_path, generate.Options(book_kind="non-fiction"))
    assert ix["bookKind"] == "non-fiction"
    assert report["bookKind"]["basis"] == "chosen"
    assert logs.count(generate.NON_STORY_NOTICE) == 1


def test_resume_keeps_the_recorded_kind(tmp_path):
    _gen(tmp_path, generate.Options(book_kind="self-help"))
    ix, _, logs = _gen(tmp_path, resume=True)
    assert ix["bookKind"] == "self-help"
