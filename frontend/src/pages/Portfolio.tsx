import { Badge, Card, Loading, Pill, Stat } from "../components/ui";
import { useApi } from "../components/useApi";
import { date, pct, pctAbs, sign, usd } from "../format";

interface Portfolio {
  journal: { counts: Record<string, number>; cumulative_net_a: number; return_week: number | null;
    return_month: number | null; max_drawdown: number; signals: any[] };
  positions: any[]; forward_tests: { id: number; start_block: number; created_at: number; reason: string;
    params: any; result: any }[];
}

export default function PortfolioPage() {
  const { data: d, error } = useApi<Portfolio>("/api/portfolio");
  if (!d) return <Loading error={error} />;
  const c = d.journal.counts;
  return (
    <>
      <h1>Виртуальный портфель <Badge label="MODELED" /></h1>
      <p className="lead">Каждый сигнал на открытие становится виртуальной позицией с неизменяемыми параметрами.
        Комиссии — модельные (на точной реконструкции пула), не фактически полученные.</p>
      <div className="grid stats">
        <Stat title="Всего сигналов" value={c.all} hint={`на открытие: ${c.open_candidates}`} />
        <Stat title="Активные позиции" value={c.active_positions} />
        <Stat title="Успешные / неудачные" value={`${c.successful} / ${c.failed}`} hint={`отменённых: ${c.cancelled}`} />
        <Stat title="Совокупный результат" label="MODELED" value={usd(d.journal.cumulative_net_a / 1e6, 2)} tone={sign(d.journal.cumulative_net_a)} />
        <Stat title="За неделю / месяц" label="MODELED" value={`${pct(d.journal.return_week)} / ${pct(d.journal.return_month)}`} />
        <Stat title="Макс. виртуальная просадка" label="MODELED" value={pctAbs(d.journal.max_drawdown)} />
      </div>
      <Card title="Виртуальные позиции">
        {d.positions.length === 0 ? <p className="muted">Позиций нет — сигналов на открытие ещё не было.</p> : (
          <div className="table-wrap"><table className="table">
            <thead><tr><th>#</th><th>Стратегия</th><th>Статус</th><th>Капитал</th><th>Текущая стоимость</th><th>Комиссии</th>
              <th>Расходы</th><th>Чистая прибыль</th><th>К HODL</th><th>К пассивному</th></tr></thead>
            <tbody>{d.positions.map((p) => {
              const m = p.metrics;
              return (<tr key={p.position_id}><td>{p.position_id}</td><td>{p.strategy}</td>
                <td>{p.status}{p.outcome ? ` · ${p.outcome}` : ""}</td><td>{usd(p.capital / 1e6)}</td>
                <td>{usd(m.value_lp / 1e6, 2)}</td><td>{usd(m.fees_a / 1e6, 2)}</td>
                <td>{usd((m.gas_cost + m.swap_cost) / 1e6, 2)}</td><td className={sign(m.net_a)}>{usd(m.net_a / 1e6, 2)}</td>
                <td className={sign(m.vs_hodl_a)}>{usd(m.vs_hodl_a / 1e6, 2)}</td>
                <td className={sign(p.vs_passive_a)}>{p.vs_passive_a === null ? "—" : usd(p.vs_passive_a / 1e6, 2)}</td></tr>);
            })}</tbody></table></div>)}
      </Card>
      <Card title="Прямые тесты кандидатов на новых данных" label="MODELED" right={<Pill>не сигналы</Pill>}>
        <p className="muted">Параметры зафиксированы при постановке; оцениваются только данные, которых тогда не было.</p>
        <div className="table-wrap"><table className="table">
          <thead><tr><th>#</th><th>Стратегия</th><th>С блока</th><th>Причина</th><th>Дней</th><th>Итог</th><th>К пассивному</th><th>Перест.</th></tr></thead>
          <tbody>{d.forward_tests.map((f) => (
            <tr key={f.id}><td>{f.id}</td><td>{f.result?.strategy ?? "—"}</td><td>{f.start_block}<div className="muted">{date(f.created_at)}</div></td>
              <td className="muted">{f.reason}</td><td>{f.result ? f.result.days.toFixed(1) : "ждёт новых блоков"}</td>
              <td className={sign(f.result?.net_a)}>{f.result ? usd(f.result.net_a / 1e6, 2) : "—"}</td>
              <td className={sign(f.result?.vs_passive_a)}>{f.result?.vs_passive_a != null ? usd(f.result.vs_passive_a / 1e6, 2) : "—"}</td>
              <td>{f.result?.rebalances ?? "—"}</td></tr>))}</tbody></table></div>
      </Card>
      <Card title="История сигналов">
        <ul>{d.journal.signals.map((s) => <li key={s.signal_id}>#{s.signal_id} {s.type} · {s.status ?? "—"} · {date(s.created_at)}
          {s.position_id ? ` · позиция #${s.position_id} ${s.position_status ?? ""}` : ""}</li>)}</ul>
      </Card>
    </>
  );
}
