import type { ReactNode } from "react";
import type { Label } from "../api";

const LABEL_RU: Record<Label, string> = { EXACT: "ТОЧНО", MODELED: "МОДЕЛЬ", SCENARIO: "ПРОГНОЗ", ESTIMATED: "ОЦЕНКА" };
const LABEL_HINT: Record<Label, string> = {
  EXACT: "восстановлено из событий блокчейна и сверено с контрактом",
  MODELED: "модельная оценка: бэктест или гипотетическая позиция",
  SCENARIO: "сценарный прогноз, не гарантия",
  ESTIMATED: "приближённая оценка по часовым данным (не точная модель пула)",
};

export function Badge({ label }: { label: Label }) {
  return <span className={`badge badge-${label.toLowerCase()}`} title={LABEL_HINT[label]}>{LABEL_RU[label]}</span>;
}

export function Card({ title, label, children, right }: { title?: string; label?: Label; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="card">
      {(title || label || right) && (
        <header className="card-head">
          <h2>{title}</h2>
          <div className="card-right">{right}{label && <Badge label={label} />}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ title, value, hint, label, tone }: { title: string; value: ReactNode; hint?: ReactNode; label?: Label; tone?: string }) {
  return (
    <div className="stat">
      <div className="stat-title">{title}{label && <Badge label={label} />}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

export function Loading({ error }: { error?: string }) {
  return error ? <div className="error">Ошибка загрузки: {error}</div> : <div className="loading">Загрузка…</div>;
}

export function Pill({ children, tone }: { children: ReactNode; tone?: string }) {
  return <span className={`pill ${tone ?? ""}`}>{children}</span>;
}
