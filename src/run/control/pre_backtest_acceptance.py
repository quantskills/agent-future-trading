from __future__ import annotations

"""Run the same PG pre-backtest gate exposed by the fixed main runner."""

import sys
from pathlib import Path


RUN_CONTROL_DIR = Path(__file__).resolve().parent
RUN_DIR = RUN_CONTROL_DIR.parent
SRC_ROOT = RUN_DIR.parent
PROJECT_ROOT = SRC_ROOT.parent
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from pre_backtest_test import main


if __name__ == "__main__":
    raise SystemExit(main())
