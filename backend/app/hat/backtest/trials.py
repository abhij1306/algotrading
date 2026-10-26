"""The trial registry a run's deflated Sharpe counts its trials from
(`HAT_DATA_ROOT/backtests/trials.json`, docs/backtests/result-standard.md).

Trials are kept per family: one strategy on one universe over one window (`family_key`), so a
run of the same parameters on another universe or window neither counts as a duplicate nor
pools with them. A trial is a distinct `resolved_hash` among the family's completed in-sample
runs: a run whose resolved parameters hash like an earlier one's is a duplicate, not a new
trial. The file holds, per family, each hash with the first run that tried it and that run's
per-session Sharpe ratio (the deflated Sharpe's trial variance reads them). Every run reads it;
a completed in-sample run adds its hash under an exclusive lock (read, merge, replace in one
rename). It is backtest output, under `HAT_DATA_ROOT` and never in the database; a reset of
the run folders never deletes it.

Prior trials are variants tried before the registry saw them (runs since deleted): `seed`
records each under a `prior:` key with its `source` (the sweep file and label) and no run or
Sharpe, so they raise the family's count without touching its variance. A strategy's trial
budget counts the family's own runs only (`counts`), and a sweep given its criteria refuses a
new trial past it (`over_budget`).

    python -m app.hat.backtest.trials seed --strategy S1 --universe NIFTY500_PIT \
        --from 2016-01-01 --to 2021-12-31 SWEEP.yaml [...]
    python -m app.hat.backtest.trials show
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..data.store import atomic_replace

__all__ = [
    "PRIOR",
    "Trial",
    "counts",
    "family",
    "family_key",
    "family_of",
    "over_budget",
    "prior_variants",
    "read_registry",
    "record_trial",
    "registry_path",
    "seed",
]

REGISTRY_VERSION = 2
PRIOR = "prior:"


def registry_path(root: Path) -> Path:
    return root / "backtests" / "trials.json"


def family_key(strategy: str, universe: str, start: date | str, end: date | str) -> str:
    """The registry's key of a trial family: `S1/NIFTY500_PIT/2016-01-01..2021-12-31`."""
    return f"{strategy}/{universe}/{start}..{end}"


def family_of(run: Mapping[str, Any], validated: Mapping[str, Any] | None) -> str | None:
    """The family key of a `run.json`: its strategy, universe and window, or for a hold-out run
    the window of the in-sample run it validates (`validated`, that run's `run.json`); None when
    that run cannot be read."""
    identity, data = run.get("identity") or {}, run.get("data") or {}
    if data.get("split") == "hold_out":
        if validated is None:
            return None
        data = {
            **data,
            **{k: (validated.get("data") or {}).get(k) for k in ("period_start", "period_end")},
        }
    return family_key(
        str(identity.get("strategy")),
        str(data.get("universe")),
        str(data.get("period_start")),
        str(data.get("period_end")),
    )


@dataclass(frozen=True)
class Trial:
    run_id: str | None
    sr: float | None
    source: str | None = None  # a prior trial's sweep file and label


def _trial(entry: object) -> Trial | None:
    if not isinstance(entry, Mapping):
        return None
    run_id, sr, source = entry.get("run_id"), entry.get("sr"), entry.get("source")
    if not isinstance(run_id, str) and not isinstance(source, str):
        return None
    return Trial(
        run_id if isinstance(run_id, str) else None,
        float(sr) if isinstance(sr, (int, float)) and not isinstance(sr, bool) else None,
        source if isinstance(source, str) else None,
    )


def _parse(raw: object) -> dict[str, dict[str, Trial]]:
    """The registry's trials by family and hash; an entry that is not one is left out. A file of
    another version raises `trials_registry_version`: read as empty it would reset every count."""
    if not isinstance(raw, Mapping) or raw.get("version") != REGISTRY_VERSION:
        raise ValueError("trials_registry_version")
    out: dict[str, dict[str, Trial]] = {}
    for key, hashes in (raw.get("families") or {}).items():
        if not isinstance(hashes, Mapping):
            continue
        for digest, entry in hashes.items():
            trial = _trial(entry)
            if trial is not None:
                out.setdefault(str(key), {})[str(digest)] = trial
    return out


def read_registry(root: Path) -> dict[str, dict[str, Trial]]:
    """The registry (empty when there is no file yet); an unreadable file raises, so a run
    never scores its DSR on a count it could not read."""
    path = registry_path(root)
    if not path.is_file():
        return {}
    return _parse(json.loads(path.read_text(encoding="utf-8")))


def family(
    registry: Mapping[str, Mapping[str, Trial]],
    key: str,
    own: tuple[str, float | None] | None,
) -> tuple[int, list[float]]:
    """(the family's trial count, the Sharpe ratios of its trials) with `own` (a completed
    in-sample run's hash and Sharpe) counted once more unless its hash is already a trial."""
    trials = dict(registry.get(key, {}))
    if own is not None and own[0] not in trials:
        trials[own[0]] = Trial("", own[1])
    return len(trials), [t.sr for t in trials.values() if t.sr is not None]


def counts(registry: Mapping[str, Mapping[str, Trial]], key: str) -> tuple[int, int]:
    """(the family's trials, of them the prior ones `seed` recorded)."""
    hashes = registry.get(key, {})
    return len(hashes), sum(1 for h in hashes if h.startswith(PRIOR))


def over_budget(
    registry: Mapping[str, Mapping[str, Trial]], key: str, digest: str, budget: int
) -> bool:
    """Whether a completed run of the resolved hash `digest` would be a new trial of the family
    `key` past `budget`: the hash is not one of its trials yet and the family's own trials (the
    prior ones excluded, `counts`) have reached the budget. A duplicate is never refused."""
    total, prior = counts(registry, key)
    return digest not in registry.get(key, {}) and total - prior >= budget


def _document(registry: Mapping[str, Mapping[str, Trial]]) -> dict[str, Any]:
    def entry(t: Trial) -> dict[str, Any]:
        out: dict[str, Any] = {"run_id": t.run_id, "sr": t.sr}
        return out if t.source is None else {**out, "source": t.source}

    return {
        "version": REGISTRY_VERSION,
        "families": {
            k: {h: entry(t) for h, t in sorted(hashes.items())}
            for k, hashes in sorted(registry.items())
        },
    }


def _update(root: Path, key: str, new: Mapping[str, Trial]) -> int:
    """Add the `new` trials to a family under the lock (a known hash keeps its first entry);
    the number added."""
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(".trials.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = read_registry(root)
        hashes = registry.setdefault(key, {})
        added = {d: t for d, t in new.items() if d not in hashes}
        if not added:
            return 0
        hashes.update(added)
        text = json.dumps(_document(registry), indent=1)
        atomic_replace(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))
        return len(added)


def record_trial(root: Path, key: str, digest: str, run_id: str, sr: float | None) -> None:
    """Add a completed in-sample run's hash to its family (a known hash keeps its first run)."""
    _update(root, key, {digest: Trial(run_id, sr)})


def prior_variants(name: str, raw: object) -> dict[str, str]:
    """The variants of one sweep file (`name`, its parsed YAML: a list of variants or a mapping
    with `variants`) as prior trials: `prior:` + sha256 of the file name and the variant
    without its label, by its source `name#label`. `trials_bad_sweep` for a file that is not
    one."""
    items = raw.get("variants") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("trials_bad_sweep")
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("label"), str):
            raise ValueError("trials_bad_sweep")
        body = {k: v for k, v in item.items() if k != "label"}
        text = json.dumps([name, body], sort_keys=True, default=str)
        out[PRIOR + hashlib.sha256(text.encode()).hexdigest()] = f"{name}#{item['label']}"
    return out


def seed(root: Path, key: str, files: Sequence[tuple[str, str]]) -> tuple[int, int]:
    """Record every variant of the sweep `files` (each its file name and text; the command line
    opens them) as a prior trial of the family `key`; (added, the family's count after)."""
    found: dict[str, Trial] = {}
    for name, text in files:
        for digest, source in prior_variants(name, yaml.safe_load(text)).items():
            found[digest] = Trial(None, None, source)
    added = _update(root, key, found)
    return added, counts(read_registry(root), key)[0]


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the command line
    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest.trials")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("seed", help="record earlier sweeps' variants as prior trials")
    add.add_argument("--strategy", required=True)
    add.add_argument("--universe", required=True)
    add.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    add.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    add.add_argument("files", nargs="+", type=argparse.FileType("r", encoding="utf-8"))
    sub.add_parser("show", help="print each family's trials and prior trials")
    args = parser.parse_args(argv)
    root = operator_settings().data_root
    try:
        if args.command == "seed":
            key = family_key(args.strategy, args.universe, args.start, args.end)
            named = [(Path(f.name).name, f.read()) for f in args.files]
            added, total = seed(root, key, named)
            sys.stdout.write(json.dumps({"family": key, "added": added, "trials": total}) + "\n")
            return 0
        registry = read_registry(root)
        shown = {
            k: dict(zip(("trials", "prior"), counts(registry, k), strict=True)) for k in registry
        }
        sys.stdout.write(json.dumps(shown, indent=1) + "\n")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.stderr.write(f"trials refused: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
