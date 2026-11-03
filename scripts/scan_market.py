"""Market Scanner: load new Swap events of all scanned pools, then rank the pools.

  python -m scripts.scan_market [--max-minutes 15] [--no-fetch]

Incremental: only blocks after the last processed one are fetched (the first run
backfills `scanner.backfill_days`). Read-only: eth_getLogs / eth_call / eth_blockNumber.
"""

from __future__ import annotations

import argparse

from backend.core.config import load_config
from backend.data_engine.rpc import EndpointUnavailable, RpcError, build_clients
from backend.database import schema as S
from backend.lab.activity import log_activity
from backend.lab.scanner import ingest, save_scan, scan, scan_pools


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=15)
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    sc = cfg.lab["scanner"]
    engine = S.make_engine(cfg.lab["database"]["url"])
    clients = {c.name: c for c in build_clients(cfg.networks["ethereum"])[0]}
    client = clients.get(sc["endpoint"]) or next(iter(clients.values()))
    pools = scan_pools(cfg, sc["pools"])
    try:
        if not args.no_fetch:
            r = ingest(engine, client, pools, backfill_days=sc["backfill_days"],
                       max_seconds=args.max_minutes * 60)
            print(f"scanner data: blocks {r['from_block']}..{r['to_block']} (head {r['head']}), "
                  f"{r['logs']} swaps, {r['seconds']}s, complete={r['complete']}")
            log_activity(engine, "data", "ok" if r["complete"] else "warn",
                         f"Сканер: загружено {r['logs']} обменов по {len(pools)} пулам"
                         + ("" if r["complete"] else " (не всё — продолжу в следующий раз)"), r)
        res = scan(engine, client, cfg, pools)
    except (EndpointUnavailable, RpcError) as exc:
        log_activity(engine, "scan", "error", f"Сканер не смог получить данные: {exc}")
        raise SystemExit(f"scanner failed: {exc}") from exc
    save_scan(engine, res)
    good = [p for p in res["pools"] if p["score"] is not None and p["best"]["net_mid_usd"] > 0]
    log_activity(engine, "scan", "ok", f"Просканировано {len(res['pools'])} пулов; положительный ожидаемый "
                 f"результат у {len(good)}", {"top": [p["pool"] for p in res["pools"][:3]]})
    print(f"\nETH ${res['eth_usd']:,.0f} · BTC ${res['btc_usd']:,.0f} · газ {res['base_fee_gwei']:.2f} gwei "
          f"(открыть+закрыть ${res['gas_open_close_usd']:.2f})")
    for p in res["pools"]:
        b = p["best"]
        print(f"{p['pair']:10s} {p['fee_tier']:.2%}  цена {p['price']:,.4g}  TVL ${p['tvl_usd']:,.0f}  "
              f"объём 24ч ${p['volume_usd']['24h']:,.0f}  комиссии 24ч ${p['fees_usd']['24h']:,.0f}  "
              f"волат. {(p['volatility_7d'] or 0):.0%}  история {p['history_days']:.1f} дн.  "
              + (f"лучший ±{b['width']:.0%}: к HODL ${b['net_vs_hodl_usd'][0]:,.0f}…${b['net_vs_hodl_usd'][1]:,.0f} "
                 f"за 7 дн. на $10k, выход {b['exit_probability']:.0%}" if b else "")
              + (f"  ! {'; '.join(p['problems'] + p['data_quality'])}" if p["problems"] or p["data_quality"] else ""))


if __name__ == "__main__":
    main()
