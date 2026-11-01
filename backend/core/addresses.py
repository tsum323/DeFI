"""Address utilities: EIP-55 checksums and Uniswap v3 CREATE2 pool derivation.

Used to verify configured pool addresses offline, independent of any RPC.
Reference: v3-periphery/contracts/libraries/PoolAddress.sol
"""

from __future__ import annotations

from eth_hash.auto import keccak


def _hex_body(address: str) -> str:
    body = address.lower().removeprefix("0x")
    if len(body) != 40 or any(c not in "0123456789abcdef" for c in body):
        raise ValueError(f"not a 20-byte hex address: {address!r}")
    return body


def to_checksum(address: str) -> str:
    """Return the EIP-55 mixed-case checksum form of an address."""
    body = _hex_body(address)
    digest = keccak(body.encode("ascii")).hex()
    return "0x" + "".join(
        c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c
        for i, c in enumerate(body)
    )


def is_checksummed(address: str) -> bool:
    return address == to_checksum(address)


def sort_tokens(a: str, b: str) -> tuple[str, str]:
    """Uniswap orders token0 < token1 by numeric address value."""
    ia, ib = int(_hex_body(a), 16), int(_hex_body(b), 16)
    if ia == ib:
        raise ValueError("identical token addresses")
    return (a, b) if ia < ib else (b, a)


def compute_v3_pool_address(
    factory: str, token_a: str, token_b: str, fee: int, init_code_hash: str
) -> str:
    """CREATE2 address: keccak256(0xff ++ factory ++ salt ++ init_code_hash)[12:],
    salt = keccak256(abi.encode(token0, token1, fee))."""
    token0, token1 = sort_tokens(token_a, token_b)
    salt = keccak(
        bytes.fromhex(_hex_body(token0)).rjust(32, b"\0")
        + bytes.fromhex(_hex_body(token1)).rjust(32, b"\0")
        + fee.to_bytes(32, "big")
    )
    raw = keccak(
        b"\xff"
        + bytes.fromhex(_hex_body(factory))
        + salt
        + bytes.fromhex(init_code_hash.removeprefix("0x"))
    )
    return to_checksum(raw[12:].hex())
