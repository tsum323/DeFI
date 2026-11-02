// Current LP opportunities (SCENARIO): ranked in three tiers from the stored historical
// study plus the current market; diagnostics explain what keeps each one lower.
import { useState } from "react";
import { Badge, Card, Loading, Pill } from "../components/ui";
import { useApi } from "../components/useApi";
import { date, num, pct, pctAbs, sign, usd } from "../format";

interface Opp {
  tier: string; strategy: string; spec_hash: string; pair: string; pool_address: string; network: string;
  fee_tier: number; current_price: number; rebalance_rule: string; horizon_days: number;
  range: { price_lower: number; price_upper: number };
  allocation: { token0: number; token1: number; usdc_value_share: number | null; deposit_usd: number };
  expected_fees_usd: [number, number]; expected_il_usd: number;
  costs_usd: { gas_open: number; gas_close: number; swap: number; total: number };
  expected_net_vs_hodl_usd: [number, number]; expected_net_mid_usd: number; expected_net_mid_pct: number | null;
  exit_probability: number | null; risk: string; risk_why: string; confidence: string;
  historical: { robustness_score: number | null; oos_mean_excess_vs_passive: number | null;
    oos_positive_segments: number; oos_mean_roi: number | null; is_mean_excess: number | null;
    max_drawdown: number; mean_drawdown: number; oos_time_out_of_range: number | null };
  reasons: string[]; blockers_ru: string[]; to_improve_ru: string[]; perspective_score: number | null;
  oos_contaminated: boolean; variants: { strategy: string; rebalance_rule: string }[];
}
interface Check { name: string; passed: boolean; value: any; threshold: string; ru: string }
interface Diag { strategy: string; tier: string | null; blockers_ru: string[]; oos_mean_excess: number | null;
  robustness_score: number | null; expected_net_mid_usd?: number }
interface Ranking { created_at: number; block: number; market: Record<string, any>; tiers: Record<string, Opp[]>;
  market_checks: Check[]; diagnostics: Diag[]; counts: Record<string, number> }
interface Ev { id: number; created_at: number; strategy: string; tier_from: string | null; tier_to: string | null }
interface Data { ranking: Ranking | null; events: Ev[] }

const TIER_INFO: Record<string, { ru: string; text: string; tone: string }> = {
  CONSERVATIVE: { ru: "CONSERVATIVE — проверенные", tone: "pos",
    text: "Прошли все исторические проверки (как строгий сигнал), чистый out-of-sample, положительный ожидаемый результат после расходов." },
  MODERATE: { ru: "MODERATE — умеренные", tone: "warn",
    text: "Честно лучше пассивного размещения на «невиденных» отрезках, выдерживают газ ×2, допустимая просадка; не хватает стабильности или совпадения с текущим рынком." },
  EXPERIMENTAL: { ru: "EXPERIMENTAL — экспериментальные", tone: "neg",
    text: "Интересные, но не подтверждённые: параметры подобраны после просмотра результатов или мало истории. Только для проверки в виртуальном портфеле." },
};
const RULE_RU: Record<string, string> = { none: "без перестановок", on_exit: "переставлять при выходе цены",
  periodic: "переставлять по расписанию", edge: "переставлять у края диапазона" };
const RISK_RU: Record<string, string> = { low: "низкий", medium: "средний", high: "высокий" };
const CONF_RU: Record<string, string> = { high: "высокая", medium: "средняя", low: "низкая" };
const RISK_N: Record<string, number> = { low: 0, medium: 1, high: 2 };
type SortKey = "score" | "net" | "risk";

function OppCard({ o }: { o: Opp }) {
  const short = `${o.pool_address.slice(0, 6)}…${o.pool_address.slice(-4)}`;
  return (
    <div className="card opp">
      <div className="card-head">
        <h2>{o.pair} · {pctAbs(o.fee_tier, 2)} · {o.strategy}</h2>
        <div className="card-right">
          <Pill tone={TIER_INFO[o.tier]?.tone}>{o.tier}</Pill>
          <Pill tone={o.risk === "low" ? "pos" : o.risk === "medium" ? "warn" : "neg"}>риск: {RISK_RU[o.risk]}</Pill>
          <Pill>достоверность: {CONF_RU[o.confidence]}</Pill>
          {o.perspective_score != null && <Pill>оценка {num(o.perspective_score, 1)}</Pill>}
        </div>
      </div>
      <p className="muted">Пул <code title={o.pool_address}>{short}</code> ({o.network}) · цена сейчас <b>{num(o.current_price, 0)} USDC</b> · {RULE_RU[o.rebalance_rule] ?? o.rebalance_rule}</p>
      <div className="grid two">
        <div>
          <p><b>Диапазон:</b> {num(o.range.price_lower, 0)} – {num(o.range.price_upper, 0)} USDC</p>
          <p><b>Внести:</b> {num(o.allocation.token0, 6)} WBTC + {num(o.allocation.token1, 2)} USDC
            {o.allocation.usdc_value_share != null && <> (USDC {pctAbs(o.allocation.usdc_value_share, 0)})</>}</p>
          <p><b>За {o.horizon_days} дн.</b> <Badge label="SCENARIO" /><br />
            комиссии {usd(o.expected_fees_usd[0])} – {usd(o.expected_fees_usd[1])} · IL при ±1σ {usd(o.expected_il_usd)} ·
            расходы {usd(o.costs_usd.gas_open + o.costs_usd.gas_close + o.costs_usd.swap, 2)}<br />
            <b>итог к HODL: <span className={sign(o.expected_net_mid_usd)}>{usd(o.expected_net_vs_hodl_usd[0])} … {usd(o.expected_net_vs_hodl_usd[1])}</span></b>
            {o.expected_net_mid_pct != null && <> (≈ {pct(o.expected_net_mid_pct, 2)} на капитал)</>}</p>
          <p><b>Риск:</b> {o.risk_why}</p>
        </div>
        <div>
          <p><b>История</b> <Badge label="MODELED" />: оценка устойчивости {num(o.historical.robustness_score != null ? o.historical.robustness_score * 100 : null, 0)}/100 ·
            OOS к пассивному {pct(o.historical.oos_mean_excess_vs_passive)} ({o.historical.oos_positive_segments}/7 отрезков) ·
            OOS доходность {pct(o.historical.oos_mean_roi)} за 91 день · на отрезках подбора {pct(o.historical.is_mean_excess)} ·
            вне диапазона {pctAbs(o.historical.oos_time_out_of_range, 0)} времени · просадка ср. {pctAbs(o.historical.mean_drawdown, 0)}
            {o.oos_contaminated && <span className="warn"> · OOS не чистый</span>}</p>
          <h3>Почему в списке</h3>
          <ul>{o.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          {o.blockers_ru.length > 0 && <><h3>Что мешает более высокому рейтингу</h3>
            <ul>{o.blockers_ru.map((r) => <li key={r} className="warn">{r}</li>)}</ul></>}
          {o.to_improve_ru.length > 0 && <><h3>Что должно измениться</h3>
            <ul>{o.to_improve_ru.map((r) => <li key={r}>{r}</li>)}</ul></>}
          {o.variants.length > 0 && <p className="muted">Та же позиция сейчас у: {o.variants.map((v) => v.strategy).join(", ")}</p>}
        </div>
      </div>
    </div>);
}

export default function Opportunities() {
  const { data, error } = useApi<Data>("/api/opportunities");
  const [sort, setSort] = useState<SortKey>("score");
  if (!data) return <Loading error={error} />;
  const r = data.ranking;
  if (!r) return <Card title="Актуальные возможности"><p className="muted">Рейтинг ещё не рассчитан.</p></Card>;
  const key = (o: Opp) => sort === "net" ? -o.expected_net_mid_usd : sort === "risk" ? RISK_N[o.risk] * 1000 - (o.perspective_score ?? 0)
    : -(o.perspective_score ?? 0);
  const m = r.market;
  return (
    <>
      <h2 className="section">Актуальные возможности <Badge label="SCENARIO" /></h2>
      <p className="lead">Рассчитано {date(r.created_at)} по уже сохранённому историческому исследованию (2021–2026, без пересчёта)
        и текущему состоянию пула. Это не сигналы на открытие: категории показывают, насколько вариант проверен.
        Лучшие из каждой категории автоматически ведутся в «Виртуальном портфеле».</p>
      <Card title="Рынок сейчас" right={<Badge label="EXACT" />}>
        <p>Цена {num(m.price, 0)} USDC · объём 24ч {usd(m.volume_24h_usd)} · 7д {usd(m.volume_7d_usd)} · TVL {usd(m.tvl_usd)} ·
          активная ликвидность ±2% {usd(m.active_liquidity_usd_2pct)} · волатильность 7д {pctAbs(m.vol_7d, 0)} / 30д {pctAbs(m.vol_30d, 0)} ·
          доходность комиссий 7д {pctAbs(m.fee_apr_7d, 1)} годовых · газ {num(m.gas_gwei, 2)} gwei · режим {m.trend}/{m.vol_class}</p>
      </Card>
      <div className="card-right" style={{ marginBottom: 12 }}>
        <span className="muted">Сортировка:</span>
        {([["score", "перспективность"], ["net", "ожидаемый итог"], ["risk", "риск"]] as const).map(([k, t]) =>
          <button key={k} className={`pill${sort === k ? " warn" : ""}`} onClick={() => setSort(k)}>{t}</button>)}
      </div>
      {Object.keys(TIER_INFO).map((t) => {
        const opps = [...(r.tiers[t] ?? [])].sort((a, b) => key(a) - key(b));
        return (
          <div key={t}>
            <h2 className="section">{TIER_INFO[t].ru}: {opps.length}</h2>
            <p className="muted">{TIER_INFO[t].text} Кандидатов по истории: {r.counts[t] ?? 0}.</p>
            {opps.length === 0 ? <p className="muted">Сейчас нет вариантов этой категории.</p> :
              opps.map((o) => <OppCard key={o.spec_hash} o={o} />)}
          </div>);
      })}
      <h2 className="section">Signal Diagnostics</h2>
      <Card title="Проверки рынка">
        <ul>{r.market_checks.map((c) => <li key={c.name} className={c.passed ? "" : "warn"}>
          {c.passed ? "✓" : "✗"} {c.passed ? c.name : c.ru}: {typeof c.value === "number" ? num(c.value, 4) : String(c.value)} (нужно {c.threshold})</li>)}</ul>
      </Card>
      <Card title="Почему стратегии не получают высокий рейтинг">
        <div className="table-wrap"><table className="table"><thead><tr><th>Стратегия</th><th>Категория</th><th>Причины</th>
          <th>OOS к пассивному</th><th>Устойчивость</th></tr></thead>
          <tbody>{r.diagnostics.map((d) => (
            <tr key={d.strategy}><td>{d.strategy}</td><td>{d.tier ?? "вне рейтинга"}</td>
              <td>{d.blockers_ru.join("; ")}{d.expected_net_mid_usd != null && ` (ожидаемо ${usd(d.expected_net_mid_usd)})`}</td>
              <td>{pct(d.oos_mean_excess, 2)}</td><td>{d.robustness_score != null ? num(d.robustness_score * 100, 0) : "—"}</td></tr>))}</tbody></table></div>
      </Card>
      <Card title="История изменений рейтинга">
        {data.events.length === 0 ? <p className="muted">Изменений пока нет.</p> :
          <ul>{data.events.slice(0, 30).map((e) => <li key={e.id}>{date(e.created_at)} · {e.strategy}: {e.tier_from ?? "—"} → {e.tier_to ?? "выбыла"}</li>)}</ul>}
      </Card>
    </>);
}
