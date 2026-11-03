"""Virtual portfolio (UNISWAP AI LAB): virtual money only, never a transaction.

Capital comes from config/lab.yaml `portfolio.virtual_capital_usd`. Every new opportunity
(the best of each tier, plus the best scanner pools) opens a virtual position with a share
of the capital (CONSERVATIVE 30%, MODERATE 15%, EXPERIMENTAL 5%) while free virtual cash
lasts. Positions have their own history and are never deleted or re-written when the
algorithms change.

Two kinds, never mixed up:
  exact      WBTC/USDC 0.3%: backed by a forward test on the exact pool replay (fees via
             fee growth, every swap, gas from real base fees) — MODELED.
  estimated  other pools: a static range valued on hourly closes from the scanner (fees =
             hour's fees x our share of active liquidity) — ESTIMATED.

A position closes after `max_days`, or when its strategy has been out of the ranking for
`close_after_hours_out_of_ranking`; the final result is frozen.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, update

from backend.database import schema as S
from backend.lab.learning import record_forecast
from backend.lab.scanner import amounts_for_liquidity
from backend.lab.scanner import position as scan_position
from backend.virtual_portfolio.portfolio import add_forward_test

MSK = timezone(timedelta(hours=3))
PREFIX = "portfolio: "


def _settings(cfg) -> dict:
    return cfg.lab["portfolio"]


def scale_card(card: dict, allocation: float) -> dict:
    """The opportunity card (computed for its deposit) re-sized to `allocation` USD:
    fees, loss vs holding and the swap scale with size; gas does not."""
    k = allocation / card["allocation"]["deposit_usd"]
    c = dict(card)
    c["expected_fees_usd"] = [x * k for x in card["expected_fees_usd"]]
    c["expected_il_usd"] = card["expected_il_usd"] * k
    costs = dict(card["costs_usd"])
    costs["swap"] = costs["swap"] * k
    c["costs_usd"] = costs
    gas = costs["gas_open"] + costs["gas_close"]
    c["expected_net_vs_hodl_usd"] = [f + c["expected_il_usd"] - gas - costs["swap"] for f in c["expected_fees_usd"]]
    c["allocation"] = {**card["allocation"], "deposit_usd": allocation}
    return c


def _open_rows(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(select(S.portfolio_positions).where(S.portfolio_positions.c.status == "open")).all()


def cash(engine, cfg) -> float:
    """Free virtual cash: capital + realized results of closed positions - open allocations."""
    with engine.connect() as conn:
        rows = conn.execute(select(S.portfolio_positions)).all()
    realized = sum(json.loads(r.mark_json or "{}").get("net_usd", 0) for r in rows if r.status == "closed")
    return _settings(cfg)["virtual_capital_usd"] + realized - sum(r.allocation_usd for r in rows if r.status == "open")


def open_from_ranking(engine, cfg, ranking: dict, strategies: dict, block: int, decimals1: int,
                      now: int | None = None) -> list[str]:
    """Best opportunity of each tier -> a virtual position (exact replay)."""
    now = now or int(time.time())
    st = _settings(cfg)
    have = {r.spec_hash for r in _open_rows(engine)}
    out = []
    for tier, opps in ranking.get("tiers", {}).items():
        if not opps or len(_open_rows(engine)) >= st["max_open"]:
            continue
        card = opps[0]
        alloc = st["virtual_capital_usd"] * st["allocation"][tier]
        if card["spec_hash"] in have or alloc > cash(engine, cfg) or strategies.get(card["spec_hash"]) is None:
            continue
        fid = add_forward_test(engine, strategies[card["spec_hash"]], block, int(alloc * 10 ** decimals1),
                               f"{PREFIX}{tier} ${alloc:,.0f}")
        scaled = scale_card(card, alloc)
        record_forecast(engine, fid, scaled, now)
        entry = {"price": card["current_price"], "range": [card["range"]["price_lower"], card["range"]["price_upper"]],
                 "deposit": card["allocation"], "forecast": {k: scaled[k] for k in (
                     "expected_fees_usd", "expected_il_usd", "costs_usd", "expected_net_vs_hodl_usd")},
                 "exit_probability": card["exit_probability"], "horizon_days": card["horizon_days"],
                 "rule": card["rebalance_rule"], "label": "MODELED"}
        with engine.begin() as conn:
            conn.execute(insert(S.portfolio_positions).values(
                opened_at=now, kind="exact", tier=tier, pool=card["pool"], pair=card["pair"],
                strategy=card["strategy"], spec_hash=card["spec_hash"], forward_test_id=fid,
                allocation_usd=alloc, entry_json=json.dumps(entry), status="open"))
        have.add(card["spec_hash"])
        out.append(f"{tier}: {card['pair']} {card['strategy']} на ${alloc:,.0f}")
    return out


def open_from_scan(engine, cfg, scan: dict, exclude_pools: set[str], n: int = 2,
                   now: int | None = None) -> list[str]:
    """Best scanner pools (other than the exactly modelled one) -> ESTIMATED positions."""
    now = now or int(time.time())
    st = _settings(cfg)
    open_pools = {r.pool for r in _open_rows(engine)}
    out = []
    for p in scan["pools"]:
        if len(out) >= n or p["pool"] in exclude_pools or p["pool"] in open_pools:
            continue
        b = p.get("best")
        if p["score"] is None or not b or b["net_mid_usd"] <= 0 or p["problems"]:
            continue
        alloc = st["virtual_capital_usd"] * st["allocation"]["EXPERIMENTAL"]
        if alloc > cash(engine, cfg) or len(_open_rows(engine)) >= st["max_open"]:
            break
        k = alloc / scan["capital_usd"]
        entry = {"price": p["price"], "range": [b["price_lower"], b["price_upper"]], "width": b["width"],
                 "amount0": b["amount0"] * k, "amount1": b["amount1"] * k, "usd": {"ETH": scan["eth_usd"], "BTC": scan["btc_usd"]},
                 "gas_usd": b["gas_usd"], "swap_usd": b["swap_usd"] * k, "since_hour": now - now % 3600,
                 "forecast": {"fees_usd": [x * k for x in b["fees_usd"]], "il_usd": b["il_usd"] * k,
                              "net_vs_hodl_usd": [x * k + b["gas_usd"] * (k - 1) for x in b["net_vs_hodl_usd"]]},
                 "exit_probability": b["exit_probability"], "horizon_days": scan["horizon_days"],
                 "label": "ESTIMATED"}
        with engine.begin() as conn:
            conn.execute(insert(S.portfolio_positions).values(
                opened_at=now, kind="estimated", tier="EXPERIMENTAL", pool=p["pool"], pair=p["pair"],
                strategy=f"static ±{b['width']:.0%}", spec_hash=None, allocation_usd=alloc,
                entry_json=json.dumps(entry), status="open"))
        open_pools.add(p["pool"])
        out.append(f"EXPERIMENTAL: {p['pair']} {p['fee_tier']:.2%} ±{b['width']:.0%} на ${alloc:,.0f}")
    return out


# ---------------------------------------------------------------- valuation
def mark_exact(row, ft_result: dict | None, decimals1: int = 6) -> dict | None:
    if not ft_result or "error" in ft_result:
        return None
    u = 10 ** decimals1
    return {"days": ft_result["days"], "value_usd": row.allocation_usd + ft_result["net_a"] / u,
            "net_usd": ft_result["net_a"] / u, "fees_usd": ft_result["fees_a"] / u,
            "il_usd": ft_result.get("il", 0) / u, "costs_usd": (ft_result["gas_cost"] + ft_result.get("swap_cost", 0)) / u,
            "vs_hodl_usd": ft_result["vs_hodl_a"] / u, "in_range": (ft_result.get("now") or {}).get("in_range"),
            "time_in_range": ft_result.get("time_in_range"), "price": (ft_result.get("now") or {}).get("price"),
            "rebalances": ft_result.get("rebalances"), "label": "MODELED"}


def mark_estimated(row, scan_pool, hours: list, usd_now: dict) -> dict | None:
    """Static range valued on hourly closes since opening (ESTIMATED)."""
    if scan_pool is None:
        return None
    e = json.loads(row.entry_json)
    usd = {"USDC": 1.0, "WETH": usd_now["ETH"], "WBTC": usd_now["BTC"]}
    u0, u1 = usd[scan_pool.token0], usd[scan_pool.token1]
    e0, e1 = e["usd"]["ETH"], e["usd"]["BTC"]
    eu = {"USDC": 1.0, "WETH": e0, "WBTC": e1}
    first = next((h for h in hours if h.hour_ts >= e["since_hour"]), None)
    if first is None:
        return {"days": 0.0, "value_usd": row.allocation_usd, "net_usd": 0.0, "fees_usd": 0.0, "il_usd": 0.0,
                "costs_usd": e["gas_usd"] + e["swap_usd"], "vs_hodl_usd": 0.0, "label": "ESTIMATED",
                "note": "ждёт новых часов данных"}
    # rebuild the position from the entry amounts at the entry price
    pos = scan_position(scan_pool, int(first.close_sqrt_price), e["width"], row.allocation_usd,
                        eu[scan_pool.token0], eu[scan_pool.token1])
    fees = inside = 0.0
    later = [h for h in hours if h.hour_ts >= e["since_hour"]]
    for h in later:
        sp = float(h.close_sqrt_price) / 2 ** 96
        if pos["sa"] <= sp < pos["sb"]:
            fees += (h.fee0 * u0 + h.fee1 * u1) * pos["L"] / (float(h.close_liquidity) + pos["L"])
            inside += 1
    last = later[-1]
    sp = float(last.close_sqrt_price) / 2 ** 96
    a0, a1 = amounts_for_liquidity(pos["L"], sp, pos["sa"], pos["sb"])
    lp = a0 / 10 ** scan_pool.dec0 * u0 + a1 / 10 ** scan_pool.dec1 * u1
    hold = pos["amount0"] * u0 + pos["amount1"] * u1
    costs = e["gas_usd"] + e["swap_usd"]
    net = lp + fees - costs - row.allocation_usd
    from backend.lab.scanner import base_price
    return {"days": (last.hour_ts - e["since_hour"]) / 86_400 + 1 / 24, "value_usd": row.allocation_usd + net,
            "net_usd": net, "fees_usd": fees, "il_usd": lp - hold, "costs_usd": costs,
            "vs_hodl_usd": lp + fees - costs - hold, "in_range": pos["sa"] <= sp < pos["sb"],
            "time_in_range": inside / len(later), "price": base_price(int(last.close_sqrt_price), scan_pool),
            "label": "ESTIMATED"}


def update_positions(engine, cfg, ranking: dict | None, scan_pools: dict, usd_now: dict,
                     now: int | None = None) -> dict:
    """Value every open position; close by the rules; freeze the final result."""
    now = now or int(time.time())
    st = _settings(cfg)
    ranked = {o["spec_hash"] for opps in (ranking or {}).get("tiers", {}).values() for o in opps} | \
             {v.get("spec_hash") for opps in (ranking or {}).get("tiers", {}).values() for o in opps
              for v in o.get("variants", [])}
    with engine.connect() as conn:
        rows = _open_rows(engine)
        fts = {f.id: f for f in conn.execute(select(S.forward_tests))}
        hours = conn.execute(select(S.pool_hours).order_by(S.pool_hours.c.hour_ts)).all()
    by_pool: dict[str, list] = {}
    for h in hours:
        by_pool.setdefault(h.pool, []).append(h)
    closed = []
    for r in rows:
        if r.kind == "exact":
            ft = fts.get(r.forward_test_id)
            m = mark_exact(r, json.loads(ft.result_json) if ft and ft.result_json else None)
        else:
            m = mark_estimated(r, scan_pools.get(r.pool), by_pool.get(r.pool, []), usd_now)
        vals: dict = {}
        if m is not None:
            vals["mark_json"] = json.dumps(m)
        out_since = r.out_of_ranking_since
        if r.kind == "exact":
            if r.spec_hash in ranked:
                out_since = None
            elif out_since is None:
                out_since = now
            vals["out_of_ranking_since"] = out_since
        reason = None
        if now - r.opened_at >= st["max_days"] * 86_400:
            reason = f"срок наблюдения {st['max_days']} дн. истёк"
        elif out_since and now - out_since >= st["close_after_hours_out_of_ranking"] * 3600:
            reason = "стратегия больше не в рейтинге возможностей"
        if reason:
            vals.update(status="closed", closed_at=now, close_reason=reason)
            closed.append(f"{r.pair} {r.strategy}: {reason}")
        if vals:
            with engine.begin() as conn:
                conn.execute(update(S.portfolio_positions).where(S.portfolio_positions.c.id == r.id).values(**vals))
    return {"marked": len(rows), "closed": closed}


def closed_forward_tests(engine) -> set[int]:
    """Forward tests behind closed positions are no longer re-simulated."""
    with engine.connect() as conn:
        return {r.forward_test_id for r in conn.execute(select(S.portfolio_positions).where(
            S.portfolio_positions.c.status == "closed")) if r.forward_test_id}


def summary(engine, cfg, now: int | None = None) -> dict:
    now = now or int(time.time())
    with engine.connect() as conn:
        rows = conn.execute(select(S.portfolio_positions).order_by(S.portfolio_positions.c.id)).all()
        hist = conn.execute(select(S.portfolio_history).order_by(S.portfolio_history.c.day)).all()
    cap = _settings(cfg)["virtual_capital_usd"]
    pos = []
    for r in rows:
        m = json.loads(r.mark_json) if r.mark_json else None
        pos.append({"id": r.id, "opened_at": r.opened_at, "kind": r.kind, "tier": r.tier, "pool": r.pool,
                    "pair": r.pair, "strategy": r.strategy, "allocation_usd": r.allocation_usd,
                    "entry": json.loads(r.entry_json), "mark": m, "status": r.status,
                    "closed_at": r.closed_at, "close_reason": r.close_reason})

    def tot(key, rows_):
        return sum((p["mark"] or {}).get(key, 0) for p in rows_)
    open_, closed_ = [p for p in pos if p["status"] == "open"], [p for p in pos if p["status"] == "closed"]
    net = tot("net_usd", pos)
    balance = cap + net
    out = {"initial_usd": cap, "balance_usd": balance, "pnl_usd": net, "pnl_pct": net / cap if cap else None,
           "fees_usd": tot("fees_usd", pos), "il_usd": tot("il_usd", pos), "costs_usd": tot("costs_usd", pos),
           "vs_hodl_usd": tot("vs_hodl_usd", pos), "cash_usd": cash(engine, cfg),
           "open": open_, "closed": closed_, "history": [json.loads(h.payload_json) for h in hist],
           "labels": {"exact": "MODELED (точная реконструкция пула)", "estimated": "ESTIMATED (часовые данные)"}}
    day_ago = [h for h in out["history"] if h["at"] <= now - 86_400]
    out["day_change_usd"] = balance - day_ago[-1]["balance_usd"] if day_ago else None
    return out


def snapshot_day(engine, s: dict, now: int | None = None) -> None:
    now = now or int(time.time())
    day = datetime.fromtimestamp(now, MSK).strftime("%Y-%m-%d")
    payload = {"day": day, "at": now, **{k: s[k] for k in ("balance_usd", "pnl_usd", "fees_usd", "il_usd",
                                                             "costs_usd", "vs_hodl_usd")},
               "open": len(s["open"]), "closed": len(s["closed"])}
    with engine.begin() as conn:
        conn.execute(S.portfolio_history.delete().where(S.portfolio_history.c.day == day))
        conn.execute(insert(S.portfolio_history).values(day=day, created_at=now, payload_json=json.dumps(payload)))
