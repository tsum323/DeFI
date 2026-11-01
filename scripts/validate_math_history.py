"""Stage 3: validate the integer Uniswap v3 math against the real pool history.

Checks, over every event of the primary pool (strict (block, logIndex) order):

1. Tick: each Swap's `tick` equals getTickAtSqrtRatio(sqrtPriceX96), except the
   contract rule for a zeroForOne swap that stops exactly on an initialized tick
   (tick = tickNext - 1 while the price sits on tickNext's boundary).
2. Mint / Burn amounts: amount0/amount1 of each event equal
   _modifyPosition's SqrtPriceMath deltas at the pool price/tick at that moment
   (Mint rounds up, Burn rounds down). Exact — no tolerance.
3. Swap step: for consecutive swaps with no Mint/Burn in between, equal active
   liquidity and no position boundary crossed, the second swap is replayed with
   computeSwapStep(price_before -> price_after, L, fee) and the event amounts
   must match within EPS wei (exact-output swaps differ by rounding: a price-
   targeted replay cannot know the swap was exact-output).

Output: data/math_validation.json. Run: python -m scripts.validate_math_history
"""

from __future__ import annotations

import json
import sqlite3
import time
from bisect import bisect_left
from collections import Counter
from pathlib import Path

from backend.backtesting import uniswap_math as um
from backend.core.config import load_config
from backend.data_engine.abi import POOL_EVENTS

ROOT = Path(__file__).resolve().parent.parent
EPS = 2  # wei


def main() -> None:
    cfg = load_config()
    p = cfg.pools[cfg.primary_pool]
    db_path = cfg.lab["database"]["url"].removeprefix("sqlite:///")
    db = sqlite3.connect(f"file:{ROOT / db_path}?mode=ro", uri=True)
    addr = p.address.lower()
    name = {e.topic0: e.name for e in POOL_EVENTS}
    t0 = time.monotonic()

    def load(table: str, cols: str) -> dict:
        return {(r[0], r[1]): r[2:] for r in db.execute(
            f"select block_number, log_index, {cols} from {table} where lower(address) = ?", (addr,))}
    swaps = load("ev_swap", "amount0, amount1, sqrtPriceX96, liquidity, tick")
    mints = load("ev_mint", "tickLower, tickUpper, amount, amount0, amount1")
    burns = load("ev_burn", "tickLower, tickUpper, amount, amount0, amount1")
    inits = load("ev_initialize", "sqrtPriceX96, tick")
    order = db.execute("select block_number, log_index, topic0 from raw_logs where lower(address) = ? "
                       "order by block_number, log_index", (addr,)).fetchall()

    fee = p.fee
    res = {"tick": Counter(), "mint": Counter(), "burn": Counter(), "swap_step": Counter()}
    step_diffs: Counter = Counter()
    failures: list[dict] = []
    sqrt_p = tick = None
    boundaries: list[int] = []  # sorted sqrt prices of every tick ever used by a position
    prev_swap = None

    def add_boundary(t: int) -> None:
        sp = um.get_sqrt_ratio_at_tick(t)
        i = bisect_left(boundaries, sp)
        if i == len(boundaries) or boundaries[i] != sp:
            boundaries.insert(i, sp)

    def crosses(a: int, b: int) -> bool:
        lo, hi = min(a, b), max(a, b)
        i = bisect_left(boundaries, lo)
        return i < len(boundaries) and boundaries[i] <= hi

    for bn, li, topic0 in order:
        ev = name.get(topic0)
        key = (bn, li)
        if ev == "Initialize":
            sqrt_p, tick = int(inits[key][0]), inits[key][1]
        elif ev in ("Mint", "Burn"):
            lower, upper, amount, a0, a1 = (mints if ev == "Mint" else burns)[key]
            amount, a0, a1 = int(amount), int(a0), int(a1)
            add_boundary(lower), add_boundary(upper)
            prev_swap = None
            if amount == 0:  # poke (fee accrual only)
                res[ev.lower()]["poke"] += 1
                continue
            d0, d1 = um.mint_amounts(sqrt_p, tick, lower, upper, amount if ev == "Mint" else -amount)
            ok = (d0, d1) == ((a0, a1) if ev == "Mint" else (-a0, -a1))
            res[ev.lower()]["ok" if ok else "mismatch"] += 1
            if not ok and len(failures) < 20:
                failures.append({"check": ev, "block": bn, "log_index": li,
                                 "event": [a0, a1], "model": [d0, d1]})
        elif ev == "Swap":
            a0, a1, sp, liq, ev_tick = swaps[key]
            a0, a1, sp, liq = int(a0), int(a1), int(sp), int(liq)
            t_calc = um.get_tick_at_sqrt_ratio(sp)
            if ev_tick == t_calc:
                res["tick"]["ok"] += 1
            elif ev_tick == t_calc - 1 and sp == um.get_sqrt_ratio_at_tick(t_calc) and a0 > 0:
                res["tick"]["ok_boundary_rule"] += 1
            else:
                res["tick"]["mismatch"] += 1
                if len(failures) < 20:
                    failures.append({"check": "tick", "block": bn, "log_index": li,
                                     "event_tick": ev_tick, "model": t_calc})
            if (prev_swap is not None and prev_swap[1] == liq and liq > 0 and sqrt_p != sp
                    and not crosses(sqrt_p, sp)):
                st = um.compute_swap_step(sqrt_p, sp, liq, 1 << 250, fee)
                zero_for_one = sp < sqrt_p
                ein, eout = (a0, -a1) if zero_for_one else (a1, -a0)
                d_in, d_out = ein - (st.amount_in + st.fee_amount), eout - st.amount_out
                step_diffs[(d_in, d_out)] += 1
                ok = st.sqrt_ratio_next_x96 == sp and abs(d_in) <= EPS and abs(d_out) <= EPS
                res["swap_step"]["ok" if ok else "mismatch"] += 1
                res["swap_step"]["exact"] += d_in == 0 and d_out == 0
                if not ok and len(failures) < 20:
                    failures.append({"check": "swap_step", "block": bn, "log_index": li,
                                     "d_in": d_in, "d_out": d_out})
            sqrt_p, tick = sp, ev_tick
            prev_swap = (sp, liq)
    out = {
        "pool": cfg.primary_pool, "address": p.address, "events": len(order),
        "eps_wei": EPS, "results": {k: dict(v) for k, v in res.items()},
        "swap_step_diff_histogram": {f"{k[0]},{k[1]}": v for k, v in step_diffs.most_common(20)},
        "failures": failures, "seconds": round(time.monotonic() - t0, 1),
    }
    path = ROOT / "data" / "math_validation.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({k: out[k] for k in ("events", "results", "swap_step_diff_histogram")}, indent=1))
    print(f"failures={len(failures)}  written {path.relative_to(ROOT)}")
    if any(v.get("mismatch") for v in res.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
