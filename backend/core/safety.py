"""Read-only enforcement.

Every JSON-RPC request in the project MUST pass through `assert_read_only`.
The allowlist is closed: anything not explicitly listed is rejected, so new
write-capable methods (eth_sendRawTransaction, eth_sign*, wallet_*, personal_*,
eth_sendUserOperation, ...) can never slip through.

The project also holds no signing material: tests/test_safety.py scans the
source tree for private keys, mnemonics and transaction-sending calls.
"""

from __future__ import annotations

READ_ONLY_RPC_METHODS: frozenset[str] = frozenset(
    {
        "eth_chainId",
        "net_version",
        "web3_clientVersion",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getLogs",
        "eth_call",
        "eth_getCode",
        "eth_getStorageAt",
        "eth_getBalance",
        "eth_getTransactionByHash",
        "eth_getTransactionReceipt",
        "eth_getBlockReceipts",
        "eth_gasPrice",
        "eth_feeHistory",
        "eth_maxPriorityFeePerGas",
        "eth_syncing",
    }
)


class ReadOnlyViolation(PermissionError):
    """Raised when code attempts any non-read blockchain operation."""


def assert_read_only(method: str, params: list | None = None) -> None:
    if method not in READ_ONLY_RPC_METHODS:
        raise ReadOnlyViolation(
            f"RPC method {method!r} is not in the read-only allowlist. "
            "This lab never signs or sends transactions."
        )
    # eth_call with a 'from' that implies impersonation is harmless on a real
    # node, but state overrides (3rd param) are rejected to keep calls honest.
    if method == "eth_call" and params is not None and len(params) > 2:
        raise ReadOnlyViolation("eth_call state overrides are not allowed")
