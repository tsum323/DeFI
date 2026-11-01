"""Daily ETH/USD history from the Chainlink feed (for converting gas to USD).

For each UTC day covered by the stored blocks, reads latestRoundData() at the
first stored block of that day. A backtest uses the latest sample at or before
its decision block (no look-ahead). Output: data/eth_usd_daily.json
([block, block_timestamp, answer_1e8, updated_at] rows). Resumable.

Run: python -m scripts.fetch_eth_usd [--endpoint drpc]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from backend.core.config import load_config
from backend.data_engine.abi import decode_words, encode_call
from backend.data_engine.rpc import select_client

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "eth_usd_daily.json"


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="drpc")
    args = ap.parse_args()
    p = cfg.pools[cfg.primary_pool]
    net = cfg.networks[p.network]
    feed = net.oracles["chainlink_eth_usd"]
    eps = {ep["name"]: ep for ep in net.rpc}
    client = select_client(net.model_copy(update={"rpc": [eps[args.endpoint]]}), cfg.lab["data_engine"])
    db = sqlite3.connect(f"file:{ROOT / cfg.lab['database']['url'].removeprefix('sqlite:///')}?mode=ro",
                         uri=True)
    days = db.execute("select min(number), min(timestamp) from blocks "
                      "group by timestamp / 86400 order by 1").fetchall()
    have = json.loads(OUT.read_text()) if OUT.exists() else {"feed": feed, "rows": []}
    done = {r[0] for r in have["rows"]}
    todo = [(b, ts) for b, ts in days if b not in done]
    print(f"{len(days)} days, {len(todo)} to fetch")
    t0 = time.monotonic()
    for i, (b, ts) in enumerate(todo, 1):
        r = decode_words(["uint80", "int256", "uint256", "uint256", "uint80"],
                         client.eth_call(feed, encode_call("latestRoundData()"), b))
        if r[1] <= 0 or r[3] > ts:
            raise SystemExit(f"bad oracle answer at block {b}: {r}")
        have["rows"].append([b, ts, r[1], r[3]])
        if i % 200 == 0:
            have["rows"].sort()
            OUT.write_text(json.dumps(have) + "\n")
            print(f"  {i}/{len(todo)} {time.monotonic() - t0:.0f}s", flush=True)
    have["rows"].sort()
    have["label"] = "EXACT oracle answers (Chainlink ETH/USD, 8 decimals) at the listed blocks"
    OUT.write_text(json.dumps(have) + "\n")
    print(f"written {OUT.relative_to(ROOT)}: {len(have['rows'])} rows")


if __name__ == "__main__":
    main()
