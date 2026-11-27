"""Does a saved style's own reference picture carry its look into the drawings, and does its subject leak into them?

    set -a; . ./.env; set +a
    uv run --extra claude python bench/style_reference.py INDEX --epub EPUB --out DIR \
        --ref NAME=PICTURE[:BUILTIN] [--ref ...] [--describe PICTURE ...] --portrait ID --event ID --at ANCHOR \
        [--quality standard] [--image-model gemini-nano-banana-2.1] [--cap 3]

For each `--ref` the script asks Claude's cheap model to describe the picture's drawing style (`art.describe_style`), then
draws one portrait (`--portrait`, an entity id) and one scene (`--event`, an event id), both as of `--at`, in three versions:
(a) the described sentence only, (b) the sentence plus the picture as the style reference (`art.SAVED_STYLE_REFS`, the path
every drawing in a saved style with a picture takes), and (c) the built-in style named after the colon, as a baseline. The
two prompts are written once, so the versions differ only in the style sentence and the picture. `--describe` adds pictures
that are only described. Everything goes into DIR: one art set per version under `sets/`, `results.json` (descriptions,
prompts, notes sent, costs) for `bench/style_reference_sheet.py` to lay out as a contact sheet.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from lorekeeper import art, epub, llm


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
    ap.add_argument("--ref", action="append", default=[], help="NAME=PICTURE[:BUILTIN]")
    ap.add_argument("--describe", action="append", default=[], help="a picture only to describe")
    ap.add_argument("--portrait", required=True)
    ap.add_argument("--event", required=True)
    ap.add_argument("--at", required=True)
    ap.add_argument("--quality", default="standard")
    ap.add_argument("--image-model", default="gemini-nano-banana-2.1")
    ap.add_argument("--cap", type=float, default=3.0)
    a = ap.parse_args()

    doc = json.loads(Path(a.index).read_text())
    bk = epub.read(a.epub)
    out = Path(a.out)
    (out / "sets").mkdir(parents=True, exist_ok=True)
    cheap = llm.PROVIDERS["claude"]["cheap"]
    eye = llm.make("claude", cheap, api_key=llm.env_key("claude"))
    writer = llm.make("claude", cheap, api_key=llm.env_key("claude"))
    image = Noting(llm.make_image("gemini", a.image_model, api_key=llm.env_key("gemini")))
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731

    refs = []
    for spec in a.ref:
        name, rest = spec.split("=", 1)
        path, _, builtin = rest.partition(":")
        refs.append((name, Path(path), builtin or None))

    # the descriptions: every reference and every --describe picture
    described = {}
    for name, path in [(n, p) for n, p, _ in refs] + [(Path(p).stem, Path(p)) for p in a.describe]:
        data = path.read_bytes()
        before = eye.usage.cost_usd
        try:
            text = art.describe_style(eye, data, art.mime_of(data) or "image/png")
        except ValueError as e:
            text = f"(failed: {e})"
        described[name] = {"picture": str(path), "text": text, "cost": round(eye.usage.cost_usd - before, 6)}
        log(f"{name}: {text}")

    # the two prompts, written once in a scratch set: the writer never sees the style or the picture
    scratch = art.Illustrator([art.Book(doc, bk, art.FileSink(out / "sets" / "scratch"), None)], writer, image, "ink",
                              portraits=None, quality=a.quality, workers=1, log=log, pause=0)
    jobs = {"portrait": scratch.job("portrait", a.portrait, at=a.at, looks="off"),
            "scene": scratch.job("event", a.event, at=a.at)}
    prompts = {k: scratch.make_prompt(j) for k, j in jobs.items()}

    spent = 0.0
    rows = []
    for name, path, builtin in refs:
        desc = described[name]["text"]
        data = path.read_bytes()
        versions = {"a": f"{name}-a", "b": f"{name}-b"} | ({"c": builtin} if builtin else {})
        art.SAVED_STYLES = {**art.SAVED_STYLES, f"{name}-a": desc, f"{name}-b": desc}
        art.SAVED_STYLE_REFS = {**art.SAVED_STYLE_REFS, f"{name}-b": {"path": str(path.resolve()),
                                                                   "mime": art.mime_of(data) or "image/png"}}
        row = {"name": name, "picture": str(path), "description": desc, "builtin": builtin, "drawn": {}}
        for v, slug in versions.items():
            il = art.Illustrator([art.Book(doc, bk, art.FileSink(out / "sets" / slug), None)], writer, image, slug,
                                 portraits=None, quality=a.quality, workers=1, log=log, pause=0,
                                 cap=max(0.0, a.cap - spent))
            for kind, base in prompts.items():
                pr = copy.deepcopy(base)
                pr.style = il.style_sentence()
                pr.text = il._assemble(pr.body, style=pr.style)
                pr.references = []
                job = il.job("portrait" if kind == "portrait" else "event", jobs[kind].target, at=a.at,
                             **({"looks": "off"} if kind == "portrait" else {}))
                rec = il.draw(job, prompt=pr)
                sent = image.sent[-1] if image.sent else {}
                row["drawn"][f"{kind}-{v}"] = {"file": str((out / "sets" / slug / rec["file"]).relative_to(out)) if rec else None,
                                               "prompt": pr.text, "note": sent.get("note", ""),
                                               "references": sent.get("references", 0),
                                               "styleRef": (rec or {}).get("styleRef"), "cost": (rec or {}).get("cost", 0)}
            spent += il.report.cost
        rows.append(row)
    result = {"index": a.index, "at": a.at, "portrait": a.portrait, "event": a.event, "quality": a.quality,
              "image_model": image.name, "describer": cheap, "described": described,
              "prompts": {k: {"body": p.body, "text": p.text} for k, p in prompts.items()}, "rows": rows,
              "cost": {"pictures": round(spent, 4), "text": round(eye.usage.cost_usd + writer.usage.cost_usd, 4)}}
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps(result["cost"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
