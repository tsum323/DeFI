"""Current market snapshot of the pool from the latest synced data (SPEC 11.1, steps 1-6).

Everything is computed from events at or before the snapshot block (no look-ahead):
price/tick/liquidity from the exact replayed state, TVL from the token amounts of
every real position, volume as the USDC side of every Swap, realized volatility from
hourly samples, trend and volatility regime, and the age of the data.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass

from backend.backtesting import uniswap_math as um
from backend.backtesting.market_view import MarketView
from backend.backtesting.pool_replay import PoolState
from backend.backtesting.position_sim import Dataset, _prefill_market

DAY = 86_400


@dataclass(frozen=True)
class MarketSnapshot:
    block: int
    timestamp: int
    data_age_seconds: int
    sqrt_price: int
    tick: int
    price: float                    # token1 per token0 in human units (USDC per WBTC)
    active_liquidity: int
    tvl_token0: int                 # tokens held by all real positions (raw units)
    tvl_token1: int
    tvl_usd: float
    volume_24h_usd: float           # USDC side of swaps
    volume_7d_usd: float
    fees_7d_usd: float              # volume x fee tier (gross, before protocol share)
    fee_apr_7d: float               # fees_7d / TVL annualized
    volume_30d_usd: float
    fee_apr_30d: float
    median_daily_volume_30d_usd: float  # robust to single-day spikes
    vol_1d: float | None
    vol_7d: float | None
    vol_30d: float | None
    change_7d: float | None
    change_30d: float | None
    trend: str                      # up | down | flat (30-day change, +-10%)
    vol_class: str                  # high | low vs the median segment volatility
    base_fee_wei: int | None
    eth_usd: float | None

    def to_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, int) and abs(v) > 2 ** 53 else v)
                for k, v in asdict(self).items()}


def human_price(sqrt_price: int, d0: int, d1: int) -> float:
    return (sqrt_price / 2 ** 96) ** 2 * 10 ** (d0 - d1)


SLOT_SECONDS = 12  # Ethereum post-merge block time (missed slots only make blocks rarer)


def snapshot(ds: Dataset, state: PoolState, now_ts: int, vol_median: float,
             synced_block: int | None = None) -> MarketSnapshot:
    """`synced_block`: the sync covers the pool up to this block. The state is the one after
    the last pool event, still valid when no event followed; data age is then measured from
    the synced block (its time estimated as 12 s per block, a lower bound on the time)."""
    block = state.block
    ts = ds.blocks[block][0]
    covered_ts = ts + SLOT_SECONDS * max(0, (synced_block or block) - block)
    market = MarketView()
    _prefill_market(market, ds, block, lookback_seconds=35 * DAY)
    sp = state.sqrt_price
    tvl0 = tvl1 = 0
    for (owner, lo, hi), pos in state.positions.items():
        if pos.liquidity > 0:
            a0, a1 = um.get_amounts_for_liquidity(sp, um.get_sqrt_ratio_at_tick(lo),
                                                  um.get_sqrt_ratio_at_tick(hi), pos.liquidity)
            tvl0 += a0
            tvl1 += a1
    price = human_price(sp, ds.decimals0, ds.decimals1)
    tvl_usd = tvl0 / 10 ** ds.decimals0 * price + tvl1 / 10 ** ds.decimals1
    blocks = [e.block for e in ds.events]
    hi_i = bisect.bisect_right(blocks, block)
    v24 = v7 = v30 = 0
    daily = [0] * 30
    for e in reversed(ds.events[:hi_i]):
        ets = ds.blocks[e.block][0]
        if ets < ts - 30 * DAY:
            break
        if e.name == "Swap":
            usdc = abs(e.f["amount1"])
            v30 += usdc
            daily[min(29, int((ts - ets) // DAY))] += usdc
            if ets >= ts - 7 * DAY:
                v7 += usdc
            if ets >= ts - DAY:
                v24 += usdc
    v30_usd = v30 / 10 ** ds.decimals1
    med_daily = sorted(daily)[15] / 10 ** ds.decimals1
    v24_usd, v7_usd = v24 / 10 ** ds.decimals1, v7 / 10 ** ds.decimals1
    fees7 = v7_usd * ds.fee / 1_000_000
    ch30 = market.price_change(ts, 30 * DAY)
    trend = "flat" if ch30 is None else "up" if ch30 > 0.10 else "down" if ch30 < -0.10 else "flat"
    vol7 = market.vol_annual(ts, 7 * DAY)
    eth = [r for r in ds.eth_usd if r[0] <= block]
    return MarketSnapshot(
        block=block, timestamp=ts, data_age_seconds=max(0, now_ts - max(ts, min(covered_ts, now_ts))), sqrt_price=sp,
        tick=state.tick, price=price, active_liquidity=state.liquidity, tvl_token0=tvl0,
        tvl_token1=tvl1, tvl_usd=tvl_usd, volume_24h_usd=v24_usd, volume_7d_usd=v7_usd,
        fees_7d_usd=fees7, fee_apr_7d=fees7 / tvl_usd * 365 / 7 if tvl_usd else 0.0,
        volume_30d_usd=v30_usd,
        fee_apr_30d=v30_usd * ds.fee / 1e6 / tvl_usd * 365 / 30 if tvl_usd else 0.0,
        median_daily_volume_30d_usd=med_daily,
        vol_1d=market.vol_annual(ts, DAY, min_returns=12), vol_7d=vol7,
        vol_30d=market.vol_annual(ts, 30 * DAY), change_7d=market.price_change(ts, 7 * DAY),
        change_30d=ch30, trend=trend,
        vol_class="high" if (vol7 or 0) >= vol_median else "low",
        base_fee_wei=ds.blocks[block][1], eth_usd=eth[-1][2] / 1e8 if eth else None)


def latest_event_block(ds: Dataset) -> int:
    return ds.events[-1].block


def finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))
