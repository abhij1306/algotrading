"""Shared setup for the HAT verification suites (HAT-VER-040).

Hypothesis profile `ci` is the one the CI `verification` job and every CI run use: derandomized,
500 examples, no deadline, reproduction blobs printed. Without `CI` set, a lighter `hat-dev`
profile runs so a local edit loop stays fast.
"""

import os

from hypothesis import settings

settings.register_profile("ci", derandomize=True, max_examples=500, deadline=None, print_blob=True)
settings.register_profile("hat-dev", max_examples=100, deadline=None, print_blob=True)
settings.load_profile("ci" if os.environ.get("CI") else "hat-dev")
