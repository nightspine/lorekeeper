"""Ask the picture name check's choosing call (`art.NAMES`, Claude's cheap tier) about caught words in real and made-up sentences, three times each.

    set -a; . ./.env; set +a
    uv run --extra claude python bench/name_chooser.py
"""
import json, sys, tempfile
from pathlib import Path
from lorekeeper import art, epub, llm
d = json.load(open("examples/dracula/dracula.json"))
cheap = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
CASES = [  # (word, sentence, expected name?)
    ("Victorian", "A young woman in plain Victorian dress, dark hair pinned up.", False),
    ("Gothic", "Behind him, the Gothic arches of an old chapel fade into shadow.", False),
    ("English", "A slim young man in a sober English travelling suit.", False),
    ("Eastern", "A carved wooden inn of Eastern European style stands behind.", False),
    ("European", "A carved wooden inn of Eastern European style stands behind.", False),
    ("Lamplit", "Lamplit stone walls rise behind her.", False),
    ("Transylvanian", "A rough Transylvanian road winds up into the hills.", False),
    ("Englishwoman", "A young Englishwoman of gentle birth, fair and slight.", False),
    ("Dutch", "A Dutch physician, philosopher and scientist, a man of learning.", False),
    ("Netherlands", "A physician and scholar of the Netherlands, an Amsterdam man of learning.", True),
    ("Vlad", "A tall old man the villagers call Vlad waits by the coach.", True),
    ("Lucy", "Beside her stands Lucy, pale in a white nightgown.", True),
    ("Whitby", "Behind her the red roofs of Whitby fall toward the harbour.", True),
    ("Kestrel", "A banner of the Kestrel order hangs over the gate.", True),
]
bad = 0
for run in range(3):
    il = art.Illustrator([art.Book(d, None, art.FileSink(Path(tempfile.mkdtemp()) / "a"), None)], cheap, None, "ink",
                         portraits=None, cheap_text=cheap, log=lambda *_: None)
    il._sentence_of = staticmethod(lambda text, w: dict((c[0], c[1]) for c in CASES)[w])
    names = il._names_among("", [c[0] for c in CASES])
    for w, s, want in CASES:
        if (w in names) != want:
            bad += 1
            print(f"run {run}: {w} -> {'name' if w in names else 'not a name'}, wanted {'name' if want else 'not a name'}")
print(f"{bad} wrong of {3 * len(CASES)}; text ${il.text_spent():.4f} for the last run")
