"""«Мой бот»: picks what pays off for its own amount, holds, re-centres, learns (SYNTHETIC)."""

from __future__ import annotations

import math
from types import SimpleNamespace

from backend.core.config import load_config
from backend.database import schema as S
from backend.lab import bot
from backend.lab.activity import recent_activity
from backend.lab.scanner import ScanPool, position

ETH = ScanPool("weth_usdc_500", "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640", "ethereum", "USDC", "WETH", 6, 18, 500, "WETH")
BASE = ScanPool("base_weth_usdc_500", "0xd0b53D9277642d899DF5C87A3966A349A798F224", "base", "WETH", "USDC", 18, 6, 500, "WETH")


def sqrt_for(price_base_in_usdc, p):
    x = price_base_in_usdc if p.base == p.token0 else 1 / price_base_in_usdc
    return int(math.sqrt(x * 10 ** (p.dec1 - p.dec0)) * 2 ** 96)


def pool_entry(p, gas, fees_week=(40.0, 60.0)):
    sp = sqrt_for(3000.0, p)
    usd0, usd1 = (1.0, 3000.0) if p.token0 == "USDC" else (3000.0, 1.0)
    pos = position(p, sp, 0.1, 10_000, usd0, usd1)
    return {"pool": p.key, "network": p.network, "network_ru": p.network, "pair": "WETH/USDC", "fee_tier": 0.0005,
            "price": 3000.0, "token0": p.token0, "token1": p.token1, "problems": [], "data_quality": [],
            "ranges": [{"width": 0.1, "price_lower": 2700.0, "price_upper": 3300.0, "amount0": pos["amount0"],
                        "amount1": pos["amount1"], "fees_usd": list(fees_week), "il_usd": -10.0, "gas_usd": gas,
                        "swap_usd": 2.5, "exit_probability": 0.1}]}, sp, pos


def hours(p, sp, pos, n, start, inside=True, fee_usdc=1.0):
    price = sp if inside else sqrt_for(4000.0, p)
    f0, f1 = (fee_usdc, 0.0) if p.token0 == "USDC" else (0.0, fee_usdc)
    return [SimpleNamespace(hour_ts=start + 3600 * i, close_sqrt_price=str(price), close_liquidity=str(int(pos["L"] * 1000)),
                            fee0=f0, fee1=f1, swaps=1) for i in range(n)]


def test_bot_picks_cheap_network_holds_recentres_and_learns(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    cfg = load_config()
    e_pool, _, _ = pool_entry(ETH, gas=2.0)
    b_pool, sp, pos = pool_entry(BASE, gas=0.02)
    scan = {"capital_usd": 10_000.0, "horizon_days": 7, "eth_usd": 3000.0, "btc_usd": 80_000.0, "pools": [e_pool, b_pool]}
    t0 = 3600 * 1000
    r = bot.tick(engine, cfg, scan, {ETH.key: ETH, BASE.key: BASE}, {}, now=t0)
    # with $100 the $2 Ethereum gas eats the income: the bot goes to Base
    assert r["action"] == "open" and r["option"]["network"] == "base"
    assert bot._open(engine).allocation_usd == 100.0
    # one day inside the range: it holds and the value includes fees
    h = hours(BASE, sp, pos, 24, t0)
    assert bot.tick(engine, cfg, scan, {BASE.key: BASE}, {BASE.key: h}, now=t0 + 86_400)["action"] == "hold"
    s = bot.summary(engine, cfg, now=t0 + 86_400)
    assert s["current"]["mark"]["fees_usd"] > 0 and s["current"]["network"] == "base"
    # price leaves the range for more than 24 h: closed, learned, reopened
    out = h + hours(BASE, sp, pos, 30, t0 + 86_400, inside=False)
    bot.tick(engine, cfg, scan, {BASE.key: BASE}, {BASE.key: out}, now=t0 + 86_400 + 3600)
    r = bot.tick(engine, cfg, scan, {BASE.key: BASE}, {BASE.key: out}, now=t0 + 3 * 86_400)
    assert r["action"] == "open" and len(bot._closed(engine)) == 1
    assert "base" in bot.trust(engine)
    msgs = [a["message"] for a in recent_activity(engine) if a["kind"] == "bot"]
    assert any("вне диапазона" in m for m in msgs) and any(m.startswith("Бот открыл") for m in msgs)


def test_bot_waits_when_nothing_pays_off(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    e_pool, _, _ = pool_entry(ETH, gas=50.0, fees_week=(1.0, 2.0))
    scan = {"capital_usd": 10_000.0, "horizon_days": 7, "eth_usd": 3000.0, "btc_usd": 80_000.0, "pools": [e_pool]}
    r = bot.tick(engine, load_config(), scan, {ETH.key: ETH}, {}, now=10_000)
    assert r["action"] == "wait" and r["cash_usd"] == 100.0
    assert "ждёт" in recent_activity(engine)[0]["message"]
