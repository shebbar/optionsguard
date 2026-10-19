"""Loads config.toml (thresholds, holidays) and .env (secrets)."""
from __future__ import annotations

import tomllib
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.toml"


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://youruser:yourpassword@127.0.0.1:5432/optionguard"
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_redirect_url: str = "http://127.0.0.1:8787/kite/callback"
    optionguard_secret_key: str = "dev-only-not-secret-change-me"


class ServerConfig(BaseModel):
    port: int = 8787


class PricingConfig(BaseModel):
    risk_free_rate: float = 0.065
    trading_days_per_year: int = 252


class DefaultsConfig(BaseModel):
    max_loss_per_trade: float = 5000
    monthly_loss_limit: float = 10000
    premium_stop_pct: float = 0.40
    time_stop_days: int = 7
    cooloff_days: int = 10
    charges_per_order_estimate: float = 60


class VerdictConfig(BaseModel):
    low_prob_red: float = 0.25
    low_prob_amber: float = 0.35
    be_to_move_red: float = 0.8
    chasing_5d_pct: float = 0.08
    chasing_20dma_pct: float = 0.07
    iv_hv_amber: float = 1.3
    hv_pct_amber: float = 80
    spread_red: float = 0.10
    spread_amber: float = 0.05
    min_volume: int = 50
    min_oi: int = 200
    near_expiry_days: int = 8


class HolidaysConfig(BaseModel):
    dates: list[date] = []


class MarketDataConfig(BaseModel):
    default_source: str = "mock"


class Config(BaseModel):
    server: ServerConfig = ServerConfig()
    pricing: PricingConfig = PricingConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    verdict: VerdictConfig = VerdictConfig()
    holidays: HolidaysConfig = HolidaysConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    secrets: Secrets = Secrets()


@lru_cache
def get_config() -> Config:
    raw: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            raw = tomllib.load(f)
    return Config(**raw, secrets=Secrets())
