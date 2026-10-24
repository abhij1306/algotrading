"""Shared setup for the HAT verification suites.

This conftest is HAT's own. The HAT suites boot no FastAPI app and import no legacy test
helper: their doubles are in `tests/hat/fakes/` (CONF `imports.hat_decoupled`).

Hypothesis profile `ci` is the one the CI `verification` job and every CI run use: derandomized,
500 examples, no deadline, reproduction blobs printed. Without `CI` set, a lighter `hat-dev`
profile runs so a local edit loop stays fast.
"""

import os
import sys
from pathlib import Path

from hypothesis import settings

# `tests/` on the path makes `tests/hat` the namespace package `hat` (`hat.fakes.*`), whichever
# directory pytest was started from. It never collides with the legacy `fakes` package.
_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

settings.register_profile("ci", derandomize=True, max_examples=500, deadline=None, print_blob=True)
settings.register_profile("hat-dev", max_examples=100, deadline=None, print_blob=True)
settings.load_profile("ci" if os.environ.get("CI") else "hat-dev")
