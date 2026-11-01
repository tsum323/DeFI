"""Past-only market features built during the replay (no look-ahead).

Hourly samples of the pool tick at block ends; realized volatility over a
lookback from hourly log returns (ln(1.0001) per tick). A query at time t only
sees samples taken at or before t.
"""

from __future__ import annotations

import bisect
import math

LN_TICK = math.log(1.0001)
HOURS_PER_YEAR = 24 * 365


class MarketView:
    def __init__(self, sample_seconds: int = 3600):
        self.sample_seconds = sample_seconds
        self.ts: list[int] = []
        self.ticks: list[int] = []
        self._c1: list[float] = [0.0]  # prefix sums of returns / squared returns
        self._c2: list[float] = [0.0]

    def update(self, ts: int, tick: int) -> None:
        if self.ts and ts < self.ts[-1] + self.sample_seconds:
            return
        if self.ts:
            r = (tick - self.ticks[-1]) * LN_TICK
            self._c1.append(self._c1[-1] + r)
            self._c2.append(self._c2[-1] + r * r)
        self.ts.append(ts)
        self.ticks.append(tick)

    def vol_annual(self, ts: int, lookback_seconds: int, min_returns: int = 24) -> float | None:
        """Annualized realized volatility of hourly returns in (ts - lookback, ts]."""
        hi = bisect.bisect_right(self.ts, ts) - 1          # last sample at or before ts
        lo = bisect.bisect_left(self.ts, ts - lookback_seconds)
        n = hi - lo                                         # returns between samples lo..hi
        if hi < 0 or n < min_returns:
            return None
        s1 = self._c1[hi] - self._c1[lo]
        s2 = self._c2[hi] - self._c2[lo]
        var = max(0.0, (s2 - s1 * s1 / n) / max(1, n - 1))
        return math.sqrt(var * HOURS_PER_YEAR)

    def price_change(self, ts: int, lookback_seconds: int) -> float | None:
        """Relative price change over the lookback (from samples)."""
        hi = bisect.bisect_right(self.ts, ts) - 1
        lo = bisect.bisect_left(self.ts, ts - lookback_seconds)
        if hi < 0 or lo >= hi:
            return None
        return math.exp((self.ticks[hi] - self.ticks[lo]) * LN_TICK) - 1
