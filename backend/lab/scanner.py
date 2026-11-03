"""Market Scanner (UNISWAP AI LAB): which Uniswap v3 pools look interesting right now.

Data (read-only, incremental):
  * Swap events of every scanned pool, fetched together (one eth_getLogs for all pool
    addresses) and stored as HOURLY aggregates in `pool_hours`: number of swaps, volume
    and fees per token, and the EXACT sqrtPrice / tick / active liquidity after the last
    swap of the hour (Uniswap emits them in every Swap event). Only blocks after
    `scan_state.last_block` are ever fetched.
  * Current state per pool by eth_call: slot0, liquidity, token balances (TVL);
    ETH/USD from the Chainlink feed; gas from the latest block's base fee.

Estimates (always labelled ESTIMATED — not the exact replay used for WBTC/USDC 0.3%):
  * fees a hypothetical range would have earned over the last 7 / 30 days: for every hour
    whose closing price lay inside the range, the hour's fees x our share of the active
    liquidity, share = L_ours / (L_active + L_ours). Hourly granularity, so a range that was
    crossed within an hour is counted by its closing price only.
  * impermanent loss at +-1 sigma (exact v3 amount formulas in float), the chance of
    leaving the range within the horizon, gas to open and close, the swap to the deposit
    ratio (pool fee on the swapped half), and the expected result versus holding.
"""

from __future__ import annotations

import itertools
import json
import math
import time
from dataclasses import dataclass

from sqlalchemy import insert, select, update

from backend.data_engine.abi import decode_words, encode_call, topic
from backend.database import schema as S

SWAP_TOPIC = topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
HOUR = 3600
DAY = 86_400
STREAM = "scanner_swaps"
WIDTHS = (0.02, 0.05, 0.10, 0.20, 0.30)     # symmetric ranges around the current price
CAPITAL_USD = 10_000.0                      # reference size for the comparison (scales)
HORIZON_DAYS = 7

# base / quote shown to the user (price of 1 base in quote)
BASE = {"wbtc_usdc_3000": "WBTC", "wbtc_usdc_500": "WBTC", "weth_usdc_100": "WETH",
        "weth_usdc_500": "WETH", "weth_usdc_3000": "WETH", "wbtc_weth_100": "WBTC",
        "wbtc_weth_500": "WBTC", "wbtc_weth_3000": "WBTC"}
TOKEN_RISK = {
    "WBTC": "WBTC — обёртка биткоина у кастодиана (BitGo/BiT Global); цена зависит от возможности выкупа",
    "USDC": "USDC — доллар Circle; был кратковременный отвяз в марте 2023",
    "WETH": "WETH — обёрнутый эфир (контракт без кастодиана)",
}


@dataclass(frozen=True)
class ScanPool:
    key: str
    address: str
    token0: str
    token1: str
    dec0: int
    dec1: int
    fee: int              # hundredths of a bip (3000 = 0.30%)
    base: str

    @property
    def pair_key(self) -> str:
        return f"{self.base}/{self.quote}"

    @property
    def quote(self) -> str:
        return self.token1 if self.base == self.token0 else self.token0


def scan_pools(cfg, keys: list[str]) -> list[ScanPool]:
    out = []
    for k in keys:
        p = cfg.pools[k]
        t = cfg.tokens[p.network]
        out.append(ScanPool(k, p.address, p.token0, p.token1, t[p.token0].decimals,
                            t[p.token1].decimals, p.fee, BASE.get(k, p.token0)))
    return out


# ---------------------------------------------------------------- learned parameters
def get_state(engine, key: str) -> dict | None:
    with engine.connect() as c:
        v = c.execute(select(S.lab_state.c.value_json).where(S.lab_state.c.key == key)).scalar()
    return json.loads(v) if v else None


def set_state(engine, key: str, value: dict, now: int | None = None) -> None:
    with engine.begin() as c:
        c.execute(S.lab_state.delete().where(S.lab_state.c.key == key))
        c.execute(insert(S.lab_state).values(key=key, value_json=json.dumps(value), updated_at=now or int(time.time())))


def calibrate_fees(engine, cfg, ds, state_at_fn, widths=(0.05, 0.10, 0.20, 0.30)) -> dict:
    """Hourly fee estimate vs the exact replay on the exactly modelled pool, last 7 days.
    The median ratio becomes the scanner's fee correction factor (a learned parameter)."""
    from backend.backtesting import uniswap_math as um
    from backend.backtesting.position_sim import _value1
    from backend.signal_engine.engine import _fee_estimate_uncached
    from backend.signal_engine.market import latest_event_block
    p = scan_pools(cfg, [cfg.primary_pool])[0]
    blk = latest_event_block(ds)
    st = state_at_fn(blk)
    now = ds.blocks[blk][0]
    with engine.connect() as c:
        hrs = c.execute(select(S.pool_hours).where(S.pool_hours.c.pool == p.key, S.pool_hours.c.hour_ts >= now - 7 * DAY,
                                                   S.pool_hours.c.hour_ts < now).order_by(S.pool_hours.c.hour_ts)).all()
    if len(hrs) < 24 * 5:
        return {"factor": None, "note": "мало часовых данных для калибровки"}
    price = price1per0(st.sqrt_price, p)
    usd1 = 1.0
    rows = []
    for w in widths:
        pos = position(p, st.sqrt_price, w, CAPITAL_USD, price, usd1)
        est, _ = range_fees(hrs, p, pos, lambda h: h.fee0 * price + h.fee1 * usd1)
        spacing = cfg.pools[p.key].tick_spacing
        lo = um.get_tick_at_sqrt_ratio(int(pos["sa"] * 2 ** 96)) // spacing * spacing
        hi = um.get_tick_at_sqrt_ratio(int(pos["sb"] * 2 ** 96)) // spacing * spacing
        ex = _fee_estimate_uncached(ds, state_at_fn, lo, hi, int(pos["L"]), blk, 7)
        exact = _value1(*ex["a"], st.sqrt_price) / 10 ** p.dec1
        rows.append({"width": w, "scanner_usd": est, "exact_usd": exact, "ratio": est / exact if exact else None})
    ratios = sorted(r["ratio"] for r in rows if r["ratio"])
    factor = 1 / ratios[len(ratios) // 2] if ratios else None
    return {"factor": factor, "rows": rows, "pool": p.key,
            "note": "множитель к оценке комиссий сканера = точная модель / часовая оценка (медиана)"}


# ---------------------------------------------------------------- ingestion
def aggregate_logs(logs: list[dict], pools: dict[str, ScanPool]) -> dict[tuple[str, int], dict]:
    """Hourly aggregates of Swap logs (logs must be in chain order)."""
    out: dict[tuple[str, int], dict] = {}
    for lg in logs:
        p = pools.get(lg["address"].lower())
        if p is None or lg["topics"][0].lower() != SWAP_TOPIC:
            continue
        a0, a1, sqrt_p, liq, tick = decode_words(["int256", "int256", "uint160", "uint128", "int24"], lg["data"])
        ts = int(lg["blockTimestamp"], 16)
        key = (p.key, ts - ts % HOUR)
        h = out.setdefault(key, {"swaps": 0, "vol0": 0.0, "vol1": 0.0, "fee0": 0.0, "fee1": 0.0})
        h["swaps"] += 1
        h["vol0"] += abs(a0) / 10 ** p.dec0
        h["vol1"] += abs(a1) / 10 ** p.dec1
        # the input amount includes the fee: fee = amountIn * fee / 1e6
        if a0 > 0:
            h["fee0"] += a0 * p.fee / 1e6 / 10 ** p.dec0
        if a1 > 0:
            h["fee1"] += a1 * p.fee / 1e6 / 10 ** p.dec1
        h.update(close_sqrt_price=str(sqrt_p), close_tick=tick, close_liquidity=str(liq),
                 last_block=int(lg["blockNumber"], 16))
    return out


def store_hours(conn, agg: dict[tuple[str, int], dict]) -> None:
    """Merge hourly aggregates (an hour can be split across two fetches)."""
    for (pool, hour), h in agg.items():
        row = conn.execute(select(S.pool_hours).where(S.pool_hours.c.pool == pool,
                                                      S.pool_hours.c.hour_ts == hour)).first()
        if row is None:
            conn.execute(insert(S.pool_hours).values(pool=pool, hour_ts=hour, **h))
        else:
            conn.execute(update(S.pool_hours).where(S.pool_hours.c.pool == pool, S.pool_hours.c.hour_ts == hour)
                         .values(swaps=row.swaps + h["swaps"], vol0=row.vol0 + h["vol0"],
                                 vol1=row.vol1 + h["vol1"], fee0=row.fee0 + h["fee0"],
                                 fee1=row.fee1 + h["fee1"], close_sqrt_price=h["close_sqrt_price"],
                                 close_tick=h["close_tick"], close_liquidity=h["close_liquidity"],
                                 last_block=h["last_block"]))


def ingest(engine, client, pools: list[ScanPool], *, backfill_days: int = 30, step: int = 100,
           batch: int = 3, max_seconds: float = 900, confirmations: int = 6,
           start_block: int | None = None, log=print) -> dict:
    """Fetch Swap logs of all pools after the last processed block (incremental).
    The first run starts `backfill_days` back. Stops after `max_seconds` (resumes later)."""
    t0 = time.monotonic()
    head = client.block_number() - confirmations
    with engine.connect() as c:
        st = c.execute(select(S.scan_state).where(S.scan_state.c.stream == STREAM)).first()
    start = st.last_block + 1 if st else (start_block if start_block is not None
                                           else max(0, head - backfill_days * DAY // 12))
    first = st.first_block if st else start
    by_addr = {p.address.lower(): p for p in pools}
    addrs = [p.address for p in pools]
    done, n_logs = start - 1, 0
    while done < head and time.monotonic() - t0 < max_seconds:
        ranges = []
        b = done + 1
        while b <= head and len(ranges) < batch:
            ranges.append((b, min(head, b + step - 1)))
            b += step
        res = client.batch([("eth_getLogs", [{"address": addrs, "topics": [SWAP_TOPIC],
                                              "fromBlock": hex(lo), "toBlock": hex(hi)}]) for lo, hi in ranges])
        logs = sorted((lg for r in res for lg in r), key=lambda g: (int(g["blockNumber"], 16), int(g["logIndex"], 16)))
        with engine.begin() as conn:
            store_hours(conn, aggregate_logs(logs, by_addr))
            values = {"first_block": first, "last_block": ranges[-1][1], "updated_at": int(time.time())}
            if st is None:
                conn.execute(insert(S.scan_state).values(stream=STREAM, **values))
                st = True
            else:
                conn.execute(update(S.scan_state).where(S.scan_state.c.stream == STREAM).values(**values))
        done = ranges[-1][1]
        n_logs += len(logs)
    if done < head:
        log(f"  scanner: stopped at block {done} ({head - done} blocks left, resumes next run)")
    return {"from_block": start, "to_block": done, "head": head, "logs": n_logs,
            "complete": done >= head, "seconds": round(time.monotonic() - t0)}


# ---------------------------------------------------------------- current state
def _word(hexdata: str, i: int) -> int:
    return int(hexdata[2 + 64 * i: 2 + 64 * (i + 1)], 16)


def read_state(client, p: ScanPool, cfg) -> dict:
    """slot0, active liquidity and token balances of the pool (EXACT, eth_call)."""
    t = cfg.tokens["ethereum"]
    slot0 = client.eth_call(p.address, encode_call("slot0()"))
    liq = int(client.eth_call(p.address, encode_call("liquidity()")), 16)
    bal0 = int(client.eth_call(t[p.token0].address, encode_call("balanceOf(address)", p.address)), 16)
    bal1 = int(client.eth_call(t[p.token1].address, encode_call("balanceOf(address)", p.address)), 16)
    tick = _word(slot0, 1)
    tick = tick - 2 ** 256 if tick >= 2 ** 255 else tick
    return {"sqrt_price": _word(slot0, 0), "tick": tick, "liquidity": liq,
            "balance0": bal0 / 10 ** p.dec0, "balance1": bal1 / 10 ** p.dec1}


def eth_usd_now(client, cfg) -> float:
    feed = cfg.networks["ethereum"].oracles["chainlink_eth_usd"]
    r = client.eth_call(feed, encode_call("latestRoundData()"))
    return _word(r, 1) / 1e8


# ---------------------------------------------------------------- math (float, human units)
def price1per0(sqrt_price: float, p: ScanPool) -> float:
    """token1 per token0 in human units."""
    return (float(sqrt_price) / 2 ** 96) ** 2 * 10 ** (p.dec0 - p.dec1)


def base_price(sqrt_price: float, p: ScanPool) -> float:
    x = price1per0(sqrt_price, p)
    return x if p.base == p.token0 else 1 / x


def amounts_for_liquidity(L: float, sp: float, sa: float, sb: float) -> tuple[float, float]:
    """Raw token amounts of liquidity L in [sa, sb] at sqrt price sp (v3 formulas)."""
    if sp <= sa:
        return L * (1 / sa - 1 / sb), 0.0
    if sp >= sb:
        return 0.0, L * (sb - sa)
    return L * (1 / sp - 1 / sb), L * (sp - sa)


def position(p: ScanPool, sqrt_price: int, width: float, capital_usd: float, usd0: float,
             usd1: float) -> dict:
    """A symmetric range +-width around the current BASE price, sized to capital_usd."""
    sp = float(sqrt_price) / 2 ** 96                     # sqrt of raw token1/token0
    if p.base == p.token0:   # base price = token1/token0: band [1-w, 1+w] in price
        sa, sb = sp * math.sqrt(1 - width), sp * math.sqrt(1 + width)
    else:                    # base price = token0/token1: band in 1/price
        sa, sb = sp / math.sqrt(1 + width), sp / math.sqrt(1 - width)
    a0, a1 = amounts_for_liquidity(1.0, sp, sa, sb)
    v_per_L = a0 / 10 ** p.dec0 * usd0 + a1 / 10 ** p.dec1 * usd1
    L = capital_usd / v_per_L
    a0, a1 = a0 * L, a1 * L
    lower, upper = base_price((sa * 2 ** 96), p), base_price((sb * 2 ** 96), p)
    return {"L": L, "sa": sa, "sb": sb, "sp": sp,
            "amount0": a0 / 10 ** p.dec0, "amount1": a1 / 10 ** p.dec1,
            "price_lower": min(lower, upper), "price_upper": max(lower, upper),
            "share_token1_value": a1 / 10 ** p.dec1 * usd1 / capital_usd}


def il_at(pos: dict, p: ScanPool, change: float, usd0: float, usd1: float) -> float:
    """Value of the LP minus value of holding the same initial tokens, after the BASE price
    moves by `change` (USD; quote token value held constant)."""
    # base price up by `change`: token1/token0 moves by (1+change) if base is token0, else 1/(1+change)
    f = (1 + change) if p.base == p.token0 else 1 / (1 + change)
    sp2 = pos["sp"] * math.sqrt(f)
    a0, a1 = amounts_for_liquidity(pos["L"], sp2, pos["sa"], pos["sb"])
    u0, u1 = (usd0 * (1 + change), usd1) if p.base == p.token0 else (usd0, usd1 * (1 + change))
    lp = a0 / 10 ** p.dec0 * u0 + a1 / 10 ** p.dec1 * u1
    hold = pos["amount0"] * u0 + pos["amount1"] * u1
    return lp - hold


def exit_probability(width: float, sigma_h: float) -> float:
    """Chance the price touches +-width within the horizon (driftless log-Brownian motion)."""
    if sigma_h <= 0:
        return 0.0
    up, down = math.log(1 + width), -math.log(1 - width)

    def phi(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return min(1.0, 2 * (1 - phi(up / sigma_h)) + 2 * (1 - phi(down / sigma_h)))


def range_fees(hours: list, p: ScanPool, pos: dict, fee_usd) -> tuple[float, float]:
    """(fees USD, hours in range) the range would have earned over `hours` (ESTIMATED)."""
    total, inside = 0.0, 0
    for h in hours:
        sp = float(h.close_sqrt_price) / 2 ** 96
        if pos["sa"] <= sp < pos["sb"]:
            L = float(h.close_liquidity)
            total += fee_usd(h) * pos["L"] / (L + pos["L"])
            inside += 1
    return total, inside


def realized_vol(hours: list, p: ScanPool) -> float | None:
    """Annualized volatility of hourly closing prices.

    The closing swap of an hour can be an outlier (a large trade arbitraged back minutes
    later): a close that jumps more than 5% and comes back by at least half the next hour
    is replaced by the neighbouring close before the standard deviation is taken."""
    px = [base_price(int(h.close_sqrt_price), p) for h in hours]
    px = [x for x in px if x > 0]
    for k in range(1, len(px) - 1):
        a, b = math.log(px[k] / px[k - 1]), math.log(px[k + 1] / px[k])
        if abs(a) > 0.05 and a * b < 0 and abs(b) > 0.5 * abs(a):
            px[k] = px[k - 1]
    rets = [math.log(b / a) for a, b in itertools.pairwise(px)]
    if len(rets) < 24:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    span_h = (hours[-1].hour_ts - hours[0].hour_ts) / HOUR or 1
    per_year = len(rets) / span_h * 24 * 365
    return math.sqrt(var * per_year)


def price_stats(hours: list, p: ScanPool) -> dict:
    """Volume-weighted average price, low and high of the hourly closes over `hours`
    (base price in quote; EXACT sums of swap amounts)."""
    v0 = sum(h.vol0 for h in hours)
    v1 = sum(h.vol1 for h in hours)
    closes = [base_price(int(h.close_sqrt_price), p) for h in hours]
    if not v0 or not v1 or not closes:
        return {"vwap_7d": None, "low_7d": None, "high_7d": None}
    vwap = v1 / v0 if p.base == p.token0 else v0 / v1
    return {"vwap_7d": vwap, "low_7d": min(closes), "high_7d": max(closes)}


# ---------------------------------------------------------------- scan
def scan(engine, client, cfg, pools: list[ScanPool], now: int | None = None) -> dict:
    now = now or int(time.time())
    eth_usd = eth_usd_now(client, cfg)
    block = client.call("eth_getBlockByNumber", ["latest", False])
    base_fee = int(block["baseFeePerGas"], 16)
    prio = cfg.lab["costs"]["priority_fee_gwei"] * 1e9
    gas = cfg.lab["costs"]["gas_units"]
    gas_usd = (gas["mint_new_position"] + gas["decrease_liquidity_and_collect"]) * (base_fee + prio) / 1e18 * eth_usd
    states = {p.key: read_state(client, p, cfg) for p in pools}
    calib = get_state(engine, "scanner_fee_calibration") or {}
    fee_k = calib.get("factor") or 1.0
    btc = next((base_price(states[k]["sqrt_price"], p) for k in ("wbtc_usdc_3000", "wbtc_usdc_500")
                for p in pools if p.key == k), None)
    usd = {"USDC": 1.0, "WETH": eth_usd, "WBTC": btc}
    with engine.connect() as c:
        rows = c.execute(select(S.pool_hours).where(S.pool_hours.c.hour_ts >= now - 31 * DAY)
                         .order_by(S.pool_hours.c.hour_ts)).all()
    by_pool: dict[str, list] = {}
    for r in rows:
        by_pool.setdefault(r.pool, []).append(r)
    # volatility per PAIR, from its most active pool (thin pools have stale closes)
    pair_vol = {}
    for pk in {p.pair_key for p in pools}:
        same = [p for p in pools if p.pair_key == pk]
        top = max(same, key=lambda p: sum(h.swaps for h in by_pool.get(p.key, [])))
        pair_vol[pk] = realized_vol([h for h in by_pool.get(top.key, []) if h.hour_ts >= now - 7 * DAY], top)
    results = []
    for p in pools:
        st, hrs = states[p.key], by_pool.get(p.key, [])
        u0, u1 = usd[p.token0], usd[p.token1]
        if u0 is None or u1 is None:
            continue

        def fee_usd(h, u0=u0, u1=u1):
            return h.fee0 * u0 + h.fee1 * u1
        h7 = [h for h in hrs if h.hour_ts >= now - 7 * DAY]
        h1 = [h for h in hrs if h.hour_ts >= now - DAY]
        span_days = (now - hrs[0].hour_ts) / DAY if hrs else 0
        vol_usd = {k: sum(max(h.vol0 * u0, h.vol1 * u1) for h in hs) for k, hs in (("24h", h1), ("7d", h7), ("30d", hrs))}
        fees_usd = {k: sum(fee_usd(h) for h in hs) for k, hs in (("24h", h1), ("7d", h7), ("30d", hrs))}
        tvl = st["balance0"] * u0 + st["balance1"] * u1
        sigma = pair_vol.get(p.pair_key)
        sig_h = (sigma or 0) * math.sqrt(HORIZON_DAYS / 365)
        # active liquidity value within +-2% of the price
        act = position(p, st["sqrt_price"], 0.02, 1.0, u0, u1)
        active_usd = st["liquidity"] / act["L"] if act["L"] else 0.0
        ranges = []
        for w in WIDTHS:
            pos = position(p, st["sqrt_price"], w, CAPITAL_USD, u0, u1)
            f7, in7 = range_fees(h7, p, pos, fee_usd)
            f30, _ = range_fees(hrs, p, pos, fee_usd)
            days7 = min(7.0, span_days)
            rate = [f7 / days7 if days7 > 0 else 0.0, f30 / span_days if span_days > 0 else 0.0]
            fees_h = sorted(r * HORIZON_DAYS * fee_k for r in rate)
            il = [il_at(pos, p, -sig_h, u0, u1), il_at(pos, p, sig_h, u0, u1)] if sig_h else [0.0, 0.0]
            il_mid = sum(il) / 2
            swap_cost = CAPITAL_USD * (1 - pos["share_token1_value"] if p.base == p.token0 else pos["share_token1_value"]) * p.fee / 1e6
            costs = gas_usd + swap_cost
            net = [fees_h[0] + il_mid - costs, fees_h[1] + il_mid - costs]
            scen = []
            for ch in (-0.2, -0.1, 0.1, 0.2):   # value of the position if the base price moves
                hold_ch = (pos["amount0"] * u0 * ch if p.base == p.token0 else pos["amount1"] * u1 * ch)
                il_ch = il_at(pos, p, ch, u0, u1)
                scen.append({"change": ch, "il_usd": il_ch, "position_change_usd": hold_ch + il_ch})
            ranges.append({"width": w, "price_lower": pos["price_lower"], "price_upper": pos["price_upper"],
                           "scenarios": scen,
                           "L": pos["L"], "sa": pos["sa"], "sb": pos["sb"],
                           "amount0": pos["amount0"], "amount1": pos["amount1"],
                           "share_quote_value": pos["share_token1_value"] if p.quote == p.token1 else 1 - pos["share_token1_value"],
                           "fees_usd": fees_h, "il_usd": il_mid, "il_scenarios_usd": il,
                           "gas_usd": gas_usd, "swap_usd": swap_cost, "net_vs_hodl_usd": net,
                           "net_mid_usd": sum(net) / 2, "exit_probability": exit_probability(w, sig_h),
                           "hours_in_range_7d": in7, "liquidity_share": pos["L"] / (st["liquidity"] + pos["L"]),
                           "label": "ESTIMATED"})
        # a range the price will very likely leave within the horizon is not a candidate:
        # its fee estimate assumes the range keeps earning
        ok = [r for r in ranges if r["exit_probability"] <= 0.5] or ranges
        best = max(ok, key=lambda r: r["net_mid_usd"] * (1 - r["exit_probability"])) if ok else None
        quality = []
        if span_days < 7:
            quality.append(f"история в сканере только {span_days:.1f} дн.")
        if len(h7) < 24 * 7 * 0.5:
            quality.append(f"обмены были лишь в {len(h7)} часах из 168 за неделю")
        problems = []
        if vol_usd["24h"] < 100_000:
            problems.append("мало торгов (< $100 000 за сутки)")
        if tvl < 1_000_000:
            problems.append("мало ликвидности в пуле (< $1 млн)")
        if sigma is not None and sigma > 1.0:
            problems.append(f"очень высокая волатильность ({sigma:.0%} годовых)")
        if best and best["net_mid_usd"] <= 0:
            problems.append("ни один диапазон не даёт положительного ожидаемого результата после расходов")
        results.append({
            "pool": p.key, "pair": f"{p.base}/{p.quote}", "address": p.address, "fee_tier": p.fee / 1e6,
            "token0": p.token0, "token1": p.token1,
            "price": base_price(st["sqrt_price"], p), "tick": st["tick"],
            "usd0": u0, "usd1": u1, "base_usd": u0 if p.base == p.token0 else u1,
            "quote_usd": u1 if p.base == p.token0 else u0, **price_stats(h7, p),
            "tvl_usd": tvl, "active_liquidity_usd_2pct": active_usd,
            "volume_usd": vol_usd, "fees_usd": fees_usd,
            "fee_yield_active_24h": fees_usd["24h"] / active_usd if active_usd else None,
            "volatility_7d": sigma, "history_days": span_days, "hours_7d": len(h7),
            "ranges": ranges, "best": best, "data_quality": quality, "problems": problems,
            "token_risks": [TOKEN_RISK[t] for t in (p.token0, p.token1) if t in TOKEN_RISK],
            "score": round(best["net_mid_usd"] / CAPITAL_USD * 100 * (1 - best["exit_probability"]), 3)
            if best and not quality else None,
            "labels": {"state": "EXACT", "volume_fees": "EXACT (sum of Swap events)",
                       "ranges": "ESTIMATED"},
        })
    results.sort(key=lambda r: -(r["score"] if r["score"] is not None else -99))
    return {"created_at": now, "block": int(block["number"], 16), "eth_usd": eth_usd, "btc_usd": btc,
            "base_fee_gwei": base_fee / 1e9, "gas_open_close_usd": gas_usd, "capital_usd": CAPITAL_USD,
            "horizon_days": HORIZON_DAYS, "fee_calibration": calib or None, "pools": results,
            "label": "ESTIMATED"}


def save_scan(engine, result: dict) -> None:
    with engine.begin() as conn:
        conn.execute(S.scan_results.delete())
        conn.execute(insert(S.scan_results).values(created_at=result["created_at"], block=result["block"],
                                                   payload_json=json.dumps(result)))
