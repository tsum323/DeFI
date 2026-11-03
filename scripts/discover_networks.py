"""Check L2 networks for UNISWAP AI LAB (read-only): RPC endpoints, token contracts,
Uniswap v3 pools via the factory (compared with the CREATE2 derivation), recent activity.

  python -m scripts.discover_networks > data_snapshots/networks/discovery.json

Run where the endpoints are reachable (GitHub Actions: .github/workflows/tools.yml).
Every call goes through the read-only guard of RpcClient.
"""

from __future__ import annotations

import json
import sys
import time

from backend.core.addresses import compute_v3_pool_address, to_checksum
from backend.data_engine.abi import encode_call
from backend.data_engine.rpc import EndpointUnavailable, RpcClient, RpcError

INIT_HASH = "0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54"
SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
NETS = {
    "base": {"chain_id": 8453, "block_time": 2, "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
             "rpc": ["https://base-rpc.publicnode.com", "https://base.drpc.org", "https://mainnet.base.org"],
             "tokens": {"WETH": "0x4200000000000000000000000000000000000006",
                        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
                        "WBTC": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c"}},
    "arbitrum": {"chain_id": 42161, "block_time": 0.25, "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                 "rpc": ["https://arbitrum-one-rpc.publicnode.com", "https://arbitrum.drpc.org", "https://arb1.arbitrum.io/rpc"],
                 "tokens": {"WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
                            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                            "USDC.e": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
                            "WBTC": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"}},
    "optimism": {"chain_id": 10, "block_time": 2, "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
                 "rpc": ["https://optimism-rpc.publicnode.com", "https://optimism.drpc.org", "https://mainnet.optimism.io"],
                 "tokens": {"WETH": "0x4200000000000000000000000000000000000006",
                            "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
                            "USDC.e": "0x7F5c764cBc14f9669B88837ca1490cCa17c31607",
                            "WBTC": "0x68f180fcCe6836688e9084f035309E29Bf0A2095"}},
}
PAIRS = [("WETH", "USDC"), ("WBTC", "USDC"), ("cbBTC", "USDC"), ("WBTC", "WETH"), ("cbBTC", "WETH"), ("WETH", "USDC.e")]
FEES = (100, 500, 3000, 10000)


def _str(hexdata: str) -> str:
    raw = bytes.fromhex(hexdata[2:])
    if len(raw) >= 64:
        n = int.from_bytes(raw[32:64], "big")
        return raw[64:64 + n].decode(errors="replace")
    return raw.rstrip(b"\0").decode(errors="replace")


def client_for(net: dict, log) -> tuple[RpcClient | None, list]:
    notes = []
    for url in net["rpc"]:
        try:
            c = RpcClient(name=url.split("//")[1].split("/")[0], url=url, max_retries=2, backoff=(2, 4))
            cid = c.chain_id()
            if cid == net["chain_id"]:
                return c, notes
            notes.append(f"{url}: chain {cid}")
        except (EndpointUnavailable, RpcError, OSError) as exc:
            notes.append(f"{url}: {str(exc)[:120]}")
    return None, notes


def logs_count(c: RpcClient, pool: str, lo: int, hi: int, step: int) -> tuple[int, int]:
    """(swaps, largest accepted window) over [lo, hi]."""
    n, b = 0, lo
    while b <= hi:
        e = min(hi, b + step - 1)
        try:
            n += len(c.get_logs(pool, b, e, [SWAP]))
            b = e + 1
        except (EndpointUnavailable, RpcError):
            if step <= 50:
                raise
            step //= 4
    return n, step


def main() -> None:
    out = {"checked_at": int(time.time()), "networks": {}}
    for name, net in NETS.items():
        rec: dict = {"chain_id": net["chain_id"], "tokens": {}, "pools": []}
        c, notes = client_for(net, print)
        rec["rpc_notes"] = notes
        if c is None:
            rec["error"] = "no endpoint answered"
            out["networks"][name] = rec
            continue
        rec["rpc"] = c.url
        head = c.block_number()
        rec["head"] = head
        for sym, addr in net["tokens"].items():
            try:
                rec["tokens"][sym] = {"address": to_checksum(addr),
                                      "symbol": _str(c.eth_call(addr, encode_call("symbol()"))),
                                      "decimals": int(c.eth_call(addr, encode_call("decimals()")), 16)}
            except (EndpointUnavailable, RpcError, ValueError) as exc:
                rec["tokens"][sym] = {"address": addr, "error": str(exc)[:120]}
        window = int(2 * 3600 / net["block_time"])  # last ~2 hours
        for a, b in PAIRS:
            ta, tb = rec["tokens"].get(a), rec["tokens"].get(b)
            if not ta or not tb or "error" in ta or "error" in tb:
                continue
            t0, t1 = sorted([ta["address"], tb["address"]], key=lambda x: int(x, 16))
            for fee in FEES:
                data = encode_call("getPool(address,address,uint24)", t0, t1, fee)
                pool = "0x" + c.eth_call(net["factory"], data)[-40:]
                if int(pool, 16) == 0:
                    continue
                pool = to_checksum(pool)
                derived = compute_v3_pool_address(net["factory"], t0, t1, fee, INIT_HASH)
                liq = int(c.eth_call(pool, encode_call("liquidity()")), 16)
                bal0 = int(c.eth_call(t0, encode_call("balanceOf(address)", pool)), 16)
                bal1 = int(c.eth_call(t1, encode_call("balanceOf(address)", pool)), 16)
                try:
                    swaps, step = logs_count(c, pool, head - window, head, 10_000)
                except (EndpointUnavailable, RpcError) as exc:
                    swaps, step = None, str(exc)[:80]
                sym0 = a if ta["address"] == t0 else b
                sym1 = b if sym0 == a else a
                d0 = rec["tokens"][sym0]["decimals"]
                d1 = rec["tokens"][sym1]["decimals"]
                rec["pools"].append({"pair": f"{a}/{b}", "fee": fee, "address": pool, "create2_match": derived == pool,
                                     "token0": sym0, "token1": sym1, "liquidity": liq,
                                     "balance0": bal0 / 10 ** d0, "balance1": bal1 / 10 ** d1,
                                     "swaps_2h": swaps, "logs_window": step})
                print(f"{name} {a}/{b} {fee / 1e4:.2f}% {pool} swaps/2h={swaps} bal0={bal0 / 10 ** d0:,.2f} "
                      f"bal1={bal1 / 10 ** d1:,.2f} create2={derived == pool}", file=sys.stderr, flush=True)
        out["networks"][name] = rec
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
