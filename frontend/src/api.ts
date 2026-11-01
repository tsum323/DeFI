// Typed fetch helpers for the read-only dashboard API.
export type Label = "EXACT" | "MODELED" | "SCENARIO";
export interface Labeled<T> { value: T; label: Label }

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

export interface RankingRow {
  spec_hash: string; name: string; pair: string; range: string; rule: string;
  allocation_usdc: number | null; fees: number | null; il: number | null; net: number | null;
  roi: number | null; max_drawdown: number; sharpe: number | null; oos_roi: number | null;
  oos_vs_passive: number | null; segments: number; score: number | null; flags: string[];
  ruined_segments: number;
}
export interface Ranking { rows: RankingRow[]; label: Label; per: string; code_version: string }
