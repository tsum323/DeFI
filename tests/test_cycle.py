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
