# Hire a Trader (HAT) - Agent & Development Guide

Hire a Trader is a rules-based NSE swing-trading system: a FastAPI backend (`backend/app/hat/`
holds the whole product), a small React shell (`frontend/`), a PostgreSQL database and a Telegram
bot. Orders reach Fyers only from a HAT signal, through the risk authority. There is no manual
trading. The repo holds nothing else: the old SmartTrader code is deleted.

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

Startup sequence, routes and environment variables: `docs/architecture.md` (Startup and
routes). Server won't start, database unreachable, Fyers disconnected: `docs/runbook.md` (Local
development).

---

## Docs and Repo Rules

- **Code is the source of truth; docs are a guide, not something maintained one-to-one with the
  code. Once a plan or spec is implemented, delete it. Only the canonical docs are kept up to
  date -- architecture, invariants, CLAUDE.md, decisions -- and they stay compact.** The canonical
  docs: `docs/architecture.md` (module map, data flow, seams, startup, routes, environment),
  `docs/invariants.md` (the money-safety and trading rules), `docs/decisions.md` (decisions in
  force), `docs/runbook.md` (operator procedures).
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
  `BACKEND_HOST` (default `127.0.0.1`) and there is no API auth, by decision. The two
  Fyers login POSTs are the only writes. Each depends on `app/hat/api/guard.py`'s
  `frontend_write`, which needs the `X-HAT-Request: 1` header and a frontend `Origin`, and any new
  write route must depend on it too. Errors never carry broker or database text.
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
The jobs: `docs/architecture.md` (Verification layout). Ruff, `vp check` and the tests stay at
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
