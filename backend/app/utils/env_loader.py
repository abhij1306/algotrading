import os
from pathlib import Path


def load_dotenv(path: str = ".env"):
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
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()

                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]

                    os.environ[key] = value
    except Exception as e:
        print(f"Warning: Failed to load .env file: {e}")
