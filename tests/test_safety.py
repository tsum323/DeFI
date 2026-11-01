"""Architectural guarantees that the lab cannot move funds."""

import re
from pathlib import Path

import pytest

from backend.core.safety import ReadOnlyViolation, assert_read_only

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_METHODS = [
    "eth_sendRawTransaction",
    "eth_sendTransaction",
    "eth_sign",
    "eth_signTransaction",
    "eth_signTypedData_v4",
    "personal_sign",
    "personal_unlockAccount",
    "wallet_addEthereumChain",
    "eth_sendUserOperation",
    "eth_accounts",
    "eth_requestAccounts",
]


@pytest.mark.parametrize("method", FORBIDDEN_METHODS)
def test_write_methods_rejected(method):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(method, [])


def test_read_methods_allowed():
    for m in ("eth_getLogs", "eth_call", "eth_blockNumber", "eth_getBlockByNumber"):
        assert_read_only(m, [])


def test_eth_call_state_override_rejected():
    with pytest.raises(ReadOnlyViolation):
        assert_read_only("eth_call", [{}, "latest", {"0x00": {"balance": "0x1"}}])


# Patterns that must never appear in project source (excluding this test file).
SOURCE_BANNED = [
    r"send_raw_transaction",
    r"sendRawTransaction",
    r"send_transaction\(",
    r"sign_transaction",
    r"Account\.from_key",
    r"from_mnemonic",
    r"PRIVATE_KEY",
    r"SEED_PHRASE",
    r"\bapprove\(",
]


def test_source_tree_has_no_signing_code():
    offenders = []
    guard = ROOT / "backend" / "core" / "safety.py"  # names forbidden methods in docs only
    for path in list((ROOT / "backend").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        if path == guard:
            continue
        text = path.read_text(encoding="utf-8")
        for pat in SOURCE_BANNED:
            if re.search(pat, text):
                offenders.append(f"{path.relative_to(ROOT)}: {pat}")
    assert not offenders, offenders


FRONTEND_BANNED = [r"window\.ethereum", r"eth_sendTransaction", r"eth_signTypedData", r"personal_sign",
                   r"eth_requestAccounts", r"wallet_"]


def test_frontend_has_no_wallet_or_signing_code():
    src = ROOT / "frontend" / "src"
    offenders = [f"{p.relative_to(ROOT)}: {pat}" for p in src.rglob("*.ts*")
                 for pat in FRONTEND_BANNED if re.search(pat, p.read_text(encoding="utf-8"))]
    assert not offenders, offenders


SECRET_PATTERNS = [r"sk-ant-[A-Za-z0-9_-]{20,}", r"alchemy\.com/v2/[A-Za-z0-9_-]{16,}",
                   r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\b0x[0-9a-fA-F]{64}\b(?=.*private)"]


def test_no_secrets_in_tracked_files():
    import subprocess
    files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).split()
    offenders = []
    for f in files:
        p = ROOT / f
        if p.suffix in {".xz", ".png", ".sqlite", ".pkl"} or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders += [f"{f}: {pat}" for pat in SECRET_PATTERNS if re.search(pat, text)]
    assert not offenders, offenders
