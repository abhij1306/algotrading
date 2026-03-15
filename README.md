# SmartTrader

AI-assisted quantitative trading platform for NSE with real-time market data, screening, terminal workflows, and Phase-1 historical snapshot data.

---

## Quick Start

### One-command startup (Windows)

```bash
start.bat
```

This starts backend + frontend and opens the app URL.

### Manual startup

```bash
# backend (repo root)
python -m venv venv
venv\Scripts\activate
scripts\setup\install_backend_deps.bat
python backend/start_server.py

# frontend
cd frontend
npm install
npm run dev
```

---

## Canonical Documentation

- `docs/ARCHITECTURE.md` (system + data architecture)
- `docs/Websocket.md` (realtime behavior and endpoints)
- `docs/DESIGN_SYSTEM.md` (single design-system source)
- `docs/SYMBOL_MANAGEMENT.md` (symbol conversion and boundary rules)
- `docs/DATABASE_MANAGEMENT_PLAYBOOK.md` (Phase-1 data operations)

---

## Core Modules

- Dashboard
- Screener
- Terminal
- Backtest

---

## Data System

Active canonical data root:
- `data_system/`

Phase-1 active scope:
- 2025 onward only for runtime/build pipeline

Canonical pipeline:
- `python -m data_platform.pipelines.phase1_build --asof YYYY-MM-DD --mode full|incremental`

---

## API Highlights

- Screener: `/api/screener/results`, `/api/screener/indices`
- WebSocket: `/api/websocket/connect`, `/api/websocket/subscribe`, `/api/websocket/disconnect`, `/api/websocket/status`, `WS /api/websocket/stream`
- Terminal: `/api/terminal/chart`, `/api/terminal/paper/order`
- Data snapshots: `/api/data/snapshot/stock`, `/api/data/snapshot/universe`, `/api/data/snapshot/status`

---

## Quality Checks

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

### SonarQube
Pass the SonarQube URL at scan time instead of hardcoding it in `sonar-project.properties`.

```bash
sonar-scanner -Dsonar.host.url=http://localhost:9000
```

For local-only defaults, keep a gitignored override such as `sonar-project.properties.local` or inject the URL in CI pipeline variables.

---

## Troubleshooting

### Backend exits immediately
Run foreground to see exact error:

```bash
.\venv\Scripts\python.exe backend\start_server.py
```

Install missing dependencies:

```bash
scripts\setup\install_backend_deps.bat
```

Python 3.14 note:
- Fyers package metadata pins an old `aiohttp` build path on Windows.
- Use `scripts\setup\install_backend_deps.bat` (or `.ps1`) to install a stable set.

### WebSocket connects post-market
- Verify `DEV_MODE` is not set to `true`.
- Default startup now uses `DEV_MODE=false`.

---

## License

See `LICENSE`.
