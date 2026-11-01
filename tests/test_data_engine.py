"""Stage 2 data engine: ABI decoding, topics, RPC client guards, sync engine.

The sync tests use a fake in-memory chain (SYNTHETIC data, never stored outside
the temporary test database). Decoder tests on recorded real logs live in
tests/fixtures/ once captured from RPC.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.core.safety import ReadOnlyViolation
from backend.data_engine import abi
from backend.data_engine.rpc import BadEndpointConfig, RangeTooLarge, RpcClient
from backend.data_engine.sync import IntegrityError, PoolSyncer, coverage_report
from backend.database import schema as S

POOL = "0x99ac8cA7087fA4A2A1FB6357269965A2014ABc35"
FIXTURES = Path(__file__).parent / "fixtures"

# keccak256 of the event signatures, as published in Uniswap v3 docs / Etherscan.
KNOWN_TOPICS = {
    "Swap": "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    "Mint": "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde",
    "Burn": "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",
    "Collect": "0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0",
    "Flash": "0xbdbdb71d7860376ba52b25a5028beea23581364a40522f6bcfb86bb1f2dca633",
    "Initialize": "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95",
}


def test_event_topics_match_known_values():
    by_name = {e.name: e for e in abi.POOL_EVENTS}
    for name, t in KNOWN_TOPICS.items():
        assert by_name[name].topic0 == t, name
    assert len(abi.POOL_EVENTS_BY_TOPIC) == len(abi.POOL_EVENTS) == 9


def test_selectors():
    assert abi.selector("getPool(address,address,uint24)") == "0x1698ee82"
    assert abi.selector("slot0()") == "0x3850c7bd"
    assert abi.selector("balanceOf(address)") == "0x70a08231"


def test_signed_and_unsigned_words():
    neg = (-5 % (1 << 256)).to_bytes(32, "big")
    assert abi.decode_word("int24", neg) == -5
    assert abi.decode_word("int256", neg) == -5
    with pytest.raises(ValueError):
        abi.decode_word("uint8", (256).to_bytes(32, "big"))
    with pytest.raises(ValueError):
        abi.decode_word("int24", (1 << 23).to_bytes(32, "big"))  # +2^23 overflows int24
    big = (1 << 160) - 1
    assert abi.decode_word("uint160", big.to_bytes(32, "big")) == big


def _w(typ, v):
    return abi.encode_word(typ, v).hex()


def make_swap_log(block, idx, amount0=-1000, amount1=60_000_000, tick=-5, bh=None):
    spec = next(e for e in abi.POOL_EVENTS if e.name == "Swap")
    sender = "0x" + "11" * 20
    return {
        "address": POOL, "blockNumber": hex(block), "logIndex": hex(idx),
        "blockHash": bh or f"0x{block:064x}", "transactionHash": f"0x{block * 1000 + idx:064x}",
        "transactionIndex": hex(idx), "removed": False,
        "topics": [spec.topic0, "0x" + _w("address", sender), "0x" + _w("address", sender)],
        "data": "0x" + _w("int256", amount0) + _w("int256", amount1)
                + _w("uint160", 2 ** 96 * 3) + _w("uint128", 10 ** 20) + _w("int24", tick),
    }


def make_mint_log(block, idx):
    spec = next(e for e in abi.POOL_EVENTS if e.name == "Mint")
    owner = "0x" + "22" * 20
    return {
        "address": POOL, "blockNumber": hex(block), "logIndex": hex(idx),
        "blockHash": f"0x{block:064x}", "transactionHash": f"0x{block * 1000 + idx:064x}",
        "transactionIndex": hex(idx), "removed": False,
        "topics": [spec.topic0, "0x" + _w("address", owner), "0x" + _w("int24", -60),
                   "0x" + _w("int24", 120)],
        "data": "0x" + _w("address", owner) + _w("uint128", 2 ** 100)
                + _w("uint256", 123) + _w("uint256", 456),
    }


def test_decode_swap_and_mint():
    sw = next(e for e in abi.POOL_EVENTS if e.name == "Swap").decode(make_swap_log(10, 0))
    assert sw["amount0"] == -1000 and sw["amount1"] == 60_000_000 and sw["tick"] == -5
    assert sw["sqrtPriceX96"] == 2 ** 96 * 3
    mt = next(e for e in abi.POOL_EVENTS if e.name == "Mint").decode(make_mint_log(10, 1))
    assert (mt["tickLower"], mt["tickUpper"], mt["amount"]) == (-60, 120, 2 ** 100)


def test_rpc_client_rejects_write_methods_before_network():
    c = RpcClient("t", "https://example.invalid/")
    with pytest.raises(ReadOnlyViolation):
        c.call("eth_sendRawTransaction", ["0x00"])
    with pytest.raises(ReadOnlyViolation):
        c.batch([("eth_blockNumber", []), ("eth_sign", [])])


def test_placeholder_url_detected_without_leaking():
    with pytest.raises(BadEndpointConfig) as ei:
        RpcClient("alchemy", "https://eth-mainnet.g.alchemy.com/v2/ВАШ_КЛЮЧ")
    assert "alchemy.com" not in str(ei.value) and "КЛЮЧ" not in str(ei.value)
    assert "SECRET" not in repr(RpcClient("x", "https://h/v2/SECRET"))


class FakeChain:
    """In-memory node: logs by block, optional max range, optional bad hash."""

    def __init__(self, logs, max_range=None):
        self.logs, self.max_range, self.name, self.requests_sent = logs, max_range, "fake", 0
        self.bad_hash_block = None

    def get_logs(self, address, lo, hi, topics=None):
        self.requests_sent += 1
        if self.max_range and hi - lo + 1 > self.max_range:
            raise RangeTooLarge("fake", "eth_getLogs", {"message": "block range too large"})
        return [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi]

    def batch(self, calls):
        out = []
        for _, (num, _) in calls:
            n = int(num, 16)
            h = f"0x{n:064x}" if n != self.bad_hash_block else "0x" + "ee" * 32
            out.append({"number": num, "hash": h, "timestamp": hex(1_700_000_000 + n * 12),
                        "baseFeePerGas": hex(10 ** 9)})
        return out


@pytest.fixture
def engine():
    return S.make_engine("sqlite:///:memory:")


def _logs():
    return [make_mint_log(100, 0), make_swap_log(100, 3), make_swap_log(250, 1),
            make_swap_log(999, 7), make_swap_log(1500, 2)]


def test_sync_full_idempotent_and_resumable(engine):
    chain = FakeChain(_logs(), max_range=300)
    s = PoolSyncer(chain, engine, 1, POOL, chunk_blocks=1000, min_chunk_blocks=50,
                   max_chunk_blocks=1000)
    s.run(100, 1000)
    assert s.last_block() == 1000 and s.stats.shrinks >= 1
    rep = coverage_report(engine, 1, POOL)
    assert rep["events"]["ev_swap"] == 3 and rep["events"]["ev_mint"] == 1
    # resume continues from 1001; re-running is a no-op for existing rows
    s.run(100, 2000)
    rep = coverage_report(engine, 1, POOL)
    assert rep["last_block"] == 2000 and rep["events"]["ev_swap"] == 4
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(S.raw_logs)).scalar_one() == 5
        amt = c.execute(select(S.event_tables["ev_mint"].c.amount)).scalar_one()
        assert amt == str(2 ** 100)  # exact big int preserved
        assert c.execute(select(func.count()).select_from(S.blocks)).scalar_one() == 4


def test_sync_reingest_same_window_no_duplicates(engine):
    chain = FakeChain(_logs())
    PoolSyncer(chain, engine, 1, POOL).run(100, 2000)
    # fresh syncer with wiped sync_state re-reads everything
    with engine.begin() as c:
        c.execute(S.sync_state.delete())
    PoolSyncer(chain, engine, 1, POOL).run(100, 2000)
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(S.event_tables["ev_swap"])).scalar_one() == 4


def test_block_hash_mismatch_stops_and_records_gap(engine):
    chain = FakeChain(_logs())
    chain.bad_hash_block = 999
    s = PoolSyncer(chain, engine, 1, POOL, chunk_blocks=200, max_chunk_blocks=200)
    with pytest.raises(IntegrityError):
        s.run(100, 2000)
    assert s.last_block() < 999  # coverage never skips the bad window
    rep = coverage_report(engine, 1, POOL)
    assert rep["open_gaps"] == 1
    chain.bad_hash_block = None  # node fixed -> resume closes the gap
    s.run(100, 2000)
    assert coverage_report(engine, 1, POOL)["open_gaps"] == 0


def test_min_chunk_exhausted_records_gap(engine):
    chain = FakeChain(_logs(), max_range=10)
    s = PoolSyncer(chain, engine, 1, POOL, chunk_blocks=400, min_chunk_blocks=50)
    with pytest.raises(RangeTooLarge):
        s.run(100, 2000)
    assert coverage_report(engine, 1, POOL)["open_gaps"] == 1


@pytest.mark.skipif(not (FIXTURES / "real_pool_logs.json").exists(),
                    reason="no recorded real logs yet")
def test_decode_recorded_real_logs():
    data = json.loads((FIXTURES / "real_pool_logs.json").read_text())
    for lg in data["logs"]:
        spec = abi.POOL_EVENTS_BY_TOPIC[lg["topics"][0].lower()]
        ev = spec.decode(lg)
        if spec.name == "Swap":
            assert -887272 <= ev["tick"] <= 887272
            assert (ev["amount0"] > 0) != (ev["amount1"] > 0)  # one side in, one side out


def _http_error(code: int, body: bytes):
    import io
    import urllib.error
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def test_http_4xx_json_range_error_becomes_range_too_large(monkeypatch):
    # SYNTHETIC: replay of real Alchemy / dRPC free-tier HTTP 400 bodies.
    import urllib.request
    bodies = [
        b'{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"Under the Free tier '
        b'plan, you can make eth_getLogs requests with up to a 10 block range."}}',
        b'{"id":1,"jsonrpc":"2.0","error":{"message":"ranges over 10000 blocks are not '
        b'supported on free plan","code":35}}',
    ]
    for body in bodies:
        def fake(*_a, _b=body, **_k):
            raise _http_error(400, _b)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        c = RpcClient("t", "https://example.invalid/rpc", max_retries=0)
        with pytest.raises(RangeTooLarge):
            c.get_logs("0x" + "11" * 20, 1, 100, [])


def test_rpc_sends_explicit_user_agent(monkeypatch):
    # Cloudflare-fronted public RPCs answer 403/1010 to the default Python-urllib agent.
    import urllib.request
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'

    def fake(req, *a, **k):
        seen["ua"] = req.get_header("User-agent")
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    RpcClient("t", "https://example.invalid/rpc", max_retries=0).chain_id()
    assert seen["ua"] and "python-urllib" not in seen["ua"].lower()


def test_batch_splits_on_persistent_http_429(monkeypatch):
    # SYNTHETIC: endpoint rejects batches larger than 2 with HTTP 429.
    import urllib.request

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    sizes = []

    def fake(req, *a, **k):
        payload = json.loads(req.data)
        sizes.append(len(payload))
        if len(payload) > 2:
            raise _http_error(429, b"Too Many Requests")
        return Resp(json.dumps([{"jsonrpc": "2.0", "id": x["id"], "result": hex(x["id"])}
                                for x in payload]).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    c = RpcClient("t", "https://example.invalid/rpc", max_retries=0)
    out = c.batch([("eth_blockNumber", [])] * 8)
    assert len(out) == 8 and sizes == [8, 4, 2, 2, 4, 2, 2]


class FakeChainLogMeta(FakeChain):
    """SYNTHETIC: node whose logs carry blockTimestamp and which serves eth_feeHistory."""

    def __init__(self, logs, **kw):
        super().__init__(logs, **kw)
        for lg in self.logs:
            lg["blockTimestamp"] = hex(1_700_000_000 + int(lg["blockNumber"], 16) * 12)
        self.header_calls = 0
        self.fee_calls = 0

    def batch(self, calls):
        self.header_calls += len(calls)
        return super().batch(calls)

    def call(self, method, params):
        assert method == "eth_feeHistory"
        self.fee_calls += 1
        count, newest = int(params[0], 16), int(params[1], 16)
        return {"oldestBlock": hex(newest - count + 1),
                "baseFeePerGas": [hex(10 ** 9)] * (count + 1)}


def test_sync_block_meta_from_logs_uses_few_requests(engine, monkeypatch):
    import backend.data_engine.sync as sync_mod
    monkeypatch.setattr(sync_mod, "LONDON_BLOCK", 0)
    chain = FakeChainLogMeta(_logs())
    s = PoolSyncer(chain, engine, 1, POOL, chunk_blocks=2000, max_chunk_blocks=2000)
    s.run(100, 2000)
    assert coverage_report(engine, 1, POOL)["events"]["ev_swap"] == 4
    assert chain.header_calls == 1 and chain.fee_calls <= 2  # 1 probe header, not 1 per block
    with engine.connect() as c:
        rows = c.execute(select(S.blocks.c.number, S.blocks.c.timestamp,
                                S.blocks.c.base_fee_per_gas)).all()
    assert {r.number for r in rows} == {100, 250, 999, 1500}
    assert all(r.timestamp == 1_700_000_000 + r.number * 12 and r.base_fee_per_gas == str(10 ** 9)
               for r in rows)


def test_sync_block_meta_probe_detects_bad_hash(engine, monkeypatch):
    import backend.data_engine.sync as sync_mod
    monkeypatch.setattr(sync_mod, "LONDON_BLOCK", 0)
    chain = FakeChainLogMeta(_logs())
    chain.bad_hash_block = 1500  # last block of the window is the probe
    s = PoolSyncer(chain, engine, 1, POOL, chunk_blocks=2000, max_chunk_blocks=2000)
    with pytest.raises(IntegrityError):
        s.run(100, 2000)
    assert coverage_report(engine, 1, POOL)["open_gaps"] == 1


def test_snapshot_roundtrip_is_exact(engine, tmp_path):
    # SYNTHETIC chain; checks export -> restore reproduces raw + decoded tables.
    from backend.data_engine.snapshot import SnapshotError, export_pool, restore_pool
    chain = FakeChain(_logs())
    PoolSyncer(chain, engine, 1, POOL).run(100, 2000)
    m = export_pool(engine, 1, POOL, tmp_path / "snap", log_rows_per_part=2, block_rows_per_part=3)
    assert m["counts"] == {"blocks": 4, "raw_logs": 5} and len(m["files"]) == 2 + 3
    other = S.make_engine("sqlite:///:memory:")
    res = restore_pool(other, tmp_path / "snap")
    assert res["raw_logs"] == 5 and coverage_report(other, 1, POOL)["last_block"] == 2000

    def dump(e, t):
        with e.connect() as c:
            return sorted(tuple(r) for r in c.execute(select(t)))
    for t in [S.blocks, S.raw_logs, *S.event_tables.values()]:
        assert dump(engine, t) == dump(other, t), t.name
    restore_pool(other, tmp_path / "snap")  # idempotent
    assert dump(engine, S.raw_logs) == dump(other, S.raw_logs)
    part = tmp_path / "snap" / m["files"][0]["name"]
    part.write_bytes(part.read_bytes()[:-1] + b"\x00")
    with pytest.raises(SnapshotError):
        restore_pool(other, tmp_path / "snap")
