// The five sections of UNISWAP AI LAB.
import { useState } from "react";
import { Badge, Card, Loading, Pill, Stat } from "../components/ui";
import { useApi } from "../components/useApi";
import { date, num, pct, pctAbs, sign, usd } from "../format";
import { type CardT, CONF_RU, RISK_RU, SignalCard, Spark, STATUS_RU, TIER_RU } from "./common";

type Any = Record<string, any>;
const STATUS_TONE: Record<string, string> = { ok: "pos", warn: "warn", error: "neg", info: "" };
const KIND_RU: Record<string, string> = { data: "данные", scan: "сканер", portfolio: "портфель", learning: "обучение",
  calibration: "калибровка", research: "исследование", report: "отчёт" };


// ---------------------------------------------------------------- Dashboard
export function Dashboard() {
  const { data: d, error } = useApi<Any>("/api/lab/dashboard");
  if (!d) return <Loading error={error} />;
  const p = d.portfolio;
  return (
    <>
      <h1>UNISWAP AI LAB</h1>
      <p className="lead"><span className={`dot ${d.status}`} />
        {d.status === "ok" ? "Система работает" : d.status === "stale" ? "Давно нет новых действий" : "Есть ошибки"} ·
        последнее действие {date(d.last_action_at)}. Только чтение блокчейна — система ничего не покупает и не подписывает.</p>
      <div className="grid stats">
        <Stat title="Строгий сигнал" value={STATUS_RU[d.strict.status] ?? d.strict.status} hint={d.strict.status === "WAIT" ? "все проверки пройдены не были" : ""} />
        <Stat title="Возможностей сейчас" value={d.counts.CONSERVATIVE + d.counts.MODERATE + d.counts.EXPERIMENTAL}
          hint={`проверенных ${d.counts.CONSERVATIVE} · умеренных ${d.counts.MODERATE} · экспериментальных ${d.counts.EXPERIMENTAL}`} />
        <Stat title="Виртуальный портфель" label="MODELED" value={usd(p.balance_usd, 2)} tone={sign(p.pnl_usd)}
          hint={`итог ${usd(p.pnl_usd, 2)} (${pct(p.pnl_pct, 2)}) · позиций ${p.open}`} />
        {d.bot && <Stat title="Мой бот" value={usd(d.bot.balance_usd, 2)} tone={sign(d.bot.pnl_usd)}
          hint={`начал с ${usd(d.bot.start_usd)} · ${d.bot.where}`} />}
        <Stat title="За сутки" label="MODELED" value={p.day_change_usd == null ? "—" : usd(p.day_change_usd, 2)} tone={sign(p.day_change_usd)}
          hint="изменение баланса портфеля" />
      </div>
      {d.errors_24h.length > 0 && <Card title={d.errors_24h.some((e: Any) => !e.resolved) ? "Ошибки за сутки" : "Ошибки за сутки — уже исправлены"}>
        <ul>{d.errors_24h.map((e: Any, i: number) => <li key={i} className={e.resolved ? "muted" : "neg"}>{date(e.at).replace(/^.*?г\.,?\s*/, "")} · {e.message}
          {e.resolved && <> — после этого шаг выполнялся успешно</>}</li>)}</ul></Card>}
      <Card title="Лучшие варианты сейчас" right={<Badge label="SCENARIO" />}>
        {d.best.length === 0 ? <p className="muted">Интересных вариантов сейчас нет — система продолжает исследование.</p> :
          <ul>{d.best.map((c: CardT) => <li key={c.id}><b>{c.pair}</b> {pctAbs(c.fee_tier, 2)} · {c.network_ru ?? "Ethereum"} · {TIER_RU[c.tier].name.toLowerCase()} ·
            диапазон {num(c.price_lower, 0)}–{num(c.price_upper, 0)} · за {c.horizon_days} дн. на {usd(c.deposit_usd)}: <span className={sign(c.net_usd[0] + c.net_usd[1])}>{usd(c.net_usd[0])} … {usd(c.net_usd[1])}</span> к хранению · риск {RISK_RU[c.risk]}
            {c.label === "ESTIMATED" && <> <Badge label="ESTIMATED" /></>}</li>)}</ul>}
        <p className="muted">Подробно — в разделе «Сигналы».</p>
      </Card>
      <Card title="Что система делала сегодня">
        {d.today.length === 0 ? <p className="muted">Сегодня ещё не было действий.</p> :
          d.today.map((a: Any, i: number) => <div className="log-item" key={i}><span className="muted">{date(a.at).replace(/^.*?г\.,?\s*/, "")}</span>
            <span><Pill tone={STATUS_TONE[a.status]}>{a.status}</Pill> {a.message}</span></div>)}
      </Card>
      <Card title="Что исследует дальше">
        {d.next_tasks.length === 0 ? <p className="muted">Новые задачи появятся после ежедневного исследования (около 03:00 МСК).</p> :
          <ul>{d.next_tasks.map((t: Any) => <li key={t.title}>{t.title}</li>)}</ul>}
      </Card>
    </>);
}

// ---------------------------------------------------------------- Signals
// The day's best strategies -> pick one, enter the amount, choose the entry price.
function useStored(key: string, initial: string): [string, (v: string) => void] {
  const [v, setV] = useState<string>(() => { try { return localStorage.getItem(key) ?? initial; } catch { return initial; } });
  return [v, (x: string) => { setV(x); try { localStorage.setItem(key, x); } catch { /* private mode */ } }];
}

// Uniswap v3 position in base/quote terms (price = quote per 1 base), float math.
function plan(c: CardT, amountUsd: number, entry: number) {
  const lr = c.price_lower / c.price, ur = c.price_upper / c.price;
  const P = entry, Pa = P * lr, Pb = P * ur, sp = Math.sqrt(P), sa = Math.sqrt(Pa), sb = Math.sqrt(Pb);
  const quoteUsd = c.quote_usd ?? 1;
  const perL = (1 / sp - 1 / sb) * P + (sp - sa);                 // value of L = 1 in quote
  const L = amountUsd / quoteUsd / perL;
  const x = L * (1 / sp - 1 / sb), y = L * (sp - sa);              // base, quote amounts
  const valueAt = (p: number) => {
    const s = Math.sqrt(p);
    const [bx, by] = s <= sa ? [L * (1 / sa - 1 / sb), 0] : s >= sb ? [0, L * (sb - sa)] : [L * (1 / s - 1 / sb), L * (s - sa)];
    return (bx * p + by) * quoteUsd;
  };
  const k = amountUsd / c.deposit_usd;
  const baseShare = (x * P) / (x * P + y);
  const fees: [number, number] = [c.fees_usd[0] * k, c.fees_usd[1] * k];
  const swap = amountUsd * baseShare * c.fee_tier;                // buying the base part from cash
  const il = c.il_usd * k;
  const net: [number, number] = [fees[0] + il - c.gas_usd - swap, fees[1] + il - c.gas_usd - swap];
  const scen = [-0.2, -0.1, 0.1, 0.2].map((ch) => {
    const p = P * (1 + ch), lp = valueAt(p), hold = (x * p + y) * quoteUsd;
    return { change: ch, position: lp - amountUsd, vsHold: lp - hold, hold: hold - amountUsd };
  });
  const perDollar = ((fees[0] + fees[1]) / 2 + il - swap) / amountUsd;
  return { Pa, Pb, x, y, baseShare, fees, swap, il, net, scen, breakEven: perDollar > 0 ? c.gas_usd / perDollar : null };
}

const fp = (x: number) => num(x, x >= 1000 ? 0 : x >= 10 ? 2 : 4);
const REPO = "tsum323/DeFI";

// A pre-filled GitHub issue: the live routine picks it up (backend/lab/tracking.py).
function trackUrl(c: CardT, amount: number, r: { Pa: number; Pb: number; net: [number, number] }) {
  const d = { pool: c.pool, price_lower: Number(r.Pa.toPrecision(8)), price_upper: Number(r.Pb.toPrecision(8)),
    amount_usd: amount, card_id: c.id, strategy: c.strategy, tier: c.tier,
    net_usd: [Number(r.net[0].toFixed(2)), Number(r.net[1].toFixed(2))], horizon_days: c.horizon_days };
  const title = `Отслеживать: ${c.pair} ${(c.fee_tier * 100).toFixed(2)}% (${c.network_ru ?? "Ethereum"}) на $${amount}`;
  const body = `Я выбрал этот сигнал. Не меняйте блок ниже — по нему система ведёт позицию.\n\n\`\`\`track\n${JSON.stringify(d)}\n\`\`\`\n\nЗакройте заявку, когда выйдете из позиции.`;
  return `https://github.com/${REPO}/issues/new?title=${encodeURIComponent(title)}&body=${encodeURIComponent(body)}&labels=track`;
}

export function Signals() {
  const { data: d, error } = useApi<Any>("/api/lab/signals");
  const [picked, setPicked] = useStored("picked_card", "");
  const [amountS, setAmountS] = useStored("invest_amount", "1000");
  const [choice, setChoice] = useStored("amount_choice", "rec");
  const [bestSort, setBestSort] = useStored("best_sort", "tier");
  if (!d) return <Loading error={error} />;
  const cards = d.cards as CardT[];
  const best0 = (d.best_today as string[]).map((id) => cards.find((c) => c.id === id)).filter(Boolean) as CardT[];
  const best = bestSort === "net" ? [...best0].sort((a, b) => (b.net_usd[0] + b.net_usd[1]) / b.deposit_usd - (a.net_usd[0] + a.net_usd[1]) / a.deposit_usd) : best0;
  const c = cards.find((x) => x.id === picked) ?? best[0];
  const g = c?.amount_guide;
  const amount = choice === "rec" && g?.recommended != null ? g.recommended
    : choice === "min" && g?.min_profit != null ? g.min_profit
    : choice === "own" ? Math.max(0, Number(amountS.replace(/\s/g, "")) || 0) : 0;
  const entry = c?.price ?? 0;
  const r = c && amount > 0 && choice !== "wait" ? plan(c, amount, entry) : null;
  const t0 = c?.token0 === c?.base;
  return (
    <>
      <h1>Сигналы <Badge label="SCENARIO" /></h1>
      <p className="lead">Строгий сигнал: <b>{STATUS_RU[d.strict.status] ?? d.strict.status}</b>. {d.strict.text}</p>

      <h2 className="section">1. Лучшие стратегии на {date(d.ranking_at ?? d.scan_at).split(",")[0]}</h2>
      <div className="tabs"><span className="muted">порядок:</span>
        {[["tier", "сначала надёжнее"], ["net", "сначала больше заработок"]].map(([k, t]) =>
          <button key={k} className={`pill${bestSort === k ? " warn" : ""}`} onClick={() => setBestSort(k)}>{t}</button>)}</div>
      {best.length === 0 ? <Card><p>Сегодня подходящих вариантов нет — система не создаёт сигналы ради заполнения экрана.</p></Card> :
        best.map((b) => (
          <button key={b.id} className={`card pick${c?.id === b.id ? " picked" : ""}`} onClick={() => setPicked(b.id)}>
            <div className="card-head"><h2>{b.pair} · {pctAbs(b.fee_tier, 2)} · {b.network_ru ?? "Ethereum"}</h2>
              <div className="card-right"><Pill tone={TIER_RU[b.tier].tone}>{TIER_RU[b.tier].name}</Pill>
                <Pill tone={b.risk === "low" ? "pos" : b.risk === "medium" ? "warn" : "neg"}>риск {RISK_RU[b.risk]}</Pill><Badge label={b.label} /></div></div>
            <p>{b.strategy_ru ?? b.strategy}</p>
            <p className="muted">Диапазон {fp(b.price_lower)} – {fp(b.price_upper)} {b.quote} · за {b.horizon_days} дн. на $1 000:{" "}
              <span className={sign(b.net_usd[0] + b.net_usd[1])}>{usd(b.net_usd[0] / b.deposit_usd * 1000, 2)} … {usd(b.net_usd[1] / b.deposit_usd * 1000, 2)}</span> к хранению
              {c?.id === b.id && <b className="vpos"> · выбрано</b>}</p>
          </button>))}

      {c && <>
        <h2 className="section">2. Сколько вложить</h2>
        <Card>
          {g?.recommended != null ? <p className="hero">Рекомендуемая сумма: <b>{usd(g.recommended)}</b></p>
            : <p className="hero warn">Рекомендуемой суммы нет</p>}
          <p className="muted">{g?.why}. Больше рекомендуемой можно — доход и возможный убыток растут вместе с суммой; решаете вы.
            Не вкладывайте деньги, потерю которых не можете себе позволить.</p>
          <div className="tabs">
            {g?.recommended != null && <button className={`pill${choice === "rec" ? " warn" : ""}`} onClick={() => setChoice("rec")}>взять рекомендуемую {usd(g.recommended)}</button>}
            {g?.min_profit != null && <button className={`pill${choice === "min" ? " warn" : ""}`} onClick={() => setChoice("min")}>минимальная для дохода {usd(g.min_profit)}</button>}
            <button className={`pill${choice === "own" ? " warn" : ""}`} onClick={() => setChoice("own")}>своя сумма</button>
            <button className={`pill${choice === "wait" ? " warn" : ""}`} onClick={() => setChoice("wait")}>подождать других вариантов</button>
          </div>
          {choice === "own" && <div className="capital"><input inputMode="decimal" value={amountS} onChange={(ev) => setAmountS(ev.target.value)} /> <span>USD</span>
            {g?.breakeven != null && amount > 0 && amount < g.breakeven && <span className="neg">меньше ~{usd(g.breakeven)} — по расчёту дохода не будет: газ больше заработка</span>}
            {g?.min != null && g?.breakeven != null && amount >= g.breakeven && amount < g.min && <span className="warn">меньше ~{usd(g.min)} — газ съест заметную часть дохода</span>}
            {g?.max != null && amount > g.max && <span className="warn">больше ~{usd(g.max)} — слишком большая доля в пуле, расчёт ненадёжен</span>}</div>}
          {choice === "min" && g?.breakeven != null && <p className="muted">Минимальная сумма — та, при которой ожидаемые комиссии
            покрывают газ, обмен и обычную потерю к хранению (порог ~{usd(g.breakeven)}). Доход при ней небольшой, а в плохом сценарии
            возможен минус; с рекомендуемой суммой газ съедает меньшую часть дохода.</p>}
          {choice === "wait" && <div>
            <p>Хорошо, ничего не открываем. Система пересчитывает варианты каждые 15 минут и раз в день ищет новые стратегии;
              если появится более проверенный вариант, придёт уведомление.</p>
            {c.to_improve.length > 0 && <p className="muted">Чтобы этот вариант стал надёжнее: {c.to_improve.join("; ")}.</p>}
          </div>}
        </Card>

        {r && <>
          <h2 className="section">3. Расчёт на {usd(amount)}</h2>
          <Card right={<Badge label={c.label} />}>
            <p className="hero">Диапазон: <b>{fp(r.Pa)} – {fp(r.Pb)} {c.quote}</b> за 1 {c.base}<br />
              Внести: <b>{num(r.x, c.base === "USDC" ? 2 : 6)} {c.base}</b> + <b>{num(r.y, c.quote === "USDC" ? 2 : 6)} {c.quote}</b> ({pctAbs(1 - r.baseShare, 0)} в {c.quote})<br />
              За {c.horizon_days} дн. по сравнению с простым хранением: <b className={sign(r.net[0] + r.net[1])}>{usd(r.net[0], 2)} … {usd(r.net[1], 2)}</b> — оценка модели, не гарантия</p>
            <div className="kv">
              <span>Комиссии (доход)</span><span>{usd(r.fees[0], 2)} – {usd(r.fees[1], 2)}</span>
              <span>Потеря к хранению при обычном движении цены</span><span>{usd(r.il, 2)}</span>
              <span>Газ (открыть + закрыть)</span><span>{usd(c.gas_usd, 2)}{c.gas_note && <span className="muted"> · {c.gas_note}</span>}</span>
              <span>Обмен части {c.quote} на {c.base}</span><span>{usd(r.swap, 2)}</span>
              <span>Шанс, что цена выйдет из диапазона за {c.horizon_days} дн.</span><span>{pctAbs(c.exit_probability, 0)}</span>
              {r.breakEven != null && <><span>Минимальная разумная сумма</span><span className={amount < r.breakEven ? "warn" : ""}>~{usd(r.breakEven)} (иначе газ съедает доход)</span></>}
            </div>
            <h3>Если цена изменится</h3>
            <div className="table-wrap"><table className="table"><thead><tr><th>Цена {c.base}</th><th>Ваша позиция</th><th>Если бы просто держали</th><th>Разница</th></tr></thead>
              <tbody>{r.scen.map((s) => <tr key={s.change}><td>{pct(s.change, 0)} ({fp(entry * (1 + s.change))})</td>
                <td className={sign(s.position)}>{usd(s.position, 2)}</td><td className={sign(s.hold)}>{usd(s.hold, 2)}</td>
                <td className={sign(s.vsHold)}>{usd(s.vsHold, 2)}</td></tr>)}</tbody></table></div>
            <p className="muted">В таблице без комиссий. Когда цена выходит за диапазон, позиция целиком становится одной монетой и комиссии прекращаются.</p>
          </Card>

          <Card title="Почему этот вариант и что может пойти не так">
            <p><b>Почему:</b> {c.why.join("; ")}.</p>
            <p><b>Главный риск:</b> {c.risk_why}.</p>
            {c.blockers.length > 0 && <p><b>Чего не хватает до более надёжного уровня:</b> {c.blockers.join("; ")}.</p>}
            <p><b>Отменить, если:</b> {c.invalidation.join("; ")}. Сигнал действует до {date(c.valid_until)}.</p>
            {c.token_risks && <p className="muted">{c.token_risks.join(" ")}</p>}
          </Card>

          <Card title="4. Как открыть на Uniswap (делаете вы сами)">
            <ol>
              <li>Откройте <b>app.uniswap.org</b> (набирайте адрес вручную) и подключите свой кошелёк.</li>
              <li>Раздел «Pool» → «New position» → сеть <b>{c.network_ru ?? "Ethereum"}</b>
                {c.network_ru && c.network_ru !== "Ethereum" && <> (деньги должны уже быть в этой сети — перевод через мост делаете отдельно)</>}.</li>
              <li>Пара <b>{c.token0} / {c.token1}</b>, комиссия <b>{pctAbs(c.fee_tier, 2)}</b>. Адрес пула для проверки: <code>{c.pool_address}</code>.</li>
              <li>Диапазон: min <b>{fp(t0 ? r.Pa : 1 / r.Pb)}</b>, max <b>{fp(t0 ? r.Pb : 1 / r.Pa)}</b> {t0 ? `${c.token1} за 1 ${c.token0}` : `${c.token1} за 1 ${c.token0}`}
                {!t0 && <> (в Uniswap можно переключить отображение на «{c.quote} за 1 {c.base}»: {fp(r.Pa)} – {fp(r.Pb)})</>}.</li>
              <li>Суммы: <b>{num(r.x, 6)} {c.base}</b> и <b>{num(r.y, c.quote === "USDC" ? 2 : 6)} {c.quote}</b> (Uniswap подставит вторую сумму сам).</li>
              <li>Проверьте газ в кошельке и подтвердите. Никому не сообщайте секретную фразу.</li>
            </ol>
          </Card>

          <Card title="5. Выбрать этот сигнал и отслеживать">
            <p>Нажмите кнопку — откроется GitHub с готовой заявкой. Нажмите там <b>«Create»</b>. В течение 15 минут система начнёт
              вести эту позицию в разделе <b>«Мои позиции»</b>: стоимость, комиссии, итог и совет — держать, переставить диапазон,
              выйти или переехать. При смене совета придёт уведомление в приложение GitHub. Закончили — закройте заявку.</p>
            <a className="pill warn" style={{ display: "inline-block", padding: "8px 14px", fontSize: 14 }} target="_blank" rel="noopener"
              href={trackUrl(c, amount, r)}>Выбрать этот сигнал</a>
            <p className="muted">Отслеживается диапазон {fp(r.Pa)} – {fp(r.Pb)} на {usd(amount)} — как вы его откроете в Uniswap
              (диапазон сам не переставляется; когда нужно — система подскажет).</p>
          </Card>
        </>}
      </>}

      <div className="warnbox">{d.risk_warning}</div>
      <details><summary>Все варианты ({cards.length}) и диагностика</summary>
        {cards.map((x) => <SignalCard key={x.id} c={x} capital={null} share={1} />)}
        <h3>Пулы с проблемами</h3>
        <ul>{(d.pools_with_problems as Any[]).map((p) => <li key={p.pair + p.fee_tier}>{p.pair} {pctAbs(p.fee_tier, 2)}: {p.problems.join("; ")}</li>)}</ul>
        <h3>Стратегии</h3>
        <ul>{(d.diagnostics as Any[]).slice(0, 12).map((x) => <li key={x.strategy}>{x.strategy}: {x.blockers_ru.join("; ")}</li>)}</ul>
      </details>
    </>);
}

// ---------------------------------------------------------------- Portfolio
function PosCard({ p }: { p: Any }) {
  const m = p.mark ?? {};
  const v = (x: number | null | undefined, d = 2) => (x == null ? "—" : usd(x, d));
  const cell = (label: string, value: string, tone = "") => <div><span>{label}</span><b className={tone}>{value}</b></div>;
  return (
    <div className="vpos">
      <div className="vpos-head"><b>{p.pair}</b> · {p.network_ru ?? "Ethereum"} · {TIER_RU[p.tier]?.name}
        <span className="vpos-tags"><Badge label={p.kind === "exact" ? "MODELED" : "ESTIMATED"} />
          {m.in_range != null && <Pill tone={m.in_range ? "pos" : "neg"}>{m.in_range ? "в диапазоне" : "вне диапазона"}</Pill>}</span></div>
      <div className="vpos-desc">{p.strategy_ru ?? p.strategy}</div>
      {p.entry.forecast_note && <div className="vpos-desc muted">⚠ {p.entry.forecast_note}</div>}
      <div className="vpos-grid">
        {cell("Открыта", date(p.opened_at).replace(/\s\d{4}\sг\.,/, ",").replace(" МСК", ""))}
        {cell("Цена входа", num(p.entry.price, p.entry.price >= 1000 ? 0 : 2))}
        {cell("Диапазон", `${num(p.entry.range[0], p.entry.range[0] >= 1000 ? 0 : 2)} – ${num(p.entry.range[1], p.entry.range[1] >= 1000 ? 0 : 2)}`)}
        {cell("Цена сейчас", m.price != null ? num(m.price, m.price >= 1000 ? 0 : 2) : "—")}
        {cell("Вложено", usd(p.allocation_usd))}
        {cell("Стоимость", v(m.value_usd))}
        {cell("Итог", v(m.net_usd), sign(m.net_usd))}
        {cell("К хранению", v(m.vs_hodl_usd), sign(m.vs_hodl_usd))}
        {cell("Комиссии", v(m.fees_usd), "pos")}
        {cell("Потеря к хранению", v(m.il_usd), sign(m.il_usd))}
        {cell("Расходы", v(m.costs_usd))}
        {cell("Время в диапазоне", m.time_in_range != null ? pctAbs(m.time_in_range, 0) : "—")}
      </div>
      {p.close_reason && <div className="vpos-desc">Закрыта: {p.close_reason}</div>}
    </div>);
}

export function Portfolio() {
  const { data: d, error } = useApi<Any>("/api/lab/portfolio");
  if (!d) return <Loading error={error} />;
  return (
    <>
      <h1>Виртуальный портфель <Badge label="MODELED" /></h1>
      <p className="lead">Виртуальные деньги, без транзакций. Позиции открываются по лучшим возможностям и ведутся на новых данных;
        цена, диапазон и состав фиксируются при открытии. Капитал задаётся в <code>config/lab.yaml</code> (portfolio.virtual_capital_usd).</p>
      <div className="grid stats">
        <Stat title="Начальный баланс" value={usd(d.initial_usd)} />
        <Stat title="Текущий баланс" label="MODELED" value={usd(d.balance_usd, 2)} tone={sign(d.pnl_usd)} hint={`свободно ${usd(d.cash_usd)}`} />
        <Stat title="Прибыль / убыток" value={usd(d.pnl_usd, 2)} tone={sign(d.pnl_usd)} hint={`за сутки ${d.day_change_usd == null ? "—" : usd(d.day_change_usd, 2)}`} />
        <Stat title="Из чего итог" value={<div className="stat-lines">
          <div><span>комиссии </span><b className="pos">{usd(d.fees_usd, 2)}</b></div>
          <div><span>потеря к хранению </span><b className={sign(d.il_usd)}>{usd(d.il_usd, 2)}</b></div>
          <div><span>расходы </span><b>{usd(d.costs_usd, 2)}</b></div></div>} />
      </div>
      {d.history.length > 1 && <Card title="Баланс по дням"><Spark values={d.history.map((h: Any) => h.balance_usd)} /></Card>}
      <Card title={`Открытые позиции: ${d.open.length}`}>
        {d.open.length === 0 ? <p className="muted">Открытых позиций нет.</p> :
          d.open.map((p: Any) => <PosCard key={p.id} p={p} />)}
      </Card>
      <Card title={`Закрытые позиции: ${d.closed.length}`}>
        {d.closed.length === 0 ? <p className="muted">Закрытых пока нет (позиция закрывается через 30 дней или когда стратегия выпадает из рейтинга на 3 дня).</p> :
          d.closed.map((p: Any) => <PosCard key={p.id} p={p} />)}
      </Card>
      <p className="muted">МОДЕЛЬ — точная реконструкция пула WBTC/USDC 0,3%; ОЦЕНКА — часовые данные других пулов (приближённо).</p>
    </>);
}

// ---------------------------------------------------------------- Research
export function Research() {
  const { data: d, error } = useApi<Any>("/api/lab/research");
  if (!d) return <Loading error={error} />;
  const f = d.foundation;
  return (
    <>
      <h1>Исследования</h1>
      <Card title="Фундамент: историческое исследование" label="MODELED">
        <p>{f.pool}: {num(f.experiments, 0)} расчётов, {num(f.strategies, 0)} стратегий, период {f.period}. {f.note}.</p>
      </Card>
      <Card title="Чему система научилась на ошибках прогнозов">
        {d.lessons.length === 0 ? <p className="muted">Первые проверки прогнозов будут через 7 дней после открытия виртуальных позиций
          ({d.forecasts.length ? date(Math.min(...d.forecasts.map((x: Any) => x.due))) : "—"}). Сейчас идут промежуточные сравнения.</p> :
          <ul>{d.lessons.map((l: Any, i: number) => <li key={i}>{date(l.at)} · {l.strategy}: {l.reasons.join(", ") || "прогноз совпал"} → {l.action}</li>)}</ul>}
        <p className="muted">Калибровка сканера: {d.scanner_calibration?.factor ? `часовая оценка комиссий умножается на ${num(d.scanner_calibration.factor, 2)} (сверка с точной моделью)` : "ещё не рассчитана"}.
          {" "}Калибровка прогнозов: {d.fee_calibration_forecasts.factor ? num(d.fee_calibration_forecasts.factor, 2) : d.fee_calibration_forecasts.note}.</p>
      </Card>
      <Card title="Прогнозы на проверке" label="SCENARIO">
        {d.forecasts.length === 0 ? <p className="muted">Прогнозов пока нет.</p> :
          <div className="table-wrap"><table className="table"><thead><tr><th>Стратегия</th><th>Прогноз комиссий</th><th>Факт сейчас</th><th>Причины расхождения</th><th>Итоговая проверка</th></tr></thead>
            <tbody>{d.forecasts.map((x: Any) => <tr key={x.id}><td>{x.strategy}<div className="muted">{TIER_RU[x.tier]?.name}</div></td>
              <td>{usd(x.forecast.fees_usd[0], 2)} – {usd(x.forecast.fees_usd[1], 2)} за 7 дн.</td>
              <td>{x.checkpoint ? <>{usd(x.checkpoint.fees_actual, 2)} за {num(x.checkpoint.days, 1)} дн. (ожидалось {usd(x.checkpoint.fees_forecast[0], 2)}–{usd(x.checkpoint.fees_forecast[1], 2)})</> : "ждёт данных"}</td>
              <td>{x.checkpoint?.reasons?.length ? x.checkpoint.reasons.map((r: string) => d.reason_names[r] ?? r).join("; ") : "—"}</td>
              <td>{x.status === "reviewed" ? "проверено" : date(x.due)}</td></tr>)}</tbody></table></div>}
      </Card>
      <Card title={`Гипотезы: ${d.hypotheses.length}`}>
        <p className="muted">Гипотезы создаёт сама система (правила обучения; с ключом Claude — ещё и языковая модель).
          Проверка — на всей сохранённой истории; «подтверждено» значит: на данных, которые стратегия не видела, была лучше простого размещения.
          Если параметры придуманы после просмотра этих данных, это отмечено — такая проверка слабее.</p>
        {d.hypotheses.map((h: Any) => (
          <details key={h.id}><summary><Pill tone={h.verdict === "confirmed" ? "pos" : h.verdict === "rejected" ? "neg" : "warn"}>
            {h.verdict === "confirmed" ? "подтверждено" : h.verdict === "rejected" ? "отклонено" : h.status}</Pill> {date(h.created_at)} · {h.title}</summary>
            <p>{h.description}</p><p className="muted">{h.rationale}</p><p>{h.conclusion}</p>
            {h.oos_contaminated && <p className="warn">Параметры выбраны после просмотра проверочных данных — нужна проверка в виртуальном портфеле.</p>}
            <ul>{h.strategies.map((s: Any) => <li key={s.strategy}>{s.strategy}: {s.verdict}, к простому размещению {pct(s.oos_vs_passive)} ({s.positive}/7)</li>)}</ul>
          </details>))}
      </Card>
      <Card title="Дальше в плане">
        {d.next_tasks.length === 0 ? <p className="muted">План появится после ежедневного исследования.</p> :
          <ul>{d.next_tasks.map((t: Any) => <li key={t.title}>{t.title}</li>)}</ul>}
      </Card>
      <p className="muted">Достоверность: {Object.entries(CONF_RU).map(([k, v]) => `${k} = ${v}`).join(", ")}.</p>
    </>);
}

// ---------------------------------------------------------------- Activity log
export function Activity() {
  const { data: d, error } = useApi<Any>("/api/lab/activity");
  const [only, setOnly] = useState("all");
  if (!d) return <Loading error={error} />;
  const items = (d.items as Any[]).filter((a) => only === "all" || a.status === only);
  return (
    <>
      <h1>Журнал действий</h1>
      <p className="lead">Реальные действия системы по времени (МСК). Ошибки и пропуски записываются как есть.</p>
      <div className="tabs">{[["all", "все"], ["error", "ошибки"], ["warn", "предупреждения"], ["ok", "успешно"]].map(([k, t]) =>
        <button key={k} className={`pill${only === k ? " warn" : ""}`} onClick={() => setOnly(k)}>{t}</button>)}</div>
      <Card>{items.map((a) => <div className="log-item" key={a.id}><span className="muted">{date(a.at)}</span>
        <span><Pill tone={STATUS_TONE[a.status]}>{KIND_RU[a.kind] ?? a.kind}</Pill> {a.message}</span></div>)}</Card>
    </>);
}

// ---------------------------------------------------------------- My bot
export function Bot() {
  const { data: d, error } = useApi<Any>("/api/lab/bot");
  if (!d) return <Loading error={error} />;
  const c = d.current, m = c?.mark ?? {}, e = c?.entry;
  const NET: Record<string, string> = { ethereum: "Ethereum", base: "Base", arbitrum: "Arbitrum", optimism: "Optimism" };
  return (
    <>
      <h1>Мой бот <Badge label="ESTIMATED" /></h1>
      <p className="lead">Виртуальный двойник: начал с {usd(d.start_usd)} и сам выбирает, куда их поставить — так, как это сделали бы вы.
        Одна позиция за раз, результат вкладывается обратно. Без реальных денег и транзакций.</p>
      <div className="grid stats">
        <Stat title="Начал с" value={usd(d.start_usd)} />
        <Stat title="Сейчас" value={usd(d.balance_usd, 2)} tone={sign(d.pnl_usd)} hint={`${usd(d.pnl_usd, 2)} (${pct(d.pnl_pct, 2)})`} />
        <Stat title="Позиций было" value={(d.closed.length + (c ? 1 : 0)).toString()} hint={`закрыто ${d.closed.length}`} />
        <Stat title="Сейчас в" value={c ? `${NET[c.network] ?? c.network}` : "ждёт"} hint={c ? c.pair : ""} />
      </div>
      {d.status && <p className="muted">Последняя проверка ботом: {date(d.status.at)} — {d.status.text}.</p>}
      {d.history.length > 1 && <Card title="Баланс по дням"><Spark values={d.history.map((h: Any) => h.balance_usd)} /></Card>}
      <Card title="Текущая позиция">
        {!c ? <p className="muted">Позиции нет: бот ждёт подходящего варианта (см. журнал решений).</p> : <>
          <div className="vpos-head"><b>{c.pair}</b> · {NET[c.network] ?? c.network} · диапазон ±{pctAbs(c.width, 0)}
            <span className="vpos-tags">{m.in_range != null && <Pill tone={m.in_range ? "pos" : "neg"}>{m.in_range ? "в диапазоне" : "вне диапазона"}</Pill>}</span></div>
          <div className="vpos-desc">{e.why}</div>
          <div className="vpos-grid">
            {([["Открыта", date(c.opened_at).replace(/\s\d{4}\sг\.,/, ",").replace(" МСК", "")],
              ["Цена входа", num(e.price, e.price >= 1000 ? 0 : 2)],
              ["Диапазон", `${num(e.range[0], e.range[0] >= 1000 ? 0 : 2)} – ${num(e.range[1], e.range[1] >= 1000 ? 0 : 2)}`],
              ["Цена сейчас", m.price != null ? num(m.price, m.price >= 1000 ? 0 : 2) : "—"],
              ["Вложено", usd(c.allocation_usd, 2)], ["Стоимость", m.value_usd != null ? usd(m.value_usd, 2) : "—"],
              ["Итог", m.net_usd != null ? usd(m.net_usd, 2) : "—", sign(m.net_usd)],
              ["К хранению", m.vs_hodl_usd != null ? usd(m.vs_hodl_usd, 2) : "—", sign(m.vs_hodl_usd)],
              ["Комиссии", m.fees_usd != null ? usd(m.fees_usd, 2) : "—", "pos"],
              ["Потеря к хранению", m.il_usd != null ? usd(m.il_usd, 2) : "—", sign(m.il_usd)],
              ["Расходы", m.costs_usd != null ? usd(m.costs_usd, 2) : "—"],
              ["Прогноз за неделю", `${usd(e.forecast.net_vs_hodl_usd[0], 2)} … ${usd(e.forecast.net_vs_hodl_usd[1], 2)}`],
              ["Время в диапазоне", m.time_in_range != null ? pctAbs(m.time_in_range, 0) : "—"]] as [string, string, string?][])
              .map(([l, v, tone]) => <div key={l}><span>{l}</span><b className={tone ?? ""}>{v}</b></div>)}
          </div></>}
      </Card>
      <Card title="Журнал решений бота">
        {d.decisions.length === 0 ? <p className="muted">Решений пока нет — бот начнёт со следующего обновления.</p> :
          d.decisions.map((a: Any) => <div className="log-item" key={a.id}><span className="muted">{date(a.at)}</span><span>{a.message}</span></div>)}
      </Card>
      {d.closed.length > 0 && <Card title={`Закрытые позиции: ${d.closed.length}`}>
        <ul>{d.closed.map((p: Any) => <li key={p.id}>{date(p.opened_at)} → {date(p.closed_at)} · {p.pair} ({NET[p.network] ?? p.network}) ±{pctAbs(p.width, 0)}:
          <b className={sign(p.mark?.net_usd)}> {usd(p.mark?.net_usd, 2)}</b> — {p.close_reason}</li>)}</ul></Card>}
      <Card title="Как бот принимает решения">
        <ul>
          <li>Выбирает вариант, который выгоден именно для его суммы: газ платится целиком при любой сумме, поэтому с небольшими деньгами выгоднее дешёвые сети.</li>
          <li>Не берёт диапазон, из которого цена за неделю выйдет с шансом больше {pctAbs(d.rules.max_exit_probability, 0)}.</li>
          <li>Если цена вне диапазона дольше {d.rules.out_of_range_hours} ч — закрывает и ставит новый диапазон (или переезжает).</li>
          <li>Переезжает в другой вариант не раньше чем через {d.rules.min_hold_days} дн. и только если тот лучше на {pctAbs(d.rules.switch_margin, 0)} с учётом расходов на переезд.</li>
          <li>Учится: если в какой-то сети комиссий выходит меньше прогноза, меньше доверяет прогнозам этой сети
            {Object.keys(d.trust).length > 0 && <> (сейчас: {Object.entries(d.trust).map(([k, v]) => `${NET[k] ?? k} ${num(v as number, 2)}`).join(", ")})</>}.</li>
        </ul>
        <p className="muted">Расчёт по часовым данным пулов (ОЦЕНКА). Перевод денег между сетями (мост) в расчёте не учтён.</p>
      </Card>
    </>);
}


// ---------------------------------------------------------------- My positions (chosen signals)
const ADVICE_TONE: Record<string, string> = { hold: "pos", out: "warn", out_long: "neg", move: "warn", review: "" };
const short = (ts: number) => date(ts).replace(/\s\d{4}\sг\.,/, ",").replace(" МСК", "");

export function Positions() {
  const { data: d, error } = useApi<Any>("/api/lab/positions");
  if (!d) return <Loading error={error} />;
  const NET: Record<string, string> = { ethereum: "Ethereum", base: "Base", arbitrum: "Arbitrum", optimism: "Optimism" };
  const card = (p: Any) => {
    const m = p.mark ?? {}, a = p.advice;
    const cells: [string, string, string?][] = [
      ["Выбран", short(p.created_at)],
      ["Цена входа", num(p.entry.price, p.entry.price >= 1000 ? 0 : 2)],
      ["Диапазон", `${num(p.range[0], p.range[0] >= 1000 ? 0 : 2)} – ${num(p.range[1], p.range[1] >= 1000 ? 0 : 2)}`],
      ["Цена сейчас", m.price != null ? num(m.price, m.price >= 1000 ? 0 : 2) : "—"],
      ["Стоимость", m.value_usd != null ? usd(m.value_usd, 2) : "—"], ["Итог", m.net_usd != null ? usd(m.net_usd, 2) : "—", sign(m.net_usd)],
      ["Комиссии", m.fees_usd != null ? usd(m.fees_usd, 2) : "—", "pos"], ["Потеря к хранению", m.il_usd != null ? usd(m.il_usd, 2) : "—", sign(m.il_usd)],
      ["Расходы (оценка)", usd(p.entry.costs_usd, 2)], ["К хранению", m.vs_hodl_usd != null ? usd(m.vs_hodl_usd, 2) : "—", sign(m.vs_hodl_usd)],
      ["Время в диапазоне", m.time_in_range != null ? pctAbs(m.time_in_range, 0) : "—"],
      ["Прогноз за неделю", p.entry.card?.net_usd ? `${usd(p.entry.card.net_usd[0], 2)} … ${usd(p.entry.card.net_usd[1], 2)}` : "—"]];
    return (
      <div className="vpos" key={p.id}>
        <div className="vpos-head"><b>{p.pair}</b> · {NET[p.network] ?? p.network} · {usd(p.amount_usd)}
          <span className="vpos-tags">{a && <Pill tone={ADVICE_TONE[a.code]}>{a.title}</Pill>}</span></div>
        {a && <p>{a.text}</p>}
        <div className="vpos-grid">{cells.map(([l, v, tone]) => <div key={l}><span>{l}</span><b className={tone ?? ""}>{v}</b></div>)}</div>
        {p.history.length > 0 && <details><summary>История советов ({p.history.length})</summary>
          <ul>{p.history.map((h: Any, i: number) => <li key={i}>{date(h.at)} · {h.title} (итог тогда {usd(h.net_usd, 2)})</li>)}</ul></details>}
        <p className="muted"><a href={`https://github.com/${d.repo}/issues/${p.issue}`} target="_blank" rel="noopener">заявка #{p.issue} на GitHub</a>
          {p.status === "open" ? " — закройте её, когда выйдете из позиции" : ` — закрыта ${date(p.closed_at)}`}</p>
      </div>);
  };
  return (
    <>
      <h1>Мои позиции <Badge label="ESTIMATED" /></h1>
      <p className="lead">Сигналы, которые вы выбрали кнопкой «Выбрать этот сигнал». Система ведёт их с момента выбора и
        подсказывает, когда держать, когда выходить или переставлять диапазон. Расчёт — по часовым данным пула (оценка).</p>
      <Card title={`Открытые: ${d.open.length}`}>
        {d.open.length === 0 ? <p className="muted">Пока нет. Выберите вариант в «Сигналах» и нажмите «Выбрать этот сигнал»
          (заявка появится здесь в течение 15 минут).</p> : d.open.map(card)}
      </Card>
      {d.closed.length > 0 && <Card title={`Закрытые: ${d.closed.length}`}>{d.closed.map(card)}</Card>}
    </>);
}
