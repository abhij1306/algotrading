# Runbook

The operator's procedures. Each `OPS` check below is a checklist item: when it is
performed, add a row to the check log at the end of this section with the date, initials and
result. CONF `ops.runbook_complete` keeps one `###` procedure here for every `OPS` name in
`docs/invariants.md`.

| OPS check | When | What |
| --- | --- | --- |
| `ops.host_checklist` | Before enabling confirm or auto; after any host change | NTP offset, static IP registered with Fyers, UPS, failover link, VPN-only dashboard |
| `ops.env_permissions` | After any `.env` change | Mode 0600, service-user owner, encrypted disk |
| `ops.deploy_window` | Every deploy | Outside 08:30–15:35 IST on trading days; reconcile on start |
| `ops.failover_drill` | Quarterly | Switch to 4G; confirm no orders, alert sent, stops intact |
| `ops.backup_restore_drill` | Monthly | Restore latest backup to a scratch DB; compare event count and latest trade date |
| `gapscan.latency_observed` | Daily for the first 2 weeks of live data | 09:16 gap-scan finish time against the 09:20 bar |

Conventions. Commands run from the repository root on the host, as the service user unless they
say otherwise. `$COMPOSE` stands for
`docker compose --env-file .env -f deploy/hat/docker-compose.yml`. Database commands take the
password from `PGPASSWORD` or `~/.pgpass`, never from the command line. "Telegram" is the one
allowlisted chat. Every procedure ends by recording what happened (check log below, or the
incident in the PR or issue tracker).

### Start and stop

1. Start (or bring up after a host reboot): `python scripts/hat_deploy_guard.py && $COMPOSE up -d`.
   Docker restarts the containers on its own after a crash (`restart: unless-stopped`); the guard
   is for planned starts.
2. Check: `$COMPOSE ps` shows `postgres` and `backend` healthy; `curl -fsS http://127.0.0.1:8000/ping`
   answers; Telegram `/status` answers. A backend that does not come up with
   `HatConfigError: fyers_proxy_set: <variable>` in `$COMPOSE logs backend` was refused because
   that proxy variable would carry Fyers traffic past the IPv4 pin: unset it for
   the backend, or add `fyers.in` to `NO_PROXY` (and to `no_proxy` if that is set too), and start
   again.
3. Stop (planned, outside the window): `python scripts/hat_deploy_guard.py && $COMPOSE stop backend`.
   The backend has 30 s to stop the feed, the scheduler and the engine: running job bodies get
   25 s, and any still running then are logged by name (look for `abandoned` in the log and
   check those jobs after the next start). Stop `postgres` only after the backend.
4. Logs: `$COMPOSE logs -f backend` (one JSON object per line).
5. Never `$COMPOSE down -v`: it deletes the database and data volumes.

### Morning login (daily Fyers login)

1. 08:30 IST: Telegram sends the Fyers login link (`login.prompt`). Open it and log in at Fyers.
2. Fyers redirects to the registered `REDIRECT_URI`; copy the `auth_code` from the redirected URL
   into the login dialog in the app header. Or send `POST /api/hat/auth/fyers/token` with the
   header `X-HAT-Request: 1` and the JSON body `{"auth_code": "<code>"}`. The token must be for the HAT Fyers account: a login
   with another Fyers user is refused once that account is onboarded.
3. Check: the header shows Fyers connected; `/status` shows the broker session live.
4. No entries are placed until the login is done; stops and exits that rest at the broker are not
   affected.

### Missed login

1. About 08:32 IST: if the session is still down, Telegram sends one `broker.session_lost` alert. 09:05 IST: Telegram sends the second prompt, URGENT when real positions are
   open, and each bot on the account is manage-only (`broker_health`)
   until the session is live. There are no entries until the login, and none that day if it
   never comes.
   Only the broker-side stops protect open positions: without a session there are no ticks, no
   reconciliation and no GTT changes, so breakeven, trim and the MA exits are off.
2. Late login: send `/login` in Telegram for a fresh link, then follow "Morning login". Entries
   resume from the next trigger after the session is live; missed triggers are not replayed.
3. If the token was revoked mid-day, `GET /api/hat/auth/fyers/status` shows disconnected: log in
   again the same way.

### First deploy: the admin command line

The setup and release steps the owner runs once per database, and again for each new strategy
version, through the local admin command line. `$ADMIN` stands for
`$COMPOSE exec -w /app/backend backend python -m app.hat.admin`. Each command runs in one
transaction and prints its result as JSON (ids included); a refusal prints `refused: <code>`,
exits 2 and writes nothing. `$ADMIN show` lists the strategies, versions and sleeves with their
ids at any point. Input files are names inside `<HAT_DATA_ROOT>/admin/` (copy the CSV or YAML
there first); a path leaving that folder, or another suffix (`.csv`, `.yaml`, `.yml`), is refused
(`file_outside_admin`). Run every step outside 08:30–15:35 IST on a trading day: the release steps
refuse inside a session (`market_hours`).

1. Prerequisites: the database is up (HAT starts with `HAT_ENABLED=true` only after step 3), the
   history build and the features have run, the trading calendar is loaded, and
   `HAT_TELEGRAM_CHAT_ID` is set (the owner's steps carry it).
2. `$ADMIN seed-policy`: risk-policy version 1, effective now. Safe to repeat.
3. Log in to Fyers ("Morning login"), then `$ADMIN onboard-account --account-ref <Fyers client
   id>`: the capability check (funds, positions, holdings) records the account `active` or
   `failed`. Note `broker_account_id`. A `failed` account takes no sleeve: fix the
   cause and run it again.
4. `$ADMIN seed-strategies`, then `$ADMIN seed-bots --account <broker_account_id>`: the two
   strategies with version 1 as a draft, and one sleeve per strategy in Research, in shadow mode.
   An account that is not `active`, or is another user's, takes no sleeve (`account_not_active`).
5. `$ADMIN publish-universe --constituents <ind_nifty500list.csv> --as-of <date>` (add
   `--include SYMBOL` or `--exclude SYMBOL` as often as needed): a universe version from NSE's
   index CSV, made live and given to every sleeve (`--bot ID` to name some). Every symbol must
   name an instrument on that date with the CSV's ISIN; otherwise the command lists the unknown
   ones (`SYMBOL(ISIN)` for an ISIN that differs) and writes nothing.
6. For each sleeve: backtest its strategy's version 1,
   `$COMPOSE exec -w /app/backend backend python -m app.hat.engine.backtest --bot <bot> --version
   <version> --from 2011-06-22 --to <last trading day> --for-promotion --regime-mult policy`; note `run_id`.
7. `$ADMIN publish-version --version <version> --run <run_id>` (draft to backtested),
   then `$ADMIN assign-version --bot <bot> --version <version>`: the sleeve now scans with it
   (an owner's act, recorded `sleeve.version_assigned` with the chat id).
8. `$ADMIN promote --bot <bot> --to backtested`: refused (`backtest_gate`) unless the run's DSR
   probability is at least 0.95 with 200 or more trades, its sessions span five years
   (`backtest_span_short`), the config parses, no known gap names a playbook the version enables,
   ambiguous days are within the cap, and it sized at the base weight under `policy`;
   the sleeve then stays in
   Research. Then `$ADMIN promote --bot <bot> --to shadow` and
   `$ADMIN shadow-version --version <version>` the same evening: the sleeve takes virtual entries
   sized from its nominal allocation and the 20 sessions start for both.
9. After 20 trading sessions: `$ADMIN promote-version --version <version>` (refused with
   `shadow_days` until then), then `$ADMIN promote --bot <bot> --to live` (refused
   with `version_not_live` before the version is promoted). The sleeve now has weight and asks
   for confirm (the default), but its orders stay virtual until every gate passes: GTT delivery on the account, the owner's DDPI confirmation
   (step 9a) and a policy version with `weights_confirmed = true`. `/status` shows the
   mode a bot runs and the gate that holds it back; `/mode <bot> auto` switches a sleeve to auto,
   where only a high-confidence signal is placed without a card.
9a. Once DDPI (or a POA) is active on the demat: `$ADMIN confirm-ddpi --account
   <account> --since <date DDPI became active> --note "<what was registered>"`, outside market
   hours (refused with `market_hours`, `ddpi_date_in_future`, `ddpi_note_required` or
   `ddpi_already_confirmed`); a bot reads it from the next session open. If DDPI is withdrawn at
   the depository, `$ADMIN revoke-ddpi --account <account> --note "<why>"` at any time, inside
   market hours too and without the trading calendar: the account's bots run shadow from their
   next routing decision.
10. A new version of a running strategy: `$ADMIN save-draft --strategy <strategy> --config
    <file.yaml>` (an exported or edited config; refused inside a session, and an invalid config
    prints the paths and codes of its problems), then steps 6, 7 (publish only: a
    Live sleeve takes only a live version) and `shadow-version`; after 20 sessions,
    `promote-version` moves the sleeves of the version it replaces onto it (their open trades keep
    their entry version). To shadow the new version on a sleeve of its own beside
    the Live one: `$ADMIN add-sleeve --strategy <strategy> --account <account>` (a sleeve in
    Research), then `$ADMIN assign-version --bot <new sleeve> --version <version>` and step 8's
    two `promote` commands for it (`backtested`, then `shadow`). The version is published and
    put in shadow once, as above: `publish-version` and `shadow-version` are not run again for
    the sleeve (a second `shadow-version` is refused, `not_backtested`). It is refused Live
    (`strategy_has_live_sleeve`) while another sleeve of the strategy is Live or in Probation:
    retire that one first.
11. A sleeve in Probation that has recovered: `$ADMIN promote --bot <bot> --to live` (refused
    with `still_failing` while it is not).
11a. A position locked by a corporate action HAT cannot rescale (URGENT `corporate_action.unhandled`,
    ): `/flatten` a real one (it sells at most the shares the broker holds). A virtual
    one, outside market hours: `$ADMIN resolve-action --position <position> --action
    <ex-date>:<type> --close-at <price>` closes it at the ex-date's adjusted price (a real one is
    refused, `real_position_flatten`).
12. Record each step in the check log.

### Deploy and planned restart: `ops.deploy_window`

1. Pick a time outside 08:30–15:35 IST on a trading day (weekends and NSE holidays are free).
   A release that adds columns to `hat_allocation_daily`
   goes out after the session and before the 18:00 `allocate` of a trading day: rows written
   before the columns existed have none, so the next session's virtual entries of Shadow and
   Probation sleeves would fail closed (`no_allocation`) until the night after.
2. Run the guard: `python scripts/hat_deploy_guard.py`. Exit 2 means refused. It treats every
   weekday as a trading day unless given `--calendar` with an export of `hat_trading_calendar`:
   `$COMPOSE exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -At -F, -c "select date, is_trading from hat_trading_calendar" > /tmp/hat_calendar.csv`.
3. Deploy: `git pull` (or check out the release), then
   `python scripts/hat_deploy_guard.py && $COMPOSE up -d --build`.
4. Emergency deploy inside the window only for a fix that cannot wait:
   `python scripts/hat_deploy_guard.py --force --reason "<why>"`. The forced run is appended to
   `<HAT_DATA_ROOT>/ops/deploy_guard.jsonl`; copy the reason into the check log.
5. After the start, follow "Restart recovery: reconcile, then resume".

### Restart recovery: reconcile, then resume

Covers every restart: planned, a crash, or the machine coming back after being down.

1. On start the scheduler marks any run left `running` by the previous process as interrupted, so
   it can run again (`recover_interrupted`); jobs resume from `hat_job_run` and a job that
   already succeeded for today is not run again.
2. Before any entry, every account's book is reconciled with the broker's order and trade books
   and its fills are read. A broker that cannot be read, or an HT order HAT does
   not know, places no entry and sends an alert; stops and sales still go out (D118). Entries left unplaced by the previous process
   (an auto signal in Triggered, a Take whose order never went out) are placed once the
   feed is fresh, and only on their own trade date (the next date's sweep ends them Rejected
   `signal_expired`); a restart after 08:45
   re-arms the day from the stored 5-minute bars and the recorded gap scan.
3. Check on Telegram: `/status` (session live, kill switches, feed), `/positions` (every open real
   position shows its stop). If the session is not live, follow "Morning login".
4. If a `reconcile.mismatch` alert arrives, follow "Reconciliation mismatch".

### Reconciliation failed or a row skipped

1. Alert `reconcile.failed` (account): the first reconciliation of the trade date failed (the
   broker could not be read, or the database failed; the backend log says which). No entry opens on that account until one succeeds (the 5-minute pass retries);
   stops and sales still go out. Check the Fyers session (`/status`, "Morning login").
2. Alert `reconcile.unreadable_row` (rows): order rows without an HT tag (manual or foreign
   orders) that HAT could not read were skipped; the reconciliation went on. Check the Fyers
   order book for an odd manual order. An HT-tagged row HAT cannot read fails the run instead.

### Failure: Machine down

1. While the host is down, broker-side stops (intraday SL-M or GTT) protect every open position;
   nothing else is needed during market hours.
2. Bring the host back, then `$COMPOSE up -d` (no guard needed for an unplanned outage; record it).
3. Follow "Restart recovery: reconcile, then resume", then check missed jobs as in "Failed nightly
   job".

### Failure: Data missing or fails the quality gate

1. Alert: `quality.failed` or `ingest.failed`. The data-health kill makes the affected bots
   manage-only: no new scans or entries; exits and stops continue.
2. Look at the job rows: `select job, trade_date, status, error_code from hat_job_run where trade_date = current_date order by started_at;`
   and the backend log for the failing job.
3. Fix the cause (source late, MCP endpoint down, a bad row). The scheduler retries a failed job
   10 minutes after each failed run while it is still that date (a chained job such as the gate
   until 08:30 the next morning); a retry that fails the same checks sends no second alert. Once
   the gate passes, the health kill clears on
   its own and the chain (features, regime, allocate, scan, watchlist) continues; `ca.adjust`
   does not wait for the gate.
4. Do not re-arm by hand: a data-health kill re-arms automatically when the check passes.

### Alert: `quality.quarantined`

1. One stock moved more than 20% with no stored corporate action. The gate passed; the stock is
   out of every scan from that day on. The other stocks scan as usual.
2. Look for the reason: a split or bonus not yet loaded, or real news. `python -m
   app.hat.data.quarantine list` shows every open quarantine.
3. A corporate action with that ex-date, once stored, releases the quarantine at the next gate
   (`ca.adjust` asks the MCP for it again every night). For real news (no corporate action), clear it once you are satisfied the price is right:
   `python -m app.hat.data.quarantine clear SYMBOL`. The stock is back in the next night's scans.
4. More than 1% of the universe unexplained in one day fails the gate instead (`quality.failed`,
   check `big_moves`); nothing is quarantined. Treat it as a bad data file, as above.

### Failure: 22:00 fallback fails

1. Alert at 22:00: neither the MCP nor the NSE bhavcopy download has the day. No scan runs; bots
   stay manage-only for the next session.
2. Check NSE's site for a delayed publication and the backend log for the fallback's error code.
3. When the file is out, a failed `ingest.nightly` is retried by the scheduler while it is still
   the same date. After midnight the scheduler does not run a past trade date again; the next
   night's ingest runs as usual and the missed day stays a gap: record it as an incident.
   **[OWNER-PENDING]** a CLI to re-run one job for a past trade date.

### Failure: Broker session drops

1. HAT retries with backoff (1 s doubling to 30 s); after 2 minutes Telegram gets one
   `broker.session_lost` alert. Bots on that account are manage-only; stops rest at
   the broker.
2. Check Fyers status and the host's internet link. If the token expired or was revoked, log in
   again ("Morning login").
3. If the host is on the 4G link, see "Failover drill": no entries until the static line returns.
4. Recovery is automatic once the session is back; check `/status`.

### Broker call limits: `broker_rate_limited`

1. Every Fyers call takes a slot from one call budget: by default 8 calls a
   second, 180 a minute and 5,000 a day (the Standard plan). Stops and orders go first; reads
   get 80% of each window. A call refused by the budget or answered HTTP 429 is
   `broker_rate_limited`: nothing was sent, and the fill sync reads again at its next step.
2. After a plan change (Fyers Prime, D154), set `HAT_BROKER_CALLS_PER_SECOND`,
   `HAT_BROKER_CALLS_PER_MINUTE` and `HAT_BROKER_CALLS_PER_DAY` (`off` for no daily cap) in the
   env file to the new plan's limits and restart. Do not raise them above the plan's limits.
3. The day count is kept in the process and restarts at zero on a restart, so after a restart
   late in the day the broker's own -360 may come first; it is also `broker_rate_limited`.

### Failure: Feed stale

1. Alert: `feed.stale`. Bars and triggers stop for the stale symbols; broker-side stops are not
   affected; MA exits at 15:20 use the live price, so a feed still stale then is an incident.
2. Check the internet link and Fyers status; `$COMPOSE logs backend | grep feed`.
3. If the socket does not come back within a few minutes, restart the backend (a planned restart
   inside the window needs `hat_deploy_guard.py --force --reason "feed stale"`), then follow
   "Restart recovery: reconcile, then resume".

### Failure: Order rejected

1. Alert: `order.rejected` with its reason. HAT retries a rejected order at most once; a second rejection ends the signal.
2. Read the reason: price band, margin, freeze, session, unregistered IP (see "Failover drill").
3. Fix the cause; do not place the order by hand (there is no manual trading). A rejected stop is
   placed again by the next fill sync (`stop.not_placed`); if it keeps failing, see "GTT missing".

### Failure: Duplicate trigger

1. A second trigger for the same bot, playbook, instrument and trade date places no second order:
   the signal id is the idempotency key. Nothing to do unless an alert says a
   second order reached the broker.
2. If one did: `/kill`, then compare `/positions` with the Fyers order book and treat it as
   "Reconciliation mismatch". Record it as a bug.

### Failure: Close vs live price

1. Known gap, not an incident: MA exits at 15:25 use the live price, NSE's official close is the
   last-30-minute weighted average. Each evaluation records `exit.close_evaluated`; after the
   nightly ingest `exit.close_gap` records the gap.
2. Weekly in shadow mode: review the gaps (`select payload from hat_event where event_type = 'exit.close_gap' order by created_at desc limit 50;`)
   and change the exit rule if exits are systematically biased.

### Kill switch and re-arm via Telegram

1. Kill: send `/kill`. Every bot becomes manage-only and pending approvals are cancelled; exits
   and stops keep running. Drawdown and data-health kills trip on their own.
2. Before re-arming: find why it tripped (`/status`, the `kill.tripped` event), fix it, and check
   `/positions` shows every open position with its stop.
3. Re-arm: send `/arm`, then press Confirm on the confirmation card (second confirmation,
   ). `kill.rearmed` is recorded. `/resume` does not lift a kill switch.
4. A data-health kill re-arms automatically once the data check passes; do not `/arm` it.

### Flatten

1. Only to get out of everything now: send `/flatten`, then press Confirm. HAT sells every open
   real position through exit-only orders and records `flatten.executed` as a discipline breach.
2. Check `/positions` and the Fyers positions page are both empty; `/kill` too if no new entries
   should follow.

### Reconciliation mismatch

1. Alert: `reconcile.mismatch` (kind, tag or symbol, bot). Entries for that bot are blocked; HAT
   never auto-closes or adopts a position.
2. Compare the Fyers order book, trades and positions with `/positions` and `hat_order`.
3. A broker position HAT does not know: close or keep it at Fyers yourself; it is not HAT's.
   A HAT position missing at the broker or a quantity difference: find the fill or cancel that HAT
   missed (backend log, Fyers trade book) and record it as a bug.
4. The block clears at the next reconciliation that matches. Record the incident.

### GTT missing

1. Alerts: `gtt.missing`, `gtt.unverified`, `gtt.swap_failed` or `stop.not_placed`. `gtt.verify`
   at 23:00 refreshes and re-places GTTs (and renews one 300 days old); `gtt.swap` at 15:25
   retries every minute and keeps the intraday stop until the GTT is confirmed. With
   `HAT_EXDATE_GTT_CHECK` on, `gtt.ex_date` at 08:40 re-places a GTT the broker cancelled before
   an ex-date (code `gtt_cancelled_by_broker`); a refused one is covered by the intraday stop from
   09:15.
2. Check the Fyers GTT page against `/positions`: every open real position needs one GTT at its
   current stop.
3. If the broker keeps refusing: check the session (log in again), the price band and the
   quantity. A position that stays unprotected overnight is an incident: decide whether to
   `/flatten` it the next morning.

### Series change

1. Alert: URGENT `position.series_changed` (symbol, old and new series, Fyers symbol or
   `unmapped`), at 08:40 (with `HAT_EXDATE_GTT_CHECK` on) and at every 23:00 `gtt.verify`.
2. The position is manage-only: no exit rule runs on it, and its broker stops go out under the new
   series' Fyers symbol (a GTT still under the old symbol is cancelled and placed again).
   `unmapped` means no Fyers symbol is stored for the new series (BZ has none): no stop is sent
   (`stop.not_placed`, code `series_unmapped`) and a GTT under the old symbol is left as it is.
3. Decide whether to keep it at Fyers yourself or `/flatten` it. The alert repeats until it is
   closed.

### Sale unfilled

1. Alert: URGENT `sale.unfilled` with a code: `sale_resting` (a market sale
   was sent again `HAT_MPP_PAGE_AFTER` times under market price protection and still rests, only
   with `HAT_MPP_RESEND` on), `lower_circuit_locked` (the last trade is at the lower circuit: the
   sale waits in the queue and is not sent again while locked) or `band_limit` (a sale refused
   twice went once more as a limit at the day's lower circuit price).
2. Check the Fyers order book for the sale and the price band. A locked stock may not trade all
   day; the sale stays in the queue and expires at the close, and the next session sells again.
3. Decide whether to `/flatten` (it waits for a working sale) or sell yourself at Fyers.

### Sale settled from holdings

1. Alert: `sale.settled_from_holdings` with a code `sold` or `unknown`, `qty_from` and `qty_to`.
   A sale of an earlier session whose answer was lost (the app was down at the close, or the book
   was unreadable) was settled from the Fyers holdings: the shares no longer held are taken as
   sold, the position's open quantity dropped to the shares held (`position.qty_corrected`), and
   its exits go on with that quantity.
2. Those shares' sale price is not in today's book, so the position's P&L misses them. Check the
   Fyers tradebook or contract note of that day. A position left at 0 shares stays open,
   with no trade result, and sells nothing.

### Failed nightly job

1. Alert or `/status` shows a failed job. List today's runs:
   `select job, status, error_code, started_at, finished_at from hat_job_run where trade_date = current_date order by started_at;`
2. A failed run is retried by the scheduler every 10 minutes while it is still that date; jobs
   after it in the chain wait for it. Exits and stops never wait.
3. Fix the cause from the error code and the backend log, then watch the next retry succeed.
4. A run stuck in `running` after a crash is reaped at the next start; restart only outside the
   window (deploy guard).

### Gap-scan latency: `gapscan.latency_observed`

1. For the first two weeks of live data, each trading day after 09:20, read the `gapscan` run:
   `select started_at, finished_at from hat_job_run where job = 'gapscan' and trade_date = current_date;`
2. Record the finish time in the check log. A finish at or after 09:20:00 also raises a health
   alert. If it is late on more than one day, raise the scan's latency assumption
   with the owner.

### Backup and restore drill: `ops.backup_restore_drill`

Nightly backup: a systemd timer on the host runs, at 23:30 IST, as root (it reads
the Docker volume) with the `.env` variables loaded (`EnvironmentFile=`):

```
python scripts/hat_backup.py run \
  --local "$HAT_BACKUP_LOCAL" --remote "$HAT_BACKUP_REMOTE" \
  --data-root "$(docker volume inspect -f '{{ .Mountpoint }}' hat_hat_data)"
```

`HAT_BACKUP_LOCAL` is the external disk and `HAT_BACKUP_REMOTE` a mounted cloud bucket. The run
exits 1 when any file is missing or differs at either target, and the timer's `OnFailure=` unit
sends the alert. pg_dump must be version 18 (or pass `--pg-dump` with the postgres container's,
see the script's help).

Monthly drill:

1. Pick the latest set on the remote target and verify it: `python scripts/hat_backup.py verify "$HAT_BACKUP_REMOTE/<set>"`.
2. Restore the dump into a scratch database:
   `createdb -h 127.0.0.1 -U "$DB_USER" hat_restore_drill` and
   `pg_restore -h 127.0.0.1 -U "$DB_USER" -d hat_restore_drill --no-owner "$HAT_BACKUP_REMOTE/<set>/hat.dump"`.
3. Compare with the live database (both queries on each): `select count(*) from hat_event;` and
   `select max(trade_date) from hat_job_run where status = 'succeeded';`. The restored counts
   equal the live ones as of the backup time (the live event count can only be larger).
4. Extract the data tar into a scratch directory (`tar -xf .../hat_data.tar -C /tmp/hat_restore`)
   and check the newest Parquet partition and raw zip are there.
5. Clean up: `dropdb -h 127.0.0.1 -U "$DB_USER" hat_restore_drill` and remove the scratch
   directory. Never point the backend at the scratch database. Record the drill in the check log.

### Restore from backup

1. Stop the backend (`$COMPOSE stop backend`); outside the window unless the database is lost.
2. Verify the set (`hat_backup.py verify`). Recreate the database:
   `$COMPOSE exec -T postgres dropdb -U "$DB_USER" "$DB_NAME"`, `... createdb -U "$DB_USER" "$DB_NAME"`,
   then `pg_restore -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" --no-owner <set>/hat.dump`.
3. Restore the data root into the volume:
   `$COMPOSE run --rm --no-deps -v "<set>:/restore:ro" backend tar -xf /restore/hat_data.tar -C /data/hat`.
4. Start the backend and follow "Restart recovery: reconcile, then resume". Positions changed at
   the broker since the backup show as reconciliation mismatches: resolve each one.

### Failover drill: `ops.failover_drill`

1. Quarterly, outside market hours with no order expected: switch the host to the 4G/5G link.
2. Confirm that broker order calls are refused (the failover IP is not registered with the Fyers
   app) or not attempted, that a health alert reaches Telegram over the 4G link, and that every
   open position still shows its GTT on the Fyers GTT page.
3. Switch back to the static line, check `/status`, and record the drill.
4. In a real failover during market hours: no new entries until the static line returns; do not
   force entries; broker-side stops cover the positions.

### Host checklist: `ops.host_checklist`, `host_static_ip_ups_failover`

Before confirm or auto mode is enabled on any bot, and after any host change:

1. Linux host, NTP synchronised with an offset under 1 s: `timedatectl` shows
   `System clock synchronized: yes`; `chronyc tracking` (or `ntpq -p`) shows the offset.
2. The ISP static IP is the one registered with the Fyers app: `curl -fsS https://ifconfig.me`
   from the host, compared with the Fyers app settings.
3. The UPS carries the host and router through a pulled plug for at least 10 minutes; the 4G link
   comes up on its own when the static line is unplugged ("Failover drill").
4. The dashboard and API are reachable only over Tailscale/WireGuard: from outside the VPN,
   ports 8000, 3000 and 5432 do not answer; `docker compose ps` shows every port on 127.0.0.1.
5. Record each item with its value in the check log.

### Env file permissions: `ops.env_permissions`, `env_file_permissions`

After any change to `.env`:

1. `chmod 600 .env` and `chown <service-user>: .env`; check with `stat -c '%a %U' .env`
   (expect `600 <service-user>`).
2. The disk holding the repository and `.env` is encrypted (LUKS): `lsblk -f` shows `crypto_LUKS`
   under it.
3. `git status --ignored .env` shows it ignored; `.env` is never copied into an image (the
   `.dockerignore` files keep it out of the build context) or a backup set.
4. Record the check in the check log.

### Check log

| Date | Check | Initials | Result |
| --- | --- | --- | --- |

