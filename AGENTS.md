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

## Current Canonical Docs

- Architecture + data architecture: `docs/ARCHITECTURE.md`
- WebSocket: `docs/Websocket.md`
- Design system: `docs/DESIGN_SYSTEM.md`
- Symbol management: `docs/SYMBOL_MANAGEMENT.md`
- Database/data operations: `docs/DATABASE_MANAGEMENT_PLAYBOOK.md`

Compatibility pointer docs exist but are deprecated:
- `docs/DATA_ARCHITECTURE.md`
- `docs/TYPOGRAPHY_IMPLEMENTATION.md`
- `docs/SPACING_RADIUS_SHADOW_TOKENS.md`
- `docs/SYMBOL_FORMAT_RULES.md`

---

## Project Overview

SmartTrader is a quantitative trading platform for NSE with:
- Dashboard, Screener, Terminal, Backtest
- Live market streaming via WebSocket/Fyers
- Paper + live trading paths
- Phase-1 data system for 2025+ snapshots

---

## Architecture (high level)

### Backend
- FastAPI + SQLAlchemy
- Core services:
  - `symbol_master.py`
  - `index_universe_loader.py`
  - `live_market_service.py`
- Routers under `backend/app/routers`

### Frontend
- Vite+ (`vp`) + React Router SPA + TypeScript + Tailwind, managed with pnpm
- Routes are declared in `frontend/router.tsx`; pages live in `frontend/app/**/page.tsx`
- Realtime quotes: `frontend/lib/realtime` (`useQuote`, `useQuotes`, `useConnectionStatus`)

### Data
- Canonical active root: `data_system/`
- Phase-1 active scope: 2025 onward
- Legacy/historical assets archived under `archive/`

---

## Symbol Management Rules

Always use `SymbolMaster` for conversion:

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
  - strategy scan/backtest timings
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
pytest        # test deps: pip install -r requirements-dev.txt
pytest --cov=app --cov-report=term-missing:skip-covered   # coverage is opt-in; CI passes these
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
4. Update canonical docs for any behavior/interface change
5. Submit

---

**Last Updated:** February 19, 2026
