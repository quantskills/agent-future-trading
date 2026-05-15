"""CLI runner for the futures accountant agent."""

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agents.accountant import accountant_agent, main


if __name__ == "__main__":
    main()
