# 08: Telegram

Telegram is HAT's only control surface and its alert channel: one bot, one allowlisted chat, long
polling from the self-hosted box. Requirement format and verification methods: see README.md and
11-verification.md.

Sources: DS "Telegram"; MA "Execution modes", "Daily cycle"; BL "Governance"; SCH "Backtest and ops
tables", "Retention"; PLAN §4.4, C10. Decisions: I11, I12, I13, T14, T15, B1, B2, B12, B26.

## §1 Transport, allowlist and logging

Telegram Bot API over HTTPS, long polling (no webhook, no inbound port). The update offset is
persisted so a restart neither replays nor drops updates. Exactly one chat is allowlisted
(`HAT_TELEGRAM_CHAT_ID`); every inbound command, accepted or not, is a `hat_command_log` row
(`id, chat_id, command, args, accepted, reason, received_at`).

Implementation (PR 16, `app/hat/telegram/`): the client posts to the Bot API with `requests`
(`aiohttp` does not build on Python 3.14, see CLAUDE.md) and calls only `getMe`, `getUpdates`,
`sendMessage`, `answerCallbackQuery` and `editMessageReplyMarkup`; any other method is refused
before a request is made. `getMe` gives the bot's own username, asked once and kept, so a
command with an `@bot` suffix runs only when the suffix names this bot (HAT-TG-030). The offset is the `updates` row of `hat_telegram_cursor` (03 §4.8). For
each update in id order the service handles it and commits the offset past it in the same
transaction as its `hat_command_log` row and state change, on the error and unknown-chat paths
too (HAT-TG-011, PR 32): a crash before that commit leaves the offset where it was, so Telegram
delivers the update again; a crash after it means the update was done. An update delivered
again after a handler's own earlier commit does not act twice: a Take finds its decision
recorded (`already_decided`), a second confirmation (`/flatten`, `/arm`, `/mode auto`) finds
its in-memory nonce gone with the process (`no_pending_confirmation`), and `/kill` finds the
switches tripped. A passing database error (`OperationalError`) leaves the offset where it was
and the poll loop backs off, so the update comes again; any other failure, in the handler or
before it, is logged as an `error` row and the offset moves past it, so one bad update never
holds back the ones after it. One window is open (a follow-up in `execution/`): a crash after a
Take's decision commits and before its entry is placed leaves the decision with no entry, and
the update delivered again is refused `already_decided`. An update from another chat is logged as `unknown_chat` with no reply and no answer to its
button. Errors carry fixed codes; no message text, token or HTTP text is logged.

#### HAT-TG-005: Messages from other chats are ignored and logged
- **Statement:** Any inbound message or callback from a chat ID other than the allowlisted one has no effect and is logged with `accepted = false`.
- **Source:** DS "Telegram / Security"; SCH "Backtest and ops tables" (`hat_command_log` includes rejected commands from unknown chats); I12, I13.
- **PR:** 16
- **Verification:** PBT `telegram.unknown_chat_has_no_effect`
- **Acceptance:**
  - Property: for every inbound update whose `chat_id` differs from `HAT_TELEGRAM_CHAT_ID`, whatever its text or callback data, the handler writes exactly one `hat_command_log` row with `accepted = false` and a non-empty `reason`, and changes no other table, sends no broker call and sends no reply. Harm: anyone who finds the bot could pause, kill, flatten or approve trades.

#### HAT-TG-010: Long polling only, no inbound port
- **Statement:** The Telegram transport uses `getUpdates` long polling from the HAT host; no webhook is registered and no listening port is opened for Telegram.
- **Source:** DS "Telegram / Security" ("Long polling from the self-hosted box (no inbound webhook port needed)"); MA "Deployment and security"; T9.
- **PR:** 16
- **Verification:** CONF `telegram.no_webhook_no_listener`
- **Acceptance:**
  - Property: across the whole `app/hat/telegram` package, no code path calls `setWebhook` or binds a server socket, and the client issues only outbound requests to the Bot API. Harm: an exposed port on the trading box widens the attack surface the design keeps at zero.

#### HAT-TG-011: The offset commits with the handled update; none is dropped or applied twice
- **Statement:** The offset past an update (`update_id + 1`) commits in the same transaction as the update's `hat_command_log` row and state change, on the error and unknown-chat paths too, and never moves back. A passing database error leaves the offset, so the update comes again after a back-off; any other failure is logged as an `error` row and the offset moves past it. After any crash or restart no update is dropped (one whose transaction did not commit is delivered again) and none is applied twice (a handler that committed on its own before the crash, such as a Take's decision or a flatten's orders, refuses the update when it comes again). Open window: a crash between a Take's decision commit and its entry placement leaves a decision with no entry (follow-up in `execution/`).
- **Source:** DS "Telegram" (long polling); MA "Runtime" (API + Telegram restarts safely; stateless, reads Postgres); PLAN PR 16 scope (offset persistence); design review 2026-10-22 item 7 (the offset committed before handling dropped an update on a crash, a `/kill` among them).
- **PR:** 16, 32
- **Verification:** PBT `telegram.offset_commits_with_update`
- **Acceptance:**
  - Property: for every sequence of updates (text commands, messages from other chats, commands whose handler fails, updates that fail before a handler runs, commands that meet a passing database error once, a Take button) and every crash point in the poll loop (before an update runs, at the commit of its log row and offset, after it), polling again until no update is left leaves each text command (the retried ones included), stranger and failure logged exactly once, the Take placing exactly one entry and accepted at most once, and the offset past the last update. Harm: a dropped `/kill` would not act at all, and one bad update would hold back every later one; a replayed `/flatten` confirm or Take button would act twice.
- **Changed (PR 32):** PR 16 committed the offset before handling (at most once), so a crash between the two dropped the update. The handlers are idempotent on a second delivery (see the statement), so the offset now commits with the update (at least once delivered, applied once).

#### HAT-TG-012: Every inbound command is logged
- **Statement:** Every inbound command from the allowlisted chat produces exactly one `hat_command_log` row with `accepted` and, when rejected, a `reason`.
- **Source:** DS "Telegram" ("every inbound action is checked against the allowlist and logged"); SCH `hat_command_log`.
- **PR:** 16
- **Verification:** PBT `telegram.every_command_logged`
- **Acceptance:**
  - Property: for every inbound command string and every system state, the number of new `hat_command_log` rows is exactly 1, and `accepted = false` implies `reason` is non-empty and no state change occurred. Harm: discipline metrics and audits miss user actions.

#### HAT-TG-013: User-initiated events carry actor and chat ID
- **Statement:** Every event caused by a Telegram command or button is written with `actor = 'user'` and the chat ID in its payload.
- **Source:** SCH "Event log / Event types" ("Every user-initiated event carries `actor = user` and the Telegram chat ID in the payload").
- **PR:** 16
- **Verification:** PBT `telegram.user_events_attributed`
- **Acceptance:**
  - Property: for every accepted command or button press that emits events, each emitted `hat_event` has `actor = 'user'` and `payload.chat_id` equal to the originating chat ID. Harm: skips, pauses and flattens cannot be counted as discipline metrics.

#### HAT-TG-014: No broker or secret text leaks into messages or logs
- **Statement:** Outbound messages and logs never contain the bot token, the approval HMAC secret, raw approval tokens or raw broker error text.
- **Source:** MA "Deployment and security" (secrets, T13); `.claude/skills/trading-safety/SKILL.md` (no broker error text in responses); PLAN PR 16 acceptance ("the token never appears in logs").
- **PR:** 16
- **Verification:** PBT `telegram.no_secret_in_output`
- **Acceptance:**
  - Property: for every generated message and every log record emitted by `app/hat/telegram`, the text contains none of `HAT_TELEGRAM_TOKEN`, `HAT_APPROVAL_SECRET`, any issued raw token, or any raw broker exception string injected by the fake broker. Harm: a leaked token or secret lets an outsider control the bot or forge approvals.

## §2 Outbound messages

Table from DS "Telegram / Outbound messages", plus the weekly veto digest from BL "Your vetoes are
measured".

| Message | When | Contains |
| --- | --- | --- |
| Login prompt | 08:30, repeat 09:05 if not done | One link per broker; the repeat is URGENT with the number of real open positions, whose app-side exits are off (HAT-TG-021) |
| Watchlist digest | After nightly scan | Per bot: new, dropped, top 10 by score, regime state |
| Shadow trade (shadow mode) | On trigger | Symbol, entry, stop, qty, risk ₹, R target, chart link |
| Approval card (confirm) | On trigger | Same + catalyst headline/PDF; Take / Skip buttons (size, stop and price are fixed); expiry time |
| Fill / exit / stop moved | On event | Price, qty, R so far |
| Rejection digest | 15:35 | Signals rejected by risk, with reasons |
| Health alert | On failure or change | Data gate, feed stale, broker session, order rejected; broker capabilities changed in either direction (HAT-BRK-092); URGENT when a position spans a corporate action the rescale cannot handle (HAT-EXEC-144) or its instrument changed series (`position.series_changed`, HAT-EXEC-146) |
| Daily P&L | 15:40 | Per bot: open risk, realised R, equity vs peak (latest `hat_daily_bot_snapshot`); last night's GTT check; close gaps |
| Weekly veto digest | Weekly | R the user's skips avoided and R they missed (skips tracked as shadow trades) |

Every sent message is a `hat_telegram_message` row (`id, chat_id, kind, ref_id, sent_at,
message_id`) linking the card to its signal or other subject.

Implementation (PR 16): the jobs run once per trade date and only on a trading day. The 09:05
reprompt goes out for the accounts still logged out even if the 08:30 prompt was missed. The
daily report reads each bot's latest snapshot on or before the day (and says so when there is
none). Fills, exits, stop moves, cards and health alerts come from the event relay
(`hat_telegram_cursor` `events`), which reads `hat_event` in id order and sends nothing for a
virtual position.

#### HAT-TG-020: Outbound messages go only to the allowlisted chat
- **Statement:** HAT sends every message to the single allowlisted chat and to no other chat.
- **Source:** DS "Telegram" ("One bot, one allowlisted chat"); I12, I13.
- **PR:** 16
- **Verification:** PBT `telegram.send_only_to_allowlist`
- **Acceptance:**
  - Property: for every message kind and every input, every `sendMessage`/`editMessage*` request made by the client targets `HAT_TELEGRAM_CHAT_ID`. Harm: trade details and approval buttons reach someone else.

#### HAT-TG-021: Login prompts at 08:30 and 09:05
- **Statement:** On each trading day a login prompt with one link per connected broker is sent at 08:30 IST, and repeated at 09:05 IST only for brokers still not logged in. The repeat is URGENT when those brokers hold real open positions: it names their number and says that they have broker stops only, because breakeven, trim and the MA exits run only on a live session.
- **Source:** DS "Outbound messages" (Login prompt); MA "Daily cycle" step 1, "Risks" (missed morning login); T14; design review 2026-10-22 item 5.
- **PR:** 16, 30
- **Verification:** TLA `JobScheduler!LoginPromptOrder`, PBT `telegram.login_prompt_links`
- **Acceptance:**
  - Property: on every trading day the 08:30 prompt is sent once, and the 09:05 prompt is sent iff some connected broker has no live `hat_broker_session` for the trade date, listing exactly those brokers. Harm: a missed login blocks all entries for the day.
  - Property: the 09:05 prompt is URGENT iff the brokers it lists hold real open positions (virtual and closed ones do not count, nor those of a logged-in broker), and it names their number. Harm: positions sit with broker stops only while the app's exits are off, and the owner reads a routine reminder.
  - Property: on every non-trading day of `hat_trading_calendar` no login prompt is sent. Harm: noise trains the user to ignore prompts.

#### HAT-TG-022: Watchlist digest content
- **Statement:** After the nightly scan, one digest per bot lists new and dropped symbols, the top 10 by score and the regime state.
- **Source:** DS "Outbound messages" (Watchlist digest); MA "Daily cycle" step 7.
- **PR:** 16
- **Verification:** PBT `telegram.watchlist_digest_content`, PBT `telegram.watchlist_digest_once`
- **Acceptance:**
  - Property: for every pair of consecutive watchlists of a bot, the digest's "new" set equals today minus yesterday, "dropped" equals yesterday minus today, and "top" is the first min(10, n) entries ordered by score descending. Harm: the user acts on a wrong summary of what the bot will trade.
  - Property: a `watchlist.digest` retry after a partial failure sends only the digests not yet sent for the latest publish of that day. Harm: the user gets the same digest again on every retry.

#### HAT-TG-023: Shadow trade and approval card content
- **Statement:** A trigger produces a shadow-trade message (shadow mode) or an approval card (confirm mode) carrying symbol, entry, stop, qty, risk ₹ and R target as fixed by the engine; the card adds its expiry time and, when announcements are attached (EP signals), the catalyst headline and PDF link.
- **Source:** DS "Outbound messages"; BL "Governance" (price, size and stop fixed by the engine); DS "Announcements for EP catalysts" (headline and PDF on the card).
- **PR:** 16
- **Verification:** PBT `telegram.card_matches_signal`
- **Acceptance:**
  - Property: for every signal, the entry, stop, qty and risk ₹ rendered on its message equal the `hat_signal` row's `trigger_price`, `stop_price`, `qty` and `risk_amount`, and an approval card shows `hat_approval.expires_at`. Harm: the user approves a trade different from the one the engine places.
  - Property: no approval card offers any control other than Take and Skip. Harm: the user could change size or stop and bypass the risk policy.

#### HAT-TG-024: Event-driven and scheduled reports
- **Statement:** Fill, exit and stop-moved messages are sent on their events; the rejection digest at 15:35; the daily P&L at 15:40; health alerts on data gate failure, feed stale, broker session lost and order rejected; the weekly veto digest weekly.
- **Source:** DS "Outbound messages"; BL "Your vetoes are measured"; MA "Failure handling"; T15.
- **PR:** 16
- **Verification:** TLA `JobScheduler!ReportsAfterClose`, PBT `telegram.report_content`, PBT `telegram.relay_late_commit`, PBT `telegram.veto_digest_on_week_end`
- **Acceptance:**
  - Property: every `order.filled`, `exit.partial`, `position.closed` and `stop.moved` event for a non-virtual position produces exactly one message, and every `quality.failed`, `feed.stale`, `broker.session_lost` and `order.rejected` event produces a health alert. Harm: the user does not learn that money moved or protection failed.
  - Property: the 15:35 rejection digest lists exactly the day's `signal.rejected` events with their reason codes, and the 15:40 P&L shows per bot open risk, realised R and equity vs peak from `hat_daily_bot_snapshot`. Harm: rejected signals and drawdowns go unseen.
  - Property: the weekly veto digest's avoided R equals the sum of negative R and its missed R the sum of positive R over the week's skipped signals' shadow results. Harm: the user misjudges whether vetoes add value and picks the wrong mode.
  - Property: an event whose transaction commits after the relay passed its id is still relayed, once, if it commits within an hour. Harm: a health alert or approval card held by a slow transaction is lost.
  - Property: the weekly veto digest goes out on a trading day exactly when no later weekday of its week may still trade (a calendar holiday does not; a day the calendar lacks may). Harm: the digest arrives daily once the calendar runs out, or never in a holiday week.

## §3 Commands

From PLAN §4.4 and DS "Telegram / Commands", with PLAN C10 applied (`signal` is superseded by
`shadow`, per revised B1).

| Command | Effect | Constraint |
| --- | --- | --- |
| `/status` | Broker sessions, feed health, regime per bot, kill-switch state; per bot the mode it runs, the mode asked for and why they differ, and any queued change | Read-only |
| `/positions` | Open positions with stop, R, days held | Read-only |
| `/watchlist [bot]` | Today's watchlist | Read-only |
| `/pause [bot]`, `/resume [bot]` | Manage-only on/off for a bot | Any time |
| `/mode bot shadow\|confirm\|auto` | Change one bot's execution mode, effective next session | Outside market hours only; confirm and auto need the gates (HAT-GOV-085); `auto` needs a second confirm |
| `/kill` | All bots manage-only; pending approvals cancelled | Any time |
| `/flatten` | Closes everything; logged as a discipline breach | Second confirm |
| `/arm` | Re-arm after a kill switch | Second confirm |
| `/login [broker]` | Fresh login link | Any time |

Implementation (PR 16): a command with no bot (`/pause`, `/resume`) applies to every bot,
all or none. `/mode` names its bot (PR 45, D130): a mode is switched one bot at a time, a name that
matches more than one bot is refused `ambiguous_bot` (name it by its id), and a refusal says
which gate holds it back (HAT-TG-038). `/arm` re-arms every tripped kill switch. The second confirmation is a Confirm button
whose callback is a random 128-bit nonce held in memory for 5 minutes (the same window as
approvals); it is consumed by its first press, and a restart drops it, so the command has to be
sent again. `/flatten` sells every open real position (a virtual trade has nothing at the broker).
`/mode auto` and its Confirm are refused (`auto_not_owner`) unless every account of the bots
named belongs to `HAT_USER_ID` (HAT-OVR-061).

#### HAT-TG-030: Command set is closed
- **Statement:** The bot accepts exactly the ten commands above; anything else is rejected and logged. A command with an `@bot` suffix (Telegram's group form) is accepted only when the suffix equals the bot's own `getMe` username, compared without case; one addressed to another bot, or any suffixed command while the username is unknown, is ignored: logged as refused (`other_bot`) with no reply and no effect.
- **Source:** DS "Telegram / Commands"; PLAN §4.4.
- **PR:** 16
- **Verification:** CONF `telegram.command_catalogue`, PBT `telegram.unknown_command_rejected`, PBT `telegram.other_bot_ignored`
- **Acceptance:**
  - Property: the registered command table equals {`/status`, `/positions`, `/watchlist`, `/pause`, `/resume`, `/mode`, `/kill`, `/flatten`, `/arm`, `/login`}. Harm: an undocumented command is an unreviewed control path.
  - Property: for every text not parsing as one of those commands with valid arguments, the result is `accepted = false` with no state change. Harm: malformed input could trigger an action.
  - Property: for every command and every `@bot` suffix that differs from the bot's username ignoring case, the result is `accepted = false` with reason `other_bot`, no reply, no state change and no broker call; the same command suffixed with the bot's own username in any case acts as the unsuffixed command. Harm: `/kill`, `/flatten` or `/arm` meant for another bot in the group acts on this one.

#### HAT-TG-031: No order from free text
- **Statement:** No inbound text message can create, modify or cancel an order; only buttons on bot-generated cards (and the exit-only `/flatten`) reach execution.
- **Source:** DS "Telegram / Security" ("No order can be created from free text"); BL "Governance".
- **PR:** 16
- **Verification:** PBT `telegram.no_free_text_orders`, CONF `telegram.no_order_import_in_text_path`
- **Acceptance:**
  - Property: for every inbound text message, the FakeBroker records no `place_order` call and no `hat_order` row with purpose `entry` is created. Harm: a typo or a forged message opens a position outside the risk policy.

#### HAT-TG-032: Mode changes only outside market hours, effective next session
- **Statement:** `/mode` is accepted only outside market hours and queues a `hat_mode_change` with `effective_from` = next session open from `hat_trading_calendar`.
- **Source:** DS "Commands" (`/mode`); BL "Governance" (Execution mode: outside market hours, effective next session); SCH "Governance enforced in the database"; PLAN C10; B1, B2.
- **PR:** 16 (command), 10 (mode-change queue)
- **Verification:** TLA `ExecutionRouter!ModeChangeNextSession`, PBT `telegram.mode_outside_hours`
- **Acceptance:**
  - Property: for every instant inside market hours (`freeze_market(True)`), `/mode` is rejected and no `hat_mode_change` row is written. Harm: switching to auto mid-session changes behaviour of live signals.
  - Property: for every accepted `/mode`, `effective_from` equals the open of the next trading session in `hat_trading_calendar`, and the bot's effective mode changes only at that instant. Harm: a change applies to the current session.
  - Property: the only accepted mode values are `shadow`, `confirm`, `auto`. Harm: the superseded `signal` mode lets the user trade around the risk policy.

#### HAT-TG-033: Second confirmation for auto, /arm and /flatten
- **Statement:** `/mode … auto`, `/arm` and `/flatten` take effect only after a second confirmation from the allowlisted chat.
- **Source:** DS "Telegram / Security", "Commands"; BL "Governance" (Emergency flatten-all: second confirm); B26.
- **PR:** 16
- **Verification:** TLA `ApprovalToken!SecondConfirmRequired`, PBT `telegram.second_confirm`
- **Acceptance:**
  - Property: in every reachable state, the auto mode change, the re-arm and the flatten action happen only after both the command and a matching confirmation message from the same chat, and a pending confirmation is consumed at most once. Harm: one mistyped message switches to real-money auto, re-arms after a kill, or sells everything.
  - Property: a pending confirmation expires and is never acted on after its expiry. [OWNER-PENDING] confirmation window: the docs give none; default is the same 5-minute window as approvals (BL/MA "Confirm expires"); owner to confirm. Harm: a stale confirmation fires long after the user's intent changed.

#### HAT-TG-034: /kill makes every bot manage-only and cancels pending approvals
- **Statement:** `/kill` trips the account kill switch: all bots manage-only, every pending approval cancelled; exits and stops keep running.
- **Source:** BL "Kill switches" (`/kill` row); DS "Commands"; B26.
- **PR:** 16 (command), 10 (kill service)
- **Verification:** TLA `KillSwitch!KillCancelsApprovals`, TLA `KillSwitch!ExitsContinue`
- **Acceptance:**
  - Property: after `/kill` is accepted, no new entry order is placed for any bot until a confirmed `/arm`, and every `hat_approval` without a decision ends as cancelled and can no longer be taken. Harm: the user hits the kill switch and the bot still opens a position.
  - Property: after `/kill`, every open position keeps its broker stop and its exit rules still fire. Harm: the kill switch leaves positions unprotected.

#### HAT-TG-035: /flatten is exit-only and logged as a discipline breach
- **Statement:** A confirmed `/flatten` closes all open positions through exit-only paths and records `flatten.executed` as a discipline breach.
- **Source:** BL "Governance" (Emergency flatten-all); DS "Commands"; SCH "Event types" (`flatten.executed`).
- **PR:** 16
- **Verification:** TLA `KillSwitch!FlattenExitOnly`, PBT `telegram.flatten_logged`
- **Acceptance:**
  - Property: every order produced by `/flatten` has purpose `flatten`, side sell and qty ≤ the position's `qty_open`, so it can only reduce exposure. Harm: flatten opens or increases a position.
  - Property: every executed flatten writes one `flatten.executed` event with `actor = 'user'` that the discipline metrics count. Harm: the breach is invisible in rule-adherence numbers.

#### HAT-TG-036: /pause and /resume
- **Statement:** `/pause [bot]` sets the bot manage-only (no new entries; stops and exits continue); `/resume [bot]` lifts that.
- **Source:** DS "Commands"; BL "Governance" (Pause a bot: only reduces risk); SCH `hat_bot.status`, events `bot.paused`, `bot.resumed`.
- **PR:** 16 (command), 10 (service)
- **Verification:** TLA `KillSwitch!PauseBlocksEntriesOnly`
- **Acceptance:**
  - Property: while a bot is paused, it places no entry order and every exit and stop rule still runs. Harm: pause either fails to stop entries or strands positions.
  - Property: `/resume` does not lift a kill switch: a bot under a tripped account or bot kill switch stays manage-only after `/resume`. Harm: resume becomes a bypass of the kill switch.

#### HAT-TG-038: /mode names one bot and says why it is refused
- **Statement:** `/mode` takes a bot and a mode; without the bot it is refused (`bad_arguments`). A confirm or auto request that a gate would hold back at the next open is refused with the gate's code and a sentence naming it (`mode_needs_gtt`, `ddpi_not_confirmed`, `weights_not_confirmed`) and writes nothing. `/status` shows each bot's mode run, the mode asked for, the reason they differ and any queued change.
- **Source:** D130; plan audit 2026-10-22 PR 45; HAT-GOV-085.
- **PR:** 45
- **Verification:** PBT `telegram.mode_names_a_bot_and_its_gate`
- **Acceptance:**
  - Property: for every mode word and every state of the DDPI flag and the policy's `weights_confirmed`, `/mode <mode>` without a bot is refused with nothing written, `/mode <bot> <mode>` is refused with the first failing gate's code and nothing written, or accepted and queued (auto only once its Confirm is pressed); a name two bots share is refused `ambiguous_bot` with nothing written; `/status` then shows the queued change and the mode the bot runs. Harm: the owner switches every bot at once by mistake, or cannot tell why a bot stays in shadow.

#### HAT-TG-037: /status, /positions, /watchlist and /login are read-only
- **Statement:** `/status`, `/positions`, `/watchlist` and `/login` change no trading state (`/login` only issues a fresh broker login link).
- **Source:** DS "Commands"; T14.
- **PR:** 16
- **Verification:** PBT `telegram.read_commands_side_effect_free`
- **Acceptance:**
  - Property: for every system state, running any of these commands leaves every `hat_*` table except `hat_command_log` and `hat_telegram_message` unchanged and makes no broker order call. Harm: a status check changes trading state.

## §4 Approval cards and signed single-use tokens

In confirm mode each triggered signal gets one approval card with Take / Skip buttons. Each button
carries a token that is HMAC-signed with `HAT_APPROVAL_SECRET`, bound to the signal ID and the
expiry, single-use, and stored only as `hat_approval.token_hash`. Default window 5 minutes; on
Take the price is re-checked against the bot's max-chase %.

Token format (PR 16, `telegram/tokens.py`): the card token is the URL-safe base64 of the signal
uuid (16 bytes), the expiry in Unix seconds (8 bytes, big-endian) and the first 16 bytes of
HMAC-SHA256 under `HAT_APPROVAL_SECRET` over a card tag, the uuid and the expiry. Each button is
its action letter (`t` Take, `s` Skip) and the same layout MACed under a button tag that names the
action, so a button cannot be turned into the other. `hat_approval.token_hash` is the sha256 of the
card token. A press is accepted only if its MAC verifies, it names the stored approval's expiry
and the stored hash is the hash of the card token the service would issue for that signal; the
single use is the approval's one decision (HAT-TG-041). The card shows the 2R target, the
max-chase level, and the catalyst headline and PDF link when there are; there is no chart link.

#### HAT-TG-040: Tokens are signed and bound to signal and expiry
- **Statement:** A button token is valid only if its HMAC verifies under `HAT_APPROVAL_SECRET` and it names the card's signal ID and expiry.
- **Source:** DS "Telegram / Security" ("Button callbacks carry a signed, single-use token tied to the signal ID and expiry"); PLAN §7 (`HAT_APPROVAL_SECRET`, 32+ random bytes).
- **PR:** 16
- **Verification:** PBT `telegram.token_forgery_rejected`
- **Acceptance:**
  - Property: for every byte string that is not a token issued by the service (including issued tokens with any bit flipped, the signal ID swapped or the expiry altered), the callback is rejected with no state change. Harm: a forged callback approves a real-money entry.
  - Property: `HAT_APPROVAL_SECRET` shorter than 32 bytes makes HAT refuse to start the Telegram service. Harm: a weak secret allows brute-forcing tokens.

#### HAT-TG-041: Tokens are single-use
- **Statement:** A token is consumed by its first valid use; any later use of it, or of the other button on the same card, is rejected.
- **Source:** DS "Telegram / Security"; SCH `hat_approval` (one decision: take / skip / expired).
- **PR:** 16
- **Verification:** TLA `ApprovalToken!SingleDecision`
- **Acceptance:**
  - Property: in every reachable state, including concurrent duplicate callbacks, each `hat_approval` has at most one decision and at most one entry order results from it. Harm: a double tap places two orders.

#### HAT-TG-042: Approvals expire after 5 minutes
- **Statement:** A card not decided by `expires_at` (`sent_at` + 5 minutes by default) becomes `expired`, emits `signal.expired`, and later callbacks are rejected.
- **Source:** MA "Execution modes" ("Confirm expires. Default window 5 minutes"); SCH `hat_approval`, event `signal.expired`; B1.
- **PR:** 16 (tokens), 15 (expiry service)
- **Verification:** TLA `ApprovalToken!NoTakeAfterExpiry`, PBT `telegram.expiry_window`
- **Acceptance:**
  - Property: for every callback time t ≥ `expires_at`, Take is refused and no order is placed; the approval eventually reaches decision `expired`. Harm: the user approves a setup whose price and context are stale.
  - Property: `expires_at − sent_at` equals the bot's configured window, 5 minutes by default. Harm: an unbounded window turns confirm into a delayed manual trade.

#### HAT-TG-043: Take re-checks price against max-chase
- **Statement:** On Take, the current price is re-checked; if it is more than the bot's max-chase % above the trigger level, the order is refused.
- **Source:** MA "Execution modes" ("At approval the price is re-checked ..."); BL "Criteria library" (`max_chase`: M 5%, Q 2%; skips at upper circuit); invariant 6; B25.
- **PR:** 16 (card), 15 (re-check)
- **Verification:** TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `telegram.take_chase_recheck`
- **Acceptance:**
  - Property: for every trigger level L, max-chase pct c and decision price p, a Take results in an order iff p ≤ L·(1 + c/100) and the stock is not locked at upper circuit; otherwise the decision is recorded with `price_at_decision = p` and refused. Harm: the user's tap buys far above the planned entry and the risk is larger than sized.

#### HAT-TG-044: Token hash only; raw token never stored or logged
- **Statement:** Only a hash of each token is stored (`hat_approval.token_hash`); the raw token appears only in the Telegram button payload.
- **Source:** SCH `hat_approval.token_hash`; PLAN PR 16 acceptance.
- **PR:** 16
- **Verification:** PBT `telegram.token_never_persisted`
- **Acceptance:**
  - Property: for every issued token, no DB column and no log record written by HAT contains the raw token, and `token_hash` equals its hash. Harm: anyone with DB or log access can approve trades.

#### HAT-TG-045: Skipped and expired signals continue as shadow trades
- **Statement:** A Skip or expired approval writes `signal.skipped` or `signal.expired` and the signal continues as a virtual (shadow) trade for veto scoring.
- **Source:** BL "Your vetoes are measured"; SCH `hat_approval` ("Skipped signals continue as shadow trades for the veto score").
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!SkipBecomesShadow`
- **Acceptance:**
  - Property: every confirm-mode signal ends either with a real entry order (Take, re-check passed) or with a virtual position marked `is_virtual = true`, never both and never neither, unless refused by risk. Harm: veto scoring misses skipped trades, or a skipped trade is placed for real.

## §5 Retention

#### HAT-TG-050: Telegram messages and command log kept 1 year
- **Statement:** `hat_telegram_message` and `hat_command_log` rows older than 1 year are purged by a scheduled job; nothing else is purged by it.
- **Source:** SCH "Retention" (Telegram messages, command log: 1 year; events, signals, orders, positions, results: forever).
- **PR:** 3
- **Verification:** PBT `telegram.retention_purge`
- **Acceptance:**
  - Property: for every set of rows and every run date d, after the purge job `hat_telegram_message` and `hat_command_log` contain exactly the rows with timestamps ≥ d − 1 year, and no row of any other table is deleted. Harm: the purge either keeps data longer than intended or deletes the audit trail of trades.
