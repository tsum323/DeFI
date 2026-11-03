"""«Мой бот» — a virtual twin of the user (virtual money only, never a transaction).

It starts with `bot.start_usd` (100 USD), holds ONE position at a time in the pool / range
that is best FOR ITS OWN AMOUNT (gas is fixed per position, so with 100 USD the cheap
networks usually win), and reinvests the result.

Rules (deterministic, explained in plain language in the activity log):
  open    — no position: take the best option with a positive expected result for the bot's
            amount (fees, loss vs holding and the swap scale with the amount; gas does not),
            whose chance of leaving the range within a week is <= max_exit_probability.
            None? Wait and say why.
  hold    — value the position every update (hourly pool data, ESTIMATED).
  re-centre / move — the price has been outside the range for out_of_range_hours: close and
            open again around the price (or elsewhere, if that is better now).
  switch  — at most once a day, after min_hold_days: only if the best option's expected
            result beats the current one by switch_margin AFTER paying to close and reopen.
  learn   — per network, the ratio of fees actually earned to fees forecast (pro rata) is
            kept as a trust factor (0.5-1.2) and applied to that network's forecasts.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import insert, select, update

from backend.database import schema as S

MSK = timezone(timedelta(hours=3))
DAY = 86_400


def _cfg(cfg) -> dict:
    return cfg.lab["bot"]


def _open(engine):
    with engine.connect() as c:
        return c.execute(select(S.bot_positions).where(S.bot_positions.c.status == "open")).first()


def _closed(engine) -> list:
    with engine.connect() as c:
        return c.execute(select(S.bot_positions).where(S.bot_positions.c.status == "closed")
                         .order_by(S.bot_positions.c.id)).all()


def cash(engine, cfg) -> float:
    """Money the bot has when it holds nothing: start + result of every closed position."""
    closed = _closed(engine)
    if not closed:
        return float(_cfg(cfg)["start_usd"])
    return json.loads(closed[-1].mark_json)["value_usd"]


def trust(engine) -> dict[str, float]:
    from backend.lab.scanner import get_state
    return (get_state(engine, "bot_trust") or {}).get("by_network", {})


def options(scan: dict, amount: float, trust_by_net: dict, max_exit: float) -> list[dict]:
    """Every (pool, range) of the scan evaluated for `amount` USD, best first."""
    out = []
    ref = scan["capital_usd"]
    k = amount / ref
    for p in scan.get("pools", []):
        if p.get("problems") or p.get("data_quality"):
            continue
        t = trust_by_net.get(p.get("network", "ethereum"), 1.0)
        for r in p.get("ranges", []):
            if r["exit_probability"] > max_exit:
                continue
            fees = [x * k * t for x in r["fees_usd"]]
            net = [f + r["il_usd"] * k - r["gas_usd"] - r["swap_usd"] * k for f in fees]
            mid = sum(net) / 2
            out.append({"pool": p["pool"], "network": p.get("network", "ethereum"),
                        "network_ru": p.get("network_ru", "Ethereum"), "pair": p["pair"], "fee_tier": p["fee_tier"],
                        "width": r["width"], "price": p["price"], "price_lower": r["price_lower"],
                        "price_upper": r["price_upper"], "amount0": r["amount0"] * k, "amount1": r["amount1"] * k,
                        "fees_usd": fees, "il_usd": r["il_usd"] * k, "gas_usd": r["gas_usd"], "swap_usd": r["swap_usd"] * k,
                        "net_usd": net, "net_mid_usd": mid, "exit_probability": r["exit_probability"],
                        "score": mid * (1 - r["exit_probability"]), "trust": t,
                        "token0": p.get("token0"), "token1": p.get("token1")})
    out.sort(key=lambda o: -o["score"])
    return out


def _say(engine, status: str, msg: str, details: dict | None = None) -> None:
    from backend.lab.activity import log_activity
    log_activity(engine, "bot", status, msg, details)


def _describe(o: dict) -> str:
    return (f"{o['pair']} {o['fee_tier']:.2%} в сети {o['network_ru']}, диапазон ±{o['width']:.0%} "
            f"({o['price_lower']:,.4g}–{o['price_upper']:,.4g})")


def _open_position(engine, scan: dict, o: dict, amount: float, now: int, why: str) -> None:
    entry = {"price": o["price"], "range": [o["price_lower"], o["price_upper"]], "width": o["width"],
             "amount0": o["amount0"], "amount1": o["amount1"], "usd": {"ETH": scan["eth_usd"], "BTC": scan["btc_usd"]},
             "gas_usd": o["gas_usd"], "swap_usd": o["swap_usd"], "since_hour": now - now % 3600,
             "forecast": {"fees_usd": o["fees_usd"], "il_usd": o["il_usd"], "net_vs_hodl_usd": o["net_usd"],
                          "horizon_days": scan["horizon_days"]}, "trust": o["trust"], "why": why, "label": "ESTIMATED"}
    with engine.begin() as c:
        c.execute(insert(S.bot_positions).values(
            opened_at=now, pool=o["pool"], network=o["network"], pair=o["pair"], width=o["width"],
            allocation_usd=amount, entry_json=json.dumps(entry), status="open"))
    _say(engine, "info", f"Бот открыл позицию на ${amount:,.2f}: {_describe(o)}. {why}", {"option": o})


def _close(engine, row, mark: dict, now: int, reason: str) -> None:
    with engine.begin() as c:
        c.execute(update(S.bot_positions).where(S.bot_positions.c.id == row.id).values(
            status="closed", closed_at=now, close_reason=reason, mark_json=json.dumps(mark)))
    _say(engine, "info", f"Бот закрыл позицию {row.pair} ({row.network}): {reason}. Итог позиции "
         f"${mark['net_usd']:+,.2f}, стало ${mark['value_usd']:,.2f}.", {"mark": mark})
    _learn(engine, row, mark)


def _learn(engine, row, mark: dict) -> None:
    """Trust factor per network = actual fees / forecast fees (pro rata), smoothed."""
    from backend.lab.scanner import get_state, set_state
    e = json.loads(row.entry_json)
    f = e["forecast"]
    days = mark.get("days") or 0
    if days < 1:
        return
    fc = (f["fees_usd"][0] + f["fees_usd"][1]) / 2 * days / f.get("horizon_days", 7)
    if fc <= 0:
        return
    ratio = max(0.5, min(1.2, mark["fees_usd"] / fc))
    st = get_state(engine, "bot_trust") or {"by_network": {}, "history": []}
    old = st["by_network"].get(row.network, 1.0)
    st["by_network"][row.network] = round(0.5 * old + 0.5 * ratio, 3)
    st["history"].append({"network": row.network, "ratio": round(ratio, 3), "days": round(days, 2)})
    set_state(engine, "bot_trust", st)
    _say(engine, "info", f"Бот учёл результат: в сети {row.network} получено {ratio:.0%} от прогноза комиссий; "
         f"доверие к прогнозам этой сети теперь {st['by_network'][row.network]:.2f}")


def tick(engine, cfg, scan: dict | None, scan_pools: dict, hours_by_pool: dict, now: int | None = None) -> dict:
    """One decision step of the bot (each market update)."""
    from backend.lab.portfolio import mark_estimated
    from backend.lab.scanner import get_state, set_state
    now = now or int(time.time())
    bc = _cfg(cfg)
    row = _open(engine)
    if scan is None:
        return {"action": "no_data"}
    usd_now = {"ETH": scan["eth_usd"], "BTC": scan["btc_usd"]}
    if row is not None:   # value the current position
        m = mark_estimated(SimpleNamespace(entry_json=row.entry_json, allocation_usd=row.allocation_usd),
                           scan_pools.get(row.pool), hours_by_pool.get(row.pool, []), usd_now)
        oor = row.out_of_range_since
        if m is not None:
            oor = None if m.get("in_range", True) else (oor or now)
            with engine.begin() as c:
                c.execute(update(S.bot_positions).where(S.bot_positions.c.id == row.id)
                          .values(mark_json=json.dumps(m), out_of_range_since=oor))
        m = m or json.loads(row.mark_json or "{}")
        amount = m.get("value_usd", row.allocation_usd)
        opts = options(scan, amount, trust(engine), bc["max_exit_probability"])
        best = opts[0] if opts else None
        if oor and now - oor >= bc["out_of_range_hours"] * 3600:
            _close(engine, row, m, now, f"цена вне диапазона больше {bc['out_of_range_hours']} ч — комиссии не идут")
            row = None
        else:
            day = datetime.fromtimestamp(now, MSK).strftime("%Y-%m-%d")
            reviewed = (get_state(engine, "bot_review") or {}).get("day")
            if reviewed != day and now - row.opened_at >= bc["min_hold_days"] * DAY and best is not None:
                set_state(engine, "bot_review", {"day": day})
                cur = next((o for o in opts if o["pool"] == row.pool and abs(o["width"] - row.width) < 1e-9), None)
                switch_cost = best["gas_usd"] + best["swap_usd"]   # reopening elsewhere
                cur_mid = cur["net_mid_usd"] if cur else 0.0
                if best["pool"] != row.pool and best["net_mid_usd"] - switch_cost > cur_mid * (1 + bc["switch_margin"]) + 0.01:
                    _close(engine, row, m, now, f"нашёлся вариант выгоднее: {_describe(best)} — ожидаемо "
                           f"${best['net_mid_usd']:,.2f} за неделю против ${cur_mid:,.2f} у текущей, даже с учётом "
                           f"${switch_cost:,.2f} на переезд")
                    row = None
                else:
                    _say(engine, "info", f"Бот держит позицию {row.pair} ({row.network}): "
                         + (f"лучший вариант сейчас ({_describe(best)}) выгоднее недостаточно, чтобы окупить переезд"
                            if best["pool"] != row.pool else "она и есть лучший вариант"))
            if row is not None:
                return {"action": "hold", "value_usd": amount}
    # no position: open the best affordable option
    amount = cash(engine, cfg)
    opts = options(scan, amount, trust(engine), bc["max_exit_probability"])
    good = [o for o in opts if o["net_mid_usd"] > 0 and o["net_usd"][0] > -0.25 * amount]
    if not good:
        last = (get_state(engine, "bot_wait") or {}).get("day")
        day = datetime.fromtimestamp(now, MSK).strftime("%Y-%m-%d")
        if last != day:
            set_state(engine, "bot_wait", {"day": day})
            _say(engine, "info", f"Бот ждёт: при ${amount:,.2f} ни один вариант не окупает газ и обмен "
                 "(или цена скорее всего выйдет из диапазона за неделю)")
        return {"action": "wait", "cash_usd": amount}
    o = good[0]
    alt = [x for x in good[1:] if x["network"] != o["network"]][:1]
    why = (f"Для ${amount:,.2f} это лучший вариант: ожидаемо ${o['net_usd'][0]:,.2f}…${o['net_usd'][1]:,.2f} за неделю "
           f"к простому хранению, газ ${o['gas_usd']:,.2f}, шанс выхода из диапазона {o['exit_probability']:.0%}"
           + (f"; следующий — {_describe(alt[0])} (${alt[0]['net_mid_usd']:,.2f})" if alt else ""))
    _open_position(engine, scan, o, amount, now, why)
    return {"action": "open", "option": o}


def summary(engine, cfg, now: int | None = None) -> dict:
    now = now or int(time.time())
    from backend.lab.activity import recent_activity
    with engine.connect() as c:
        rows = c.execute(select(S.bot_positions).order_by(S.bot_positions.c.id)).all()
        hist = c.execute(select(S.bot_history).order_by(S.bot_history.c.day)).all()
    start = float(_cfg(cfg)["start_usd"])
    pos = [{"id": r.id, "opened_at": r.opened_at, "pool": r.pool, "network": r.network, "pair": r.pair,
            "width": r.width, "allocation_usd": r.allocation_usd, "entry": json.loads(r.entry_json),
            "mark": json.loads(r.mark_json) if r.mark_json else None, "status": r.status,
            "closed_at": r.closed_at, "close_reason": r.close_reason, "out_of_range_since": r.out_of_range_since}
           for r in rows]
    cur = next((p for p in pos if p["status"] == "open"), None)
    balance = (cur["mark"] or {}).get("value_usd", cur["allocation_usd"]) if cur else cash(engine, cfg)
    decisions = [a for a in recent_activity(engine, 400) if a["kind"] == "bot"][:60]
    return {"start_usd": start, "balance_usd": balance, "pnl_usd": balance - start, "pnl_pct": balance / start - 1,
            "current": cur, "closed": [p for p in pos if p["status"] == "closed"][::-1],
            "history": [json.loads(h.payload_json) for h in hist], "decisions": decisions,
            "trust": trust(engine), "rules": _cfg(cfg), "label": "ESTIMATED"}


def snapshot_day(engine, s: dict, now: int | None = None) -> None:
    now = now or int(time.time())
    day = datetime.fromtimestamp(now, MSK).strftime("%Y-%m-%d")
    payload = {"day": day, "at": now, "balance_usd": s["balance_usd"], "pnl_usd": s["pnl_usd"],
               "network": (s["current"] or {}).get("network")}
    with engine.begin() as c:
        c.execute(S.bot_history.delete().where(S.bot_history.c.day == day))
        c.execute(insert(S.bot_history).values(day=day, created_at=now, payload_json=json.dumps(payload)))
