"""Typed loading and validation of config/*.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

from backend.core.addresses import is_checksummed

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# Fee tier -> tick spacing as enabled in UniswapV3Factory (100 added by governance).
V3_TICK_SPACING = {100: 1, 500: 10, 3000: 60, 10000: 200}


class Token(BaseModel):
    address: str
    decimals: int
    wrapped_asset: str | None = None
    risk_notes: list[str] = []

    @model_validator(mode="after")
    def _check(self) -> "Token":
        if not is_checksummed(self.address):
            raise ValueError(f"token address not EIP-55 checksummed: {self.address}")
        return self


class Pool(BaseModel):
    network: str
    protocol: Literal["uniswap_v3"]
    token0: str
    token1: str
    fee: int
    tick_spacing: int
    address: str
    status: Literal["derived_create2", "verified_onchain"]
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "Pool":
        if V3_TICK_SPACING.get(self.fee) != self.tick_spacing:
            raise ValueError(f"fee {self.fee} does not match tick spacing {self.tick_spacing}")
        if not is_checksummed(self.address):
            raise ValueError(f"pool address not EIP-55 checksummed: {self.address}")
        return self


class UniswapV3Deployment(BaseModel):
    factory: str
    nonfungible_position_manager: str
    pool_init_code_hash: str
    factory_deploy_block: int


class Network(BaseModel):
    chain_id: int
    block_time_seconds: float
    native_symbol: str
    rpc: list[dict]
    indexers: dict[str, dict]
    oracles: dict[str, str] = {}
    uniswap_v3: UniswapV3Deployment
    rollup: str | None = None      # op_stack | arbitrum (L2 data fee) | None (Ethereum)


class LabConfig(BaseModel):
    networks: dict[str, Network]
    tokens: dict[str, dict[str, Token]]
    pools: dict[str, Pool]
    primary_pool: str
    lab: dict

    @model_validator(mode="after")
    def _cross_check(self) -> "LabConfig":
        if self.lab.get("mode") != "research_only":
            raise ValueError("lab.mode must be 'research_only'")
        if self.primary_pool not in self.pools:
            raise ValueError(f"primary_pool {self.primary_pool!r} not defined")
        for name, p in self.pools.items():
            if p.network not in self.networks:
                raise ValueError(f"pool {name}: unknown network {p.network}")
            toks = self.tokens.get(p.network, {})
            for sym in (p.token0, p.token1):
                if sym not in toks:
                    raise ValueError(f"pool {name}: unknown token {sym}")
            a0 = int(toks[p.token0].address, 16)
            a1 = int(toks[p.token1].address, 16)
            if not a0 < a1:
                raise ValueError(f"pool {name}: token0 must have the lower address")
        return self

    def token(self, network: str, symbol: str) -> Token:
        return self.tokens[network][symbol]


def _read(name: str, config_dir: Path) -> dict:
    with open(config_dir / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_config(config_dir: Path = CONFIG_DIR) -> LabConfig:
    nets = _read("networks.yaml", config_dir)
    pools = _read("pools.yaml", config_dir)
    lab = _read("lab.yaml", config_dir)
    return LabConfig(
        networks=nets["networks"],
        tokens=pools["tokens"],
        pools=pools["pools"],
        primary_pool=pools["research"]["primary_pool"],
        lab=lab,
    )
