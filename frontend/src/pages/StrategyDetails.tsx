import { useParams } from "react-router-dom";
import { Chart } from "../components/Chart";
import { Card, Loading, Pill, Stat } from "../components/ui";
import { useApi } from "../components/useApi";
import { day, msk, pct, pctAbs, sign, usd } from "../format";

interface Point { t: number; price: number; value_lp: number; value_hodl: number; fees_a: number; fees_b: number;
  net_a: number; gas: number; in_range: boolean; token0: number; token1: number }
interface Action { t: number; kind: string; price_lower: number | null; price_upper: number | null; gas: number;
  swap_cost: number; il_realized: number }
interface Details {
  name: string; params: Record<string, unknown>;
  segments: { index: number; role: string; start_ts: number; trend: string; vol_class: string; roi: number | null;
    passive_roi: number | null; fees: number | null; max_drawdown: number | null; rebalances: number | null; ruined: boolean | null }[];
  series: { points: Point[]; actions: Action[]; metrics: Record<string, number | boolean | null>; note: string } | null;
}

const ts = (t: number) => msk(t); // chart axes in Moscow time

export default function StrategyDetailsPage() {
  const { hash } = useParams();
  const { data: d, error } = useApi<Details>(`/api/strategies/${hash}`);
  if (!d) return <Loading error={error ?? "считаю бэктест за OOS-период (до ~30 с при первом открытии)…"} />;
  const s = d.series;
  const m = s?.metrics ?? {};
  const x = s?.points.map((p) => ts(p.t)) ?? [];
  // range bands: each action opens a range valid until the next action
  const bands = (s?.actions ?? []).filter((a) => a.price_lower).map((a, i, arr) => ({
    type: "rect" as const, xref: "x" as const, yref: "y" as const, x0: ts(a.t), x1: ts(arr[i + 1]?.t ?? s!.points[s!.points.length - 1].t),
    y0: a.price_lower!, y1: a.price_upper!, fillcolor: "rgba(212,175,55,0.12)", line: { width: 0 } }));
  const reb = (s?.actions ?? []).filter((a) => a.kind !== "open");
  const exits = s?.points.filter((p, i, arr) => i > 0 && arr[i - 1].in_range && !p.in_range) ?? [];
  return (
    <>
      <h1>{d.name}</h1>
      <p className="lead">Непрерывный бэктест на OOS-периоде (капитал $10 000) · {s?.note}</p>
      {s && (
        <div className="grid stats">
          <Stat title="Чистый результат (A)" label="MODELED" value={usd(m.net_a as number)} tone={sign(m.net_a as number)}
            hint={<>модель B: {usd(m.net_b as number)}</>} />
          <Stat title="Против HODL" label="MODELED" value={usd(m.vs_hodl_a as number)} tone={sign(m.vs_hodl_a as number)} />
          <Stat title="Комиссии" label="MODELED" value={usd(m.fees_a as number)} hint={<>B: {usd(m.fees_b as number)}</>} />
          <Stat title="Газ и обмены" label="MODELED" value={usd((m.gas_cost as number) + (m.swap_cost as number))}
            hint={<>газ {usd(m.gas_cost as number)} · обмены {usd(m.swap_cost as number)}</>} />
          <Stat title="IL реализованный / текущий" label="MODELED" value={usd(m.il_realized as number)}
            hint={usd(m.il_unrealized as number)} tone="neg" />
          <Stat title="Max DD · Sharpe" label="MODELED" value={pctAbs(m.max_drawdown_a as number)}
            hint={<>Sharpe {(m.sharpe_a as number)?.toFixed?.(2) ?? "—"} · Sortino {(m.sortino_a as number)?.toFixed?.(2) ?? "—"}</>} />
          <Stat title="В диапазоне" label="MODELED" value={pctAbs(m.time_in_range as number)}
            hint={<>перестановок {m.rebalances as number} · выходов по волатильности {m.exits as number}</>} />
          <Stat title="Средний дневной доход" label="MODELED" value={usd(m.avg_daily_net_a as number, 2)} />
        </div>)}
      {s && (
        <>
          <Card title="Стоимость позиции и HODL" label="MODELED">
            <Chart data={[
              { x, y: s.points.map((p) => p.value_lp + p.fees_a - p.gas), name: "LP + комиссии − газ", type: "scatter", line: { color: "#d4af37", width: 2 } },
              { x, y: s.points.map((p) => p.value_lp), name: "LP без комиссий", type: "scatter", line: { color: "#7aa2f7", width: 1 } },
              { x, y: s.points.map((p) => p.value_hodl), name: "HODL", type: "scatter", line: { color: "#9aa5b8", dash: "dot" } },
            ]} layout={{ yaxis: { title: { text: "USDC" } } }} />
          </Card>
          <div className="grid two">
            <Card title="Накопленные комиссии" label="MODELED">
              <Chart height={260} data={[
                { x, y: s.points.map((p) => p.fees_a), name: "модель A (консервативная)", type: "scatter", line: { color: "#5eead4" } },
                { x, y: s.points.map((p) => p.fees_b), name: "модель B (оптимистичная)", type: "scatter", line: { color: "#5eead4", dash: "dot" } },
              ]} />
            </Card>
            <Card title="Количество активов в позиции" label="MODELED">
              <Chart height={260} data={[
                { x, y: s.points.map((p) => p.token0), name: "WBTC", type: "scatter", line: { color: "#f7931a" } },
                { x, y: s.points.map((p) => p.token1), name: "USDC", type: "scatter", yaxis: "y2", line: { color: "#2775ca" } },
              ]} layout={{ yaxis2: { overlaying: "y", side: "right", gridcolor: "rgba(0,0,0,0)" } }} />
            </Card>
          </div>
          <Card title="Цена, диапазоны, выходы и перестановки" label="EXACT" right={<Pill>цена — точно, диапазоны — модель</Pill>}>
            <Chart height={340} data={[
              { x, y: s.points.map((p) => p.price), name: "цена WBTC, USDC", type: "scatter", line: { color: "#e6e9f2" } },
              { x: reb.map((a) => ts(a.t)), y: reb.map((a) => ((a.price_lower ?? 0) + (a.price_upper ?? 0)) / 2), name: "перестановка",
                mode: "markers", type: "scatter", marker: { color: "#d4af37", size: 9, symbol: "diamond" } },
              { x: exits.map((p) => ts(p.t)), y: exits.map((p) => p.price), name: "выход из диапазона",
                mode: "markers", type: "scatter", marker: { color: "#f87171", size: 7, symbol: "x" } },
            ]} layout={{ shapes: bands }} />
          </Card>
        </>)}
      <Card title="Результаты по отрезкам (капитал заново на каждом)" label="MODELED">
        <div className="table-wrap"><table className="table">
          <thead><tr><th>#</th><th>Роль</th><th>Начало</th><th>Режим</th><th>ROI</th><th>Пассивный LP</th><th>Разница</th>
            <th>Комиссии</th><th>Max DD</th><th>Перест.</th></tr></thead>
          <tbody>{d.segments.map((g) => (
            <tr key={g.index} className={g.role === "OOS" ? "oos" : ""}>
              <td>{g.index}</td><td>{g.role}</td><td>{day(g.start_ts)}</td><td>{g.trend}/{g.vol_class}</td>
              <td className={sign(g.roi)}>{pct(g.roi)}</td><td className={sign(g.passive_roi)}>{pct(g.passive_roi)}</td>
              <td className={sign(g.roi !== null && g.passive_roi !== null ? g.roi - g.passive_roi : null)}>
                {g.roi !== null && g.passive_roi !== null ? pct(g.roi - g.passive_roi) : "—"}</td>
              <td>{pctAbs(g.fees)}</td><td>{pctAbs(g.max_drawdown)}</td><td>{g.rebalances ?? "—"}{g.ruined ? " · разорение" : ""}</td>
            </tr>))}</tbody>
        </table></div>
      </Card>
    </>
  );
}
