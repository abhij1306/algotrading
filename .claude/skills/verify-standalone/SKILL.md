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
- `PBT metrics.longest_drawdown` The longest drawdown is the longest run of points below the running maximum (polars `cum_max`), 0 exactly when the curve never falls, shorter than the curve. Harm: a run long under water reports a short recovery, or a return to the old peak still counts as under water.
- `PBT metrics.exposure` Exposure is the share of the sessions inside the union of the held spans, both ends included, 0.0 without sessions. Harm: an idle strategy reads as invested, or the exit day is missed.
- `PBT metrics.mar_ratio` MAR has the CAGR's sign and times the drawdown depth gives the CAGR; None without a drawdown. Harm: a deeper drawdown raises MAR, or a smooth run divides by zero.
- `PBT metrics.tail_trimmed_mean` The mean without the `drop` largest values is the exact mean of the rest, never above the mean with one fewer dropped, None when nothing is left. Harm: an edge resting on one outlier passes the tail test.
- `PBT metrics.cost_share` The charges over the gross P&L, exact, in [0, 1] unless the charges turned a gain into a loss, None unless the gross is a gain. Harm: costs that ate the edge read as a small share.
- `CONF imports.hat_decoupled_from_legacy` The `app` modules that `app/hat` and `tests/hat` import are exactly the `SHARED` set in `test_conf_decoupling.py` (`app.base`, `app.database`, `app.paths`, `app.utils.env_loader`: infrastructure with no legacy code and no HAT rule); a new coupling fails, and a module no longer imported must leave the set. Harm: HAT quietly grows back into code it is meant to stand apart from.
- `CONF imports.hat_suites_boot_no_legacy_app` Collecting `tests/hat` in a fresh interpreter loads neither `app.main` nor `app.routers` (the manual-trading singletons they once also listed were deleted in K4; `risk.no_manual_trading_path` keeps them deleted). Harm: HAT suites depend on booting the whole legacy app.
- `CONF imports.legacy_does_not_import_hat` Only `app/database.py` (the table registry and schema upkeep) and `app/main.py` (the wiring) import `app.hat`; no other `app` module does. Harm: deleting or moving HAT code breaks the other side.
- `CONF fixtures.vcp_oracle_frozen` The frozen legacy VCP outputs in `tests/hat/fixtures/vcp_oracle.json` match their recorded hash, case list and internal consistency. Harm: PR 6's characterization test compares against a drifting oracle.
- `PBT money.exact` (PR 75c) `exact` answers None or a finite Decimal; None, a bool, NaN, an infinity and any other object are never a number; a float (numpy's too) is refused unless `floats`, then read by its shortest text; text is read as written, in ASCII and without underscores (commas only with `grouped`; never "1_5" or another script's digits, which `Decimal` alone reads), and an exponent Decimal cannot hold is not a number; `places` rounds half-even to exactly that many places and `positive` refuses what is or rounds to 0 or less; `require_exact` raises `NotExactError` (an `InvalidOperation` and a `ValueError`) exactly when `exact` is None. Harm: two readers of one tick disagree, a float's binary expansion or a NaN reaches money, or "1,5" reads as 15.
- `PBT money.float_text` (PR 75c) A Python float and a numpy float of one value read as the Decimal the float's shortest text names. Harm: `Decimal(repr(x))` of a numpy float raises where `str` reads it.
- `CONF money.one_decimal_boundary` (PR 75c) No `app/hat` module but `money.py` reads a number into a Decimal by hand (`Decimal(str|repr|float(x))`, `Decimal(x.method())`, `Decimal(row["k"])`, or any `Decimal(...)` in a `try`); lane E's files are listed, and an entry with nothing left to move fails. Harm: the next author copies the nearest private `_dec` with its own rule.

## Run

```bash
cd backend
# every pytest, Hypothesis or break run goes under the 2 GB memory cap
CAP="systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=512M --quiet"
CI=1 $CAP python -m pytest tests/hat/properties/test_metrics.py tests/hat/properties/test_money.py -q --no-cov
$CAP python -m pytest tests/hat/conformance/test_conf_decoupling.py tests/hat/conformance/test_conf_fixtures.py \
  tests/hat/conformance/test_conf_money.py -q --no-cov
```

No check touches the network or a database. The decoupling checks read source files, plus one subprocess that collects `tests/hat` and lists loaded modules.

## Bug or model error

- A failing `imports.hat_decoupled_from_legacy` names the module and the HAT file. Fix the import (use a HAT module, or inject the dependency); add to `SHARED` only a module that holds no HAT rule and no feature code, with its reason written next to the entry.
- A failing `imports.hat_suites_boot_no_legacy_app` lists the loaded legacy modules. Find the first import that pulled them (a test helper, a root `conftest.py` import) and make it lazy or HAT-local; never filter the list.
- A failing `metrics.*` property is a real bug in `app/hat/backtest/metrics.py`; the legacy copy has the same flaw for a constant series (found here, fixed with `VOLATILITY_FLOOR`).
- A failing `fixtures.vcp_oracle_frozen` means the file changed. The legacy detector it came from is deleted, so the file is now the only record of those outputs: regenerate it only on purpose, from a recovered `pattern.py`, and change the recorded hash with it.

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
- `backend/tests/hat/properties/test_money.py`
- `backend/tests/hat/conformance/test_conf_decoupling.py`
- `backend/tests/hat/conformance/test_conf_fixtures.py`
- `backend/tests/hat/conformance/test_conf_money.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `e71a5f2025df0cbf158d242b71f28e4e4e74e4e3`
Covered digest: `76e9fbf4ff3489c910e5eb149b3ecafee0c714a6a4e0c176a742ddb87f5f4909`

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or fixture, then the break was removed and the check returned to passing.

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

Break: in `backend/app/hat/backtest/metrics.py`: drawdown measured from the first value.

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

Break: in `backend/app/hat/backtest/metrics.py`: a trade of exactly 0 is neither win nor loss.

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

Break: in `backend/app/hat/backtest/metrics.py`: the mean of absolute returns.

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

Break: in `backend/app/hat/backtest/metrics.py`: the capital guard lets a negative capital through.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:92: in test_returns_guard_bad_capital
    st.floats(min_value=-1e6, max_value=1e7, allow_nan=False),
               ^^^
tests/hat/properties/test_metrics.py:101: in test_returns_guard_bad_capital
    assert metrics.total_return(final, initial) == 0.0
E   assert -1.0 == 0.0
```

### PBT metrics.longest_drawdown

Break: in `backend/app/hat/backtest/metrics.py`: a point equal to the running peak counts as under water.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:123: in test_longest_drawdown
    assert longest == max(len(r) for r in runs)
E   assert 4 == 2
E    +  where 2 = max(<generator object test_longest_drawdown.<locals>.<genexpr>>)
E   Failing explicit example: test_longest_drawdown(
E       equity=shape: (5,)
E       Series: '' [f64]
E       [
```

### PBT metrics.exposure

Break: in `backend/app/hat/backtest/metrics.py`: a span's exit date is not counted as held.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:150: in test_exposure
    assert value == sum(1 for d in sessions if d in covered) / len(sessions)
E   assert 0.0 == (1 / 1)
E    +  where 1 = sum(<generator object test_exposure.<locals>.<genexpr>>)
E    +  and   1 = len([datetime.date(2026, 1, 1)])
E   Failing test case: test_exposure(
E       days={0},
E       held=[(datetime.date(2026, 1, 1), datetime.date(2026, 1, 1))],
```

### PBT metrics.mar_ratio

Break: in `backend/app/hat/backtest/metrics.py`: MAR divides by the (negative) drawdown, not its depth.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:169: in test_mar_ratio
    assert math.isclose(mar * -drawdown, cagr, rel_tol=1e-9, abs_tol=1e-12)
E   assert False
E    +  where False = <built-in function isclose>((-2.0 * --0.5), 1.0, rel_tol=1e-09, abs_tol=1e-12)
E    +    where <built-in function isclose> = math.isclose
E   Failing test case: test_mar_ratio(
E       cagr=1.0,
E       drawdown=-0.5,
```

### PBT metrics.tail_trimmed_mean

Break: in `backend/app/hat/backtest/metrics.py`: the trim drops the smallest values instead of the largest.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:189: in test_tail_trimmed_mean
    assert trimmed == sum(rest, Decimal(0)) / len(rest)
E   AssertionError: assert Decimal('3') == (Decimal('0') / 2)
E    +  where Decimal('0') = sum([Decimal('1'), Decimal('-1')], Decimal('0'))
E    +    where Decimal('0') = Decimal(0)
E    +  and   2 = len([Decimal('1'), Decimal('-1')])
E   Failing explicit example: test_tail_trimmed_mean(
E       values=[Decimal('-1'), Decimal('5'), Decimal('1')],
```

### PBT metrics.cost_share

Break: in `backend/app/hat/backtest/metrics.py`: the share divides by gross + charges.

```
tests/hat/properties/test_metrics.py F
tests/hat/properties/test_metrics.py:211: in test_cost_share
    assert share == charges / gross
E   AssertionError: assert Decimal('0.5') == (Decimal('0.01') / Decimal('0.01'))
E   Failing test case: test_cost_share(
E       charges=Decimal('0.01'),
E       net=Decimal('0'),
E   )
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEBQQA=') as a decorator on your test function
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

Break: appended `import app.utils.logger` to `app/hat/money.py`, a module that is not in `SHARED`.

```
tests/hat/conformance/test_conf_decoupling.py F                          [100%]
tests/hat/conformance/test_conf_decoupling.py:113: in test_imports__hat_decoupled_from_legacy
E   AssertionError: {'app.utils.logger': ['app/hat/money.py']}
E   assert {'app.utils.l...at/money.py']} == {}
```

Break: added `"app.config": "break"` to `SHARED` although nothing under HAT imports it.

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

Break: a deleted shim recreated as `app/services/execution/paper_book.py` importing `app.hat.money`.

```
tests/hat/conformance/test_conf_decoupling.py F                          [100%]
tests/hat/conformance/test_conf_decoupling.py:152: in test_imports__legacy_does_not_import_hat
E   AssertionError: assert ['services/ex...aper_book.py'] == []
E     
E     Left contains one more item: 'services/execution/paper_book.py'
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_decoupling.py::test_imports__legacy_does_not_import_hat
```

Break: recreated `app/utils/clock.py` re-exporting `app.hat.clock.SYSTEM_CLOCK`.

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

### PBT money.exact

Break: (PR 75c) a float is read without `floats` (the refusal in `_read` removed).

```
    assert floats
E   assert False
E   Failing test case: test_exact(
E       value=0.0,
```

Break: (PR 75c) `positive` checked before `places` rounds.

```
E   AssertionError: assert Decimal('0.0000') > 0
E   Failing explicit example: test_exact(
E       value=4e-05,
```

Break: (PR 75c) commas dropped from every text, not only with `grouped`.

```
E   decimal.InvalidOperation: [<class 'decimal.ConversionSyntax'>]
E   Failing explicit example: test_exact(
E       value='1,5',
E       floats=False,
```

Break: (PR 75c, code review) text with an underscore passes the text check (`Decimal` reads "1_5" as 15).

```
E   decimal.InvalidOperation: 1_5
E   Failing explicit example: test_exact(
E       value='1_5',
```

Break: (PR 75c, CodeAnt) an exponent `Decimal` cannot hold raises out of `exact` instead of reading as not a number.

```
E   decimal.InvalidOperation: [<class 'decimal.InvalidOperation'>]
E   Failing explicit example: test_exact(
E       value='1e99999999999999999999',
```

Break: (PR 75c, CodeAnt) `places` rounds half up.

```
E   AssertionError: assert Decimal('0.13') == Decimal('0.12')
```

### PBT money.float_text

Break: (PR 75c) `_read` reads a float by `repr` (the old copies' rule).

```
E   AssertionError: assert None == Decimal('0.0')
E    +  where None = exact(np.float64(0.0), floats=True)
```

### CONF money.one_decimal_boundary

Break: (PR 75c) a `_decimal` copy (`Decimal(str(raw))`) put back in `data/gapscan.py`.

```
E   AssertionError: assert {'app/hat/dat...can.py': [88]} == {}
```

Second break: (PR 75c) `admin._price` reads with `Decimal(raw)` in a `try` catching `InvalidOperation`.

```
E   AssertionError: assert {'app/hat/admin.py': [190]} == {}
```

Fourth break: (PR 75c, code review) `daily/intraday` reads a gapper's open with `Decimal(row["open"])` again.

```
E   AssertionError: assert {'app/hat/dai...ay.py': [322]} == {}
```

Fifth break: (PR 75c, code review) `admin._price` reads with `Decimal(raw)` in a `try` catching `Exception`.

```
E   AssertionError: assert {'app/hat/admin.py': [190]} == {}
```

Third break: (PR 75c) `app/hat/data/live.py` (no hand read left) added to `LANE_E`.

```
E   AssertionError: app/hat/data/live.py
E   assert []
```
