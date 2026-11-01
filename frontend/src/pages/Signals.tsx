import { Badge, Card, Loading, Pill } from "../components/ui";
import { useApi } from "../components/useApi";
import { date, num, pct, sign, usd } from "../format";

interface Ev { at: number; block: number; status: string; reason: string }
interface Sig { id: number; created_at: number; block: number; type: string; status: string; history: Ev[];
  payload: Record<string, any> }
interface WatchItem { strategy: string; spec_hash: string; failed: string; value: any; threshold: string;
  oos_mean_excess: number | null; oos_mean_roi: number | null; oos_contaminated: boolean }
interface WatchTest { id: number; name: string; start_block: number; created_at: number; reason: string;
  on_list_now: boolean; result: any }
interface Signals { signals: Sig[]; active: Sig[];
  watch?: { current: WatchItem[]; evaluated_at: number | null; tests: WatchTest[] } }
// Plain-language names of the quality checks (signal engine, SPEC 11.4).
export const CHECK_RU: Record<string, string> = {
  data_current: "данные свежие (не старше 90 мин)",
  pool_activity: "в пуле достаточно торгов (от $100 000 за сутки)",
  market_data_complete: "есть все рыночные данные",
  sufficient_history: "есть результаты на всех отрезках истории",
  oos_beats_passive: "на «невиденных» отрезках лучше пассивного размещения (≥ 5 из 7)",
  oos_positive_roi: "на «невиденных» отрезках в плюсе",
  stable_across_periods: "работала и на отрезках подбора (результат не случайный)",
  acceptable_drawdown: "средняя просадка не больше 30%",
  costs_included_robust: "в плюсе даже при газе ×2, без разорений",
  fits_current_regime: "раньше зарабатывала при таком же рынке, как сейчас",
  no_validated_strategy: "ни одна стратегия не прошла все проверки",
};
const ru = (k: string) => CHECK_RU[k] ?? k;

function Watch({ w }: { w: NonNullable<Signals["watch"]> }) {
  return (
    <Card title={`Наблюдение: ${w.current.length}`} right={<Badge label="MODELED" />}>
      <p className="muted">Стратегии, которым до сигнала не хватает одной проверки. Это <b>не сигнал</b> — система
        ведёт их на виртуальных деньгах с момента попадания в список, чтобы проверить на новых данных.
        Реальные деньги не участвуют.</p>
      {w.current.length === 0 ? <p className="muted">Сейчас таких стратегий нет.</p> : (
        <div className="table-wrap"><table className="table"><thead><tr><th>Стратегия</th><th>Чего не хватает</th>
          <th>OOS к пассивному</th><th>OOS ROI</th></tr></thead>
          <tbody>{w.current.map((c) => (
            <tr key={c.spec_hash}><td>{c.strategy}{c.oos_contaminated && <div className="muted">новый поиск: OOS не чистый</div>}</td>
              <td>{ru(c.failed)}</td><td>{pct(c.oos_mean_excess, 2)}</td><td>{pct(c.oos_mean_roi, 2)}</td></tr>))}</tbody></table></div>)}
      <h3>Прямой тест на новых данных</h3>
      {w.tests.length === 0 ? <p className="muted">Тестов наблюдения пока нет.</p> : (
        <div className="table-wrap"><table className="table"><thead><tr><th>Стратегия</th><th>С какого момента</th>
          <th>Дней</th><th>Итог на $10 000</th><th>К пассивному</th></tr></thead>
          <tbody>{w.tests.map((t) => (
            <tr key={t.id}><td>{t.name}{!t.on_list_now && <div className="muted">уже не в списке</div>}</td>
              <td>{date(t.created_at)}</td><td>{t.result ? num(t.result.days, 1) : "ждёт новых блоков"}</td>
              <td className={sign(t.result?.net_a)}>{t.result ? usd(t.result.net_a / 1e6, 2) : "—"}</td>
              <td className={sign(t.result?.vs_passive_a)}>{t.result?.vs_passive_a != null ? usd(t.result.vs_passive_a / 1e6, 2) : "—"}</td></tr>))}</tbody></table></div>)}
    </Card>);
}
const RU: Record<string, string> = { WAIT: "ЖДАТЬ", "OPEN CANDIDATE": "КАНДИДАТ НА ОТКРЫТИЕ", HOLD: "ДЕРЖАТЬ",
  REBALANCE: "ПЕРЕСТАВИТЬ", "CLOSE CANDIDATE": "КАНДИДАТ НА ЗАКРЫТИЕ", INVALIDATED: "ОТМЕНЁН" };

function OpenCandidate({ p }: { p: Record<string, any> }) {
  return (
    <div className="grid two">
      <div>
        <p><b>Стратегия:</b> {p.strategy} · уверенность <b>{p.confidence}</b>{p.oos_contaminated && " · OOS не чистый"}</p>
        <p><b>Диапазон:</b> {num(p.range.price_lower, 0)} – {num(p.range.price_upper, 0)} USDC (цена {num(p.current_price, 0)})</p>
        <p><b>Распределение:</b> {num(p.allocation.token0, 6)} WBTC + {num(p.allocation.token1, 2)} USDC (доля USDC {pct(p.allocation.usdc_value_share, 0)})</p>
        <p><b>Ожидаемые комиссии</b> за {p.horizon_days} дн.: {usd(p.expected_fees_usd[0], 2)} – {usd(p.expected_fees_usd[1], 2)} <Badge label="SCENARIO" /></p>
        <p><b>Расходы:</b> {usd(p.expected_costs_usd.total, 2)} (газ открытия {usd(p.expected_costs_usd.gas_open, 2)}, закрытия {usd(p.expected_costs_usd.gas_close, 2)}, обмен {usd(p.expected_costs_usd.swap, 2)})</p>
        <p><b>Прогноз чистого результата (±1σ):</b> {usd(p.net_forecast_usd[0])} – {usd(p.net_forecast_usd[1])} <Badge label="SCENARIO" /></p>
        <p><b>История:</b> макс. просадка {pct(p.historical.max_drawdown)}, OOS к пассивному {pct(p.historical.oos_mean_excess_vs_passive)} ({p.historical.oos_positive_segments}/7)</p>
        <p><b>Действует до:</b> {date(p.valid_until)}</p>
      </div>
      <div>
        <h3>Риски: непостоянные потери при движении цены</h3>
        <table className="table"><thead><tr><th>Цена</th><th>IL</th><th>Итог к HODL</th></tr></thead>
          <tbody>{p.il_scenarios.map((s: any) => (
            <tr key={s.price_change}><td>{pct(s.price_change, 1)}</td><td className="neg">{usd(s.il_usd, 2)}</td>
              <td>{usd(s.net_vs_hodl_usd[0], 2)} … {usd(s.net_vs_hodl_usd[1], 2)}</td></tr>))}</tbody></table>
        <h3>Обоснование</h3>
        <ul>{p.reasons.map((r: any) => <li key={r.name}>{r.passed ? "✓" : "✗"} {r.name}: {JSON.stringify(r.value)} (порог {String(r.threshold)})</li>)}</ul>
      </div>
    </div>);
}

export default function SignalsPage() {
  const { data, error } = useApi<Signals>("/api/signals");
  if (!data) return <Loading error={error} />;
  return (
    <>
      <h1>Центр сигналов <Badge label="SCENARIO" /></h1>
      <p className="lead">Исследовательские сигналы, не инвестиционная рекомендация. Если подходящих условий нет — система ждёт.</p>
      <Card title={`Активные сигналы: ${data.active.length}`}>
        {data.active.length === 0 ? <p className="muted">Активных сигналов нет.</p> :
          data.active.map((s) => <OpenCandidate key={s.id} p={s.payload} />)}
      </Card>
      {data.watch && <Watch w={data.watch} />}
      {data.signals.map((s) => (
        <Card key={s.id} title={`#${s.id} · ${RU[s.type] ?? s.type}`} right={<Pill tone={s.status === "WAIT" ? "warn" : ""}>статус: {RU[s.status] ?? s.status}</Pill>}>
          <p className="muted">{date(s.created_at)} · блок {s.block} · цена {num(s.payload.current_price, 0)} USDC</p>
          {s.type === "OPEN CANDIDATE" ? <OpenCandidate p={s.payload} /> : (
            <>
              <h3>Причины</h3>
              <ul>{(s.payload.reasons ?? []).map((r: any) => <li key={r.name}>{ru(r.name)}: {String(r.value)}</li>)}</ul>
              <h3>Ближе всех к прохождению</h3>
              <div className="table-wrap"><table className="table"><thead><tr><th>Стратегия</th><th>Не прошла</th><th>OOS к пассивному</th><th>OOS ROI</th></tr></thead>
                <tbody>{(s.payload.closest_candidates ?? []).map((c: any) => (
                  <tr key={c.strategy}><td>{c.strategy}</td><td>{c.failed.map(ru).join("; ")}</td><td>{pct(c.oos_mean_excess, 2)}</td><td>{pct(c.oos_mean_roi, 2)}</td></tr>))}</tbody></table></div>
            </>)}
          <h3>История статусов</h3>
          <ul>{s.history.map((e, i) => <li key={i}>{date(e.at)} · {RU[e.status] ?? e.status} — {e.reason}</li>)}</ul>
        </Card>))}
    </>
  );
}
