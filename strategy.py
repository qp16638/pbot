"""
Logic kiểm tra điều kiện vào lệnh snipe.

4 điều kiện PHẢI thỏa mãn đồng thời:
  C1: 0 < seconds_remaining <= snipe_seconds
  C2: |btc_current - btc_open| >= snipe_min_diff
  C3: direction đồng thuận (diff>0 → trade UP, diff<0 → trade DOWN)
  C4: best ask của side == target_price
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from config import Config
from polymarket import OrderbookState


@dataclass
class CheckResult:
    should_enter: bool

    best_ask: Optional[Decimal]
    shares_at_price: Decimal
    seconds_remaining: float

    cond1_price_ok:  bool   # C4: ask == target
    cond2_supply_ok: bool   # C2: diff >= min_diff
    cond3_time_ok:   bool   # C1: timing ok
    cond4_bid_ok:    bool   # C3: direction match

    bid_at_price: Decimal
    reason: str


def check_snipe(
    side: str,
    ob: Optional[OrderbookState],
    market_end: Optional[datetime],
    btc_current: Optional[float],
    btc_open: Optional[float],
    cfg: Config,
) -> CheckResult:
    target = Decimal(str(cfg.target_price))

    if market_end:
        now = datetime.now(timezone.utc)
        secs_left = (market_end - now).total_seconds()
    else:
        secs_left = -1.0

    best_ask  = ob.best_ask()           if (ob and ob.has_data) else None
    shares_at = ob.shares_at_ask_price(target) if (ob and ob.has_data) else Decimal("0")
    bid_at    = ob.shares_at_bid_price(target) if (ob and ob.has_data) else Decimal("0")

    # C1: timing
    c1 = 0 < secs_left <= cfg.snipe_seconds

    # C2 & C3: price diff + direction
    if btc_current is not None and btc_open is not None:
        diff = btc_current - btc_open
        c2 = abs(diff) >= cfg.snipe_min_diff
        c3 = (diff > 0 and side.upper() == "UP") or (diff < 0 and side.upper() == "DOWN")
    else:
        diff = 0.0
        c2 = False
        c3 = False

    # C4: ask trống (book empty = coi như giá ~$1) HOẶC ask >= target_price
    c4 = best_ask is None or best_ask >= target

    parts = []
    if not c1:
        parts.append(f"time={secs_left:.1f}s (need 0<t<={cfg.snipe_seconds}s)")
    if not c2:
        parts.append(f"diff={diff:.2f} (need >={cfg.snipe_min_diff})")
    if not c3:
        parts.append(f"direction mismatch (diff={diff:+.2f}, side={side})")
    if not c4:
        parts.append(f"ask={best_ask} (need >={target})")

    should_enter = c1 and c2 and c3 and c4

    return CheckResult(
        should_enter      = should_enter,
        best_ask          = best_ask,
        shares_at_price   = shares_at,
        seconds_remaining = secs_left,
        cond1_price_ok    = c4,
        cond2_supply_ok   = c2,
        cond3_time_ok     = c1,
        cond4_bid_ok      = c3,
        bid_at_price      = bid_at,
        reason            = "ALL CONDITIONS MET" if should_enter else " | ".join(parts),
    )
