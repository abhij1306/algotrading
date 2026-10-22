---
name: verify-standalone
description: How to run, read and update the checks that keep Hire a Trader independent of the legacy SmartTrader code, the import-direction and no-app-boot checks, the owned copies of the clock, sessions, money, ratio and backtest-metric helpers, and the frozen VCP oracle fixtures.
---

# verify-standalone

Checks that HAT does not import or boot the app wiring or any module outside the shared infrastructure (K1 of the cleanup, finished by K6), that nothing but the database upkeep and the app wiring imports HAT, and that the helpers HAT copied out of the legacy code (backtest metrics, with the clock, sessions, money and ratio helpers) keep their properties and their frozen oracle.

## Properties

- `PBT metrics.drawdown_bounds` The drawdown of a positive equity curve is in (-1, 0], is exactly 0 at every new running peak, and `max_drawdown` is its minimum. Harm: a backtest reports a loss deeper than the account, or none after a fall.
- `PBT metrics.trade_stats_partition` Every trade is a win or a loss (exactly 0 is a loss), the counts add up, the win rate is in [0, 1], average win is not negative, average loss is not positive and the profit factor is finite and not negative. Harm: a flat trade is counted as a win and the win rate is overstated.
- `PBT metrics.sharpe_zero_volatility` Returns that never vary give exactly 0.0 for both Sharpe conventions, down to the volatility floor of 1e-12 per period (a series with a per-period deviation of 1e-13 is flat too). Harm: an enormous or infinite Sharpe ranks a flat strategy first.
- `PBT metrics.sharpe_sign_follows_mean` With risk-free 0 the Sharpe has the sign of the mean return and is finite. Harm: a losing strategy is reported as profitable.
- `PBT metrics.returns_guard_bad_capital` A non-positive initial capital or period count gives 0.0 for total return and CAGR, never a division error or a sign flip. Harm: a bad input prints a large false return.
- `CONF imports.hat_decoupled_from_legacy` The `app` modules that `app/hat` and `tests/hat` import are exactly the `SHARED` set in `test_conf_decoupling.py` (`app.base`, `app.database`, `app.paths`, `app.utils.env_loader`: infrastructure with no legacy code and no HAT rule); a new coupling fails, and a module no longer imported must leave the set. Harm: HAT quietly grows back into code it is meant to stand apart from.
- `CONF imports.hat_suites_boot_no_legacy_app` Collecting `tests/hat` in a fresh interpreter loads neither `app.main` nor `app.routers` (the manual-trading singletons they once also listed were deleted in K4; `risk.no_manual_trading_path` keeps them deleted). Harm: HAT suites depend on booting the whole legacy app.
- `CONF imports.legacy_does_not_import_hat` Only `app/database.py` (the table registry and schema upkeep) and `app/main.py` (the wiring) import `app.hat`; no other `app` module does. Harm: deleting or moving HAT code breaks the other side.
- `CONF fixtures.vcp_oracle_frozen` The frozen legacy VCP outputs in `tests/hat/fixtures/vcp_oracle.json` match their recorded hash, case list and internal consistency. Harm: PR 6's characterization test compares against a drifting oracle.

## Run

```bash
cd backend
# every pytest, Hypothesis or break run goes under the 2 GB memory cap
CAP="systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=512M --quiet"
CI=1 $CAP python -m pytest tests/hat/properties/test_metrics.py -q --no-cov
$CAP python -m pytest tests/hat/conformance/test_conf_decoupling.py tests/hat/conformance/test_conf_fixtures.py -q --no-cov
```

No check touches the network or a database. The decoupling checks read source files, plus one subprocess that collects `tests/hat` and lists loaded modules.

## Bug or model error

- A failing `imports.hat_decoupled_from_legacy` names the module and the HAT file. Fix the import (use a HAT module, or inject the dependency); add to `SHARED` only a module that holds no HAT rule and no feature code, with its reason written next to the entry.
- A failing `imports.hat_suites_boot_no_legacy_app` lists the loaded legacy modules. Find the first import that pulled them (a test helper, a root `conftest.py` import) and make it lazy or HAT-local; never filter the list.
- A failing `metrics.*` property is a real bug in `app/hat/backtest/metrics.py`; the legacy copy has the same flaw for a constant series (found here, fixed with `VOLATILITY_FLOOR`).
- A failing `fixtures.vcp_oracle_frozen` means the file changed. The legacy detector it came from is deleted (K5a), so the file is now the only record of those outputs: regenerate it only on purpose, from a recovered `pattern.py`, and change the recorded hash with it.

## Updating

- A new helper copied into HAT: copy it with its tests, import the HAT copy everywhere under `app/hat` and `tests/hat`, and add its code to Coverage below.
- A module removed from `SHARED` (no longer imported) must leave the set in the same change.
- After re-confirming everything, update the sync commit and digest: `python backend/tests/hat/conformance/hat_conf_skills.py verify-standalone` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/clock.py`
- `backend/app/hat/money.py`
- `backend/app/hat/data/sessions.py`
- `backend/app/hat/data/ratios.py`
- `backend/app/hat/backtest/metrics.py`

Checks:
- `backend/tests/hat/properties/test_metrics.py`
- `backend/tests/hat/conformance/test_conf_decoupling.py`
- `backend/tests/hat/conformance/test_conf_fixtures.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `e71a5f2025df0cbf158d242b71f28e4e4e74e4e3`
Covered digest: `5660b51be2ff0c0537fc729ed6aad873a9f12361045b77147be1d8717f488d65`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `metrics.trade_stats_partition` found a subnormal gross loss giving an infinite profit factor; the HAT copy treats a gross loss below the floor as none and keeps the factor finite.
- PR 12 rewrote `backtest/metrics.py` on polars (`pl.Series` in, the arithmetic unchanged; the population deviation is `std(ddof=0)`). The five `metrics.*` properties were moved to polars series with the same strategies and bounds, and each was shown failing again on the polars code (the `PR 12` breaks below). No property was narrowed.
- `metrics.sharpe_zero_volatility` found that the legacy copy returned a Sharpe near 1e17 for a constant series (float noise passed `std > 0`); the HAT copy compares with a volatility floor of 1e-12. A deviation from the legacy function, recorded in the K1 PR.
- K6 emptied `PENDING` in `test_conf_decoupling.py` and replaced it, with `PERMANENT`, by one `SHARED` set holding `app.base`, `app.database`, `app.paths` and `app.utils.env_loader`. They are kept as HAT-neutral shared infrastructure rather than moved into HAT: after K6 they hold no legacy code (`database.py` creates only HAT tables and Postgres only, `paths.py` has the roots and `HAT_DATA_ROOT`, `env_loader.py` parses `.env`), and moving them would duplicate the engine and the one `.env` loader. No check was narrowed: a new coupling is still refused.

- K4 removed `services/execution/paper_book.py`, `brokers/*` and `services/fyers/token_store.py` from `SHIMS` (deleted), added `config.py` (it imports HAT's token store for the legacy data fetcher) and shortened `NEVER_LOADED` to the app and the routers. No check was narrowed: the deleted modules cannot be imported and `risk.no_manual_trading_path` asserts they stay gone.
- K2 moved the six Fyers and symbol modules out of `PENDING` in `test_conf_decoupling.py`, added the six legacy re-export modules to `SHIMS` (they may import HAT; HAT may not import them), and added `database.py` and `main.py` to the wiring files the legacy-does-not-import-HAT check allows. The decoupling check is stricter for HAT (no `app.brokers`, `app.services` or `app.routers` import) and no check was narrowed.
- K3 removed the four live-feed entries from `PENDING` in `test_conf_decoupling.py` (the data socket, the provider session, the live market service and the subscription registry moved into HAT or no longer touch it); `PENDING` now holds the K6 database modules only. `SHIMS` lists the legacy `live_market_service.py` and `ws_manager.py`, which import HAT.
- K5b emptied `SHIMS` (it is deleted from `test_conf_decoupling.py`): `utils/clock.py`, `services/fyers/auth_flow.py`, `services/fyers_client.py`, `services/symbol_master.py`, `services/live_market_service.py` and `utils/ws_manager.py` were deleted, and `config.py` no longer imports HAT. `PENDING` (`app.database`, `app.paths`, `app.utils.env_loader`, all K6) is unchanged. No check was narrowed.
- PR 44 added `test_fixtures_index_history_no_data` to `test_conf_fixtures.py` (a verify-data check of the MCP's no-data answer); the frozen VCP oracle check is unchanged and still passes.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or fixture (HAT-VER-072), then the break was removed and the check returned to passing.

### PBT metrics.drawdown_bounds

Break: in `backend/app/hat/backtest/metrics.py`: drawdown measured from the first value, not the running peak.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:33: in test_drawdown_bounds
tests/hat/properties/test_metrics.py:39: in test_drawdown_bounds
E   assert np.False_
E    +  where np.False_ = all()
E    +    where all = (0    0.0\n1    1.0\ndtype: float64 <= 0 & 0    0.0\n1    1.0\ndtype: float64 > -1).all
```

Break (PR 12, polars): in `backend/app/hat/backtest/metrics.py`: drawdown measured from the first value.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:33: in test_drawdown_bounds
    def test_drawdown_bounds(equity: pl.Series) -> None:
                   ^^^
tests/hat/properties/test_metrics.py:39: in test_drawdown_bounds
    assert ((dd <= 0) & (dd > -1)).all()
E   AssertionError: assert False
```

### PBT metrics.trade_stats_partition

Break: in `backend/app/hat/backtest/metrics.py`: a trade of exactly 0 is neither a win nor a loss.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:47: in test_trade_stats_partition
tests/hat/properties/test_metrics.py:53: in test_trade_stats_partition
E   assert (0 + 0) == 1
E    +  where 0 = TradeStats(total=1, wins=0, losses=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0, profit_factor=0.0).wins
E    +  and   0 = TradeStats(total=1, wins=0, losses=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0, profit_factor=0.0).losses
```

Break (PR 12, polars): in `backend/app/hat/backtest/metrics.py`: a trade of exactly 0 is neither win nor loss.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:47: in test_trade_stats_partition
    def test_trade_stats_partition(values: list[float]) -> None:
                   ^^^
tests/hat/properties/test_metrics.py:53: in test_trade_stats_partition
    assert stats.wins + stats.losses == stats.total
E   assert (0 + 0) == 1
```

### PBT metrics.sharpe_zero_volatility

Break: in `backend/app/hat/backtest/metrics.py`: the volatility floor replaced by `std > 0`.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:63: in test_sharpe_zero_volatility
tests/hat/properties/test_metrics.py:71: in test_sharpe_zero_volatility
E   assert 7.860675556700752e+16 == 0.0
E    +  where 7.860675556700752e+16 = <function sharpe_mean_std at 0x7fb2f6bbbb60>(0     1.000000e-12\n1     1.000000e-12\n2     1.000000e-12\n3     1.000000e-12\n4     1.000000e-12\n5     1
E    +    where <function sharpe_mean_std at 0x7fb2f6bbbb60> = metrics.sharpe_mean_std
```

Break: in `backend/app/hat/backtest/metrics.py`: the excess-CAGR floor compared on the annualised deviation (a per-period 1e-13 passes it).

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:63: in test_sharpe_zero_volatility
tests/hat/properties/test_metrics.py:75: in test_sharpe_zero_volatility
E   assert 31497039417.435608 == 0.0
```

Break (PR 12, polars): in `backend/app/hat/backtest/metrics.py`: the volatility floor replaced by `std > 0`.

```
tests/hat/properties/test_metrics.py F
  + Exception Group Traceback (most recent call last):
  |   File "/tmp/claude-1000/-data-workspace-wt-hat-pr12-backtest/b5cc6546-d93d-4f00-8bcc-c5ce687ba958/scratchpad/pr12/root4/backend/tests/hat/properties/test_metrics.py", line 63, in test_sharpe_zero_volatility
  |     st.floats(min_value=-0.2, max_value=0.2, allow_nan=False),
  |                ^^^
  |   File "/data/workspace/algotrading/venv/lib/python3.14/site-packages/hypothesis/core.py", line 2299, in wrapped_test
  |     raise the_error_hypothesis_found
```

### PBT metrics.sharpe_sign_follows_mean

Break: in `backend/app/hat/backtest/metrics.py`: the mean taken as an absolute value.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:76: in test_sharpe_sign_follows_mean
tests/hat/properties/test_metrics.py:83: in test_sharpe_sign_follows_mean
E   assert -0.25 > 0
E   Failing test case: test_sharpe_sign_follows_mean(
E       series=Series([0.0, -0.5]),
```

Break (PR 12, polars): in `backend/app/hat/backtest/metrics.py`: the mean of absolute returns.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:79: in test_sharpe_sign_follows_mean
    def test_sharpe_sign_follows_mean(series: pl.Series) -> None:
                   ^^^
tests/hat/properties/test_metrics.py:86: in test_sharpe_sign_follows_mean
    assert mean > 0
E   assert -0.25 > 0
```

### PBT metrics.returns_guard_bad_capital

Break: in `backend/app/hat/backtest/metrics.py`: the capital guard removed from `total_return`.

```
tests/hat/properties/test_metrics.py F
```

Break (PR 12, polars): in `backend/app/hat/backtest/metrics.py`: the capital guard lets a negative capital through.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:92: in test_returns_guard_bad_capital
    st.floats(min_value=-1e6, max_value=1e7, allow_nan=False),
               ^^^
tests/hat/properties/test_metrics.py:101: in test_returns_guard_bad_capital
    assert metrics.total_return(final, initial) == 0.0
E   assert -1.0 == 0.0
```

### CONF imports.hat_decoupled_from_legacy

Break: appended `import app.utils.helpers` to `app/hat/money.py`.

```
tests/hat/conformance/test_conf_decoupling.py F                          [100%]
tests/hat/conformance/test_conf_decoupling.py:128: in test_imports__hat_decoupled_from_legacy
E   AssertionError: {'app.utils.helpers': ['app/hat/money.py']}
E   assert {'app.utils.h...at/money.py']} == {}
E     
E     Left contains 1 more item:
E     {'app.utils.helpers': ['app/hat/money.py']}
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_decoupling.py::test_imports__hat_decoupled_from_legacy
```

Break (K6, `SHARED` replaced `PENDING`): appended `import app.utils.logger` to `app/hat/money.py`, a module that is not in `SHARED`.

```
tests/hat/conformance/test_conf_decoupling.py F                          [100%]
tests/hat/conformance/test_conf_decoupling.py:113: in test_imports__hat_decoupled_from_legacy
E   AssertionError: {'app.utils.logger': ['app/hat/money.py']}
E   assert {'app.utils.l...at/money.py']} == {}
```

Break (K6, a stale entry): added `"app.config": "break"` to `SHARED` although nothing under HAT imports it.

```
tests/hat/conformance/test_conf_decoupling.py F                          [100%]
tests/hat/conformance/test_conf_decoupling.py:116: in test_imports__hat_decoupled_from_legacy
E   AssertionError: assert {'app.base', ...s.env_loader'} == {'app.base', ...s.env_loader'}
E     Extra items in the right set:
E     'app.config'
```

### CONF imports.hat_suites_boot_no_legacy_app

Break: appended `import app.main` to `tests/hat/properties/test_caps.py`.

```
tests/hat/conformance/test_conf_decoupling.py F                          [100%]
tests/hat/conformance/test_conf_decoupling.py:181: in test_imports__hat_suites_boot_no_legacy_app
E   AssertionError: ['app.main', 'app.routers', 'app.routers.data_snapshot', 'app.routers.market', 'app.routers.market_dashboard', 'app.routers.system_health', ...]
E   assert ['app.main', ..._health', ...] == []
E     
E     Left contains 9 more items, first extra item: 'app.main'
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_decoupling.py::test_imports__hat_suites_boot_no_legacy_app
```

### CONF imports.legacy_does_not_import_hat

Break: appended `from app.hat.money import to_money` to `app/utils/market_hours.py`.

```
tests/hat/conformance/test_conf_decoupling.py F
tests/hat/conformance/test_conf_decoupling.py:144: in test_imports__legacy_does_not_import_hat
E   AssertionError: assert ['utils/market_hours.py'] == []
```

Break (K4, after `SHIMS` shrank): a deleted shim recreated as `app/services/execution/paper_book.py` importing `app.hat.money`.

```
tests/hat/conformance/test_conf_decoupling.py F                          [100%]
tests/hat/conformance/test_conf_decoupling.py:152: in test_imports__legacy_does_not_import_hat
E   AssertionError: assert ['services/ex...aper_book.py'] == []
E     
E     Left contains one more item: 'services/execution/paper_book.py'
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_decoupling.py::test_imports__legacy_does_not_import_hat
```

Break (K5b, after `SHIMS` was deleted): recreated `app/utils/clock.py` re-exporting `app.hat.clock.SYSTEM_CLOCK`.

```
tests/hat/conformance/test_conf_decoupling.py F
E   AssertionError: assert ['utils/clock.py'] == []
E     
E     Left contains one more item: 'utils/clock.py'
```

### CONF fixtures.vcp_oracle_frozen

Break: appended a newline to `tests/hat/fixtures/vcp_oracle.json`.

```
tests/hat/conformance/test_conf_fixtures.py F
tests/hat/conformance/test_conf_fixtures.py:137: in test_fixtures_vcp_oracle_frozen
E   AssertionError: assert 'cf5b2990d397...a16aef92de15b' == '95e991f5a33b...549a2dabf3f4b'
E     - 95e991f5a33b950f443db7ccc9a14caff24969832c9ecd10c6e549a2dabf3f4b
```
