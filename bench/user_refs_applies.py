"""Does tying one of the person's reference pictures to a labelled figure (`UserRef.applies`) put what its note names on that
figure alone, and keep every figure who the prompt says they are?

    set -a; . ~/projects/lorekeeper/.env; set +a
    uv run --extra claude python bench/user_refs_applies.py --out DIR \
        --rr-index RR.lore.json --rr-epub RR.epub --rr-refs RR_DIR \
        [--dracula examples/dracula/dracula.json --dracula-epub corpus/pg345/pg345.epub] [--dry] [--cap 1.5]

Each case is one written prompt (one writer call, Claude's cheap model), drawn twice per condition on Gemini
`gemini-nano-banana-2.1` at standard, in watercolour, so only the reference note differs between conditions:

- the whole picture ("scene"), as #170 sent it;
- tied to a figure, named by entity and turned into that figure's label in the written prompt;
- tied to the subject, for a portrait;
- "noguard": tied to a figure, the closing line without "and never who that figure is" (the wording that shipped);
- "strong" (`--extend`, on the stored prompts): the figure line keeping the figure's sex, age and people.

Then a vision model (Claude's strong model) judges every drawing blind against `RUBRIC`: it sees the drawing, the person's
reference photograph and the prompt as drawn, never the condition or the note. `--dry` writes the prompts and prints their
figures, with no image call. Everything goes into DIR: `sets/`, `results.json`; `bench/user_refs_applies_sheet.py DIR` lays
out the contact sheet. The cap covers every image call.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

from lorekeeper import art, epub, llm

DRESS = "use this for the woman's dress"
# (case id, book, kind, target, at, [(reference, note, applies)], conditions); applies "@<entity id>" is that entity's label
CASES = [
    ("rr-plate-1", "rr", "plate", "Chapter 1: Helldiver", None, "dress", DRESS, ["scene", "@narol", "noguard@narol"]),
    ("rr-ev28", "rr", "event", "red-rising.ev.28", "toc11.para02", "dress", DRESS,
     ["scene", "@little-eo", "@ugly-dan", "noguard@ugly-dan"]),
]
TWO = ("dr-ev141", "dr", "event", "dracula.ev.141", "toc15.para49",
       [("jacket", "use this for his jacket, waistcoat and cravat", "arthur"),
        ("dress", "use this for her dress", "lucy-westenra")])
PORTRAIT = ("dr-lucy", "dr", "portrait", "lucy-westenra", "toc15.para49", "dress", "use this for her dress", ["scene", "subject"])
TIMES = 2
NOGUARD = ("Take only what the person says from their pictures, never their drawing style; where their note and the prompt "
           "disagree about that thing, draw it as their picture shows, but a picture that is only for one figure changes that "
           "thing on that figure alone.")

# a stronger figure line tried after the first run: the figure's identity kept inside the line itself
STRONG_LINE = ("Reference picture {n} is from the person and is only for figure {label}: {note}. Use it for figure {label} "
               "alone and only for that thing: figure {label} stays who the prompt says, the same sex, age and people, and "
               "nothing else about figure {label}, and nothing about any other figure, changes.")
STRONG = {"rr-plate-1": ["narol"], "rr-ev28": ["ugly-dan", "little-eo"]}

RUBRIC = """You judge a drawing made by an image model. You get: picture 1, the drawing; then one or more reference photographs the person gave (numbered after it), each of one garment; and the prompt the drawing was made from, in which figures are called "figure A", "figure B" and so on.

Look closely, then answer:
- "figures": every person drawn, in the prompt's labels where you can tell which is which (else "unlabelled"), each with "sex" as drawn ("man", "woman", "unclear") and "wears", for each reference photograph by its number, whether that figure wears that garment ("yes", "partly", "no"). A garment counts when its colour, material and cut plainly match the photograph; a garment the prompt itself asks for does not count unless it matches the photograph.
- "sexes_as_prompt": true when every figure the prompt describes is drawn as the sex the prompt gives (a figure the prompt calls a man, a boy or "he" is a man), false otherwise.
- "changed": one short sentence on anything the drawing shows that the prompt does not ask for (a figure added, a figure of the other sex, a garment out of place), or "" for nothing.
Judge only what you see. Never guess what the person wanted."""
SCHEMA = {"type": "object", "properties": {
    "figures": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "sex": {"type": "string", "enum": ["man", "woman", "unclear"]},
        "wears": {"type": "array", "items": {"type": "object", "properties": {
            "ref": {"type": "integer"}, "value": {"type": "string", "enum": ["yes", "partly", "no"]}},
            "required": ["ref", "value"]}}}, "required": ["label", "sex", "wears"]}},
    "sexes_as_prompt": {"type": "boolean"}, "changed": {"type": "string"}},
    "required": ["figures", "sexes_as_prompt", "changed"]}


class Noting:
    """The image model, keeping what each call was sent besides the prompt."""

    def __init__(self, model):
        self.model, self.sent = model, []
        self.provider, self.name = getattr(model, "provider", ""), getattr(model, "name", "")

    def draw(self, prompt, **kw):
        self.sent.append({"prompt": prompt, "note": kw.get("note", ""), "references": len(kw.get("references") or [])})
        return self.model.draw(prompt, **kw)


def load_ref(path: Path, note: str, applies: str = "scene") -> art.UserRef:
    data = path.read_bytes()
    return art.UserRef(data, art.mime_of(data) or "image/jpeg", hashlib.sha256(data).hexdigest(), note, None, applies)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--rr-index", required=True)
    ap.add_argument("--rr-epub", required=True)
    ap.add_argument("--rr-refs", required=True, help="folder with dress.jpg and jacket.jpg")
    ap.add_argument("--dracula", default="examples/dracula/dracula.json")
    ap.add_argument("--dracula-epub", default="corpus/pg345/pg345.epub")
    ap.add_argument("--style", default="watercolour")
    ap.add_argument("--quality", default="standard")
    ap.add_argument("--image-model", default="gemini-nano-banana-2.1")
    ap.add_argument("--cap", type=float, default=1.5)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--extend", action="store_true",
                    help="draw the STRONG conditions on the prompts results.json already holds, judge them and add them")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    writer = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
    judge = llm.make("claude", llm.PROVIDERS["claude"]["strong"], api_key=llm.env_key("claude"))
    image = Noting(llm.make_image("gemini", a.image_model, api_key=llm.env_key("gemini")))
    refdir = Path(a.rr_refs)
    refs = {k: refdir / f"{k}.jpg" for k in ("dress", "jacket")}
    slug = art.style_slug(a.style)

    def illustrator(index, ep, name):
        doc = json.loads(Path(index).read_text())
        return art.Illustrator([art.Book(doc, epub.read(ep), art.FileSink(out / "sets" / name / slug), None)], writer, image,
                               a.style, portraits=None, quality=a.quality, workers=1, log=log, pause=0, cap=a.cap)

    ils = {"rr": illustrator(a.rr_index, a.rr_epub, "red-rising"), "dr": illustrator(a.dracula, a.dracula_epub, "dracula")}
    prompts: dict[str, tuple[art.Job, art.Prompt]] = {}
    before = json.loads((out / "results.json").read_text()) if a.extend else None
    for cid, bk, kind, target, at, *_ in [] if a.extend else [*CASES, TWO, PORTRAIT]:
        il = ils[bk]
        job = il.job(kind, target, at=at)
        pr = il.make_prompt(job)
        prompts[cid] = (job, pr)
        log(f"{cid}: figures {pr.figures}\n  {pr.body}")
    if a.dry:
        return 0

    def label(cid, eid):
        f = next((f for f in prompts[cid][1].figures or [] if f["entity"] == eid), None)
        if f is None:
            raise SystemExit(f"{cid}: {eid} has no label in the written prompt; pick another scene")
        return f["label"]

    rows = []

    def draw(cid, cond, mine, guard=True, strong=False):
        il = ils[cid[:2]]
        job, pr = prompts[cid]
        saved, line = art.USER_REF_SCOPED, art.USER_REF_FIGURE_LINE
        if not guard:
            art.USER_REF_SCOPED = NOGUARD
        if strong:
            art.USER_REF_FIGURE_LINE = STRONG_LINE
        try:
            for n in range(TIMES):
                rec = il.draw(job, prompt=copy.deepcopy(pr), user_refs=mine)
                book = "red-rising" if cid.startswith("rr") else "dracula"
                rows.append({"case": cid, "condition": cond, "take": n + 1, "kind": job.kind, "target": job.target,
                             "file": str((out / "sets" / book / slug / rec["file"]).relative_to(out)) if rec else None,
                             "prompt": pr.text, "figures": pr.figures, "note": image.sent[-1]["note"],
                             "refs": [{"ref": k, "note": u.note, "applies": u.applies} for k, u in
                                      zip([r for r in mine_names], mine)],
                             "userRefs": (rec or {}).get("userRefs"), "appliesFallback": (rec or {}).get("appliesFallback")})
                log(f"  {cid} {cond} #{n + 1}: {rows[-1]['file']}")
        finally:
            art.USER_REF_SCOPED, art.USER_REF_FIGURE_LINE = saved, line

    if a.extend:
        # the same written prompts as the first run: every figure there is portrait="no", so no figure picture was sent and
        # a prompt rebuilt from the stored text and figures draws with the same pictures (the person's only)
        for cid, bk, kind, target, at, ref, note, _ in CASES:
            r0 = next(r for r in before["rows"] if r["case"] == cid)
            job = ils[bk].job(kind, target, at=at)
            prompts[cid] = (job, art.Prompt(text=r0["prompt"], scene=r0["prompt"], anchor=job.anchor,
                                            shape=job.shape or art.SHAPE_OF[kind], body=r0["prompt"],
                                            figures=[dict(f) for f in r0["figures"]]))
            for eid in STRONG[cid]:
                mine_names = [ref]
                draw(cid, f"strong@{eid}", [load_ref(refs[ref], note, label(cid, eid))], strong=True)

    for cid, bk, kind, target, at, ref, note, conds in [] if a.extend else CASES + [PORTRAIT]:
        for cond in conds:
            guard = not cond.startswith("noguard")
            applies = cond.split("@", 1)[1] if "@" in cond else cond
            applies = label(cid, applies) if "@" in cond else applies
            mine_names = [ref]
            draw(cid, cond, [load_ref(refs[ref], note, applies)], guard)
    if not a.extend:
        cid = TWO[0]
        mine_names = [r for r, _, _ in TWO[5]]
        draw(cid, "scene", [load_ref(refs[r], n) for r, n, _ in TWO[5]])
        draw(cid, "@figures", [load_ref(refs[r], n, label(cid, e)) for r, n, e in TWO[5]])

    # the blind judge: the drawing, the reference photographs, the prompt as drawn
    for row in rows:
        if not row["file"]:
            continue
        pics = [(out / row["file"]).read_bytes()] + [refs[r["ref"]].read_bytes() for r in row["refs"]]
        ask = f"<prompt>{row['prompt']}</prompt>\nPicture 1 is the drawing; pictures 2 to {len(pics)} are the reference photographs."
        try:
            row["judge"] = judge.complete(RUBRIC, ask, SCHEMA, images=[(p, art.mime_of(p) or "image/jpeg") for p in pics])
        except Exception as e:      # noqa: BLE001 - a failed judgement is reported, not fatal
            row["judge"] = {"error": f"{type(e).__name__}: {e}"[:200]}

    spent = sum(il.report.cost for il in ils.values())
    text = sum(il.text_spent() for il in ils.values())
    if a.extend:
        before["rows"] += rows
        before.setdefault("extend_cost", {}).update({"pictures": round(spent, 4), "judge": round(judge.usage.cost_usd, 4)})
        (out / "results.json").write_text(json.dumps(before, indent=1, ensure_ascii=False))
        print(json.dumps(before["extend_cost"]))
        return 0
    result = {"style": a.style, "quality": a.quality, "image_model": image.name, "writer": writer.name, "judge": judge.name,
              "rows": rows, "cost": {"pictures": round(spent, 4), "text": round(text, 4),
                                     "judge": round(judge.usage.cost_usd, 4)},
              "reports": {k: il.report.lines() for k, il in ils.items()}}
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(json.dumps(result["cost"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
