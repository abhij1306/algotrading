import logging
import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """
    Manually load .env file into os.environ.
    Replaces python-dotenv dependency to fix PyInstaller build issues.
    """
    env_path = Path(path)
    if not env_path.exists():
        project_root = Path(__file__).resolve().parents[3]
        candidate_paths = [
            project_root / path,
            Path.cwd() / path,
            Path.cwd().parent / path,
        ]
        env_path = next((candidate for candidate in candidate_paths if candidate.exists()), None)
        if env_path is None:
            return

    try:
        with env_path.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "=" in line:
                    if line.startswith("export "):
                        line = line[len("export ") :].strip()

                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if not key:
                        continue

                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]

                    if key not in os.environ:
                        os.environ[key] = value
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to load .env file: %s", e)
