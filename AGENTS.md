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
scripts\setup\install_backend_deps.bat
python backend/start_server.py

# Frontend
cd frontend
npm install
npm run dev
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
- Next.js App Router + TypeScript + Tailwind
- Realtime hook: `frontend/hooks/useWebSocket.ts`

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

---

## Code Quality

### Frontend
```bash
cd frontend
npm run lint
npm run type-check
npm run build
```

### Backend
```bash
cd backend
ruff check .
pytest
```

---

## Troubleshooting

### Backend window closes immediately
1. Run in terminal for traceback:
```bash
.\venv\Scripts\python.exe backend\start_server.py
```
2. Ensure dependencies installed:
```bash
scripts\setup\install_backend_deps.bat
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
- Use `scripts\setup\install_backend_deps.bat` (or `.ps1`) to install a working set.

---

## Contribution Workflow

1. Plan changes
2. Implement with tests
3. Run quality checks
4. Update canonical docs for any behavior/interface change
5. Submit

---

**Last Updated:** February 19, 2026
