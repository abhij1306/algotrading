# Hire a Trader (HAT)

A rules-based NSE swing-trading system for one owner. Two bots (S1
`stage2-contraction-breakout`, S2 `momentum-breakout-ep`) each run a daily cycle: after the
close they ingest the day's data, gate its quality, read the regime, size their sleeves and
scan for candidates; the next session they enter on triggers and manage stops and exits. Orders
go to Fyers only from a bot's signal, through the risk authority. There is no manual trading.

How a bot earns real money: a backtest of at least five years, then daily paper trading
(`shadow` mode: virtual orders, never at the broker), then `confirm` mode, where the owner
approves every entry on Telegram. `auto` mode places only high-confidence signals without a
card. It is a personal switch the owner flips per bot, and a bot stays in shadow until its
account, DDPI and policy gates pass. See `docs/decisions.md`.

The backend is FastAPI on PostgreSQL, the frontend a small read-only React dashboard, and the
owner's controls are Telegram commands and a local admin command line.

---

## Run it

Python 3.14, PostgreSQL 18, Node 26 and pnpm are required.

```bash
# backend, from the repo root
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
python scripts/install_backend_deps.py
cp .env.example .env              # fill in DB_* and the Fyers credentials
python backend/start_server.py    # http://127.0.0.1:8000

# frontend, in a second terminal
cd frontend
pnpm install
pnpm dev                          # http://127.0.0.1:3000, proxies /api to the backend
```

- **Database.** Point `DB_*` at a fresh PostgreSQL database. The schema is created at startup;
  there is no migration tool, and HAT refuses any other database engine.
- **Data.** Market data lives under `HAT_DATA_ROOT` (default `data_system/hat/`, gitignored).
- **HAT itself.** The scheduler, live feed and Telegram bot start only with `HAT_ENABLED=true`
  (default false); without it the server serves the login and dashboard routes only.
  `.env.example` lists every variable.

### Building the history

Once per data root, from `backend/` with the venv active:

```bash
python -m app.hat.data.history_cli [--from 2011-06-22] [--to YYYY-MM-DD] [--resume]
python -m app.hat.data.lineage build      # link ISIN changes into one history per company
python -m app.hat.data.features --full    # compute the scan features for every date
```

After that the nightly cycle keeps them current. `lineage report` and `lineage show SYMBOL`
list what needs review. The first-deploy steps (policy, broker account, bots, universe,
versions) are in `docs/runbook.md`.

---

## Docs

- `docs/architecture.md`: module map, data flow and seams
- `docs/invariants.md`: the money-safety and trading rules the code keeps
- `docs/decisions.md`: the decisions in force
- `docs/runbook.md`: operator procedures (start, login, first deploy, failures)
- `CLAUDE.md`: development guide: routes, environment, checks, repo rules

---

## Checks

```bash
# from the repo root
ruff format --check .
ruff check .

# frontend
cd frontend
pnpm check
pnpm test
```

`CLAUDE.md` lists the backend test commands and the CI jobs.

---

## License

See `LICENSE`.
