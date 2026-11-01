import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { Ranking, RankingRow } from "../api";
import { Badge, Loading, Pill } from "../components/ui";
import { useApi } from "../components/useApi";
import { num, pct, pctAbs, sign, usd } from "../format";

type Key = keyof RankingRow;
const RULE_RU: Record<string, string> = { none: "нет", on_exit: "при выходе", periodic: "по расписанию", edge: "у края" };
const COLS: { key: Key; title: string; fmt: (r: RankingRow) => string; tone?: (r: RankingRow) => string }[] = [
  { key: "score", title: "Устойчивость", fmt: (r) => num(r.score, 1) },
  { key: "range", title: "Диапазон", fmt: (r) => r.range },
  { key: "rule", title: "Перестановка", fmt: (r) => RULE_RU[r.rule] ?? r.rule },
  { key: "allocation_usdc", title: "Доля USDC", fmt: (r) => (r.allocation_usdc === null ? "50/50" : pctAbs(r.allocation_usdc, 0)) },
  { key: "fees", title: "Комиссии", fmt: (r) => pctAbs(r.fees) },
  { key: "il", title: "IL", fmt: (r) => pct(r.il), tone: (r) => sign(r.il) },
  { key: "net", title: "Чистая прибыль", fmt: (r) => usd(r.net), tone: (r) => sign(r.net) },
  { key: "roi", title: "ROI", fmt: (r) => pct(r.roi), tone: (r) => sign(r.roi) },
  { key: "max_drawdown", title: "Max DD", fmt: (r) => pctAbs(r.max_drawdown) },
  { key: "sharpe", title: "Sharpe", fmt: (r) => num(r.sharpe) },
  { key: "oos_roi", title: "OOS ROI", fmt: (r) => pct(r.oos_roi), tone: (r) => sign(r.oos_roi) },
  { key: "oos_vs_passive", title: "OOS к пассивному", fmt: (r) => pct(r.oos_vs_passive), tone: (r) => sign(r.oos_vs_passive) },
];

export default function RankingPage() {
  const { data, error } = useApi<Ranking>("/api/strategies");
  const [sort, setSort] = useState<{ key: Key; desc: boolean }>({ key: "score", desc: true });
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const r = (data?.rows ?? []).filter((x) => x.name.includes(q));
    const v = (x: RankingRow) => { const y = x[sort.key]; return typeof y === "number" ? y : y === null ? -1e18 : String(y); };
    return [...r].sort((a, b) => (v(a) < v(b) ? 1 : v(a) > v(b) ? -1 : 0) * (sort.desc ? 1 : -1));
  }, [data, sort, q]);
  if (!data) return <Loading error={error} />;
  return (
    <>
      <h1>Рейтинг стратегий <Badge label={data.label} /></h1>
      <p className="lead">{rows.length} стратегий · значения — {data.per}. Победитель выбирается по оценке устойчивости,
        а не по максимальной доходности.</p>
      <input className="search" placeholder="Фильтр по названию…" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="table-wrap">
        <table className="table">
          <thead><tr>
            <th>#</th><th>Стратегия</th><th>Пара</th>
            {COLS.map((c) => (
              <th key={c.key} className="sortable" onClick={() => setSort({ key: c.key, desc: sort.key === c.key ? !sort.desc : true })}>
                {c.title}{sort.key === c.key ? (sort.desc ? " ▾" : " ▴") : ""}
              </th>))}
            <th>Флаги</th>
          </tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.spec_hash}>
                <td className="muted">{i + 1}</td>
                <td><Link to={`/strategy/${r.spec_hash}`}>{r.name}</Link></td>
                <td className="muted">{r.pair}</td>
                {COLS.map((c) => <td key={c.key} className={c.tone?.(r) ?? ""}>{c.fmt(r)}</td>)}
                <td>{r.flags.map((f) => <Pill key={f} tone="warn">{f}</Pill>)}</td>
              </tr>))}
          </tbody>
        </table>
      </div>
    </>
  );
}
