import logging
import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """
    Manually load .env file into os.environ.
    Replaces python-dotenv dependency to fix PyInstaller build issues.
    """
    env_path = _find_env_file(path)
    if env_path is None:
        return

    try:
        with env_path.open() as f:
            for line in f:
                entry = _parse_env_line(line)
                if entry and entry[0] not in os.environ:
                    os.environ[entry[0]] = entry[1]
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to load .env file: %s", e)


def _find_env_file(path: str) -> Path | None:
    """The path itself, else the first match under the project root, cwd or cwd's parent."""
    env_path = Path(path)
    if env_path.exists():
        return env_path
    project_root = Path(__file__).resolve().parents[3]
    candidate_paths = [
        project_root / path,
        Path.cwd() / path,
        Path.cwd().parent / path,
    ]
    return next((candidate for candidate in candidate_paths if candidate.exists()), None)


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """(key, value) of a KEY=value line, or None for blanks, comments and invalid lines."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None

    if line.startswith("export "):
        line = line[len("export ") :].strip()

    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    # Remove matching surrounding quotes if present
    if value[:1] in {'"', "'"} and value.endswith(value[0]):
        value = value[1:-1]
    return key, value
