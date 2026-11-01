"""Stage 8: evaluate the current market and issue / update a research signal.

  python -m scripts.evaluate_market [--sync]

--sync first loads the newest pool events (free dRPC endpoint). The decision uses only
data up to the latest synced block. Prints the signal in plain Russian; everything is
stored in signals / signal_events / market_evaluations. Signals are research output,
not investment advice; nothing is ever executed on-chain.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

from backend.backtesting.dataset import ROOT, cached_state_at, load_dataset
from backend.core.config import load_config
from backend.database import schema as S
from backend.signal_engine.engine import evaluate_market, load_strategies
from backend.signal_engine.market import latest_event_block, snapshot
from backend.strategies.mass_test import code_version, relabel_commit_versions
from backend.validation.segments import load_segment_table
from backend.validation.study import setup_study
from backend.virtual_portfolio.portfolio import journal, mark_all, open_new_positions

TYPE_RU = {"OPEN CANDIDATE": "КАНДИДАТ НА ОТКРЫТИЕ", "WAIT": "ЖДАТЬ", "HOLD": "ДЕРЖАТЬ",
           "REBALANCE": "ПЕРЕСТАВИТЬ ДИАПАЗОН", "CLOSE CANDIDATE": "КАНДИДАТ НА ЗАКРЫТИЕ",
           "INVALIDATED": "СИГНАЛ ОТМЕНЁН"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true", help="load new blocks first (dRPC)")
    args = ap.parse_args()
    if args.sync:
        r = subprocess.run([sys.executable, "-m", "scripts.sync_pool", "--endpoint", "drpc",
                            "--max-minutes", "20"], cwd=ROOT, capture_output=True, text=True)
        print("sync:", "ok" if r.returncode == 0 else f"failed ({r.stderr.strip().splitlines()[-1:]})")
    cfg = load_config()
    sc = cfg.lab["signal_engine"]
    p = cfg.pools[cfg.primary_pool]
    ds = load_dataset(cfg)
    study = setup_study(cfg, ds)
    engine = S.make_engine(cfg.lab["database"]["url"])
    relabel_commit_versions(engine)
    table = load_segment_table(engine, study.base, study.segments, code_version())
    state = cached_state_at(ds, latest_event_block(ds))
    now = int(time.time())
    vol_med = statistics.median(s.realized_vol for s in study.segments)
    snap = snapshot(ds, state, now, vol_med)
    meta = {"pool": cfg.primary_pool, "pair": f"{p.token0}/{p.token1}", "network": p.network,
            "pool_address": p.address, "fee_tier": p.fee / 1e6}
    d = evaluate_market(engine, ds, state, snap, table, load_strategies(engine), study, sc, meta,
                        lambda b: cached_state_at(ds, b), now)
    when = datetime.fromtimestamp(snap.timestamp, timezone.utc)
    print(f"\n{meta['pair']} {meta['fee_tier']:.2%} ({meta['network']}), блок {snap.block} "
          f"({when:%Y-%m-%d %H:%M} UTC, данные {snap.data_age_seconds // 60} мин назад)")
    print(f"цена {snap.price:,.0f} USDC · волатильность 7д {(snap.vol_7d or 0):.0%} · "
          f"изменение 30д {(snap.change_30d or 0):+.1%} · режим {snap.trend}/{snap.vol_class} · "
          f"объём 24ч ${snap.volume_24h_usd:,.0f} · TVL ${snap.tvl_usd:,.0f} · "
          f"доходность комиссий 7д {snap.fee_apr_7d:.1%} / 30д {snap.fee_apr_30d:.1%} годовых · "
          f"медианный дневной объём 30д ${snap.median_daily_volume_30d_usd:,.0f}")
    print(f"\nСИГНАЛ: {TYPE_RU.get(d.decision, d.decision)}  (#{d.signal_id})")
    pl = d.payload
    if d.decision == "OPEN CANDIDATE":
        r, a = pl["range"], pl["allocation"]
        print(f"  стратегия {pl['strategy']} · уверенность {pl['confidence']}"
              + (" · OOS-проверка не чистая" if pl["oos_contaminated"] else ""))
        print(f"  диапазон {r['price_lower']:,.0f} – {r['price_upper']:,.0f} USDC; "
              f"депозит {a['token0']:.6f} WBTC + {a['token1']:,.2f} USDC (доля USDC {a['usdc_value_share']:.0%})")
        print(f"  ожидаемые комиссии за {pl['horizon_days']} дн: ${pl['expected_fees_usd'][0]:,.2f} – "
              f"${pl['expected_fees_usd'][1]:,.2f}; расходы ${pl['expected_costs_usd']['total']:,.2f}")
        print(f"  прогноз чистого результата при ±1σ цены: ${pl['net_forecast_usd'][0]:,.0f} – "
              f"${pl['net_forecast_usd'][1]:,.0f}; макс. историческая просадка {pl['historical']['max_drawdown']:.0%}")
        print(f"  действует до {datetime.fromtimestamp(pl['valid_until'], timezone.utc):%Y-%m-%d %H:%M} UTC")
    elif d.decision == "WAIT":
        for x in pl.get("reasons", []):
            print(f"  причина: {x['name']} — {x['value']} (нужно {x['threshold']})")
        for c in pl.get("closest_candidates", [])[:5]:
            print(f"  ближе всех: {c['strategy']} — не прошла: {', '.join(c['failed'])}; "
                  f"OOS к пассивному {c['oos_mean_excess']:+.2%}, OOS ROI {c['oos_mean_roi']:+.2%} за отрезок")
    else:
        print("  " + "; ".join(str(x) for x in d.reasons))
    opened = open_new_positions(engine)
    marks = mark_all(engine, ds, lambda b: cached_state_at(ds, b), snap.block,
                     priority_fee_gwei=cfg.lab["costs"]["priority_fee_gwei"])
    j = journal(engine)
    c = j["counts"]
    usd = 10 ** ds.decimals1

    def pct(x):
        return "—" if x is None else f"{x:+.2%}"
    print(f"\nВИРТУАЛЬНЫЙ ПОРТФЕЛЬ: сигналов {c['all']} (на открытие {c['open_candidates']}), "
          f"активных позиций {c['active_positions']}, успешных {c['successful']}, неудачных "
          f"{c['failed']}, отменённых {c['cancelled']}; совокупный результат "
          f"${j['cumulative_net_a'] / usd:,.2f} (MODELED), за неделю {pct(j['return_week'])}, "
          f"за месяц {pct(j['return_month'])}, макс. просадка {j['max_drawdown']:.1%}")
    for m in marks:
        mm = m["metrics"]
        print(f"  позиция #{m['position_id']} {m['strategy']} [{m['status']}"
              f"{'/' + m['outcome'] if m['outcome'] else ''}]: итог ${mm['net_a'] / usd:,.2f}, "
              f"комиссии ${mm['fees_a'] / usd:,.2f}–${mm['fees_b'] / usd:,.2f}, к пассивному LP "
              f"{'—' if m['vs_passive_a'] is None else f'${m['vs_passive_a'] / usd:,.2f}'}"
              + ("" if m["opening_matches_signal"] else " — ОТКРЫТИЕ НЕ СОВПАЛО С СИГНАЛОМ"))
    if opened:
        print(f"  открыто виртуальных позиций: {len(opened)}")
    print("\nЭто исследовательский сигнал, а не инвестиционная рекомендация; система ничего не исполняет.")
    (ROOT / "data_snapshots" / "signals").mkdir(parents=True, exist_ok=True)
    (ROOT / "data_snapshots" / "signals" / "latest.json").write_text(
        json.dumps({"decision": d.decision, "signal_id": d.signal_id, "snapshot": d.snapshot,
                    "payload": d.payload}, indent=1, default=str) + "\n")


if __name__ == "__main__":
    main()
