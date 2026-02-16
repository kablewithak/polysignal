from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class WalletFeatures:
    # global trader “skill” proxies
    pnl_all: float = 0.0
    win_rate: Optional[float] = None
    wr_n: int = 0
    days_since_active: Optional[float] = None

    # position/conviction proxies
    conviction_ratio: Optional[float] = None
    market_outcome: Optional[str] = None

    # allow both names so analysis versions don’t break
    market_value: float = 0.0
    position_size: float = 0.0


def to_days_since(ts: Optional[int]) -> Optional[float]:
    """
    Convert a unix timestamp to "days since".
    Accepts seconds or milliseconds.
    Returns None if ts is missing/invalid.
    """
    if ts is None:
        return None
    try:
        t = int(ts)
    except Exception:
        return None

    # ms -> s
    if t > 10_000_000_000:  # > year ~2286 in seconds, so treat as ms
        t = t // 1000

    now = int(datetime.now(timezone.utc).timestamp())
    if t <= 0 or t > now + 60:  # guard weird future timestamps
        return None

    return max(0.0, (now - t) / 86400.0)


def _safe(v: Optional[float], default: float) -> float:
    return default if v is None else float(v)


def wallet_weight(f: WalletFeatures) -> float:
    """
    Stable, monotonic weighting. Keep it simple and predictable.
    """
    pnl = max(0.0, float(f.pnl_all))
    profit_w = min(5.0, max(1.0, pnl / 5000.0))  # 1x..5x

    win_w = 0.5 + _safe(f.win_rate, 0.5)  # None -> 1.0, else 0.5..1.5-ish

    if f.days_since_active is None:
        rec_w = 0.75
    else:
        rec_w = 0.5 + 0.5 * math.exp(-float(f.days_since_active) / 30.0)

    conv_w = min(3.0, max(0.5, _safe(f.conviction_ratio, 1.0)))

    mv = float(f.market_value) if f.market_value > 0 else float(f.position_size)
    value_w = math.sqrt(max(1.0, mv))

    w = profit_w * win_w * rec_w * conv_w * value_w
    return float(max(0.0, w))
