"""Does sending a saved style's picture keep the style more consistent? Run after `bench/style_reference.py` in the same DIR.

    set -a; . ./.env; set +a
    uv run --extra claude python bench/style_consistency.py INDEX --epub EPUB --out DIR --ref NAME [--ref NAME] \
        [--repeats 3] [--crowd EVENT --crowd-at ANCHOR --crowd-ref NAME] [--cap 1.5]

For each `--ref` (a reference of the earlier run, with its described sentence and picture from DIR/results.json) the same
scene prompt (the earlier run's, as written) is drawn until there are `--repeats` pictures under (a) the sentence only and
`--repeats` under (b) the sentence plus the picture, counting the one the earlier run drew. `--crowd`: one crowded scene in
(b) for `--crowd-ref`, whose people get their portraits first in that set, so the scene carries up to four labelled figure
references before the style picture. Writes DIR/consistency.json; `bench/style_reference_sheet.py` lays it out.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lorekeeper import art, epub, llm


class Noting:
    def __init__(self, model):
        self.model, self.sent = model, []
        self.provider, self.name = getattr(model, "provider", ""), getattr(model, "name", "")

    def draw(self, prompt, **kw):
        self.sent.append({"note": kw.get("note", ""), "references": len(kw.get("references") or [])})
        return self.model.draw(prompt, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index")
    ap.add_argument("--epub", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--crowd")
    ap.add_argument("--crowd-at")
    ap.add_argument("--crowd-ref")
    ap.add_argument("--cap", type=float, default=1.5)
    a = ap.parse_args()

    out = Path(a.out)
    res = json.loads((out / "results.json").read_text())
    doc = json.loads(Path(a.index).read_text())
    bk = epub.read(a.epub)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    writer = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
    image = Noting(llm.make_image("gemini", res["image_model"], api_key=llm.env_key("gemini")))
    rows = {r["name"]: r for r in res["rows"]}
    spent = 0.0

    def illustrator(slug: str, row: dict, send: bool) -> art.Illustrator:
        path = Path(row["picture"]).resolve()
        art.SAVED_STYLES = {**art.SAVED_STYLES, slug: row["description"]}
        if send:
            art.SAVED_STYLE_REFS = {**art.SAVED_STYLE_REFS, slug: {"path": str(path), "mime": art.mime_of(path.read_bytes())}}
        return art.Illustrator([art.Book(doc, bk, art.FileSink(out / "sets" / slug), None)], writer, image, slug,
                               portraits=None, quality=res["quality"], workers=1, log=log, pause=0,
                               cap=max(0.0, a.cap - spent))

    result = {"repeats": {}, "crowd": None}
    body = res["prompts"]["scene"]["body"]
    for name in a.ref:
        row = rows[name]
        got = {}
        for v in ("a", "b"):
            slug = f"{name}-{v}"
            il = illustrator(slug, row, v == "b")
            files = [row["drawn"][f"scene-{v}"]["file"]]
            job = il.job("event", res["event"], at=res["at"])
            for _ in range(a.repeats - 1):
                style = il.style_sentence()
                pr = art.Prompt(text=il._assemble(body, style=style), scene=body, anchor=job.anchor, shape="landscape",
                                body=body, style=style, figures=None)
                rec = il.draw(job, prompt=pr)
                files.append(str((out / "sets" / slug / rec["file"]).relative_to(out)) if rec else None)
            spent += il.report.cost
            got[v] = files
        result["repeats"][name] = got

    if a.crowd:
        row = rows[a.crowd_ref]
        slug = f"{a.crowd_ref}-crowd"
        il = illustrator(slug, row, True)
        ev = next(e for e in doc["events"] if e["id"] == a.crowd)
        people = [x for x in ev["entities"] if next(e for e in doc["entities"] if e["id"] == x)["type"] in art.PORTRAIT_TYPES]
        portraits = {}
        for x in people[:art.MAX_REFERENCES]:
            rec = il.draw(il.job("portrait", x, at=a.crowd_at, looks="off"))
            portraits[x] = str((out / "sets" / slug / rec["file"]).relative_to(out)) if rec else None
        job = il.job("event", a.crowd, at=a.crowd_at)
        pr = il.make_prompt(job)
        rec = il.draw(job, prompt=pr)
        sent = image.sent[-1]
        spent += il.report.cost
        result["crowd"] = {"ref": a.crowd_ref, "event": a.crowd, "summary": ev["summary"], "portraits": portraits,
                           "file": str((out / "sets" / slug / rec["file"]).relative_to(out)) if rec else None,
                           "prompt": pr.text, "figures": pr.figures, "note": sent["note"], "references": sent["references"],
                           "styleRef": (rec or {}).get("styleRef")}
    result["cost"] = {"pictures": round(spent, 4), "text": round(writer.usage.cost_usd, 4)}
    (out / "consistency.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps(result["cost"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
