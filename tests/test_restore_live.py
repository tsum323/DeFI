"""Regression: an older database must not win over the newer live state (SYNTHETIC).

On 2026-11-03/04 the once-a-day database cache kept old scan progress and old positions, the
newer live state was merged with "insert or ignore", and the same swaps were counted again on
every update. Now the newer state replaces live tables; research tables stay append-only."""

from __future__ import annotations

from sqlalchemy import insert, select

from backend.database import schema as S
from backend.database.research_snapshot import export_research, restore_research
from backend.lab.scanner import fill_timestamps


def test_newer_live_state_replaces_older_database(tmp_path):
    old = S.make_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    new = S.make_engine(f"sqlite:///{tmp_path / 'new.sqlite'}")
    for e, last, swaps in ((old, 100, 5), (new, 200, 7)):
        with e.begin() as c:
            c.execute(insert(S.scan_state).values(stream="scanner_swaps", first_block=1, last_block=last, updated_at=0))
            c.execute(insert(S.pool_hours).values(pool="p", hour_ts=3600, swaps=swaps, vol0=0, vol1=0, fee0=0, fee1=0,
                                                  close_sqrt_price="1", close_tick=0, close_liquidity="1", last_block=last))
            c.execute(insert(S.strategies).values(spec_hash="a" * 64 if e is old else "b" * 64, name="s", params_json="{}"))
    with new.begin() as c:   # the newer state also deleted an hour the old one still has
        c.execute(insert(S.pool_hours).values(pool="q", hour_ts=7200, swaps=1, vol0=0, vol1=0, fee0=0, fee1=0,
                                              close_sqrt_price="1", close_tick=0, close_liquidity="1", last_block=1))
    with old.begin() as c:
        c.execute(insert(S.pool_hours).values(pool="gone", hour_ts=1, swaps=9, vol0=0, vol1=0, fee0=0, fee1=0,
                                              close_sqrt_price="1", close_tick=0, close_liquidity="1", last_block=1))
    state = tmp_path / "state"
    export_research(new, state)
    restore_research(old, state, mirror_live=True)
    with old.connect() as c:
        assert c.execute(select(S.scan_state.c.last_block)).scalar() == 200          # newer progress wins
        hours = {r.pool: r.swaps for r in c.execute(select(S.pool_hours))}
        assert hours == {"p": 7, "q": 1}                                             # no stale or deleted rows
        assert len(c.execute(select(S.strategies)).all()) == 2                       # research: append-only


class Blocks:
    def call(self, m, p):
        return {"timestamp": hex({0x64: 1_700_000_000, 0xc8: 1_700_000_100}[int(p[0], 16)])}


def test_missing_block_timestamps_are_interpolated():
    logs = [{"blockNumber": hex(150)}, {"blockNumber": hex(200), "blockTimestamp": "0x0"},
            {"blockNumber": hex(120), "blockTimestamp": hex(1_700_000_040)}]
    fill_timestamps(Blocks(), logs, 100, 200)
    assert [int(x["blockTimestamp"], 16) for x in logs] == [1_700_000_050, 1_700_000_100, 1_700_000_040]


def test_repair_once_rebuilds_hours_and_restarts_bot(tmp_path):
    from backend.core.config import load_config
    from backend.lab.activity import recent_activity
    from backend.lab.cycle import repair_once
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    with engine.begin() as c:
        c.execute(insert(S.scan_state).values(stream="scanner_swaps", first_block=1, last_block=5, updated_at=0))
        c.execute(insert(S.bot_positions).values(opened_at=0, pool="p", network="ethereum", pair="X/Y", width=0.1,
                                                 allocation_usd=100, entry_json="{}", status="open"))
    assert repair_once(engine, load_config()) is True and repair_once(engine, load_config()) is False
    with engine.connect() as c:
        assert not c.execute(select(S.scan_state)).all() and not c.execute(select(S.bot_positions)).all()
    assert any("перезапущен" in a["message"] for a in recent_activity(engine))
