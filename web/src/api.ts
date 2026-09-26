export type Kind = "bullish" | "bearish";

export interface OrderBlock {
  kind: Kind;
  top: number;
  bottom: number;
  time: string;
  break_time: string;
  touches: number;
  mitigated_time: string | null;
  time_unix: number;
  break_time_unix: number;
}

export interface TimeframeScan {
  timeframe: string;
  candles: number;
  active_count: number;
  above: OrderBlock | null;
  below: OrderBlock | null;
  inside: OrderBlock[];
  active: OrderBlock[];
}

export type NearestOB = OrderBlock & { timeframe: string };

export interface Scan {
  time: string;
  price: number;
  timeframes: TimeframeScan[];
  nearest_above: NearestOB | null;
  nearest_below: NearestOB | null;
}

export interface ScanResponse {
  symbol: string;
  source: string;
  version: number;
  scanned_at: string | null;
  error: string | null;
  error_at: string | null;
  config: {
    timeframes: string[];
    lookback: number;
    swing_length: number;
    zone: string;
    mitigation: string;
  };
  scan: Scan | null;
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface CandlesResponse {
  timeframe: string;
  price: number;
  candles: Candle[];
  order_blocks: OrderBlock[];
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const fetchScan = () => getJson<ScanResponse>("/api/scan");

export const fetchCandles = (tf: string, limit: number) =>
  getJson<CandlesResponse>(`/api/candles?tf=${encodeURIComponent(tf)}&limit=${limit}`);
