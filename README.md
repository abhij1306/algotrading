# Hire a Trader (HAT)

A rules-based NSE swing-trading system. Bots score the market after the close, pick candidates,
size them under a risk policy and trade through Fyers in shadow, confirm (Telegram approval) or
auto mode. The backend is FastAPI on PostgreSQL; the frontend is a small React shell.

---

## Quick Start

Python 3.14, PostgreSQL 18, Node 26 and pnpm are required.

```bash
# backend (repo root)
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
python scripts/install_backend_deps.py
cp .env.example .env              # fill in DB_* and the Fyers credentials
python backend/start_server.py    # http://127.0.0.1:8000

# frontend
cd frontend
pnpm install
pnpm dev                          # http://127.0.0.1:3000
```

The schema is created at startup in a fresh PostgreSQL database; there is no migration tool and
no legacy data. HAT's scheduler and live feed start only when `HAT_ENABLED=true` (default false).

---

## Documentation

- `CLAUDE.md`: development guide, startup, routes, environment and the rules the code enforces
- `docs/architecture.md`, `docs/invariants.md`, `docs/decisions.md`: module map and data flow,
  the money-safety rules, the decisions in force
- `docs/runbook.md`: operator procedures
- `.claude/skills/`: how to run and read each area's verification (`verify-*`) and the trading
  safety rules

---

## API

- `GET /` and `GET /ping`: liveness
- The daily Fyers login under `/api/hat/auth/fyers`: `GET /status`, `GET /url`, `POST /token`,
  `POST /disconnect`

The `/api/hat/*` read routes and the dashboard come with later PRs. The OpenAPI types for the
frontend are generated (`pnpm gen:api` in `frontend/`).

---

## Quality Checks

```bash
# from the repo root
ruff format --check .
ruff check .

# frontend
cd frontend
pnpm check
pnpm test
pnpm build
```

Backend tests are verification suites (Hypothesis properties, TLA+ models, Postgres and
conformance checks); see `CLAUDE.md` for the commands and `docs/invariants.md` for the
index of checks.

### SonarQube
Pass the SonarQube URL at scan time instead of hardcoding it in `sonar-project.properties`.

```bash
sonar-scanner -Dsonar.host.url=http://localhost:9000
```

---

## License

See `LICENSE`.
