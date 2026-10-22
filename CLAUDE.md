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

`BACKEND_HOST`, `BACKEND_PORT`, `BACKEND_RELOAD`, `BACKEND_ACCESS_LOG` and `BACKEND_LOG_LEVEL`
configure `backend/start_server.py`. The frontend reads `BACKEND_URL` and `PORT`.

### Startup (`backend/app/main.py` lifespan)

1. Get the event loop.
2. Create the database schema (`init_db`). If the database is down, one log line says so and the
   app keeps serving (requests answer 503) unless `HAT_ENABLED` is true, which refuses to start.
3. Only when `HAT_ENABLED` is true: check the Fyers token (warn only), then start the HAT runtime
   (`app/hat/daily/runtime.py`, production parts in `app/hat/daily/production.py`). It refuses a
   database that is not PostgreSQL and a bad Telegram configuration, reconciles every broker
   account before anything is placed, then starts the scheduler with the daily cycle
   (`daily_jobs`, the 10-ops §3 job table), the live feed (`app/hat/data/feed.py`, the one Fyers
   data socket, built in `main.py`), the periodic loop (session watch, fill sync, approval expiry,
   reconciliation, live exits, announcements) and the Telegram poller and relay.

With `HAT_ENABLED` unset or false (the default) no HAT task runs: the app serves only the routes
below. Shutdown stops the HAT runtime (tasks, scheduler, feed, broker sessions), then the database
engine, each even if the other failed.

### Routes

- `GET /` and `GET /ping`: liveness.
- The daily Fyers login, `app/hat/api/fyers_auth.py`, under `/api/hat/auth/fyers`:
  `GET /status`, `GET /url` (the Fyers login link), `POST /token` (trade the auth code for a
  token and save it) and `POST /disconnect`. Fyers redirects to the registered `REDIRECT_URI`;
  the backend serves no callback, so paste the `auth_code` from the redirected URL into the login
  dialog in the app header. The token file is owned by `app/hat/execution/fyers_tokens.py`.
- The read-only dashboard, `app/hat/api/dashboard.py`, under `/api/hat` (GET only, HAT-DEC-T3;
  kill, re-arm, mode and flatten are Telegram commands): `/status`, `/bots`, `/watchlist`,
  `/signals`, `/positions`, `/allocations`, `/regime` and `/events` (`signals`, `regime` and
  `events` take `limit`, 1..200, default 50). Money and weights are decimal strings. The
  frontend page `frontend/app/hat/page.tsx` polls them every 15 s.

---

## Specs and Repo Rules

- **The specs are the source of truth** for `backend/app/hat/`: `docs/specs/hat/README.md`. Where
  repo code and the specs conflict, the specs win. Every HAT PR names the requirement IDs it
  implements. A change to HAT behaviour changes the spec first, in the same PR.
- **Verification, not unit tests.** HAT code is proved with TLA+ models (`verification/tla/`),
  Hypothesis properties (`backend/tests/hat/properties/`), Postgres checks
  (`backend/tests/hat/db/`) and CONF checks (`backend/tests/hat/conformance/`), as
  `docs/specs/hat/11-verification.md` describes. Do not write example-based unit tests for HAT
  code; show each new check failing on a deliberate break. The `verify-*` skills in
  `.claude/skills/` say how to run, read and update each area, and record a digest of the files
  they cover: after changing a covered file, re-confirm the area and refresh the digest with
  `python backend/tests/hat/conformance/hat_conf_skills.py <skill>`.
- **Trading safety.** Read `.claude/skills/trading-safety/SKILL.md` before touching orders, risk,
  positions, brokers or execution. Never call real Fyers, NSE or Telegram endpoints from tests.
- **Repo paths** come from `backend/app/paths.py`; never compute them from `__file__`. HAT's data
  lives under `HAT_DATA_ROOT` (default `data_system/hat/`, gitignored). The Fyers token path is
  owned by `fyers_tokens.py`.
- **Schema.** `init_db()` runs `Base.metadata.create_all` and HAT's own upkeep
  (`app/hat/schema.py`). There is no Alembic and no legacy table: HAT uses a fresh PostgreSQL
  database, and it refuses to run on any other (HAT-SCH-105). Postgres-only behaviour is tested
  with `@pytest.mark.postgres` tests (CI job `backend-postgres`).
- **Import rule.** `app/hat` and `tests/hat` import only the shared modules listed in
  `SHARED` in `tests/hat/conformance/test_conf_decoupling.py` (`app.base`, `app.database`,
  `app.paths`, `app.utils.env_loader`), never `app.main` or a router.
- **Sync, async and local-only.** Blocking work (SDK calls, DB via the sync session, file IO) goes
  in `def` routes, which FastAPI runs in its threadpool, or behind `asyncio.to_thread`. Never
  block in `async def`. SDK callbacks arrive on SDK threads: hand work to the event loop with
  `asyncio.run_coroutine_threadsafe` or `loop.call_soon_threadsafe`. The server binds to
  `BACKEND_HOST` (default `127.0.0.1`) and there is no API auth, by decision. Errors never carry
  broker or database text.
- **DB sessions.** Use `db_session_scope()` from `app.database` for service-owned DB work and
  `get_db()` for request-scoped FastAPI dependencies.
- **Frontend: no new `useEffect`/`useLayoutEffect`** (`.claude/hooks/no-useeffect.sh` blocks them).

---

## Environment

`.env.example` lists every variable. Read from the environment (loaded from `.env`):

- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`: PostgreSQL.
- `CLIENT_ID`, `SECRET_KEY`, `REDIRECT_URI`: the Fyers app (`FYERS_APP_ID`, `FYERS_CLIENT_ID` and
  `FYERS_SECRET_KEY` also work).
- `HAT_ENABLED` (default false), `HAT_USER_ID`, `HAT_DATA_ROOT`, `HAT_MCP_BHAVCOPY_URL`,
  `HAT_MCP_CMMARKET_URL`, and the Telegram secrets `HAT_TELEGRAM_TOKEN`, `HAT_TELEGRAM_CHAT_ID`,
  `HAT_APPROVAL_SECRET`.
- `CORS_ORIGINS`, `LOG_LEVEL`, `FRAMEWORK_LOG_LEVEL`.

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
result: CI job `api-schema` fails when the committed `lib/api/schema.d.ts` is stale. The
`sync-api-types` skill does the same and reports contract changes.

### Backend
```bash
# from the repo root (the root ruff.toml covers scripts/)
ruff format --check .
ruff check .

cd backend
python -m pytest tests/hat/conformance -q            # CONF checks, no database needed
CI=1 python -m pytest tests/hat/properties/<file> -q # Hypothesis properties under the CI profile
python -m pytest -n auto --ignore=tests/hat/properties --ignore=tests/hat/conformance

# the TLC models, from the repo root
cd ..
verification/tla/run.sh
```

Run only the tests your change touches plus `tests/hat/conformance`; CI runs everything.
`python backend/tests/hat/conformance/hat_conf_skills.py --stale` lists `verify-*` skills whose
covered files changed; `--refresh` rewrites their digests.

### CI
`.github/workflows/ci.yml` runs on every pull request and on pushes to main: `backend` (ruff and
the app tests), `backend-postgres`, `verification-tla`, `verification-conformance`,
`verification-properties` (four shards, each property file once), `frontend` and `api-schema`.
Ruff, `vp check` and the tests stay at zero errors and zero warnings. CI installs the hash-pinned
`backend/requirements-ci.lock`; regenerate it with `python scripts/lock_ci_requirements.py` after
changing `backend/requirements*.txt`. `fyers-apiv3` is installed with `--no-deps` because it pins
an `aiohttp` that does not build on Python 3.14.

---

## Troubleshooting

- **The server exits at start:** run `python backend/start_server.py` in a terminal for the
  traceback; install dependencies with `python scripts/install_backend_deps.py`; check that port
  8000 is free.
- **"Database unreachable" in the log:** start PostgreSQL and check the `DB_*` variables. With
  `HAT_ENABLED=true` the server will not start until the database answers.
- **Fyers shows disconnected:** the token lasts until midnight IST. Log in again from the app
  header.

---

## Contribution Workflow

1. Plan changes, starting from the spec.
2. Implement with checks (properties, models, DB and CONF checks).
3. Run the quality checks above.
4. Update `docs/specs/hat/` for any HAT behaviour or interface change.
5. Submit.

---

## Agent Workflow

The HAT MVP is built by an orchestrator agent and up to two builder agents in herdr tabs. On a new
machine: run `claude --permission-mode auto` inside herdr, then `/hat-orchestrator`. Orchestrator guide:
`docs/agents/orchestrator.md`. Builder rules: `docs/agents/builder-protocol.md`. Roadmap and
status: `docs/agents/plan.md`.
