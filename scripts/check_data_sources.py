"""Probe configured data sources and print an availability report.

Read-only: only eth_chainId / eth_blockNumber / eth_getLogs are sent, each
checked by backend.core.safety. Endpoint URLs (which may contain API keys) are
never printed. Run: python -m scripts.check_data_sources
"""

from __future__ import annotations

import os

from backend.core.config import load_config
from backend.data_engine.abi import POOL_EVENTS
from backend.data_engine.rpc import (
    BadEndpointConfig, EndpointUnavailable, RangeTooLarge, RpcClient, RpcError,
)

SWAP_TOPIC = next(e.topic0 for e in POOL_EVENTS if e.name == "Swap")


def main() -> None:
    cfg = load_config()
    pool = cfg.pools[cfg.primary_pool]
    for net_name, net in cfg.networks.items():
        for ep in net.rpc:
            url = ep.get("url") or os.environ.get(ep.get("url_env", ""), "")
            tag = f"[{net_name}] {ep['name']}"
            if not url:
                print(f"{tag}: SKIPPED (env {ep.get('url_env')} not set)")
                continue
            try:
                c = RpcClient(ep["name"], url, max_retries=2, backoff=(2, 4))
                chain = c.chain_id()
                head = c.block_number()
                status = "OK" if chain == net.chain_id else f"WRONG CHAIN {chain}"
                try:
                    logs = c.get_logs(pool.address, head - 300, head - 64, [SWAP_TOPIC])
                    extra = f"swaps(~1h)={len(logs)}"
                except RangeTooLarge:
                    # Reachable, but the plan caps the eth_getLogs window.
                    c.get_logs(pool.address, head - 73, head - 64, [SWAP_TOPIC])
                    status = "LIMITED" if status == "OK" else status
                    extra = "eth_getLogs window capped by plan (10 blocks works)"
                print(f"{tag}: {status} head={head} {extra} "
                      f"archive(config)={ep.get('archive', False)}")
            except BadEndpointConfig as exc:
                print(f"{tag}: MISCONFIGURED ({exc})")
            except (EndpointUnavailable, RpcError) as exc:
                print(f"{tag}: UNAVAILABLE ({exc})")


if __name__ == "__main__":
    main()
