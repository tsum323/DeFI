export const pct = (x: number | null | undefined, d = 1) =>
  x === null || x === undefined || Number.isNaN(x) ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(d)}%`;
export const pctAbs = (x: number | null | undefined, d = 1) =>
  x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`;
export const usd = (x: number | null | undefined, d = 0) =>
  x === null || x === undefined ? "—" :
  `${x < 0 ? "−" : ""}$${Math.abs(x).toLocaleString("ru-RU", { maximumFractionDigits: d, minimumFractionDigits: d })}`;
export const num = (x: number | null | undefined, d = 2) =>
  x === null || x === undefined ? "—" : x.toLocaleString("ru-RU", { maximumFractionDigits: d });
export const date = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleString("ru-RU", { timeZone: "UTC", dateStyle: "medium", timeStyle: "short" }) + " UTC" : "—";
export const day = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleDateString("ru-RU", { timeZone: "UTC" }) : "—";
export const sign = (x: number | null | undefined) =>
  x === null || x === undefined ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "";
