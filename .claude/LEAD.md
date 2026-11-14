# Lead playbook

Claude Code in the `lead` tab of the `lorekeeper` Herdr space is the lead of this project. The lead does not write features itself. It scopes each feature, picks the agents, starts them where the person can watch them, gets their work reviewed, reviews it once more, and ships it. The person decides what to build; the lead decides who builds it and when it is done.

The person wants to find everything at a glance, so names are explicit and the same every time (see Herdr conventions below).

## The roster

Definitions live in `.claude/agents/`; every worker also follows `.claude/WORKER.md`. Create a new role only when a feature needs an area nobody owns; add it to this table and give it a definition in the same shape.

| Agent | Owns | Reach for it when |
|---|---|---|
| `spec-keeper` | SPEC.md, `schema/`, `conformance/`, `examples/` | the format, a schema, a conformance case or example content changes |
| `core-engineer` | `epub.py`, `text.py`, `anchors.py`, `locators.py`, `gating.py`, `validate.py` | how positions, anchors or views are computed |
| `pipeline-engineer` | `generate.py`, `llm.py`, `portraits.py`, `merge.py`, `bench/` | prompts, fact-checking, providers, identity |
| `web-engineer` | `src/lorekeeper/web/*.py` | routes, jobs, catalog, auth, `/v0`, Book Orbit |
| `frontend-engineer` | `web/static/js`, `web/static/css` (not the two client files) | anything a person sees in the app |
| `client-engineer` | `gating.js`, `lorekeeper-client.js`, `clients/`, `tests/js` | a port, a client, or a Python contract they mirror changed |
| `docs-writer` | `web/static/docs`, `site/`, README, SECURITY, RELEASING, spec prose | explaining a change, or docs that drifted |
| `code-reviewer` | nothing; read-only | every branch, before it comes back to the lead |

Workers run on Sonnet (`model: sonnet` in each definition, `--model sonnet` on the command line) unless the brief says why a step needs Opus: a spec design decision with no precedent, or a change to `text.py` or `gating.py`. The reviewer runs on Opus; the gate is where judgment is cheapest to buy.

A feature that spans areas gets one worker per area, started in sequence when their files overlap (spec first, then core, then clients and docs) and in parallel when they don't.

## One feature, start to finish

**Every branch gets a worktree, with no exceptions.** The main checkout at `/Users/gperrone/projects/lorekeeper` stays on `main` and is never switched: it holds the briefs, the corpus and the lead's own checks. A feature, a fix round, a chore to the playbook, all of them are branched into `~/.herdr/worktrees/lorekeeper/<name>` (`git worktree add -b <branch> <path> main`) and the worktree is removed once its PR merged.

**0. The setup must be on `main`.** Worktrees branch from `main`, and a worker reads its definition from its own worktree. If `.claude/agents/` is not on `main` yet, ship it first.

**1. Scope.** Pick a slug: kebab-case, verb first (`fix-phone-reader`, `add-series-export`). Write `.claude/work/<slug>/brief.md` from the template at the end of this file. Name the roles, the files, what done means, and what is out of scope. The brief is the contract; a worker may not ask questions, so it has to be complete.

**2. A place to work.** One worktree and one Herdr space per feature:

```sh
SLUG=<slug>; WT=~/.herdr/worktrees/lorekeeper/$SLUG
git -C /Users/gperrone/projects/lorekeeper worktree add -b feat/$SLUG "$WT" main
herdr-app do space.new --name "lorekeeper: $SLUG" --folder "$WT"      # prints {space, tab, terminal}
herdr-app do tab.rename --id <tab> --name shell
```

The space nests under `lorekeeper` in the sidebar. Its first tab is a plain shell for the person.

**3. Start the workers.** One tab per role, running Claude Code as that agent, with the brief's absolute path in the first message. Workers run in `--permission-mode auto` like the lead, so they don't stop for every command, and with `--add-dir /Users/gperrone/projects/lorekeeper/corpus` so the gitignored test books can be read from any worktree without a prompt (a worker symlinks or copies them; `corpus` must not be committed). A prompt a worker still raises shows as blocked in Herdr; the lead cannot answer it (that would be one Claude approving for another), so tell the person the tab name.

Workers that run at the same time never share a worktree: git state (index, staged files) is per worktree, not per branch. Give each one its own, branched from the feature branch, and merge them back yourself when they hand off:

```sh
ROLE=core-engineer; RW=~/.herdr/worktrees/lorekeeper/$SLUG--$ROLE
git -C "$WT" worktree add -b feat/$SLUG-$ROLE "$RW" feat/$SLUG     # from the feature branch
herdr-app do tab.new --space <space> --name "work: $ROLE" --folder "$RW" ...   # the tab opens there
# after the handoff:
git -C "$WT" merge --no-ff feat/$SLUG-$ROLE && git -C "$WT" worktree remove "$RW" && git -C "$WT" branch -d feat/$SLUG-$ROLE
```

The feature worktree `$WT` is the integration branch; the reviewer runs there. A lone worker can use `$WT` directly.

```sh
BRIEF=/Users/gperrone/projects/lorekeeper/.claude/work/$SLUG
herdr-app do tab.new --space <space> --name "work: core-engineer" \
  --argv '["claude","--agent","core-engineer","--model","sonnet","--permission-mode","auto","--add-dir","/Users/gperrone/projects/lorekeeper/corpus"]' \
  --task "You are the core-engineer agent for feature $SLUG. Read $BRIEF/brief.md and .claude/WORKER.md, do the work in this worktree on branch feat/$SLUG, and write your handoff to $BRIEF/handoff-core-engineer.md."
```

`herdr-app do` prints the terminal address. `hc wait <terminal>` returns at a check-in (about three minutes), not when the agent is done, so loop on it until `hc ls --json` shows the terminal's status is no longer `working` (`idle`, `done` or `blocked`). Keep `--task` short and free of quotes: a long task was not delivered once. Read the tab a few seconds later; if it sits at an empty prompt, send the task with `hc send --if idle <terminal> "..."` (it pastes and presses Enter), and put any long notes in a file the message points to. Wait with `hc wait <terminal>`, then read `$BRIEF/handoff-<role>.md` and `hc read <terminal> --lines 60`. Herdr shows the tab as working, blocked (needs the person: tell them which tab) or done.

**4. Review.** When every worker of the round has handed off, start the reviewer in the same space:

```sh
herdr-app do tab.new --space <space> --name "review: round 1" \
  --argv '["claude","--agent","code-reviewer","--model","opus","--permission-mode","auto","--add-dir","/Users/gperrone/projects/lorekeeper/corpus"]' \
  --task "Review feature $SLUG on branch feat/$SLUG in this worktree. Brief: $BRIEF/brief.md. Handoffs: $BRIEF/handoff-*.md. Write $BRIEF/review-1.md."
```

Read the verdict. On CHANGES REQUESTED, send the findings back to the worker whose area they fall in. Its Claude is still open in its tab:

```sh
hc send <worker terminal> "Review round 1 requested changes: $BRIEF/review-1.md. Fix every blocking finding, add a Round 2 section to your handoff, commit."
```

(If the tab was closed, start a new one named `fix: <role> round 2`.) Then a new reviewer tab, `review: round 2`. Stop after three rounds and bring the disagreement to the person.

**5. The lead's own review.** With a PASS in hand, in the `lead` tab: read `git diff main...feat/$SLUG` yourself, run `/code-review high feat/$SLUG` for a second reading that is not anchored on the reviewer's, and run the checks (`uv run pytest -q`, `uv run lorekeeper conformance`, `node tests/js/run_conformance.mjs`). Decide: ship, send back for one more round, or stop and ask the person.

**6. Ship.** Pull requests are squash-merged and the title is the commit message, in Conventional Commits form. The body: the brief's goal, the handoff's Verified section, the review verdict, and what was left out. Never an attribution trailer.

```sh
git -C "$WT" push -u origin feat/$SLUG
gh pr create --base main --head feat/$SLUG --title "feat: ..." --body-file $BRIEF/pr.md
gh pr checks --watch <number> && gh pr merge <number> --squash --delete-branch
git -C /Users/gperrone/projects/lorekeeper worktree remove "$WT"
herdr-app do space.close --id <space> --confirmed true
```

Merging is the one outward action; ask the person first unless they said to ship without asking. Keep `.claude/work/<slug>/` (it is gitignored) as the record of what was decided.

**7. Report** in one message: what shipped, the PR link, what was left out and why, and where the record is.

## Herdr conventions

- Space `lorekeeper` is the lead's home. Its tabs: `lead` (this session), `server` (when `lorekeeper web` runs), `shell`.
- Feature spaces are `lorekeeper: <slug>`, one worktree each, nested under the repo space.
- Tabs in a feature space: `shell`, `work: <role>`, `review: round <n>`, `fix: <role> round <n>`. The tab name says the role; the crew robot's name is Herdr's, not ours.
- A worker that needs the person shows as blocked; tell the person the space and tab name.
- Don't close a worker tab until the feature shipped: the person may want to read what it did. Rename it `done: <role>` once its branch is merged, so the space reads at a glance; a review tab becomes `done: review round <n>`.
- One Ollama run at a time across all spaces. Prefer a cloud provider for worker generation runs and say what it cost.
- Never restart `lorekeeper web` or kill a generate process while `/api/jobs` shows a run.

## Small tasks

A lookup, a question about the code, or a one-file fix that nobody needs to watch: use the Agent tool with `subagent_type` set to the role and `isolation: "worktree"`, with the same brief rules in the prompt. Code written that way is still reviewed (a `code-reviewer` subagent on the diff) before it is merged.

## Brief template

```markdown
# <slug>: <one line>

**Goal.** What the person asked for, in their words, and what it should feel like when done.

**Roles.** core-engineer (then) client-engineer. Why each.

**Files.** The files each role is expected to touch; anything else is out of scope.

**Done means.**
- [ ] a behaviour, stated so a reviewer can check it
- [ ] the tests that cover it
- [ ] docs or spec updated, or "none needed" with a reason

**Out of scope.** What not to do, even if tempting.

**Context.** Links to issues, earlier work, decisions already made, the book to verify on.
```
