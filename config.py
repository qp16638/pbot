"""Đọc toàn bộ cấu hình từ .env và trả về một Config dataclass."""

import os
from dataclasses import dataclass, replace

from dotenv import load_dotenv

load_dotenv()


@dataclass
class MarketSpec:
    name: str          # "BTC-5M"
    slug_prefix: str   # "btc-updown-5m"
    symbol: str        # "BTCUSDT"
    interval: str      # "5m"
    interval_secs: int # 300
    min_diff: float    # 7.0


@dataclass
class Config:
    # Auth
    private_key: str
    funder: str
    api_key: str
    api_secret: str
    api_passphrase: str

    # Strategy
    target_price: float
    order_price: float
    order_size: int
    snipe_seconds: int
    snipe_min_diff: float  # fallback nếu market không có riêng

    # Safety
    dry_run: bool
    profit_limit: float

    # Markets
    markets: list


_ALL_MARKETS = {
    "BTC_5M":  MarketSpec("BTC-5M",  "btc-updown-5m",  "BTCUSDT", "5m",  300, 0.0),
    "BTC_15M": MarketSpec("BTC-15M", "btc-updown-15m", "BTCUSDT", "15m", 900, 0.0),
    "ETH_5M":  MarketSpec("ETH-5M",  "eth-updown-5m",  "ETHUSDT", "5m",  300, 0.0),
    "ETH_15M": MarketSpec("ETH-15M", "eth-updown-15m", "ETHUSDT", "15m", 900, 0.0),
}


def load() -> Config:
    snipe_min_diff = float(os.getenv("SNIPE_MIN_DIFF", "7.0"))

    enabled_keys = [k.strip().upper() for k in os.getenv("MARKETS", "BTC_5M").split(",")]
    markets = []
    for key in enabled_keys:
        spec = _ALL_MARKETS.get(key)
        if spec is None:
            continue
        per_market_diff = float(os.getenv(f"{key}_MIN_DIFF", str(snipe_min_diff)))
        markets.append(replace(spec, min_diff=per_market_diff))

    return Config(
        private_key      = os.getenv("PRIVATE_KEY", ""),
        funder           = os.getenv("POLYMARKET_FUNDER", ""),
        api_key          = os.getenv("POLYMARKET_API_KEY", ""),
        api_secret       = os.getenv("POLYMARKET_API_SECRET", ""),
        api_passphrase   = os.getenv("POLYMARKET_API_PASSPHRASE", ""),
        target_price     = float(os.getenv("TARGET_PRICE", "0.90")),
        order_price      = float(os.getenv("ORDER_PRICE", "0.99")),
        order_size       = int(os.getenv("ORDER_SIZE", "5")),
        snipe_seconds    = int(os.getenv("SNIPE_SECONDS", "6")),
        snipe_min_diff   = snipe_min_diff,
        dry_run          = os.getenv("DRY_RUN", "true").lower() == "true",
        profit_limit     = float(os.getenv("PROFIT_LIMIT", "0")),
        markets          = markets,
    )
