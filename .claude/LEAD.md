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
| `code-reviewer` | nothing; read-only. A Cursor agent on GPT-5.6 Sol, not a Claude one (step 4) | every branch, before it comes back to the lead |

Leads run on Opus 5.5 (`claude --model claude-opus-5-5`; the person asked on 2026-11-20 that every lead switch to it once the features then in flight had shipped, and that it stay the lead model from then on). Workers run on Sonnet (`model: sonnet` in each definition, `--model sonnet` on the command line) unless the brief says why a step needs Opus: a spec design decision with no precedent, or a change to `text.py` or `gating.py`. The reviewer is a Cursor agent running GPT-5.6 Sol at High (`cursor-agent --model gpt-5.6-sol-high`; the person decided this on 2026-11-18): the gate is where judgment is cheapest to buy, and a model from another maker does not share the workers' blind spots. The lead starts one for every feature.

A feature that spans areas gets one worker per area, started in sequence when their files overlap (spec first, then core, then clients and docs) and in parallel when they don't.

## One feature, start to finish

**Every branch gets a worktree, with no exceptions.** The main checkout at `~/projects/lorekeeper` stays on `main` and is never switched: it holds the briefs, the corpus and the lead's own checks. A feature, a fix round, a chore to the playbook, all of them are branched into `~/.herdr/worktrees/lorekeeper/<name>` (`git worktree add -b <branch> <path> main`) and the worktree is removed once its PR merged.

**0. The setup must be on `main`.** Worktrees branch from `main`, and a worker reads its definition from its own worktree. If `.claude/agents/` is not on `main` yet, ship it first.

**1. Scope.** Pick a slug: kebab-case, verb first (`fix-phone-reader`, `add-series-export`). Write `.claude/work/<slug>/brief.md` from the template at the end of this file. Name the roles, the files, what done means, and what is out of scope. The brief is the contract; a worker may not ask questions, so it has to be complete.

**2. A place to work.** One worktree and one Herdr space per feature:

```sh
SLUG=<slug>; WT=~/.herdr/worktrees/lorekeeper/$SLUG
git -C ~/projects/lorekeeper worktree add -b feat/$SLUG "$WT" main
herdr-app do space.new --name "lorekeeper: $SLUG" --folder "$WT"      # prints {space, tab, terminal}
herdr-app do tab.rename --id <tab> --name shell
```

The space nests under `lorekeeper` in the sidebar. Its first tab is a plain shell for the person.

**3. Start the workers.** One tab per role, running Claude Code as that agent, with the brief's absolute path in the first message. Workers run in `--permission-mode auto` like the lead, so they don't stop for every command, and with `--add-dir ~/projects/lorekeeper/corpus` so the gitignored test books can be read from any worktree without a prompt (a worker symlinks or copies them; `corpus` must not be committed). A prompt a worker still raises shows as blocked in Herdr; the lead cannot answer it (that would be one Claude approving for another), so tell the person the tab name.

Workers that run at the same time never share a worktree: git state (index, staged files) is per worktree, not per branch. Give each one its own, branched from the feature branch, and merge them back yourself when they hand off:

```sh
ROLE=core-engineer; RW=~/.herdr/worktrees/lorekeeper/$SLUG--$ROLE
git -C "$WT" worktree add -b feat/$SLUG-$ROLE "$RW" feat/$SLUG     # from the feature branch
herdr-app do tab.new --space <space> --name "work: $ROLE" --folder "$RW" ...   # the tab opens there
# after the handoff:
git -C "$WT" merge --no-ff feat/$SLUG-$ROLE && git -C "$WT" worktree remove "$RW" && git -C "$WT" branch -d feat/$SLUG-$ROLE
```

The feature worktree `$WT` is the integration branch; the reviewers read it (step 4). A lone worker can use `$WT` directly.

```sh
BRIEF=~/projects/lorekeeper/.claude/work/$SLUG
herdr-app do tab.new --space <space> --name "work: core-engineer" \
  --argv '["claude","--agent","core-engineer","--model","sonnet","--permission-mode","auto","--add-dir","~/projects/lorekeeper/corpus"]' \
  --task "You are the core-engineer agent for feature $SLUG. Read $BRIEF/brief.md and .claude/WORKER.md, do the work in this worktree on branch feat/$SLUG, and write your handoff to $BRIEF/handoff-core-engineer.md."
```

`herdr-app do` prints the terminal address. `hc wait <terminal>` returns at a check-in (about three minutes), not when the agent is done, so loop on it until `hc ls --json` shows the terminal's status is no longer `working` (`idle`, `done` or `blocked`). Keep `--task` short and free of quotes: a long task was not delivered once. Read the tab a few seconds later; if it sits at an empty prompt, send the task with `hc send --if idle <terminal> "..."` (it pastes and presses Enter), and put any long notes in a file the message points to. Wait with `hc wait <terminal>`, then read `$BRIEF/handoff-<role>.md` and `hc read <terminal> --lines 60`. Herdr shows the tab as working, blocked (needs the person: tell them which tab) or done.

**4. Review.** Every implementer is reviewed by a Cursor agent on Sol that sits beside it: a pane to the right of the worker, in the worker's own tab (the person asked on 2026-11-20; there is no separate review tab). Start it when that worker hands off. It runs in plan mode so it cannot edit, and follows the same instructions a Claude reviewer would (`.claude/agents/code-reviewer.md`). It only reads: Cursor asks before every shell command and the lead cannot answer for it, so the lead runs the checks and hands over the worker's diff and the results as files inside the feature worktree (`.claude/work/` is gitignored there too). When workers ran in their own worktrees, merge them into `$WT` first; every reviewer reads `$WT`.

```sh
cd $WT; R=.claude/work/$SLUG; mkdir -p $R; cp $BRIEF/brief.md $BRIEF/handoff-*.md $BRIEF/review-*.md $R/ 2>/dev/null
git diff main...HEAD -- <the files of this role> > $R/diff-$ROLE-1.patch     # one patch per worker
# the checks from code-reviewer.md ("What you run"), each command and the end of its output, into $R/checks-1.txt;
# uv may touch uv.lock: git checkout uv.lock afterwards
herdr-app ls                                                # the worker's pane id, under its tab
herdr-app do pane.split --direction right --pane <worker pane> \
  --argv '["cursor-agent","--trust","--mode","plan","--model","gpt-5.6-sol-high","--workspace","'$WT'"]'   # prints {pane, terminal}
hc label <terminal> herdr.crew=hex "herdr.crew.name=Sol reviewer ($ROLE)"
hc send <terminal> "You are the code reviewer. Your instructions are .claude/agents/code-reviewer.md: follow them. Review the $ROLE part of feature $SLUG, branch feat/$SLUG, in this worktree. Do not run shell commands: use only your file reading and search tools. Everything you need is in $R: brief.md, handoff-$ROLE.md, diff-$ROLE-1.patch (this worker's whole change against main) and checks-1.txt (the checks, already run by the lead on this commit). Review only what is in that patch, reading the surrounding code as needed. You cannot write files: give the whole review as your final message, in the format the instructions describe, starting with the verdict line."
```

`pane.split` takes no folder and no task: `--workspace` gives Cursor the worktree, `--trust` skips its "Trust this workspace" question for a new worktree (the lead cannot answer that either), and the task goes with `hc send` about ten seconds after the pane opens (not `--if idle`: a fresh Cursor reports no status until its first prompt). Reviewers of different workers can run at the same time; they only read.

The label makes it show in Herdr's Companions as "Sol reviewer (<role>)" (HEX, the tester); for a later round name it "Sol reviewer (<role>) 2" and add `review-<role>-<n>.md` and a patch of only that round's commits to what it reads. It reports working and done like the Claude agents, so wait on it the same way. When it is done, copy its final message (`hc read <terminal> --lines 300`) into `$BRIEF/review-<role>-1.md` unchanged, then add a section with your check of each blocking finding against the code and your decision on it, which the worker reads as overriding the brief. Its findings are claims, not a verdict you pass on unread. Close the reviewer's pane (`herdr-app do pane.close --pane <pane>`) as soon as its review is filed (the person asked on 2026-11-18): a finished reviewer is not left idle, and a later round splits a new one beside the same worker.

This needs a one-time setup per machine, outside the repository: `cursor-agent` signed in on a plan that offers Sol (`cursor-agent models`); `~/.cursor/hooks.json` calling `~/.cursor/hooks/herdr-status.sh` on `beforeSubmitPrompt` and `stop` (not `sessionStart`: it can land after the first prompt and overwrite working) (Cursor does not report its status to Herdr by itself; the script writes the OSC 7501 report to the terminal of the agent's process); and the crew mod reading the `herdr.crew.name` label (`labelName` in `mods/crew/cast.js`, a local change a mod update would undo). If Cursor or Sol is not available, say so to the person and fall back to the Claude reviewer for that round: `claude --agent code-reviewer --model opus --permission-mode auto --add-dir ~/projects/lorekeeper/corpus`, which writes the review file itself.

Read the verdict. On CHANGES REQUESTED, send the findings back to the worker whose area they fall in. Its Claude is still open in its tab:

```sh
hc send <worker terminal> "Review round 1 requested changes: $BRIEF/review-$ROLE-1.md. Fix every blocking finding, add a Round 2 section to your handoff, commit."
```

(If the tab was closed, start a new one named `fix: <role> round 2`.) Then a new reviewer pane beside it for round 2. Stop after three rounds and bring the disagreement to the person.

**5. The lead's own review.** With a PASS in hand, in the `lead` tab: read `git diff main...feat/$SLUG` yourself, run `/code-review high feat/$SLUG` for a second reading, by Claude this time and not anchored on the reviewer's, and run the checks (`uv run pytest -q`, `uv run lorekeeper conformance`, `node tests/js/run_conformance.mjs`). Decide: ship, send back for one more round, or stop and ask the person.

**6. Ship.** Pull requests are squash-merged and the title is the commit message, in Conventional Commits form. The body: the brief's goal, the handoff's Verified section, the review verdict, and what was left out. Never an attribution trailer.

```sh
git -C "$WT" push -u origin feat/$SLUG
gh pr create --base main --head feat/$SLUG --title "feat: ..." --body-file $BRIEF/pr.md
gh pr checks --watch <number> && gh pr merge <number> --squash --delete-branch
git -C ~/projects/lorekeeper worktree remove "$WT"
herdr-app do space.close --id <space> --confirmed true
```

Merging needs no question any more: the person said on 2026-11-18 "merge when CI is green, do not wait for me, you are in charge of releases". So once the review gate has passed and CI is green, squash-merge. Then run the release: find the release-please PR (its title starts with `chore(main): release`; `gh pr list --search` cannot find it, filter the JSON by title), close and reopen it so its checks run on the bot's branch, merge it when green, confirm the tag and the Docker image, and open and merge the `uv.lock` refresh chore that RELEASING.md asks for. Ask first only for anything destructive or outward beyond that. Keep `.claude/work/<slug>/` (it is gitignored) as the record of what was decided.

**7. Report** in one message: what shipped, the PR link, what was left out and why, and where the record is.

## Herdr conventions

- Space `lorekeeper` is the lead's home. Its tabs: one per lead session, `server` (when `lorekeeper web` runs), `shell`.
- A lead names its own tab after its work as soon as it starts (the person asked on 2026-11-17): `lead: <slug or a few plain words>`, always with the `lead: ` prefix so the person can tell leads from workers and see what each one is on. Find your tab with `$HC_TERMINAL_ID` and `herdr-app ls`, then `herdr-app do tab.rename --id <tab> --name "lead: <work>"`. Rename again when the work changes, and to `lead: open for work` once the feature is closed out (PR merged, release done, space and worktrees gone; the person asked on 2026-11-20), so the person can see which leads can take a brief.
- Feature spaces are `lorekeeper: <slug>`, one worktree each, nested under the repo space.
- Tabs in a feature space: `shell`, `work: <role>`, `fix: <role> round <n>`. A worker's tab holds two panes while it is under review: the worker on the left, its Sol reviewer on the right. The tab name says the role; the crew robot's name is Herdr's, not ours, except the reviewer's, which the lead names "Sol reviewer" (step 4).
- A worker that needs the person shows as blocked; tell the person the space and tab name.
- The lead dismisses its agents on its own judgment, without asking (the person said so on 2026-11-13 and again on 2026-11-17): close a worker tab once its branch is merged and its handoff is on disk, and a reviewer pane once its verdict is on disk, and close the feature space when the PR has merged. The lasting record is `.claude/work/<slug>/`, not the terminal. While a tab stays open, rename it `done: <role>` so the space reads at a glance.
- One Ollama run at a time across all spaces. Prefer a cloud provider for worker generation runs and say what it cost.
- Never restart `lorekeeper web` or kill a generate process while `/api/jobs` shows a run.

## Small tasks

A lookup, a question about the code, or a one-file fix that nobody needs to watch: use the Agent tool with `subagent_type` set to the role and `isolation: "worktree"`, with the same brief rules in the prompt. Code written that way is still reviewed (the Sol reviewer on the diff, headless and given the same files as in step 4: `cursor-agent -p --trust --mode plan --model gpt-5.6-sol-high --workspace <worktree> "<task as in step 4>"`) before it is merged.

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
