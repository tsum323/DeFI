"""Config integrity: checksums, token ordering, and offline CREATE2 verification
of every configured pool address."""

import pytest

from backend.core.addresses import (
    compute_v3_pool_address,
    is_checksummed,
    sort_tokens,
    to_checksum,
)
from backend.core.config import load_config


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def test_eip55_reference_vectors():
    # Vectors from the EIP-55 specification.
    for addr in (
        "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed",
        "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
        "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB",
        "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb",
    ):
        assert to_checksum(addr.lower()) == addr
        assert is_checksummed(addr)


def test_research_only_mode(cfg):
    assert cfg.lab["mode"] == "research_only"


def test_token_order(cfg):
    t = cfg.tokens["ethereum"]
    assert sort_tokens(t["USDC"].address, t["WBTC"].address)[0] == t["WBTC"].address
    assert sort_tokens(t["USDC"].address, t["WETH"].address)[0] == t["USDC"].address


def test_pool_addresses_match_create2(cfg):
    for name, pool in cfg.pools.items():
        dep = cfg.networks[pool.network].uniswap_v3
        derived = compute_v3_pool_address(
            dep.factory,
            cfg.token(pool.network, pool.token0).address,
            cfg.token(pool.network, pool.token1).address,
            pool.fee,
            dep.pool_init_code_hash,
        )
        assert derived == pool.address, name


def test_known_mainnet_pool_independent_vector():
    # WETH/USDC 0.05% — the most widely referenced v3 pool; independent of our config.
    assert (
        compute_v3_pool_address(
            "0x1F98431c8aD98523631AE4a59f267346ea31F984",
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            500,
            "0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54",
        )
        == "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
    )
