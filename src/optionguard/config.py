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


class OrdersConfig(BaseModel):
    """Section 7.8: order ticket. `enabled` is the master switch -- the
    ticket renders preview-only (no real placement) while it's false, which
    is the right default until a whitelisted static IP exists (Kite rejects
    order endpoints -- place/modify/cancel/GTT -- from unwhitelisted IPs)."""

    enabled: bool = False
    allow_market: bool = False
    market_protection: int = 3
    gtt_limit_buffer_pct: float = 0.10
    fill_poll_seconds: int = 2
    fill_poll_timeout_s: int = 120
    entry_cutoff: str = "15:15"
    egress_proxy: str = ""


class OptionThesesPoliciesConfig(BaseModel):
    grace_days: int = 3
    disaster_stop_pct: float = 0.60
    time_checkpoints: list[int] = [5, 8, 11]
    time_stop_min_gain_pct: float = 0.0


class OptionThesesConfig(BaseModel):
    """Section 7.9: theses on the option contract itself (entry/target/stop
    premium, scored from the option's own price)."""

    market_benchmark: str = "NIFTY 50"
    min_duration_days: int = 1
    max_duration_days: int = 20
    hold_cutoff_calendar_days: int = 7
    expiry_warning_days: int = 3
    paper_slippage_pct: float = 0.01
    late_exit_flag_days: int = 1
    mc_paths: int = 20000
    mc_steps_per_day: int = 25
    mc_seed: int = 7
    findings_min_n: int = 10
    policies: OptionThesesPoliciesConfig = OptionThesesPoliciesConfig()


class Config(BaseModel):
    server: ServerConfig = ServerConfig()
    pricing: PricingConfig = PricingConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    verdict: VerdictConfig = VerdictConfig()
    holidays: HolidaysConfig = HolidaysConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    # sectors: sector name -> NSE index tradingsymbol, used by option theses (7.9) to
    # resolve a contract's sector. symbol_sectors: optional per-symbol sector defaults.
    sectors: dict[str, str] = {}
    symbol_sectors: dict[str, str] = {}
    orders: OrdersConfig = OrdersConfig()
    option_theses: OptionThesesConfig = OptionThesesConfig()
    secrets: Secrets = Secrets()


@lru_cache
def get_config() -> Config:
    raw: dict = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            raw = tomllib.load(f)
    return Config(**raw, secrets=Secrets())
