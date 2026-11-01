"""Probe configured data sources and print an availability report.

Read-only: only eth_chainId / eth_blockNumber / eth_getLogs(1 block) are sent,
each checked by backend.core.safety. Run: python -m scripts.check_data_sources
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

from backend.core.config import load_config
from backend.core.safety import assert_read_only

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def rpc(url: str, method: str, params: list) -> object:
    assert_read_only(method, params)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
        payload = json.loads(r.read())
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["result"]


def main() -> None:
    cfg = load_config()
    pool = cfg.pools[cfg.primary_pool]
    for net_name, net in cfg.networks.items():
        for ep in net.rpc:
            url = ep.get("url") or os.environ.get(ep.get("url_env", ""), "")
            if not url:
                print(f"[{net_name}] {ep['name']}: SKIPPED (env {ep.get('url_env')} not set)")
                continue
            try:
                chain = int(rpc(url, "eth_chainId", []), 16)
                head = int(rpc(url, "eth_blockNumber", []), 16)
                logs = rpc(url, "eth_getLogs", [{
                    "address": pool.address, "topics": [SWAP_TOPIC],
                    "fromBlock": hex(head - 300), "toBlock": hex(head - 64)}])
                status = "OK" if chain == net.chain_id else f"WRONG CHAIN {chain}"
                print(f"[{net_name}] {ep['name']}: {status} head={head} swaps(~1h)={len(logs)}")
            except (urllib.error.URLError, RuntimeError, OSError, ValueError) as exc:
                print(f"[{net_name}] {ep['name']}: UNAVAILABLE ({exc})")


if __name__ == "__main__":
    main()
