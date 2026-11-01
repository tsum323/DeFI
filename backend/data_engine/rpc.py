"""Read-only JSON-RPC client.

Every request passes `core.safety.assert_read_only`. Endpoint URLs may embed
API keys, so they are NEVER included in exceptions, logs or reprs — only the
endpoint *name* from config/networks.yaml is shown.
"""

from __future__ import annotations

import json
import os
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from backend.core.safety import assert_read_only

# Provider messages meaning "your eth_getLogs window is too large".
_RANGE_MARKERS = (
    "query returned more than",
    "log response size exceeded",
    "block range",
    "range is too large",
    "range too large",
    "exceed maximum block range",
    "too many results",
    "response size",
    "limit exceeded",
    "query timeout",
)


class RpcError(RuntimeError):
    """Non-retryable JSON-RPC error returned by the node."""

    def __init__(self, endpoint: str, method: str, error: object):
        self.endpoint, self.method, self.error = endpoint, method, error
        super().__init__(f"[{endpoint}] {method}: {error}")


class RangeTooLarge(RpcError):
    """eth_getLogs window must be reduced."""


class EndpointUnavailable(RuntimeError):
    """Transport-level failure after all retries (network, 403, 429, 5xx)."""


class BadEndpointConfig(ValueError):
    pass


def validate_url(name: str, url: str) -> None:
    """Catch obviously broken URLs (e.g. an unfilled '<ВАШ_КЛЮЧ>' placeholder)
    without echoing the URL itself."""
    if not url.isascii():
        raise BadEndpointConfig(
            f"endpoint {name!r}: URL contains non-ASCII characters — looks like an "
            "unfilled placeholder instead of a real API key"
        )
    if not url.startswith(("https://", "http://")):
        raise BadEndpointConfig(f"endpoint {name!r}: URL must start with http(s)://")


@dataclass
class RpcClient:
    name: str
    url: str = field(repr=False)
    timeout: float = 30.0
    max_retries: int = 5
    backoff: tuple[float, ...] = (2, 4, 8, 16, 32)
    min_interval: float = 0.0  # seconds between requests (client-side rate limit)
    _last: float = field(default=0.0, repr=False)
    _ctx: ssl.SSLContext = field(default_factory=ssl.create_default_context, repr=False)
    requests_sent: int = 0

    def __post_init__(self) -> None:
        validate_url(self.name, self.url)

    # -- transport -------------------------------------------------------
    def _post(self, payload: object) -> object:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.url, body, {"content-type": "application/json", "accept": "application/json"}
        )
        last_exc: str = "unknown"
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests_sent += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as exc:
                last_exc = f"HTTP {exc.code}"
                if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                # str(exc) of URLError never contains the request URL.
                last_exc = type(exc).__name__ + ": " + str(getattr(exc, "reason", exc))
                if "403" in last_exc:  # proxy/network-policy denial: permanent
                    break
            if attempt < self.max_retries:
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                time.sleep(delay * (0.75 + random.random() / 2))
        raise EndpointUnavailable(f"[{self.name}] unavailable after retries: {last_exc}")

    @staticmethod
    def _classify(endpoint: str, method: str, err: dict) -> RpcError:
        msg = json.dumps(err).lower()
        if method == "eth_getLogs" and any(m in msg for m in _RANGE_MARKERS):
            return RangeTooLarge(endpoint, method, err)
        return RpcError(endpoint, method, err)

    @staticmethod
    def _rate_limited(err: dict) -> bool:
        msg = json.dumps(err).lower()
        return err.get("code") == 429 or "rate limit" in msg or "compute units" in msg \
            or "throughput" in msg or "capacity" in msg

    # -- public API ------------------------------------------------------
    def call(self, method: str, params: list) -> object:
        assert_read_only(method, params)
        for attempt in range(self.max_retries + 1):
            resp = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if not isinstance(resp, dict):
                raise RpcError(self.name, method, f"unexpected response type {type(resp)}")
            if "error" in resp:
                if self._rate_limited(resp["error"]) and attempt < self.max_retries:
                    time.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
                    continue
                raise self._classify(self.name, method, resp["error"])
            return resp["result"]
        raise EndpointUnavailable(f"[{self.name}] {method}: rate limited after retries")

    def batch(self, calls: list[tuple[str, list]]) -> list[object]:
        """JSON-RPC batch; results returned in request order. Falls back to
        sequential calls if the endpoint rejects batches."""
        if not calls:
            return []
        for m, p in calls:
            assert_read_only(m, p)
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                   for i, (m, p) in enumerate(calls)]
        resp = self._post(payload)
        if not isinstance(resp, list) or len(resp) != len(calls):
            return [self.call(m, p) for m, p in calls]
        by_id = {r.get("id"): r for r in resp}
        out = []
        for i, (m, p) in enumerate(calls):
            r = by_id.get(i, {})
            if "result" in r:
                out.append(r["result"])
            else:  # per-item error (often rate limit): retry individually
                out.append(self.call(m, p))
        return out

    # -- helpers ---------------------------------------------------------
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def eth_call(self, to: str, data: str, block: int | str = "latest") -> str:
        tag = hex(block) if isinstance(block, int) else block
        return self.call("eth_call", [{"to": to, "data": data}, tag])

    def get_logs(self, address: str | list[str], from_block: int, to_block: int,
                 topics: list | None = None) -> list[dict]:
        flt: dict = {"address": address, "fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if topics is not None:
            flt["topics"] = topics
        return self.call("eth_getLogs", [flt])


def build_clients(net, settings: dict | None = None) -> tuple[list[RpcClient], list[str]]:
    """Clients for every configured endpoint, in config order, plus human-readable
    notes for endpoints that were skipped (missing env var, broken URL)."""
    settings = settings or {}
    clients, notes = [], []
    for ep in net.rpc:
        url = ep.get("url") or os.environ.get(ep.get("url_env", ""), "")
        if not url:
            notes.append(f"{ep['name']}: skipped (env {ep.get('url_env')} not set)")
            continue
        try:
            clients.append(RpcClient(
                name=ep["name"], url=url,
                max_retries=settings.get("max_retries", 5),
                backoff=tuple(settings.get("retry_backoff_seconds", (2, 4, 8, 16, 32))),
                min_interval=float(ep.get("min_interval_seconds", 0.0)),
            ))
        except BadEndpointConfig as exc:
            notes.append(str(exc))
    return clients, notes


def select_client(net, settings: dict | None = None, *, require_archive: bool = False,
                  verbose: bool = True) -> RpcClient:
    """First endpoint that answers eth_chainId with the expected chain id."""
    clients, notes = build_clients(net, settings)
    archive = {ep["name"]: ep.get("archive", False) for ep in net.rpc}
    for c in clients:
        if require_archive and not archive.get(c.name):
            notes.append(f"{c.name}: skipped (not an archive endpoint)")
            continue
        try:
            cid = c.chain_id()
        except (EndpointUnavailable, RpcError) as exc:
            notes.append(f"{c.name}: {exc}")
            continue
        if cid != net.chain_id:
            notes.append(f"{c.name}: wrong chain id {cid}")
            continue
        if verbose:
            for n in notes:
                print("  rpc note:", n)
            print(f"  using RPC endpoint: {c.name}")
        return c
    raise EndpointUnavailable("no usable RPC endpoint: " + "; ".join(notes))
