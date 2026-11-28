"""Do the person's own reference pictures (`art.UserRef`) carry what their note asks for into a drawing, without their
drawing style leaking over the chosen style or the labelled figures losing their faces?

    set -a; . ./.env; set +a
    uv run --extra claude python bench/user_refs.py examples/dracula/dracula.json --epub corpus/pg345/pg345.epub --out DIR \
        [--style watercolour] [--quality standard] [--image-model gemini-nano-banana-2.1] [--cap 1.5]

It draws the three reference pictures first, as photographs (one Gemini call each: a dress, a man's jacket, a house), then
into one art set: the portraits of the scenes' figures, so the scenes have labelled figure pictures; two portraits and two
scenes twice each from one written prompt, (a) with nothing of the person's and (b) with one picture and its note; and one
portrait through the describe path (the image model forced to "takes no pictures", the picture described by Claude's strong
model). Everything goes into DIR: `refs/`, `sets/`, `results.json`; `bench/user_refs_sheet.py DIR` lays out the contact
sheet. The cap covers every image call.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

from lorekeeper import art, epub, llm

REFS = {
    "dress": ("A studio photograph of a long emerald-green velvet dress with a tall white lace collar, puffed shoulders and "
              "a row of small gold buttons down the front, on a dressmaker's mannequin against a plain grey backdrop, soft "
              "even light.", "use this for her dress"),
    "jacket": ("A studio photograph of a rust-orange checked tweed jacket with wide lapels over a mustard-yellow waistcoat and "
               "a white high collar with a black silk cravat, on a tailor's dummy against a plain grey backdrop.",
               "use this for his jacket, waistcoat and cravat"),
    "house": ("A photograph of a tall narrow red-brick house with a round corner tower topped by a pointed green copper roof, "
              "white window frames and a black iron gate, overcast daylight.", "this is the house"),
}
PORTRAITS = [("lucy-westenra", "toc15.para49", "dress"), ("jonathan-harker", "toc15.para49", "jacket")]
SCENES = [("dracula.ev.141", "toc15.para49", "dress", "use this for the woman's dress"),
          ("dracula.ev.130", "toc15.para01", "house", "this is the house")]
FIGURES = ["arthur", "dr-john-seward"]      # the scenes' other figures, drawn first so their faces are references


class Noting:
    """The image model, keeping what each call was sent besides the prompt."""

    def __init__(self, model):
        self.model, self.sent = model, []
        self.provider, self.name = getattr(model, "provider", ""), getattr(model, "name", "")

    def draw(self, prompt, **kw):
        self.sent.append({"prompt": prompt, "note": kw.get("note", ""), "references": len(kw.get("references") or [])})
        return self.model.draw(prompt, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index")
    ap.add_argument("--epub", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--style", default="watercolour")
    ap.add_argument("--quality", default="standard")
    ap.add_argument("--image-model", default="gemini-nano-banana-2.1")
    ap.add_argument("--cap", type=float, default=1.5)
    a = ap.parse_args()

    doc = json.loads(Path(a.index).read_text())
    bk = epub.read(a.epub)
    out = Path(a.out)
    (out / "refs").mkdir(parents=True, exist_ok=True)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    writer = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
    eye = llm.make("claude", llm.PROVIDERS["claude"]["strong"], api_key=llm.env_key("claude"))
    raw = llm.make_image("gemini", a.image_model, api_key=llm.env_key("gemini"))
    image = Noting(raw)
    spent = 0.0

    # the person's pictures, drawn as photographs (kept across reruns)
    refs: dict[str, art.UserRef] = {}
    for name, (prompt, note) in REFS.items():
        f = next(iter(sorted((out / "refs").glob(f"{name}.*"))), None)
        if f is None:
            pic = raw.draw(prompt, shape="square", quality="draft")
            spent += pic.cost
            f = out / "refs" / f"{name}.{art._ext(pic.mime or 'image/png')}"
            f.write_bytes(pic.data)
        data = f.read_bytes()
        refs[name] = art.UserRef(data, art.mime_of(data) or "image/png", hashlib.sha256(data).hexdigest(), note)

    il = art.Illustrator([art.Book(doc, bk, art.FileSink(out / "sets" / art.style_slug(a.style)), None)], writer, image,
                         a.style, portraits=None, quality=a.quality, workers=1, log=log, pause=0, cap=max(0.0, a.cap - spent),
                         describer=eye)
    rows = []

    def file(rec):
        return str((out / "sets" / art.style_slug(a.style) / rec["file"]).relative_to(out)) if rec else None

    for eid in FIGURES:
        il.draw(il.job("portrait", eid, at="toc15.para49"))
    for eid, at, ref in PORTRAITS:
        job = il.job("portrait", eid, at=at)
        pr = il.make_prompt(job)
        ra = il.draw(job, prompt=copy.deepcopy(pr))
        rb = il.draw(job, prompt=copy.deepcopy(pr), user_refs=[refs[ref]])
        rows.append({"kind": "portrait", "target": eid, "ref": ref, "note": refs[ref].note, "a": file(ra), "b": file(rb),
                     "prompt": pr.text, "note_b": image.sent[-1]["note"]})
    for ev, at, ref, note in SCENES:
        job = il.job("event", ev, at=at)
        pr = il.make_prompt(job)
        mine = art.UserRef(refs[ref].data, refs[ref].mime, refs[ref].sha, note)
        ra = il.draw(job, prompt=copy.deepcopy(pr))
        rb = il.draw(job, prompt=copy.deepcopy(pr), user_refs=[mine])
        rows.append({"kind": "event", "target": ev, "ref": ref, "note": note, "a": file(ra), "b": file(rb), "prompt": pr.text,
                     "figures_a": (ra or {}).get("figures"), "figures_b": (rb or {}).get("figures"),
                     "note_b": image.sent[-1]["note"], "refsLeftOut": (rb or {}).get("refsLeftOut", 0)})

    # the describe path: the same model, forced to take no pictures
    eid, at, ref = PORTRAITS[0]
    real = llm.image_takes_references
    llm.image_takes_references = lambda provider, model: False
    try:
        job = il.job("portrait", eid, at=at)
        rd = il.draw(job, prompt=copy.deepcopy(il.make_prompt(job)), user_refs=[refs[ref]])
    finally:
        llm.image_takes_references = real
    described = {"target": eid, "ref": ref, "file": file(rd), "prompt": (rd or {}).get("prompt"),
                 "userRefs": (rd or {}).get("userRefs"), "sent": image.sent[-1]}

    spent += il.report.cost
    result = {"index": a.index, "style": a.style, "quality": a.quality, "image_model": image.name, "writer": writer.name,
              "describer": eye.name, "refs": {k: {"file": str(next((out / "refs").glob(f"{k}.*")).relative_to(out)),
                                                  "prompt": p} for k, (p, _) in REFS.items()},
              "rows": rows, "described": described,
              "cost": {"pictures": round(spent, 4), "text": round(il.text_spent(), 4),
                       "text_by_role": {k: round(v, 4) for k, v in il.text_costs().items()}},
              "report": il.report.lines()}
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps(result["cost"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
