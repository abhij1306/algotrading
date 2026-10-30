# Runbook

Operator procedures. Rules: `invariants.md`; job times: `architecture.md` (Daily cycle).

Conventions: commands run from the repo root on the host as the service user. `$COMPOSE` is
`docker compose --env-file .env -f deploy/hat/docker-compose.yml`; `$ADMIN` is `$COMPOSE exec -w
/app/backend backend python -m app.hat.admin`; `python -m app.hat.data.*` tools run from
`backend/` (in the stack via `$COMPOSE exec -w /app/backend backend`). Database passwords come
from `PGPASSWORD` or `~/.pgpass`. "The window" is 08:30–15:35 IST on a trading day: deploys, DBA
and admin steps run outside it. Commands using `$DB_USER`/`$DB_NAME` need the `.env` variables
in the shell (`set -a; . ./.env; set +a`). The SQL below on the state tables (`hat_job_run`,
`hat_event`, orders, positions) runs in `"$DB_STATE_NAME"` (hat_state), on the stock data in
`"$DB_NAME"` (hat_live). Record every check and incident in the check log.

| OPS check | When |
| --- | --- |
| `ops.host_checklist` | Before enabling confirm or auto; after any host change |
| `ops.env_permissions` | After any `.env` change |
| `ops.deploy_window` | Every deploy |
| `ops.failover_drill` | Quarterly |
| `ops.backup_restore_drill` | Monthly |
| `gapscan.latency_observed` | Daily for the first 2 weeks of live data |

## Pending owner DBA steps

Not yet run on hat_live. As a superuser, outside the window, after a `pg_dump`:

1. Role limits, then restart the backend:
   ```sql
   ALTER ROLE <db_user> SET idle_in_transaction_session_timeout = '5min';
   ALTER ROLE <db_user> SET lock_timeout = '60s';
   ```
2. `VACUUM FULL hat_instrument;` (9 MB heap for 0.5 MB of rows; locks the table about a second).
3. Once the owner calls the ISIN merge final (no `python -m app.hat.data.isin_merge down` needed):
   `DROP TABLE hat_isin_merge_row, hat_isin_merge;` and remove both from `DRIFT_ALLOWED_TABLES`
   in `app/hat/schema.py`. These are the last tables HAT allows in hat_live beyond its models
   (the pre-split tables were dropped on 2026-10-26).
4. After the next Saturday `reference.weekly`: `select symbol from hat_instrument where symbol in
   ('AZADIND','BONFIG','SMT','TONBO','ZETWERK');` returns all five.
5. Install the nightly backup timer and run the first restore drill (Backup and restore).
6. Date the stored demergers' record dates, unless already done: restart the backend (creates
   `hat_demerger_notice`), then from the directory holding the reviewed `ca8-announced.tsv`
   (columns `isin ex_date announced_on source_url note`; kept outside the repo), with
   `PYTHONPATH=<repo>/backend`: `python -m app.hat.data.demerger_notice announced
   ca8-announced.tsv`, then the same with `--apply` (fills only NULL `announced_on`).
7. Take TRUNCATE on the event log away from the backend's role. The `hat_event_no_truncate`
   triggers (on `hat_event` and each partition, made at start) refuse a TRUNCATE, but a trigger
   stops neither DROP nor DETACH PARTITION, and a superuser bypasses both triggers and grants: on
   hat_state the tables belong to `postgres`, so first have the backend connect as a role that is
   not a superuser and does not own them. Then, for `hat_event` and each `hat_event_pYYYYMM`:
   ```sql
   REVOKE TRUNCATE ON hat_event FROM PUBLIC, <db_user>;
   REVOKE TRUNCATE ON hat_event_p202610 FROM PUBLIC, <db_user>;  -- and so on
   ```
   Retention never truncates or drops event partitions (`ops/retention.py` deletes only the two
   chat tables).
8. Point-in-time membership: restart the backend (creates `hat_index_member` and
   `hat_index_membership`), copy the NIFTY 500 history CSV into `<HAT_DATA_ROOT>/admin/` and load
   it (Index membership), then republish the universe (`publish-universe`, step 3 of First deploy)
   so its live version names its index: until then a backtest of it is refused
   `backtest_universe_no_index`. Runs made before this, over today's list, are no longer any
   sleeve's evidence: backtest each sleeve again (First deploy step 4) before its next
   promotion.

## Running it

### Start, stop, deploy: `ops.deploy_window`

- Start or deploy: `git pull` (for a release), then `python scripts/hat_deploy_guard.py &&
  $COMPOSE up -d --build`, then "Restart recovery". The guard exits 2 inside the window; it treats
  every weekday as a trading day unless given `--calendar` (export: `$COMPOSE exec -T postgres
  psql -U "$DB_USER" -d "$DB_NAME" -At -F, -c "select date, is_trading from hat_trading_calendar"
  > /tmp/hat_calendar.csv`). Emergency: `--force --reason "<why>"` (logged to
  `<HAT_DATA_ROOT>/ops/deploy_guard.jsonl`). A release adding columns to `hat_allocation_daily`
  goes out between the close and the 18:00 `allocate`.
- Check: `$COMPOSE ps` healthy, `curl -fsS http://127.0.0.1:8000/ping`, Telegram `/status`.
- Stop: `python scripts/hat_deploy_guard.py && $COMPOSE stop backend` (then `postgres`). Jobs
  still running after 25 s are logged `abandoned`: check them after the next start.
- Logs: `$COMPOSE logs -f backend`. Never `$COMPOSE down -v` (deletes the volumes).
- `HatConfigError: fyers_proxy_set: <var>`: that proxy would carry Fyers traffic; unset it or add
  `fyers.in` to `NO_PROXY`/`no_proxy` (`kite_proxy_set`: the same for `kite.trade`).
- `HatConfigError: kite_credentials_missing`: `HAT_BROKER=zerodha` needs `KITE_API_KEY`,
  `KITE_API_SECRET` and `KITE_REDIRECT_URI` in `.env`; `HAT_BROKER is not a known broker`: use
  `fyers` or `zerodha`.
- `RuntimeLockHeldError`: another `HAT_ENABLED=true` process uses the database. Find it with
  `select pid, client_addr, backend_start from pg_stat_activity where application_name =
  'hat_runtime_lock';` and stop it; `pg_terminate_backend(<pid>)` only if the process is gone. A
  running backend that loses the lock alerts `runtime.lock_lost` and opens no entry until
  restarted; when another runtime took it (the alert's `halted`), it also stops its jobs and
  sends no stop or sale: stop that backend.
- `[ERROR] Schema drift at <db>`: run the listed DDL as the owner (a `--` line is an object the
  models lack: drop it or model it), then start again.

### Restart recovery

On start, interrupted runs are reaped and jobs resume from `hat_job_run`. Every account is
reconciled before any entry; unplaced entries go out once the feed is fresh, on their own trade
date only. After 08:45 the stored 5-minute bars are restored (`live.restore`; on failure URGENT
`job.failed` and fewer triggers that day). Check `/status` and `/positions` (every real position
has its stop).

### Morning login

08:30 IST Telegram sends the Fyers link (`login.prompt`). Log in, then paste the `auth_code` from
the redirect URL into the app header's dialog (or `POST /api/hat/auth/fyers/token`, header
`X-HAT-Request: 1`, body `{"auth_code": "<code>"}`). Another Fyers user's token is refused. No
entries until the session is live; `/login` gives a fresh link. On a host whose desktop is only
seen remotely, `python scripts/fyers_login.py` opens the login in a browser on `DISPLAY`, catches
the redirect and exchanges the code through the same flow (it prints only whether it connected).

With `HAT_BROKER=zerodha` the prompt also carries the Kite link (for the Zerodha account): log in
and paste the `request_token` from the redirect URL into the header's Zerodha dialog (`POST
/api/hat/auth/zerodha/token`, same header, body `{"auth_code": "<request_token>"}`). Kite's token
lasts until 06:00 IST. Fyers is still logged in every day: it carries the market data.

Missed: ~08:32 `broker.session_lost`, 09:05 a second prompt (URGENT with real positions). Bots
stay manage-only and only broker-side stops protect positions (no ticks, trims, MA exits or GTT
changes). Missed triggers are not replayed.

Secrets go into `.env` through `python scripts/set_secrets.py [--env PATH]`: a one-shot form on
`127.0.0.1:8765` (open it on the host's desktop) that writes the filled keys with mode 600 and
exits; values are typed at runtime, never printed or logged.

## Admin command line

`$ADMIN --help` and `app/hat/admin.py` list every command. Each is one transaction printing JSON;
a refusal prints `refused: <code>`, exits 2 and writes nothing. Input files are names inside
`<HAT_DATA_ROOT>/admin/`. `$ADMIN show` lists strategies, versions and sleeves with ids.
`$ADMIN books [--date YYYY-MM-DD]` prints each bot's live and paper book through that session
(read-only; Live and paper books).
Strategies, versions, the risk policy versions, broker accounts and sleeves are config files
(`<HAT_DATA_ROOT>/admin/config/<kind>/<id>/<rev>.json`, the revision in force in
`<kind>/in_force.json`, every earlier pointer in `<kind>/pointers/`); the commands are the only
way to change them, never an editor: a hand-edited file fails its load. Two changes of one record
at once: the later commit is refused `config_conflict`; run it again. Only the data commands
(`admin.DATA_COMMANDS`) write hat_live.

### First deploy and releases

1. Database up, history and features built, calendar loaded, `HAT_TELEGRAM_CHAT_ID` set.
2. `seed-policy`; "Morning login"; `onboard-account --account-ref <Fyers client id>` (a `failed`
   account takes no sleeve); `seed-strategies`; `seed-bots --account <id>`. Then start HAT with
   `HAT_ENABLED=true`.
   Until the first real order, `set-policy --file <name>` (a YAML file in `HAT_DATA_ROOT/admin/`,
   written from `show-policy`) replaces the policy at once (decisions: Pre-trading policy reset).
3. `publish-universe --constituents <ind_nifty500list.csv> --as-of <date> [--index I]
   [--include S] [--exclude S]`, and load the index's membership (Index membership). Rows whose
   ISIN is not an ordinary share (a REIT, a placeholder such as
   `DUMMYHEG`) are skipped and listed under `not_stocks`; an `--exclude` drops its row before
   symbols resolve. Any other unknown symbol or ISIN is listed and nothing is written.
4. Backtest each sleeve: `$COMPOSE exec -w /app/backend backend python -m app.hat.backtest --bot
   <bot> --version <v> --from 2016-01-01 --to <last trading day> --for-promotion --regime-mult
   policy`; then `publish-version --version <v> --run <run_id>` (the run folder's id; the run must
   be complete, flagged for promotion and its `audit.json` pass or warn, else `run_audit_not_passed`;
   the version is `frozen`) and `assign-version --bot <bot> --version <v>`.
5. `promote --bot <bot> --to backtested` (`backtest_gate`), `promote --to shadow` and
   `trial-version --version <v>` the same evening. Versions move draft -> frozen -> trial ->
   released -> superseded; sleeves move research -> backtested -> shadow -> live (`promote`).
6. After 20 sessions the shadow sleeve ran the version (each a published watchlist naming it; the
   calendar alone is not enough): `release-version --version <v>`, then `promote --bot <bot> --to
   live`.
   Orders stay virtual until GTT delivery, DDPI and `weights_confirmed`; `/status` shows the gate.
   DDPI active: `confirm-ddpi --account <a> --since <date> --note "<what>"`; withdrawn:
   `revoke-ddpi --account <a> --note "<why>"`. `/mode <bot> auto` for auto.
7. New version: `save-draft --strategy <s> --config <file.yaml>`, step 4's backtest and
   `publish-version`. A Live or Probation sleeve takes only a released version, so the trial runs
   on a paper sleeve: `add-sleeve`, `assign-version` of the frozen version, step 5's `promote`s to
   shadow and `trial-version`. After 20 sessions it ran the version, `release-version` (supersedes
   the old version and moves the Live sleeve onto it; open trades keep theirs). One Live sleeve per
   strategy.
8. Recovered Probation sleeve: `promote --bot <bot> --to live`.

### Research backtest

A research run needs no bot, broker account or Fyers login, only a strategy version, a published
universe and the risk policy (`seed-policy`, `seed-strategies`, step 3's `publish-universe`). Every
command (data check, run, sweep, rerun, compare, review request), knob and audit is in
`docs/backtests/process.md`; this section holds only the operator facts around it.

`--universe` takes the universe's id or name. The universe is its index's members as of each
session, through lineage (Index membership). Backtests run from 2016-01-01 onward until the
2011-2015 membership is resolved (decisions.md "Backtests from 2016-01-01"): a `--from` before the
loaded history's first day is refused `backtest_starts_before_membership@2016-01-01`, a `--to`
after its last day `backtest_ends_after_membership@<day>`, and a universe whose index has no
history loaded `backtest_no_index_membership`. Bars before 2016 still warm the indicators up. A
sleeve the policy gives base weight 0 sizes at 0.5 in a research run (recorded in the run's
conventions); a `--for-promotion` run of it is refused `backtest_no_base_weight`, and takes no
overrides. A version that enables `volume_pace` or `daily_close` cannot be promoted on a run
(known gaps). A sweep prints one JSON line per variant as each finishes. The trade ledger's `qty`
is in today's split-adjusted shares; `qty_real` is the whole real shares on the entry day. Admin
commands gated outside market hours run at weekends: an ordinary Saturday or Sunday inside the
stored calendar reads closed. A data audit that fails
refuses every run on that data (`backtest_data_audit_failed`): read its file under
`HAT_DATA_ROOT/backtests/data_audits/` (the data check prints the path), fix the data, and run
`python -m app.hat.data.audit ... --refresh`.

The app's Backtests page (`/hat/backtests`) starts a research run or a sweep of up to eight
variants (New run or sweep): it runs the backtest command line in a child process, one job at a
time, and shows its progress and links each variant's run page. The page lists the run folders
by strategy and sweep with each variant's deltas against its base, and compares up to four. A
job's files are under `HAT_DATA_ROOT/backtests/jobs/<job id>/` (`err.log` holds the child's
stderr). The job list is in memory: after a restart it is empty, and a child still running
finishes and records its runs.

### Shadow-day replay

`python -m app.hat.daily.replay --date YYYY-MM-DD [--bot <id> ...]` (backend venv,
`HAT_DATA_ROOT` set) compares what the live daily path decides on session D's close with what the
backtest engine decides for the same night on the same store and config: each bot's regime gate,
watchlist rows and their planned entry (pivot and stop, as shares of D's close), a rotation's
ranking, targets and size on a rebalance night (the capital and each order, on the live paper
book at D's close and the night's allocation, live's sizing beside the backtest's), and the
close-phase exits of the positions the live
book held at D's close. It prints one JSON report to stdout (redirect it to keep it): `compared` (sizes), `counts` by kind and cause,
and every difference with its cause (`membership`, `quality_excluded`, `factor_pending`,
`no_bars`, `bars_differ`, `regime_inputs`, `passers_differ`, `ranking_differs`,
`prices_differ`, `capital_differs`, `target_differs`, `sizing_basis` (the sides size on another
basis: a bug), or
`same_inputs`: the code paths decide apart on the same inputs, a bug to chase). It reads nothing
after D (`replay_lookahead`), writes nothing (read-only transaction) and needs D's next session
stored (`replay_no_next_session`, or the backtest loader's `backtest_no_data`). Without `--bot`
it replays every active bot with a live version. The intraday 5-minute trigger is not replayed.

### Daily data update

`python -m app.hat.data.update` (or `scripts/hat_data_update.sh`, from any directory) brings
every data store up to the latest finished session (from 18:00 IST, the day's; before, the
previous) when the live service is not running. It runs the steps below in order; each finds its
own last good session and fills every session after it through `--through`, so a missed day is
caught up on the next run; a step with no good session yet starts at `--through` (older history
is the backfill tools' job). A failed step does not stop the steps that do not read its data; the
steps that read it are skipped.

| Step | Does | Waits on |
| --- | --- | --- |
| `calendar` | `calendar.refresh`: NSE's holiday list into `hat_trading_calendar` | |
| `bars` | `history_cli --resume`: the bhavcopy days into `bars_daily_raw`, the index archive | |
| `listing` | `listing.refresh`: the stocks of the instrument master from NSE's equity list (`EQUITY_L.csv`): an ISIN the bars registered is activated and named, renames and series moves are taken, a stock the list dropped is delisted; funds and rights entitlements are left to `reference.weekly` | bars |
| `ingest` | `ingest.nightly` per session on the stored bars: index levels, market breadth, NIFTY 500 TRI, `ingest.completed` | bars |
| `quality` | `quality.gate` per session | ingest |
| `ca` | `ca.adjust` per session: `bc` file, dividends, demergers, lineage (next-session lookups need MCP: logged `corporate_action.unchecked`) | ingest |
| `features` | `features --through` | quality, ca |
| `membership` | `nifty500_membership.py` fetch, text, parse, reference, build as of today; the build copied into `admin/` as `nifty500-membership-<today>.csv`, then `load-index-membership --apply` of it | bars |
| `bars5m` | `kotak_5m.py members`, then `topup` from the first session (of the last 10 and every one after the last stored day) in which a member with a daily bar has no 5-minute bars and no ledgered refusal, then `bars5m_import --apply`; fails while one is still missing | bars, membership |
| `etf` | `overlay_etf.py` local, kotak, indices, dividends, build, store | |
| `results` | weekly: results fetch, fetch-secondary, parse, derive | |
| `fundamentals` | weekly: fundamentals extract, derive | results, bars |
| `record` | `record.refresh`: manifests and reference CSVs pushed to the data repo (Data record) | |

Results and fundamentals run weekly: the cached filings change only in results season, `extract`
replaces every row (about 15 minutes), and an NSE filing keeps NSE's own `public_at`, so a week's
delay moves no point-in-time date (a secondary-source page counts from its fetch: up to a week
later, never earlier). The working directories are `HAT_DATA_ROOT/work/nifty500`,
`work/kotak_5m` and `work/overlay`, or `NIFTY500_DIR`, `KOTAK_5M_DIR`, `OVERLAY_DATA_DIR`; the
membership one holds the hand-kept `overrides.json` and `symbol_alias.json` (Index membership).
Kotak credentials come from the environment or `.env` and are never printed; `KOTAK_PYTHON`
(the timer installer passes it on) names an interpreter with neo-api-client and pyotp when the
backend's lacks them.

```bash
scripts/hat_data_update.sh --dry-run                 # what each step would do
scripts/hat_data_update.sh                           # everything due
scripts/hat_data_update.sh --only bars5m             # one step (a weekly one runs when named)
scripts/hat_data_update.sh --through 2026-10-28      # stop at an earlier session
scripts/hat_data_update.sh --status                  # the status file
```

Status: `HAT_DATA_ROOT/update/status.json` holds, per step, `result` (`ok`, `failed`, `skipped`,
`not_due`), `last_good` (the session its data is complete through), `last_run`, `last_ok_run`,
`detail` and `log`; the step's output is in `HAT_DATA_ROOT/logs/update/<date>/<step>.log`. The
exit is 1 when a step failed or was skipped, 3 when another run holds `update/update.lock`.
`bars5m` also needs the minute-history lock: while another `bars_5m` writer runs it fails, and
the next run catches up.

Schedule: `scripts/install_data_update_timer.sh <HAT_DATA_ROOT>` installs the systemd user timer
(`deploy/systemd/`) for that checkout: every day at 19:30 IST, `Persistent=true`, so a run missed
while the machine was stopped or suspended starts when it is back (needs lingering:
`loginctl enable-linger`). `systemctl --user list-timers hat-data-update.timer` shows the next
run, `journalctl --user -u hat-data-update` the summary lines.

Recover: read the failed step's `detail` and log, fix the cause, then run that step with
`--only` (or wait for the next run). A failing `quality` keeps `features` back, as it does the
live chain; a `membership` build that refuses an unresolved symbol needs an `overrides.json` or
`symbol_alias.json` entry.

### Data record

The private data repo (a local clone at `HAT_DATA_REPO`, default `/data/workspace/algotradingdata`;
its `README.md` maps it) keeps what must not be lost silently. `python -m app.hat.data.record`
(the update's last step) rewrites, in the clone:

- `manifests/<store>.csv` for `raw`, `bars_daily_raw`, `bars_5m`, `features_daily`,
  `index_daily`, `breadth_daily`, `etf` and `verify_reference`: per file its path, rows, date
  range, bytes and sha256.
  The commit message counts per store the files added (`+`), rewritten (`~`, bytes of an existing
  file changed) and removed (`-`) and lists the first 50 rewritten and removed files of each store
(the rest are counted; the manifests' diff names every one); the step's detail
  names the first rewrites. A rewrite in `raw`, `bars_daily_raw` or `bars_5m` without a planned
  repair is a data incident; `features_daily` partitions are rewritten when an adjustment
  changes.
- `reference/<table>.csv`: index membership, corporate actions and their versions, demerger
  notices, results and fundamentals filings with `public_at`, the trading calendar, instruments,
  symbol history, ISIN lineage, merges and overrides, read through the read-only engine in a
  stable order (UTC timestamps).

It commits and pushes only when a file changed. The clone's pre-push guard
(`.githooks/pre-push`; enable once with `git config core.hooksPath .githooks`) refuses a push
that adds a credential-looking file (`.env`, `*token*`, `*.key`, ...) or text (private keys,
GitHub/AWS/Slack tokens, JWTs, password or secret assignments); the step fails while the guard is
off or the clone is not on `main`. A store folder that is missing while its manifest is committed
(a wrong or unmounted data root) fails the step instead of reading as no change. A failed
`record` (a missing clone, a refused or failed push) is reported like any step and
stops no data step; fix it and run `--only record`.

### Index membership

A backtest reads its universe's index (`publish-universe --index`, default `NIFTY 500`) point in
time from `hat_index_member`. NSE publishes only today's list, so the history is a CSV rebuilt
from the NSE Indices change notices (`index_name,isin,symbol_at_the_time,from_date,to_date,
source_notice`; `to_date` is the first session no longer a member, empty for a member on
`as_of`) by `scripts/data/nifty500_membership.py` in a working directory outside the repo:
`fetch` (notices and today's list), `text`, `parse`, `reference` (hat_live and the daily bars,
read-only), `build --as-of <list date>`; its docstring has the hand-kept `overrides.json` and
`symbol_alias.json`. Write the coverage report beside the CSV. Load it from
`<HAT_DATA_ROOT>/admin/`:

```bash
$ADMIN load-index-membership --csv nifty500-membership.csv --as-of <list date>          # dry run
$ADMIN load-index-membership --csv nifty500-membership.csv --as-of <list date> --apply
```

The command replaces that index's rows with the file's (loading the same file again changes
nothing) and records the source, digest, cover (the earliest `from_date` to `--as-of`) and
`off_size`, the change dates whose member count is not `--size` (default 500; the coverage report
explains each: a second share class, a vacancy, a non-stock row). Every ISIN must name an
instrument, or nothing is written. A run ending after `--as-of` is refused: after an index change
notice, extend the CSV and load it again.

### Industry history

`hat_industry` holds each chain's point-in-time industry (docs/decisions.md, Industry history),
built from NIFTY 500 list snapshots kept under `<HAT_DATA_ROOT>/admin/nifty500-industry/` as
`YYYY-MM-DD.csv` (the capture day; NSE's list and its archived copies, e.g. the Wayback
Machine's). Add a newer list as a new file and load again:

```bash
$ADMIN load-industry-history --dir nifty500-industry           # dry run: changes and coverage
$ADMIN load-industry-history --dir nifty500-industry --apply
```

It replaces each classified chain's rows dated up to the newest snapshot (rows the nightly load
wrote later stay) and prints the old-label mapping it used and the NIFTY 500 coverage from 2016
(member chains classified, unknown member-days before and after, the names left unknown).
Loading the same files again changes nothing.

### Financial results

`hat_financial_result` holds NSE's quarterly results from the quarter ending 2020-03-31, one row
per filing version (docs/decisions.md, Financial results store). One command per step, from
`backend/` with the venv, `HAT_DATA_ROOT` and `DB_NAME=hat_live`:

```bash
python -m app.hat.data.results fetch             # NSE, about 1 request/s (hours on a first run)
python -m app.hat.data.results fetch-secondary   # the secondary source, 1 request/3 s
python -m app.hat.data.results parse             # the cache into hat_financial_result
python -m app.hat.data.results derive            # hat_financial_growth, rebuilt
```

`--symbols A,B` limits `fetch`, `fetch-secondary` and `parse` to some instruments; `--from`/`--to`
limit the period ends `fetch` and `parse` take (default 2020-03-31 to today; `fetch-secondary`
reads whole pages); `fetch --offline` reads the cache only. `derive` rebuilds the whole table and
takes only `--basis`. The scope (`scope.py`): `--universe nifty500` (default) covers the NIFTY 500
point-in-time members with a membership on any day from 2021-01-01, `--universe ep` the instruments
passing the S2 EP universe on any session since; `--basis consolidated-first` (default) takes each
company's consolidated results, and its standalone results only for a quarter it filed no
consolidated results for (`both` takes every filing); a consolidated filing NSE lists counts even
when its XBRL is unreadable, and `derive` and `results_as_of` apply the same rule to rows already
stored. Both NSE listings are read: Reg 33 holds the
quarters to December 2024, the Integrated Filing listing those from March 2025. Every step is
incremental and can be stopped and rerun: a document is fetched once, a server error or no answer
again on the next run (raw answers live gzipped by sha256 under `raw/nse-results/` and
`raw/secondary-results/`, with a `manifest.jsonl`), listings again after 20 hours, a secondary
page a week after its fetch or once NSE's cached listing has a newer filing of the company (run
`fetch` first), an announcement day until fetched after it ended, and `parse` adds only filings not stored
yet. A new quarter is the four commands again once its results are out. `parse` writes every
filing's outcome to `<HAT_DATA_ROOT>/results/filings.csv`, and re-times every stored row: a row the
current rules time differently moves, later, or (a first version) earlier only onto an
announcement's dissemination (`results/retimed-<UTC time>.csv`, one per run and kept, written as
each company commits, lists each move with the old and new time, source, estimate flag and XBRL
time; a row whose filing is gone from NSE's listing moves only from its announcement's submission
to that announcement's dissemination). Before the first parse of a release that changes the
timing rules on hat_live, which the daily update would otherwise run itself: stop the timer
(`systemctl --user stop hat-data-update.timer`), take a snapshot (`scripts/hat_snapshot.py`)
and a `pg_dump -t hat_financial_result` beside it, run `parse` and `derive`, check them, then
start the timer again. Rows are otherwise append-only (a trigger refuses DELETE
and any other UPDATE),
so try a change on a scratch database first and run `derive` after a re-time. Run `parse` only after `fetch` has
finished: a quarter not yet fetched would be backfilled from the secondary source.

### Fundamentals

`hat_fundamental_filing`, `hat_fundamental_fact` and `hat_fundamental_metric` hold every figure
the results cache gives, and the ratios derived from it (docs/fundamentals.md). Run from
`backend/` with the venv, `HAT_DATA_ROOT` and `DB_NAME=hat_live`, after the results `fetch` and
`fetch-secondary` steps have finished:

```bash
python -m app.hat.data.fundamentals extract   # the cache into filings and facts (about 15 min)
python -m app.hat.data.fundamentals derive    # hat_fundamental_metric, rebuilt
python -m app.hat.data.fundamentals report --out <path>.md [--example RELIANCE]
```

`extract` reads only the cache and replaces both tables. With `--symbols A,B` it replaces only
those instruments' rows. `--from`/`--to` limit the NSE period ends (default 2020-03-31 to
today). The outcome counts go to `<HAT_DATA_ROOT>/fundamentals/extract-outcomes.json`. `derive`
reads the stored filings, the daily closes and the split and bonus factors, and replaces the
metrics. `report` writes the coverage report (default
`<HAT_DATA_ROOT>/fundamentals/coverage-<date>.md`). Every step is rerunnable: the same cache
gives the same rows.

### Data verification

Compares the stored data with independent references (`app/hat/data/verify`, read-only: the
database under a read-only transaction). From `backend/`, with `HAT_DATA_ROOT` and the database set:

```bash
python -m app.hat.data.verify [--check D1 ...] [--from YYYY-MM-DD] [--to YYYY-MM-DD] \
    [--ca-data DIR] [--notices DIR] [--kotak DIR] [--fetch] [--report DIR]
```

D1 daily bars vs the bhavcopy files under `raw/`; D2 corporate actions vs NSE's corporate-action
pulls (`--ca-data`: `splits_bonus_*`, `rights_issues_*`, `demergers_*`, `etf_unit_actions_*`,
`dividends/ca_*.json`, `results_announcements_*`), each demerger factor against the company's
published cost-of-acquisition split (`d2_cost_splits.csv` beside the exceptions; a book-value
apportionment, so a far one warns), each stored price factor against the bhavcopy's previous
close on the ex-session (raw on nearly every ex-date, so no evidence there and no check of a
split or bonus factor; one that moved must agree), each split and bonus against the ex-date open,
the volume either side, the results XBRL share count and screener's paid-up capital (warns on a
disagreement, fails when none agrees and the open or two kinds disagree), the adjusted series' moves over 20% (a band's only when the close is
held at the band) and a seeded sample of one stock per company, yearly price returns
(2022-2025, the depth Fyers serves) against Fyers' daily history (fails under 70% of the sample
or 250 stock-years compared). The pull's date is its note's `Collected DD-Mon-YYYY` line
(`README.md` in `--ca-data`), never a file time; without it D2 fails and `ca_import` refuses; D3 NIFTY 500 membership vs the
parsed notices and archived lists (`--notices`: `changes_parsed.json`, `overrides.json`); D4 the
5-minute bars vs the Kotak raw files (`--kotak`: the refused days classified, and a seeded sample
of stored days compared bar by bar); D5 results `public_at` vs NSE's dissemination times
(`--ca-data`); D6 calendar, suspensions and ISIN lineage vs NSE's archive (every weekday the
calendar closes is asked of it) and symbol changes; D7 index, TRI and ETF series vs NSE's files,
niftyindices' answers and NSE's ETF unit actions. D5 demands every quarter of each NIFTY 500
member from 2020-03-31 (its lineage chain judged as one company) and reads its reviewed
exceptions, each naming one company and one quarter with its reason, from
`backend/app/hat/data/verify/d5_exceptions.csv`: an excepted finding warns, and an exception no
finding needs warns too (`exception_unused`). D2 and D6 read theirs the same way, each naming
one item by its own ISIN and day (an ex-date, a link's date, a move's date) and pinning the
value it was reviewed at (`pin`, `field=value`: a changed item fails again), from
`d2_exceptions.csv` and `d6_exceptions.csv` beside it; a row whose reason starts `OPEN:` excepts
nothing and lists its item as an `open_defect` (reviewed, no cause found). D2 and D6 judge stocks only (funds, rights entitlements are
counted). D3, D4 and D7 read theirs the same way (`exceptions.py`) from `d3_exceptions.csv`,
`d4_exceptions.csv` and `d7_exceptions.csv`: D3 and D4 by the item's ISIN (a notice's symbol
resolved to the one ISIN it has had), D7 by the index name under `key`; D4 pins the bars and the
scale (`bars=62;scale=1.3367`). A row two items match excepts neither, and a row listed twice is
refused. A membership history that starts after 2020-03-31 fails D5
(`scope_unknown`). D3 judges every session from 2016-01-01 (`sessions_before_history` warns
before it); D7 compares an index day neither NSE's archive nor niftyindices' copy has a day file
for with niftyindices' price history (`verify_reference/index/history_<index>_<year>.json`). A
check without its reference fails
(`reference_missing` and the like); a span a reference does not reach fails or limits the
check's coverage, never warns. `--fetch` downloads a missing public reference file once, two
seconds apart, into `verify_reference/<group>/`, each file written once (NSE archives, the
closed-day probes as one new file per run, Wayback captures, niftyindices); without it only kept files are read. The full window
(2011-06-22 to the last bar) takes about 20 minutes. D2's Fyers sample is downloaded apart,
after a Fyers login from the app (it never logs in or refreshes the token): `python -m
app.hat.data.verify.fyers` asks one request every 6 seconds into `verify_reference/fyers/`, keeps
each answer once and stops at the first refusal, so a rerun asks only what is missing.

The report (`verify.json`, `verify.md`) goes to `HAT_DATA_ROOT/audits/verify-<date>/` (a run of
fewer checks or a shorter window: `verify-<date>-<checks>-<from>-<to>/`; `--report` names a folder
`audits/verify-*`, the only folders read, and any other is refused); the command exits 2
when a check fails. It records the data snapshot it judged (the verified stores' files and the
reference tables' rows in its window) before and after the checks; data that moved during the
run fails every check. A backtest's data audit judges each blocking check by the newest report
covering the window that ran the check on data unchanged since (`verify` check,
`docs/backtests/process.md`), so a narrow run never hides a wider report's other checks. The
window starts at the first session the run reads (the lookback before its period). A failed
finding dated in it, a check that does not cover it, or data changed since every report that ran
the check leaves the run unverified; rerun the verify after a data correction. Findings
are not corrected here: each goes through its own correction path (`correct-actions`, the
membership and lineage tools).

### First live session checks (O2 day)

Checks on the first session with a real Fyers account; note each in the Check log. They place
no order, though `onboard-account` writes the account row and its capabilities.
Fyers' reply fields here are assumed from its documentation until read on the live account.

- GTT fields and tags: a placed GTT shows its HT `orderTag`; its sale carries the tag.
- Error codes: `-360` (day's calls spent), 429, `-50`, `-16` reach HAT as their fixed codes.
- Holdings vs positions: a stock bought today is in positions (`netQty`, `productType` CNC)
  only, not in holdings; it moves to holdings (T1, then settled) without appearing in both; a
  sale today of a holding shows as a negative net position against the holding.
- Equity basis (Q1): before a CNC buy, on its trade date and on its settlement day,
  "Available Balance" + every share at `ltp` (holdings plus today's CNC positions) gives the same
  equity at unchanged prices: the cost leaves "Available Balance" on the trade date (into
  "Utilized Amount") and is not taken again at settlement. Also check a sale day: the proceeds
  reach "Available Balance" on the trade date. The evening `allocate` logs no
  `equity_unavailable`.
- Stop-to-market: a sale over a day stop shows the stop cancelled in the order book before the
  market sale, within the bounded wait (`stop.cancel_unacked` otherwise).
- The market-price-protection fixture and `onboard-account` against the live account.

### Switching the order broker

HAT places orders with one broker, `HAT_BROKER` (decisions: One active order broker). To make
Zerodha the order broker, outside market hours:

0. Close every real position and working order at Fyers first: HAT refuses to start with them on
   an account of a broker that is not the active one (`inactive_broker_holds_positions: account
   <id> (<broker>)`; the same holds switching back).
1. Register the host's static IP with the Kite Connect app; set the app's redirect URL to
   `http://127.0.0.1:8000/api/hat/auth/zerodha/callback` (no route serves it: copy the
   `request_token` from the address bar) and put `KITE_*` and `HAT_BROKER=zerodha` in `.env`.
2. Restart; log in to Kite (Morning login); `onboard-account --account-ref <Kite user id>` (the
   broker defaults to the active one; another is refused `broker_not_active`).
3. `refresh-kite-symbols [--dump <instruments.csv>]` maps the instruments to Kite's trading
   symbols and `instrument_token` (from the latest bhavcopy's NSE tokens; the weekly reference job
   does it too while Zerodha is active). Without `--dump` it downloads Kite's instrument list.
   A result that maps nothing or under 80% of the stored mappings changes no row
   (`kite_symbols_shrank`; on the weekly job, `job.failed`): check the dump and the bhavcopy.
4. The account stays shadow until the live check (decisions: Zerodha stays shadow). Moving bots
   and sleeves from the Fyers account is not built.

Back to Fyers: `HAT_BROKER=fyers` (or unset) and restart.

### Corporate actions and reference data

Alerts:

- URGENT `corporate_action.unhandled`: `/flatten` a real position; a virtual one, outside market
  hours: `$ADMIN resolve-action --position <id> --action <ex-date>:<type> --close-at <price>`.
- URGENT `position.split_gap` (symbol, previous close, price): a held name gapped like a split
  with no stored action (a stored dividend does not count). A split or bonus: add it (`correct-actions`, op add), and the next
  `ca.rescale` rescales the position. A real crash: `/flatten` a real position; a virtual one,
  outside market hours: `$ADMIN resolve-action --position <id> --action <day>:split_gap
  --close-at <price>`.
- URGENT `position.isin_changed` (symbol, old and new ISIN): a position held across an ISIN
  change. `/flatten` a real position; a virtual one, outside market hours: `$ADMIN resolve-action
  --position <id> --action <new first day>:isin_change --close-at <price>`.
- URGENT `job.failed` of `ca.rescale`, `fills_not_synced`: an account holding a position to rescale
  has not read its fills since the last close (no broker login, a broker outage). Its positions
  stay manage-only: log in, and the next morning's run rescales them; `/flatten` sells one now.
  URGENT `corporate_action.pending`: a position could not be rescaled; read the log.
- `corporate_action.conflict`: a lookup disagrees with the stored factor, which stays. Check
  NSE's announcement; correct it if wrong.
- `dividend.review`, `dividend.amount_missing`: set the amount once checked (`correct-action
  --cash` or a list line). `lineage report` lists every dividend without an amount.
- `dividend.unchecked`: from `nsearchives.nseindia.com/archives/equities/bhavcopy/pr/` take
  `PR<ddmmyy>.zip`'s `bc<ddmmyyyy>.csv`, then `python -m app.hat.data.ca_dividends --bc <file>
  --day YYYY-MM-DD [--apply]`.
- URGENT `corporate_action.demerger_exit` (symbol, `sell_by_close_of`): `/flatten` the real
  position on that day before 15:15 (continuous trading ends there); HAT sells nothing for it (a
  virtual one closes itself).
  `corporate_action.demerger_late`: the notice came too late or the position is still open; it
  is handled as `unhandled` once the factor is stored. `demerger.unchecked`: tonight's notices
  failed; read the log.
- `corporate_action.demerger_unlisted` (symbol, `<ex-date>:demerger`): tonight's `bc` file no
  longer lists a stored notice before its ex-date (a moved or withdrawn record date). The notice
  stands and its exit still fires; check NSE's announcement and sell or hold by hand.
- `dividend.trades_stale`: the closed-trade dividend restatement failed; read the log.
- `corporate_action.projection` (`mismatches`, the first `keys`): a stored action is not what its
  version ledger selects (a writer skipped `Ledger`), or the check failed (`unchecked`); `point_in_time
  report` lists every key. A backtest selects from the versions, so fix the writer first.
- `lineage.unchecked`, `lineage.review`: see `lineage report`.

Owner corrections (journalled; no load rewrites them):

- `$ADMIN correct-action --isin ISIN --ex-date D --type T (--factor F [--as T2] | --cash RS |
  --delete) --note TEXT [--known-on DATE]`. `--known-on` is when the value was public: a factor
  without one is refused (`known_on_required`); a dividend's amount without one is dated at its
  ex-date (NSE announces it before). D2 fails any applied value left undated
  (`ca_value_undated`): a backtest would skip the name from its ex-date on.
- `$ADMIN correct-actions --list NAME.tsv [--apply]`: columns `op isin ex_date type factor
  new_ex_date note`, optional `cash` and `known_on`; ops replace, delete, move, add. All or
  nothing; a line writing a factor without `known_on` refuses the list. A demerger is added as type `demerger` (stored as an `other` flagged one); a move
  keeps the row's own text and flag.

Known raw-bar artefacts (raw bars are immutable; kept, not corrected):

- ADANIENT 2015-06-03 (demerger ex-date): open and high 573.30 are a single pre-open print (low
  106.35, close 109.75). The demerger factor is the close over the previous close, so the close
  series is continuous, but the bar's high lifts the 52-week high until about 2016-06 and adtr20
  for 20 sessions (`lineage report` lists it as an `other` jump). Conservative: S1's distance
  from the 52-week high fails, so it gives no false entry.

Tools, `python -m app.hat.data.<tool>`: imports are dry runs until `--apply` and idempotent;
input files sit under the working directory (run there with `PYTHONPATH=<repo>/backend`).

| Tool | Does |
| --- | --- |
| `lineage build` | Decide and store every ISIN succession |
| `lineage report [--since D] [--kind K]` | Review list: unlinked successions, disagreeing copies, unexplained jumps by kind (`other` needs a reason) |
| `lineage show SYMBOL...` | Chains and jumps of symbols |
| `lineage link\|unlink OLD NEW --note T` | Owner's verdict on a succession |
| `ca_import --db FILE` | Collected NSE corporate-action file (its folder's `README.md` gives the pull date: `Collected DD-Mon-YYYY`) |
| `ca_research --db FILE --a .. --d ..` | Research-confirmed demergers and rights |
| `ca_dividends --feed DIR [--bonus F]` | Dividends from NSE's monthly feed JSON; `symbol_isin_disagree` (NSE filed one company under another's ISIN) is the owner's to fix |
| `ca_demergers [--apply [--neutral]]` | Demerger gaps, ratio, disputed list; `--neutral` only with owner approval |
| `demerger_notice announced F.tsv [--replace]` | Date stored demergers' `announced_on` from a reviewed file (`isin ex_date announced_on source_url note`); `--replace` also moves a stored date to an earlier sourced one; journalled `corporate_action.announced` |
| `demerger_notice exits [SYMBOL...]` | The session each stored demerger's forced exit falls on, or why it holds |
| `point_in_time report` / `pending SYMBOL` | Lagged and undated actions, version ledger (projection check must be 0); factor-pending sessions |
| `index_history backfill [--from D] [--to D] [--apply]` | Fill the bar days `index_daily` lacks: the nightly indices from NSE's day files, then niftyindices' copy, then its price history; NIFTY 500 TRI from niftyindices (a year per request). A day no source has stays missing and is listed (exit 1; India VIX has 5 such days), never the last close carried. The nightly ingest fetches the TRI itself (below); a day still missing in its `tri_missing` is filled here |
| `index_history reload --day D [--day D ...] [--apply]` | Rewrite a stored day's index levels that NSE's day file for it differs on (the file fetched into `raw/` when not kept; only its rows dated that day are read); the day's other indices are kept. Prints each changed row, stored beside the file's. Dry run by default; `--apply` first writes every stored row of the changed days to `index_reload/before-<UTC time>.csv` under the data root |
| `index_history nightly-tri --day D [--apply]` | The ingest's TRI step for one session: NIFTY 500 TRI for the bar days from 14 days before `D` to `D` that lack it. Each night's `ingest.completed` carries `tri_stored`, `tri_missing` (bar days niftyindices has not published yet: left missing, asked again the next nights) and `tri_failed`; a TRI failure never fails the ingest. Dry run by default |
| `index_history verify-tri --from D --to D [--fetch]` | The stored NIFTY 500 TRI closes against niftyindices' own answers kept under `raw_tri/` (exact decimal equality): lists mismatches, stored days not in the raw files, raw days not stored, years with no file; exit 1 on any. `--fetch` first asks for each calendar year of the range (one request per year, paced) and keeps the answer as `NIFTY_500_TRI_<first>_<last>.json`; an answer with the same closes as a kept file is `kept`, one with other closes is reported `changed` and never replaces it. Writes nothing else |
| `renames` | Seed symbol history from renames |
| `bars5m_import --source DIR [--batch N] [--only SYMBOL...] [--apply]` | The broker's 5-minute history (`DIR/<SYMBOL>.parquet`, ISINs from `../universe_raw.csv` and `../nse_cm_scrips.csv`) into `bars_5m`: adds only the (instrument, day) pairs not stored, ledgers refused rows and days in `bars_5m_rejects`, resumes from `bars5m_import/progress.json` (`--only` imports just those files, again even when done); reads the database, holds the minute-history lock. Summary also in `bars5m_import/report-<day>.txt`. Refuses (`bars5m_store_unfit`) while a stored day is off the raw daily bar: run `bars5m_repair` |
| `bars5m_volume --kotak DIR [--apply] [--left FILE]` | Restore the raw volume of the stored `bars_5m` days the import cut by a factor, from the broker export. A candidate is a day whose session volume does not reconcile with the bhavcopy's, or is off the export's by more than 0.5%. It is restored only when the export's bars are the stored ones (same prices at the day's scale), its volume reconciles with the bhavcopy's, and every stored bar's volume is the export's times one common factor; prices untouched. Prints the candidates (off, scaled), restored, agreeing (every stored bar already has the export's volume, as a repaired day does), unproven and without export bars by year; `--left` lists the days left as they are (unproven, no file). Dry run by default; `--apply` first writes every changed bar's old and new volume to `bars5m_volume/before-<UTC time>.parquet`, then rewrites each affected day file once. A second run finds nothing to restore. Holds the minute-history lock |
| `bars5m_refetch fetch --first D --last D --run NAME [--resume] [--per-minute N]`, `apply --run NAME [--apply]`, `gaps --run NAME [--write]` | Refetch from Fyers (log in first) the stored `bars_5m` days a vendor left short: a session of a Kotak `bars5m_gaps` row without its 15:25 bar, and a NIFTY 500 member-day without its 09:15 bar. `fetch` judges each day (fits the raw daily bar, replayable by `on_basis`, opens at NSE's daily open to a tick, holds the 15:25 bar, lacks no refused bucket, volume reconciles with the bhavcopy) and keeps the admitted bars and every outcome (`outcomes.csv`) in the run folder `bars5m_refetch/NAME` under the data root, writing nothing to the store; `--resume` continues a stopped fetch over the same `--first`/`--last` and retries the instruments whose history failed. The fetch counts its calls in the minute history's progress and holds its lock, so do not run it across the daily update. D4 reads each run's `applied.csv` as Fyers-written days. `apply` prints the days by kind and month (dry run by default); `--apply` saves the stored rows to the run's `before.parquet`, then replaces each admitted day whole, skipping a day whose stored rows changed since the fetch, and refuses a run applied already; holds the minute-history and update locks. `gaps` prints the `bars5m_gaps.csv` rows left (Kotak rows narrowed to their still-short sessions, with the outcomes, and an `open_not_auction` row for each day an applied run wrote whose 09:15 bar opens off NSE's open); `--write` writes them to the repo's `bars5m_gaps.csv`. To roll a run back, restore its `before.parquet` rows over its `applied.csv` days with the update stopped, then put a `rolled_back` file (with the reason) in the run folder: `apply` then refuses the run, and D4 and `gaps` no longer read its days as Fyers' |
| `bars5m_repair --first D --last D --fetched-on D [--apply]` | Mend every stored `bars_5m` day that does not fit the raw daily bar: a day of a download that wrote the broker's adjusted candles (`--first..--last`, fetched on `--fetched-on`) is converted with the stored factors as of that day; a day filed under the wrong ISIN-chain member moves to the member with the daily bar (dropped as `chain_duplicate` when that member holds the day); the rest are dropped and ledgered. Dry run by default; back up the data root before `--apply`. Writes `bars5m_repair/mended-<time>.csv` and `report-<day>.txt`; a second run finds nothing. Holds the minute-history lock |
| `bars_basis explained-rejects [--write]` | Counts the ledgered `basis_mismatch` days still without bars and those whose raw daily bar opened at its high or low, which the basis check now admits when no bar passes that open; `--write` lists them in `bars_5m_explained/explained-rejects.csv` under the data root for a refetch |
| `$ADMIN repair-symbol-history [--apply]` | Close overlapping symbol intervals |

Download tools, `python scripts/data/<tool>.py --work DIR` (DIR outside the repo; each prints its
steps with `--help`): `kotak_5m` fetches the broker's 5-minute history for the NIFTY 500's
current and former members (`members` from hat_live, then `download`, `former-map`,
`former-download`; market data only, about one call a second, resumable) into the
`raw_5m/<SYMBOL>.parquet` layout `bars5m_import` reads; `nifty500_membership` builds the index
history (Index membership); `overlay_etf` gathers the idle-cash overlay's research data
(NIFTYBEES, GOLDBEES, LIQUIDBEES daily and 5-minute bars, the TRIs, the 1D Rate Index and
LIQUIDBEES' dividends from the AMC) into raw files it never overwrites, and builds the
split-adjusted bars, the LIQUIDBEES total-return series and a coverage report from them; its
`store` step writes the build and the 5-minute bars into `HAT_DATA_ROOT/etf/`, what a backtest's
overlay reads (rerun it after each `build`). It writes nothing to the database.

After an import or correction: `lineage build`, then `features`: it recomputes the chains whose
factors, dividends or links changed (every date when more than `MAX_CHAINS` did), so `--full` is
never needed for it. A release that changes a feature formula or the adjusted views bumps
`FEATURES_RULE` (`features_state.py`), which makes the next run, scheduled or manual, recompute
every date (`inputs_changed`); `features --full` does the same at once. The writers of actions,
lineage and symbol history share the `hat_reference_data` advisory lock; `features` does not take
it, so run it after they finish.

### Live and paper books

Each bot's real positions (`live`) and shadow ones (`paper`) are one book each, from its first
allocation with capital (`capital`, or the sleeve's nominal `virtual_capital` for paper), marked
at the official closes, each dividend credited on its ex-date as the backtest does (`daily/books.py`). The report: equity and the day's P&L, realised P&L
(net, with dividends) and unrealised, costs, the invested share (today's and the mean; idle cash
is a failure), each closed trade's R, and the Backtest Result Standard's headline metrics
(`standard.book_metrics`, the code a run's `run.json` uses, so `win_pct`, `expectancy_r`,
`max_dd_pct`, `invested_pct`, `cost_share_pct` and the rest compare one to one with the
strategy's evidence run) against the NIFTY 500 TRI. Telegram sends it at 23:45 IST after
`ca.adjust` (`telegram.books`); the dashboard's Books panel (`/api/hat/books`) and `$ADMIN
books` show it on demand. It leads with the return against the TRI over the book's sessions;
CAGR, MAR, Sharpe and Sortino show from 252 sessions (n/a before). A partial sale's gain is
realised when it fills, as the shadow book's free cash counts it; a position closed with no
trade result yet is listed under its book.

## Alerts and failures

Every alert ends with a `Do:` line, what to do (`telegram/render.py:ACTIONS`); the entries
below are the detail.

### Health watch

What the runtime cannot tell about itself: `python -m app.hat.ops.watch` (or
`scripts/hat_watch.sh`), every five minutes on the host by a systemd user timer installed with
`scripts/install_watch_timer.sh <HAT_DATA_ROOT>` (the database and Telegram settings from the
repo's `.env`; `HAT_ENABLED=true` there turns on the heartbeat check). `--dry-run` prints the
findings and sends nothing. Each finding is alerted once while it stands and told `Resolved:`,
with its subject, when it clears; the open ones are in `HAT_DATA_ROOT/ops/watch.json`, saved
before anything is sent, and an alert Telegram did not take is tried again at the next run.
Install it on the machine that runs the backend or the daily data update; `systemctl --user
list-timers hat-watch.timer`, `journalctl --user -u hat-watch`.

- **`runtime.down`** (URGENT): the scheduler's `hat_heartbeat` row (stamped every 30 s) is over
  3 minutes old: the backend is dead or wedged. Broker stops protect positions; `$COMPOSE ps`,
  `$COMPOSE logs backend`, start it, then Restart recovery.
- **`db.down`** (URGENT, `live` or `state`): `SELECT 1` fails. `$COMPOSE ps postgres`.
- **`disk.low`**: under 10 GB or 10% free on the data root's or the root file system.
- **`update.failed`** (one per run), **`update.late`** (no run started for the due date: today
  from 21:30 IST, else yesterday): Daily data update, Recover; for late, `systemctl --user
  list-timers hat-data-update.timer` and linger.
- **`verify.failed`**: the newest report that ran a check failed it; read its `verify.md`
  (Data verification).
- **`watch.check_error`**: one check raised (its name and error type); the others ran.
- **`watch.state_unwritable`** (URGENT, once until a save works): `ops/watch.json` cannot be
  written, so the watch tells nothing else; free the disk or fix `ops/`. With hat_state down
  too, the run exits 1 and `hat-watch-failed` sends `watch.failed` (at most once a day).
- **`watch.failed`** (URGENT, once per failure spell): a run failed outright; when it could not
  send that itself, the `OnFailure=` unit `hat-watch-failed` does, at most once a day.

- **`quality.failed`, `ingest.failed`:** affected bots manage-only. `select job, trade_date,
  status, error_code from hat_job_run where trade_date = current_date order by started_at;` and
  the log. Failed jobs retry every 10 minutes that date (chained ones until 08:30); the kill clears
  when the gate passes: never re-arm it by hand. If the 22:00 fallback fails too, the day stays a
  gap after midnight (incident).
- **`coverage.gaps`:** `python -m app.hat.data.coverage report`. Missing bar day: `history_cli
  --from DAY --to DAY`, then `features`. Index day NSE cannot supply: `coverage ack --note TEXT`.
- **`quality.quarantined`:** a >20% move with no stored action; out of scans. `quarantine list`;
  storing the action releases it, or `quarantine clear SYMBOL` for real news.
- **Special sessions:** add `{"date": "YYYY-MM-DD", "note": "Muhurat trading", "session":
  "HH:MM-HH:MM"}` to `backend/app/hat/data/nse_holidays_history.json` and deploy before the day.
- **`broker.session_lost`:** bots manage-only, stops at the broker; recovery is automatic once the
  session is back (log in if the token expired). A token the broker refused (revoked or expired
  at the broker) shows not connected in the app header until a new login.
- **`broker.unreachable`:** URGENT, once per outage for every account sharing the broker's
  circuit breaker, from 08:30 IST to the close. The broker's calls have failed in a row for two
  minutes, though the token may be valid: no entry goes; stops, sales
  and reconciliation retry. Check the network and Fyers' status; if it lasts into the close,
  decide on the positions at Fyers.
- **`broker.budget_spent`:** URGENT. The day's broker calls are spent: no new entry and no
  reconciliation until tomorrow; stops, sales and the fill sync go on. Check the logs for what
  burned the calls before the next session.
- **`broker_rate_limited`:** nothing was sent. After a plan change set
  `HAT_BROKER_CALLS_PER_SECOND`/`_PER_MINUTE`/`_PER_DAY` to the plan's limits and restart.
- **`feed.stale`:** no triggers for those symbols. If it persists, restart with `--force --reason
  "feed stale"`; stale at the 15:05-15:10 MA exits is an incident: they decide on the live last
  trade, continuous trading ends at 15:15 (NSE's closing auction), and HAT sends no sale from
  then on, so an exit missed by 15:15 waits for the next session (or the owner's `/flatten`
  before 15:15).
- **`order.rejected`:** retried once. Fix the cause (band, margin, freeze, IP); never place by
  hand. **`order.unconfirmed`:** once per entry; the broker did not answer, so the entry is
  never sent again and its tag is settled by reconciliation and the fill sync. A fill of part of
  an order says `Partial: n of m filled` under its fill message.
- **`relay.skipped`:** Telegram refused a message for good; read the event. A skipped card expires.
- **`reconcile.failed`:** no entry on that account until a pass succeeds; check the session.
  Raised once a day: before the day's first success, or once passes have failed after one for
  10 minutes in the session, 30 outside it (the gate stays open then; mismatches go unseen until
  a pass succeeds).
  **`reconcile.unreadable_row`:** check the Fyers book for a manual order.
  **`reconcile.mismatch`:** that bot's entries are blocked; a broker position HAT does not know is
  not HAT's; a missing fill or quantity difference is a bug. A duplicate order at the broker:
  `/kill`, then the same.
- **`gtt.missing`, `gtt.unverified`, `gtt.swap_failed`, `stop.not_placed`:** every real position
  needs one GTT at its stop (Fyers GTT page vs `/positions`). A position unprotected overnight is
  an incident: decide on `/flatten`. `gtt.missing` and `gtt.unverified` come once a day per
  position and reason. `stop.not_placed` and `stop.not_moved` come once a day per
  position and reason while each sync keeps trying. **`stop.would_move_down`:** the position's
  stop is below its resting broker stop, which only rises, so the broker stop stays where it is
  (still protective); the stored stop or tick is wrong: a bug to report.
- **`stop.cancel_unacked`:** URGENT, once a day per position. A sale over a day stop waited for
  the broker to show the stop cancelled and it did not (Fyers cannot turn an SL-M into a market
  order, so the stop is cancelled before the sale). The sale was not sent. A stop still shown
  working, or a cancel the broker refused, stays HAT's working stop; a cancel whose answer was
  lost is probed with a modify to the stop's own trigger, and a refused probe (the stop gone)
  places a new stop at once. Check the Fyers order book: the stop resting is protective (the
  exit tries again); no stop resting means decide on `/flatten`.
- **`position.series_changed`:** exits continue under the new symbol; `unmapped` means none run:
  decide whether to sell at Fyers.
- **`exits.view_failed`:** URGENT, once a day per position. Building the position's exit view
  raised, so its exit rules (trailing stop, MA, target, time) are off and only its resting broker
  stop protects it; the other positions' exits run. Read the log for the cause; decide on
  `/flatten` for a real position.
- **`sale.unfilled`:** check the book and band; decide on `/flatten` or a sale at Fyers.
  **`sale.settled_from_holdings`:** those shares' price is missing from P&L. Outside market
  hours, book it from the contract note: `$ADMIN resolve-sale --tag <sale tag> --qty <n> --price
  <p> --at <trade time> --note <contract note no.> [--fees <rs>]`; an emptied position then
  closes with its proceeds. `baseline: unknown` means HAT could not tell your own shares of the
  symbol from its own: it refuses every sale of the position until you book that sale with
  `resolve-sale` (the contract note's quantity; `--qty 0` when it shows no fill, which ends the
  sale so the exit sells again).
- **`entry.unresolved`:** URGENT, once per entry. An entry of an earlier session whose answer
  was lost may have filled: the account holds `held` shares of the symbol that no HAT position
  explains, and no HAT stop covers them. HAT neither rejects nor adopts it. Check the contract
  note: if it filled, those shares are unprotected; sell them at Fyers or protect them there.
  The entry stays unsure until the holdings show no such shares, when the session end rejects it.
- **`fill.off_lifecycle`:** URGENT. A fill landed after its trade ended (an entry seen cancelled
  or closed, or a sale on a closed position); it is booked and the stop covers the shares.
  Check the position and decide on `/flatten`.
- **Kill and flatten:** `/kill` makes every bot manage-only and cancels its entries still working
  at the broker (so does `/pause`). Re-arm after fixing the cause with `/arm` (every tripped
  drawdown or manual kill), `/arm account` or `/arm <bot>`, and Confirm: the question lists each
  kill it re-arms (whose, reason, when tripped) and Confirm re-arms exactly those, refusing with
  `kill_changed` if one changed or another tripped since. A re-arm keeps the drawdown peak, so
  a drawdown kill re-armed while still 10% under it trips again at the next mark (`/resume`
  does not lift a kill; data-health kills clear themselves). `/flatten` and Confirm
  trips the account kill, cancels the working entries and sells every real position; `/arm`
  lifts the kill. With the market closed it still trips the kill but sells nothing and queues
  nothing: send `/flatten` again in the session.
- **`exit.close_gap`:** weekly in shadow, `select payload from hat_event where event_type =
  'exit.close_gap' order by created_at desc limit 50;`; change the exit rule if biased.
- **`gapscan.latency_observed`:** `select started_at, finished_at from hat_job_run where job =
  'gapscan' and trade_date = current_date;` before 09:20.
- **Machine down:** broker stops protect positions; `$COMPOSE up -d`, then "Restart recovery".

## Backup and host

### Backup and restore drill: `ops.backup_restore_drill`

A systemd timer runs nightly at 23:30 IST as root, `EnvironmentFile=` the `.env`, with an
`OnFailure=` alert unit:

```
python scripts/hat_backup.py run \
  --local "$HAT_BACKUP_LOCAL" --remote "$HAT_BACKUP_REMOTE" \
  --data-root "$(docker volume inspect -f '{{ .Mountpoint }}' hat_hat_data)"
```

A set is the dump plus the data root with a manifest; `raw/` and `bars_5m/` cannot be rebuilt
(`--require raw` while no minute history exists). A set holds `hat.dump` (hat_live),
`hat_state.dump` (hat_state) and the data root, the config files with it. pg_dump must be 18 (or
`--pg-dump`).

Monthly drill: `python scripts/hat_backup.py verify "$HAT_BACKUP_REMOTE/<set>"`; `createdb -h
127.0.0.1 -U "$DB_USER" hat_restore_drill` and `pg_restore -h 127.0.0.1 -U "$DB_USER" -d
hat_restore_drill --no-owner <set>/hat_state.dump`; compare `select count(*) from hat_event;` and
`select max(trade_date) from hat_job_run where status = 'succeeded';` with hat_state; extract
`hat_data.tar` to scratch and check the newest partition and raw zip; drop both.

### Restore from backup

`$COMPOSE stop backend`; verify the set; `dropdb`/`createdb` `"$DB_NAME"` and `"$DB_STATE_NAME"`
in the postgres container, `pg_restore ... -d "$DB_NAME" --no-owner <set>/hat.dump` and
`pg_restore ... -d "$DB_STATE_NAME" --no-owner <set>/hat_state.dump`; `$COMPOSE run --rm --no-deps
-v "<set>:/restore:ro" backend tar -xf /restore/hat_data.tar -C /data/hat`; start, then "Restart
recovery" (changes since the backup show as mismatches).

### Snapshot and restore

`scripts/hat_snapshot.py release` uploads a snapshot as GitHub Release assets of the data repo
(tag `snapshot-<date>`): `pg_dump --format=custom` of the databases (`<db>.dump.partNN`) and a
gzipped tar per folder (`<part>.tar.gz.partNN`), each asset under 1.9 GB. It stages one asset at a
time in a private folder it makes under `--stage` or `HAT_SNAPSHOT_STAGE` (default `/var/tmp`, on
the root disk: `/tmp` is a small RAM tmpfs and the data disk must never fill), uploads and deletes
it, then
uploads `SHA256SUMS` and the record `snapshots/<tag>.json` (every asset's size and sha256 and
every file each tar holds), checks the assets on GitHub against it (size and GitHub's sha256
digest) and commits the record to the clone, under the clone's lock that the update's `record`
step also takes (it waits up to 30 minutes). It refuses a tag whose release already holds its
record (`<tag>.json`): a complete release is never rewritten; an incomplete one (a failed run) has
its assets deleted and is taken again. Like `record`, it needs the clone on `main` with the guard
on, and it skips writers' temporary files (`.*.tmp`) and files removed while it runs. `check --tag T --repo OWNER/algotradingdata`
re-checks a release later. pg_dump is `--pg-dump` or `HAT_PG_DUMP` (version 18; for the database
container `docker exec <container> pg_dump -U postgres`). The command carries its own connection:
the unit loads no `.env`, so a host pg_dump takes `PGHOST`/`PGUSER` in the command and the
password from `~/.pgpass`.

The weekly release (`scripts/hat_snapshot_weekly.sh`, Sundays 10:00 IST) is incremental: both
dumps, and the files of `raw/` and `bars_5m/` that the earlier records do not already hold.
Install it with `scripts/install_snapshot_timer.sh <HAT_DATA_ROOT> <clone> ["<pg_dump command>"]`;
`journalctl --user -u hat-snapshot` shows the run. The full set (the stores, the Kotak raw files,
`ca-data`, the rest of the data root and the workspace folders) is taken by hand with `--part` and
`--exclude PART:PATTERN` (a pattern without `/` matches any path component, one starting with `/`
a folder from the part's root); the data repo's README lists the command of the first one.

Restore: `gh release download <tag> --repo OWNER/algotradingdata --dir <scratch>`;
`sha256sum -c SHA256SUMS`; `cat <db>.dump.part* > <db>.dump` and `pg_restore -d <db> --no-owner
<db>.dump` into a fresh database; `cat <part>.tar.gz.part* | tar -xz -C <target>` for each folder,
the full release first, then each later incremental release in date order (a record's `removed`
lists the files to delete after unpacking it). Drill: restore the latest `hat_live` dump into
`hat_restore_drill`, compare the main tables' row counts with hat_live, drop it.

### Failover drill: `ops.failover_drill`

Outside market hours, switch to the 4G link: broker orders refused or not attempted, a health
alert reaches Telegram, every GTT intact. Switch back, check `/status`.

### Host checklist: `ops.host_checklist`

NTP offset under 1 s (`chronyc tracking`); `curl -fsS https://ifconfig.me` is the IP registered
with Fyers; UPS holds host and router 10+ minutes and 4G takes over; ports 8000, 3000, 5432 closed
outside the VPN and bound to 127.0.0.1.

### Env file permissions: `ops.env_permissions`

`.env` mode 600, owned by the service user (`stat -c '%a %U' .env`), on a LUKS disk, git-ignored,
never in an image or backup.

## Check log

| Date | Check | Initials | Result |
| --- | --- | --- | --- |
