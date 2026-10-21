"""app.paths is the one owner of repository paths, and the modules that read data use it."""

from pathlib import Path

from app import paths
from app.hat.execution import fyers_auth, fyers_session
from app.hat.execution.fyers_tokens import token_store
from app.utils import logger

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_project_root_is_the_repository_checkout() -> None:
    assert paths.PROJECT_ROOT == REPO_ROOT
    assert (paths.PROJECT_ROOT / "backend" / "app" / "paths.py").is_file()


def test_standard_locations_sit_at_fixed_places_under_the_root() -> None:
    root = paths.PROJECT_ROOT

    assert paths.ENV_FILE == root / ".env"
    assert paths.LOG_DIR == root / "logs"
    assert paths.FYERS_TOKEN_PATH == root / "fyers" / "config" / "access_token.json"
    assert paths.INDEX_WEIGHTAGE_DIR == root / "data" / "index_weightage"
    assert paths.DATA_ROOT == root / "data_system"
    assert paths.SOURCES_ROOT == root / "data_system" / "01_sources"
    assert paths.UNIVERSE_CONSTITUENTS_DIR == root / "data_system" / "03_universe" / "constituents"
    assert paths.CURATED_ROOT == root / "data_system" / "04_curated" / "phase1"
    assert paths.METADATA_ROOT == root / "data_system" / "05_metadata" / "phase1"


def test_token_and_log_files_resolve_under_the_repository_root() -> None:
    token = REPO_ROOT / "fyers" / "config" / "access_token.json"

    assert token_store.path == token
    assert fyers_session.token_store is token_store
    assert fyers_auth.fyers_auth.store is token_store
    assert logger.LOG_DIR == REPO_ROOT / "logs"
