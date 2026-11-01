import { Card, Loading, Pill } from "../components/ui";
import { useApi } from "../components/useApi";
import { date, pct } from "../format";

interface StratRes { strategy: string; mean_excess: number | null; positive_excess_segments: number; oos_segments: number;
  mean_excess_gas_x2: number | null; ruined: boolean; verdict: string; is_mean_excess?: number | null;
  is_positive_excess_segments?: number; is_segments?: number; consistent_with_is?: boolean }
interface Hyp { id: number; cycle_id: string; created_at: number; title: string; description: string; rationale: string;
  test_plan: string; expected_outcome: string; status: string; verdict: string | null; confidence: string | null;
  conclusion: string | null; strategies: StratRes[]; invalid_reasons: string[]; oos_contaminated: boolean; author: string }
interface Comm { summary: string; patterns: string[]; ineffective_configurations: string[]; next_directions: string[] }
interface Research { hypotheses: Hyp[]; commentary: Record<string, Comm> }

const VERDICT: Record<string, [string, string]> = { confirmed: ["подтверждена", "pos"], rejected: ["отклонена", "neg"],
  inconclusive: ["нет вывода", "warn"] };

export default function ResearchPage() {
  const { data, error } = useApi<Research>("/api/research");
  if (!data) return <Loading error={error} />;
  const cycles = [...new Set(data.hypotheses.map((h) => h.cycle_id))];
  return (
    <>
      <h1>AI-исследования</h1>
      <p className="lead">Гипотезы предлагает модель (или ассистент), проверяет и оценивает Python на OOS-отрезках
        по заранее заданным правилам. Модель не меняет код и не придумывает результаты.</p>
      {cycles.length === 0 && <Card><p>Циклов исследования ещё не было.</p></Card>}
      {cycles.map((cid) => {
        const hs = data.hypotheses.filter((h) => h.cycle_id === cid);
        const c = data.commentary[cid];
        return (
          <div key={cid}>
            <h2 className="section">Цикл {cid} · {date(hs[0].created_at)} · {hs[0].author}</h2>
            {hs.some((h) => h.oos_contaminated) && <div className="warnbox">OOS-проверка не чистая: автор гипотез уже видел
              OOS-результаты. «Подтверждено» здесь — не доказательство; кандидаты проверяются на новых данных.</div>}
            {c && (
              <Card title="Выявленные закономерности и предложения" label="MODELED">
                <p>{c.summary}</p>
                <div className="grid three">
                  <div><h3>Закономерности</h3><ul>{c.patterns.map((x) => <li key={x}>{x}</li>)}</ul></div>
                  <div><h3>Неэффективно</h3><ul>{c.ineffective_configurations.map((x) => <li key={x}>{x}</li>)}</ul></div>
                  <div><h3>Новые предложения</h3><ul>{c.next_directions.map((x) => <li key={x}>{x}</li>)}</ul></div>
                </div>
              </Card>)}
            {hs.map((h) => {
              const [vr, tone] = VERDICT[h.verdict ?? ""] ?? [h.status, "warn"];
              return (
                <Card key={h.id} title={h.title} right={<><Pill tone={tone}>{vr}</Pill>{h.confidence && <Pill>уверенность: {h.confidence}</Pill>}</>}>
                  <p>{h.description}</p>
                  <p className="muted"><b>Обоснование:</b> {h.rationale}</p>
                  <p className="muted"><b>План проверки:</b> {h.test_plan} <b>Ожидание:</b> {h.expected_outcome}</p>
                  {h.conclusion && <p><b>Вывод:</b> {h.conclusion}</p>}
                  {h.invalid_reasons.map((r) => <div key={r} className="warnbox">Причина отклонения параметров: {r}</div>)}
                  <div className="table-wrap"><table className="table">
                    <thead><tr><th>Стратегия</th><th>OOS к пассивному</th><th>Лучше в</th><th>Газ ×2</th><th>IS к пассивному</th><th>Вердикт</th></tr></thead>
                    <tbody>{h.strategies.map((s) => (
                      <tr key={s.strategy}><td>{s.strategy}</td>
                        <td className={(s.mean_excess ?? 0) > 0 ? "pos" : "neg"}>{pct(s.mean_excess, 2)}</td>
                        <td>{s.positive_excess_segments}/{s.oos_segments}</td><td>{pct(s.mean_excess_gas_x2, 2)}</td>
                        <td className={(s.is_mean_excess ?? 0) > 0 ? "pos" : "neg"}>{pct(s.is_mean_excess, 2)}
                          {s.is_segments ? ` (${s.is_positive_excess_segments}/${s.is_segments})` : ""}</td>
                        <td>{s.verdict}{s.consistent_with_is === false ? " · на IS нет" : ""}{s.ruined ? " · разорение" : ""}</td></tr>))}</tbody>
                  </table></div>
                </Card>);
            })}
          </div>);
      })}
    </>
  );
}
