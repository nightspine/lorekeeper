# How every Lorekeeper worker agent works

Every agent definition in `.claude/agents/` points here; read it first.

You were started by the lead agent with a brief. The brief is a file whose absolute path is in your first message. Read it, then `AGENTS.md` and `CLAUDE.md` at the repo root, then the files the brief names.

## Where you work

- You run in a git worktree made for one feature, on branch `feat/<slug>`. Work only inside it. Never touch the main checkout, other worktrees, `~/.config/herdr-app` or `lorekeeper-data/`.
- Stay inside your role's area (listed in your definition). If the feature needs a change outside it, make the smallest one that unblocks you and say so in the handoff, or stop and report that another role is needed. Don't quietly take over another area.
- Nobody answers questions while you work. Make routine calls yourself, state each assumption in the handoff, and stop only when going on under any assumption would waste the work.

## How you ship

- Commit as you go. Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`), one concern per commit. Never add `Co-authored-by`, "Generated with" or any attribution trailer.
- Before handing off, run the checks that cover your change (your definition lists them) and the full `uv run pytest -q`. Tests that need `corpus/` skip without it; say so if they did.
- Never restart `lorekeeper web` or kill a generate process while a run is active: check `/api/jobs` first.
- The spoiler invariant comes first: nothing past the reader's position may be revealed by any view, count, marker, graph or protocol response.

## The handoff

Write `<brief folder>/handoff-<role>.md` (the brief folder is where the brief lives) with these sections, then end your turn with a one-paragraph summary:

1. **Done**: what changed, file by file, in sentences.
2. **Verified**: each command you ran and what it printed (pass counts, skips, failures).
3. **Not done**: anything in the brief you left out, and why.
4. **Assumptions and questions** for the lead.
5. **Commits**: `git log --oneline main..HEAD`.

## The review loop

A reviewer reads your branch and writes `<brief folder>/review-<n>.md`. If it requests changes, you get a new message naming it: fix every blocking finding, add a **Round n** section to your handoff saying what you changed for each one, commit, and summarise again. Disagree in the handoff with a reason, not by ignoring a finding.
