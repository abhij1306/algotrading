---
name: hat-architecture-audit
description: Audit one area of the algotrading codebase through one lens (architecture depth, money safety, or test shape) and write ranked, evidence-backed findings to a report outside the repo. Read-only and non-interactive, so it can run as a background subagent while builders work. Use when the orchestrator asks for an audit, or before planning a refactor of a HAT subsystem.
disable-model-invocation: true
---

# HAT architecture audit

Find the places where the code makes the next change or the next bug more likely, and say exactly what to do about each one. This skill is adapted from Matt Pocock's `improve-codebase-architecture` and `codebase-design`, without the interactive grilling: an audit runs unattended, so it ends in a report the orchestrator triages into PRs.

**Read-only.** Never edit, commit or push. Don't run anything that calls Fyers, NSE or Telegram. Tests run only against fakes and fixtures, as CI runs them.

**The repo is public.** Write the report to `/data/workspace/audits/`, never into the repo. A finding that describes an exploitable flaw names the class of problem and the file, not a working exploit.

## Inputs

The caller gives you:

- **Lens:** `architecture`, `money-safety` or `tests` (sections below).
- **Scope:** a directory or a list of modules. If none is given, rank hot spots with `git log --since=60.days --name-only --format= -- backend/app frontend/src | sort | uniq -c | sort -rn | head -30`, then start from the top.
- **Report path:** where to write. The default is `/data/workspace/audits/<date>-<lens>-<scope-slug>.md`.

Read first: `CLAUDE.md`, `docs/architecture.md`, `docs/invariants.md`, `docs/decisions.md`, and the `.claude/skills/` skill for your area (`trading-safety` for execution and risk). Decisions recorded there are settled. Report a conflict with one only when the friction is real, and mark it as a conflict.

## Vocabulary

Use these terms exactly. Don't drift into "component", "service" or "boundary".

- **Module:** anything with an interface and an implementation, at any scale.
- **Interface:** everything a caller must know: signature, invariants, ordering, error modes, config, clock and timezone assumptions.
- **Depth:** behaviour a caller or test gets per unit of interface learned. A module is **shallow** when its interface is nearly as complex as its implementation.
- **Seam:** where a module's interface lives, so behaviour can change without editing callers. One adapter means the seam is hypothetical. Two adapters, such as `FyersHatBroker` and `FakeBroker`, mean it is real.
- **Locality:** a change, bug or verification concentrates in one place.
- **Deletion test:** imagine deleting the module. If complexity vanishes, it was a pass-through. If it reappears across N callers, it earns its keep.

## Lenses

### architecture

Explore and note where you hit friction:

- Understanding one concept means bouncing between many small modules.
- Shallow modules, and pass-through wrappers that fail the deletion test.
- Two code paths that do the same job, for example a legacy `app/services` path beside `app/hat`, or two clocks, two money types or two order builders. Name which one is live and whether the other can still be reached.
- Logic that leaks across a seam, such as engine code that knows Fyers payload shapes, or routers that do risk math.
- Very large files (`wc -l`) that change for several unrelated reasons.
- Modules hard to test through their interface, so the tests reach into internals.

### money-safety

Read the code paths that can place, change, cancel or account for an order, or move a stop. Check each invariant in `trading-safety` against the code, not the docs. Pay special attention to:

- Fail-open paths: an exception, `None`, timeout or empty response that lets an order through or drops a stop.
- Idempotency and retries: can a retry, restart or duplicate message place a second order?
- Reconciliation gaps between the broker's state and ours: partial fills, rejects, GTT swaps, restarts mid-order.
- Time: IST against UTC, market-hours edges (09:15, 15:30), holidays, clock injection against `datetime.now()`.
- Exact money: `float` anywhere in price, quantity, P&L or sizing math.
- Kill switches, pause and modes: is every order path gated, including scheduler jobs and Telegram approvals?

A money-safety finding needs a concrete failure: the state, the input and the wrong outcome. A rule merely restated is not a finding.

### tests

Tests are small and only for pure functions at their seams (money maths, parsers, state rules); changes are verified by running them on real data (CLAUDE.md). Flag what departs from that:

- World tests: a test that runs a backtest, the daily pipeline, a scheduler cycle or a full database on generated or fake data. Say what pure function it should test instead, or that it should go.
- Ceremony: deliberate-break records, broken variants kept only to prove a test fails, test digests, hand-kept lists a conformance check copies from a doc, and test helpers or rigs larger than the code they exercise.
- A pure money or state function on a live path with no small test of its own.
- Tests that mock past the interface, or assert on internals, so a refactor breaks them and a real bug doesn't.
- Fakes that drift from the real adapter: compare `FakeBroker` and the Fyers SDK fake with `fyers_adapter.py`.
- Flaky signals: wall-clock sleeps, real time, test order dependence.

Don't run the test suites; read them. Measure a slow file with one targeted run in the background if the cost matters.

## Verify before you report

Each finding must survive your own attempt to refute it. Open the code, trace the call, and check whether a caller already guards the case. Drop anything you can't point to by `file:line`. A plausible finding you couldn't confirm is marked `Plausible` with what would confirm it. Ignore style and nitpicks.

## Report

Write markdown to the report path:

```
# <lens> audit: <scope>  (<date>, main @ <sha>)

## Summary
3 to 5 sentences: the state of the area and the one thing to fix first.

## Findings
### F1. <title>   [Critical|High|Medium|Low] [Confirmed|Plausible] [Strong|Worth exploring|Speculative]
Files: path:line, ...
Problem: what is wrong, and the concrete failure or friction.
Fix: what would change, in plain words. For architecture, the deepened module and where its seam sits.
Tests: the test that would have caught it, or that proves the fix.
Size: S (< 1 h), M (one PR), L (several PRs).
```

Rank by severity, then by leverage. Put Critical money-safety findings first whatever the lens. End with **Suggested PRs**: group the findings into PR-sized units that a builder could take one by one, and give each a one-line goal.

Finish by replying to the caller with the report path, the count by severity, and the top three findings in one line each. Don't paste the report.
