---
name: verify-standalone
description: How to run, read and update the checks that keep Hire a Trader independent of the legacy SmartTrader code, the import-direction and no-app-boot checks, the owned copies of the clock, sessions, money, ratio and backtest-metric helpers, and the frozen VCP oracle fixtures.
---

# verify-standalone

Checks that HAT does not import or boot the legacy application (K1 of the cleanup), that the legacy code does not import HAT, and that the helpers HAT copied out of the legacy code (backtest metrics, with the clock, sessions, money and ratio helpers; the legacy modules now re-export them, so there is one implementation) keep their properties and their frozen oracle.

## Properties

- `PBT metrics.drawdown_bounds` The drawdown of a positive equity curve is in (-1, 0], is exactly 0 at every new running peak, and `max_drawdown` is its minimum. Harm: a backtest reports a loss deeper than the account, or none after a fall.
- `PBT metrics.trade_stats_partition` Every trade is a win or a loss (exactly 0 is a loss), the counts add up, the win rate is in [0, 1], average win is not negative, average loss is not positive and the profit factor is finite and not negative. Harm: a flat trade is counted as a win and the win rate is overstated.
- `PBT metrics.sharpe_zero_volatility` Returns that never vary give exactly 0.0 for both Sharpe conventions, down to the volatility floor of 1e-12 per period (a series with a per-period deviation of 1e-13 is flat too). Harm: an enormous or infinite Sharpe ranks a flat strategy first.
- `PBT metrics.sharpe_sign_follows_mean` With risk-free 0 the Sharpe has the sign of the mean return and is finite. Harm: a losing strategy is reported as profitable.
- `PBT metrics.returns_guard_bad_capital` A non-positive initial capital or period count gives 0.0 for total return and CAGR, never a division error or a sign flip. Harm: a bad input prints a large false return.
- `CONF imports.hat_decoupled_from_legacy` The legacy modules that `app/hat` and `tests/hat` import are exactly the listed pending set (K2 broker and symbols, K3 live feed, K6 database); a new coupling fails, and a module no longer imported must leave the list. Harm: HAT quietly grows back into the code it is meant to replace.
- `CONF imports.hat_suites_boot_no_legacy_app` Collecting `tests/hat` in a fresh interpreter loads none of `app.main`, `app.routers`, the risk manager or the order, position-sync and live-exit singletons. Harm: HAT suites depend on booting the whole legacy app.
- `CONF imports.legacy_does_not_import_hat` Only `app/database.py` (the table registry) imports `app.hat` from legacy code. Harm: deleting HAT or legacy code in a later PR breaks the other side.
- `CONF fixtures.vcp_oracle_frozen` The frozen legacy VCP outputs in `tests/hat/fixtures/vcp_oracle.json` match their recorded hash, case list and internal consistency. Harm: PR 6's characterization test compares against a drifting oracle.

## Run

```bash
cd backend
# every pytest, Hypothesis or break run goes under the 2 GB memory cap
CAP="systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=512M --quiet"
CI=1 $CAP python -m pytest tests/hat/properties/test_metrics.py -q --no-cov
$CAP python -m pytest tests/hat/conformance/test_conf_decoupling.py tests/hat/conformance/test_conf_fixtures.py -q --no-cov
# the legacy-side check of the frozen oracle against the detector (delete in K5):
$CAP python -m pytest tests/test_hat_oracle_copies.py -q --no-cov
```

No check touches the network or a database. The decoupling checks read source files, plus one subprocess that collects `tests/hat` and lists loaded modules; only `tests/test_hat_oracle_copies.py` (legacy side) imports the legacy VCP detector, to compare it with the frozen oracle.

## Bug or model error

- A failing `imports.hat_decoupled_from_legacy` names the module and the HAT file. Fix the import (use the HAT copy, or inject the dependency); add to the pending set only with the cleanup PR that removes it written next to the entry.
- A failing `imports.hat_suites_boot_no_legacy_app` lists the loaded legacy modules. Find the first import that pulled them (a test helper, a root `conftest.py` import) and make it lazy or HAT-local; never filter the list.
- A failing `metrics.*` property is a real bug in `app/hat/backtest/metrics.py`; the legacy copy has the same flaw for a constant series (found here, fixed with `VOLATILITY_FLOOR`).
- A failing `fixtures.vcp_oracle_frozen` means the file changed. Regenerate it only on purpose, from the legacy `pattern.py`, and change the recorded hash with it.

## Updating

- A new helper copied into HAT: copy it with its tests, import the HAT copy everywhere under `app/hat` and `tests/hat`, and add its code to Coverage below.
- When a cleanup PR (K2 to K6) removes a legacy module from the pending set, delete its entry in `test_conf_decoupling.py` in the same PR.
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
- `backend/tests/test_hat_oracle_copies.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `742e0f09885017e4b63ac0050f2ac4dc9f7a6cff`
Covered digest: `d3897b99334382c3f8872972b16ed95c4c32d9eb38e8e75bee3f8b4271019562`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `metrics.trade_stats_partition` found a subnormal gross loss giving an infinite profit factor; the HAT copy treats a gross loss below the floor as none and keeps the factor finite.
- `metrics.sharpe_zero_volatility` found that the legacy copy returned a Sharpe near 1e17 for a constant series (float noise passed `std > 0`); the HAT copy compares with a volatility floor of 1e-12. A deviation from the legacy function, recorded in the K1 PR.
- The pending set in `imports.hat_decoupled_from_legacy` is not empty: K2, K3 and K6 remove the entries.
- `imports.legacy_does_not_import_hat` allows `app/database.py` until K6 gives HAT its own session factory, and four re-export shims (`utils/clock.py`, `services/bhavcopy_text.py`, `services/backtest_metrics.py`, `services/execution/paper_book.py`) until the legacy code is deleted (K4/K5). Re-exporting avoids a second copy of each helper.

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

### PBT metrics.returns_guard_bad_capital

Break: in `backend/app/hat/backtest/metrics.py`: the capital guard removed from `total_return`.

```
tests/hat/properties/test_metrics.py F
```

### CONF imports.hat_decoupled_from_legacy

Break: appended `import app.services.risk_manager` to `app/hat/money.py`.

```
tests/hat/conformance/test_conf_decoupling.py F
tests/hat/conformance/test_conf_decoupling.py:111: in test_imports__hat_decoupled_from_legacy
E   AssertionError: {'app.services.risk_manager': ['app/hat/money.py']}
E   assert {'app.service...at/money.py']} == {}
```

### CONF imports.hat_suites_boot_no_legacy_app

Break: appended `import app.main` to `tests/hat/properties/test_caps.py`.

```
tests/hat/conformance/test_conf_decoupling.py F
tests/hat/conformance/test_conf_decoupling.py:163: in test_imports__hat_suites_boot_no_legacy_app
E   AssertionError: ['app.main', 'app.routers', 'app.routers.activity', 'app.routers.auth', 'app.routers.backtest', 'app.routers.data_snapshot', ...]
```

### CONF imports.legacy_does_not_import_hat

Break: appended `from app.hat.money import to_money` to `app/utils/market_hours.py`.

```
tests/hat/conformance/test_conf_decoupling.py F
tests/hat/conformance/test_conf_decoupling.py:144: in test_imports__legacy_does_not_import_hat
E   AssertionError: assert ['utils/market_hours.py'] == []
```

### CONF fixtures.vcp_oracle_frozen

Break: appended a newline to `tests/hat/fixtures/vcp_oracle.json`.

```
tests/hat/conformance/test_conf_fixtures.py F
tests/hat/conformance/test_conf_fixtures.py:137: in test_fixtures_vcp_oracle_frozen
E   AssertionError: assert 'cf5b2990d397...a16aef92de15b' == '95e991f5a33b...549a2dabf3f4b'
E     - 95e991f5a33b950f443db7ccc9a14caff24969832c9ecd10c6e549a2dabf3f4b
```
