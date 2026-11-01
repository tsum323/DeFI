"""Virtual portfolio (SPEC §12): every OPEN CANDIDATE becomes a virtual position.

- Opening: the position's parameters (strategy, capital, signal block, validity)
  are copied from the signal once and never changed; a SHA-256 of the signal
  payload is stored to prove the signal itself was not edited later.
- Marking: the position is the backtest of its own strategy started at the
  signal block (same state, same code as the signal) and replayed on the exact
  pool reconstruction up to the latest synced block. Fees are MODELED (bounds A
  and B on the real path) and are never presented as fees actually received.
- Benchmarks from the same start and capital: HODL of the opening tokens, the
  passive full-range LP, and the base strategies.
- Lifetime: until the signal is closed or invalidated, at most until valid_until.
  Outcome when closed: success if net (model A, after gas) > 0, else failure.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import time
from dataclasses import asdict

from sqlalchemy import insert, select

from backend.backtesting.position_sim import Dataset, PositionResult, simulate_many
from backend.database import schema as S
from backend.strategies.generator import BASE_STRATEGIES, params_from_json

BENCHMARKS = ("passive_full_range", "static_wide_50", "exit_rebalance_10")
DAY = 86_400


def payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode()).hexdigest()


def open_from_signal(engine, signal_row) -> int | None:
    """Create the virtual position of an OPEN CANDIDATE (idempotent per signal)."""
    if signal_row.signal_type != "OPEN CANDIDATE":
        return None
    with engine.begin() as conn:
        existing = conn.execute(select(S.virtual_positions.c.id).where(
            S.virtual_positions.c.signal_id == signal_row.id)).first()
        if existing:
            return existing[0]
        p = json.loads(signal_row.payload_json)
        return conn.execute(insert(S.virtual_positions).values(
            signal_id=signal_row.id, created_at=int(time.time()), open_block=signal_row.block,
            spec_hash=signal_row.spec_hash, params_json=json.dumps(p["params"]),
            capital_token1=p["capital_token1"], valid_until=p["valid_until"],
            signal_payload_sha256=payload_hash(signal_row.payload_json),
            opening_json=json.dumps({"range": p["range"], "allocation": p["allocation"]}),
        )).inserted_primary_key[0]


def open_new_positions(engine) -> list[int]:
    with engine.connect() as conn:
        rows = conn.execute(select(S.signals).where(S.signals.c.signal_type == "OPEN CANDIDATE")).all()
    return [pid for pid in (open_from_signal(engine, r) for r in rows) if pid]


def _close_info(conn, pos) -> tuple[int | None, str | None]:
    ev = conn.execute(select(S.signal_events).where(
        S.signal_events.c.signal_id == pos.signal_id,
        S.signal_events.c.status.in_(("CLOSE CANDIDATE", "INVALIDATED")))
        .order_by(S.signal_events.c.id).limit(1)).first()
    return (ev.block, f"{ev.status}: {ev.reason}") if ev else (None, None)


def _block_at_ts(ds: Dataset, ts: int) -> int:
    blocks = sorted(ds.blocks)
    i = bisect.bisect_right([ds.blocks[b][0] for b in blocks], ts) - 1
    return blocks[max(0, i)]


def mark_all(engine, ds: Dataset, state_at_fn, latest_block: int, gas_set: str = "gas_units",
             priority_fee_gwei: float = 1.0) -> list[dict]:
    """Recompute every position and its benchmarks up to `latest_block`."""
    out = []
    with engine.connect() as conn:
        positions = conn.execute(select(S.virtual_positions)).all()
        sig_payloads = {r.id: r.payload_json for r in conn.execute(select(S.signals))}
        closes = {pos.id: _close_info(conn, pos) for pos in positions}
    for pos in positions:
        if payload_hash(sig_payloads[pos.signal_id]) != pos.signal_payload_sha256:
            raise RuntimeError(f"signal {pos.signal_id} was modified after the position opened")
        close_block, close_reason = closes[pos.id]
        expiry_block = _block_at_ts(ds, pos.valid_until)
        if close_block is None and ds.blocks[latest_block][0] >= pos.valid_until:
            close_block, close_reason = expiry_block, "validity period ended"
        end = min(latest_block, close_block) if close_block else latest_block
        if end <= pos.open_block:
            continue
        params = params_from_json(pos.params_json)
        capital = int(pos.capital_token1)
        specs = [params.to_spec(pos.open_block, end, capital, gas_set, priority_fee_gwei)]
        specs += [BASE_STRATEGIES[n].to_spec(pos.open_block, end, capital, gas_set, priority_fee_gwei)
                  for n in BENCHMARKS]
        results = simulate_many(ds, specs, start_state=state_at_fn(pos.open_block - 1),
                                return_exceptions=True)
        main, bench = results[0], dict(zip(BENCHMARKS, results[1:]))
        if not isinstance(main, PositionResult):
            raise RuntimeError(f"virtual position {pos.id}: {main}")
        opening = json.loads(pos.opening_json)
        a0 = main.actions[0] if main.actions else None
        consistent = a0 is not None and (a0.tick_lower, a0.tick_upper) == (
            opening["range"]["tick_lower"], opening["range"]["tick_upper"])
        m = main.metrics
        res = {"strategy": params.name, "capital": capital, "open_block": pos.open_block,
               "last_block": end, "metrics": m, "opening_matches_signal": consistent,
               "rebalances": [asdict(a) for a in main.actions[1:]],
               "benchmarks": {n: (r.metrics if isinstance(r, PositionResult) else {"error": str(r)})
                              for n, r in bench.items()},
               "labels": {"prices_state": "EXACT", "fees": "MODELED", "pnl": "MODELED"}}
        passive = bench.get("passive_full_range")
        res["vs_passive_a"] = (m["net_a"] - passive.metrics["net_a"]
                               if isinstance(passive, PositionResult) else None)
        status = "closed" if close_block and end >= close_block else "open"
        outcome = (("success" if m["net_a"] > 0 else "failure") if status == "closed" else None)
        now = int(time.time())
        with engine.begin() as conn:
            conn.execute(S.virtual_marks.delete().where(S.virtual_marks.c.position_id == pos.id))
            conn.execute(insert(S.virtual_marks), [
                {"position_id": pos.id, "block": r["block"], "timestamp": r["timestamp"],
                 "mark_json": json.dumps(r)} for r in main.timeseries])
            conn.execute(S.virtual_results.delete().where(S.virtual_results.c.position_id == pos.id))
            conn.execute(insert(S.virtual_results).values(
                position_id=pos.id, status=status, close_block=close_block if status == "closed" else None,
                close_reason=close_reason if status == "closed" else None, outcome=outcome,
                last_block=end, result_json=json.dumps(res), updated_at=now))
        out.append({"position_id": pos.id, "status": status, "outcome": outcome, **res})
    return out


def journal(engine) -> dict:
    """SPEC 12 journal: all / successful / failed / cancelled signals, active positions,
    cumulative virtual result, weekly and monthly return, max virtual drawdown."""
    with engine.connect() as conn:
        sigs = conn.execute(select(S.signals).order_by(S.signals.c.id)).all()
        last_status = {}
        for ev in conn.execute(select(S.signal_events).order_by(S.signal_events.c.id)):
            last_status[ev.signal_id] = ev.status
        res = {r.position_id: r for r in conn.execute(select(S.virtual_results))}
        pos = {p.id: p for p in conn.execute(select(S.virtual_positions))}
        marks = conn.execute(select(S.virtual_marks.c.position_id, S.virtual_marks.c.timestamp,
                                    S.virtual_marks.c.mark_json)).all()
    by_signal = {p.signal_id: pid for pid, p in pos.items()}
    rows = []
    for s in sigs:
        pid = by_signal.get(s.id)
        r = res.get(pid) if pid else None
        rows.append({"signal_id": s.id, "type": s.signal_type, "status": last_status.get(s.id),
                     "created_at": s.created_at, "position_id": pid,
                     "position_status": r.status if r else None, "outcome": r.outcome if r else None,
                     "net_a": json.loads(r.result_json)["metrics"]["net_a"] if r else None})
    # aggregate equity: capital + net_a of every position over time (each position fixed after close)
    series: dict[int, list] = {}
    for pid, ts, mj in marks:
        series.setdefault(pid, []).append((ts, json.loads(mj)["net_a"]))
    times = sorted({ts for v in series.values() for ts, _ in v})
    caps = {pid: int(p.capital_token1) for pid, p in pos.items()}
    equity = []
    for t in times:
        total_cap = total_net = 0
        for pid, pts in series.items():
            if pts[0][0] > t:
                continue
            i = bisect.bisect_right([x[0] for x in pts], t) - 1
            total_cap += caps[pid]
            total_net += pts[i][1]
        equity.append((t, total_cap, total_net))
    peak, mdd = None, 0.0
    for _, cap, net in equity:
        v = cap + net
        peak = v if peak is None or v > peak else peak
        if peak:
            mdd = max(mdd, (peak - v) / peak)

    def ret(days):
        if not equity:
            return None
        t_end = equity[-1][0]
        past = [e for e in equity if e[0] <= t_end - days * DAY]
        if not past:
            return None
        a, b = past[-1], equity[-1]
        return (b[2] - a[2]) / b[1] if b[1] else None
    closed = [r for r in res.values() if r.status == "closed"]
    return {
        "signals": rows,
        "counts": {"all": len(sigs),
                   "open_candidates": sum(s.signal_type == "OPEN CANDIDATE" for s in sigs),
                   "successful": sum(r.outcome == "success" for r in closed),
                   "failed": sum(r.outcome == "failure" for r in closed),
                   "cancelled": sum(v == "INVALIDATED" for v in last_status.values()),
                   "active_positions": sum(r.status == "open" for r in res.values())},
        "cumulative_net_a": sum(json.loads(r.result_json)["metrics"]["net_a"] for r in res.values()),
        "return_week": ret(7), "return_month": ret(30), "max_drawdown": mdd,
        "label": "MODELED",
    }


# ---------------------------------------------------------------- forward tests
def add_forward_test(engine, params, start_block: int, capital_token1: int, reason: str) -> int:
    """Watch a candidate strategy from `start_block` on (research, not a signal).
    The parameters are fixed at registration; only later data can judge it."""
    with engine.begin() as conn:
        row = conn.execute(select(S.forward_tests.c.id).where(
            S.forward_tests.c.spec_hash == params.spec_hash,
            S.forward_tests.c.start_block == start_block)).first()
        if row:
            return row[0]
        return conn.execute(insert(S.forward_tests).values(
            created_at=int(time.time()), start_block=start_block, spec_hash=params.spec_hash,
            params_json=json.dumps(asdict(params)), capital_token1=str(capital_token1),
            reason=reason)).inserted_primary_key[0]


def mark_forward_tests(engine, ds: Dataset, state_at_fn, latest_block: int,
                       priority_fee_gwei: float = 1.0) -> list[dict]:
    with engine.connect() as conn:
        tests = conn.execute(select(S.forward_tests)).all()
    out = []
    by_start: dict[tuple, list] = {}
    for t in tests:
        if latest_block > t.start_block:
            by_start.setdefault((t.start_block, t.capital_token1), []).append(t)
    for (start, cap), group in by_start.items():
        capital = int(cap)
        params = [params_from_json(t.params_json) for t in group]
        passive = BASE_STRATEGIES["passive_full_range"]
        specs = [p.to_spec(start, latest_block, capital, "gas_units", priority_fee_gwei)
                 for p in params + [passive]]
        res = simulate_many(ds, specs, start_state=state_at_fn(start - 1), return_exceptions=True)
        pm = res[-1].metrics if isinstance(res[-1], PositionResult) else None
        for t, p, r in zip(group, params, res):
            if not isinstance(r, PositionResult):
                doc = {"error": str(r)}
            else:
                m = r.metrics
                doc = {"strategy": p.name, "start_block": start, "last_block": latest_block,
                       "days": m["days"], "net_a": m["net_a"], "net_b": m["net_b"],
                       "fees_a": m["fees_a"], "gas_cost": m["gas_cost"], "vs_hodl_a": m["vs_hodl_a"],
                       "vs_passive_a": (m["net_a"] - pm["net_a"]) if pm else None,
                       "rebalances": m["rebalances"], "ruined": m["ruined"],
                       "label": "MODELED (forward test on data unseen at registration)"}
            with engine.begin() as conn:
                conn.execute(S.forward_tests.update().where(S.forward_tests.c.id == t.id).values(
                    result_json=json.dumps(doc), updated_at=int(time.time())))
            out.append({"id": t.id, "reason": t.reason, **doc})
    return out
