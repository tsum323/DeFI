"""Which free endpoint serves eth_getLogs how far back and in what window (read-only).

  python -m scripts.probe_logs     (run on GitHub Actions: Tools workflow)
"""

from __future__ import annotations

import json
import time

from backend.core.config import load_config
from backend.data_engine.rpc import EndpointUnavailable, RpcError, build_clients
from backend.lab.scanner import SWAP_TOPIC, scan_pools


def main() -> None:
    cfg = load_config()
    pools = scan_pools(cfg, cfg.lab["scanner"]["pools"])
    out = {}
    for net in ("base", "optimism", "arbitrum"):
        addrs = [p.address for p in pools if p.network == net]
        bt = cfg.networks[net].block_time_seconds
        clients, _ = build_clients(cfg.networks[net])
        for c in clients:
            c.max_retries, c.backoff = 1, (1,)
            res = {}
            try:
                head = c.block_number()
            except (EndpointUnavailable, RpcError) as exc:
                out[f"{net}/{c.name}"] = {"error": str(exc)[:150]}
                continue
            for depth_h in (1, 24, 24 * 8):
                for win in (100, 1000, 10_000, 50_000):
                    lo = head - int(depth_h * 3600 / bt)
                    t0 = time.monotonic()
                    try:
                        n = len(c.get_logs(addrs, lo, lo + win - 1, [SWAP_TOPIC]))
                        res[f"{depth_h}h/{win}"] = f"ok {n} logs {time.monotonic() - t0:.1f}s"
                    except (EndpointUnavailable, RpcError) as exc:
                        res[f"{depth_h}h/{win}"] = "ERR " + str(exc)[:90]
            out[f"{net}/{c.name}"] = res
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
