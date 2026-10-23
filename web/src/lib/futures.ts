// Futures view helpers. Mirrors gexlab/futures.py (round_tick, to_futures); the API tests cover the Python side.
import type { FuturesView } from "../api/types";

export const FUTURES_TICKERS = new Set(["SPX", "NDX", "SPY", "QQQ"]);

export interface FutConv {
  ratio: number;
  basis: number;
  tick: number;
  symbol: string; // "/ESZ26"
  root: string; // "/ES"
}

/** Nearest tick, halves up (0.125 -> 0.25), like round_tick in gexlab/futures.py. */
export function roundTick(x: number, tick: number): number {
  return Math.floor(x / tick + 0.5) * tick;
}

/** An index (ratio 1) or ETF level as a futures equivalent on the tick. */
export function toFut(level: number | null | undefined, c: FutConv | null): number | null {
  if (level === null || level === undefined || !Number.isFinite(level) || !c) return null;
  return roundTick(level * c.ratio + c.basis, c.tick);
}

/** "7,812.25" (two decimals: the tick is a quarter point). */
export function futPx(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "n/a";
  return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** "/ES" from "/ESZ26" */
export function rootOf(symbol: string): string {
  return symbol.replace(/[FGHJKMNQUVXZ]\d{2}$/, "");
}

export function convOf(f: FuturesView | null): FutConv | null {
  if (!f || !f.available || !f.convert) return null;
  return { ...f.convert, symbol: f.contract.symbol, root: rootOf(f.contract.symbol) };
}

/** The futures price the user typed, frozen with the index value it was measured against. Kept for this page view
 *  only: a typed price ages as the market moves, so it is never stored. */
export interface StoredOverride {
  price: number;
  basis: number;
  indexValue: number;
  indexSource: string;
  delayed: boolean;
  label: string;
  at: string | null;
  symbol: string;
}
