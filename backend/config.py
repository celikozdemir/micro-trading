from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    db_user: str = "algo"
    db_password: str = "algo_secret"
    db_name: str = "algo_trading"
    db_host: str = "localhost"
    db_port: int = 5435

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""

    # Alpaca
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # Anthropic
    anthropic_api_key: str = ""

    # App
    env: str = "development"
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


def load_trading_config(path: str | Path = "configs/default.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


settings = Settings()
