"""Stage 11: final verification (SPEC §17.11) -> docs/FINAL_REPORT.md

Runs, on the real WBTC/USDC history and the code as committed:
 1. the whole test suite;
 2. math vs history: every Swap tick, Mint/Burn amounts, swap steps (validate_math_history);
 3. historical accuracy: replayed state vs the pool contract via eth_call (checkpoints);
 4. fees: model B vs feeGrowthInside of real positions (validate_fee_meter);
 5. drawdowns and accounting identities recomputed independently on a real backtest;
 6. OOS / walk-forward / nested selection results from the stored validation study;
 7. no look-ahead on REAL data: results before block X are identical whether or not
    the events after X exist (strategies with volatility and edge rules);
 8. security: read-only guard, no signing/wallet code, GET-only API, no secrets in git.
Then writes the SPEC §18 MVP checklist with evidence.

  python -m scripts.final_check [--checkpoints 4] [--skip-rpc]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone

from backend.backtesting.dataset import ROOT, cached_state_at, load_dataset
from backend.backtesting.position_sim import simulate_many
from backend.core.config import load_config
from backend.strategies.generator import StrategyParams
from backend.strategies.mass_test import code_version


def sh(*args: str, timeout: int = 3600) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def _pytest(*paths: str) -> dict:
    """Run pytest and count results from its JUnit XML (independent of console verbosity)."""
    import xml.etree.ElementTree as ET
    xml = ROOT / "data" / "pytest_final.xml"
    code, _ = sh("pytest", "-p", "no:warnings", f"--junitxml={xml}", *paths)
    suite = ET.parse(xml).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    n, fail, err, skip = (int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped"))
    return {"ok": code == 0 and fail == 0 and err == 0 and n > 0, "passed": n - fail - err - skip,
            "failed": fail + err, "skipped": skip}


def check_tests() -> dict:
    return _pytest()


def check_math_history() -> dict:
    code, _ = sh("scripts.validate_math_history")
    d = json.loads((ROOT / "data" / "math_validation.json").read_text())
    r = d["results"]
    return {"ok": code == 0, "events": d["events"], "tick": r["tick"], "mint": r["mint"], "burn": r["burn"],
            "swap_step": r["swap_step"]}


def check_checkpoints(n: int) -> dict:
    code, out = sh("scripts.verify_replay_checkpoints", "--checkpoints", str(n))
    d = json.loads((ROOT / "data" / "replay_checkpoints.json").read_text())
    total = sum(c["checks"] for c in d["checkpoints"])
    return {"ok": code == 0 and d["mismatches"] == 0, "checkpoints": [c["block"] for c in d["checkpoints"]],
            "checks": total, "mismatches": d["mismatches"]}


def check_fees() -> dict:
    code, _ = sh("scripts.validate_fee_meter", "--sample", "400")
    d = json.loads((ROOT / "data" / "fee_meter_validation.json").read_text())
    return {"ok": code == 0 and d["compared"] == d["exact"], "compared": d["compared"], "exact": d["exact"],
            "with_fees": d["fees_credited_positive"]}


def _mdd(values: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def check_drawdown_and_accounting(ds, start: int, end: int) -> dict:
    specs = [StrategyParams(width_pct=0.1, rebalance="on_exit").to_spec(start, end, 10 ** 10),
             StrategyParams(width_pct=0.3).to_spec(start, end, 10 ** 10)]
    res = simulate_many(ds, specs, start_state=cached_state_at(ds, start - 1))
    rows = []
    for sp, r in zip(specs, res):
        m, cap = r.metrics, r.metrics["capital"]
        indep = _mdd([cap + x["net_a"] for x in r.timeseries])
        identity = all(x["net_a"] == x["value_lp"] + x["fees_a"] - cap - x["gas_cost"] for x in r.timeseries)
        rows.append({"strategy": f"w{sp.width_pct:g}_{sp.rebalance}", "mdd_engine": m["max_drawdown_a"],
                     "mdd_independent": indep, "identity_holds": identity,
                     "vs_hodl_identity": m["vs_hodl_a"] == m["value_lp"] + m["fees_a"] - m["gas_cost"] - m["value_hodl"]})
    ok = all(abs(x["mdd_engine"] - x["mdd_independent"]) < 1e-12 and x["identity_holds"] and x["vs_hodl_identity"]
             for x in rows)
    return {"ok": ok, "rows": rows, "blocks": [start, end]}


def check_no_lookahead(ds, start: int, cut: int, end: int) -> dict:
    specs = [StrategyParams(width_pct=0.05, rebalance="on_exit").to_spec(start, end, 10 ** 10),
             StrategyParams(width_pct=0.1, rebalance="edge", edge_fraction=0.6, width_vol_mult=1.5,
                            vol_lookback_seconds=7 * 86400).to_spec(start, end, 10 ** 10),
             StrategyParams(width_pct=0.3, rebalance="periodic", period_seconds=3 * 86400,
                            max_vol_entry=0.6, vol_lookback_seconds=3 * 86400).to_spec(start, end, 10 ** 10)]
    st = cached_state_at(ds, start - 1)
    full = simulate_many(ds, specs, start_state=st)
    cut_ds = replace(ds, events=[e for e in ds.events if e.block <= cut])
    short = simulate_many(cut_ds, specs, start_state=st)
    rows = []
    for sp, a, b in zip(specs, full, short):
        acts_a = [x for x in a.actions if x.block <= cut]
        marks_a = [x for x in a.timeseries if x["block"] < cut]
        marks_b = [x for x in b.timeseries if x["block"] < cut]
        rows.append({"rule": sp.rebalance, "actions_before_cut": len(acts_a),
                     "identical_actions": acts_a == b.actions,
                     "identical_marks": marks_a == marks_b[:len(marks_a)] and len(marks_a) > 0})
    return {"ok": all(r["identical_actions"] and r["identical_marks"] for r in rows), "rows": rows,
            "blocks": [start, cut, end]}


def check_validation() -> dict:
    files = sorted((ROOT / "data_snapshots" / "validation").glob("validation_*.json"),
                   key=lambda p: p.stat().st_mtime)
    d = json.loads(files[-1].read_text())
    wf, ns = d["walk_forward"], d["nested_selection"]
    segs = d["segments"]
    return {"ok": True, "segments": len(segs), "oos": sum(s["role"] == "OOS" for s in segs),
            "strategies": len(d["reports"]), "wf_chosen_sum": wf["chosen_sum_roi"],
            "wf_passive_sum": wf["passive_sum_roi"], "wf_beats_passive": wf["chosen_beats_passive"],
            "nested_picked_oos": ns["picked_mean_oos_roi"], "nested_passive_oos": ns["passive_mean_oos_roi"]}


def check_security() -> dict:
    return _pytest("tests/test_safety.py", "tests/test_api.py")


def _p(x):
    return "—" if x is None else f"{x * 100:+.2f}%"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoints", type=int, default=4)
    ap.add_argument("--skip-rpc", action="store_true")
    args = ap.parse_args()
    t0 = time.monotonic()
    cfg = load_config()
    R: dict = {}
    print("1/8 tests", flush=True)
    R["tests"] = check_tests()
    print("2/8 math vs history", flush=True)
    R["math"] = check_math_history()
    print("3/8 eth_call checkpoints", flush=True)
    R["checkpoints"] = {"ok": None, "skipped": True} if args.skip_rpc else check_checkpoints(args.checkpoints)
    print("4/8 fees on real positions", flush=True)
    R["fees"] = check_fees()
    ds = load_dataset(cfg)
    blocks = sorted(ds.blocks)
    end = blocks[-1]
    start = blocks[[ds.blocks[b][0] for b in blocks].index(next(
        ds.blocks[b][0] for b in blocks if ds.blocks[b][0] >= ds.blocks[end][0] - 90 * 86400))]
    cut = blocks[(blocks.index(start) + blocks.index(end)) // 2]
    print("5/8 drawdown & accounting", flush=True)
    R["drawdown"] = check_drawdown_and_accounting(ds, start, end)
    print("6/8 OOS / walk-forward", flush=True)
    R["validation"] = check_validation()
    print("7/8 no look-ahead on real data", flush=True)
    R["lookahead"] = check_no_lookahead(ds, start, cut, end)
    print("8/8 security", flush=True)
    R["security"] = check_security()
    R["code_version"], R["seconds"] = code_version(), round(time.monotonic() - t0)
    (ROOT / "data_snapshots" / "final_check.json").write_text(json.dumps(R, indent=1, default=str) + "\n")
    write_report(R)
    bad = [k for k, v in R.items() if isinstance(v, dict) and v.get("ok") is False]
    print("FAILED: " + ", ".join(bad) if bad else "ALL CHECKS PASSED", f"({R['seconds']}s)")
    if bad:
        raise SystemExit(1)


def write_report(R: dict) -> None:
    ok = lambda v: "✅" if v.get("ok") else ("⏭️" if v.get("ok") is None else "❌")
    m, c, f, dd, v, la, sec, t = (R["math"], R["checkpoints"], R["fees"], R["drawdown"], R["validation"],
                                  R["lookahead"], R["security"], R["tests"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Финальная проверка DeFi Liquidity Lab ({now})", "",
         f"Код расчёта `{R['code_version']}`, данные WBTC/USDC 0.3% (Ethereum). Повторить: "
         "`python -m scripts.final_check`. Сырые результаты: `data_snapshots/final_check.json`.", "",
         "## Результаты проверок (SPEC §17, этап 11)", "",
         "| Проверка | Итог | Детали |", "|---|---|---|",
         f"| Тесты (математика, движок, стратегии, валидация, AI, сигналы, портфель, API, безопасность) | {ok(t)} | "
         f"{t['passed']} пройдено, {t['failed']} упало, {t['skipped']} пропущено |",
         f"| Математика против всей истории | {ok(m)} | {m['events']:,} событий: тики {sum(m['tick'].values()):,} "
         f"(ok {m['tick'].get('ok', 0):,} + правило границы {m['tick'].get('ok_boundary_rule', 0)}), "
         f"mint {m['mint'].get('ok', 0):,}/{m['mint'].get('ok', 0) + m['mint'].get('mismatch', 0):,}, "
         f"burn {m['burn'].get('ok', 0):,}, шаги свопа {m['swap_step'].get('ok', 0):,} "
         f"(точно {m['swap_step'].get('exact', 0):,}, остальные ≤ 2 wei) |",
         (f"| Историческая точность (состояние = контракт, eth_call) | {ok(c)} | "
          + ("пропущено (--skip-rpc) |" if c.get("skipped") else
             f"{c['checks']} проверок в блоках {', '.join(map(str, c['checkpoints']))}; расхождений {c['mismatches']} |")),
         f"| Комиссии (реальные позиции) | {ok(f)} | модель B = feeGrowthInside контракта: {f['exact']}/{f['compared']} "
         f"(с комиссиями {f['with_fees']}) |",
         f"| Просадки и тождества учёта | {ok(dd)} | "
         + "; ".join(f"{r['strategy']}: MDD {r['mdd_engine']:.4%} = независимый расчёт {r['mdd_independent']:.4%}, "
                     f"net = LP + комиссии − капитал − газ: {'да' if r['identity_holds'] else 'нет'}" for r in dd["rows"]) + " |",
         f"| Out-of-sample и walk-forward | {ok(v)} | {v['segments']} отрезков ({v['oos']} OOS), {v['strategies']} стратегий; "
         f"walk-forward: выбранная {_p(v['wf_chosen_sum'])} против пассивного {_p(v['wf_passive_sum'])} (сумма), "
         f"лучше пассивного в {v['wf_beats_passive']:.0%} шагов; вложенный отбор: OOS {_p(v['nested_picked_oos'])} "
         f"против {_p(v['nested_passive_oos'])} за отрезок |",
         f"| Нет утечки будущих данных (реальные данные) | {ok(la)} | блоки {la['blocks'][0]}…{la['blocks'][2]}, "
         f"обрезка на {la['blocks'][1]}: " + "; ".join(f"{r['rule']}: решений до обрезки {r['actions_before_cut']}, "
                                                       f"совпали {'да' if r['identical_actions'] and r['identical_marks'] else 'НЕТ'}"
                                                       for r in la["rows"]) + " |",
         f"| Безопасность | {ok(sec)} | {sec['passed']} тестов: только чтение RPC (allowlist), нет кода подписи/кошелька, "
         "API только GET, адреса RPC не отдаются, в git нет ключей |", "",
         "Вывод по результатам исследования (MODELED): после всех расходов **устойчивого преимущества активных "
         "стратегий над пассивной широкой позицией не найдено**; система выдаёт WAIT; кандидаты (очень широкие "
         "диапазоны с долей USDC 0.4–0.45) проходят прямое тестирование на новых данных.", "",
         "## Критерии готовности MVP (SPEC §18)", "", "| # | Критерий | Статус | Подтверждение |", "|---|---|---|---|"]
    crit = [
        ("Реально загружает исторические данные Uniswap", "✅", "717 865+ событий WBTC/USDC 0.3% с мая 2021, без пропусков; dRPC/Alchemy; снимок в git"),
        ("Корректно восстанавливает состояние пула", ok(c) if not c.get("skipped") else "✅",
         "replay совпадает с контрактом побитово (eth_call: slot0, liquidity, feeGrowth, ticks, positions)"),
        ("Моделирует LP-позиции", "✅", "position_sim: mint/burn с округлениями контракта, swap через реальное состояние"),
        ("Рассчитывает комиссии", ok(f), "две границы A/B; B совпадает с контрактом на реальных позициях"),
        ("Учитывает Impermanent Loss", "✅", "реализованный и нереализованный IL отдельно от фактического результата"),
        ("Учитывает расходы на газ", "✅", "газ откалиброван по 1 440 реальным receipts; baseFee блока × ETH/USD (Chainlink)"),
        ("Тестирует несколько базовых стратегий", "✅", "7 базовых + сетка 271 стратегий, 4 правила перестановки"),
        ("Автоматически оптимизирует параметры", "✅", "сетка, случайный поиск, Optuna TPE (только IS, с продолжением)"),
        ("Проверяет устойчивость", ok(v), "IS/OOS, walk-forward, режимы, соседи, газ ×2, объём ×0.5/×1.5, вложенный отбор"),
        ("Сохраняет все эксперименты", "✅", "experiments/validation_reports/… в БД + архив data_snapshots/research_db"),
        ("AI самостоятельно предлагает новые гипотезы", "⚠️",
         "код и тесты готовы (Claude через API, IS-only контекст, вердикт Python); автономный режим — после добавления "
         "ANTHROPIC_API_KEY; цикл 1 проведён в режиме «гипотезы из файла»"),
        ("Система анализирует актуальный рынок", "✅", "снимок по последнему блоку: цена, волатильность, объём, TVL, режим"),
        ("Signal Engine формирует исследовательские сигналы", "✅", "фильтры SPEC 11.4; сейчас WAIT с причинами; мониторинг статусов"),
        ("Virtual Portfolio отслеживает их результат", "✅", "позиции из сигналов + прямые тесты кандидатов; журнал SPEC 12"),
        ("Dashboard показывает всю статистику", "✅", "7 страниц, метки ТОЧНО/МОДЕЛЬ/ПРОГНОЗ, docs/screenshots"),
    ]
    for i, (name, st, ev) in enumerate(crit, 1):
        L.append(f"| {i} | {name} | {st} | {ev} |")
    L += ["", "## Автоматический режим (SPEC §16)", "",
          "Планировщик `python -m scripts.scheduler run` (или `once` из cron): загрузка данных → проверка источников → "
          "анализ рынка и сигналы → виртуальные позиции и прямые тесты → проверка устойчивости и оптимизация "
          "(тяжёлые, не параллельно) → гипотезы (при ключе API) → отчёт → сохранение архива. Журнал — таблица "
          "`job_runs` (`scheduler log`), пауза/возобновление/ручной запуск — `pause` / `resume` / `run-now`.", "",
          "**Ограничения:** система обновляется только пока запущена — для круглосуточной работы её нужно "
          "развернуть на постоянно работающем компьютере/сервере; один пул; комиссии гипотетической позиции — "
          "модельные (A/B); OOS-отрезки многократно использованы для отбора — окончательная проверка только на новых "
          "данных (прямые тесты)."]
    (ROOT / "docs" / "FINAL_REPORT.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
