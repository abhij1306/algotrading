"""Configuration read from the environment."""

import os

from .utils.env_loader import load_dotenv

load_dotenv()


# The frontend origins (the Vite dev server ports) allowed by CORS. Override with CORS_ORIGINS,
# a comma-separated list.
DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001"
)


def parse_origins(raw: str) -> tuple[str, ...]:
    """Split a comma-separated origin list, dropping blanks and stray whitespace."""
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


class Config:
    ALLOWED_ORIGINS = parse_origins(os.getenv("CORS_ORIGINS", DEFAULT_ALLOWED_ORIGINS))


config = Config()
