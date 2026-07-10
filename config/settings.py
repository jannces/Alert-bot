"""Configuration loading for the Tamad scanner.

Configuration lives in ``config/config.yaml``. Any string value may contain
``${ENV_VAR}`` placeholders, which are substituted from the process
environment at load time — secrets (Telegram token) should always come from
the environment, never be committed to the YAML file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: object) -> object:
    """Recursively substitute ``${VAR}`` placeholders in strings."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class AppSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class ExchangeSettings(BaseModel):
    name: str = "MEXC"
    tv_prefix: str = "MEXC"
    market: str = "USDT Perpetual"


class SymbolsSettings(BaseModel):
    mode: Literal["all", "whitelist"] = "all"
    whitelist: list[str] = Field(default_factory=list)

    def allows(self, symbol: str) -> bool:
        if self.mode == "all":
            return True
        return symbol in self.whitelist


class ScannerSettings(BaseModel):
    timeframes: list[str] = Field(default_factory=lambda: ["15", "30", "60"])
    symbols: SymbolsSettings = Field(default_factory=SymbolsSettings)
    history_bars: int = Field(default=150, ge=50, le=2000)
    boundary_settle_seconds: float = 5.0
    max_signal_age_seconds: int = 600
    clock_skew_seconds: int = 30
    symbol_refresh_seconds: int = 3600

    @field_validator("timeframes")
    @classmethod
    def _timeframes_numeric(cls, v: list[str]) -> list[str]:
        for tf in v:
            if not tf.isdigit() or int(tf) <= 0:
                raise ValueError(f"timeframe must be minutes as a string, got {tf!r}")
        return v

    @property
    def timeframe_minutes(self) -> list[int]:
        return sorted(int(tf) for tf in self.timeframes)


class MexcSettings(BaseModel):
    base_url: str = "https://contract.mexc.com"
    requests_per_second: float = Field(default=8.0, gt=0)
    max_concurrency: int = Field(default=8, ge=1)
    retries: int = Field(default=3, ge=0)
    timeout_seconds: float = 15.0


class EqualCloseSettings(BaseModel):
    tolerance_percent: float = Field(default=0.05, ge=0)
    # "average" is intentionally not offered: with two candles it is
    # mathematically identical to "midpoint" (see ARCHITECTURE.md, D2).
    comparison_mode: Literal["strict", "midpoint"] = "strict"


class SupportResistanceSettings(BaseModel):
    method: Literal["swing_high_low"] = "swing_high_low"
    left_bars: int = Field(default=20, ge=1)
    right_bars: int = Field(default=20, ge=1)
    proximity_percent: float = Field(default=0.25, gt=0)


class StrategySettings(BaseModel):
    equal_close: EqualCloseSettings = Field(default_factory=EqualCloseSettings)
    support_resistance: SupportResistanceSettings = Field(
        default_factory=SupportResistanceSettings
    )
    risk_reward_targets: list[float] = Field(default_factory=lambda: [2.0, 3.0])


class TelegramSettings(BaseModel):
    bot_token: str = ""
    chat_id: str = ""
    retries: int = 3
    timeout_seconds: float = 30.0


class ViewportSettings(BaseModel):
    width: int = 1600
    height: int = 900


class PlaywrightSettings(BaseModel):
    chart_url: str = "https://www.tradingview.com/chart/"
    storage_state_path: str = "config/tv_storage_state.json"
    executable_path: str = ""
    viewport: ViewportSettings = Field(default_factory=ViewportSettings)
    render_wait_seconds: float = 6.0
    timeout_seconds: float = 60.0
    retries: int = 2


class ScreenshotSettings(BaseModel):
    provider: Literal["playwright", "disabled"] = "playwright"
    annotations: Literal["python_overlay", "legend_only", "none"] = "python_overlay"
    on_failure: Literal["send_without_image", "skip_alert"] = "send_without_image"
    output_dir: str = "screenshots/out"
    playwright: PlaywrightSettings = Field(default_factory=PlaywrightSettings)


class DatabaseSettings(BaseModel):
    path: str = "database/tamad.sqlite3"


class LoggingSettings(BaseModel):
    level: str = "INFO"
    file: str = "logs/tamad.log"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    mexc: MexcSettings = Field(default_factory=MexcSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    screenshots: ScreenshotSettings = Field(default_factory=ScreenshotSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def load_settings(path: str | Path = "config/config.yaml") -> Settings:
    """Load, env-expand, and validate the YAML configuration file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text()) or {}
    return Settings.model_validate(_expand_env(raw))
