"""The real-world lookup arm of `bench/action_prompts.py`: a step in writing a scene prompt, never the image model searching.

1. The writer (the same text model that writes the prompt) reads the moment's material and lists the real-world things in
   it (a weapon, armour, period dress, a building, a vehicle, a way of fighting or moving) as at most `MAX_QUERIES` web
   search queries, with no names.
2. Code refuses any query holding the book's title, an author, or a name (or a coined word of a name) from the index.
3. Each query that passes runs on Gemini with Google Search grounding (`GROUNDER`), which answers in plain sentences on how
   the thing looks and is used, with its sources.
4. Code drops an answer that mentions the book, its author, a name from the index, a film, a series, an actor, an
   adaptation or an illustrator.
5. What is left goes into the writer's material as `<real_world>` (the c3L writer says what the tag is), and the writer
   writes the prompt as usual.

Every query, its source URLs and the sentences added are returned for the bench's results. General rules only.
"""

from __future__ import annotations

import json
import re
import urllib.request

from lorekeeper import llm
from lorekeeper.common_words import COMMON

MAX_QUERIES = 3
GROUNDER = "gemini-3-flash-preview"      # a Gemini 3 text model with Google Search grounding (documented for it)
GROUNDER_PRICE = (0.50, 3.00)            # USD per million tokens in and out (ai.google.dev pricing, 2026-11-28)
SEARCH_PRICE = 14 / 1000                 # USD per search request after 5,000 free a month (Gemini 3 models)
ANSWER_CHARS = 600

QUERIES = """Below is the material for one picture of a moment in a novel. List the real-world things in this moment whose look or use an illustrator might need to check: a weapon, armour, a period's dress, a kind of building or room, a vehicle or a horse's harness, a way of fighting, riding, falling or moving. At most {n}, the ones that matter most to drawing the moment; none when nothing in it needs checking.

Write each as a short web search query about the real-world thing itself ("how a medieval flail is swung in combat", "Victorian gentleman's travelling clothes 1890s", "inside a ruined Gothic chapel crypt"). A query never holds the book's title, its author, a character's name or any name or word the book made up: only plain words for real things, with a period or place in the real world where the material makes it plain.

Return "queries": the list of query strings.

{material}"""
QUERIES_SCHEMA = {"type": "object", "required": ["queries"], "properties": {"queries": {"type": "array", "items": {"type": "string"}}}}
ASK = """Search the web once, then answer for an illustrator in two or three plain sentences: how this looks to the eye, and how a body holds, wears or moves with it. Only what can be seen: shapes, materials, how it is held or worn, the motion. No history, no dates beyond the period, and no names of people, films, series, games or books.

{query}"""
# a result that brings in an adaptation, a performer or an illustrator is dropped (with the book, author and index names)
MEDIA = re.compile(r"\b(films?|movies?|television|tv|series|episodes?|actors?|actress\w*|starring|adaptations?|adapted|"
                   r"illustrat\w*|novels?|video games?)\b", re.IGNORECASE)


def forbidden(doc: dict) -> list[str]:
    """What a query or an answer may not hold: the title, the authors, every name in the index, and each word of those
    names that is capitalised there and is not an ordinary English word (a coined name inside a fuller one). A thing's
    plain word ("flail" in "Bloody flail") is not a name: only the whole name is."""
    work = doc.get("work", {})
    names = [work.get("title", "")] + list(work.get("authors", []))
    names += [n["value"] for e in doc.get("entities", []) for n in e.get("names", [])]
    out = set()
    for n in names:
        n = " ".join(str(n).split())
        if not n:
            continue
        out.add(n.lower())
        for w in re.findall(r"[A-Za-zÀ-ÿ'’-]+", n):
            w = w.strip("'’-")
            if len(w) >= 3 and w[0].isupper() and w.lower() not in COMMON:
                out.add(w.lower())
    return sorted(out, key=len, reverse=True)


def mentions(text: str, forb: list[str]) -> str | None:
    """The first forbidden name `text` holds as a whole word or phrase, else None."""
    low = " " + re.sub(r"[^\w'’ -]+", " ", str(text).lower()) + " "
    for f in forb:
        if re.search(r"(?<![\w])" + re.escape(f) + r"(?![\w])", low):
            return f
    return None


def ground(query: str, key: str, timeout: float = 60) -> dict:
    """One grounded answer from Gemini: {"text", "sources": [{"uri", "title"}], "searches": [...], "cost"}."""
    body = {"contents": [{"parts": [{"text": ASK.format(query=query)}]}], "tools": [{"google_search": {}}]}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{GROUNDER}:generateContent",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    cand = (d.get("candidates") or [{}])[0]
    text = " ".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
    meta = cand.get("groundingMetadata", {})
    sources = [{"uri": c["web"].get("uri", ""), "title": c["web"].get("title", "")}
               for c in meta.get("groundingChunks", []) if "web" in c]
    searches = meta.get("webSearchQueries", [])
    u = d.get("usageMetadata", {})
    tin = u.get("promptTokenCount", 0) + u.get("toolUsePromptTokenCount", 0)
    tout = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
    cost = tin / 1e6 * GROUNDER_PRICE[0] + tout / 1e6 * GROUNDER_PRICE[1] + len(searches) * SEARCH_PRICE
    text = " ".join(text.split())
    if len(text) > ANSWER_CHARS:                      # cut at the last sentence end that fits
        cut = text[:ANSWER_CHARS]
        text = cut[:cut.rfind(". ") + 1] if ". " in cut else cut.rsplit(" ", 1)[0]
    return {"text": text, "sources": sources, "searches": searches, "cost": round(cost, 5)}


def lookup(doc: dict, material: str, writer, key: str) -> dict:
    """The lookup for one moment: {"queries": [...], "block": the <real_world> text or "", "cost"}; `writer` is a text
    model (its usage counts in the bench's text spend), the grounding cost is in "cost"."""
    forb = forbidden(doc)
    out = {"queries": [], "block": "", "cost": 0.0}
    try:
        got = writer.complete("You prepare pictures for an illustrated companion to a novel.",
                              QUERIES.format(n=MAX_QUERIES, material=material), QUERIES_SCHEMA)
        queries = [" ".join(str(q).split())[:160] for q in got.get("queries", []) if str(q).strip()][:MAX_QUERIES]
    except Exception as e:     # noqa: BLE001 - a bench records every outcome
        out["error"] = f"queries: {type(e).__name__}: {e}"[:200]
        return out
    kept = []
    for q in queries:
        row = {"query": q}
        out["queries"].append(row)
        if hit := mentions(q, forb):
            row["refused"] = f"holds a name: {hit}"
            continue
        try:
            g = ground(q, key)
        except Exception as e:     # noqa: BLE001
            row["error"] = f"{type(e).__name__}: {e}"[:200]
            continue
        row.update(g)
        out["cost"] += g["cost"]
        said = g["text"] + " " + " ".join(s["title"] for s in g["sources"])
        if hit := mentions(said, forb):
            row["dropped"] = f"mentions {hit}"
        elif m := MEDIA.search(said):
            row["dropped"] = f"mentions {m.group(0)}"
        elif g["text"]:
            kept.append(g["text"])
    out["block"] = " ".join(kept)
    out["cost"] = round(out["cost"], 5)
    return out


def env_key() -> str | None:
    return llm.env_key("gemini")
