"""Compatibility wrapper for the moved evaluation CLI.

The executable entry point now lives in ``src/run/evaluate_config.py``.
Keep this module so older commands and docs that call
``src/evaluation/evaluate_config.py`` continue to work.
"""

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from run.evaluate_config import (  # noqa: E402,F401
    load_config_exp_name,
    main,
    print_evaluation_summary,
    print_futures_quality_summary,
    resolve_config_path,
)


if __name__ == "__main__":
    main()
