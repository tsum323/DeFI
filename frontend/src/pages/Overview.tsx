import { Link } from "react-router-dom";
import type { Labeled } from "../api";
import { Card, Loading, Pill, Stat } from "../components/ui";
import { useApi } from "../components/useApi";
import { date, pct, pctAbs } from "../format";

interface Overview {
  experiments: Labeled<number>; strategies_tested: Labeled<number>;
  best_oos: Labeled<{ name: string; oos_roi: number; oos_vs_passive: number } | null>;
  mean_net_roi: Labeled<number | null>; max_drawdown: Labeled<number | null>;
  ruined_strategies: Labeled<number>; total_fees_per_segment_mean: Labeled<number | null>;
  promising_strategies: Labeled<number>; promising_names: string[];
  active_signals: Labeled<number>; signals_total: Labeled<number>;
  last_decision: Labeled<string | null>; last_decision_at: number | null;
  ai_researcher: { hypotheses: number; last_cycle: string | null; last_cycle_at: number | null;
    author: string | null; api_key_configured: boolean };
}

const DECISION_RU: Record<string, string> = { WAIT: "ЖДАТЬ", "OPEN CANDIDATE": "КАНДИДАТ НА ОТКРЫТИЕ",
  HOLD: "ДЕРЖАТЬ", REBALANCE: "ПЕРЕСТАВИТЬ", "CLOSE CANDIDATE": "КАНДИДАТ НА ЗАКРЫТИЕ", INVALIDATED: "ОТМЕНЁН" };

export default function OverviewPage() {
  const { data: d, error } = useApi<Overview>("/api/overview");
  if (!d) return <Loading error={error} />;
  const ai = d.ai_researcher;
  return (
    <>
      <h1>Главная</h1>
      <p className="lead">Исследование стратегий ликвидности WBTC/USDC 0,3% (Uniswap v3, Ethereum).
        Только чтение блокчейна — система ничего не исполняет.</p>
      <div className="grid stats">
        <Stat title="Экспериментов" label={d.experiments.label} value={d.experiments.value.toLocaleString("ru-RU")}
          hint="стратегия × отрезок × вариант" />
        <Stat title="Проверено стратегий" label={d.strategies_tested.label} value={d.strategies_tested.value} />
        <Stat title="Лучший out-of-sample" label={d.best_oos.label}
          value={pct(d.best_oos.value?.oos_roi)} tone="pos"
          hint={<>{d.best_oos.value?.name} · к пассивному {pct(d.best_oos.value?.oos_vs_passive)} за отрезок</>} />
        <Stat title="Средняя чистая доходность" label={d.mean_net_roi.label} value={pct(d.mean_net_roi.value)}
          tone={(d.mean_net_roi.value ?? 0) < 0 ? "neg" : "pos"} hint="среднее по стратегиям, за 91-дневный отрезок" />
        <Stat title="Макс. просадка" label={d.max_drawdown.label} value={pctAbs(d.max_drawdown.value)} tone="neg"
          hint={`среди не разорившихся; разорились: ${d.ruined_strategies.value}`} />
        <Stat title="Комиссии (среднее)" label={d.total_fees_per_segment_mean.label}
          value={pctAbs(d.total_fees_per_segment_mean.value)} hint="от капитала за отрезок, модель A" />
        <Stat title="Перспективных стратегий" label={d.promising_strategies.label} value={d.promising_strategies.value}
          hint="лучше пассивного LP и на OOS (≥5/7), и на IS" />
        <Stat title="Актуальных сигналов" label={d.active_signals.label} value={d.active_signals.value}
          hint={<>последнее решение: <b>{DECISION_RU[d.last_decision.value ?? ""] ?? "—"}</b> · {date(d.last_decision_at)}</>} />
        <Stat title="AI-исследователь" value={ai.hypotheses ? `${ai.hypotheses} гипотез` : "нет циклов"}
          hint={<>{ai.last_cycle ? <>последний цикл {date(ai.last_cycle_at)} · {ai.author}</> : "—"}<br />
            {ai.api_key_configured ? <Pill tone="pos">ключ API подключён</Pill> : <Pill tone="warn">без ключа API — гипотезы из файла</Pill>}</>} />
      </div>
      <Card title="Главный вывод на сейчас" label="MODELED">
        <p>После всех расходов устойчивого преимущества активных стратегий над пассивной широкой позицией не найдено;
          сигнал — <b>{DECISION_RU[d.last_decision.value ?? ""] ?? "—"}</b>. Перспективные кандидаты поставлены на проверку
          на новых данных (<Link to="/portfolio">виртуальный портфель</Link>).</p>
        <div className="chips">{d.promising_names.map((n) => <Pill key={n}>{n}</Pill>)}</div>
      </Card>
    </>
  );
}
