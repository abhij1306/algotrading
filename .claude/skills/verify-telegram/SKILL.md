---
name: verify-telegram
description: How to run, read and update the checks behind Hire a Trader's Telegram bot (long polling with an at-most-once offset, the allowlisted chat and the command log, the ten commands and their second confirmations, signed single-use approval buttons, the event relay, health alerts, login prompts and the after-close reports, the owner-only auto mode). Use before and after any change to backend/app/hat/telegram, the Telegram rig or properties, test_conf_telegram.py, or the Telegram parts of the ApprovalToken, KillSwitch, ExecutionRouter and JobScheduler models.
---

# verify-telegram

Checks that the bot answers only the allowlisted chat and logs every update once; that a crash never replays an update; that the ten commands are the only text the bot acts on and none of them reaches an order; that auto, re-arm and flatten need a second confirmation in time; that a Take works only through a button the service signed for that card, before it expires, within max-chase; that no token, secret or broker text reaches a message, a log or the database; and that alerts, prompts and reports say what the database says. Specs: `docs/specs/hat/08-telegram.md` (HAT-TG-005..044), HAT-DEC-I11, I13, T15, B12, HAT-GOV-080..082, HAT-OVR-061, HAT-SCH-056, 065, 074, HAT-EXEC-043, HAT-OPS-071.

## Properties

- `TLA ApprovalToken!SecondConfirmRequired` Auto, a re-arm and a flatten act only on a Confirm pressed in the allowlisted chat, before its expiry, once per ask. Harm: one mistyped message switches to real-money auto, re-arms after a kill, or sells everything.
- `TLA KillSwitch!FlattenNeedsConfirm` A flatten is sent only after `/flatten` asked and its Confirm was pressed. Harm: one message sells every position.
- `TLA KillSwitch!FlattenExitOnly` A flatten only lowers open quantity, even under a tripped kill switch. Harm: flatten opens or grows a position.
- `TLA ExecutionRouter!NoEntryWithoutSession` No entry is placed for an account with no live broker session that trade date. Harm: orders go out on an expired session and fail or land unseen.
- `TLA ExecutionRouter!RejectNoCard` A rejected signal never produces an approval card. Harm: the owner is asked to approve a signal the risk authority refused.
- `TLA ExecutionRouter!UserCannotRaiseRisk` A user action only takes or skips an engine-sized entry: it never creates an entry or changes its size or stop. Harm: discretionary edits bypass the risk policy.
- `TLA JobScheduler!LoginPromptOrder` The 08:30 prompt goes out only on a trading day, and the 09:05 reprompt only for accounts still logged out. Harm: noise trains the owner to ignore prompts, or a missed login blocks the day.
- `TLA JobScheduler!ReportsAfterClose` The rejection digest, the daily report and the veto digest go out only after the 15:30 close of a trading day. Harm: a report built mid-session shows a day that is not over.
- `PBT telegram.health_alert_for_every_failure` Every `quality.failed`, `feed.stale`, `broker.session_lost` and `order.rejected` event is exactly one health alert; every other service alert is one message; GTT alerts are URGENT. Harm: the owner does not learn that a bot went manage-only or an order failed.
- `PBT telegram.unknown_chat_has_no_effect` An update from any other chat writes one refused `hat_command_log` row (`unknown_chat`) and nothing else: no reply, no broker call. Harm: a stranger controls the bots.
- `PBT telegram.every_command_logged` Every message is one `hat_command_log` row with the sender's chat id; a refused one has a reason and changed nothing. Harm: discipline metrics and audits miss user actions.
- `PBT telegram.unknown_command_rejected` Text that is not one of the ten commands with its arguments is refused and changes nothing. Harm: malformed input triggers an action.
- `PBT telegram.other_bot_ignored` A command addressed to another bot (`/kill@other_bot`) is refused (`other_bot`) with no reply, no state change and no broker call; suffixed with this bot's `getMe` username in any case it acts as the bare command; while `getMe` fails every suffixed command is refused. Harm: a kill, flatten or re-arm meant for another bot in the group acts on this one.
- `PBT telegram.no_free_text_orders` No text, even a Take button's data typed as a message, places an order or writes an entry `hat_order`. Harm: a typo or a forged message opens a position.
- `PBT telegram.offset_at_most_once` Across any crash point, no update is handled twice and every update after the persisted offset is handled. Harm: a replayed confirmation or Take acts twice; a dropped `/kill` does not act.
- `PBT telegram.user_events_attributed` Every event a command or button records has `actor = 'user'` and the chat id, and every accepted change records one. Harm: skips, pauses and flattens are missing from the discipline metrics.
- `PBT telegram.no_secret_in_output` No message or log record holds the bot token, the approval secret, an issued token or broker error text. Harm: a leaked secret lets an outsider control the bot or forge approvals.
- `PBT telegram.send_only_to_allowlist` Every `sendMessage` and `editMessageReplyMarkup` goes to the allowlisted chat; another chat is never answered. Harm: trade details and buttons reach someone else.
- `PBT telegram.login_prompt_links` 08:30 sends one link per active account once; 09:05 lists exactly the accounts with no live session; nothing on a non-trading day. Harm: a missed login blocks the day, or noise is ignored.
- `PBT telegram.watchlist_digest_content` The digest's new and dropped symbols are the set differences and the top list is the first min(10, n) by score. Harm: the owner acts on a wrong summary.
- `PBT telegram.card_matches_signal` The card or shadow message shows the `hat_signal` row's trigger, stop, qty and risk exactly; a card offers exactly Take and Skip, signed for that signal. Harm: the owner approves a trade other than the one placed.
- `PBT telegram.report_content` The 15:35 digest lists exactly the day's `signal.rejected` signals with reasons; the 15:40 report shows each bot's snapshot; every real fill, partial, close and stop move is one message. Harm: rejections, drawdowns and money moving go unseen.
- `PBT telegram.mode_outside_hours` `/mode` is refused in market hours and for unknown values, writing nothing; an accepted one queues for the next session open. Harm: switching to auto mid-session.
- `PBT telegram.second_confirm` Auto, re-arm and flatten act only on a Confirm pressed in time, in the allowlisted chat, at most once. Harm: one message switches to auto or sells everything.
- `PBT telegram.flatten_logged` A confirmed `/flatten` only sells, at most the open qty, even under a kill; it writes one `flatten.executed` with `actor = 'user'` and the chat id. Harm: flatten grows a position or the breach is invisible.
- `PBT telegram.read_commands_side_effect_free` `/status`, `/positions`, `/watchlist` and `/login` change no table but the chat log and call no broker. Harm: a status check changes trading state.
- `PBT telegram.token_forgery_rejected` A callback the service did not issue for that card is refused with no state change. Harm: a forged callback approves a real-money entry.
- `PBT telegram.expiry_window` The card expires 5 minutes after it is sent; a later Take is refused and places nothing. Harm: the owner approves a stale setup.
- `PBT telegram.take_chase_recheck` A Take places only within max-chase and when the stock is known not locked at the upper circuit (unknown fails closed). Harm: a tap buys far above the planned entry.
- `PBT telegram.token_never_stored` No column holds an issued card or button token; `token_hash` is its sha256. Harm: anyone with database access can approve trades.
- `PBT telegram.relay_late_commit` An event whose transaction commits after the relay passed its id is relayed once if it commits within `GAP_WAIT` (an hour), and given up after; the events around it go out once each. Harm: a health alert or approval card held by a slow transaction is lost for good.
- `PBT telegram.veto_digest_on_week_end` The weekly veto digest goes out on a trading day exactly when no later weekday of its week may still trade (a calendar holiday does not; a day the calendar lacks may). Harm: a daily "weekly" digest once the calendar runs out, or none in a holiday week.
- `PBT telegram.token_never_persisted` No table and no HAT log record holds a raw token; the stored hash is the card token's. Harm: database or log access can approve trades.
- `CONF telegram_command_set` HAT-DEC-I11: the accepted commands are exactly the 08 §3 table. Harm: an undocumented command changes state outside governance.
- `CONF telegram__command_catalogue` HAT-TG-030: `HANDLERS`, `_ARITY` and `COMMANDS` name the same ten commands. Harm: an unreviewed control path.
- `CONF gov__lever_catalogue` HAT-GOV-080: no command, button or API route sets price, size or stop or exits one position; Take and Skip carry only the signal. Harm: discretionary edits bypass the risk policy.
- `CONF no_override_command` HAT-DEC-B12: no command, route or strategy config field overrides the regime gate. Harm: a discretionary override reintroduces the risk the gate removes.
- `CONF telegram__no_webhook_no_listener` HAT-TG-010: the package binds no socket, imports no server and names no setWebhook; the client refuses any method but the four outbound ones before a request. Harm: an exposed port on the trading box.
- `CONF telegram__no_order_import_in_text_path` HAT-TG-031: no module of the text path reaches the router, submission, the trade manager, the adapter, trades, approvals or the risk authority. Harm: free text reaches an order.
- `CONF auto_mode_single_user` HAT-OVR-061: no API route reaches the mode code, and `/mode auto` (and its Confirm) is refused for a bot whose account is not `HAT_USER_ID`'s. Harm: orders for someone else's account without SEBI empanelment.
- `PBT telegram.watchlist_digest_once` A `watchlist.digest` retry after a partial send failure sends only the digests not yet sent for the day's latest publish. Harm: the owner gets the same digest on every retry.

## Run

```bash
export JAVA_HOME=/path/to/jdk   # Java 17+
verification/tla/run.sh ApprovalToken && verification/tla/run.sh --broken ApprovalToken
verification/tla/run.sh KillSwitch && verification/tla/run.sh --broken KillSwitch
verification/tla/run.sh ExecutionRouter && verification/tla/run.sh --broken ExecutionRouter
verification/tla/run.sh JobScheduler && verification/tla/run.sh --broken JobScheduler
cd backend
CI=1 python -m pytest tests/hat/properties/test_telegram.py -q --no-cov   # FakeTransport, FakeBroker, SQLite
python -m pytest tests/hat/conformance/test_conf_telegram.py -q --no-cov
```

No check calls Telegram: `FakeTransport` (`backend/tests/hat/hat_telegram_support.py`) stands in for the Bot API and records every call, and `HttpsTransport` is only ever given a recording `post`. The approval secret in the rig is a fixed test key; the real one comes from `HAT_APPROVAL_SECRET`.

## Bug or model error

- A failing property prints a falsifying example. Replay it on `TgRig`: `rig.service.handle(text_update(...))` or `button_update(...)`, then read `rig.transport.calls`, `rig.log()` and `rig.places()`.
- A TLC counterexample names its actions; each has a `\* code:` comment naming the function (`confirm.py:ask` and `take`, `commands.py:_flatten`, `manage.py:flatten`, `relay.py:message_for`, `commands.py:run_command`, `reports.py:login_prompt` and `daily_report`, `ops/scheduler.py:tick`). If the code can take the steps it is a bug; if a guard the model omits stops it, add the guard to the model and say so under "Scope changes".
- Never weaken a property or shrink a strategy without a reason under "Scope changes".
- Known windows, kept visible: the confirmation nonces live in memory, so a restart drops a pending Confirm (it must be asked again); a crash between committing the offset and handling an update drops that one update (at most once, by design: HAT-TG-011).

## Updating

- A new command: the 08 §3 table first, then `COMMANDS`, `_ARITY` and `HANDLERS` together (`telegram_command_set` and `telegram__command_catalogue` compare them), a property for its effect, and a scratch break.
- A new button: a prefix in `tokens.py` or `confirm.py`, its dispatch in `callbacks.run_callback`, and `gov__lever_catalogue` (it lists the buttons and their arguments).
- A new Bot API method: add it to `ALLOWED_METHODS` only if it is outbound and needed; `telegram__no_webhook_no_listener` pins the set.
- A new scheduled message: a job in `reports.telegram_jobs`, its window in `JobScheduler.tla` (`Prompt`, `Reprompt`, `Report`), a broken variant, and a property in `test_telegram.py`.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-telegram` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/telegram/callbacks.py`
- `backend/app/hat/telegram/client.py`
- `backend/app/hat/telegram/commands.py`
- `backend/app/hat/telegram/config.py`
- `backend/app/hat/telegram/confirm.py`
- `backend/app/hat/telegram/notifier.py`
- `backend/app/hat/telegram/reads.py`
- `backend/app/hat/telegram/relay.py`
- `backend/app/hat/telegram/render.py`
- `backend/app/hat/telegram/reports.py`
- `backend/app/hat/telegram/service.py`
- `backend/app/hat/telegram/store.py`
- `backend/app/hat/telegram/tokens.py`

Checks:
- `backend/tests/hat/hat_telegram_support.py`
- `backend/tests/hat/properties/test_telegram.py`
- `backend/tests/hat/conformance/test_conf_telegram.py`

Models (their Telegram actions):
- `verification/tla/ApprovalToken.tla`, `verification/tla/KillSwitch.tla`, `verification/tla/ExecutionRouter.tla`, `verification/tla/JobScheduler.tla`
- `verification/tla/broken/ApprovalToken__act_without_confirm.cfg`, `verification/tla/broken/ApprovalToken__confirm_twice.cfg`, `verification/tla/broken/ApprovalToken__confirm_after_expiry.cfg`, `verification/tla/broken/ApprovalToken__other_chat.cfg`
- `verification/tla/broken/KillSwitch__flatten_without_confirm.cfg`, `verification/tla/broken/KillSwitch__flatten_opens.cfg`
- `verification/tla/broken/ExecutionRouter__no_session_check.cfg`, `verification/tla/broken/ExecutionRouter__card_on_reject.cfg`, `verification/tla/broken/ExecutionRouter__user_resizes.cfg`, `verification/tla/broken/ExecutionRouter__user_creates_entry.cfg`
- `verification/tla/broken/JobScheduler__prompt_holiday.cfg`, `verification/tla/broken/JobScheduler__reprompt_logged_in.cfg`, `verification/tla/broken/JobScheduler__report_before_close.cfg`, `verification/tla/broken/JobScheduler__report_holiday.cfg`

`PBT telegram.retention_purge` lives in `verify-events`, with the retention job.

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `17be6609c80b16a748f4462b2da81668b6fcb939`
Covered digest: `6c48bbdfc76086cf2de21e49c167227d0bd4f394d4bbbbd5f9058daa19b9164f`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 16 built this skill. The properties run one bot (confirm unless a property needs auto) on one account over SQLite and `FakeBroker`, with the HTTP layer faked; `SLOW` properties run 15 examples.
- `telegram.report_content` first routed its refusals on a confirm bot, where the router posts a card and checks only at the Take, so no signal was ever rejected and the digest assertion was vacuous; the scratch break found it. It now routes them on an auto bot, one instrument per refusal (a signal a day per instrument), and asserts as many digest lines as refusals.
- `telegram.user_events_attributed` and `telegram.send_only_to_allowlist` gained an `@example` each (a confirmed flatten; a Take on a live card, which clears its buttons): in 15 examples the generator reached neither reliably and the scratch breaks went unseen.
- `telegram.mode_outside_hours`: the market-hours rule is checked twice, in `commands._mode_refusal` and in `risk/modes.plan_mode_change`; the break removes both, since either alone holds the property.
- `CONF auto_mode_single_user` found that auto mode was not bound to `HAT_USER_ID`; `/mode auto` and its Confirm now refuse (`auto_not_owner`) a bot whose account belongs to anyone else. The rig's account belongs to the configured owner.
- `TLA ApprovalToken!SecondConfirmRequired` models the confirmation of a lever (auto, re-arm, flatten), not the approval card; the card's signature and single use are checked on the real code (`telegram.token_forgery_rejected`, `telegram.expiry_window`).
- Review round (before the first push): sales of one position are now serialised in `TradeManager` (`_sale_lock`, held from the "nothing else is selling" check until the order is sent), because the Telegram flatten runs on its own thread beside the tick path; `ManagerFlattener` must be given the same `TradeManager` instances the live exits use (PR 17 wiring). A flatten confirmed outside the session is refused (`market_closed`) with nothing recorded, and one position or account that fails no longer stops the others (`FlattenReport.failed`); `telegram.flatten_logged` now asserts the refusal after 15:30. The poller survives any error, a refused button answer no longer loses the reply, an unrenderable event is skipped instead of holding back the relay, a Take whose placement was refused says so, `route_by_mode` refuses confirm bots (their cards need the router's signer), and `/mode` on a day the calendar lacks says `no_calendar`. `telegram.relay_late_commit` and `telegram.veto_digest_on_week_end` were added for the relay gaps and the week-end rule, which had no check.
- `TgRig` uses `OpenFlattener` (no positions, session open) instead of an empty `ManagerFlattener`; the real `ManagerFlattener` over `TradeManager` runs in `telegram.flatten_logged`.
- `/cmd@other_bot` (found by a CodeAnt nitpick on PR #113): the `@bot` suffix was dropped without checking it, so in a group `/kill@another_bot` ran here. The client now also calls `getMe`; the service asks it once, when the first suffixed command arrives, and keeps the username. `telegram.other_bot_ignored` was added, and the method-allowlist CONF check now lists five methods.
- Sonar round (PR #113): `COMMANDS` is now `tuple(_ARITY)`, so the command set is written once; the token decoder catches `ValueError` alone (`binascii.Error` is one). Behaviour is unchanged and every check here passed again. The `logging.exception` suggestions were declined: a traceback can carry broker or database text, so failures log only the exception type (HAT-TG-014).

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model (HAT-VER-072), then the break was removed and the check passed again. TLA breaks are the models' `BrokenMode` variants, run by `run.sh --broken`; the state lines are the last steps of TLC's counterexample.

### TLA ApprovalToken!SecondConfirmRequired

Break: broken variants `ApprovalToken__act_without_confirm`, `__confirm_twice`, `__confirm_after_expiry` and `__other_chat` (the model's `BrokenMode`): the lever acts with no Confirm, a Confirm acts twice, after its expiry, or from another chat.

```
Error: Invariant SecondConfirmRequired is violated.
State 2: <Ask line 137, col 5 to line 140, col 25 of module ApprovalToken>
State 3: <Confirm line 145, col 5 to line 155, col 25 of module ApprovalToken>
State 4: <Confirm line 145, col 5 to line 155, col 25 of module ApprovalToken>
```

### TLA KillSwitch!FlattenNeedsConfirm

Break: broken variant `KillSwitch__flatten_without_confirm.cfg`: `ConfirmFlatten` runs with no pending ask.

```
Error: Invariant FlattenNeedsConfirm is violated.
Error: Invariant FlattenNeedsConfirm is violated.
State 1: <Initial predicate>
State 2: <ConfirmFlatten line 301, col 5 to line 311, col 49 of module KillSwitch>
```

### TLA KillSwitch!FlattenExitOnly

Break: broken variant `KillSwitch__flatten_opens.cfg`: `ConfirmFlatten` may add to a position.

```
Error: Invariant FlattenExitOnly is violated.
State 1: <Initial predicate>
State 2: <RequestFlatten line 293, col 5 to line 296, col 79 of module KillSwitch>
State 3: <ConfirmFlatten line 301, col 5 to line 311, col 49 of module KillSwitch>
```

### TLA ExecutionRouter!NoEntryWithoutSession

Break: broken variant `ExecutionRouter__no_session_check.cfg`: `Take` places without a live session.

```
Error: Invariant NoEntryWithoutSession is violated.
State 4: <GateRefresh line 319, col 5 to line 323, col 23 of module ExecutionRouter>
State 5: <Route line 152, col 5 to line 178, col 23 of module ExecutionRouter>
State 6: <Take line 221, col 5 to line 228, col 62 of module ExecutionRouter>
```

### TLA ExecutionRouter!RejectNoCard

Break: broken variant `ExecutionRouter__card_on_reject.cfg`: the relay sends a card for a rejected signal.

```
Error: Invariant RejectNoCard is violated.
State 1: <Initial predicate>
State 2: <Reject line 201, col 5 to line 206, col 23 of module ExecutionRouter>
State 3: <Relay line 360, col 5 to line 366, col 86 of module ExecutionRouter>
```

### TLA ExecutionRouter!UserCannotRaiseRisk

Break: broken variants `ExecutionRouter__user_resizes.cfg` (Take with a user size) and `ExecutionRouter__user_creates_entry.cfg` (a command opens an entry).

```
Error: Invariant UserCannotRaiseRisk is violated.
Error: Invariant UserCannotRaiseRisk is violated.
State 1: <Initial predicate>
State 2: <UserEntry line 371, col 5 to line 378, col 72 of module ExecutionRouter>
```

### TLA JobScheduler!LoginPromptOrder

Break: broken variants `JobScheduler__prompt_holiday.cfg` and `JobScheduler__reprompt_logged_in.cfg`.

```
Error: Invariant LoginPromptOrder is violated.
State 3: <TgTick line 262, col 5 to line 266, col 45 of module JobScheduler>
State 4: <TgTick line 262, col 5 to line 266, col 45 of module JobScheduler>
State 5: <Reprompt line 290, col 5 to line 296, col 45 of module JobScheduler>
```

### TLA JobScheduler!ReportsAfterClose

Break: broken variants `JobScheduler__report_before_close.cfg` and `JobScheduler__report_holiday.cfg`.

```
Error: Invariant ReportsAfterClose is violated.
State 3: <TgTick line 262, col 5 to line 266, col 45 of module JobScheduler>
State 4: <TgTick line 262, col 5 to line 266, col 45 of module JobScheduler>
State 5: <Report line 301, col 5 to line 308, col 45 of module JobScheduler>
```

### PBT telegram.health_alert_for_every_failure

Break: in `relay.py`, `HEALTH_EVENTS` loses `feed.stale`.

```
E   AssertionError: assert Counter() == Counter({'feed.stale': 1})
E
E     Right contains 1 more item:
E     {'feed.stale': 1}
```

### PBT telegram.unknown_chat_has_no_effect

Break: in `service.py`, `handle` treats only a missing chat id as a stranger.

```
E   AssertionError: assert Reply(accepted=True, reason=None, text='Kill switch tripped: every bot is manage-only; approvals cancelled', buttons=()) is None
E    +  where Reply(accepted=True, reason=None, text='Kill switch tripped: every bot is manage-only; approvals cancelled', buttons=()) = Handled(chat_id=424243,
E   (note: 1 other explicit example also failed with this error; use Verbosity.verbose to view)
E   Failing explicit example: test_unknown_chat_has_no_effect(
```

### PBT telegram.every_command_logged

Break: in `service.py`, the row is always written `accepted = true`.

```
E   AssertionError: assert True == False
E    +  where True = <app.hat.models.ops.HatCommandLog object at 0x7fe33f248440>.accepted
E    +  and   False = Reply(accepted=False, reason='not_a_command', text='Refused: not_a_command', buttons=()).accepted
E    +    where Reply(accepted=False, reason='not_a_command', text='Refused: not_a_command', buttons=()) = Handled(chat_id=424242, command='text', reply=Reply(a
```

### PBT telegram.unknown_command_rejected

Break: in `commands.py`, `parse` drops the upper bound on argument count.

```
    | AssertionError: assert 'unknown_bot' in {'bad_arguments', 'not_a_command', 'unknown_command'}
    | Failing test case: test_unknown_command_rejected(
    | AssertionError: assert not True
    | Failing test case: test_unknown_command_rejected(
```

### PBT telegram.other_bot_ignored

Break: three in `commands.py`, each run alone: (1) `run_command` drops the `for_another_bot` check; (2) `for_another_bot` compares the suffix case-sensitively; (3) `for_another_bot` lets every suffix through while the username is unknown (fail open).

```
(1) E   AssertionError: assert not True
(2) E   AssertionError: assert ('/kill', False, 'other_bot') == ('/kill', True, None)
(3) E   AssertionError: assert None == 'other_bot'
```

### PBT telegram.no_free_text_orders

Break: in `service.py`, a text that is not a command is handled as button data.

```
E   AssertionError: assert [{'symbol': '...'LIMIT', ...}] == []
E
E     Left contains one more item: {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}
E
```

### PBT telegram.offset_at_most_once

Break: in `service.py`, `poll_once` handles the update before committing the offset.

```
    | AssertionError: assert 2 == 1
    | Failing test case: test_offset_at_most_once(
    | AssertionError: assert {'u1'} == set()
    | Failing test case: test_offset_at_most_once(
```

### PBT telegram.user_events_attributed

Break: in `callbacks.py`, `flatten.executed` is recorded with `actor = 'engine'`.

```
E   AssertionError: flatten.executed
E   assert 'engine' == 'user'
E
E     - user
```

### PBT telegram.no_secret_in_output

Break: in `client.py`, a transport failure keeps the exception text (with the URL) as its code.

```
E   AssertionError: assert '2222222222222222' not in 'https://api...222222222222'
E
E     '2222222222222222' is contained here:
E       https://api.telegram.org/bot2222222222222222/sendMessage 2222222222222222 TelegramError('https://api.telegram.org/bot2222222222222222/sendMessage 222222
```

### PBT telegram.send_only_to_allowlist

Break: in `client.py`, `clear_buttons` addresses chat id + 1.

```
E   assert 424243 == 424242
E   Failing explicit example: test_send_only_to_allowlist(
E       actions=['route', 'relay', 'take'],
E   )
```

### PBT telegram.login_prompt_links

Break: in `reports.py`, the reprompt lists every account.

```
    | AssertionError: assert {'X0001', 'Y0001'} == {'Y0001'}
    | Failing test case: test_login_prompt_links(
    | AssertionError: assert ['Still not l...n.test/X0001'] == []
    | Failing test case: test_login_prompt_links(
```

### PBT telegram.watchlist_digest_content

Break: in `reads.py`, `TOP_N = 5`.

```
E   AssertionError: assert 5 == 6
E    +  where 5 = len([['W00', '0.000'], ['W01', '0.000'], ['W02', '0.000'], ['W03', '0.000'], ['W04', '0.000']])
E    +  and   6 = min(10, 6)
E    +    where 6 = len({'W00': 0, 'W01': 0, 'W02': 0, 'W03': 0, ...})
```

### PBT telegram.card_matches_signal

Break: in `render.py`, the card prints entry minus stop as the stop.

```
E   AssertionError: assert 'Entry 50.00 ....05  Qty 4000' == 'Entry 50.00 ....95  Qty 4000'
E
E     - Entry 50.00  Stop 49.95  Qty 4000
E     ?                   ^^ ^
```

### PBT telegram.report_content

Break: in `render.py`, a digest line drops its reason.

```
E   IndexError: list index out of range
E   Failing test case: test_report_content(
E       # The test always failed when commented parts were varied together.
E       refusals=['market_closed'],
```

### PBT telegram.mode_outside_hours

Break: the market-hours guard removed from both `commands.py:_mode_refusal` and `risk/modes.py:plan_mode_change` (either alone is caught by the other, by design).

```
E   AssertionError: assert not True
E    +  where True = Reply(accepted=True, reason=None, text='Mode auto for s-7d830ec3-6348-4f23-bb4b-7247476bf2e6 from 2026-10-22T03:45:00+00:00', buttons=()).a
E    +    where Reply(accepted=True, reason=None, text='Mode auto for s-7d830ec3-6348-4f23-bb4b-7247476bf2e6 from 2026-10-22T03:45:00+00:00', buttons=()) = Hand
E   Failing explicit example: test_mode_outside_hours(
```

### PBT telegram.second_confirm

Break: in `confirm.py`, `take` reads the nonce without consuming it.

```
E   AssertionError: assert Counter({'flatten': 2}) == Counter({'flatten': 1})
E
E     Differing items:
E     {'flatten': 2} != {'flatten': 1}
```

### PBT telegram.flatten_logged

Break: in `callbacks.py`, `flatten.executed` carries chat id 0.

```
E   assert 0 == 424242
E   Failing test case: test_flatten_logged(
E       # The test always failed when commented parts were varied together.
E       when=(11, 0),
```

### PBT telegram.read_commands_side_effect_free

Break: in `commands.py`, `_status` re-arms the kill switches.

```
E   assert {'hat_broker_...run': [], ...} == {'hat_broker_...run': [], ...}
E
E     Omitting 37 identical items, use -vv to show
E     Differing items:
```

### PBT telegram.token_forgery_rejected

Break: in `tokens.py`, `verify` skips the MAC comparison.

```
E   AssertionError: assert not True
E    +  where True = Reply(accepted=True, reason=None, text='Skip: skipped', buttons=()).accepted
E    +    where Reply(accepted=True, reason=None, text='Skip: skipped', buttons=()) = Handled(chat_id=424242, command='skip', reply=Reply(accepted=True, reason=
E   Failing explicit example: test_token_forgery_rejected(
```

### PBT telegram.expiry_window

Break: in `engine/approvals.py`, the expiry check is removed.

```
E   AssertionError: assert not True
E    +  where True = Reply(accepted=True, reason=None, text='Take: approved, entry sent', buttons=()).accepted
E    +    where Reply(accepted=True, reason=None, text='Take: approved, entry sent', buttons=()) = Handled(chat_id=424242, command='take', reply=Reply(accepted=
E   Failing explicit example: test_expiry_window(
```

### PBT telegram.take_chase_recheck

Break: in `callbacks.py`, `RouterDesk.take` passes `bool(locked)` (unknown reads as not locked).

```
E   AssertionError: assert not True
E    +  where True = Reply(accepted=True, reason=None, text='Take: approved, entry sent', buttons=()).accepted
E    +    where Reply(accepted=True, reason=None, text='Take: approved, entry sent', buttons=()) = Handled(chat_id=424242, command='take', reply=Reply(accepted=
E   Failing test case: test_take_chase_recheck(
```

### PBT telegram.token_never_stored

Break: in `engine/trades.py`, `open_entry` stores the token itself.

```
E   AssertionError: assert 'AaFgkTywdJa4...wGnmvU9SAr2uA' == 'c062558aca7e...e4e80285809d4'
E
E     - c062558aca7e7edd7a490133f2110e93caa59a83cb4e5f698c8e4e80285809d4
E     + AaFgkTywdJa4kZJegFzvFwAAAABq2Ef8lqgO180QlwGnmvU9SAr2uA
```

### PBT telegram.relay_late_commit

Break: in `relay.py`, the remembered gaps are never looked up again (and, second break, never expire: `assert 3 == 2` after 61 minutes).

```
E   AssertionError: assert 2 == 3
E    +  where 2 = len(['Alert: feed.stale (2026-10-22)\nstatus: stale', 'Alert: feed.stale (2026-10-22)\nstatus: stale'])
E   Failing explicit example: test_relay_late_commit(
E       count=3,
```

### PBT telegram.veto_digest_on_week_end

Break: `is_week_end` restored to its first draft (no later known trading day counts as week end).

```
E   AssertionError: assert True is False
E    +  where True = is_week_end(SessionCalendar(days={datetime.date(2026, 10, 19): SessionDay(is_trading=True, open_at=datetime.datetime(2026, 10, 19,...key='A
E   Failing explicit example: test_veto_digest_on_week_end(
E       kinds=['trading',
```

### PBT telegram.token_never_persisted

Break: in `service.py`, `handle` logs the button data.

```
E   assert False
E    +  where False = all(<generator object test_token_never_persisted.<locals>.<genexpr> at 0x7ff6530d8040>)
E   Failing test case: test_token_never_persisted(
E       # The test always failed when commented parts were varied together.
```

### CONF telegram_command_set

Break: `/buy` added to `commands.COMMANDS`.

```
E   AssertionError: assert {'/arm', '/bu... '/mode', ...} == {'/arm', '/fl...'/pause', ...}
E
E     Extra items in the left set:
E     '/buy'
```

### CONF telegram__command_catalogue

Break: `/size` added to `HANDLERS`.

```
E   AssertionError: assert {'/arm', '/fl...'/pause', ...} == {'/arm', '/fl...'/pause', ...}
E
E     Extra items in the left set:
E     '/size'
```

### CONF gov__lever_catalogue

Break: `qty` added to `ApprovalDesk.take`.

```
E   AssertionError: {'chat_id', 'now', 'qty', 'self', 'session', 'signal_id'}
E   assert {'chat_id', '..., 'signal_id'} <= {'chat_id', '..., 'signal_id'}
E
E     Extra items in the left set:
```

### CONF no_override_command

Break: `gate_override: bool` added to `StrategyConfig`.

```
E   AssertionError: gate_override
E   assert not <re.Match object; span=(5, 13), match='override'>
E    +  where <re.Match object; span=(5, 13), match='override'> = <function search at 0x7f559f3dc670>('override|force|bypass', 'gate_override')
E    +    where <function search at 0x7f559f3dc670> = re.search
```

### CONF telegram__no_webhook_no_listener

Break: `import socket` added to `client.py` (and the planted-server scan, `test_telegram__listener_scan_catches_a_server`).

```
E   AssertionError: assert ['client.py imports socket'] == []
E
E     Left contains one more item: 'client.py imports socket'
E
```

### CONF telegram__no_order_import_in_text_path

Break: `from ..execution import router` added to `reads.py`.

```
E   AssertionError: app.hat.telegram.commands
E   assert ['app.hat.tel...ution.router'] == []
E
E     Left contains 3 more items, first extra item: 'app.hat.telegram.commands'
```

### CONF auto_mode_single_user

Break: the owner check in `commands.py:_mode_refusal` removed.

```
E   AssertionError: assert None == 'auto_not_owner'
E    +  where None = Reply(accepted=True, reason=None, text='Mode auto for s-a16b4a4b-1258-44fd-9a2e-2665dabe2eb5 from 2026-10-22T03:45:00+00:00', buttons=()).r
E    +    where Reply(accepted=True, reason=None, text='Mode auto for s-a16b4a4b-1258-44fd-9a2e-2665dabe2eb5 from 2026-10-22T03:45:00+00:00', buttons=()) = Hand
```

### PBT telegram.watchlist_digest_once

Break: `send_watchlist_digest` ignores the digests already sent (`if bot is None or _digest_sent(session, bot_id, day):` to `if bot is None:`).

```
E   AssertionError: assert 2 == (0 + 1)
```
