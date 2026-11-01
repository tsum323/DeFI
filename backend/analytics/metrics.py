"""Risk / return metrics from a daily equity series (SPEC §8). Floats: analytics only."""

from __future__ import annotations

import math
import statistics


def series_metrics(equity: list[float], days: float, net: float, capital: float,
                   max_drawdown: float) -> dict:
    rets = [b / a - 1 for a, b in zip(equity, equity[1:]) if a > 0]
    out = {"sharpe_a": None, "sortino_a": None, "calmar_a": None,
           "avg_daily_net_a": net / days if days > 0 else 0.0, "daily_returns": len(rets)}
    if len(rets) >= 2:
        mu, sd = statistics.fmean(rets), statistics.stdev(rets)
        if sd > 0:
            out["sharpe_a"] = mu / sd * math.sqrt(365)
        downside = [min(0.0, r) for r in rets]
        dd = math.sqrt(sum(d * d for d in downside) / len(downside))
        if dd > 0:
            out["sortino_a"] = mu / dd * math.sqrt(365)
    apr = net / capital * 365 / days if days > 0 and capital else 0.0
    if max_drawdown > 0:
        out["calmar_a"] = apr / max_drawdown
    return out
