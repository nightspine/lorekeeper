"""Draw bench/user_refs.py's two scenes again with a candidate USER_REF line, into the same set (DIR/strong.json).

    uv run --extra claude python bench/user_refs_stronger.py DIR [EPUB]
"""
import copy, hashlib, json, sys
from pathlib import Path
from lorekeeper import art, epub, llm
out = Path(sys.argv[1]); doc = json.loads(Path("examples/dracula/dracula.json").read_text())
bk = epub.read(sys.argv[2] if len(sys.argv) > 2 else "corpus/pg345/pg345.epub")
art.USER_REF = ("Take only what the person says from their pictures, never their drawing style; where their note and the "
                "prompt disagree about that thing, draw it as their picture shows.")
writer = llm.make("claude", llm.PROVIDERS["claude"]["cheap"], api_key=llm.env_key("claude"))
image = llm.make_image("gemini", "gemini-nano-banana-2.1", api_key=llm.env_key("gemini"))
il = art.Illustrator([art.Book(doc, bk, art.FileSink(out / "sets" / "watercolour"), None)], writer, image, "watercolour",
                     portraits=None, quality="standard", workers=1, log=print, pause=0, cap=0.2)
res = {}
for ev, at, ref, note in [("dracula.ev.141", "toc15.para49", "dress", "use this for the woman's dress"),
                          ("dracula.ev.130", "toc15.para01", "house", "this is the house")]:
    data = next((out / "refs").glob(f"{ref}.*")).read_bytes()
    u = art.UserRef(data, art.mime_of(data), hashlib.sha256(data).hexdigest(), note)
    job = il.job("event", ev, at=at)
    rec = il.draw(job, user_refs=[u])
    res[ev] = {"file": rec["file"], "prompt": rec["prompt"]}
(out / "strong.json").write_text(json.dumps(res, indent=1))
print(il.report.lines())
