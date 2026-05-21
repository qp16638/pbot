"""Đọc toàn bộ cấu hình từ .env và trả về một Config dataclass."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Auth
    private_key: str
    funder: str
    api_key: str
    api_secret: str
    api_passphrase: str

    # Strategy
    target_price: float       # Ngưỡng ask tối thiểu để xét hợp lệ (mặc định 0.90)
    order_price: float        # Giá đặt lệnh limit thực tế (luôn 0.99)
    order_size: int           # Số shares mỗi lệnh
    snipe_seconds: int        # Đặt lệnh khi còn <= X giây cuối round
    snipe_min_diff: float     # Ngưỡng cách biệt BTC open vs current (Binance perp)

    # Safety
    dry_run: bool
    profit_limit: float       # Dừng bot khi session P&L >= giá trị này (0 = không giới hạn)


def load() -> Config:
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
        snipe_min_diff   = float(os.getenv("SNIPE_MIN_DIFF", "7.0")),
        dry_run          = os.getenv("DRY_RUN", "true").lower() == "true",
        profit_limit     = float(os.getenv("PROFIT_LIMIT", "0")),
    )
