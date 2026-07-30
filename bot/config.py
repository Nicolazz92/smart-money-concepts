"""Configuration module.

- EnvSettings: secrets loaded from .env (Telegram, MT5, OANDA)
- StrategyConfig: strategy parameters configurable from the UI, persisted in JSON
"""

import json
import logging
from pathlib import Path
from typing import List

from pydantic import BaseModel
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent / "data"
STRATEGY_CONFIG_PATH = CONFIG_DIR / "strategy_config.json"


class EnvSettings(BaseSettings):
    """Secrets and infrastructure — loaded from .env, never exposed in UI."""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # MT5
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = "ICMarketsSC-Demo"

    # OANDA
    oanda_api_token: str = ""
    oanda_account_id: str = ""
    oanda_environment: str = "practice"

    # General
    data_provider: str = "mt5"
    timezone: str = "Europe/Helsinki"

    # MOEX
    moex_market: str = "forts"  # "forts" or "shares"

    # Paper trading
    paper_initial_capital: float = 100000.0  # RUB
    paper_position_pct_long: float = 100.0
    paper_position_pct_short: float = 50.0
    paper_max_concurrent: int = 3
    paper_cost_pct: float = 0.26  # Finam Strateg round-trip %
    paper_enabled: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


class StrategyConfig(BaseModel):
    """Strategy parameters — configurable from the dashboard UI."""

    pairs: List[str] = ["EURUSD", "GBPUSD", "XAUUSD"]
    bias_timeframe: str = "4H"
    ob_timeframe: str = "30M"
    bias_swing_length: int = 10
    ob_swing_length: int = 10
    scan_interval: int = 300  # seconds (5 minutes)
    sl_buffer_pips: float = 5.0
    killzones: List[str] = [
        "London open kill zone",
        "New York kill zone",
        "London close kill zone",
    ]
    min_rr: float = 1.5  # minimum R:R for signal (from walk-forward optimisation)

    @classmethod
    def load(cls) -> "StrategyConfig":
        if STRATEGY_CONFIG_PATH.exists():
            try:
                return cls.model_validate_json(STRATEGY_CONFIG_PATH.read_text())
            except Exception as e:
                logger.warning("Failed to load strategy config, using defaults: %s", e)
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STRATEGY_CONFIG_PATH.write_text(self.model_dump_json(indent=2))


class BacktestConfig(BaseModel):
    """Parameters specific to a backtest run."""

    start_date: str = ""  # "YYYY-MM-DD"
    end_date: str = ""  # "YYYY-MM-DD"
