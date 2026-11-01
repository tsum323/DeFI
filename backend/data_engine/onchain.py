"""On-chain verification of configured pools and discovery of their creation block."""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.addresses import to_checksum
from backend.data_engine.abi import POOL_CREATED, decode_words, encode_call
from backend.data_engine.rpc import RangeTooLarge, RpcClient

ZERO = "0x" + "0" * 40


def _call(c: RpcClient, to: str, sig: str, out: list[str], *args, block="latest"):
    return decode_words(out, c.eth_call(to, encode_call(sig, *args), block))


def get_pool(c: RpcClient, factory: str, token_a: str, token_b: str, fee: int,
             block="latest") -> str | None:
    (addr,) = _call(c, factory, "getPool(address,address,uint24)", ["address"],
                    token_a, token_b, fee, block=block)
    return None if addr == to_checksum(ZERO) else addr


@dataclass
class PoolOnchain:
    address: str
    token0: str
    token1: str
    fee: int
    tick_spacing: int
    sqrt_price_x96: int
    tick: int
    fee_protocol: int
    liquidity: int
    balance0: int
    balance1: int
    block: int


def read_pool(c: RpcClient, pool: str, block: int) -> PoolOnchain:
    (t0,) = _call(c, pool, "token0()", ["address"], block=block)
    (t1,) = _call(c, pool, "token1()", ["address"], block=block)
    (fee,) = _call(c, pool, "fee()", ["uint24"], block=block)
    (ts,) = _call(c, pool, "tickSpacing()", ["int24"], block=block)
    slot0 = _call(c, pool, "slot0()",
                  ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"], block=block)
    (liq,) = _call(c, pool, "liquidity()", ["uint128"], block=block)
    (b0,) = _call(c, t0, "balanceOf(address)", ["uint256"], pool, block=block)
    (b1,) = _call(c, t1, "balanceOf(address)", ["uint256"], pool, block=block)
    return PoolOnchain(pool, t0, t1, fee, ts, slot0[0], slot0[1], slot0[5], liq, b0, b1, block)


def has_code(c: RpcClient, address: str, block: int) -> bool:
    return c.call("eth_getCode", [address, hex(block)]) not in ("0x", "0x0", "")


@dataclass
class PoolCreation:
    block: int
    tx_hash: str
    log_index: int
    tick_spacing: int
    method: str  # 'PoolCreated' | 'getCode-bisection'


def find_pool_creation(c: RpcClient, factory: str, token0: str, token1: str, fee: int,
                       pool: str, from_block: int, to_block: int) -> PoolCreation:
    """Locate PoolCreated for (token0, token1, fee). Topic filters make the result
    tiny, so a full-range query usually works; on range errors, bisect with
    eth_getCode (needs an archive node) to a small window, then query logs."""
    topics = [POOL_CREATED.topic0,
              "0x" + token0.lower().removeprefix("0x").rjust(64, "0"),
              "0x" + token1.lower().removeprefix("0x").rjust(64, "0"),
              "0x" + hex(fee)[2:].rjust(64, "0")]
    lo, hi, method = from_block, to_block, "PoolCreated"
    try:
        logs = c.get_logs(factory, lo, hi, topics)
    except RangeTooLarge:
        method = "getCode-bisection"
        while hi - lo > 2000:
            mid = (lo + hi) // 2
            if has_code(c, pool, mid):
                hi = mid
            else:
                lo = mid + 1
        logs = c.get_logs(factory, max(from_block, lo - 1), min(to_block, hi), topics)
    for lg in logs:
        ev = POOL_CREATED.decode(lg)
        if ev["pool"] == pool:
            return PoolCreation(int(lg["blockNumber"], 16), lg["transactionHash"],
                                int(lg["logIndex"], 16), ev["tickSpacing"], method)
    raise LookupError(f"PoolCreated for {pool} not found in [{from_block}, {to_block}]")
