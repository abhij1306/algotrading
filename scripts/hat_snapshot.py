"""Snapshot release: database dumps and data folders uploaded as GitHub Release assets of the
private data repo, each under GitHub's 2 GB asset limit, with a SHA-256 record (docs/runbook.md,
Snapshot and restore).

Usage (from the repo root):
    python scripts/hat_snapshot.py release --tag snapshot-2026-10-29 \\
        --repo OWNER/algotradingdata --record /data/workspace/algotradingdata \\
        --db hat_live --db hat_state --part raw=$HAT_DATA_ROOT/raw --part bars_5m=... \\
        [--exclude PART:PATTERN ...] [--incremental]
    python scripts/hat_snapshot.py check --tag snapshot-2026-10-29 --repo OWNER/algotradingdata

`release` streams each database dump (`pg_dump --format=custom`, the command from --pg-dump or
HAT_PG_DUMP) and each part (a gzipped tar of a folder) into numbered assets `<name>.partNN` in
the stage directory, uploads each asset with `gh release upload` as soon as it is full and
deletes it, so the stage never holds more than one asset. The record, `snapshots/<tag>.json` in
the data repo clone, lists every asset's size and sha256 and every file each tar holds; it is
uploaded too, with `SHA256SUMS`, then committed and pushed. `--incremental` takes into each part
only the files that differ from what the earlier records in `snapshots/` already hold (and lists
the files gone since): a restore unpacks the full release, then each later one in order.
`check` compares the release's assets on GitHub (size and the sha256 digest GitHub keeps) with
the record.

Restore: `cat <name>.part* > <name>`, then `pg_restore -d DB <name>` for a dump or
`tar -xzf <name>` for a part. The database password never reaches this script's argv: pg_dump
reads PGPASSWORD or ~/.pgpass.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

ASSET_LIMIT = 1_900_000_000  # bytes per asset; GitHub refuses 2 GiB and over
CHUNK = 1 << 20
RECORD_DIR = "snapshots"
SUMS_NAME = "SHA256SUMS"
# Never uploaded, whatever the part: credential-looking files (env files, keys, token and cookie
# jars, secrets logs) and browser profiles.
SECRET_PATTERNS = (
    ".env*",
    "*.key",
    "*.pem",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    ".pgpass",
    ".netrc",
    "*token*",
    "*secret*",
    "*credential*",
    "*cookie*",
    "chrome",
    "chromium",
)
# A tag, part or database name, and OWNER/NAME of a repo: never an option or a path.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")

Entry = dict[str, int | str]


# --- pure parts -----------------------------------------------------------------------------


def excluded(rel: str, patterns: Iterable[str]) -> bool:
    """Whether the POSIX path `rel` (relative to its part's root) is excluded: a pattern without
    a slash matches any one path component (`__pycache__`, `*.pyc`), one with a slash matches
    the path or a folder prefix of it from the part's root (`/tmp`, `backtests/runs`)."""
    parts = rel.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in patterns:
        names = prefixes if "/" in pattern else parts
        if any(fnmatch.fnmatchcase(name, pattern.removeprefix("/")) for name in names):
            return True
    # a credential-looking name in any case (`.ENV`, `MyToken.json`)
    return any(fnmatch.fnmatchcase(name.lower(), p) for p in SECRET_PATTERNS for name in parts)


def part_excludes(specs: Iterable[str], part: str) -> list[str]:
    """`SECRET_PATTERNS` and the patterns of the `--exclude PART:PATTERN` specs that apply to
    `part` (PART `*` is all)."""
    out = []
    for spec in specs:
        name, _, pattern = spec.partition(":")
        if not pattern:
            raise ValueError(f"--exclude {spec!r} is not PART:PATTERN")
        if name in ("*", part):
            out.append(pattern)
    return [*SECRET_PATTERNS, *out]


def held_files(records: Sequence[Mapping]) -> dict[str, dict[str, Entry]]:
    """Per part, the files the records hold once restored in order (a full release's files
    replace the part's state, an incremental one's are added and its removed files dropped):
    what an incremental release need not carry again."""
    state: dict[str, dict[str, Entry]] = {}
    for record in sorted(records, key=lambda r: r["created"]):
        for part, body in record.get("parts", {}).items():
            if not record.get("incremental"):
                state[part] = {}
            files = state.setdefault(part, {})
            files.update(body.get("files", {}))
            for name in body.get("removed", []):
                files.pop(name, None)
    return state


def changes(held: Mapping[str, Entry], current: Mapping[str, Entry]) -> tuple[list[str], list[str]]:
    """The files of `current` that `held` lacks or holds with another size or sha256, and the
    held files no longer present; both sorted."""
    changed = sorted(
        name
        for name, entry in current.items()
        if held.get(name, {}).get("sha256") != entry["sha256"]
        or held[name].get("size") != entry["size"]
    )
    return changed, sorted(set(held) - set(current))


def sums_text(assets: Mapping[str, Entry]) -> str:
    """`sha256sum -c` input for the assets."""
    return "".join(f"{assets[name]['sha256']}  {name}\n" for name in sorted(assets))


def asset_problems(
    record_assets: Mapping[str, Entry], remote: Mapping[str, Entry], extra: Iterable[str] = ()
) -> list[str]:
    """Each recorded asset that GitHub lacks or holds with another size or sha256 digest, and
    each release asset the record does not list (other than the `extra` names: the sums and the
    record itself), which a `cat <name>.part*` restore would pick up."""
    allowed = set(record_assets) | set(extra)
    problems = [
        f"{name} is on the release but not in the record"
        for name in sorted(remote)
        if name not in allowed
    ]
    for name, want in sorted(record_assets.items()):
        got = remote.get(name)
        if got is None:
            problems.append(f"{name} is missing from the release")
        elif got["size"] != want["size"]:
            problems.append(f"{name} has size {got['size']}, recorded {want['size']}")
        elif got.get("sha256") and got["sha256"] != want["sha256"]:
            problems.append(f"{name} digest differs from the record")
    return problems


# --- streaming --------------------------------------------------------------------------------


class AssetWriter:
    """A write-only stream cut into numbered files of at most `limit` bytes; each full file is
    handed to `done(path, entry)` (which uploads and deletes it) before the next one opens."""

    def __init__(
        self, stage: Path, name: str, done: Callable[[Path, Entry], None], limit: int
    ) -> None:
        self.stage, self.name, self.done, self.limit = stage, name, done, limit
        self.index = 0
        self.handle: IO[bytes] | None = None
        self.size = 0
        self.digest = hashlib.sha256()

    def _path(self) -> Path:
        return self.stage / f"{self.name}.part{self.index:03d}"

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            if self.handle is None:
                self.handle = self._path().open("wb")
                self.size, self.digest = 0, hashlib.sha256()
            take = view[: self.limit - self.size]
            self.handle.write(take)
            self.digest.update(take)
            self.size += len(take)
            view = view[len(take) :]
            if self.size >= self.limit:
                self._finish()
        return len(data)

    def _finish(self) -> None:
        assert self.handle is not None
        self.handle.close()
        self.handle = None
        self.done(self._path(), {"size": self.size, "sha256": self.digest.hexdigest()})
        self.index += 1

    def close(self) -> None:
        if self.handle is not None or self.index == 0:
            if self.handle is None:  # an empty stream still gives one (empty) asset
                self.handle = self._path().open("wb")
                self.size, self.digest = 0, hashlib.sha256()
            self._finish()

    def flush(self) -> None:
        """Nothing to flush: each part file is closed when it is handed on."""


class _Hashing:
    """A file read through, its sha256 kept."""

    def __init__(self, handle: IO[bytes]) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.digest.update(data)
        return data


def walk(root: Path, patterns: Sequence[str]) -> list[str]:
    """Every regular file under `root` not excluded, relative and sorted (symlinks skipped)."""
    names = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if base == "." else f"{base}/"
        dirnames[:] = [d for d in dirnames if not excluded(prefix + d, patterns)]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink() and not excluded(prefix + name, patterns):
                names.append(prefix + name)
    return sorted(names)


def file_entry(path: Path) -> Entry:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest()}


def member_name_ok(root: Path, name: str) -> bool:
    """Whether `name` is a relative path that stays inside `root` (no `..`, not absolute, no
    symlink out): what an archive member may be called."""
    parts = name.split("/")
    if not name or name.startswith("/") or ".." in parts or "" in parts:
        return False
    return (root / name).resolve().is_relative_to(root.resolve())


def write_tar(root: Path, names: Sequence[str], out: AssetWriter) -> dict[str, Entry]:
    """A gzipped tar of `names` (relative to `root`) into `out`; each file's size and sha256 as
    it went into the tar."""
    entries: dict[str, Entry] = {}
    with tarfile.open(fileobj=out, mode="w|gz", compresslevel=1) as archive:
        for name in names:
            path = root / name
            if not member_name_ok(root, name):
                raise ValueError(f"{name} is not a path inside {root}")
            with path.open("rb") as handle:
                archive.inodes = {}  # every path a regular file, never a link to another member
                info = archive.gettarinfo(arcname=name, fileobj=handle)
                before = os.fstat(handle.fileno())
                reader = _Hashing(handle)
                # writes, never extracts; every name passed member_name_ok
                archive.addfile(info, reader)  # NOSONAR
                after = os.fstat(handle.fileno())
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise RuntimeError(f"{path} changed while it was archived")
            entries[name] = {"size": info.size, "sha256": reader.digest.hexdigest()}
    out.close()
    return entries


def dump_database(pg_dump: str, database: str, out: AssetWriter) -> None:
    command = [*shlex.split(pg_dump), "--format=custom", "--no-password", f"--dbname={database}"]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as proc:  # noqa: S603  # NOSONAR - validated db name
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(CHUNK), b""):
            out.write(chunk)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump of {database} exited with code {proc.returncode}")
    out.close()


# --- GitHub and git -------------------------------------------------------------------------


def gh(*args: str, capture: bool = False) -> str:
    result = subprocess.run(  # noqa: S603
        ["gh", *args],  # noqa: S607 - the operator's gh on PATH
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout if capture else ""


def ensure_release(repo: str, tag: str) -> None:
    probe = subprocess.run(  # noqa: S603  # NOSONAR - tag and repo validated
        ["gh", "release", "view", tag, "--repo", repo],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        notes = "HAT data snapshot; restore: README.md and snapshots/<tag>.json of this repo."
        gh("release", "create", tag, "--repo", repo, "--title", tag, "--notes", notes)
        return
    # a retry of a run that failed before its record: its old parts would mix into a restore
    for name in remote_assets(repo, tag):
        gh("release", "delete-asset", tag, name, "--repo", repo, "--yes")


def remote_assets(repo: str, tag: str) -> dict[str, Entry]:
    out = gh("release", "view", tag, "--repo", repo, "--json", "assets", capture=True)
    assets = {}
    for asset in json.loads(out)["assets"]:
        digest = asset.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        assets[asset["name"]] = {"size": asset["size"], "sha256": sha}
    return assets


def uploader(repo: str, tag: str, assets: dict[str, Entry]) -> Callable[[Path, Entry], None]:
    def done(path: Path, entry: Entry) -> None:
        gh("release", "upload", tag, str(path), "--repo", repo, "--clobber")
        path.unlink()
        assets[path.name] = entry
        print(f"  uploaded {path.name}: {entry['size']:,} bytes", flush=True)

    return done


def git(clone: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # NOSONAR - fixed git verbs, paths after --
        ["git", "-C", str(clone), *args],  # noqa: S607
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def commit_record(clone: Path, path: Path, message: str) -> None:
    rel = str(path.relative_to(clone))
    git(clone, "add", "--", rel)
    git(clone, "commit", "-q", "-m", message, "--", rel)
    try:
        git(clone, "pull", "-q", "--rebase", "--autostash", "origin", "main")
    except subprocess.CalledProcessError:
        subprocess.run(["git", "-C", str(clone), "rebase", "--abort"], check=False)  # noqa: S603, S607  # NOSONAR
        raise
    git(clone, "push", "-q", "origin", "HEAD:main")


# --- commands -------------------------------------------------------------------------------


def _parts(specs: Sequence[str]) -> dict[str, Path]:
    parts = {}
    for spec in specs:
        name, _, path = spec.partition("=")
        if not path or name in parts or not NAME.match(name):
            raise ValueError(f"--part {spec!r} is not a new NAME=PATH")
        if not Path(path).is_dir():
            raise ValueError(f"--part {name}: {path} is not a directory")
        parts[name] = Path(path)
    return parts


def read_records(clone: Path) -> list[dict]:
    folder = clone / RECORD_DIR
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


def run_release(args: argparse.Namespace) -> int:
    parts = _parts(args.part)
    record_path = args.record / RECORD_DIR / f"{args.tag}.json"
    if record_path.exists():
        raise ValueError(f"{record_path} exists: one record per tag")
    held = held_files(read_records(args.record)) if args.incremental else {}
    if args.stage is not None:
        args.stage.mkdir(parents=True, exist_ok=True)
    # a private (0700) folder of this run: at most one asset at a time, removed at the end
    stage = Path(tempfile.mkdtemp(prefix="hat-snapshot-", dir=args.stage))
    try:
        return _release(args, parts, record_path, held, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _release(
    args: argparse.Namespace,
    parts: Mapping[str, Path],
    record_path: Path,
    held: Mapping[str, Mapping[str, Entry]],
    stage: Path,
) -> int:
    ensure_release(args.repo, args.tag)
    assets: dict[str, Entry] = {}
    done = uploader(args.repo, args.tag, assets)
    record: dict = {
        "tag": args.tag,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "incremental": args.incremental,
        "databases": {},
        "parts": {},
    }
    for database in args.db:
        name = f"{database}.dump"
        print(f"dump {database}", flush=True)
        dump_database(args.pg_dump, database, AssetWriter(stage, name, done, args.limit))
        record["databases"][database] = name
    for part, root in parts.items():
        names = walk(root, part_excludes(args.exclude, part))
        removed: list[str] = []
        if args.incremental:
            current = {n: file_entry(root / n) for n in names}
            names, removed = changes(held.get(part, {}), current)
        print(f"part {part}: {len(names)} files from {root}", flush=True)
        writer = AssetWriter(stage, f"{part}.tar.gz", done, args.limit)
        files = write_tar(root, names, writer)
        record["parts"][part] = {
            "source": str(root),
            "asset": f"{part}.tar.gz",
            "excludes": part_excludes(args.exclude, part)[len(SECRET_PATTERNS) :],
            "files": files,
            "removed": removed,
        }
    record["assets"] = dict(sorted(assets.items()))
    staged = stage / record_path.name  # into the clone only once GitHub matches it
    text = json.dumps(record, indent=1, sort_keys=True) + "\n"
    staged.write_text(text)  # NOSONAR - a validated tag names it
    sums = stage / SUMS_NAME
    sums.write_text(sums_text(record["assets"]))
    gh("release", "upload", args.tag, str(sums), str(staged), "--repo", args.repo, "--clobber")
    sums.unlink()
    extra = (SUMS_NAME, staged.name)
    problems = asset_problems(record["assets"], remote_assets(args.repo, args.tag), extra)
    if problems:
        print("SNAPSHOT FAILED", *problems, sep="\n  ", file=sys.stderr)
        return 1
    record_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(staged, record_path)  # the stage is on another disk
    commit_record(args.record, record_path, f"snapshot {args.tag}")
    total = sum(int(a["size"]) for a in assets.values())
    print(f"snapshot {args.tag}: {len(assets)} assets, {total:,} bytes, checked on GitHub")
    return 0


def run_check(args: argparse.Namespace) -> int:
    record = json.loads((args.record / RECORD_DIR / f"{args.tag}.json").read_text())
    extra = (SUMS_NAME, f"{args.tag}.json")
    problems = asset_problems(record["assets"], remote_assets(args.repo, args.tag), extra)
    if problems:
        print("CHECK FAILED", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"{args.tag}: {len(record['assets'])} assets match the record")
    return 0


def _matching(pattern: re.Pattern[str], what: str) -> Callable[[str], str]:
    """An argparse type that accepts only values matching `pattern` (never an option)."""

    def check(value: str) -> str:
        if not pattern.match(value):
            raise argparse.ArgumentTypeError(f"not a valid {what}: {value!r}")
        return value

    return check


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("release", "check"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--tag", required=True, type=_matching(NAME, "tag"))
        cmd.add_argument(
            "--repo", required=True, type=_matching(REPO, "OWNER/NAME"), help="the data repo"
        )
        cmd.add_argument(
            "--record",
            type=Path,
            default=Path(os.environ.get("HAT_DATA_REPO", "/data/workspace/algotradingdata")),
            help="the data repo clone (default HAT_DATA_REPO)",
        )
        if name == "release":
            cmd.add_argument(
                "--db",
                action="append",
                default=[],
                type=_matching(NAME, "database name"),
                help="database to dump",
            )
            cmd.add_argument("--part", action="append", default=[], help="NAME=FOLDER to tar")
            cmd.add_argument("--exclude", action="append", default=[], help="PART:PATTERN")
            cmd.add_argument("--incremental", action="store_true")
            cmd.add_argument(
                "--stage",
                type=Path,
                default=_env_path("HAT_SNAPSHOT_STAGE"),
                help="base folder of the run's private stage (default HAT_SNAPSHOT_STAGE, else "
                "the system temp folder); on a disk with 2 GB free",
            )
            cmd.add_argument("--limit", type=int, default=ASSET_LIMIT, help=argparse.SUPPRESS)
            cmd.add_argument("--pg-dump", default=os.environ.get("HAT_PG_DUMP", "pg_dump"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_release(args) if args.command == "release" else run_check(args)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"SNAPSHOT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
