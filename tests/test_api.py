"""Stage 10: dashboard API queries on a SYNTHETIC database (no real data needed)."""

from __future__ import annotations

from backend.api import queries as Q
from backend.core.config import load_config
from backend.database import schema as S


def test_queries_work_on_an_empty_database(tmp_path, monkeypatch):
    cfg = load_config()
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    monkeypatch.setattr(Q, "study_from_report", lambda: (None, None, None))
    o = Q.overview(engine, cfg)
    assert o["experiments"] == {"value": 0, "label": "MODELED"}
    assert o["active_signals"]["label"] == "SCENARIO"
    assert Q.ranking(engine, cfg)["rows"] == []
    assert Q.signals(engine)["signals"] == [] and Q.research(engine)["hypotheses"] == []
    p = Q.portfolio(engine)
    assert p["journal"]["counts"]["all"] == 0 and p["positions"] == []
    ds = Q.datasources(engine, cfg)
    assert ds["label"] == "EXACT" and ds["network"] == "ethereum"
    assert all("url" not in e for e in ds["endpoints"])  # never expose endpoint URLs (keys)
    assert Q.strategy_details(engine, cfg, "0" * 64) is None


def test_api_routes_are_read_only():
    from backend.api.app import app
    methods = {m for r in app.routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET", "HEAD"}
