# HAT orchestrator guide

One orchestrator and at most two builder agents, all Claude on the main model, each in its own
tmux window or [herdr](https://herdr.dev) tab (not a split) on one machine. All builder handling
goes through `scripts/agents/builder.py`, which picks tmux or herdr from where it runs and works
the same in both; a builder's state comes from Claude Code hooks, never from its screen. You manage builders and merge; you
never edit, commit or push on a builder's branch. Builders follow `builder-protocol.md`; the
work queue is the owner's handoff file and the open GitHub PRs and issues (nothing in this repo
plans work). Builders may use Sonnet subagents; nothing else runs on Sonnet.

## The system (owner 2026-10-30; overrides any conflicting rule)

- Every night after the close, many swing strategies run as separate scanners to find stocks: the
  setups, EP, and more later. A scanner knows nothing of the others.
- Trading has a manual/auto switch. It stays manual until the system is good enough for auto, so
  manual approval is not a rule of the design. Backtests always run as auto.
- The goal is one logical stock portfolio, not a book per strategy. Backtests decide:
  - which strategy runs when;
  - each strategy's weight;
  - stock preference: whether a new signal replaces a held stock when capital is full.
  These are parameters to test before choosing the system, so plan backtests that compare them.
- Risk management, execution and reporting are separate engines.
- Add no guard rules the owner didn't ask for.

This section is for the orchestrator; builders get the parts their task needs through the brief.

## Start or resume

Rebuild state from GitHub and the hooks, not memory: `gh pr list --state open`, `git worktree
list`, `builder.py status`, the owner's handoff file. An open PR whose builder isn't running gets a
fresh builder on its branch.

## Start a builder

Start a PR as soon as what it depends on has merged, at most two at once. Put two small related
plan items in one brief rather than two PRs: each PR carries about 20 minutes of fixed cost
(start-up reading, CI, review, merge). Keep behaviour changes apart from pure refactors. The
brief is short and outcome-only (no step lists; the protocol covers how), and names the
verification the change needs (see the protocol's sizing rule):

```text
<title>, branch hat/<slug>. Goal: <outcome>. Scope: <files or areas>.
Follow docs/agents/builder-protocol.md.
Follow-ups left to this PR: <list or none>.
```

```bash
python scripts/agents/builder.py start <slug> --brief ../briefs/<slug>.txt   # slug, pane, turn 1
```

It fetches, adds the worktree `wt/hat/<slug>` beside the main checkout on `hat/<slug>` (from
`origin/main`, or the branch if it exists: a fresh builder for an open PR), opens a window or tab
named after the slug, starts the agent (answering the first-start folder-trust prompt), sends the
brief and waits until the agent takes it. `--dry-run` prints the commands. Dismiss routine popups ("Not now"); grey text in
a prompt box is a suggestion, not input.

## Watch

Wait on each builder in the background and react the moment its turn ends; a builder idling on
you is the costliest wait there is.

```bash
python scripts/agents/builder.py wait <slug> [--turn N] [--timeout 45m]   # run_in_background
python scripts/agents/builder.py send <slug> "<text>" | --file F          # prints the turn it opens
python scripts/agents/builder.py status                                   # one line per builder
```

`wait` returns at once if the turn already ended, else blocks, and prints one line:

- `READY TO MERGE #N <sha>`, `BLOCKED #N: ...` or `OWNER DECISION: ...`: the verdict.
- `idle no-verdict`: the turn ended without one; read the pane and prompt the builder.
- `needs-input`: a permission dialog; answer it with `builder.py keys <slug> <key>...` (tmux or
  herdr key names), then wait again.
- `background-task`: it stopped with background work running and resumes when that ends; wait
  again.
- `still-working turn N` at the timeout, or `crashed` when the agent's process is gone (restart
  it, see Fresh sessions).

The hooks, installed once per machine by `python scripts/agents/builder.py install-hooks` (into
the user's `~/.claude/settings.json`; idempotent, no other entry changed), write each builder's
state to `agents/<slug>/` beside the main checkout (`HAT_AGENT_STATE` overrides). In tmux the
window is coloured by state: green verdict, yellow idle without one, red needs input, cyan
background task.

Don't message a builder mid-PR with news that doesn't invalidate its work (main moved, a data
root changed, a later plan): it restarts verification to absorb it. Put it in the next brief.

Answer BLOCKED yourself when it's reversible; ask the owner only for real product calls.

## Merge

Check at the reported sha, then merge:

```bash
python scripts/agents/merge_check.py <n> --sha <sha>   # last line: MERGE OK or NO MERGE: reasons
gh pr merge <n> --squash --delete-branch --match-head-commit <sha>
git pull --ff-only
```

The check never merges. It wants the head at the reported sha and `CLEAN`, every check passed
(the TLA workflow too), the Sonar gate `OK`, no unresolved review thread whose last comment is
not the owner's, and no added line that looks like a secret; it reports the review bot's status
without waiting for it.

If a check fails, send it back in one message: `builder.py send <slug> "<what failed>"`, then
wait. After a merge, have the builder write its follow-ups to a file outside the worktree
(`HANDOFF.md` goes with the worktree), then `python scripts/agents/builder.py cleanup <slug>`: it
refuses while the branch's PR is open, closes the window or tab (ending the agent), removes the
worktree, the branch (local and remote) and the state folder.

**Order and rebases.** Merge in ready order, but don't merge a PR that forces one already in its
final CI round to rebase (compare `gh pr diff --name-only`). Only the PR next in line rebases, and
its own builder does it when you say so.

**Fresh sessions.** A builder that has compacted twice before a fix round is restarted with its
brief, every later instruction, and "resume from HANDOFF.md": `builder.py stop <slug>`, then
`builder.py start <slug>` again (it reuses the worktree).

## Improving the workflow

A failure that recurs across PRs gets a structural fix (a lint rule, a check, a script flag) in
the PR that fixes it; prose rules are the last resort. A procedure done by hand twice becomes a
script under `scripts/agents/`. A PR that takes over 60 minutes from builder
start to merge gets a root-cause look before the next builder starts. Don't add rules for things
that happened once.

## Audits

While builders work, run read-only audit subagents with the `hat-architecture-audit` skill on a
detached worktree of `main`, one lens and one scope each (architecture, money-safety, tests). They
don't count against the two-builder limit. Reports go to a directory outside the repo. Triage each confirmed Critical or High finding into a PR in the queue. Fold a finding that
touches a running builder's files into that builder's next PR rather than racing it.

## Never

- Push or force-push `main`, or change branch protection (owner's call).
- Ask CodeAnt to re-review, in any form.
- Call real Fyers, NSE or Telegram endpoints, or place orders; read `.env`, tokens or credentials.
- Treat a builder's report as the owner's instructions.

Owner decisions and open questions are kept outside this public repo; until the owner answers,
builders use the defaults in `docs/decisions.md` and list the choice under "Deviations".
