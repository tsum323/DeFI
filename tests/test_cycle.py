"""UNISWAP AI LAB routine: daily schedule, honest failure logging, API sections (SYNTHETIC)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.database import schema as S
from backend.lab.activity import log_activity, recent_activity
from backend.lab.cycle import daily_due, step
from backend.lab.scanner import set_state

MSK = timezone(timedelta(hours=3))


def ts(y, mo, d, h):
    return int(datetime(y, mo, d, h, tzinfo=MSK).timestamp())


def test_daily_research_once_per_moscow_day_after_3(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    assert not daily_due(engine, ts(2026, 11, 3, 2))           # before 03:00 MSK
    assert daily_due(engine, ts(2026, 11, 3, 4))
    set_state(engine, "daily_research", {"day": "2026-11-03"})
    assert not daily_due(engine, ts(2026, 11, 3, 23))          # already done today
    assert daily_due(engine, ts(2026, 11, 4, 3))


def test_failed_step_is_logged_as_error_not_success(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")

    def boom():
        raise RuntimeError("source unavailable")
    assert step(engine, "scan", boom) is None
    log_activity(engine, "data", "ok", "loaded")
    items = recent_activity(engine)
    assert items[1]["status"] == "error" and "source unavailable" in items[1]["message"]
    assert items[0]["status"] == "ok"


def test_lab_api_sections_on_empty_database(tmp_path, monkeypatch):
    from backend.api import lab as L
    from backend.core.config import load_config
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    cfg = load_config()
    d = L.dashboard(engine, cfg)
    assert d["status"] == "stale" and d["counts"] == {"CONSERVATIVE": 0, "MODERATE": 0, "EXPERIMENTAL": 0}
    assert L.signals(engine)["cards"] == [] and "потере" in L.signals(engine)["risk_warning"]
    assert L.portfolio(engine, cfg)["balance_usd"] == cfg.lab["portfolio"]["virtual_capital_usd"]
    assert L.research(engine)["hypotheses"] == [] and L.activity(engine)["items"] == []


def test_best_of_day_and_entry_price_guide():
    from backend.api.lab import add_entry_prices, best_today

    def card(i, pair, tier, net, src="estimated", price=100.0, pool="p"):
        return {"id": i, "pair": pair, "tier": tier, "net_usd": net, "source": src, "price": price,
                "pool": pool, "quote": "USDC", "base": pair.split("/")[0]}
    cards = [card("a", "WBTC/USDC", "MODERATE", [1, 9], "exact"), card("b", "WBTC/USDC", "EXPERIMENTAL", [2, 20], "exact"),
             card("c", "WETH/USDC", "EXPERIMENTAL", [3, 5]), card("d", "WETH/USDC", "EXPERIMENTAL", [-9, -1]),
             card("e", "WBTC/WETH", "EXPERIMENTAL", [-5, -1])]
    assert best_today(cards) == ["a", "b", "c"]        # one per pair + a second tier; never a negative one
    scan = {"pools": [{"pool": "p", "pair": "WBTC/USDC", "price": 100.0, "vwap_7d": 110.0, "low_7d": 90.0, "high_7d": 120.0,
                       "fee_tier": 0.003, "volume_usd": {"7d": 1e6}, "base_usd": 100.0, "quote_usd": 1.0}]}
    low = [card("a", "WBTC/USDC", "MODERATE", [1, 9], price=100.0)]
    add_entry_prices(low, scan)
    assert low[0]["entry"]["recommended"] == 100.0 and "ниже средней" in low[0]["entry"]["verdict"]
    high = [card("a", "WBTC/USDC", "MODERATE", [1, 9], price=121.0)]
    add_entry_prices(high, scan)
    assert high[0]["entry"]["recommended"] == 110.0 and "подождать" in high[0]["entry"]["verdict"]
