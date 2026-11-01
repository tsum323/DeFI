import { Badge, Card, Loading, Pill, Stat } from "../components/ui";
import { useApi } from "../components/useApi";
import { date } from "../format";

interface DS { network: string; chain_id: number; pool: string; pool_address: string;
  endpoints: { name: string; archive: boolean; max_logs_blocks: number | null; configured: boolean }[];
  sync: { start_block: number; last_block: number; updated_at: number }[]; last_block: number; last_block_ts: number;
  events: Record<string, number>; logs_by_source: Record<string, number>;
  gaps: { from_block: number; to_block: number; error: string; source: string; resolved: boolean }[]; open_gaps: number }

export default function DataSourcesPage() {
  const { data: d, error } = useApi<DS>("/api/datasources");
  if (!d) return <Loading error={error} />;
  const age = Math.round((Date.now() / 1000 - d.last_block_ts) / 60);
  const total = Object.values(d.events).reduce((a, b) => a + b, 0);
  return (
    <>
      <h1>Источники данных <Badge label="EXACT" /></h1>
      <div className="grid stats">
        <Stat title="Сеть" value={`${d.network} (${d.chain_id})`} hint={<>пул {d.pool}<br /><code>{d.pool_address}</code></>} />
        <Stat title="Последний блок" value={d.last_block.toLocaleString("ru-RU")} hint={`${date(d.last_block_ts)} · ${age} мин назад`} />
        <Stat title="Событий" value={total.toLocaleString("ru-RU")} hint={`Swap: ${d.events.ev_swap?.toLocaleString("ru-RU")}`} />
        <Stat title="Пропуски" value={d.open_gaps} tone={d.open_gaps ? "neg" : "pos"} hint="открытых интервалов без данных" />
        <Stat title="Синхронизация" value={age <= 90 ? "актуальна" : "устарела"} tone={age <= 90 ? "pos" : "warn"}
          hint={d.sync.map((s) => `${s.start_block}…${s.last_block}`).join(", ")} />
      </div>
      <Card title="RPC-источники">
        <div className="table-wrap"><table className="table">
          <thead><tr><th>Имя</th><th>Архив</th><th>Окно eth_getLogs</th><th>Настроен</th><th>Логов загружено</th></tr></thead>
          <tbody>{d.endpoints.map((e) => (
            <tr key={e.name}><td>{e.name}</td><td>{e.archive ? "да" : "нет"}</td><td>{e.max_logs_blocks ?? "без ограничения"}</td>
              <td>{e.configured ? <Pill tone="pos">да</Pill> : <Pill tone="warn">нет</Pill>}</td>
              <td>{(d.logs_by_source[e.name] ?? 0).toLocaleString("ru-RU")}</td></tr>))}</tbody></table></div>
      </Card>
      <Card title="События пула по типам">
        <div className="chips">{Object.entries(d.events).map(([k, v]) => <Pill key={k}>{k.replace("ev_", "")}: {v.toLocaleString("ru-RU")}</Pill>)}</div>
      </Card>
      <Card title="Ошибки и пропуски (последние 20)">
        {d.gaps.length === 0 ? <p className="muted">Нет.</p> : (
          <div className="table-wrap"><table className="table"><thead><tr><th>Блоки</th><th>Источник</th><th>Статус</th><th>Ошибка</th></tr></thead>
            <tbody>{d.gaps.map((g, i) => <tr key={i}><td>{g.from_block}…{g.to_block}</td><td>{g.source}</td>
              <td>{g.resolved ? <Pill tone="pos">закрыт</Pill> : <Pill tone="neg">открыт</Pill>}</td><td className="muted">{g.error}</td></tr>)}</tbody></table></div>)}
      </Card>
    </>
  );
}
