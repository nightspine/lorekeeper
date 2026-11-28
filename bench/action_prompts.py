"""Do scene prompts make action read as action? Today's scene writer against candidate writers, judged blind.

    set -a; . ~/projects/lorekeeper/.env; set +a
    uv run --extra claude python bench/action_prompts.py --out DIR [--candidates old,c1] [--reuse EARLIER_DIR] \
        [--writer claude:claude-sonnet-5-5] [--image gemini:gemini-nano-banana-2.1] [--quality standard] \
        [--style digital-art] [--draws 1] [--only ID,ID] [--cap 3]

A fixed set of moments (`MOMENTS`: event ids of real books, each drawn at its own anchor, so the set is reproducible): 8
action moments (fights, a chase, a fall, a struggle) and 4 controls (calm, emotional, crowded, a place). For each
candidate (`bench/action_candidates.py`; "old" is the writer on this branch) and each moment the script writes the
event prompt the way the picture sheet does (`Illustrator.job` and `make_prompt` on a fresh, empty art set, so no
reference pictures; `--writer` writes, Claude's cheap tier makes the choosing calls), draws it `--draws` times on
`--image` at `--quality`, landscape, in `--style` (a refusal gets the pipeline's one quieter rewrite, `soften`), and
checks each picture for drawing faults with `art.check_picture` on `--checker`. Then the judge (`--judge`, a vision model)
compares each candidate's picture with the first candidate's (the baseline) for the same moment and draw, blind: it sees
the two pictures and the moment's material (never the prompts), in both orders, and scores each picture 1 to 5 on
dynamism, clarity of the action, fidelity, anatomy and overall, and says which it prefers.

DIR gets `results.json` (every prompt, picture, check and verdict), `summary.json` (means per criterion and group, the
preferences, the fault rates, the spend) and `index.html` (the contact sheet). `--reuse` takes the prompts, pictures,
checks and verdicts of an earlier DIR for the candidates it has, so a baseline is drawn once. The real books are bench
data only. Nothing is written outside DIR.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import shutil
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import action_candidates as cands
import action_lookup

from lorekeeper import art, epub, images, llm

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home() / "projects/lorekeeper"
BOOKS = {
    "dracula": (ROOT / "examples/dracula/dracula.json", HOME / "corpus/pg345/pg345.epub"),
    "eotv": (HOME / "lorekeeper-data/indexes/empire-of-the-vampire-3ec7065b2283.lore.json",
             HOME / "lorekeeper-data/books/865fa20bac04f9dd.epub"),
}
# (book, event id, group, what it is): the fixed set
MOMENTS = [
    ("dracula", "dracula.ev.52", "action", "a blow with a shovel, stopped"),
    ("dracula", "dracula.ev.125", "action", "a knife attack and a struggle"),
    ("dracula", "dracula.ev.346", "action", "a fight of several, with knives"),
    ("eotv", "empire-of-the-vampire.ev.48", "action", "a fight in a crypt, flail against two"),
    ("eotv", "empire-of-the-vampire.ev.58", "action", "a sword duel won with an elbow"),
    ("eotv", "empire-of-the-vampire.ev.166", "action", "back to back against a horde"),
    ("eotv", "empire-of-the-vampire.ev.149", "action", "a chase on horseback through a blizzard"),
    ("eotv", "empire-of-the-vampire.ev.150", "action", "a horse and riders falling over a cliff"),
    ("eotv", "empire-of-the-vampire.ev.61", "control", "calm: watching a falling star from a window"),
    ("eotv", "empire-of-the-vampire.ev.86", "control", "emotional: a company moved to tears by music"),
    ("dracula", "dracula.ev.215", "control", "crowded: five pledge with joined hands"),
    ("dracula", "dracula.ev.7", "control", "a place: a carriage stops in a ruined castle's courtyard"),
]
# (book, chapter title): comic pages of chapters with fights, to see how much of a scene writer's change carries into comics
COMICS = [
    ("eotv", "Chapter IV: House of the Dead"),
    ("eotv", "Chapter XII: Everything Falling Apart"),
    ("dracula", "CHAPTER XXVII MINA HARKER’S JOURNAL"),
]
CRITERIA = ("dynamism", "clarity", "fidelity", "anatomy", "overall")

JUDGE = """You judge illustrations for a reader's illustrated companion to a novel. You are shown two pictures, A (the first) and B (the second), drawn for the same moment of the book in the same drawing style from two different prompts. You do not know how either was made, and neither is the reference. Below the pictures comes the material the moment was drawn from: what happens, and the book's own paragraphs. Judge each picture against that material, then compare.

Score each picture from 1 to 5 on each of these:
- "dynamism": does the moment read as happening at this instant? For action (a blow, a fight, a chase, a fall, a struggle): 5 = bodies caught in the middle of the action, weight and balance committed, motion in the body, cloth, hair or surroundings, a camera angle and framing that sell it; 3 = the action is there but stiff, posed or half-hearted; 1 = figures stand or pose and nothing is happening. For a quiet moment: 5 = a living instant with the moment's feeling in the postures, gestures and gazes; 1 = a stiff tableau of figures standing about. Calm drawn as calm can earn 5.
- "clarity": can a viewer tell at a glance who does what to whom, and where? 5 = at once; 1 = unreadable.
- "fidelity": is this the moment the material tells: the people and creatures in it, what they do, with what, where, the light? Mark down missing or added figures, a wrong action, wrong objects, a wrong place. Never mark down a look (hair, dress, colour) the material does not state.
- "anatomy": are the bodies, hands, faces and the things they hold drawn right (the count of heads, arms, hands and legs, joints that bend the right way, a held thing gripped by a hand)? 5 = no fault; 3 = small faults on a second look; 1 = a gross fault at a glance.
- "overall": how good is it as the picture for this moment in the book?

Then "prefer": "A", "B" or "tie", the one you would put in the book, and "why": one short sentence.
Return {"A": {criteria}, "B": {criteria}, "prefer", "why"}."""
_SCORES = {"type": "object", "required": list(CRITERIA), "properties": {c: {"type": "integer"} for c in CRITERIA}}
JUDGE_SCHEMA = {"type": "object", "required": ["A", "B", "prefer", "why"], "properties": {
    "A": _SCORES, "B": _SCORES, "prefer": {"type": "string", "enum": ["A", "B", "tie"]}, "why": {"type": "string"}}}
JUDGE_PROMPT = """The two pictures are above: A first, then B.

<moment>{moment}</moment>
<material>{material}</material>

Score both pictures and say which you prefer."""
MATERIAL_CHARS = 5000


class Mem:
    """An in-memory art set: the bench never writes a set anywhere."""

    def __init__(self, m):
        self.m, self.files = m, {}

    def manifest(self):
        return self.m

    def save(self, manifest):
        self.m = json.loads(json.dumps(manifest))

    def put(self, file, data):
        self.files[file] = data

    def read(self, file):
        return self.files.get(file)


def text_model(spec: str, tier: str = "cheap"):
    provider, _, model = spec.partition(":")
    return llm.make(provider, model or llm.PROVIDERS[provider][tier], api_key=llm.env_key(provider))


class Spend:
    """What this run spends, per arm: images, the lookup's grounding calls, and every text model made for the arm (its
    writer, the choosing calls, the drawing-fault checks and the judge's verdicts on it)."""

    def __init__(self, cap: float):
        self.cap, self.lock, self.models = cap, threading.Lock(), []
        self.by_arm: dict[str, dict[str, float]] = {}

    def _arm(self, arm: str) -> dict:
        return self.by_arm.setdefault(arm, {"images": 0.0, "lookup": 0.0})

    def model(self, m, arm: str = "", what: str = "text"):
        with self.lock:
            self.models.append((arm, what, m))
        return m

    def add(self, arm: str, what: str, usd: float) -> None:
        with self.lock:
            self._arm(arm)[what] += usd

    @property
    def images(self) -> float:
        return sum(v["images"] for v in self.by_arm.values())

    def text(self) -> float:
        return sum(getattr(getattr(m, "usage", None), "cost_usd", 0.0) for _, _, m in self.models)

    def total(self) -> float:
        return self.images + self.text() + sum(v["lookup"] for v in self.by_arm.values())

    def report(self) -> dict:
        out = {}
        for arm in sorted({a for a, _, _ in self.models} | set(self.by_arm)):
            row = {k: round(v, 4) for k, v in self._arm(arm).items()}
            for _, what, m in [x for x in self.models if x[0] == arm]:
                row[what] = round(row.get(what, 0.0) + getattr(getattr(m, "usage", None), "cost_usd", 0.0), 4)
            row["total"] = round(sum(row.values()), 4)
            out[arm] = row
        return out

    def room(self, price: float) -> bool:
        return self.total() + price <= self.cap


def load_books() -> dict:
    out = {}
    for key, (ix, ep) in BOOKS.items():
        if not ix.is_file() or not ep.is_file():
            print(f"missing {ix if not ix.is_file() else ep}: its moments are skipped", file=sys.stderr)
            continue
        out[key] = (json.loads(ix.read_text()), epub.read(str(ep)))
    return out


def illustrator(doc, bk, writer, picker, image, style, quality):
    sink = Mem(art.blank(doc["editions"][0]["textHash"], style))
    return art.Illustrator([art.Book(doc, bk, sink, None)], writer, image, style, portraits=None, quality=quality,
                           workers=1, cheap_text=picker, log=lambda *_: None)


def write_side(name: str, books: dict, moments, a, spend: Spend, img: Path, log) -> dict:
    """Prompts, pictures and checks of one candidate for every moment: {event id: row}."""
    rows: dict[str, dict] = {}
    prov, _, mid = a.image.partition(":")
    image = llm.make_image(prov, mid or None)
    quality = llm.nearest_quality(prov, image.name, a.quality)
    price = llm.image_price(prov, image.name, quality, "landscape") or 0.05
    with cands.patched(name):
        def one(m) -> None:
            book, eid, group, what = m
            doc, bk = books[book]
            writer = spend.model(text_model(a.writer, "strong"), name, "writer")
            picker = spend.model(text_model(a.picker), name, "writer")
            il = illustrator(doc, bk, writer, picker, image, a.style, quality)
            row = {"book": book, "event": eid, "group": group, "what": what, "draws": []}
            rows[eid] = row
            if not spend.room(0.05 + price * a.draws):
                row["skipped"] = "cap"
                return
            look = None
            try:
                job = il.job("event", eid)
                if name in cands.LOOKUP:
                    look = moment_lookup(il, doc, eid, spend.model(text_model(a.writer, "strong"), name, "lookup"))
                    spend.add(name, "lookup", look["cost"])
                    row["lookup"] = look
                extra = f"\n\n<real_world>{art._esc(look['block'])}</real_world>" if look and look["block"] else ""
                pr = il._make_prompt(job, None, extra) if extra else il.make_prompt(job)
            except Exception as e:     # noqa: BLE001 - a bench records every outcome
                row["skipped"] = f"no prompt: {type(e).__name__}: {e}"[:300]
                log(f"  {name} {eid}: {row['skipped']}")
                return
            ev = next(e for e in doc["events"] if e["id"] == eid)
            row.update({"anchor": pr.anchor, "summary": ev["summary"], "prompt": pr.text, "body": pr.body,
                        "words": len(pr.body.split()), "figures": pr.figures, "material": pr.check[:MATERIAL_CHARS],
                        "writer_cost": round(writer.usage.cost_usd, 5)})
            for k in range(0 if a.no_draw else a.draws):
                if not spend.room(price):
                    row["draws"].append({"skipped": "cap"})
                    continue
                text, softened = pr.text, False
                try:
                    try:
                        pic = image.draw(text, shape="landscape", quality=quality, references=[])
                    except images.ImageRefused as e:
                        soft = il.soften(job, text, e.reason)
                        text, softened = soft.text, True
                        pic = image.draw(text, shape="landscape", quality=quality, references=[])
                except Exception as e:     # noqa: BLE001
                    row["draws"].append({"error": f"{type(e).__name__}: {e}"[:300]})
                    log(f"  {name} {eid} draw {k}: {row['draws'][-1]['error']}")
                    continue
                spend.add(name, "images", pic.cost if pic.cost is not None else price)
                f = img / f"{name}-{eid.rsplit('.', 2)[0][:4]}-{eid.rsplit('.', 1)[1]}-{k}-{hashlib.sha256(pic.data).hexdigest()[:6]}.{art._ext(pic.mime)}"
                f.write_bytes(pic.data)
                c = (art.Check([], False, 0.0, "not run (--no-check)") if a.no_check else
                     art.check_picture(spend.model(text_model(a.checker, "strong"), name, "checks"), pic.data, pic.mime, text))
                row["draws"].append({"file": f.name, "mime": pic.mime, "cost": pic.cost, "softened": softened,
                                     **({"prompt": text} if softened else {}),
                                     "check": {"checked": c.checked, "faults": c.faults, "why": c.why}})
                log(f"  {name} {eid} draw {k}: {pr.anchor}, {row['words']} words, "
                    f"{len(c.faults)} faults{' (softened)' if softened else ''}")

        with ThreadPoolExecutor(a.workers) as pool:
            list(pool.map(one, moments))
    return rows


def comic_side(name: str, books: dict, a, spend: Spend, img: Path, log) -> dict:
    """One comic page per COMICS chapter with candidate `name`'s writers, drawn once, portrait, and checked."""
    prov, _, mid = a.image.partition(":")
    image = llm.make_image(prov, mid or None)
    quality = llm.nearest_quality(prov, image.name, a.quality)
    rows = {}
    with cands.patched(name):
        def one(c) -> None:
            book, title = c
            doc, bk = books[book]
            writer = spend.model(text_model(a.writer, "strong"), name, "writer")
            il = illustrator(doc, bk, writer, spend.model(text_model(a.picker), name, "writer"), image, a.style, quality)
            key = f"comic:{title}"
            row = rows[key] = {"book": book, "event": key, "group": "comic", "what": title, "draws": []}
            try:
                job = il.job("comic", title)
                pr = il.make_prompt(job)
            except Exception as e:     # noqa: BLE001
                row["skipped"] = f"no prompt: {type(e).__name__}: {e}"[:300]
                return
            evs = {x["id"]: x for x in doc["events"]}
            summary = " ".join(f"Panel {k}: {evs[x]['summary']}" for k, x in enumerate(pr.panels or [], 1))
            row.update({"summary": "A comic page, one panel per moment. " + summary, "prompt": pr.text, "body": pr.body,
                        "words": len(pr.body.split()), "material": pr.check[:MATERIAL_CHARS]})
            try:
                pic = image.draw(pr.text, shape="portrait", quality=quality, references=[])
            except Exception as e:     # noqa: BLE001
                row["draws"].append({"error": f"{type(e).__name__}: {e}"[:300]})
                return
            spend.add(name, "images", pic.cost or 0.0)
            f = img / f"{name}-comic-{book}-{hashlib.sha256(pic.data).hexdigest()[:6]}.{art._ext(pic.mime)}"
            f.write_bytes(pic.data)
            ck = art.check_picture(spend.model(text_model(a.checker, "strong"), name, "checks"), pic.data, pic.mime, pr.text)
            row["draws"].append({"file": f.name, "mime": pic.mime, "cost": pic.cost, "softened": False,
                                 "check": {"checked": ck.checked, "faults": ck.faults, "why": ck.why}})
            log(f"  {name} {key}: {row['words']} words, {len(ck.faults)} faults")

        with ThreadPoolExecutor(a.workers) as pool:
            list(pool.map(one, COMICS))
    return rows


def more_draws(name: str, side: dict, a, spend: Spend, img: Path, log) -> None:
    """Draw a reused arm's prompts again until each has `--draws` pictures (the prompt as first drawn, softened or not)."""
    prov, _, mid = a.image.partition(":")
    image = llm.make_image(prov, mid or None)
    quality = llm.nearest_quality(prov, image.name, a.quality)
    price = llm.image_price(prov, image.name, quality, "landscape") or 0.05

    def one(row: dict) -> None:
        first = next((d for d in row["draws"] if d.get("file")), None)
        if not first and row["draws"]:
            return
        first = first or {}
        text = first.get("prompt") or row["prompt"]
        while len(row["draws"]) < a.draws:
            k = len(row["draws"])
            if not spend.room(price):
                row["draws"].append({"skipped": "cap"})
                continue
            try:
                pic = image.draw(text, shape="landscape", quality=quality, references=[])
            except Exception as e:     # noqa: BLE001
                row["draws"].append({"error": f"{type(e).__name__}: {e}"[:300]})
                continue
            spend.add(name, "images", pic.cost if pic.cost is not None else price)
            eid = row["event"]
            f = img / f"{name}-{eid.rsplit('.', 2)[0][:4]}-{eid.rsplit('.', 1)[1]}-{k}-{hashlib.sha256(pic.data).hexdigest()[:6]}.{art._ext(pic.mime)}"
            f.write_bytes(pic.data)
            c = (art.Check([], False, 0.0, "not run (--no-check)") if a.no_check else
                 art.check_picture(spend.model(text_model(a.checker, "strong"), name, "checks"), pic.data, pic.mime, text))
            row["draws"].append({"file": f.name, "mime": pic.mime, "cost": pic.cost, "softened": first.get("softened", False),
                                 **({"prompt": text} if first.get("softened") else {}),
                                 "check": {"checked": c.checked, "faults": c.faults, "why": c.why}})
            log(f"  {name} {eid} draw {k}: {len(c.faults)} faults")

    with ThreadPoolExecutor(a.workers) as pool:
        list(pool.map(one, [r for r in side.values() if "prompt" in r]))


def moment_lookup(il, doc: dict, eid: str, writer) -> dict:
    """The real-world lookup for one moment, from its summary and the book's paragraphs that tell it."""
    b = il.books[0]
    ev = next(e for e in doc["events"] if e["id"] == eid)
    seqs = [b.seq_of[x] for x in ev.get("evidence", []) if x in b.seq_of]
    told, _ = il.passage(0, seqs, max(seqs)) if seqs else ("", [])
    material = f"<moment>{art._esc(ev['summary'])}</moment>\n<passage>{art._esc(told)}</passage>"
    return action_lookup.lookup(doc, material, writer, action_lookup.env_key())


def judge_pair(model_spec: str, spend: Spend, arm: str, row_a: dict, row_b: dict, da: dict, db: dict, img: Path, seed: str) -> dict:
    """One blind verdict on two pictures, in both orders; scores are returned per side ("base", "cand")."""
    pics = {"base": ((img / da["file"]).read_bytes(), da["mime"]), "cand": ((img / db["file"]).read_bytes(), db["mime"])}
    rng = random.Random(seed)
    first = rng.choice(["base", "cand"])
    orders = [(first, "cand" if first == "base" else "base")]
    orders.append(orders[0][::-1])
    out = {"orders": []}
    for left, right in orders:
        if not spend.room(0.03):
            out["orders"].append({"skipped": "cap"})
            continue
        m = spend.model(text_model(model_spec, "strong"), arm, "judge")
        prompt = JUDGE_PROMPT.format(moment=html.escape(row_a["summary"], quote=False),
                                     material=html.escape(row_a["material"], quote=False))
        try:
            v = m.complete(JUDGE, prompt, JUDGE_SCHEMA, images=[pics[left], pics[right]])
        except Exception as e:     # noqa: BLE001
            out["orders"].append({"error": f"{type(e).__name__}: {e}"[:200]})
            continue
        side = {"A": left, "B": right}
        scores = {side[k]: {c: v[k].get(c) for c in CRITERIA} for k in ("A", "B")}
        prefer = side.get(v.get("prefer"), "tie")
        out["orders"].append({"A": left, "scores": scores, "prefer": prefer, "why": str(v.get("why", ""))[:300],
                              "cost": round(m.usage.cost_usd, 5)})
    done = [o for o in out["orders"] if "scores" in o]
    if done:
        out["mean"] = {s: {c: statistics.mean(o["scores"][s][c] for o in done if isinstance(o["scores"][s][c], int))
                           for c in CRITERIA} for s in ("base", "cand")}
        prefs = [o["prefer"] for o in done]
        out["prefer"] = (prefs[0] if len(set(prefs)) == 1 else "tie") if len(prefs) == 2 else prefs[0]
    return out


def summarise(results: dict, names: list[str], pairs: list[tuple[str, str]], spend: Spend) -> dict:
    out = {"pairs": {}, "faults": {}}
    for name in names:
        pics = [(r["group"], d) for r in results["sides"][name].values() for d in r["draws"] if d.get("file")]
        out["faults"][name] = {}
        for g in ("action", "control", "comic", "all"):
            ds = [d for gg, d in pics if g in ("all", gg) and d["check"]["checked"]]
            out["faults"][name][g] = {
                "pictures": len(ds), "with_fault": sum(bool(d["check"]["faults"]) for d in ds),
                "with_major": sum(any(f.get("severity") == "major" for f in d["check"]["faults"]) for d in ds),
                "faults": sum(len(d["check"]["faults"]) for d in ds),
                "softened": sum(d["softened"] for gg, d in pics if g in ("all", gg))}
        words = [r["words"] for r in results["sides"][name].values() if "words" in r]
        out["faults"][name]["words"] = {"min": min(words), "median": statistics.median(words), "max": max(words)} if words else None
    for name, base in pairs:
        vs = results["verdicts"].get(f"{name}|{base}", {})
        c = {}
        for g in ("action", "control", "comic", "all"):
            got = [v for k, v in vs.items() if "mean" in v and (g == "all" or v["group"] == g)]
            per = {}
            for crit in CRITERIA:
                b = [v["mean"]["base"][crit] for v in got]
                n = [v["mean"]["cand"][crit] for v in got]
                per[crit] = {"base": round(statistics.mean(b), 2) if b else None, "cand": round(statistics.mean(n), 2) if n else None,
                             "diff": round(statistics.mean(n) - statistics.mean(b), 2) if b else None}
            prefs = [v["prefer"] for v in got]
            c[g] = {"pairs": len(got), **per, "prefer": {"cand": prefs.count("cand"), "base": prefs.count("base"),
                                                         "tie": prefs.count("tie")}}
        out["pairs"][f"{name}|{base}"] = c
    out["changes"] = {n: cands.describe(n) for n in names if n in cands.CANDIDATES and n != "old"}
    out["spent"] = {"images": round(spend.images, 4), "text": round(spend.text(), 4), "total": round(spend.total(), 4),
                    "by_arm": spend.report()}
    return out


def sheet(results: dict, summary: dict, names: list[str], pairs: list[tuple[str, str]]) -> str:
    e = lambda s: html.escape(str(s))
    head = (f"<h1>Action prompts: {e(', '.join(names))}</h1><p>image {e(results['config']['image'])} at "
            f"{e(results['config']['quality'])}, style {e(results['config']['style'])}, writer {e(results['config']['writer'])}, "
            f"judge {e(results['config']['judge'])}; this run spent ${summary['spent']['total']}.</p>"
            f"<pre>{e(json.dumps({k: summary[k] for k in ('pairs', 'faults', 'spent')}, indent=1))}</pre>")
    body = []
    for book, eid, group, what in MOMENTS + [(b_, f"comic:{t}", "comic", t) for b_, t in COMICS]:
        rows = {n: results["sides"][n].get(eid) for n in names}
        r0 = rows[names[0]]
        if not r0:
            continue
        cells = []
        for n in names:
            r = rows[n] or {}
            pics = "".join(
                f"<img src='img/{e(d['file'])}'><p class=f>" + (
                    "; ".join(f"[{e(f['severity'])}] {e(f['what'])}" for f in d["check"]["faults"]) or "no drawing faults found")
                + (" (softened after a refusal)" if d.get("softened") else "") + "</p>"
                if d.get("file") else f"<p>{e(d.get('error') or d.get('skipped'))}</p>" for d in r.get("draws", []))
            verdict = ""
            for cand, base in pairs:
                if cand != n:
                    continue
                for v in results["verdicts"].get(f"{cand}|{base}", {}).values():
                    if v["event"] == eid and "mean" in v:
                        verdict += (f"<p class=v>vs {e(base)}, draw {v['draw']}: prefer <b>{e({'cand': cand, 'base': base}.get(v['prefer'], 'tie'))}</b>; "
                                    + ", ".join(f"{c} {v['mean']['base'][c]:.1f}&rarr;{v['mean']['cand'][c]:.1f}" for c in CRITERIA)
                                    + "</p>" + "".join(f"<p class=w>{e(o.get('why', ''))}</p>" for o in v["orders"]))
            cells.append(f"<td><h3>{e(n)}</h3>{pics}{verdict}<details><summary>prompt ({r.get('words', '?')} words)</summary>"
                         f"<small>{e(r.get('prompt', r.get('skipped', '')))}</small></details></td>")
        if "lookup" in (rows.get(names[-1]) or {}):
            lk = rows[names[-1]]["lookup"]
            cells.append("<td class=m><b>lookup</b>" + "".join(
                f"<p><i>{e(q['query'])}</i>: " + e(q.get("refused") or q.get("dropped") or q.get("error") or q.get("text", ""))
                + "".join(f"<br><small><a href='{e(x['uri'])}'>{e(x['title'])}</a></small>" for x in q.get("sources", []))
                + "</p>" for q in lk.get("queries", [])) + "</td>")
        body.append(f"<tr><td class=m><b>{e(group)}</b>: {e(what)}<br><small>{e(eid)}</small><p>{e(r0.get('summary', ''))}</p></td>"
                    + "".join(cells) + "</tr>")
    return ("<!doctype html><meta charset=utf-8><title>action prompts</title><style>body{font:13px system-ui;background:#111;"
            "color:#ddd}td{vertical-align:top;padding:6px}td.m{width:14%}img{width:100%}small{color:#aaa}pre{font-size:11px}"
            ".f{color:#c96}.v{color:#9cf}.w{color:#999;font-size:12px}h3{margin:2px 0}</style>" + head
            + "<table>" + "".join(body) + "</table>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--candidates", default="old,c1", help="comma-separated names; the first is the baseline")
    ap.add_argument("--reuse", type=Path, action="append", default=[], help="an earlier DIR whose sides and verdicts to reuse")
    ap.add_argument("--writer", default="claude:claude-sonnet-5-5", help="PROVIDER[:MODEL]: the picture sheet's strong tier")
    ap.add_argument("--picker", default="claude", help="PROVIDER[:MODEL] of the choosing calls (its cheap tier)")
    ap.add_argument("--image", default="gemini:gemini-nano-banana-2.1")
    ap.add_argument("--quality", default="standard")
    ap.add_argument("--style", default="digital-art")
    ap.add_argument("--judge", default="claude:claude-sonnet-5-5")
    ap.add_argument("--checker", default="claude:claude-sonnet-5-5", help="the drawing-fault check (the lightbox's default)")
    ap.add_argument("--draws", type=int, default=1, help="pictures per prompt; a reused arm with fewer gets more")
    ap.add_argument("--pairs", default="", help="CAND:BASE,... to judge (default every candidate against the first)")
    ap.add_argument("--no-draw", action="store_true", help="write the prompts only")
    ap.add_argument("--restyle", type=Path, help="an earlier DIR whose prompts to draw again in --style (the writer never "
                    "sees the style, so the body stays and only the style sentence changes); its pictures are not reused")
    ap.add_argument("--style-check", action="store_true", help="the judge also scores how well each picture keeps the style")
    ap.add_argument("--no-check", action="store_true", help="skip the drawing-fault check on new pictures")
    ap.add_argument("--comics", action="store_true", help="draw comic pages of COMICS instead of the scene moments")
    ap.add_argument("--only", default="", help="comma-separated event ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cap", type=float, default=3.0)
    a = ap.parse_args()
    names = [n for n in a.candidates.split(",") if n]
    for n in names:
        if n not in cands.CANDIDATES and not (a.reuse or a.restyle):
            ap.error(f"unknown candidate {n} (an arm not on this art.py can only be reused from an earlier DIR)")
    out, img = a.out, a.out / "img"
    img.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lock = threading.Lock()

    def log(m):
        with lock:
            lines.append(str(m))
            print(m, flush=True)

    global CRITERIA, JUDGE, JUDGE_SCHEMA
    if a.style_check:
        sentence = art.style_sentence(a.style)
        CRITERIA = (*CRITERIA[:-1], "style", "overall")
        JUDGE = JUDGE.replace('- "overall":', '- "style": does the picture look like the drawing style asked for: "' + sentence
                              + '"? 5 = plainly that medium and look throughout; 3 = partly (a photographic or painted look '
                              'creeping in); 1 = another style altogether.\n- "overall":')
        scores = {"type": "object", "required": list(CRITERIA), "properties": {c: {"type": "integer"} for c in CRITERIA}}
        JUDGE_SCHEMA = {**JUDGE_SCHEMA, "properties": {**JUDGE_SCHEMA["properties"], "A": scores, "B": scores}}
    books = load_books()
    only = {x for x in a.only.split(",") if x}
    moments = [m for m in MOMENTS if m[0] in books and (not only or m[1] in only)]
    spend = Spend(a.cap)
    config = {k: getattr(a, k) for k in ("writer", "picker", "image", "quality", "style", "judge", "checker", "draws")}
    results = {"config": config, "sides": {}, "verdicts": {}}
    old = {}
    for d in a.reuse:
        prev = json.loads((d / "results.json").read_text())
        if {k: prev["config"].get(k) for k in ("image", "quality", "style", "writer")} != \
                {k: config[k] for k in ("image", "quality", "style", "writer")}:
            print(f"{d}: drawn with other settings, not reused", file=sys.stderr)
            continue
        for n, side in prev["sides"].items():
            if n in names and n not in old:
                old[n] = (d, side)
        for n, vs in prev.get("verdicts", {}).items():
            results["verdicts"].setdefault(n if "|" in n else f"{n}|old", {}).update(vs)
    if a.restyle:
        prev = json.loads((a.restyle / "results.json").read_text())
        sentence = art.style_sentence(a.style)
        for n in names:
            side = {eid: {**{k: v for k, v in r.items() if k != "draws"}, "draws": [],
                          "prompt": " ".join(x for x in (r["body"], sentence, art.NO_TEXT) if x)}
                    for eid, r in prev["sides"].get(n, {}).items() if r.get("body") and any(m[1] == eid for m in moments)}
            if side:
                log(f"{n}: prompts from {a.restyle}, drawn again in {a.style}")
                results["sides"][n] = side
                more_draws(n, side, a, spend, img, log)
                old[n] = None
    for n in names:
        if n in old and old[n] is None:
            continue
        if n in old:
            d, side = old[n]
            for r in side.values():
                for x in r["draws"]:
                    if x.get("file") and not (img / x["file"]).exists():
                        shutil.copyfile(d / "img" / x["file"], img / x["file"])
            results["sides"][n] = {eid: r for eid, r in side.items() if any(m[1] == eid for m in moments)}
            log(f"{n}: reused from {d}")
            more_draws(n, results["sides"][n], a, spend, img, log)
            continue
        if a.comics:
            log(f"{n}: writing and drawing {len(COMICS)} comic pages")
            results["sides"][n] = comic_side(n, books, a, spend, img, log)
        else:
            log(f"{n}: writing and drawing {len(moments)} moments")
            results["sides"][n] = write_side(n, books, moments, a, spend, img, log)
        (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    pairs = [tuple(x.split(":")) for x in a.pairs.split(",") if x] or [(n, names[0]) for n in names[1:]]
    jobs = []
    for n, base in pairs:
        vs = results["verdicts"].setdefault(f"{n}|{base}", {})
        for book, eid, group, what in ([(b_, f"comic:{t}", "comic", t) for b_, t in COMICS] if a.comics else moments):
            ra, rb = results["sides"][base].get(eid), results["sides"][n].get(eid)
            if not ra or not rb:
                continue
            for k, (da, db) in enumerate(zip(ra["draws"], rb["draws"])):
                key = f"{eid}#{k}:{da.get('file')}:{db.get('file')}"
                if da.get("file") and db.get("file") and "mean" not in vs.get(key, {}):
                    jobs.append((n, base, key, eid, group, k, ra, rb, da, db))

    def judge_one(j):
        n, base, key, eid, group, k, ra, rb, da, db = j
        v = judge_pair(a.judge, spend, n, ra, rb, da, db, img, key)
        v.update({"event": eid, "group": group, "draw": k})
        results["verdicts"][f"{n}|{base}"][key] = v
        log(f"  judge {n} vs {base} {eid}#{k}: prefer {v.get('prefer')}")

    with ThreadPoolExecutor(a.workers) as pool:
        list(pool.map(judge_one, jobs))
    summary = summarise(results, names, pairs, spend)
    results["summary"] = summary
    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    (out / "index.html").write_text(sheet(results, summary, names, pairs))
    (out / "log.txt").write_text("\n".join(lines) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
