/** A level from any detector: a zone, or a line when top === bottom. */
export interface Level {
  detector: string; // "ob" | "liquidity" | "idm" | future detectors
  kind: string; // "bullish"/"bearish" (ob, idm), "bsl"/"ssl" (liquidity)
  label: string;
  top: number;
  bottom: number;
  time: string;
  time_unix: number;
  confirmed_time: string;
  touches: number;
  ended_time: string | null;
  ended_time_unix: number | null;
  meta: {
    grab?: boolean; // liquidity / IDM: swept by a wick that closed back inside
    equal?: number; // liquidity: number of equal highs/lows in the pool
    priority?: "extreme" | "middle"; // order blocks
    entry?: number; // order blocks: limit price (proximal body edge)
    sl?: number; // order blocks: stop (far body edge, capped at max_sl)
    risk?: number;
    body?: number;
    sl_capped?: boolean;
    swing?: "LH" | "HH" | "HL" | "LL" | null; // extreme order blocks: the swing their leg started from
    [k: string]: unknown;
  };
}

export type NearestLevel = Level & { timeframe: string };

export interface LevelSet {
  active_count: number;
  active: Level[];
  above: Level | null;
  below: Level | null;
  inside: Level[];
  recent: Level[];
}

/** Latest break of structure on a timeframe. */
export interface Bias {
  direction: "bullish" | "bearish";
  event: "BOS" | "CHoCH";
  level: number;
  swing_time: string;
  swing_time_unix: number;
  break_time: string;
  break_time_unix: number;
  break_close_time: string; // when the break candle closed (break confirmed)
  bars_ago: number;
  streak: number;
}

export interface TimeframeScan {
  timeframe: string;
  candles: number;
  bias: Bias | null;
  detectors: Record<string, LevelSet>;
}

/** Label from the trader's bias: with it, against it, or no trading (neutral). null = no bias set. */
export type Risk = "on" | "off" | "no_trade" | null;

/** An order block limit setup: distance is from price to the entry. */
export type Setup = Level & { timeframe: string; distance: number; risk: Risk };

export interface Scan {
  time: string;
  price: number;
  detectors: string[];
  setups: { sell: Setup[]; buy: Setup[] };
  nearest: Record<string, { above: NearestLevel | null; below: NearestLevel | null }>;
  timeframes: TimeframeScan[];
}

export interface DetectorInfo {
  name: string;
  title: string;
}

export interface ScanResponse {
  symbol: string;
  source: string;
  clock: string; // the feed's clock, e.g. "UTC" or "NY+7"
  trade_bias: TradeBias | null;
  version: number;
  scanned_at: string | null;
  error: string | null;
  error_at: string | null;
  config: {
    timeframes: string[];
    lookback: number;
    detectors: DetectorInfo[];
    recent_bars: number;
    swing_length: number;
    zone: string;
    mitigation: string;
    eq_tolerance: number;
    idm_length: number;
    max_sl: number;
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

/** A confirmed swing point, labelled against the previous swing of its kind. */
export interface SwingPoint {
  kind: "high" | "low";
  time: string;
  time_unix: number;
  price: number;
  label: "HH" | "LH" | "HL" | "LL" | null;
}

export interface CandlesResponse {
  timeframe: string;
  price: number;
  candles: Candle[];
  levels: Level[];
  swings: SwingPoint[];
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const fetchScan = () => getJson<ScanResponse>("/api/scan");

export const fetchCandles = (tf: string, limit: number) =>
  getJson<CandlesResponse>(`/api/candles?tf=${encodeURIComponent(tf)}&limit=${limit}`);

/** Quarterly theory rows: weekdays, the four sessions of a day, and 90-minute quarters. */
export type QuarterRow = "week" | "session" | "q90";

export interface QuarterBlock {
  row: QuarterRow;
  label: string; // "Wed", "NY AM", "Q3"
  day: string; // trading day (starts 18:00 New York the evening before)
  start_unix: number;
  end_unix: number;
  open: number;
  high: number;
  low: number;
  close: number;
  change: number;
  live: boolean; // still running
}

export interface QuarterStat {
  label: string;
  count: number;
  green: number;
  avg_change: number | null;
}

export interface QuartersResponse {
  clock: string;
  rows: Record<QuarterRow, QuarterBlock[]>;
  stats: Record<QuarterRow, QuarterStat[]>;
}

export const fetchQuarters = () => getJson<QuartersResponse>("/api/quarters");

export interface SourceInfo {
  id: string;
  title: string;
  description: string;
  default_symbol: string;
  default_clock: string;
  available: boolean;
  unavailable_reason: string | null;
}

export interface DataSettings {
  source: string;
  symbol: string | null;
  csv_path: string | null;
  mt5_login: number | null;
  mt5_server: string | null;
  mt5_path: string | null;
  clock: string | null; // null = the source default
}

export interface SettingsResponse {
  editable: boolean;
  settings: DataSettings | null;
  sources: SourceInfo[];
  mt5_password_set: boolean;
  running: {
    source: string;
    symbol: string;
    version: number;
    scanned_at: string | null;
    error: string | null;
    bars_loaded: number;
    bars_needed: number;
    first_bar: string | null;
    last_bar: string | null;
  };
  storage: {
    backend: string;
    url: string;
    series: { source: string; symbol: string; bars: number; first: string | null; last: string | null }[];
  } | null;
}

export const fetchSettings = () => getJson<SettingsResponse>("/api/settings");

export async function saveSettings(settings: DataSettings): Promise<SettingsResponse> {
  const res = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<SettingsResponse>;
}

export interface AlertSettings {
  enabled: boolean;
  timeframes: string[];
  priorities: string[];
  neutral_alerts: boolean; // alert even when the bias is neutral (not trading)
}

export interface AlertRow {
  key: string;
  source: string;
  symbol: string;
  timeframe: string;
  kind: string;
  priority: string;
  entry: number;
  price: number;
  sent_at: string;
  status: "sent" | "failed";
  error: string | null;
}

export interface AlertsResponse {
  editable: boolean;
  token_set: boolean;
  chat_id_set: boolean;
  configured: boolean;
  settings: AlertSettings | null;
  last_error: string | null;
  recent: AlertRow[];
}

async function send<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const fetchAlerts = () => getJson<AlertsResponse>("/api/alerts");
export const saveAlerts = (s: AlertSettings) => send<AlertsResponse>("PUT", "/api/alerts", s);
export const testAlert = () => send<{ ok: boolean }>("POST", "/api/alerts/test");

/** The trader's directional bias, set by hand. */
export type BiasDirection = "bullish" | "bearish" | "neutral";
export type BiasExpiry = "day" | "week" | "none";

export interface TradeBias {
  direction: BiasDirection;
  note: string;
  expiry: BiasExpiry;
  set_at: string | null;
  expires_at: string | null;
  expired: boolean;
}

export const saveBias = (b: { direction: BiasDirection | null; note?: string; expiry?: BiasExpiry }) =>
  send<{ bias: TradeBias | null }>("PUT", "/api/bias", b);

export interface BriefSettings {
  provider: "anthropic" | "openai";
  model: string | null;
  prompt: string | null;
  urls: string[];
  max_chars_per_source: number;
}

export interface BriefSource {
  url: string;
  ok: boolean;
  error: string | null;
  chars: number;
  truncated: boolean;
}

export interface Brief {
  text: string;
  suggested_bias: BiasDirection | null;
  created_at: string;
  provider: string;
  model: string;
  sources: BriefSource[];
}

export interface BriefProvider {
  id: "anthropic" | "openai";
  title: string;
  default_model: string;
  env: string;
  installed: boolean;
  key_set: boolean;
}

export interface BriefResponse {
  editable: boolean;
  settings?: BriefSettings;
  default_prompt?: string;
  providers?: BriefProvider[];
  running?: boolean;
  error?: string | null;
  last?: Brief | null;
}

export const fetchBrief = () => getJson<BriefResponse>("/api/brief");
export const saveBrief = (s: BriefSettings) => send<BriefResponse>("PUT", "/api/brief", s);
export const generateBrief = () => send<BriefResponse>("POST", "/api/brief/generate");

/** An economic calendar event (times in UTC). */
export interface CalendarEvent {
  title: string;
  currency: string;
  impact: "High" | "Medium" | "Low" | string;
  time: string;
  forecast: string | null;
  previous: string | null;
  chart_time_unix: number; // the release time on the chart's axis (feed clock)
}

export interface CalendarSettings {
  enabled: boolean;
  url: string;
  currencies: string[];
  impacts: string[];
}

export interface CalendarResponse {
  editable: boolean;
  settings?: CalendarSettings;
  events: CalendarEvent[]; // upcoming (and just released), for the risk-time card
  week: CalendarEvent[]; // every matching event this week, for the chart
  fetched_at?: string | null;
  error?: string | null;
  loading?: boolean;
}

export const fetchCalendar = () => getJson<CalendarResponse>("/api/calendar");
export const saveCalendar = (s: CalendarSettings) => send<CalendarResponse>("PUT", "/api/calendar", s);
