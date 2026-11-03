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
        <Stat title="За сутки" label="MODELED" value={p.day_change_usd == null ? "—" : usd(p.day_change_usd, 2)} tone={sign(p.day_change_usd)}
          hint="изменение баланса портфеля" />
      </div>
      {d.errors_24h.length > 0 && <Card title="Ошибки за сутки"><ul>{d.errors_24h.map((e: string) => <li key={e} className="neg">{e}</li>)}</ul></Card>}
      <Card title="Лучшие варианты сейчас" right={<Badge label="SCENARIO" />}>
        {d.best.length === 0 ? <p className="muted">Интересных вариантов сейчас нет — система продолжает исследование.</p> :
          <ul>{d.best.map((c: CardT) => <li key={c.id}><b>{c.pair}</b> {pctAbs(c.fee_tier, 2)} · {TIER_RU[c.tier].name.toLowerCase()} ·
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

export function Signals() {
  const { data: d, error } = useApi<Any>("/api/lab/signals");
  const [picked, setPicked] = useStored("picked_card", "");
  const [amountS, setAmountS] = useStored("invest_amount", "1000");
  const [choice, setChoice] = useStored("amount_choice", "rec");
  if (!d) return <Loading error={error} />;
  const cards = d.cards as CardT[];
  const best = (d.best_today as string[]).map((id) => cards.find((c) => c.id === id)).filter(Boolean) as CardT[];
  const c = cards.find((x) => x.id === picked) ?? best[0];
  const g = c?.amount_guide;
  const amount = choice === "rec" && g?.recommended != null ? g.recommended
    : choice === "own" ? Math.max(0, Number(amountS.replace(/\s/g, "")) || 0) : 0;
  const entry = c?.price ?? 0;
  const r = c && amount > 0 && choice !== "wait" ? plan(c, amount, entry) : null;
  const t0 = c?.token0 === c?.base;
  return (
    <>
      <h1>Сигналы <Badge label="SCENARIO" /></h1>
      <p className="lead">Строгий сигнал: <b>{STATUS_RU[d.strict.status] ?? d.strict.status}</b>. {d.strict.text}</p>

      <h2 className="section">1. Лучшие стратегии на {date(d.ranking_at ?? d.scan_at).split(",")[0]}</h2>
      {best.length === 0 ? <Card><p>Сегодня подходящих вариантов нет — система не создаёт сигналы ради заполнения экрана.</p></Card> :
        best.map((b) => (
          <button key={b.id} className={`card pick${c?.id === b.id ? " picked" : ""}`} onClick={() => setPicked(b.id)}>
            <div className="card-head"><h2>{b.pair} · {pctAbs(b.fee_tier, 2)}</h2>
              <div className="card-right"><Pill tone={TIER_RU[b.tier].tone}>{TIER_RU[b.tier].name}</Pill>
                <Pill tone={b.risk === "low" ? "pos" : b.risk === "medium" ? "warn" : "neg"}>риск {RISK_RU[b.risk]}</Pill><Badge label={b.label} /></div></div>
            <p>{b.strategy_ru ?? b.strategy}</p>
            <p className="muted">Диапазон {fp(b.price_lower)} – {fp(b.price_upper)} {b.quote} · за {b.horizon_days} дн. на $1 000:{" "}
              <span className={sign(b.net_usd[0] + b.net_usd[1])}>{usd(b.net_usd[0] / b.deposit_usd * 1000, 2)} … {usd(b.net_usd[1] / b.deposit_usd * 1000, 2)}</span> к хранению
              {c?.id === b.id && <b className="pos"> · выбрано</b>}</p>
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
            <button className={`pill${choice === "own" ? " warn" : ""}`} onClick={() => setChoice("own")}>своя сумма</button>
            <button className={`pill${choice === "wait" ? " warn" : ""}`} onClick={() => setChoice("wait")}>подождать других вариантов</button>
          </div>
          {choice === "own" && <div className="capital"><input inputMode="decimal" value={amountS} onChange={(ev) => setAmountS(ev.target.value)} /> <span>USD</span>
            {g?.min != null && amount > 0 && amount < g.min && <span className="warn">меньше ~{usd(g.min)} — газ съест заметную часть дохода</span>}
            {g?.max != null && amount > g.max && <span className="warn">больше ~{usd(g.max)} — слишком большая доля в пуле, расчёт ненадёжен</span>}</div>}
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
              <span>Газ (открыть + закрыть)</span><span>{usd(c.gas_usd, 2)}</span>
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
              <li>Раздел «Pool» → «New position» → сеть <b>Ethereum</b>.</li>
              <li>Пара <b>{c.token0} / {c.token1}</b>, комиссия <b>{pctAbs(c.fee_tier, 2)}</b>. Адрес пула для проверки: <code>{c.pool_address}</code>.</li>
              <li>Диапазон: min <b>{fp(t0 ? r.Pa : 1 / r.Pb)}</b>, max <b>{fp(t0 ? r.Pb : 1 / r.Pa)}</b> {t0 ? `${c.token1} за 1 ${c.token0}` : `${c.token1} за 1 ${c.token0}`}
                {!t0 && <> (в Uniswap можно переключить отображение на «{c.quote} за 1 {c.base}»: {fp(r.Pa)} – {fp(r.Pb)})</>}.</li>
              <li>Суммы: <b>{num(r.x, 6)} {c.base}</b> и <b>{num(r.y, c.quote === "USDC" ? 2 : 6)} {c.quote}</b> (Uniswap подставит вторую сумму сам).</li>
              <li>Проверьте газ в кошельке и подтвердите. Никому не сообщайте секретную фразу.</li>
            </ol>
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
function PosRow({ p }: { p: Any }) {
  const m = p.mark ?? {};
  return (
    <tr><td><b>{p.pair}</b> · {TIER_RU[p.tier]?.name}<div>{p.strategy_ru ?? p.strategy}</div>
      <div className="muted" style={{ fontSize: 11 }}>{p.strategy}</div></td>
      <td>{date(p.opened_at)}<div className="muted">{num(p.entry.price, 0)} · {num(p.entry.range[0], 0)}–{num(p.entry.range[1], 0)}</div></td>
      <td>{usd(p.allocation_usd)}</td><td>{m.value_usd != null ? usd(m.value_usd, 2) : "—"}</td>
      <td className={sign(m.net_usd)}>{m.net_usd != null ? usd(m.net_usd, 2) : "—"}</td>
      <td>{m.fees_usd != null ? usd(m.fees_usd, 2) : "—"}</td><td className={sign(m.il_usd)}>{m.il_usd != null ? usd(m.il_usd, 2) : "—"}</td>
      <td>{m.costs_usd != null ? usd(m.costs_usd, 2) : "—"}</td>
      <td className={sign(m.vs_hodl_usd)}>{m.vs_hodl_usd != null ? usd(m.vs_hodl_usd, 2) : "—"}</td>
      <td>{m.in_range == null ? "—" : m.in_range ? "да" : "нет"}<div className="muted"><Badge label={p.kind === "exact" ? "MODELED" : "ESTIMATED"} /></div>
        {p.close_reason && <div className="muted">{p.close_reason}</div>}</td></tr>);
}

export function Portfolio() {
  const { data: d, error } = useApi<Any>("/api/lab/portfolio");
  if (!d) return <Loading error={error} />;
  const head = <thead><tr><th>Пара</th><th>Открыта · цена · диапазон</th><th>Вложено</th><th>Стоимость</th><th>Итог</th>
    <th>Комиссии</th><th>Потеря к хранению</th><th>Расходы</th><th>К хранению</th><th>В диапазоне</th></tr></thead>;
  return (
    <>
      <h1>Виртуальный портфель <Badge label="MODELED" /></h1>
      <p className="lead">Виртуальные деньги, без транзакций. Позиции открываются по лучшим возможностям и ведутся на новых данных;
        цена, диапазон и состав фиксируются при открытии. Капитал задаётся в <code>config/lab.yaml</code> (portfolio.virtual_capital_usd).</p>
      <div className="grid stats">
        <Stat title="Начальный баланс" value={usd(d.initial_usd)} />
        <Stat title="Текущий баланс" label="MODELED" value={usd(d.balance_usd, 2)} tone={sign(d.pnl_usd)} hint={`свободно ${usd(d.cash_usd)}`} />
        <Stat title="Прибыль / убыток" value={usd(d.pnl_usd, 2)} tone={sign(d.pnl_usd)} hint={`за сутки ${d.day_change_usd == null ? "—" : usd(d.day_change_usd, 2)}`} />
        <Stat title="Комиссии · потеря к хранению · расходы" value={`${usd(d.fees_usd, 2)} · ${usd(d.il_usd, 2)} · ${usd(d.costs_usd, 2)}`} />
      </div>
      {d.history.length > 1 && <Card title="Баланс по дням"><Spark values={d.history.map((h: Any) => h.balance_usd)} /></Card>}
      <Card title={`Открытые позиции: ${d.open.length}`}>
        {d.open.length === 0 ? <p className="muted">Открытых позиций нет.</p> :
          <div className="table-wrap"><table className="table">{head}<tbody>{d.open.map((p: Any) => <PosRow key={p.id} p={p} />)}</tbody></table></div>}
      </Card>
      <Card title={`Закрытые позиции: ${d.closed.length}`}>
        {d.closed.length === 0 ? <p className="muted">Закрытых пока нет (позиция закрывается через 30 дней или когда стратегия выпадает из рейтинга на 3 дня).</p> :
          <div className="table-wrap"><table className="table">{head}<tbody>{d.closed.map((p: Any) => <PosRow key={p.id} p={p} />)}</tbody></table></div>}
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
