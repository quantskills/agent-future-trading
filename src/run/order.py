"""CLI runner for the futures trader agent."""

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.execution_team.trader import (
    _reconcile_rollover_with_strategy_target,
    _translate_pre_open_recommendation_to_order,
    main,
)


if __name__ == "__main__":
    main()
