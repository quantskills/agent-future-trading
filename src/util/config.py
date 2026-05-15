import yaml
from datetime import datetime
from pathlib import Path
from util.logger import logger
from typing import Dict, Any

class ConfigParser:
    """Manages configuration loading and validation."""

    def __init__(self, args):
        """Initialize the configuration manager."""
        self.config_path = self._resolve_config_path(args.config)
        self.trading_date = args.trading_date
        self.config = self.load_config()

    @staticmethod
    def _resolve_config_path(config_path: str) -> str:
        path = Path(config_path)
        if path.is_absolute() or path.exists():
            return str(path)

        src_root = Path(__file__).resolve().parents[1]
        project_root = src_root.parent
        for candidate in (src_root / path, project_root / path):
            if candidate.exists():
                return str(candidate)

        return str(path)
        
    def load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        cfg = {}
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                logger.info(f"Loading configuration from {self.config_path}")
                cfg = yaml.safe_load(f)
        except FileNotFoundError:
            raise ValueError(f"Configuration file not found: {self.config_path}")
        except yaml.YAMLError as e:
            raise ValueError(f"Error parsing configuration file: {e}")

        cfg['trading_date'] = datetime.strptime(self.trading_date, '%Y-%m-%d')
        cfg['planner_mode'] = cfg.get('planner_mode', False)
        if 'trade_auditor' not in cfg and 'decision_planner' in cfg:
            cfg['trade_auditor'] = cfg.get('decision_planner')
        if 'decision_planner' not in cfg and 'trade_auditor' in cfg:
            cfg['decision_planner'] = cfg.get('trade_auditor')
        cfg['workflow_analysts'] = [
            'commodity_news' if analyst == 'company_news' else analyst
            for analyst in cfg.get('workflow_analysts', [])
        ]

        return cfg

    def get_config(self) -> Dict[str, Any]:
        """Get the configuration."""
        return self.config
