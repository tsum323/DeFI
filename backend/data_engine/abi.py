"""Minimal ABI codec for the static types used by Uniswap v3 events and views.

Only static types are needed (address, bool, uintN, intN). Large integers are
kept as Python int (arbitrary precision) — never float.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_hash.auto import keccak

from backend.core.addresses import to_checksum


def selector(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()[:8]


def topic(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()


def _bits(typ: str) -> int:
    digits = typ.removeprefix("u").removeprefix("int")
    n = int(digits or 256)
    if n % 8 or not 8 <= n <= 256:
        raise ValueError(f"bad integer type {typ}")
    return n


def decode_word(typ: str, word: bytes) -> int | str | bool:
    if len(word) != 32:
        raise ValueError(f"ABI word must be 32 bytes, got {len(word)}")
    raw = int.from_bytes(word, "big")
    if typ == "address":
        if raw >> 160:
            raise ValueError("dirty high bits in address word")
        return to_checksum(word[12:].hex())
    if typ == "bool":
        if raw not in (0, 1):
            raise ValueError("invalid bool word")
        return bool(raw)
    if typ.startswith("uint"):
        n = _bits(typ)
        if raw >> n:
            raise ValueError(f"value overflows {typ}")
        return raw
    if typ.startswith("int"):
        n = _bits(typ)
        val = raw - (1 << 256) if raw >> 255 else raw
        if not -(1 << (n - 1)) <= val < (1 << (n - 1)):
            raise ValueError(f"value overflows {typ}")
        return val
    raise NotImplementedError(f"ABI type {typ} not supported")


def encode_word(typ: str, value) -> bytes:
    if typ == "address":
        return bytes.fromhex(value.lower().removeprefix("0x")).rjust(32, b"\0")
    if typ == "bool":
        return int(bool(value)).to_bytes(32, "big")
    if typ == "bytes32":
        raw = bytes.fromhex(value.removeprefix("0x"))
        if len(raw) != 32:
            raise ValueError("bytes32 must be 32 bytes")
        return raw
    if typ.startswith(("uint", "int")):
        return (value % (1 << 256)).to_bytes(32, "big")
    raise NotImplementedError(typ)


def decode_words(types: list[str], hexdata: str) -> list:
    data = bytes.fromhex(hexdata.removeprefix("0x"))
    if len(data) != 32 * len(types):
        raise ValueError(f"expected {32 * len(types)} bytes, got {len(data)}")
    return [decode_word(t, data[32 * i: 32 * i + 32]) for i, t in enumerate(types)]


def encode_call(signature: str, *args) -> str:
    """Encode a call whose arguments are all static types."""
    inner = signature[signature.index("(") + 1: -1]
    types = [t for t in inner.split(",") if t]
    if len(types) != len(args):
        raise ValueError("argument count mismatch")
    return selector(signature) + b"".join(encode_word(t, a) for t, a in zip(types, args)).hex()


@dataclass(frozen=True)
class EventSpec:
    name: str
    table: str
    fields: tuple[tuple[str, str, bool], ...]  # (name, type, indexed)

    @property
    def signature(self) -> str:
        return f"{self.name}({','.join(t for _, t, _ in self.fields)})"

    @property
    def topic0(self) -> str:
        return topic(self.signature)

    def decode(self, log: dict) -> dict:
        topics = log["topics"]
        if topics[0].lower() != self.topic0:
            raise ValueError(f"topic0 mismatch for {self.name}")
        indexed = [(n, t) for n, t, ix in self.fields if ix]
        plain = [(n, t) for n, t, ix in self.fields if not ix]
        if len(topics) != 1 + len(indexed):
            raise ValueError(f"{self.name}: expected {1 + len(indexed)} topics, got {len(topics)}")
        out: dict = {}
        for (n, t), tp in zip(indexed, topics[1:]):
            out[n] = decode_word(t, bytes.fromhex(tp.removeprefix("0x")))
        for (n, _), v in zip(plain, decode_words([t for _, t in plain], log["data"])):
            out[n] = v
        return out


def _ev(name: str, table: str, sig: str) -> EventSpec:
    """Parse 'type [indexed] name, ...' — copied verbatim from IUniswapV3PoolEvents.sol."""
    fields = []
    for part in sig.split(","):
        toks = part.split()
        fields.append((toks[-1], toks[0], "indexed" in toks))
    return EventSpec(name, table, tuple(fields))


# v3-core/contracts/interfaces/pool/IUniswapV3PoolEvents.sol
POOL_EVENTS: tuple[EventSpec, ...] = (
    _ev("Initialize", "ev_initialize", "uint160 sqrtPriceX96, int24 tick"),
    _ev("Mint", "ev_mint",
        "address sender, address indexed owner, int24 indexed tickLower, int24 indexed tickUpper,"
        " uint128 amount, uint256 amount0, uint256 amount1"),
    _ev("Collect", "ev_collect",
        "address indexed owner, address recipient, int24 indexed tickLower,"
        " int24 indexed tickUpper, uint128 amount0, uint128 amount1"),
    _ev("Burn", "ev_burn",
        "address indexed owner, int24 indexed tickLower, int24 indexed tickUpper,"
        " uint128 amount, uint256 amount0, uint256 amount1"),
    _ev("Swap", "ev_swap",
        "address indexed sender, address indexed recipient, int256 amount0, int256 amount1,"
        " uint160 sqrtPriceX96, uint128 liquidity, int24 tick"),
    _ev("Flash", "ev_flash",
        "address indexed sender, address indexed recipient, uint256 amount0, uint256 amount1,"
        " uint256 paid0, uint256 paid1"),
    _ev("IncreaseObservationCardinalityNext", "ev_observation_cardinality",
        "uint16 observationCardinalityNextOld, uint16 observationCardinalityNextNew"),
    _ev("SetFeeProtocol", "ev_set_fee_protocol",
        "uint8 feeProtocol0Old, uint8 feeProtocol1Old, uint8 feeProtocol0New, uint8 feeProtocol1New"),
    _ev("CollectProtocol", "ev_collect_protocol",
        "address indexed sender, address indexed recipient, uint128 amount0, uint128 amount1"),
)
POOL_EVENTS_BY_TOPIC = {e.topic0: e for e in POOL_EVENTS}

# v3-core/contracts/interfaces/IUniswapV3Factory.sol
POOL_CREATED = _ev("PoolCreated", "factory_pool_created",
                   "address indexed token0, address indexed token1, uint24 indexed fee,"
                   " int24 tickSpacing, address pool")
