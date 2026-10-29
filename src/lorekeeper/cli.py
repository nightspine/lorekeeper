"""`lorekeeper` command line: authoring and validation tools for index files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import conformance, epub, gating, locators, text, validate


def _provider_choices() -> list[str]:
    from . import llm
    return ["ollama", *llm.PROVIDERS]


def _add_provider_args(s, default: str) -> None:
    """--provider (every registry id), --model, --api-key-file and --base-url, shared by the model-using commands."""
    from . import llm
    envs = ", ".join(f"{llm.PROVIDERS[p]['key_env']}" for p in llm.PROVIDERS if p != "custom")
    s.add_argument("--provider", choices=_provider_choices(), default=default,
                   help="ollama (local), claude (needs `uv sync --extra claude`), openai, gemini, openrouter, mistral, "
                        "groq, xai, deepseek, or custom (any OpenAI-compatible server, with --base-url). "
                        f"Keys come from --api-key-file or the environment ({envs}).")
    s.add_argument("--api-key-file", metavar="PATH", help="file holding the provider's API key (default: its environment variable)")
    s.add_argument("--base-url", help="the server's base URL (the custom provider, or to point a provider at a proxy)")


def _make_model(args, model: str | None = None, provider: str | None = None):
    """The backend for --provider: a key from --api-key-file or the provider's environment variable."""
    from . import llm
    provider = provider or args.provider
    if provider == "ollama":
        return llm.make("ollama", model, url=getattr(args, "ollama_url", "http://localhost:11434"),
                        num_ctx=getattr(args, "num_ctx", 16384))
    key = None
    if getattr(args, "api_key_file", None):
        key = Path(args.api_key_file).read_text().strip() or None
    key = key or llm.env_key(provider)
    if key is None and provider not in ("custom", "claude"):
        var = llm.PROVIDERS[provider]["key_env"]
        raise SystemExit(f"error: no API key for {llm.PROVIDERS[provider]['label']}: set {var} or pass --api-key-file")
    try:
        return llm.make(provider, model, api_key=key, base_url=getattr(args, "base_url", None))
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def _dump(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def cmd_blocks(args) -> int:
    for b in epub.blocks(args.epub)[args.start : args.end]:
        print(f"{b.seq}\t{b.spine}\t{len(b.words)}\t{b.fingerprint}\t{b.raw[: args.width]}")
    return 0


def cmd_fill_anchors(args) -> int:
    """Fill fingerprints from seq hints and record the edition the index was built from."""
    index = _load(args.index)
    blocks = epub.blocks(args.epub)
    for a in index["anchors"]:
        if "seq" not in a:
            continue
        fp = blocks[a["seq"]].fingerprint
        if a.get("fp") and a["fp"] != fp:
            print(f"warning: {a['id']}: fingerprint changed", file=sys.stderr)
        a["fp"] = fp
    th = text.text_hash(b.words for b in blocks)
    fh = hashlib.sha256(Path(args.epub).read_bytes()).hexdigest()
    editions = [e for e in index.get("editions", []) if e.get("textHash") not in ("", th)]
    built = next((e for e in index.get("editions", []) if e.get("textHash") in ("", th)), {})
    built.update({"textHash": th, "fileHash": fh, "builtFrom": True})
    index["editions"] = [built, *editions]
    out = _dump(index)
    if args.write:
        Path(args.index).write_text(out)
    else:
        sys.stdout.write(out)
    return 0


def cmd_validate(args) -> int:
    issues: list[validate.Issue] = []
    indexes = [_load(p) for p in args.index]
    if args.series:
        issues += validate.check_series(_load(args.series), indexes)
    else:
        for p, ix in zip(args.index, indexes):
            issues += [validate.Issue(i.level, f"{p}: {i.where}", i.message)
                       for i in validate.check_index(ix)]
    if args.epub:
        if len(indexes) != 1:
            print("--epub needs exactly one index", file=sys.stderr)
            return 2
        issues += validate.check_against_book(indexes[0], epub.blocks(args.epub))
    for i in issues:
        print(i)
    errors = sum(1 for i in issues if i.level == "error")
    print(f"{errors} error(s), {len(issues) - errors} warning(s)", file=sys.stderr)
    return 1 if errors else 0


def cmd_view(args) -> int:
    indexes = [_load(p) for p in args.index]
    series = _load(args.series) if args.series else None
    finished = set(args.finished.split(",")) if args.finished is not None else None
    v = gating.view(indexes, gating.Position(args.book, args.anchor), series, args.order, finished)
    sys.stdout.write(_dump(v))
    return 0


def _locator_from_args(args) -> dict:
    forms = {"block": args.seq, "cfi": args.cfi, "xpointer": args.xpointer}
    given = {k: v for k, v in forms.items() if v is not None}
    if args.spine is not None:
        if (args.word is None) == (args.progress is None):
            raise locators.LocatorError("--spine needs exactly one of --word and --progress")
        given["spine"] = args.spine
        given.update({"word": args.word} if args.word is not None else {"progress": args.progress})
    elif args.word is not None or args.progress is not None:
        raise locators.LocatorError("--word and --progress need --spine")
    if len([k for k in given if k not in ("word", "progress")]) != 1:
        raise locators.LocatorError("give exactly one of --seq, --cfi, --xpointer, --spine")
    return given


def cmd_locate(args) -> int:
    """Simulate a reader: where is a reader (given as a locator), and which anchor have they reached?"""
    index = _load(args.index)
    book = epub.read(args.epub)
    try:
        found = locators.locate(book, _locator_from_args(args))
    except locators.LocatorError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    resolved = locators.resolve_edition(index, book)
    by_id = {a["id"]: a for a in index["anchors"]}
    cur = locators.position(index["anchors"], resolved, found.seq, inclusive=args.inclusive)
    print(f"block    {found.seq}" + ("  (before the first block)" if found.seq < 0 else ""))
    print(f"basis    {found.basis} ({'exact' if found.exact else 'approximate: a fallback decided the block'})")
    if 0 <= found.seq < len(book.blocks):
        print(f"text     {book.blocks[found.seq].raw[:90]}")
    hint = by_id[cur].get("seq") if cur else None
    print(f"anchor   {cur}" + (f"  (seq hint {hint})" if hint is not None else "")
          + f"  [{'inclusive' if args.inclusive else 'finished'}]")
    return 0


def cmd_toc(args) -> int:
    book = epub.read(args.epub)
    print(f"{book.title} — {', '.join(book.authors)} ({book.language}), {len(book.blocks)} blocks")
    ends = [c.start for c in book.chapters[1:]] + [len(book.blocks)]
    for i, (c, end) in enumerate(zip(book.chapters, ends), 1):
        words = sum(len(b.words) for b in book.blocks[c.start:end])
        print(f"{i:3}  seq {c.start:5}  {words:6} words  {c.title}")
    return 0


def _parse_ranges(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def cmd_generate(args) -> int:
    from . import generate, llm

    book = epub.read(args.epub)
    opts = generate.Options(
        chapters=_parse_ranges(args.chapters) if args.chapters else None,
        chunk_words=args.chunk_words, depth=args.depth, events=not args.no_events,
        verify=not args.no_verify, index_id=args.id,
        series_id=args.series_id, book_key=args.book_key, license=args.license)
    if args.provider != "ollama":
        # --model keeps its Ollama default; a cloud provider then uses its own default model.
        model = _make_model(args, args.model if args.model != "gemma4:12b-it-qat" else None)
        if args.check_model:
            opts.check_llm = _make_model(args, args.check_model)
    else:
        model = _make_model(args, args.model)
    report = generate.run(book, model, opts, args.epub, args.output, resume=args.resume,
                          log=lambda m: print(m, flush=True))
    print(json.dumps(report, indent=2))
    return 1 if report["validation_errors"] else 0


def cmd_web(args) -> int:
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        print("the web UI needs its extra: uv sync --extra web", file=sys.stderr)
        return 2
    libs = [(Path(d).name or "library", Path(d)) for d in args.library if Path(d).is_dir()]
    epubs = [Path(d) for d in args.corpus if Path(d).is_dir()]
    from .web.app import LOCAL_HOSTS
    allowed = LOCAL_HOSTS if args.host in LOCAL_HOSTS else None
    if allowed is None:
        print("warning: listening beyond localhost; anyone who can reach this port can use it", file=sys.stderr)
    token = args.api_token
    if args.api_token_file:
        try:
            lines = Path(args.api_token_file).read_text().splitlines()
        except OSError as e:
            print(f"cannot read --api-token-file: {e}", file=sys.stderr)
            return 2
        token = lines[0].strip() if lines else ""
        if not token:
            print("--api-token-file is empty", file=sys.stderr)
            return 2
    app = create_app(Path(args.data), libs, epubs, args.ollama_url, allowed_hosts=allowed, api_token=token)
    from .web.protocol import api_token as active_token
    if active_token(token):
        print("API token required for /v0")
    print(f"Lorekeeper on http://{args.host}:{args.port}  (data: {Path(args.data).resolve()})")
    # Open progress streams would otherwise hold a shutdown open indefinitely.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", timeout_graceful_shutdown=3)
    return 0


def _static_root() -> Path:
    return Path(__file__).resolve().parent / "web" / "static"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cmd_docs_export(args) -> int:
    """Lay out the docs site as DIR/docs, DIR/css, DIR/fonts, DIR/vendor, DIR/spec.md, DIR/readme.md,
    plus the landing page (the repo's site/ folder: index.html, site.css, site.js, assets) at DIR/ when it exists."""
    import shutil
    static, repo, out = _static_root(), _repo_root(), Path(args.dir)
    docs = static / "docs"
    if not docs.is_dir():
        print(f"no docs site at {docs}", file=sys.stderr)
        return 1
    if out.exists() and not out.is_dir():
        print(f"{out} is not a directory", file=sys.stderr)
        return 2
    if out.is_dir() and any(out.iterdir()) and not args.force:
        print(f"{out} is not empty (use --force to export into it)", file=sys.stderr)
        return 2
    pairs: list[tuple[Path, Path]] = []

    def add_tree(src: Path, dst: Path) -> None:
        for f in sorted(src.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                pairs.append((f, dst / f.relative_to(src)))

    add_tree(docs, out / "docs")
    add_tree(static / "fonts", out / "fonts")
    if (repo / "site").is_dir():
        add_tree(repo / "site", out)
    for src, dst in [(static / "css" / "base.css", out / "css" / "base.css"),
                     (static / "vendor" / "marked.min.js", out / "vendor" / "marked.min.js"),
                     (repo / "SPEC.md", out / "spec.md"), (repo / "README.md", out / "readme.md")]:
        if src.is_file():
            pairs.append((src, dst))
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    print(f"exported {len(pairs)} files to {out}")
    return 0


def cmd_profiles(args) -> int:
    """Tidy entities and backfill profiles in existing indexes (series reading order), in place."""
    import shutil
    import time

    from . import llm, profiles
    paths = [Path(x) for x in args.index]
    docs = [json.loads(p.read_text()) for p in paths]
    before = {i: {str(e) for e in validate.check_index(d) if e.level == "error"} for i, d in enumerate(docs)}
    changed: set[int] = set()
    if args.tidy:
        t = profiles.tidy(docs)
        changed |= t["changed"]
        print(f"tidy: removed {len(t['removed'])} description entries, folded {len(t['merged'])} into their entity")
        for name in t["removed"]:
            print(f"  - {name}")
        for drop, keep in t["merged"]:
            print(f"  {drop} → {keep}")
    if args.redo:
        print(f"redo: removed {profiles.clear(docs)} existing profiles")
        changed |= set(range(len(docs)))
    if not args.no_profiles:
        model = _make_model(args, args.model)
        stats = profiles.backfill(docs, model, log=lambda m: print(m, file=sys.stderr))
        print(f"profiles: {stats.get('profiles', 0)} written, {stats.get('dropped', 0)} dropped")
        changed |= set(range(len(docs)))
    for i in sorted(changed):
        errs = [e for e in validate.check_index(docs[i]) if e.level == "error" and str(e) not in before[i]]
        if errs:
            print(f"error: {paths[i].name} would become invalid: {errs[0]}", file=sys.stderr)
            return 1
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    backup = Path(args.backup) / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    for i in sorted(changed):
        shutil.copy2(paths[i], backup / paths[i].name)
        tmp = paths[i].with_suffix(".tmp")
        tmp.write_text(json.dumps(docs[i], indent=2, ensure_ascii=False) + "\n")
        tmp.replace(paths[i])
    print(f"wrote {len(changed)} file(s); originals in {backup}")
    return 0


def cmd_portraits(args) -> int:
    """Rewrite profiles from every passage that mentions each entity (series reading order), in place."""
    import shutil
    import time

    from . import epub, llm, portraits
    paths = [Path(x) for x in args.index]
    docs = [json.loads(p.read_text()) for p in paths]
    books = []
    for i, d in enumerate(docs):
        if args.epub and i < len(args.epub):
            src = Path(args.epub[i])
        else:
            built = next((e for e in d.get("editions", []) if e.get("builtFrom")), {})
            src = Path(args.data) / "books" / f"{built.get('fileHash', '')[:16]}.epub"
        if not src.exists():
            print(f"error: no EPUB for {paths[i].name} (pass --epub)", file=sys.stderr)
            return 1
        books.append(epub.read(src))
    before = {i: {str(e) for e in validate.check_index(d) if e.level == "error"} for i, d in enumerate(docs)}
    if args.redo:
        print(f"redo: removed {sum(portraits.clear(d) for d in docs)} existing portraits")
    model = _make_model(args, args.model or (llm.PROVIDERS[args.provider]["cheap"] if args.provider in llm.PROVIDERS else None))
    prior = [json.loads(Path(x).read_text()) for x in args.prior or []]
    stats = portraits.portraits(list(zip(docs, books)), model, log=lambda m: print(m, file=sys.stderr), prior=prior)
    print(f"portraits: {stats.get('portraits', 0)} written, {stats.get('dropped', 0)} dropped, "
          f"{stats.get('superseded', 0)} extraction profiles superseded; cost ${model.usage.cost_usd:.2f}")
    for i, d in enumerate(docs):
        errs = [e for e in validate.check_index(d) if e.level == "error" and str(e) not in before[i]]
        if errs:
            print(f"error: {paths[i].name} would become invalid: {errs[0]}", file=sys.stderr)
            return 1
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for p, d in zip(paths, docs):
            (out / p.name).write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {len(docs)} file(s) to {out}")
        return 0
    backup = Path(args.backup) / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    for p, d in zip(paths, docs):
        shutil.copy2(p, backup / p.name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(p)
    print(f"wrote {len(docs)} file(s); originals in {backup}")
    return 0


def cmd_conformance(args) -> int:
    failed = 0
    for path in conformance.cases(args.dir):
        if args.update:
            conformance.regenerate(path)
            print(f"updated {path.name}")
            continue
        ok, _, _ = conformance.check(path)
        print(f"{'ok  ' if ok else 'FAIL'} {path.name}")
        failed += not ok
    for path in conformance.locator_cases(args.dir):
        name = f"locators/{path.name}"
        if args.update:
            print(f"{'updated' if conformance.regenerate_locator(path) else 'skipped (edition missing)'} {name}")
            continue
        ok, _, _ = conformance.check_locator(path)
        print(f"{'skip' if ok is None else 'ok  ' if ok else 'FAIL'} {name}")
        failed += ok is False
    return 1 if failed else 0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lorekeeper")
    sub = p.add_subparsers(required=True)

    s = sub.add_parser("blocks", help="list an EPUB's text blocks (seq, spine, words, fp, text)")
    s.add_argument("epub")
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--end", type=int, default=None)
    s.add_argument("--width", type=int, default=100)
    s.set_defaults(fn=cmd_blocks)

    s = sub.add_parser("fill-anchors", help="compute anchor fingerprints from seq hints")
    s.add_argument("index")
    s.add_argument("--epub", required=True)
    s.add_argument("--write", action="store_true", help="rewrite the index file in place")
    s.set_defaults(fn=cmd_fill_anchors)

    s = sub.add_parser("validate", help="validate index files")
    s.add_argument("index", nargs="+")
    s.add_argument("--series")
    s.add_argument("--epub", help="also check anchors and quotes against this book")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("view", help="print the gated view at a position")
    s.add_argument("index", nargs="+")
    s.add_argument("--series")
    s.add_argument("--order")
    s.add_argument("--book")
    s.add_argument("--anchor")
    s.add_argument("--finished", help="comma-separated book keys (default: earlier books in order)")
    s.set_defaults(fn=cmd_view)

    s = sub.add_parser("locate", help="map a reader's locator (block, CFI, XPointer, spine+word/progress) to a block and the current anchor")
    s.add_argument("index")
    s.add_argument("--epub", required=True)
    s.add_argument("--seq", type=int, help="block seq in this EPUB")
    s.add_argument("--cfi", help='EPUB CFI, e.g. "epubcfi(/6/22!/4/108/1:0)"')
    s.add_argument("--xpointer", help="KOReader XPointer, e.g. /body/DocFragment[11]/body/div[3]/p[7]/text().0")
    s.add_argument("--spine", type=int, help="0-based linear spine document; with --word or --progress")
    s.add_argument("--word", type=int, help="0-based word index within --spine")
    s.add_argument("--progress", type=float, help="fraction 0..1 of the words of --spine")
    s.add_argument("--inclusive", action="store_true", help="count the current block (default: it does not count, §3.7)")
    s.set_defaults(fn=cmd_locate)

    s = sub.add_parser("toc", help="list an EPUB's table of contents (chapter numbers for --chapters)")
    s.add_argument("epub")
    s.set_defaults(fn=cmd_toc)

    s = sub.add_parser("generate", help="generate an index from an EPUB with a local Ollama model or a cloud model (your own key)")
    s.add_argument("epub")
    s.add_argument("-o", "--output", required=True)
    _add_provider_args(s, "ollama")
    s.add_argument("--model", default="gemma4:12b-it-qat", help="Ollama model, or a cloud model id (default: the provider's own default, e.g. claude-haiku-5-5, gpt-5-mini, gemini-2.5-flash)")
    s.add_argument("--chapters", help="table-of-contents entries to index, e.g. 5-6 or 5,7-9 (see `toc`)")
    s.add_argument("--chunk-words", type=int, default=2500, help="words per model call; smaller is more accurate")
    s.add_argument("--check-model", default=None, help="cloud model of the same provider for the fact-check (e.g. claude-sonnet-5-5); default: the same model")
    s.add_argument("--depth", choices=["light", "standard", "deep"], default="standard")
    s.add_argument("--no-events", action="store_true", help="skip the timeline (faster)")
    s.add_argument("--no-verify", action="store_true", help="skip the support check (faster, less accurate)")
    s.add_argument("--resume", action="store_true", help="continue an interrupted run")
    s.add_argument("--id", help="index id (default: from the title)")
    s.add_argument("--series-id")
    s.add_argument("--book-key")
    s.add_argument("--license", default="CC0-1.0")
    s.add_argument("--ollama-url", default="http://localhost:11434")
    s.add_argument("--num-ctx", type=int, default=16384)
    s.set_defaults(fn=cmd_generate)

    s = sub.add_parser("web", help="run the local Studio + Reader web UI")
    s.add_argument("--host", default=os.environ.get("LOREKEEPER_HOST") or "127.0.0.1",
                   help="address to bind (env LOREKEEPER_HOST); anything but a local address switches the Host check off")
    s.add_argument("--port", type=int, default=_env_int("LOREKEEPER_PORT", 8765), help="env LOREKEEPER_PORT")
    s.add_argument("--data", default=os.environ.get("LOREKEEPER_DATA") or "lorekeeper-data",
                   help="uploads, jobs and generated indexes (env LOREKEEPER_DATA)")
    s.add_argument("--library", action="append", default=["examples", "bench/runs"],
                   help="extra directories with index files (repeatable)")
    s.add_argument("--corpus", action="append", default=["corpus"],
                   help="directories with EPUBs used to map reading positions (repeatable)")
    s.add_argument("--ollama-url", default=os.environ.get("LOREKEEPER_OLLAMA_URL") or "http://localhost:11434",
                   help="env LOREKEEPER_OLLAMA_URL")
    s.add_argument("--api-token", help="require this bearer token on /v0 (overrides LOREKEEPER_API_TOKEN; visible in shell history)")
    s.add_argument("--api-token-file", help="read the bearer token from the first line of this file")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("docs", help="the documentation site")
    dsub = s.add_subparsers(required=True)
    d = dsub.add_parser("export", help="write the static docs site (with spec and styles) to a directory")
    d.add_argument("dir")
    d.add_argument("--force", action="store_true", help="export into a non-empty directory")
    d.set_defaults(fn=cmd_docs_export)

    s = sub.add_parser("profiles", help="tidy entities and backfill profiles in existing indexes")
    s.add_argument("index", nargs="+", help="index files, in series reading order")
    s.add_argument("--tidy", action="store_true", help="also remove description entries and fold wrapped names")
    s.add_argument("--no-profiles", action="store_true", help="only tidy")
    s.add_argument("--redo", action="store_true", help="rewrite existing profiles (with the current prompt)")
    _add_provider_args(s, "claude")
    s.add_argument("--model", default=None, help="model name (default: the provider's default)")
    s.add_argument("--backup", default="lorekeeper-data/backups", help="where originals are copied first")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_profiles)

    s = sub.add_parser("portraits", help="rewrite profiles from every passage that mentions each entity (a cloud model)")
    s.add_argument("index", nargs="+", help="index files, in series reading order")
    s.add_argument("--epub", action="append", help="each index's EPUB, in the same order (default: the web app's copy)")
    s.add_argument("--data", default="lorekeeper-data")
    _add_provider_args(s, "claude")
    s.add_argument("--model", default=None, help="model name (default: the provider's cheap model; for Claude, "
                                                  "claude-sonnet-5-5 is sharper at ~20x the cost)")
    s.add_argument("--prior", action="append", help="an earlier series book's index, read only (repeatable, reading order)")
    s.add_argument("--redo", action="store_true", help="remove existing portraits first")
    s.add_argument("--out", help="write the results to this directory instead of in place")
    s.add_argument("--backup", default="lorekeeper-data/backups", help="where originals are copied first")
    s.set_defaults(fn=cmd_portraits)

    s = sub.add_parser("conformance", help="run (or regenerate) conformance cases")
    s.add_argument("dir", nargs="?", default="conformance")
    s.add_argument("--update", action="store_true", help="rewrite expected views from the reference implementation")
    s.set_defaults(fn=cmd_conformance)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
