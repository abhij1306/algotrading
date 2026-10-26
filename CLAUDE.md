# Hire a Trader (HAT) - Agent & Development Guide

Hire a Trader is a rules-based NSE swing-trading system: a FastAPI backend (`backend/app/hat/`
holds the whole product), a small React shell (`frontend/`), a PostgreSQL database and a Telegram
bot. Orders reach Fyers only from a HAT signal, through the risk authority. There is no manual
trading. The repo holds nothing else.

**Current state.** Built and running on paper (shadow) against the live database `hat_live`; no
bot trades real money until it passes the gates in `docs/runbook.md` (First deploy and releases).
Open owner steps: `docs/runbook.md` (Pending owner DBA steps). Not built yet and owner-pending
defaults: the last two sections of `docs/decisions.md`.

**Where things are written down** (each fact in one place; link, don't restate):

| Doc | Holds |
| --- | --- |
| `docs/architecture.md` | Module map, data flow, seams, storage, daily cycle, CI, startup, routes, environment |
| `docs/invariants.md` | The trading, money-safety and data rules the code must keep |
| `docs/decisions.md` | Owner decisions in force with their reasons; planned work; pending defaults |
| `docs/runbook.md` | Operator procedures: run, deploy, admin CLI, data tools, alerts, backup |
| `docs/agents/` | The orchestrator guide and the builder protocol |
| `docs/backtests/` | The backtest process (`process.md`: every command, knob and check) and the result standard |
| `README.md` | The public pitch |

---

## Quick Start

```bash
# from the repo root: Python 3.14 and PostgreSQL 18 are required
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
python scripts/install_backend_deps.py
cp .env.example .env              # then fill in DB_* and the Fyers credentials

python backend/start_server.py    # http://127.0.0.1:8000

# frontend, in a second terminal
cd frontend
pnpm install
pnpm dev                          # http://127.0.0.1:3000, proxies /api to the backend
```

Server exits at start: run it in a terminal for the traceback and check port 8000 is free.
"Database unreachable": start PostgreSQL and check `DB_*`. Fyers disconnected: the token lasts
until midnight IST; log in again from the app header. Other helpers in `scripts/`:
`lock_ci_requirements.py`, `export_openapi.py` (for `pnpm gen:api`), and the operator's
`hat_backup.py` and `hat_deploy_guard.py` (runbook).

---

## Docs and Repo Rules

- **Code is the source of truth; docs are a guide, not a mirror of the code.** Keep only the
  canonical docs above, current and compact; delete a plan or spec once implemented. Write what
  is true now, not how it was built: no PR numbers, plan ids or history as instructions, and no
  local machine paths, account ids or secrets.
- **Implement and verify; don't build test ceremony.** Verify a change by running it for real:
  the affected job, CLI or route on real data (`HAT_DATA_ROOT`, the local database read-only);
  never a backtest as PR verification; put the evidence in the PR. Tests are
  small and only for pure functions at their seams (money maths, parsers, state rules). Never
  write tests that run a backtest, the pipeline or a full database on generated data, and no
  deliberate-break rituals or test digests. A conformance check enforces a structural rule of
  the code, never a copy of a doc or a hand-kept list.
- **Trading safety.** Read `.claude/skills/trading-safety/SKILL.md` before touching orders, risk,
  positions, brokers or execution. Never call real Fyers, NSE or Telegram endpoints from tests.
- **Repo paths** come from `backend/app/paths.py`; never compute them from `__file__`. HAT's data
  lives under `HAT_DATA_ROOT` (default `data_system/hat/`, gitignored). The Fyers token path is
  owned by `fyers_tokens.py`.
- **Schema.** `init_db()` runs `Base.metadata.create_all` and HAT's own upkeep
  (`app/hat/schema.py`). There is no Alembic and no legacy table: HAT uses a fresh PostgreSQL
  database, and it refuses to run on any other. Postgres-only behaviour is tested
  with `@pytest.mark.postgres` tests (CI job `backend-postgres`).
- **Import rule.** `app/hat` and `tests/hat` import only the shared modules listed in
  `SHARED` in `tests/hat/conformance/test_conf_decoupling.py` (`app.base`, `app.database`,
  `app.paths`, `app.utils.env_loader`), never `app.main` or a router.
- **Sync, async and local-only.** Blocking work (SDK calls, DB via the sync session, file IO) goes
  in `def` routes, which FastAPI runs in its threadpool, or behind `asyncio.to_thread`. Never
  block in `async def`. SDK callbacks arrive on SDK threads: hand work to the event loop with
  `asyncio.run_coroutine_threadsafe` or `loop.call_soon_threadsafe`. The server binds to
  `BACKEND_HOST` (default `127.0.0.1`) and there is no API auth, by decision. The broker
  login POSTs (Fyers, Zerodha) and the backtest job start are the only writes. Each depends on
  `app/hat/api/guard.py`'s `frontend_write`, which needs the `X-HAT-Request: 1` header and a
  frontend `Origin`, and any new write route must depend on it too. Errors never carry broker or database text.
- **DB sessions.** Use `db_session_scope()` from `app.database` for service-owned DB work and
  `get_db()` for request-scoped FastAPI dependencies.
- **Frontend: no new `useEffect`/`useLayoutEffect`** (`.claude/hooks/no-useeffect.sh` blocks them).

---

## Code Quality

### Frontend
```bash
cd frontend
pnpm check      # format, lint and type check (vp check)
pnpm test       # vp test
pnpm build      # vp build
pnpm gen:api    # regenerate lib/api/schema.d.ts from the backend OpenAPI schema
```

`pnpm gen:api` needs the backend venv active (it runs `python ../scripts/export_openapi.py`; no
server or database needed). Run it after any backend route or response-model change and commit the
result: CI job `api-schema` fails when the committed `lib/api/schema.d.ts` is stale.

### Backend
```bash
# from the repo root (the root ruff.toml covers scripts/)
ruff format --check .
ruff check .

cd backend
python -m pytest tests/hat/conformance -q            # CONF checks, no database needed
CI=1 python -m pytest tests/hat/properties/<file> -q # Hypothesis properties under the CI profile
python -m pytest -n auto -q --ignore=tests/hat/properties --ignore=tests/hat/conformance

# the TLC models, from the repo root
cd ..
verification/tla/run.sh
```

Run only the tests your change touches plus `tests/hat/conformance`; CI runs everything. For
questions spanning many files, write one throwaway command/script that prints only the answer
instead of reading files one by one.

### CI
The jobs: `docs/architecture.md` (Verification and CI). Ruff, `vp check` and the tests stay at
zero errors and zero warnings. A Python file newly mapped by a TLA model's `code:` comments goes
into `tla.yml`'s paths. CI installs the hash-pinned `backend/requirements-ci.lock`; regenerate it
with `python scripts/lock_ci_requirements.py` after changing `backend/requirements*.txt`
(`fyers-apiv3` goes in with `--no-deps`: it pins an `aiohttp` that does not build on 3.14).

---

## Contribution Workflow

Read the invariants and decisions that touch your change; implement and verify on real data; run
the checks above; update the canonical docs only where architecture, an invariant or a decision
changed, and delete any plan or spec the change finishes; submit.

---

## Agent Workflow

The HAT build is run by an orchestrator agent and up to two builder agents in herdr tabs. On a new
machine: run `claude --permission-mode auto` inside herdr, then `/hat-orchestrator`. Orchestrator
guide: `docs/agents/orchestrator.md`. Builder rules: `docs/agents/builder-protocol.md`.
