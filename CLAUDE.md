# SmartTrader - Agent & Development Guide

> Philosophy: build with AI agents as first-class users. Every major capability should be available via UI and API.

---

## Quick Start

### App Startup (recommended)

```bash
# from repo root
start.bat
```

This launches:
- Backend: `http://127.0.0.1:8000`
- Frontend: `http://127.0.0.1:3000` (or next available port)

### Manual Startup

```bash
# Backend (repo-root venv expected)
python -m venv venv
venv\Scripts\activate
python scripts/install_backend_deps.py
python backend/start_server.py

# Frontend
cd frontend
pnpm install
pnpm dev
```

---

## Specs and Repo Rules

- **Hire a Trader (HAT):** the specs in `docs/specs/hat/README.md` are the source of truth for
  `backend/app/hat/`. Where repo code and the specs conflict, the specs win. Every HAT PR names the
  spec requirement IDs it implements and the verification that proves them. New HAT code is
  verified with TLA+ models, Hypothesis properties and Postgres checks
  (`docs/specs/hat/11-verification.md`), not example unit tests; existing tests stay as they are.
- **Repo paths** come from `backend/app/paths.py`; never compute them from `__file__`
  (`backend/tests/test_paths.py`).
- **Startup order** (`main.py` lifespan): symbol_master, then the DB (deferred and retried in the
  background when it is down), then the Fyers token (warn only), then the index universe (warn
  only), then the live connect (`backend/tests/test_startup_sequence.py`, `backend/tests/test_deferred_startup.py`).
- **Schema changes** go through `init_db()`: `Base.metadata.create_all` plus idempotent
  `ensure_*_schema()` functions. There is no Alembic. Tests build the schema on SQLite, so models
  must create there; Postgres-only behaviour is tested with `@pytest.mark.postgres` tests (CI job
  `backend-postgres`).
- **Frontend: no new `useEffect`/`useLayoutEffect`** (`.claude/hooks/no-useeffect.sh` blocks them).

---

## Symbol Management Rules

DB format (`SBIN`, `NIFTY50`) internally; Fyers format (`NSE:SBIN-EQ`) only at the provider
boundary. Always use `SymbolMaster` for conversion (`scripts/check_symbol_format.py` enforces it):

```python
from app.services.symbol_master import symbol_master

symbol_master.to_db("NSE:SBIN-EQ")
symbol_master.to_fyers("SBIN")
```

Do not use ad-hoc string parsing/replacing.

---

## WebSocket Rules

Canonical endpoints:
- `POST /api/websocket/connect`
- `POST /api/websocket/subscribe`
- `POST /api/websocket/disconnect`
- `GET /api/websocket/status`
- `WS /api/websocket/stream`

Runtime behavior:
- Market-hours aware in `live_market_service.py`
- `DEV_MODE=true` bypasses market-hours checks
- Subscriptions are aggregate-delta managed across clients
- Use `GET /api/websocket/status` as the first operational check for provider connection,
  client counts, queue pressure, and local runtime metrics.
- `WS /api/websocket/stream` checks the `Origin` header before `accept()` with the rule CORS
  also uses (`app/utils/origins.py`): `config.ALLOWED_ORIGINS` (`CORS_ORIGINS`) plus any
  `http://localhost`, `http://127.0.0.1` or `http://[::1]` origin on any port, so a frontend
  moved off 3000 still works. Anything else, including `null` and look-alikes such as
  `localhost.evil.com`, is refused (close code 1008 on the stream). A missing `Origin` is allowed:
  browsers always send one, so only non-browser clients (scripts, tests) omit it.

## Sync, Async and Local-Only Rules

- **Sync vs async routes.** Blocking work (SDK calls, DB via the sync session, file IO) goes
  in `def` routes, which FastAPI runs in its threadpool, or behind
  `run_in_threadpool`/`asyncio.to_thread`. Never block in `async def`.
- **SDK callbacks** arrive on SDK threads. Hand work to the event loop with
  `asyncio.run_coroutine_threadsafe` or `loop.call_soon_threadsafe`.
- **Single-user localhost.** There is no API auth, by decision. The server binds to
  `BACKEND_HOST` (default `127.0.0.1`). The trading safety
  rules still apply: idempotency, risk checks, fail closed, PAPER never touches the broker,
  and no broker or DB error text in responses.

## Backbone Service Conventions

### DB sessions
- For service-owned non-request DB work, prefer `db_session_scope()` from `app.database`
  over ad hoc open/close handling.
- Keep request-scoped FastAPI dependencies on `get_db()`.

### Runtime metrics
- Add lightweight in-memory runtime metrics for real hot paths before introducing any
  heavier observability stack.
- Current approved targets:
  - websocket connection and queue health
  - canonical history load timings
- Do not add speculative telemetry that does not help current local operations or near-term development.

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

`pnpm gen:api` needs the backend venv active (it runs `python ../scripts/export_openapi.py`; no server
or database needed). Run it after any backend route or response-model change and commit the result:
CI job `api-schema` fails when the committed `lib/api/schema.d.ts` is stale.

### Backend
```bash
# from the repo root (root ruff.toml covers start_dev.py, scripts/, data_platform/)
ruff format --check .
ruff check .

cd backend
python -m pytest   # test deps: pip install -r requirements-dev.txt; -m puts backend/ on sys.path
python -m pytest -n auto   # pytest-xdist, as CI does; add --cov=app --cov-report=term-missing:skip-covered for coverage (CI skips it)
```

Trading test fixtures (`backend/tests/conftest.py`):
- `fake_broker`: a `FakeBroker` (`backend/tests/fakes/broker.py`) behind every broker handle the app
  holds. It records each call and is configured by attributes (`ltp`, `positions`, `working`,
  `place_reply`, `circuit_open`, ...) or per call with `script(method, *outcomes)`. Do not write a new
  fake broker in a test file; extend this one.
- `freeze_market`: the one way to pin market hours. `freeze_market(True)` (open), `False` (a
  Saturday) or an IST `datetime`; it freezes the risk manager, `live_market` (plus any
  `LiveMarketService` passed after the instant) and the `market_hours` helpers on a `FixedClock`.
  Do not monkeypatch `is_market_open` / `is_market_hours` or `datetime` instead.

### CI
`.github/workflows/ci.yml` runs both blocks above on every pull request and on pushes to main.
Ruff, `vp check` and the tests are expected to stay at zero errors and zero warnings.

---

## Troubleshooting

### Backend window closes immediately
1. Run in terminal for traceback:
```bash
.\venv\Scripts\python.exe backend\start_server.py
```
2. Ensure dependencies installed:
```bash
python scripts/install_backend_deps.py
```
3. Confirm port 8000 availability:
```bash
netstat -ano | findstr :8000
```

### WebSocket behavior post-market
- Check `DEV_MODE` env value.
- Default startup scripts now use `DEV_MODE=false`.

### Python 3.14 dependency note
- `fyers-apiv3` pins an old `aiohttp` build path that can fail on Windows/Python 3.14.
- Use `python scripts/install_backend_deps.py` to install a working set.

---

## Contribution Workflow

1. Plan changes
2. Implement with tests
3. Run quality checks
4. Update `docs/specs/hat/` for any HAT behavior/interface change
5. Submit

---

**Last Updated:** October 20, 2026
