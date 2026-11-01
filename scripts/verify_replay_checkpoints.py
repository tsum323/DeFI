"""Stage 4: compare the replayed pool state with the contract itself (eth_call).

Replays the primary pool's history and, at each checkpoint block B (state after
all events of B), reads from the pool contract at block B:
slot0 (sqrtPriceX96, tick, feeProtocol), liquidity, feeGrowthGlobal0/1X128,
protocolFees, ticks(i) for sampled initialized ticks, and positions(key) for
sampled real positions. Everything must match bit for bit.
Output: data/replay_checkpoints.json.

Run: python -m scripts.verify_replay_checkpoints [--checkpoints 8] [--endpoint drpc]
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from eth_hash.auto import keccak

from backend.backtesting.events import load_pool_events
from backend.backtesting.pool_replay import PoolReplay
from backend.core.config import load_config
from backend.data_engine.abi import decode_words, encode_call
from backend.data_engine.rpc import select_client

ROOT = Path(__file__).resolve().parent.parent


def position_key(owner: str, lower: int, upper: int) -> str:
    packed = bytes.fromhex(owner[2:]) + (lower & 0xFFFFFF).to_bytes(3, "big") \
        + (upper & 0xFFFFFF).to_bytes(3, "big")
    return "0x" + keccak(packed).hex()


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoints", type=int, default=8)
    ap.add_argument("--ticks", type=int, default=6, help="sampled ticks per checkpoint")
    ap.add_argument("--positions", type=int, default=6, help="sampled positions per checkpoint")
    ap.add_argument("--endpoint", default="drpc")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    p = cfg.pools[cfg.primary_pool]
    net = cfg.networks[p.network]
    eps = {ep["name"]: ep for ep in net.rpc}
    client = select_client(net.model_copy(update={"rpc": [eps[args.endpoint]]}),
                           cfg.lab["data_engine"])
    db = ROOT / cfg.lab["database"]["url"].removeprefix("sqlite:///")
    events = load_pool_events(db, p.address)
    first, last = events[0].block, events[-1].block
    rng = random.Random(args.seed)
    span = last - first
    wanted = sorted({first + span * (i + 1) // args.checkpoints for i in range(args.checkpoints)})
    event_blocks = sorted({e.block for e in events})
    # snap each checkpoint to a block that actually has events (state is then "after block B")
    import bisect
    checkpoints = [event_blocks[min(bisect.bisect_left(event_blocks, w), len(event_blocks) - 1)]
                   for w in wanted]
    checkpoints = sorted(set(checkpoints))

    def call(sig: str, out_types: list[str], block: int, *a) -> list:
        return decode_words(out_types, client.eth_call(p.address, encode_call(sig, *a), block))

    replay = PoolReplay(p.fee, p.tick_spacing)
    report = {"pool": cfg.primary_pool, "address": p.address, "endpoint": client.name,
              "checkpoints": [], "mismatches": 0}
    idx = 0
    t0 = time.monotonic()
    for cp in checkpoints:
        while idx < len(events) and events[idx].block <= cp:
            replay.apply(events[idx])
            idx += 1
        s = replay.s
        checks: list[dict] = []

        def check(name: str, model, chain) -> None:
            checks.append({"what": name, "ok": model == chain,
                           **({} if model == chain else {"model": str(model), "chain": str(chain)})})
        slot0 = call("slot0()", ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], cp)
        check("slot0.sqrtPriceX96", s.sqrt_price, slot0[0])
        check("slot0.tick", s.tick, slot0[1])
        check("slot0.feeProtocol", s.fee_protocol, slot0[5])
        check("liquidity", s.liquidity, call("liquidity()", ["uint128"], cp)[0])
        check("feeGrowthGlobal0X128", s.fee_growth_global0,
              call("feeGrowthGlobal0X128()", ["uint256"], cp)[0])
        check("feeGrowthGlobal1X128", s.fee_growth_global1,
              call("feeGrowthGlobal1X128()", ["uint256"], cp)[0])
        pf = call("protocolFees()", ["uint128", "uint128"], cp)
        check("protocolFees", (s.protocol_fees0, s.protocol_fees1), tuple(pf))
        init_ticks = sorted(t for t, i in s.ticks.items() if i.liquidity_gross > 0)
        near = sorted(init_ticks, key=lambda t: abs(t - s.tick))[:3]
        for t in sorted(set(near) | set(rng.sample(init_ticks, min(args.ticks, len(init_ticks))))):
            chain = call("ticks(int24)", ["uint128", "int128", "uint256", "uint256", "int56",
                                          "uint160", "uint32", "bool"], cp, t)
            i = s.ticks[t]
            check(f"ticks({t})", (i.liquidity_gross, i.liquidity_net, i.fee_growth_outside0_x128,
                                  i.fee_growth_outside1_x128, True), (*chain[:4], chain[7]))
        live = [k for k, v in s.positions.items()
                if v.liquidity > 0 or v.tokens_owed0 or v.tokens_owed1]
        for k in rng.sample(live, min(args.positions, len(live))):
            chain = call("positions(bytes32)", ["uint128", "uint256", "uint256", "uint128", "uint128"],
                         cp, position_key(*k))
            v = s.positions[k]
            check(f"positions{k}", (v.liquidity, v.fee_growth_inside0_last_x128,
                                    v.fee_growth_inside1_last_x128, v.tokens_owed0, v.tokens_owed1),
                  tuple(chain))
        bad = [c for c in checks if not c["ok"]]
        report["mismatches"] += len(bad)
        report["checkpoints"].append({"block": cp, "checks": len(checks), "mismatches": bad})
        print(f"block {cp}: {len(checks)} checks, {len(bad)} mismatches "
              f"({time.monotonic() - t0:.0f}s)", flush=True)
    out = ROOT / "data" / "replay_checkpoints.json"
    out.write_text(json.dumps(report, indent=1) + "\n")
    total = sum(c["checks"] for c in report["checkpoints"])
    print(f"total {total} checks, {report['mismatches']} mismatches -> {out.relative_to(ROOT)}")
    if report["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
