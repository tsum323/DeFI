"""Stage 4: validate FeeMeter (model B) on real positions of the primary pool.

For a seeded sample of real positions, a FeeMeter is opened right after the
Mint that creates the position (liquidity 0 -> L) and closed at the position's
next liquidity change (Mint/Burn/poke) or at the end of history. At close, the
meter's per-unit growth must equal the contract's feeGrowthInside delta
(Tick.getFeeGrowthInside now minus feeGrowthInsideLast at open), i.e. the
fees credited to that real position. Output: data/fee_meter_validation.json.

Run: python -m scripts.validate_fee_meter [--sample 400]
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from backend.backtesting import uniswap_math as um
from backend.backtesting.events import load_pool_events
from backend.backtesting.fee_meter import FeeMeter
from backend.backtesting.pool_replay import PoolReplay
from backend.core.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    p = cfg.pools[cfg.primary_pool]
    events = load_pool_events(ROOT / cfg.lab["database"]["url"].removeprefix("sqlite:///"), p.address)
    creating = [e for e in events if e.name == "Mint"]
    rng = random.Random(args.seed)
    chosen = {(e.block, e.log_index) for e in rng.sample(creating, min(args.sample * 2, len(creating)))}

    replay = PoolReplay(p.fee, p.tick_spacing)
    meters: dict[tuple, tuple[FeeMeter, int, int, int]] = {}  # key -> (meter, in0, in1, open_block)
    results = {"compared": 0, "exact": 0, "mismatch": [], "fees_credited_positive": 0}

    def close(key, now_block):
        meter, in0, in1, opened = meters.pop(key)
        s = replay.s
        g0, g1 = um.get_fee_growth_inside(s.ticks, key[1], key[2], s.tick,
                                          s.fee_growth_global0, s.fee_growth_global1)
        want = ((g0 - in0) & um.MAX_UINT256, (g1 - in1) & um.MAX_UINT256)
        got = (meter.growth_b0, meter.growth_b1)
        results["compared"] += 1
        results["fees_credited_positive"] += any(want)
        if got == want:
            results["exact"] += 1
        elif len(results["mismatch"]) < 20:
            results["mismatch"].append({"position": list(key), "opened": opened, "closed": now_block,
                                        "contract": [str(x) for x in want],
                                        "meter": [str(x) for x in got]})
        replay.swap_listeners.remove(meter.on_swap)
        replay.flash_listeners.remove(meter.on_flash)

    t0 = time.monotonic()
    for ev in events:
        key = (ev.f.get("owner"), ev.f.get("tickLower"), ev.f.get("tickUpper"))
        if ev.name in ("Mint", "Burn") and key in meters:
            close(key, ev.block)
        before = replay.s.positions.get(key)
        was_empty = before is None or before.liquidity == 0
        replay.apply(ev)
        if (ev.name == "Mint" and was_empty and (ev.block, ev.log_index) in chosen
                and len(meters) + results["compared"] < args.sample):
            pos = replay.s.positions[key]
            m = FeeMeter(key[1], key[2], pos.liquidity)
            meters[key] = (m, pos.fee_growth_inside0_last_x128, pos.fee_growth_inside1_last_x128,
                           ev.block)
            replay.swap_listeners.append(m.on_swap)
            replay.flash_listeners.append(m.on_flash)
    for key in list(meters):
        close(key, replay.s.block)
    results["seconds"] = round(time.monotonic() - t0, 1)
    out = ROOT / "data" / "fee_meter_validation.json"
    out.write_text(json.dumps(results, indent=1) + "\n")
    print({k: v for k, v in results.items() if k != "mismatch"}, "mismatches:", len(results["mismatch"]))
    if results["mismatch"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
