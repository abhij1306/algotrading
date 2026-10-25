# Runbook

Operator procedures. Rules: `invariants.md`; job times: `architecture.md` (Daily cycle).

Conventions: commands run from the repo root on the host as the service user. `$COMPOSE` is
`docker compose --env-file .env -f deploy/hat/docker-compose.yml`; `$ADMIN` is `$COMPOSE exec -w
/app/backend backend python -m app.hat.admin`; `python -m app.hat.data.*` tools run from
`backend/` (in the stack via `$COMPOSE exec -w /app/backend backend`). Database passwords come
from `PGPASSWORD` or `~/.pgpass`. "The window" is 08:30–15:35 IST on a trading day: deploys, DBA
and admin steps run outside it. Commands using `$DB_USER`/`$DB_NAME` need the `.env` variables
in the shell (`set -a; . ./.env; set +a`). Record every check and incident in the check log.

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
   in `app/hat/schema.py`.
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
   hat_live the tables belong to `postgres`, so first have the backend connect as a role that is
   not a superuser and does not own them. Then, for `hat_event` and each `hat_event_pYYYYMM`:
   ```sql
   REVOKE TRUNCATE ON hat_event FROM PUBLIC, <db_user>;
   REVOKE TRUNCATE ON hat_event_p202610 FROM PUBLIC, <db_user>;  -- and so on
   ```
   Retention never truncates or drops event partitions (`ops/retention.py` deletes only the two
   chat tables).

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
entries until the session is live; `/login` gives a fresh link.

With `HAT_BROKER=zerodha` the prompt also carries the Kite link (for the Zerodha account): log in
and paste the `request_token` from the redirect URL into the header's Zerodha dialog (`POST
/api/hat/auth/zerodha/token`, same header, body `{"auth_code": "<request_token>"}`). Kite's token
lasts until 06:00 IST. Fyers is still logged in every day: it carries the market data.

Missed: ~08:32 `broker.session_lost`, 09:05 a second prompt (URGENT with real positions). Bots
stay manage-only and only broker-side stops protect positions (no ticks, trims, MA exits or GTT
changes). Missed triggers are not replayed.

## Admin command line

`$ADMIN --help` and `app/hat/admin.py` list every command. Each is one transaction printing JSON;
a refusal prints `refused: <code>`, exits 2 and writes nothing. Input files are names inside
`<HAT_DATA_ROOT>/admin/`. `$ADMIN show` lists strategies, versions and sleeves with ids.

### First deploy and releases

1. Database up, history and features built, calendar loaded, `HAT_TELEGRAM_CHAT_ID` set.
2. `seed-policy`; "Morning login"; `onboard-account --account-ref <Fyers client id>` (a `failed`
   account takes no sleeve); `seed-strategies`; `seed-bots --account <id>`. Then start HAT with
   `HAT_ENABLED=true`.
3. `publish-universe --constituents <ind_nifty500list.csv> --as-of <date> [--include S]
   [--exclude S]` (unknown symbols or ISINs are listed; nothing written).
4. Backtest each sleeve: `$COMPOSE exec -w /app/backend backend python -m app.hat.backtest --bot
   <bot> --version <v> --from 2011-06-22 --to <last trading day> --for-promotion --regime-mult
   policy`; then `publish-version --version <v> --run <run_id>` (the version is `frozen`) and
   `assign-version --bot <bot> --version <v>`.
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
  --delete) --note TEXT [--known-on DATE]`. Without `--known-on` (when the value was public) the
  value is undated and a backtest skips the name from its ex-date.
- `$ADMIN correct-actions --list NAME.tsv [--apply]`: columns `op isin ex_date type factor
  new_ex_date note`, optional `cash` and `known_on`; ops replace, delete, move, add. All or
  nothing. A demerger is added as type `demerger` (stored as an `other` flagged one); a move
  keeps the row's own text and flag.

Tools, `python -m app.hat.data.<tool>`: imports are dry runs until `--apply` and idempotent;
input files sit under the working directory (run there with `PYTHONPATH=<repo>/backend`).

| Tool | Does |
| --- | --- |
| `lineage build` | Decide and store every ISIN succession |
| `lineage report [--since D] [--kind K]` | Review list: unlinked successions, disagreeing copies, unexplained jumps by kind (`other` needs a reason) |
| `lineage show SYMBOL...` | Chains and jumps of symbols |
| `lineage link\|unlink OLD NEW --note T` | Owner's verdict on a succession |
| `ca_import --db FILE` | Collected NSE corporate-action file |
| `ca_research --db FILE --a .. --d ..` | Research-confirmed demergers and rights |
| `ca_dividends --feed DIR [--bonus F]` | Dividends from NSE's monthly feed JSON |
| `ca_demergers [--apply [--neutral]]` | Demerger gaps, ratio, disputed list; `--neutral` only with owner approval |
| `demerger_notice exits [SYMBOL...]` | The session each stored demerger's forced exit falls on, or why it holds |
| `point_in_time report` / `pending SYMBOL` | Lagged and undated actions, version ledger (projection check must be 0); factor-pending sessions |
| `renames` | Seed symbol history from renames |
| `$ADMIN repair-symbol-history [--apply]` | Close overlapping symbol intervals |

After an import or correction: `lineage build`, then `features`: it recomputes the chains whose
factors, dividends or links changed (every date when more than `MAX_CHAINS` did), so `--full` is
never needed for it. The writers of actions, lineage and symbol history share the `hat_reference_data`
advisory lock; `features` does not take it, so run it after they finish.

## Alerts and failures

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
  hand.
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
  an incident: decide on `/flatten`. `stop.not_placed` and `stop.not_moved` come once a day per
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
(`--require raw` while no minute history exists). pg_dump must be 18 (or `--pg-dump`).

Monthly drill: `python scripts/hat_backup.py verify "$HAT_BACKUP_REMOTE/<set>"`; `createdb -h
127.0.0.1 -U "$DB_USER" hat_restore_drill` and `pg_restore -h 127.0.0.1 -U "$DB_USER" -d
hat_restore_drill --no-owner <set>/hat.dump`; compare `select count(*) from hat_event;` and
`select max(trade_date) from hat_job_run where status = 'succeeded';` with live; extract
`hat_data.tar` to scratch and check the newest partition and raw zip; drop both.

### Restore from backup

`$COMPOSE stop backend`; verify the set; `dropdb`/`createdb` `"$DB_NAME"` in the postgres
container, `pg_restore ... -d "$DB_NAME" --no-owner <set>/hat.dump`; `$COMPOSE run --rm --no-deps
-v "<set>:/restore:ro" backend tar -xf /restore/hat_data.tar -C /data/hat`; start, then "Restart
recovery" (changes since the backup show as mismatches).

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
