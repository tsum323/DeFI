// Typed fetch helpers for the read-only dashboard API.
export type Label = "EXACT" | "MODELED" | "SCENARIO";
export interface Labeled<T> { value: T; label: Label }

// Static snapshot build (VITE_STATIC=1): data comes from JSON files exported next to the page.
export const STATIC = import.meta.env.VITE_STATIC === "1";

function staticPath(path: string): string {
  const m = path.match(/^\/api\/strategies\/([0-9a-f]+)$/);
  if (m) return `data/strategy_${m[1]}.json`;
  return `data/${path.replace(/^\/api\//, "")}.json`;
}

export async function getJSON<T>(path: string): Promise<T> {
  // static site: bypass the CDN/browser cache so a live update is seen immediately
  const url = STATIC ? `${staticPath(path)}?v=${Date.now()}` : path;
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return (await r.json()) as T;
}

export interface RankingRow {
  spec_hash: string; name: string; pair: string; range: string; rule: string;
  allocation_usdc: number | null; fees: number | null; il: number | null; net: number | null;
  roi: number | null; max_drawdown: number; sharpe: number | null; oos_roi: number | null;
  oos_vs_passive: number | null; segments: number; score: number | null; flags: string[];
  ruined_segments: number; has_details?: boolean;
}
export interface Ranking { rows: RankingRow[]; label: Label; per: string; code_version: string }
export interface SnapshotInfo { exported_at: number; last_block: number; last_block_ts: number;
  live?: boolean; every_minutes?: number }
export const DATA_UPDATED = "lab-data-updated";
