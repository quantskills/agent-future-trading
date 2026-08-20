"""Default deterministic unittest discovery entry for agent-future-trading.

Running `python -m unittest` from the repository root only discovers test files
in the current directory by default. The real test suite lives under
`src/tests`, so this module bridges the default command to the project tests.

This default suite must stay deterministic: tests may fake PandaAI, Finoview,
news, and LLM providers, but must not require live provider credentials or make
network calls. Real-provider checks belong in explicitly named integration
tests and should be run separately under the deepfund conda environment.
"""

import unittest
from pathlib import Path


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str):
    test_dir = Path(__file__).resolve().parent / "src" / "tests"
    return loader.discover(str(test_dir), pattern or "test*.py", top_level_dir=str(test_dir))
