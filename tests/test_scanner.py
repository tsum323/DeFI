"""UNISWAP AI LAB market scanner: aggregation, range math, incremental ingestion (SYNTHETIC)."""

from __future__ import annotations

import math
from types import SimpleNamespace

from backend.backtesting import uniswap_math as um
from backend.data_engine.abi import encode_word
from backend.database import schema as S
from backend.lab.scanner import (
    SWAP_TOPIC,
    ScanPool,
    aggregate_logs,
    base_price,
    exit_probability,
    il_at,
    ingest,
    position,
    range_fees,
)

WBTC_USDC = ScanPool("wbtc_usdc_3000", "0x99ac8cA7087fA4A2A1FB6357269965A2014ABc35", "ethereum", "WBTC", "USDC", 8, 6, 3000, "WBTC")
USDC_WETH = ScanPool("weth_usdc_500", "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640", "ethereum", "USDC", "WETH", 6, 18, 500, "WETH")


def sqrt_for(price_1per0_human: float, p: ScanPool) -> int:
    return int(math.sqrt(price_1per0_human * 10 ** (p.dec1 - p.dec0)) * 2 ** 96)


def swap_log(p, block, ts, a0, a1, sqrt_p, liq, tick, idx=0):
    data = "0x" + b"".join(encode_word(t, v) for t, v in
                           (("int256", a0), ("int256", a1), ("uint160", sqrt_p), ("uint128", liq), ("int24", tick))).hex()
    return {"address": p.address.lower(), "topics": [SWAP_TOPIC], "data": data, "blockNumber": hex(block),
            "logIndex": hex(idx), "blockTimestamp": hex(ts)}


def test_aggregate_hourly_volume_fees_and_close_state():
    sp = sqrt_for(80_000, WBTC_USDC)
    logs = [swap_log(WBTC_USDC, 10, 7200 + 5, 10 ** 8, -80_000 * 10 ** 6, sp, 123, 5),        # 1 WBTC in
            swap_log(WBTC_USDC, 11, 7200 + 600, -10 ** 7, 8_000 * 10 ** 6, sp + 1, 456, 6, 1),  # 8000 USDC in
            swap_log(WBTC_USDC, 12, 3 * 3600, 10 ** 6, -800 * 10 ** 6, sp, 789, 7)]
    agg = aggregate_logs(logs, {WBTC_USDC.address.lower(): WBTC_USDC})
    h = agg[("wbtc_usdc_3000", 7200)]
    assert h["swaps"] == 2 and math.isclose(h["vol0"], 1.1) and math.isclose(h["vol1"], 88_000)
    assert math.isclose(h["fee0"], 1 * 0.003) and math.isclose(h["fee1"], 8_000 * 0.003)
    assert h["close_liquidity"] == "456" and h["close_tick"] == 6 and h["last_block"] == 11
    assert agg[("wbtc_usdc_3000", 3 * 3600)]["swaps"] == 1


def test_position_bounds_value_and_matches_exact_v3_amounts():
    for p, px in ((WBTC_USDC, 80_000.0), (USDC_WETH, 1 / 3_500.0)):
        sp = sqrt_for(px, p)
        usd0, usd1 = (80_000.0, 1.0) if p is WBTC_USDC else (1.0, 3_500.0)
        pos = position(p, sp, 0.10, 10_000, usd0, usd1)
        b = base_price(sp, p)
        assert math.isclose(pos["price_lower"], b * 0.9, rel_tol=1e-9) and math.isclose(pos["price_upper"], b * 1.1, rel_tol=1e-9)
        assert math.isclose(pos["amount0"] * usd0 + pos["amount1"] * usd1, 10_000, rel_tol=1e-9)
        # the same liquidity through the integer core (Solidity rounding) gives the same amounts
        sa, sb = int(pos["sa"] * 2 ** 96), int(pos["sb"] * 2 ** 96)
        e0, e1 = um.get_amounts_for_liquidity(sp, sa, sb, int(pos["L"]))
        assert math.isclose(e0 / 10 ** p.dec0, pos["amount0"], rel_tol=1e-6)
        assert math.isclose(e1 / 10 ** p.dec1, pos["amount1"], rel_tol=1e-6)


def test_impermanent_loss_zero_at_start_negative_after_moves():
    sp = sqrt_for(80_000, WBTC_USDC)
    pos = position(WBTC_USDC, sp, 0.10, 10_000, 80_000.0, 1.0)
    assert abs(il_at(pos, WBTC_USDC, 0.0, 80_000.0, 1.0)) < 1e-6
    losses = [il_at(pos, WBTC_USDC, ch, 80_000.0, 1.0) for ch in (-0.2, -0.05, 0.05, 0.2)]
    assert all(x < 0 for x in losses) and losses[0] < losses[1] and losses[3] < losses[2]
    # outside the range the position is all in one token: loss grows no faster than the move
    assert abs(losses[0]) < 10_000 * 0.2


def test_range_fees_share_and_exit_probability():
    sp = sqrt_for(80_000, WBTC_USDC)
    pos = position(WBTC_USDC, sp, 0.05, 10_000, 80_000.0, 1.0)
    inside = SimpleNamespace(close_sqrt_price=str(sp), close_liquidity=str(int(pos["L"])), fee0=0.0, fee1=100.0)
    outside = SimpleNamespace(close_sqrt_price=str(sqrt_for(95_000, WBTC_USDC)), close_liquidity="1", fee0=0.0, fee1=100.0)
    fees, n = range_fees([inside, outside], WBTC_USDC, pos, lambda h: h.fee0 * 80_000 + h.fee1)
    assert n == 1 and math.isclose(fees, 50.0, rel_tol=1e-6)  # half of the active liquidity is ours
    assert exit_probability(0.02, 0.05) > exit_probability(0.10, 0.05) > exit_probability(0.30, 0.05)
    assert exit_probability(0.1, 0.0) == 0.0


class FakeClient:
    def __init__(self, head, logs):
        self.head, self.logs, self.calls = head, logs, []

    def block_number(self):
        return self.head

    def batch(self, calls):
        out = []
        for _, (flt,) in calls:
            lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
            self.calls.append((lo, hi))
            out.append([g for g in self.logs if lo <= int(g["blockNumber"], 16) <= hi])
        return out


def test_ingest_is_incremental(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    sp = sqrt_for(80_000, WBTC_USDC)
    logs = [swap_log(WBTC_USDC, b, 1_700_000_000 + 3600 * (b // 100), 10 ** 6, -800 * 10 ** 6, sp, 10, 1) for b in range(1000, 1300, 7)]
    c = FakeClient(1206, logs)
    r1 = ingest(engine, c, [WBTC_USDC], start_block=1000, step=50, confirmations=6)
    assert r1["complete"] and r1["to_block"] == 1200
    c.head, c.logs, c.calls = 1406, logs, []
    r2 = ingest(engine, c, [WBTC_USDC], step=50, confirmations=6)
    assert r2["from_block"] == 1201 and min(lo for lo, _ in c.calls) == 1201  # nothing re-fetched
    with engine.connect() as conn:
        n = sum(r.swaps for r in conn.execute(S.pool_hours.select()))
    assert n == len(logs)  # every swap counted exactly once across the two runs


def test_base_token_and_markets_across_networks():
    from backend.core.config import load_config
    from backend.lab.scanner import base_of, scan_pools
    assert base_of("USDC", "WETH") == "WETH" and base_of("WBTC", "USDC") == "WBTC"
    assert base_of("WETH", "cbBTC") == "cbBTC" and base_of("WBTC", "WETH") == "WBTC"
    cfg = load_config()
    pools = {p.key: p for p in scan_pools(cfg, cfg.lab["scanner"]["pools"])}
    assert pools["base_cbbtc_usdc_500"].asset_pair == pools["wbtc_usdc_3000"].asset_pair == "BTC/USD"
    assert pools["arb_weth_usdc_500"].network == "arbitrum" and pools["op_weth_usdc_3000"].base == "WETH"


class GasClient:
    def __init__(self, base_fee, l1=None, arb=None):
        self.base_fee, self.l1, self.arb = base_fee, l1, arb

    def call(self, method, params):
        return {"baseFeePerGas": hex(self.base_fee), "number": "0x10"}

    def eth_call(self, to, data):
        word = lambda x: x.to_bytes(32, "big").hex()
        if self.l1 is not None:
            return "0x" + word(self.l1)
        return "0x" + "".join(word(x) for x in self.arb)


def test_gas_cost_per_network_includes_l1_data_fee():
    from backend.core.config import load_config
    from backend.lab.scanner import gas_cost_usd
    cfg = load_config()
    units = cfg.lab["costs"]["gas_units"]
    u = units["mint_new_position"] + units["decrease_liquidity_and_collect"]
    eth = gas_cost_usd(GasClient(10 ** 9), cfg, "ethereum", 3000.0)
    prio = cfg.lab["costs"]["priority_fee_gwei"] * 1e9
    assert math.isclose(eth["usd"], u * (1e9 + prio) / 1e18 * 3000, rel_tol=1e-9)
    op = gas_cost_usd(GasClient(10 ** 6, l1=10 ** 12), cfg, "base", 3000.0)      # 0.001 gwei + L1 fee
    assert math.isclose(op["usd"], (u * (1e6 + 1e6) + 2e12) / 1e18 * 3000, rel_tol=1e-9)
    arb = gas_cost_usd(GasClient(0, arb=[10 ** 9, 10 ** 6, 0, 0, 0, 10 ** 7]), cfg, "arbitrum", 3000.0)
    assert math.isclose(arb["usd"], (2 * 1e9 + 2 * 700 * 1e6 + u * 1e7) / 1e18 * 3000, rel_tol=1e-9)
    assert op["usd"] < eth["usd"] and arb["usd"] < eth["usd"]
