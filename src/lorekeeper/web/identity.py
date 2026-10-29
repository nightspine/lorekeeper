"""Duplicate entities in generated indexes: find them, merge them, undo a merge, remember "not the same".

Entity ids are series-scoped, so a merge rewrites every generated index of the series (all versions
of all books), and a pair is looked for across all of them. Only generated indexes (our own files)
are ever changed. Before a merge the affected files are copied to `merges/<id>/`, which is what undo
restores.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from .. import epub, merge, validate

# Pairs merged without asking: the names alone tell a reader they are one (the same name, or a
# fuller form of it), and a model reading both entries and the book's text agrees. A looser link is
# merged only when it runs through a proper name (`auto_ok`): "Aaron" or "Van Helsing" is someone,
# while a shared descriptor such as "the stranger" is how a hidden identity looks, so that stays
# for review.
AUTO_REASONS = ("same name", "fuller name")


def auto_ok(reason: str, names_a: list[str], names_b: list[str]) -> bool:
    """May a pair judged "same" be merged without asking, given why it was suggested?"""
    if reason in AUTO_REASONS:
        return True
    if reason == "shared alias":
        shared = {merge.core(n) for n in names_a} & {merge.core(n) for n in names_b}
        return any(merge.proper_name(n) for n in [*names_a, *names_b] if merge.core(n) in shared)
    if reason == "described alike":   # proposed from descriptions: only if nothing about either is a descriptor
        return all(merge.proper_name(n) for n in [*names_a, *names_b])
    return False


class Identity:
    def __init__(self, data_dir: Path, library, catalog, jobs, checker=lambda: None, judge_checker=None) -> None:
        self.dir = data_dir / "merges"
        self.library, self.catalog, self.jobs = library, catalog, jobs
        self.checker = checker            # () → a model for proposing pairs (reads the whole cast), or None
        self.judge_checker = judge_checker or checker   # () → the model that judges pairs (few, short, so a strong one)
        self._lock = threading.Lock()
        self._checking: dict[str, str] = {}   # group id → model name while a check runs
        self._check_error: dict[str, str] = {}
        self._tried: set[str] = set()          # groups checked automatically this session

    # ---- which files ---------------------------------------------------------------------------

    def group(self, key: str) -> tuple[str, list[dict]]:
        """(group id, items) for an index key: every index of its series, or just itself.
        Items are in series reading order so the earliest introduction wins."""
        found = self.library.scan()
        item = found.get(key)
        if item is None:
            raise KeyError(key)
        sid = (item["doc"].get("series") or {}).get("id")
        if not sid:
            return key, [item]
        order = []
        for it in found.values():
            if it["doc"].get("kind") == "series" and it["doc"].get("id") == sid:
                order = [b["key"] for b in it["doc"].get("books", [])]
                break
        members = [it for it in found.values() if it["doc"].get("kind") == "index"
                   and (it["doc"].get("series") or {}).get("id") == sid]
        rank = lambda it: order.index(it["doc"]["series"]["book"]) if it["doc"]["series"]["book"] in order else len(order)  # noqa: E731
        return f"series:{sid}", sorted(members, key=lambda it: (rank(it), it["key"]))

    def _busy(self, items: list[dict]) -> bool:
        paths = {str(it["path"]) for it in items}
        return any(j.status in ("queued", "running", "stopping") and j.output in paths for j in self.jobs.jobs.values())

    # ---- queries -------------------------------------------------------------------------------

    def distinct(self, gid: str) -> set[frozenset]:
        return {frozenset(p) for p in self.catalog.state().get("distinct", {}).get(gid, [])}

    def verdicts(self, gid: str) -> dict[str, str]:
        st = self.catalog.state()
        if st.get("judgeVersion") != merge.JUDGE_VERSION:   # judged with an older prompt: check again
            return {}
        return st.get("verdicts", {}).get(gid, {})

    def _book_loader(self):
        """A function from an index document to its parsed EPUB (None when it isn't on disk), caching
        each book for the lifetime of the function."""
        cache: dict[str, object] = {}

        def load(doc: dict):
            for ed in doc.get("editions", []):
                th = ed.get("textHash")
                if not th or not ed.get("builtFrom", True):
                    continue
                if th not in cache:
                    path = self.library.edition_path(th)
                    try:
                        cache[th] = epub.read(path) if path else None
                    except Exception:   # noqa: BLE001 - judge without the text rather than not at all
                        cache[th] = None
                if cache[th] is not None:
                    return cache[th]
            return None
        return load

    def _has_book(self, docs: list[dict]) -> bool:
        """Is the EPUB of any of these indexes on disk? (A cheap lookup: nothing is parsed.)"""
        return any(self.library.edition_path(ed.get("textHash", "")) for d in docs for ed in d.get("editions", [])
                   if ed.get("builtFrom", True))

    def _texted(self, gid: str) -> dict[str, bool]:
        """Pairs whose verdict was reached with the book's own text in front of the model."""
        return self.catalog.state().get("texted", {}).get(gid, {}) if self.catalog.state().get("judgeVersion") == merge.JUDGE_VERSION else {}

    @staticmethod
    def _cast_sig(docs: list[dict]) -> str:
        """Which people and creatures exist: proposals are redone when this changes."""
        ids = sorted({e["id"] for d in docs for e in d.get("entities", []) if e["type"] in ("person", "creature")})
        return hashlib.sha256("|".join(ids).encode()).hexdigest()[:16]

    def _proposals(self, gid: str, docs: list[dict]) -> list[dict] | None:
        """Model-proposed pairs (merge.propose) for this cast, or None when they need redoing."""
        p = self.catalog.state().get("proposals", {}).get(gid)
        return p["pairs"] if p and p.get("sig") == self._cast_sig(docs) else None

    def duplicates(self, key: str) -> dict:
        gid, items = self.group(key)
        writable = all(it["source"] == "generated" for it in items)
        docs = [it["doc"] for it in items]
        pairs = merge.candidates(docs, self.distinct(gid))
        # Pairs a model found from descriptions ("Mr. Dursley" / "Uncle Vernon"): judged like the rest,
        # and merged automatically only when every name involved is a proper name (see `auto_ok`).
        have = {frozenset((p["keep"], p["drop"])) for p in pairs} | self.distinct(gid)
        alive = {e["id"]: e for d in docs for e in d.get("entities", [])}
        proposed = self._proposals(gid, docs)
        for q in proposed or []:
            if frozenset((q["keep"], q["drop"])) not in have and q["keep"] in alive and q["drop"] in alive:
                pairs.append({**q, "sure": False, "reason": "described alike",
                              "keepNames": [n["value"] for n in alive[q["keep"]]["names"]],
                              "dropNames": [n["value"] for n in alive[q["drop"]]["names"]], "ranked": []})
        verdicts = self.verdicts(gid)
        for p in pairs:
            v = verdicts.get(f"{p['keep']}|{p['drop']}")
            p["verdict"] = v
            if v:   # a model's reading of both entries beats name rules alone
                p["sure"] = v == "same"
        pairs = [p for p in pairs if p["verdict"] != "different"]   # judged two things: nothing to ask
        model = self.checker()
        unjudged = sum(p["verdict"] is None for p in pairs)
        texted = self._texted(gid)
        retry = any(p["verdict"] == "unsure" and not texted.get(f"{p['keep']}|{p['drop']}") for p in pairs) \
            and model is not None and self._has_book(docs)   # first pass ran without the text
        if (unjudged or retry or proposed is None) and model is not None and gid not in self._checking and gid not in self._tried and writable:
            # Check new pairs in the background (once per session), then merge the obvious ones.
            self._tried.add(gid)
            self.check(key)
        elif writable and gid not in self._checking and gid not in self._tried:
            # Nothing to judge, but earlier verdicts may now qualify (the policy changed): merge those once.
            self._tried.add(gid)
            try:
                if self.auto_merge(key).get("merged"):
                    return self.duplicates(key)
            except (ValueError, RuntimeError) as e:   # a merge that would break a file is simply not done
                self._check_error[gid] = str(e)
        last = self._journal(gid)[-1:] if self._journal(gid) else []
        return {"pairs": pairs, "writable": writable, "busy": self._busy(items),
                "checking": self._checking.get(gid), "checkError": self._check_error.get(gid),
                "checker": getattr(model, "name", None), "unjudged": unjudged,
                "undo": ({"id": last[0]["id"], "keepNames": last[0]["keepNames"], "dropNames": last[0]["dropNames"],
                          "auto": bool(last[0].get("auto")), "at": last[0]["at"]} if last else None)}

    def check(self, key: str) -> dict:
        """Judge every unjudged pair with a model, in the background. Verdicts are cached per group.
        Pairs proposed from descriptions are judged like the rest, and a pair left "unsure" without the
        book's text is judged once more when the EPUB is on disk."""
        gid, items = self.group(key)
        if gid in self._checking:
            return {"checking": self._checking[gid]}
        model = self.checker()
        if model is None:
            raise LookupError("no model available: add a Claude key in Settings or start Ollama")
        judge_model = self.judge_checker() or model
        docs = [it["doc"] for it in items]
        books = self._book_loader()
        have_books = self._has_book(docs)
        done, texted = self.verdicts(gid), self._texted(gid)
        alive = {e["id"] for d in docs for e in d.get("entities", [])}

        def due(k: str) -> bool:
            return k not in done or (done[k] == "unsure" and not texted.get(k) and have_books)
        candidates = merge.candidates(docs, self.distinct(gid))
        stored = [q for q in self._proposals(gid, docs) or [] if q["keep"] in alive and q["drop"] in alive]
        stale = self._proposals(gid, docs) is None
        if not stale and not any(due(f"{p['keep']}|{p['drop']}") for p in [*candidates, *stored]):
            try:
                self.auto_merge(key)
            except (ValueError, RuntimeError) as e:
                self._check_error[gid] = str(e)
            return {"checking": None}
        self._checking[gid] = judge_model.name
        self._check_error.pop(gid, None)

        def work():
            try:
                known = {frozenset((p["keep"], p["drop"])) for p in candidates} | self.distinct(gid)
                proposed = [{"keep": p["keep"], "drop": p["drop"]} for p in merge.propose(docs, model, known)] if stale else None
                todo = [p for p in [*candidates, *(proposed if proposed is not None else stored)]
                        if due(f"{p['keep']}|{p['drop']}")]
                meta: dict = {}
                found = merge.judge(docs, todo, judge_model, books=books, meta=meta) if todo else {}

                def save(s):
                    if s.get("judgeVersion") != merge.JUDGE_VERSION:
                        s["verdicts"], s["because"], s["texted"], s["hidden"], s["judgeVersion"] = {}, {}, {}, {}, merge.JUDGE_VERSION
                    s.setdefault("verdicts", {}).setdefault(gid, {}).update(found)
                    s.setdefault("because", {}).setdefault(gid, {}).update({k: m["because"] for k, m in meta.items()})
                    s.setdefault("texted", {}).setdefault(gid, {}).update({k: True for k, m in meta.items() if m["text"]})
                    s.setdefault("hidden", {}).setdefault(gid, {}).update({k: m["hidden"] for k, m in meta.items()})
                    if proposed is not None:
                        s.setdefault("proposals", {})[gid] = {"sig": self._cast_sig(docs), "pairs": proposed}
                self.catalog.update_state(save)
                self.auto_merge(key)
            except Exception as e:   # surfaced to the UI; pairs stay as name rules left them
                self._check_error[gid] = str(e)
            finally:
                self._checking.pop(gid, None)
        threading.Thread(target=work, daemon=True).start()
        return {"checking": judge_model.name}

    # ---- changes -------------------------------------------------------------------------------

    def mark_distinct(self, key: str, a: str, b: str) -> None:
        gid, _ = self.group(key)

        def add(s):
            lst = s.setdefault("distinct", {}).setdefault(gid, [])
            if sorted((a, b)) not in lst:
                lst.append(sorted((a, b)))
        self.catalog.update_state(add)

    def auto_merge(self, key: str) -> dict:
        """Merge every pair that is certain: judged "same" and linked by names a reader would take for
        one person (`auto_ok`). Each merge records the model's one-line reason in the journal."""
        gid, items = self.group(key)
        if not all(it["source"] == "generated" for it in items) or self._busy(items):
            return {"merged": 0}
        docs = [it["doc"] for it in items]
        verdicts = self.verdicts(gid)
        because = self.catalog.state().get("because", {}).get(gid, {})
        hidden = self.catalog.state().get("hidden", {}).get(gid, {})   # the book conceals the link at first
        cands = merge.candidates(docs, self.distinct(gid))
        ents = merge._entities(docs)
        alive = set(ents)
        seen = {frozenset((p["keep"], p["drop"])) for p in cands}
        pool = [(p, p["reason"]) for p in cands]
        for q in self._proposals(gid, docs) or []:
            if frozenset((q["keep"], q["drop"])) not in seen and q["keep"] in alive and q["drop"] in alive \
                    and frozenset((q["keep"], q["drop"])) not in self.distinct(gid):
                pool.append(({"keep": q["keep"], "drop": q["drop"], "sure": False}, "described alike"))
        # Entries linked by pairs judged the same form a cluster. A cluster is merged only if no pair
        # among its members was judged otherwise, and any two members that no suggestion links don't
        # carry different full names ("Chloe" fits Chloe Sauvage and Chloe Brun: those stay for a person).
        good: dict[tuple[str, str], str] = {}
        edges: set[frozenset] = set()
        stars: dict[str, list[str]] = {}
        for p, rsn in pool:
            k = f"{p['keep']}|{p['drop']}"
            edges.add(frozenset((p["keep"], p["drop"])))
            if p["sure"] and rsn != "described alike":   # a group that all match one another is folded into one entry
                stars.setdefault(p["keep"], []).append(p["drop"])
            if verdicts.get(k) == "same" and not hidden.get(k) \
                    and auto_ok(rsn, ents[p["keep"]]["names"], ents[p["drop"]]["names"]):
                good[(p["keep"], p["drop"])] = k
        for keep, drops in stars.items():
            edges |= {frozenset((x, y)) for x in drops for y in drops if x != y}

        def conflict(x: str, y: str) -> bool:
            """Both have a full (multi-word) name and no two of those names are forms of one another."""
            nx = [n for n in ents[x]["names"] if len(merge.core(n)) > 1]
            ny = [n for n in ents[y]["names"] if len(merge.core(n)) > 1]
            return bool(nx and ny) and not any(merge.extends(a, b, loose=True) for a in nx for b in ny)
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for keep, drop in good:
            parent[find(keep)] = find(drop)
        members: dict[str, set[str]] = {}
        for keep, drop in good:
            members.setdefault(find(keep), set()).update((keep, drop))
        pairs, why = [], {}
        for root, group in members.items():
            names = sorted(group)
            clique = all(frozenset((x, y)) in edges or not conflict(x, y) for i, x in enumerate(names) for y in names[i + 1:])
            clean = all(verdicts.get(f"{k}|{d}") == "same" and not hidden.get(f"{k}|{d}")
                        for k, d in ((p["keep"], p["drop"]) for p, _ in pool) if k in group and d in group)
            if not (clique and clean):
                continue
            for (keep, drop), k in good.items():
                if keep in group:
                    pairs.append((keep, drop, None))
                    if because.get(k):
                        why[k] = because[k]
        return self.merge(key, pairs, auto=True, because=why) if pairs else {"merged": 0}

    def merge(self, key: str, pairs: list[tuple[str, str, str | None]], auto: bool = False,
              because: dict[str, str] | None = None) -> dict:
        """Apply merges (keep, drop, display name or None) in order to every generated index of the
        group, as one undoable step."""
        with self._lock:
            gid, items, docs = self._writable(key)
            ents = {e["id"]: [n["value"] for n in e.get("names", [])] for d in docs for e in d.get("entities", [])}
            counts = merge.word_counts(docs)
            proposals = self._proposals(gid, docs)   # still valid for this cast: carried over to the merged one
            changed: set[int] = set()
            done = []
            into: dict[str, str] = {}   # merged-away id → where it went, so later pairs follow the chain

            def live(i: str) -> str:
                while i in into:
                    i = into[i]
                return i
            for keep, drop, name in pairs:
                keep, drop = live(keep), live(drop)
                if keep == drop or keep not in ents or drop not in ents:
                    continue
                changed |= set(merge.merge(docs, keep, drop, name, counts))
                into[drop] = keep
                done.append((keep, drop))
            if not changed:
                return {"merged": 0, "files": 0}
            entry = self._commit(gid, items, docs, changed, {
                "keepNames": [ents[k][0] if ents[k] else k for k, _ in done],
                "dropNames": [ents[d][0] if ents[d] else d for _, d in done], "auto": auto,
                **({"because": {k: v for k, v in because.items() if tuple(k.split("|")) in set(done)}} if because else {})})
            for i in sorted(changed):
                self._fix_state(items[i]["path"], done)
            if proposals is not None:
                moved, seen = [], set()
                for q in proposals:
                    k, d = live(q["keep"]), live(q["drop"])
                    if k != d and frozenset((k, d)) not in seen:
                        seen.add(frozenset((k, d)))
                        moved.append({"keep": k, "drop": d})
                sig = self._cast_sig(docs)
                self.catalog.update_state(lambda st: st.setdefault("proposals", {}).__setitem__(gid, {"sig": sig, "pairs": moved}))
            return {"merged": len(done), "files": len(changed), "undo": entry["id"]}

    def tidy_names(self, key: str) -> dict:
        """Give every entity with more than one name its best display name (merge.rank_names): the
        obvious name a reader knows them by, not whichever was marked primary last."""
        with self._lock:
            gid, items, docs = self._writable(key)
            counts, surnames = merge.word_counts(docs), merge.family_names(docs)
            names: dict[str, list[tuple[str, str]]] = {}
            for d in docs:
                for e in d.get("entities", []):
                    names.setdefault(e["id"], []).extend((n["value"], n.get("kind", "alias")) for n in e.get("names", []))
            fixed = []
            for eid, ns in names.items():
                if len({v.casefold() for v, _ in ns}) < 2:
                    continue
                best = merge.rank_names([v for v, _ in ns], counts, surnames)[0]
                if {v.casefold() for v, k in ns if k == "primary"} != {best.casefold()}:
                    merge.canonicalize(docs, eid, best, counts, surnames)
                    fixed.append(best)
            if not fixed:
                return {"fixed": 0}
            entry = self._commit(gid, items, docs, set(range(len(items))), {"keepNames": fixed, "dropNames": []})
            return {"fixed": len(fixed), "names": fixed, "undo": entry["id"]}

    def _writable(self, key: str) -> tuple[str, list[dict], list[dict]]:
        gid, items = self.group(key)
        if not all(it["source"] == "generated" for it in items):
            raise PermissionError("only indexes built here can be changed")
        if self._busy(items):
            raise RuntimeError("a build is writing one of these indexes; try again once it has finished")
        return gid, items, [it["doc"] for it in items]

    def _commit(self, gid: str, items: list[dict], docs: list[dict], changed: set[int], meta: dict) -> dict:
        """Validate, snapshot the files for undo, then write. Nothing is written if a file would break."""
        for i in changed:
            before = {str(e) for e in validate.check_index(json.loads(items[i]["path"].read_text())) if e.level == "error"}
            errs = [e for e in validate.check_index(docs[i]) if e.level == "error" and str(e) not in before]
            if errs:   # only what this change itself would break
                raise ValueError(f"this would make {items[i]['path'].name} invalid: {errs[0]}")
        entry = {"id": uuid.uuid4().hex[:12], "group": gid, "at": time.time(), **meta,
                 "files": [str(items[i]["path"]) for i in sorted(changed)]}
        snap = self.dir / entry["id"]
        snap.mkdir(parents=True, exist_ok=True)
        for i in sorted(changed):
            p = items[i]["path"]
            shutil.copy2(p, snap / p.name)
            state = p.with_name(p.name + ".state")
            if state.exists():
                shutil.copy2(state, snap / state.name)
        (snap / "entry.json").write_text(json.dumps(entry, indent=2))
        for i in sorted(changed):
            p = items[i]["path"]
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(docs[i], indent=2, ensure_ascii=False) + "\n")
            tmp.replace(p)
        return entry

    def undo(self, key: str, merge_id: str) -> dict:
        with self._lock:
            gid, items = self.group(key)
            journal = self._journal(gid)
            if not journal or journal[-1]["id"] != merge_id:
                raise LookupError("only the latest merge can be undone")
            if self._busy(items):
                raise RuntimeError("a build is writing one of these indexes; undo once it has finished")
            snap = self.dir / merge_id
            for f in journal[-1]["files"]:
                p = Path(f)
                shutil.copy2(snap / p.name, p)
                state = snap / (p.name + ".state")
                if state.exists():
                    shutil.copy2(state, p.with_name(p.name + ".state"))
            shutil.rmtree(snap)
            return {"undone": merge_id}

    def _journal(self, gid: str) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for d in self.dir.iterdir():
            try:
                e = json.loads((d / "entry.json").read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if e.get("group") == gid:
                out.append(e)
        return sorted(out, key=lambda e: e["at"])

    @staticmethod
    def _fix_state(path: Path, done: list[tuple[str, str]]) -> None:
        """Keep a stopped run's resume state consistent with the merged ids."""
        sp = path.with_name(path.name + ".state")
        if not sp.exists():
            return
        try:
            st = json.loads(sp.read_text())
        except (OSError, json.JSONDecodeError):
            return
        ef = st.get("entity_from", {})
        for keep, drop in done:
            if drop in ef:
                ef[keep] = min(ef.get(keep, ef[drop]), ef.pop(drop))
        sp.write_text(json.dumps(st, indent=2))
