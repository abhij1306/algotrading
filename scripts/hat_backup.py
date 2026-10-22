"""Nightly HAT backup: a pg_dump and the HAT_DATA_ROOT store, copied to two targets and checked.

Usage (from the repo root, with the .env variables in the environment):
    python scripts/hat_backup.py run --local /mnt/backup/hat --remote /mnt/bucket/hat
    python scripts/hat_backup.py verify /mnt/bucket/hat/20261021T180000Z

`run` writes one backup set, a directory named by its UTC start time, holding
    hat.dump       pg_dump of the HAT database in the custom format (pg_restore reads it)
    hat_data.tar   every file under HAT_DATA_ROOT (Parquet store, raw bhavcopy zips, backtests)
    manifest.json  size and sha256 of both files and of every file inside the tar
into the local target, copies it to the remote target, then re-reads both copies against the
manifest (HAT-OPS-060, docs/specs/hat/10-ops.md §7). Any missing, resized or changed file fails
the run with exit code 1; the host timer that runs it alerts on that exit code (runbook §9).
Older sets beyond --keep are pruned only after a run verified.

The targets are plain directories: the local one is the external disk, the remote one a mounted
cloud bucket (an rclone or s3fs mount), so the script holds no bucket credentials. The database
password reaches pg_dump only through the PGPASSWORD environment variable (copied from
DB_PASSWORD when unset) or ~/.pgpass, never on the command line. pg_dump must be version 18
(the server's major): a host install, or `--pg-dump "docker compose --env-file .env -f
deploy/hat/docker-compose.yml exec -T -e PGPASSWORD postgres pg_dump"` with DB_HOST=localhost
to use the one inside the postgres container. The dump is read from pg_dump's stdout.

The manifest functions (`build_manifest`, `archive_manifest`, `verify_manifest`) do no
subprocess work, so the property `ops.backup_manifest` checks them directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

DUMP_NAME = "hat.dump"
DATA_TAR = "hat_data.tar"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
FILES = "files"
ARCHIVES = "archives"
SIZE = "size"
SHA256 = "sha256"
CHUNK = 1 << 20
SET_NAME = re.compile(r"^\d{8}T\d{6}Z$")
DEFAULT_DATA_ROOT = Path("data_system") / "hat"
DEFAULT_KEEP = 14

Entry = dict[str, int | str]


# --- manifest core (no subprocesses) --------------------------------------------------------


def _sha256_stream(stream: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in stream:
        digest.update(chunk)
    return digest.hexdigest()


def _chunks(handle: IO[bytes]) -> Iterable[bytes]:
    return iter(lambda: handle.read(CHUNK), b"")


def file_entry(path: Path) -> Entry:
    """Size and sha256 of one file."""
    with path.open("rb") as handle:
        return {SIZE: path.stat().st_size, SHA256: _sha256_stream(_chunks(handle))}


def build_manifest(base: Path, names: Iterable[str]) -> dict:
    """The manifest of the files `names` (POSIX paths relative to `base`)."""
    return {
        "version": MANIFEST_VERSION,
        FILES: {name: file_entry(base / name) for name in sorted(names)},
    }


def archive_manifest(tar_path: Path) -> dict[str, Entry]:
    """Size and sha256 of every regular file inside a tar, keyed by member name."""
    entries: dict[str, Entry] = {}
    with tarfile.open(tar_path, "r") as archive:
        for member in archive:
            handle = archive.extractfile(member) if member.isfile() else None
            if handle is not None:
                entries[member.name] = {SIZE: member.size, SHA256: _sha256_stream(_chunks(handle))}
    return entries


def compare_entries(where: str, expected: Mapping[str, Entry], actual: Mapping) -> list[str]:
    """One line per listed file that is missing from `actual` or differs in size or checksum."""
    problems = []
    for name, want in sorted(expected.items()):
        got = actual.get(name)
        if got is None:
            problems.append(f"{where}: {name} is missing")
        elif got[SIZE] != want[SIZE]:
            problems.append(f"{where}: {name} has size {got[SIZE]}, expected {want[SIZE]}")
        elif got[SHA256] != want[SHA256]:
            problems.append(f"{where}: {name} checksum differs")
    return problems


def _present_entries(directory: Path, names: Iterable[str]) -> dict[str, Entry]:
    return {name: file_entry(directory / name) for name in names if (directory / name).is_file()}


def _archive_problems(directory: Path, archives: Mapping[str, Mapping[str, Entry]]) -> list[str]:
    problems = []
    for tar_name, members in sorted(archives.items()):
        tar_path = directory / tar_name
        if not tar_path.is_file():
            continue  # already reported as a missing file
        try:
            actual = archive_manifest(tar_path)
        except (tarfile.TarError, OSError):
            problems.append(f"{directory}: {tar_name} cannot be read as a tar")
            continue
        problems.extend(compare_entries(f"{directory}/{tar_name}", members, actual))
    return problems


def verify_manifest(directory: Path, manifest: Mapping) -> list[str]:
    """Every problem found re-reading `directory` against `manifest`; empty when it matches."""
    files = manifest.get(FILES) or {}
    if not files:
        return [f"{directory}: the manifest lists no files"]
    problems = compare_entries(str(directory), files, _present_entries(directory, files))
    problems.extend(_archive_problems(directory, manifest.get(ARCHIVES) or {}))
    return problems


# --- the backup run -------------------------------------------------------------------------


def data_files(data_root: Path, exclude: Iterable[Path]) -> list[str]:
    """Every regular file under `data_root`, relative, skipping the backup targets themselves."""
    skipped = [path.resolve() for path in exclude]
    names = []
    for path in sorted(data_root.rglob("*")):
        resolved = path.resolve()
        if path.is_file() and not any(resolved.is_relative_to(skip) for skip in skipped):
            names.append(path.relative_to(data_root).as_posix())
    return names


def write_data_tar(data_root: Path, names: list[str], tar_path: Path) -> None:
    with tarfile.open(tar_path, "w") as archive:
        for name in names:
            archive.add(data_root / name, arcname=name, recursive=False)


def pg_dump_command(pg_dump: str, env: Mapping[str, str]) -> list[str]:
    """The pg_dump argv (the dump goes to stdout). `pg_dump` may be a command prefix such as
    `docker compose ... exec -T -e PGPASSWORD postgres pg_dump`. The argv carries no password:
    that goes through PGPASSWORD or ~/.pgpass."""
    return [
        *shlex.split(pg_dump),
        "--format=custom",
        "--no-password",
        f"--host={env.get('DB_HOST', 'localhost')}",
        f"--port={env.get('DB_PORT', '5432')}",
        f"--username={env.get('DB_USER', 'postgres')}",
        f"--dbname={env.get('DB_NAME', 'algotrading')}",
    ]


def pg_dump_env(env: Mapping[str, str]) -> dict[str, str]:
    child = dict(env)
    if "PGPASSWORD" not in child and env.get("DB_PASSWORD"):
        child["PGPASSWORD"] = env["DB_PASSWORD"]
    return child


def run_pg_dump(pg_dump: str, out: Path) -> None:
    command = pg_dump_command(pg_dump, os.environ)
    with out.open("wb") as handle:
        result = subprocess.run(command, env=pg_dump_env(os.environ), stdout=handle, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump exited with code {result.returncode}")


def write_manifest(directory: Path, manifest: Mapping) -> None:
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (directory / MANIFEST_NAME).write_text(text, encoding="utf-8")


def read_manifest(directory: Path) -> dict:
    return json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))


def copy_set(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.iterdir()):
        shutil.copy2(path, target / path.name)


def verify_set(directory: Path, manifest: Mapping) -> list[str]:
    """The set at `directory` against the manifest in hand, including its own manifest copy."""
    if not (directory / MANIFEST_NAME).is_file():
        return [f"{directory}: {MANIFEST_NAME} is missing"]
    problems = verify_manifest(directory, manifest)
    if read_manifest(directory) != manifest:
        problems.append(f"{directory}: {MANIFEST_NAME} differs from the run's manifest")
    return problems


def prune(target: Path, keep: int) -> list[Path]:
    """Delete the oldest backup sets beyond `keep`; returns what was removed."""
    sets = sorted(p for p in target.iterdir() if p.is_dir() and SET_NAME.match(p.name))
    removed = sets[: max(len(sets) - keep, 0)]
    for path in removed:
        shutil.rmtree(path)
    return removed


def run_backup(args: argparse.Namespace) -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    local = args.local / stamp
    remote = args.remote / stamp
    local.mkdir(parents=True, exist_ok=False)
    run_pg_dump(args.pg_dump, local / DUMP_NAME)
    names = data_files(args.data_root, [args.local, args.remote])
    write_data_tar(args.data_root, names, local / DATA_TAR)
    manifest = build_manifest(local, [DUMP_NAME, DATA_TAR])
    manifest[ARCHIVES] = {DATA_TAR: archive_manifest(local / DATA_TAR)}
    manifest["created"] = stamp
    write_manifest(local, manifest)
    copy_set(local, remote)
    problems = verify_set(local, manifest) + verify_set(remote, manifest)
    if problems:
        print("BACKUP FAILED", *problems, sep="\n  ", file=sys.stderr)
        return 1
    for target in (args.local, args.remote):
        prune(target, args.keep)
    print(f"backup {stamp}: {len(names)} data files, verified at {local} and {remote}")
    return 0


def run_verify(args: argparse.Namespace) -> int:
    problems = verify_set(args.set_dir, read_manifest(args.set_dir))
    if problems:
        print("VERIFY FAILED", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"{args.set_dir}: every file matches its manifest")
    return 0


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="take a backup and verify it at both targets")
    run.add_argument("--local", type=Path, default=_env_path("HAT_BACKUP_LOCAL"))
    run.add_argument("--remote", type=Path, default=_env_path("HAT_BACKUP_REMOTE"))
    run.add_argument(
        "--data-root", type=Path, default=_env_path("HAT_DATA_ROOT") or DEFAULT_DATA_ROOT
    )
    run.add_argument("--pg-dump", default="pg_dump", help="pg_dump command (version 18)")
    run.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="sets kept per target")
    verify = sub.add_parser("verify", help="re-check one backup set against its manifest")
    verify.add_argument("set_dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run" and (args.local is None or args.remote is None):
        parser.error(
            "both targets are needed: --local/HAT_BACKUP_LOCAL, --remote/HAT_BACKUP_REMOTE"
        )
    if args.command == "run" and args.local.resolve() == args.remote.resolve():
        parser.error("--local and --remote must be different directories")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_backup(args) if args.command == "run" else run_verify(args)
    except (OSError, RuntimeError, tarfile.TarError, json.JSONDecodeError) as exc:
        print(f"BACKUP FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
