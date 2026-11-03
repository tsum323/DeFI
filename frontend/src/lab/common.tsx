// Shared pieces of the UNISWAP AI LAB interface: plain-language names and the signal card.
import { useState } from "react";
import { Badge, Pill } from "../components/ui";
import { date, num, pct, pctAbs, sign, usd } from "../format";

export const TIER_RU: Record<string, { name: string; tone: string; text: string }> = {
  CONSERVATIVE: { name: "Проверенный", tone: "pos", text: "прошёл все проверки на истории, включая данные, которые стратегия раньше не видела" },
  MODERATE: { name: "Умеренный", tone: "warn", text: "на данных, которые стратегия не видела, был лучше простого размещения, но результат не во всех периодах стабилен" },
  EXPERIMENTAL: { name: "Экспериментальный", tone: "neg", text: "интересно, но не подтверждено — только для проверки в виртуальном портфеле" },
};
export const RISK_RU: Record<string, string> = { low: "низкий", medium: "средний", high: "высокий" };
export const CONF_RU: Record<string, string> = { high: "высокая", medium: "средняя", low: "низкая" };
export const STATUS_RU: Record<string, string> = {
  WAIT: "ЖДАТЬ", "OPEN CANDIDATE": "МОЖНО ОТКРЫВАТЬ", HOLD: "ДЕРЖАТЬ", REBALANCE: "СМЕНИТЬ ДИАПАЗОН",
  "CLOSE CANDIDATE": "МОЖНО ЗАКРЫВАТЬ", INVALIDATED: "СИГНАЛ ОТМЕНЁН",
};
export const RULE_RU: Record<string, string> = {
  none: "диапазон не меняется", on_exit: "диапазон меняется, когда цена из него выходит",
  periodic: "диапазон меняется по расписанию", edge: "диапазон меняется у края",
};
const fmtPrice = (x: number) => num(x, x >= 1000 ? 0 : x >= 10 ? 2 : 4);

export interface CardT {
  id: string; source: "exact" | "estimated"; tier: string; pair: string; network: string; pool_address: string;
  fee_tier: number; price: number; token0: string; token1: string; network_ru?: string; gas_note?: string; price_lower: number; price_upper: number; deposit_usd: number;
  token0_amount: number; token1_amount: number; quote_share: number | null; base: string; quote: string;
  strategy: string; strategy_ru?: string; rule: string; horizon_days: number; fees_usd: [number, number]; il_usd: number;
  scenarios: { change: number; il_usd: number; position_change_usd: number }[];
  gas_usd: number; swap_usd: number; net_usd: [number, number]; exit_probability: number | null;
  risk: string; risk_why: string; confidence: string; history: Record<string, any>; why: string[];
  blockers: string[]; to_improve: string[]; invalidation: string[]; valid_until: number; label: "MODELED" | "ESTIMATED";
  oos_contaminated?: boolean; token_risks?: string[]; variants: string[];
  base_usd?: number | null; quote_usd?: number | null; liquidity_share?: number | null;
  amount_guide?: { recommended: number | null; min: number | null; max: number | null; why: string;
    min_profit?: number | null; breakeven?: number | null };
  entry?: { now: number; average_7d: number; low_7d: number; high_7d: number; recommended: number;
    reference_pool: string; reference_price: number; pool_vs_market: number; verdict: string; assumption: string } | null;
}

// Size for the user's capital: fees, loss vs holding and the swap scale with size, gas does not.
export function sized(c: CardT, capital: number | null, share: number) {
  const size = capital ? Math.max(0, capital * share) : c.deposit_usd;
  const k = size / c.deposit_usd;
  const fees: [number, number] = [c.fees_usd[0] * k, c.fees_usd[1] * k];
  const net: [number, number] = [fees[0] + c.il_usd * k - c.gas_usd - c.swap_usd * k, fees[1] + c.il_usd * k - c.gas_usd - c.swap_usd * k];
  const perDollar = (c.fees_usd[0] + c.fees_usd[1]) / 2 / c.deposit_usd + (c.il_usd - c.swap_usd) / c.deposit_usd;
  const breakEven = perDollar > 0 ? c.gas_usd / perDollar : null;
  return { size, k, fees, il: c.il_usd * k, swap: c.swap_usd * k, net, breakEven,
    scen: c.scenarios.map((s) => ({ ...s, position_change_usd: s.position_change_usd * k })) };
}

export function SignalCard({ c, capital, share }: { c: CardT; capital: number | null; share: number }) {
  const [open, setOpen] = useState(false);
  const s = sized(c, capital, share);
  const worst = s.scen.length ? s.scen.reduce((a, b) => (a.position_change_usd < b.position_change_usd ? a : b)) : null;
  const t = TIER_RU[c.tier];
  return (
    <div className="card opp">
      <div className="card-head">
        <h2>{c.pair} · {pctAbs(c.fee_tier, 2)} · {c.network_ru ?? "Ethereum"}</h2>
        <div className="card-right">
          <Pill tone={t.tone}>{t.name}</Pill>
          <Pill tone={c.risk === "low" ? "pos" : c.risk === "medium" ? "warn" : "neg"}>риск {RISK_RU[c.risk]}</Pill>
          <Badge label={c.label} />
        </div>
      </div>
      <p className="hero">
        Предлагаемый диапазон: <b>{fmtPrice(c.price_lower)} – {fmtPrice(c.price_upper)} {c.quote}</b> за 1 {c.base} (сейчас {fmtPrice(c.price)}).<br />
        Размер: <b>{usd(s.size)}</b>{capital ? <> ({pctAbs(share, 0)} вашего капитала)</> : <> (пример)</>} —{" "}
        {num(c.token0_amount * s.k, c.token0 === "USDC" ? 2 : 6)} {c.token0} + {num(c.token1_amount * s.k, c.token1 === "USDC" ? 2 : 6)} {c.token1}<br />
        Ожидаемый результат за {c.horizon_days} дн. по сравнению с простым хранением токенов:{" "}
        <b className={sign(s.net[0] + s.net[1])}>{usd(s.net[0])} … {usd(s.net[1])}</b> — оценка модели, не гарантия.
      </p>
      {c.strategy_ru && <p><b>Как устроено:</b> {c.strategy_ru}.</p>}
      <p><b>Почему:</b> {c.why.join("; ")}.</p>
      <p><b>Главный риск:</b> цена может выйти за диапазон — тогда комиссии перестанут начисляться, а позиция
        целиком превратится в один токен ({c.risk_why}).
        {worst && <> Если цена изменится на {pct(worst.change, 0)}, позиция изменится примерно на <b className="neg">{usd(worst.position_change_usd)}</b>.</>}</p>
      {s.breakEven != null && capital != null && s.size < s.breakEven &&
        <p className="warn">При таком размере газ съедает доход: имеет смысл от ~{usd(s.breakEven)}.</p>}
      <button className="pill" onClick={() => setOpen(!open)}>{open ? "Скрыть подробности" : "Подробнее"}</button>
      {open && (
        <div className="grid two" style={{ marginTop: 10 }}>
          <div className="kv">
            <span>Сеть, пул</span><span>{c.network} · <code>{c.pool_address}</code></span>
            <span>Правило</span><span>{c.strategy_ru ?? RULE_RU[c.rule] ?? c.rule} <span className="muted">({c.strategy})</span></span>
            <span>Доля {c.quote} при входе</span><span>{pctAbs(c.quote_share, 0)}</span>
            <span>Комиссии за {c.horizon_days} дн.</span><span>{usd(s.fees[0])} – {usd(s.fees[1])}</span>
            <span>Потеря к хранению (±1σ)</span><span>{usd(s.il)}</span>
            <span>Газ (вход + выход)</span><span>{usd(c.gas_usd, 2)}</span>
            <span>Обмен до нужной пропорции</span><span>{usd(s.swap, 2)}</span>
            <span>Шанс выхода из диапазона</span><span>{pctAbs(c.exit_probability, 0)}</span>
            <span>Достоверность</span><span>{CONF_RU[c.confidence]}{c.oos_contaminated ? " (параметры подобраны после просмотра проверки)" : ""}</span>
            <span>Действует до</span><span>{date(c.valid_until)}</span>
          </div>
          <div>
            <h3>Сценарии цены</h3>
            <ul>{s.scen.map((x) => <li key={x.change}>цена {pct(x.change, 0)}: позиция {usd(x.position_change_usd)}, из них потеря к хранению {usd(x.il_usd * s.k)}</li>)}</ul>
            <h3>История</h3>
            {c.source === "exact" ? <p>Проверка на данных, которые стратегия не видела: лучше простого размещения в {c.history.oos_positive} из 7 периодов,
              в среднем {pct(c.history.oos_vs_passive)} за 91 день; на периодах подбора {pct(c.history.is_vs_passive)};
              средняя просадка {pctAbs(c.history.mean_drawdown, 0)}, худшая {pctAbs(c.history.max_drawdown, 0)} ({c.history.segments}).</p>
              : <p>{c.history.segments}.</p>}
            {c.blockers.length > 0 && <><h3>Что мешает рейтингу выше</h3><ul>{c.blockers.map((b) => <li key={b} className="warn">{b}</li>)}</ul></>}
            {c.to_improve.length > 0 && <><h3>Что должно измениться</h3><ul>{c.to_improve.map((b) => <li key={b}>{b}</li>)}</ul></>}
            <h3>Сигнал отменяется, если</h3><ul>{c.invalidation.map((b) => <li key={b}>{b}</li>)}</ul>
            {c.token_risks && <><h3>Риски токенов</h3><ul>{c.token_risks.map((b) => <li key={b}>{b}</li>)}</ul></>}
            {c.variants.length > 0 && <p className="muted">Та же позиция у: {c.variants.join(", ")}</p>}
          </div>
        </div>)}
    </div>);
}

export function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const lo = Math.min(...values), hi = Math.max(...values), w = 300, h = 60;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - lo) / (hi - lo || 1)) * (h - 6) - 3}`).join(" ");
  return <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label="баланс по дням">
    <polyline points={pts} fill="none" stroke="var(--gold)" strokeWidth="2" /></svg>;
}
