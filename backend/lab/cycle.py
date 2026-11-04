"""The autonomous research routine of UNISWAP AI LAB.

Every market update (each 15 minutes, after the pool sync, the strict signal, the opportunity
ranking and the forward tests — see scripts/live_update.py):
  1. scanner: new Swap events of all pools, pool ranking with range estimates
  2. virtual portfolio: open positions from the best opportunities, value all, close by rules
  3. self-learning: interim / final comparison of forecasts with results (lessons)
Once a day (first update after 03:00 MSK):
  4. calibration of the scanner fee estimate against the exact replay
  5. research: new hypotheses from lessons, near misses and the market regime, tested on the
     stored history (only the new strategies are computed — the study is never re-run)
  6. daily report (what was done, found, learned, and what comes next)
Every step writes to the activity log; a failure is logged as a failure, never as success.
"""

from __future__ import annotations

import json
import statistics
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from backend.database import schema as S
from backend.lab.activity import log_activity, recent_activity

MSK = timezone(timedelta(hours=3))
DAILY_HOUR_MSK = 3


def msk_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%Y-%m-%d")


def daily_due(engine, now: int) -> bool:
    """True once per Moscow day, from DAILY_HOUR_MSK on."""
    from backend.lab.scanner import get_state
    last = (get_state(engine, "daily_research") or {}).get("day")
    return datetime.fromtimestamp(now, MSK).hour >= DAILY_HOUR_MSK and last != msk_day(now)


def step(engine, kind: str, fn, *args, **kw):
    """Run one step; log a failure honestly and continue with the rest of the cycle."""
    try:
        return fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001 - every failure must reach the activity log
        log_activity(engine, kind, "error", f"Шаг «{kind}» не выполнен: {type(exc).__name__}: {exc}",
                     {"trace": traceback.format_exc()[-2000:]})
        print(f"  ! step {kind} failed: {exc}")
        return None


DATA_FIX = "data_fix_2026_11_04"


def repair_once(engine, cfg) -> bool:
    """One-time repair after the restore bug of 2026-11-03/04: the once-a-day database cache
    kept old scan progress, so the same Swap events were added to the hourly data again on
    every update (fees inflated many times). The hourly data and the scan progress are rebuilt
    from scratch, and the bot (whose decisions used the inflated numbers) starts again."""
    from backend.lab.scanner import get_state, set_state
    if get_state(engine, DATA_FIX):
        return False
    with engine.begin() as c:
        for t in (S.pool_hours, S.scan_state, S.scan_results, S.bot_positions, S.bot_history):
            c.execute(t.delete())
        c.execute(S.lab_state.delete().where(S.lab_state.c.key.in_(["bot_trust", "bot_review", "bot_wait"])))
    set_state(engine, DATA_FIX, {"done": True})
    log_activity(engine, "data", "warn", "Исправлена ошибка данных: с вечера 3 ноября обмены одних и тех же блоков "
                 "засчитывались повторно, и комиссии в приближённых оценках были завышены во много раз. Часовые данные "
                 "всех пулов удалены и загружаются заново; оценки пересчитаются автоматически.")
    log_activity(engine, "bot", "warn", "Бот перезапущен с $100: его прошлые решения (и результат +23%) опирались на "
                 "завышенные из-за ошибки данных комиссии. Это был ненастоящий результат.")
    return True


def latest_ranking(engine) -> dict | None:
    with engine.connect() as c:
        v = c.execute(select(S.opportunity_rankings.c.payload_json).order_by(
            S.opportunity_rankings.c.id.desc()).limit(1)).scalar()
    return json.loads(v) if v else None


def latest_scan(engine) -> dict | None:
    with engine.connect() as c:
        v = c.execute(select(S.scan_results.c.payload_json).order_by(S.scan_results.c.id.desc()).limit(1)).scalar()
    return json.loads(v) if v else None


# ---------------------------------------------------------------- research of the day
def daily_research(engine, cfg, ds, study, version: str, n: int = 3) -> dict:
    """Hypotheses from the learning adapter -> experiments on the stored history."""
    from backend.ai_researcher.research import run_cycle
    from backend.lab.learning import LearningAdapter
    from backend.strategies.generator import DEFAULT_SPACE, generate
    from backend.validation.segments import load_segment_table
    table = load_segment_table(engine, study.base, study.segments, version)
    full = {h for (h, _, var), m in table.items() if var == "base" and m}
    full = {h for h in full if all(table.get((h, s.index, "base")) for s in study.segments)}
    ranking = latest_ranking(engine) or {}
    near = [o for t in ("CONSERVATIVE", "MODERATE", "EXPERIMENTAL") for o in ranking.get("tiers", {}).get(t, [])][:3]
    vol_now = (ranking.get("market") or {}).get("vol_7d")
    vol_med = statistics.median(s.realized_vol for s in study.segments)
    adapter = LearningAdapter(engine, full, near, vol_now, vol_med)
    summary = run_cycle(engine, ds, study, adapter, generate(DEFAULT_SPACE), n=n, version=version)
    summary["next_tasks"] = adapter.queue
    return summary


def daily_report(engine, cfg, now: int, research: dict | None, out_dir: Path) -> Path:
    """Plain-language report of the day -> data_snapshots/reports/daily_<day>.md (+ .json)."""
    from backend.lab.learning import fee_calibration
    from backend.lab.portfolio import summary as pf_summary
    day = msk_day(now)
    acts = [a for a in recent_activity(engine, 500) if msk_day(a["at"]) == day]
    scan = latest_scan(engine) or {}
    ranking = latest_ranking(engine) or {}
    pf = pf_summary(engine, cfg, now)
    with engine.connect() as c:
        lessons = c.execute(select(S.lessons).order_by(S.lessons.c.id.desc()).limit(10)).all()
        n_hyp = c.execute(select(func.count()).select_from(S.hypotheses)).scalar()
    top = [p for p in scan.get("pools", []) if p.get("score") is not None][:3]
    doc = {
        "day": day, "created_at": now,
        "done": [{"at": a["at"], "kind": a["kind"], "status": a["status"], "message": a["message"]} for a in acts],
        "errors": [a["message"] for a in acts if a["status"] == "error"],
        "opportunities": {t: [o["strategy"] for o in ranking.get("tiers", {}).get(t, [])] for t in
                          ("CONSERVATIVE", "MODERATE", "EXPERIMENTAL")},
        "top_pools": [{"pair": p["pair"], "fee_tier": p["fee_tier"], "width": p["best"]["width"],
                       "net_mid_usd": p["best"]["net_mid_usd"]} for p in top],
        "portfolio": {k: pf[k] for k in ("balance_usd", "pnl_usd", "fees_usd", "il_usd", "costs_usd", "day_change_usd")},
        "lessons": [{"strategy": le.strategy, "reasons": json.loads(le.reasons_json), "action": le.action} for le in lessons],
        "fee_calibration": fee_calibration(engine), "hypotheses_total": n_hyp,
        "research": {k: research.get(k) for k in ("proposed", "tested", "confirmed")} if research else None,
        "next_tasks": (research or {}).get("next_tasks", []),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"daily_{day}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str) + "\n")
    md = [f"# UNISWAP AI LAB — отчёт за {day}", "",
          f"Портфель (виртуальный): баланс ${pf['balance_usd']:,.2f}, итог ${pf['pnl_usd']:+,.2f}", "",
          "## Что система сделала"] + [f"- {datetime.fromtimestamp(a['at'], MSK):%H:%M} [{a['status']}] {a['message']}"
                                       for a in doc["done"][:60]]
    md += ["", "## Лучшие пулы сейчас (ESTIMATED)"] + [
        f"- {t['pair']} {t['fee_tier']:.2%}: диапазон ±{t['width']:.0%}, ожидаемо ${t['net_mid_usd']:,.0f} за 7 дн. на $10k к хранению"
        for t in doc["top_pools"]]
    md += ["", "## Ошибки прогнозов и выводы"] + ([f"- {le['strategy']}: {', '.join(le['reasons'])} → {le['action']}"
                                               for le in doc["lessons"]] or ["- пока нет завершённых проверок прогнозов"])
    if research:
        md += ["", (f"## Исследование: предложено {research.get('proposed')}, проверено {research.get('tested')}, "
                    f"подтверждено {research.get('confirmed')}")]
    md += ["", "## Дальше"] + ([f"- {q['title']}" for q in doc["next_tasks"]] or ["- продолжить наблюдение"])
    path = out_dir / f"daily_{day}.md"
    path.write_text("\n".join(md) + "\n")
    return path


def run_tick(engine, cfg, *, client, ds=None, study=None, version: str | None = None,
             now: int | None = None, report_dir: Path | None = None, allow_daily: bool = True) -> dict:
    """One tick of the routine (steps 1-3; 4-6 once a day). Returns what happened."""
    from backend.lab import portfolio as pf
    from backend.lab.learning import review_forecasts
    from backend.lab.scanner import (
        NETWORK_RU,
        calibrate_fees,
        ingest_all,
        repair_bad_hours,
        save_scan,
        scan,
        scan_pools,
        scanner_clients,
        set_state,
    )
    now = now or int(time.time())
    out: dict = {}
    sc = cfg.lab["scanner"]
    pools = scan_pools(cfg, sc["pools"])
    step(engine, "data", repair_once, engine, cfg)
    for net in step(engine, "data", repair_bad_hours, engine, cfg) or []:
        log_activity(engine, "data", "warn", f"{NETWORK_RU.get(net, net)}: найдено неверное время обменов — "
                     "часовые данные этой сети удалены и загружаются заново")
    nets = {p.network for p in pools}
    fallbacks, errors = scanner_clients(cfg, nets - {"ethereum"}, with_fallbacks=True)
    clients = {n: cs[0] for n, cs in fallbacks.items()}
    clients["ethereum"] = client
    fallbacks["ethereum"] = [client]
    for net, err in errors.items():
        log_activity(engine, "data", "error", f"Сеть {NETWORK_RU.get(net, net)}: нет доступного узла — пулы этой сети "
                     f"сейчас не обновляются ({err[:200]})")
    rs = step(engine, "data", ingest_all, engine, cfg, fallbacks, pools, 480) or []
    for r in rs:
        if r.get("error"):
            log_activity(engine, "data", "error", f"{NETWORK_RU.get(r['network'], r['network'])}: ни один узел не отдал "
                         f"историю обменов — пулы этой сети не обновлены ({r['error'][:220]})", r)
            continue
        n = len([p for p in pools if p.network == r["network"]])
        log_activity(engine, "data", "ok" if r["complete"] else "warn",
                     f"{NETWORK_RU.get(r['network'], r['network'])}: загружено {r['logs']} обменов по {n} пулам "
                     f"(блоки {r['from_block']}–{r['to_block']})"
                     + ("" if r["complete"] else "; не всё — продолжу в следующий раз"), r)
    loading = {r["network"] for r in rs if not r["complete"]}
    res = step(engine, "scan", scan, engine, clients, cfg, pools, now, loading)
    if res is not None:
        save_scan(engine, res)
        for net, err in res.get("network_errors", {}).items():
            log_activity(engine, "scan", "warn", f"Сеть {NETWORK_RU.get(net, net)} не просканирована: {err[:200]}")
        good = [p for p in res["pools"] if p["score"] is not None and p["best"]["net_mid_usd"] > 0]
        log_activity(engine, "scan", "ok", f"Просканировано пулов: {len(res['pools'])} в {len({p['network'] for p in res['pools']})} сетях; "
                     f"с положительной оценкой: {len(good)}"
                     + (f"; лучший {good[0]['pair']} {good[0]['fee_tier']:.2%} ({good[0]['network_ru']})" if good else ""))
    out["scan"] = res is not None
    ranking = latest_ranking(engine)
    strategies = {}
    if ds is not None:
        from backend.signal_engine.engine import load_strategies
        strategies = load_strategies(engine)
    opened = []
    if ranking and strategies:
        if now - ranking["created_at"] <= 3600:   # only a fresh ranking, from its own block
            opened += step(engine, "portfolio", pf.open_from_ranking, engine, cfg, ranking, strategies,
                           ranking["block"], ds.decimals1, now) or []
        else:
            log_activity(engine, "portfolio", "warn", "Рейтинг возможностей устарел (старше часа) — новые "
                         "виртуальные позиции по нему не открываются")
    if res is not None:
        opened += step(engine, "portfolio", pf.open_from_scan, engine, cfg, res, {cfg.primary_pool}, 2, now) or []
    for o in opened:
        log_activity(engine, "portfolio", "info", f"Открыта виртуальная позиция: {o}")
    if res is not None:
        usd_now = {"ETH": res["eth_usd"], "BTC": res["btc_usd"]}
        upd = step(engine, "portfolio", pf.update_positions, engine, cfg, ranking,
                   {p.key: p for p in pools}, usd_now, now)
        for c in (upd or {}).get("closed", []):
            log_activity(engine, "portfolio", "info", f"Закрыта виртуальная позиция: {c}")
    s = step(engine, "portfolio", pf.summary, engine, cfg, now)
    if s is not None:
        pf.snapshot_day(engine, s, now)
        out["portfolio"] = {k: s[k] for k in ("balance_usd", "pnl_usd")}
    if res is not None:   # «Мой бот»: the user's virtual twin with its own small capital
        from backend.lab import bot
        with engine.connect() as c:
            hrs = c.execute(select(S.pool_hours).where(S.pool_hours.c.hour_ts >= now - 40 * 86_400)
                            .order_by(S.pool_hours.c.hour_ts)).all()
        by_pool: dict = {}
        for h in hrs:
            by_pool.setdefault(h.pool, []).append(h)
        bt = step(engine, "bot", bot.tick, engine, cfg, res, {p.key: p for p in pools}, by_pool, now)
        from backend.lab import (
            tracking,  # positions the user chose ("Выбрать" -> GitHub issue)
        )
        tr = step(engine, "tracking", tracking.sync, engine, cfg, res, {p.key: p for p in pools}, by_pool, None, now)
        if tr:
            out["tracking"] = tr
        bs = step(engine, "bot", bot.summary, engine, cfg, now)
        if bs is not None:
            bot.snapshot_day(engine, bs, now)
            out["bot"] = {"action": (bt or {}).get("action"), "balance_usd": round(bs["balance_usd"], 2)}
    rv = step(engine, "learning", review_forecasts, engine, now)
    for le in (rv or {}).get("lessons", []):
        log_activity(engine, "learning", "info", f"Проверен прогноз {le['strategy']}: "
                     f"{', '.join(le['reasons']) or 'в пределах ожиданий'} → {le['action']}")
    if allow_daily and ds is not None and study is not None and daily_due(engine, now):
        from backend.backtesting.dataset import cached_state_at
        cal = step(engine, "calibration", calibrate_fees, engine, cfg, ds, lambda b: cached_state_at(ds, b))
        if cal and cal.get("factor"):
            set_state(engine, "scanner_fee_calibration", cal)
            log_activity(engine, "calibration", "ok", f"Калибровка сканера: множитель комиссий {cal['factor']:.2f} "
                         "(часовая оценка против точной модели WBTC/USDC 0,3%)", cal)
        research = step(engine, "research", daily_research, engine, cfg, ds, study, version)
        if research is not None:
            set_state(engine, "next_tasks", {"items": research.get("next_tasks", []), "day": msk_day(now)}, now)
            log_activity(engine, "research", "ok", f"Исследование дня: предложено гипотез {research['proposed']}, "
                         f"проверено {research['tested']}, подтверждено {research['confirmed']}",
                         {"cycle": research["cycle_id"], "next": research.get("next_tasks")})
        path = step(engine, "report", daily_report, engine, cfg, now, research, report_dir or Path("data_snapshots/reports"))
        if path is not None:
            log_activity(engine, "report", "ok", f"Ежедневный отчёт: {path.name}")
        set_state(engine, "daily_research", {"day": msk_day(now)}, now)
        out["daily"] = True
    return out
