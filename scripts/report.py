"""SPEC 16 step 12: analytical report of the current state -> data_snapshots/reports/report_<date>.md"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.api import queries as Q
from backend.backtesting.dataset import ROOT
from backend.core.config import load_config
from backend.database import schema as S

MSK = timezone(timedelta(hours=3), "МСК")  # Moscow time, UTC+3 all year


def _t(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d %H:%M МСК") if ts else "—"


def _p(x, d=2):
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def main() -> None:
    cfg = load_config()
    engine = S.make_engine(cfg.lab["database"]["url"])
    now = int(time.time())
    ds, ov, sig, pf = Q.datasources(engine, cfg), Q.overview(engine, cfg), Q.signals(engine), Q.portfolio(engine)
    rk = Q.ranking(engine, cfg)["rows"]
    res = Q.research(engine)
    with engine.connect() as c:
        runs = c.execute(select(S.job_runs).where(S.job_runs.c.started_at >= now - 86400)
                         .order_by(S.job_runs.c.id)).all()
    last = sig["signals"][0] if sig["signals"] else None
    L = [f"# Отчёт DeFi Liquidity Lab — {_t(now)}", "",
         "Метки: ТОЧНО — из блокчейна; МОДЕЛЬ — бэктест/гипотетическая позиция; ПРОГНОЗ — сценарий.", "",
         "## Данные (ТОЧНО)",
         f"- {ds['network']} · пул `{ds['pool']}` · последний блок {ds['last_block']} ({_t(ds['last_block_ts'])})",
         f"- событий: {sum(ds['events'].values()):,} · открытых пропусков: {ds['open_gaps']}", "",
         "## Сигнал (ПРОГНОЗ)"]
    if last:
        L.append(f"- #{last['id']} {last['type']} · статус {last['status']} · {_t(last['created_at'])}")
        for r in last["payload"].get("reasons", [])[:3]:
            L.append(f"  - {r['name']}: {r['value']}")
    else:
        L.append("- сигналов ещё не было")
    j = pf["journal"]
    L += ["", "## Виртуальный портфель (МОДЕЛЬ)",
          f"- сигналов {j['counts']['all']}, на открытие {j['counts']['open_candidates']}, активных позиций "
          f"{j['counts']['active_positions']}, успешных {j['counts']['successful']}, неудачных {j['counts']['failed']}",
          f"- совокупный результат ${j['cumulative_net_a'] / 1e6:,.2f}; неделя {_p(j['return_week'])}; "
          f"месяц {_p(j['return_month'])}; макс. просадка {j['max_drawdown']:.1%}", "",
          "## Прямые тесты кандидатов (МОДЕЛЬ, данные после постановки)"]
    for f in pf["forward_tests"]:
        r = f["result"]
        L.append(f"- #{f['id']} {r['strategy'] if r else f['params'].get('width_pct')}: "
                 + (f"{r['days']:.1f} дн., итог ${r['net_a'] / 1e6:,.2f}, к пассивному LP "
                    f"${(r['vs_passive_a'] or 0) / 1e6:,.2f}" if r else "ждёт новых блоков"))
    L += ["", "## Рейтинг по устойчивости — топ-5 (МОДЕЛЬ)"]
    for r in rk[:5]:
        L.append(f"- {r['name']}: оценка {r['score']}, OOS ROI {_p(r['oos_roi'])}, к пассивному {_p(r['oos_vs_passive'])}"
                 + (f" · флаги: {', '.join(r['flags'])}" if r["flags"] else ""))
    L += [f"- перспективных (лучше пассивного LP и на OOS, и на IS): {ov['promising_strategies']['value']}", "",
          "## AI-исследования",
          f"- гипотез всего: {len(res['hypotheses'])}; подтверждено: "
          f"{sum(h['verdict'] == 'confirmed' for h in res['hypotheses'])}; "
          f"ключ API: {'есть' if ov['ai_researcher']['api_key_configured'] else 'нет'}", "",
          "## Задачи за 24 часа"]
    if not runs:
        L.append("- запусков не было")
    for r in runs:
        L.append(f"- {_t(r.started_at)} {r.job} ({r.trigger}): {r.status}")
    out = ROOT / "data_snapshots" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"report_{datetime.fromtimestamp(now, MSK):%Y-%m-%d}.md"
    path.write_text("\n".join(L) + "\n")
    print(f"written {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
