import type { ShapeKind } from "./shapes";

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
  conn: FeedConn;
  tick_seconds: number; // live price interval between scans; 0 = none
  pip?: number; // one pip in price: the symbol's usual size
  clock_offset?: number; // seconds the feed clock is ahead of UTC now
  poll?: boolean; // false: started with --no-poll, the scan never updates
  app_version: string;
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

/** The market at a replay clock: candles and scan built only from bars closed by `at` (unix, feed clock). */
export interface ReplayResponse extends CandlesResponse {
  at: number;
  bar_time: number; // the last closed minute
  scan: Scan;
}
export const fetchReplay = (at: number, tf: string, limit: number) =>
  getJson<ReplayResponse>(`/api/replay?at=${at}&tf=${encodeURIComponent(tf)}&limit=${limit}`);
export const fetchReplayRange = () => getJson<{ first: number; last: number }>("/api/replay/range");
/** A window of a workspace: the dashboard ("#") or a popped-out chart, where it was. */
export interface WorkspaceWindow { route: string; x: number; y: number; width: number; height: number; maximized?: boolean }
export interface Workspace { name: string; layout: Record<string, unknown>; windows: WorkspaceWindow[]; saved_at: string }
export const fetchWorkspaces = () =>
  getJson<{ workspaces: { name: string; saved_at: string; windows: number }[] }>("/api/workspaces");
export const fetchWorkspace = (name: string) => getJson<Workspace>(`/api/workspaces/${encodeURIComponent(name)}`);
export const saveWorkspace = (name: string, layout: Record<string, unknown>, windows: WorkspaceWindow[]) =>
  send<Workspace>("PUT", `/api/workspaces/${encodeURIComponent(name)}`, { layout, windows });
export const deleteWorkspace = (name: string) => send<{ deleted: boolean }>("DELETE", `/api/workspaces/${encodeURIComponent(name)}`);

/** Sent when an API call answers 401: the session ended, so the login screen comes back. */
export const LOGGED_OUT_EVENT = "wednesday:logged-out";

async function apiFetch(url: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(url, init);
  if (res.status === 401 && !url.startsWith("/api/auth/")) window.dispatchEvent(new Event(LOGGED_OUT_EVENT));
  return res;
}

// The login: on when the dashboard is served on a network host (or with --login).
export interface AuthUser {
  username: string;
  name: string | null;
  email: string | null;
  role: string;
}
export interface AuthStatus {
  login: boolean; // the server asks for a login
  logged_in: boolean;
  setup: boolean; // no account yet: create one with the setup code from the server's log
  user: AuthUser | null;
}
export interface Device {
  id: string;
  device: string | null;
  created_at: string;
  last_seen_at: string | null;
  current: boolean;
}
export const fetchAuthStatus = () => getJson<AuthStatus>("/api/auth/status");
export const logIn = (username: string, password: string) =>
  send<AuthStatus>("POST", "/api/auth/login", { username, password });
export const setUp = (code: string, username: string, password: string) =>
  send<AuthStatus>("POST", "/api/auth/setup", { code, username, password });
export const logOut = () => send<AuthStatus>("POST", "/api/auth/logout");
export const fetchDevices = () => getJson<{ sessions: Device[] }>("/api/auth/sessions");
export const endDevice = (id: string) => send<{ sessions: Device[] }>("DELETE", `/api/auth/sessions/${encodeURIComponent(id)}`);
export const changePassword = (current: string, next: string) =>
  send<{ sessions: Device[] }>("PUT", "/api/auth/password", { current, new: next });

async function getJson<T>(url: string): Promise<T> {
  const res = await apiFetch(url, { cache: "no-store" });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const fetchScan = () => getJson<ScanResponse>("/api/scan");

/** The live price between scans and the forming M1 candle (times are unix seconds on the feed clock). */
export interface TickResponse {
  version: number; // the scan version: a new one means /api/scan has news
  tick: number;
  price: number | null;
  time: number | null;
  bar: { time: number; open: number; high: number; low: number; close: number } | null;
}
export const fetchTick = () => getJson<TickResponse>("/api/tick");

/** A full-screen pane's own symbol (e.g. FX:XAGUSD) from TradingView: candles only, never scanned. */
export interface MarketCandles extends CandlesResponse {
  symbol: string;
  precision: number; // decimals the symbol is quoted with
  has_more: boolean;
}
const marketQuery = (symbol: string) => `symbol=${encodeURIComponent(symbol)}`;
export const fetchMarketCandles = (symbol: string, tf: string, limit: number, before?: number) =>
  getJson<MarketCandles>(`/api/markets/candles?${marketQuery(symbol)}&tf=${encodeURIComponent(tf)}&limit=${limit}` +
    (before === undefined ? "" : `&before=${before}`));
export const fetchMarketTick = (symbol: string) =>
  getJson<{ symbol: string; price: number | null; time: number | null }>(`/api/markets/tick?${marketQuery(symbol)}`);

export const fetchCandles = (tf: string, limit: number) =>
  getJson<CandlesResponse>(`/api/candles?tf=${encodeURIComponent(tf)}&limit=${limit}`);

/** Candles opening before `before` (unix), for scrolling back; `has_more` is false at the start of the history. */
export interface OlderCandles {
  timeframe: string;
  candles: Candle[];
  has_more: boolean;
}

export const fetchOlderCandles = (tf: string, before: number, limit: number) =>
  getJson<OlderCandles>(`/api/candles?tf=${encodeURIComponent(tf)}&limit=${limit}&before=${before}`);

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

export type RefLevelKind =
  | "asia_high" | "asia_low" | "pdh" | "pdl" | "pwh" | "pwl" | "pmh" | "pml"
  | "day_open" | "week_open" | "midnight_open" | "quarter_open";

/** A reference level drawn from start to end (feed-clock unix), faded after it was swept. */
export interface RefLevel {
  kind: RefLevelKind;
  label: string;
  price: number;
  start_unix: number;
  end_unix: number;
  swept_unix: number | null;
  current: boolean; // the running period's: labelled at the right edge
}

export interface Killzone {
  name: "Asia" | "London" | "NY AM" | "NY PM";
  day: string;
  start_unix: number;
  end_unix: number;
}

/** Killzones and reference levels for the price chart (/api/sessions). */
export interface SessionsResponse {
  clock: string;
  killzones: Killzone[];
  lines: RefLevel[];
}

export const fetchSessions = () => getJson<SessionsResponse>("/api/sessions");

export type DistPeriod = "day" | "week" | "session" | "q90";
export type DistMeasure = "change" | "range_pct" | "range_atr";
export type DistLookback = "1y" | "5y" | "all";

/** How unusual the current period's move is against past closed ones (/api/distribution). */
export interface Distribution {
  period: DistPeriod;
  measure: DistMeasure;
  unit: string; // "%" or "×ATR"
  lookback: DistLookback;
  samples: number;
  min_samples: number; // under this, no percentile or z-score
  current: { day: string; label: string; value: number | null; forming: boolean; start_unix: number } | null;
  stats: { mean: number; median: number; std: number; min: number; max: number } | null;
  percentile: { below: number; above: number; tail: number; side: "low" | "high" } | null;
  z: number | null;
  histogram: { edges: number[]; counts: number[] };
  like_this: { day: string; label: string; value: number; next: number | null }[];
  filters: { weekday: string | null; session: string | null };
  coverage: { from: string | null; to: string | null } | null;
}

export const fetchDistribution = (q: { period: DistPeriod; measure: DistMeasure; lookback: DistLookback; weekday: boolean; session: boolean }) =>
  getJson<Distribution>(`/api/distribution?${new URLSearchParams({
    period: q.period, measure: q.measure, lookback: q.lookback, weekday: String(q.weekday), session: String(q.session),
  })}`);

/** A New York wall time ("2026-09-24T09:30") as the chart's feed-clock unix seconds. */
export const nyToFeed = (ny: string) =>
  getJson<{ ny: string; feed: string; feed_unix: number }>(`/api/clock/feed?ny=${encodeURIComponent(ny)}`);

export interface SourceInfo {
  id: string;
  title: string;
  description: string;
  default_symbol: string;
  default_clock: string;
  default_tick: number; // live price interval in seconds; 0 = none
  min_tick: number;
  available: boolean;
  unavailable_reason: string | null;
}

export interface DataSettings {
  source: string;
  symbol: string | null;
  clock: string | null; // null = the source default
  tick_seconds: number | null; // null = the source default, 0 = off
}

/** connecting -> connected; reconnecting while a lost connection is retried; failed once it gave up. */
export type FeedConn = "connecting" | "connected" | "reconnecting" | "failed";

export interface SettingsResponse {
  editable: boolean;
  settings: DataSettings | null;
  sources: SourceInfo[];
  running: {
    source: string;
    symbol: string;
    version: number;
    scanned_at: string | null;
    error: string | null;
    conn: FeedConn;
    attempt: number;
    max_attempts: number | null; // null = the feed keeps retrying

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

/** Save and restart the feed. */
export const saveSettings = (settings: DataSettings) => send<SettingsResponse>("PUT", "/api/settings", settings);

/** Restart the feed with the saved settings, e.g. after it gave up connecting. */
export const reconnectFeed = () => send<SettingsResponse>("POST", "/api/settings/reconnect");

export interface AlertSettings {
  enabled: boolean;
  timeframes: string[];
  priorities: string[];
  neutral_alerts: boolean; // alert even when the bias is neutral (not trading)
  chat_id: string | null; // null = TELEGRAM_CHAT_ID from .env
}

/** Where a secret comes from: the credential store, memory for this run, or .env. Never the value. */
export type SecretSource = "saved" | "session" | "env" | null;

/** Write-only secret fields sent with a save: a new value, or forget the stored one. */
export type SecretUpdate = Record<string, string | boolean | undefined>;

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
  status: "sent" | "failed" | "logged"; // logged: a drawing alert that fired without Telegram set up
  error: string | null;
  summary?: string | null; // drawing alerts: what happened
}

export interface AlertsResponse {
  editable: boolean;
  token_set: boolean;
  token_source: SecretSource;
  chat_id_set: boolean;
  configured: boolean;
  settings: AlertSettings | null;
  last_error: string | null;
  recent: AlertRow[];
}

async function send<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await apiFetch(url, {
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

// ---- The symbol menu's markets (Settings > Markets) ----

export interface Market {
  symbol: string; // TradingView's name, e.g. OANDA:XAUUSD
  label: string; // what the menu shows, e.g. XAU
  name: string | null;
  venue: string | null; // FXCM, OANDA: who quotes it
  position: number;
  keep_live: boolean; // its candles are stored even while another market is scanned
}
export interface MarketsResponse {
  markets: Market[];
}
export const fetchMarkets = () => getJson<MarketsResponse>("/api/markets");
export const addMarket = (m: { symbol: string; label?: string }) => send<MarketsResponse>("POST", "/api/markets", m);
export const saveMarkets = (markets: Market[]) => send<MarketsResponse>("PUT", "/api/markets", { markets });
export const deleteMarket = (symbol: string) =>
  send<MarketsResponse>("DELETE", `/api/markets?symbol=${encodeURIComponent(symbol)}`);

// ---- Chart drawings ----

/** The first tools, which the editor draws itself, then the rest (shapes.ts). */
export type DrawingKind = "trendline" | "hline" | "rect" | "long" | "short" | "path" | "text" | ShapeKind;
export type DrawingDash = "solid" | "dashed" | "dotted";

/** Anchored to (time, price): t is unix seconds on the feed clock, like the candles. */
export interface DrawingPoint {
  t: number;
  p: number;
}

export interface Drawing {
  id: string;
  kind: DrawingKind;
  points: DrawingPoint[]; // trendline and rect: 2, hline and text: 1, path: 2+; long / short: start and end, at the entry
  // color null: the theme's drawing colour; size: a text's font size
  style: { color: string | null; width: number; dash?: DrawingDash; size?: number };
  props: { stop?: number; target?: number; text?: string; ax?: number; ay?: number } | null; // long / short: stop and target; text: its text
  timeframes: string[] | null; // null: every timeframe
  locked: boolean;
  hidden: boolean;
  updated_at?: string;
}

export type AlertCondition = "cross_up" | "cross_down" | "cross" | "enter" | "exit";

/** A price alert on a line, trendline or rectangle, checked by the server on closed M1 bars. */
export interface DrawingAlert {
  drawing_id: string;
  condition: AlertCondition;
  mode: "once" | "every"; // once: fires then turns off until re-armed
  note: string | null;
  expires_at: string | null; // ISO UTC
  intrabar: boolean; // also on the live tick
  armed: boolean;
  armed_bar: number | null;
  fired_at: string | null;
  fired_price: number | null;
  fires: number;
  created_at: string;
}
export type DrawingAlertInput = Pick<DrawingAlert, "condition" | "mode" | "note" | "expires_at" | "intrabar">;

/** `?symbol=` for a full-screen pane on another market; omitted, the scanned one. */
const drawingQuery = (symbol?: string) => (symbol ? `?symbol=${encodeURIComponent(symbol)}` : "");
export const fetchDrawings = (symbol?: string) =>
  getJson<{ source: string; symbol: string; drawings: Drawing[]; alerts: DrawingAlert[] }>(`/api/drawings${drawingQuery(symbol)}`);
export const fetchDrawingAlerts = () => getJson<{ alerts: DrawingAlert[] }>("/api/drawings/alerts");
export const putDrawingAlert = (id: string, a: DrawingAlertInput) => send<DrawingAlert>("PUT", `/api/drawings/${id}/alert`, a);
export const deleteDrawingAlert = (id: string) => send<{ deleted: string }>("DELETE", `/api/drawings/${id}/alert`);
/** A saved drawing style: colour, width, line style, text size and label, per drawing family. */
export type DrawingFamily = "line" | "rect" | "text";
export interface DrawingTemplate {
  family: DrawingFamily;
  name: string;
  style: Drawing["style"];
  text: string | null;
  saved_at: string;
}
export const fetchDrawingTemplates = () => getJson<{ templates: DrawingTemplate[] }>("/api/drawing-templates");
export const putDrawingTemplate = (family: DrawingFamily, name: string, t: { style: Drawing["style"]; text: string | null }) =>
  send<DrawingTemplate>("PUT", `/api/drawing-templates/${family}/${encodeURIComponent(name)}`, t);
export const deleteDrawingTemplate = (family: DrawingFamily, name: string) =>
  send<{ deleted: string }>("DELETE", `/api/drawing-templates/${family}/${encodeURIComponent(name)}`);
export const putDrawing = (d: Drawing, symbol?: string) => send<Drawing>("PUT", `/api/drawings/${d.id}${drawingQuery(symbol)}`, d);
export const deleteDrawing = (id: string, symbol?: string) =>
  send<{ deleted: string }>("DELETE", `/api/drawings/${id}${drawingQuery(symbol)}`);

export interface TermsResponse {
  version: string;
  accepted: boolean;
  accepted_at: string | null;
  text: string; // DISCLAIMER.md
}

export const fetchTerms = () => getJson<TermsResponse>("/api/terms");
export const acceptTerms = () => send<TermsResponse>("POST", "/api/terms/accept");

export const fetchAlerts = () => getJson<AlertsResponse>("/api/alerts");
export const saveAlerts = (s: AlertSettings, secrets: SecretUpdate = {}) =>
  send<AlertsResponse>("PUT", "/api/alerts", { ...s, ...secrets });

export interface TelegramChat {
  id: number;
  type: string;
  name: string;
}

/** Chats that recently messaged the bot, to pick the chat id. */
export const fetchTelegramChats = () => getJson<{ chats: TelegramChat[] }>("/api/alerts/chats");
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

/** An economic calendar event (times in UTC). */
export interface CalendarEvent {
  title: string;
  currency: string;
  impact: "High" | "Medium" | "Low" | string;
  time: string;
  forecast: string | null;
  previous: string | null;
  chart_time_unix: number; // the release time on the chart's axis (feed clock)
  reaction?: NewsReaction | null; // how gold moved after past releases of this event
}

type ByWindow = { "5": number | null; "15": number | null; "60": number | null };

/** Gold after past releases of one event type, from the stored calendar and M1 bars (news_stats.py). */
export interface NewsReaction {
  title: string;
  currency: string;
  count: number; // releases with M1 bars around them (the most recent 24 at most)
  stored: number; // releases of it in the calendar history
  last: number | null; // unix seconds of the latest one used
  move: ByWindow; // median size of the move after 5/15/60 minutes, in price
  move_atr: ByWindow; // the same in ATRs (15M ATR(14) before the release)
  range15: number | null; // median high-low of the first 15 minutes
  range15_atr: number | null;
  reversed: number; // releases whose first (5m) move had reversed by 60m
  up15: number; // releases with the 15m move up
  surprise: Record<"above" | "below", { count: number; move15: number | null }>; // actual vs forecast
  summary: string; // "CPI m/m: median 15m range 9.40 (last 12)"
}

export interface CalendarHistory {
  stored: number;
  first: string | null;
  last: string | null;
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
  history?: CalendarHistory;
}

export const fetchCalendar = () => getJson<CalendarResponse>("/api/calendar");
export const fetchNewsReactions = () =>
  getJson<{ available: boolean; types: NewsReaction[]; history: CalendarHistory | null }>("/api/calendar/reactions");

/** Add a past calendar (CSV) to the history; ``zone`` is how times without an offset are read. */
export async function importCalendar(file: File, zone: string): Promise<{ imported: number; history: CalendarHistory }> {
  const res = await apiFetch(`/api/calendar/import?zone=${encodeURIComponent(zone)}`, { method: "POST", body: file });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}
export const saveCalendar = (s: CalendarSettings) => send<CalendarResponse>("PUT", "/api/calendar", s);

// ---- Plugins (e.g. the paid wednesday-ee package) ----

export interface PluginInfo {
  name: string;
  version: string | null;
  api: number | null;
  features: string[];
  loaded: boolean;
  error: string | null;
}

export interface PluginsResponse {
  api: number;
  plugins: PluginInfo[];
  features: string[]; // feature ids provided by loaded plugins; screens unlock from these
  catalog: FeatureInfo[]; // every known paid feature, on or not
}

export interface FeatureInfo {
  id: string;
  title: string;
  description: string;
  issue: number | null;
  enabled: boolean;
}

/** Licence state from the plugin that handles licences; available=false when none does. */
export interface LicenceStatus {
  available: boolean;
  valid?: boolean;
  plan?: string | null;
  licensee?: string | null;
  licence_id?: string | null;
  issued_at?: string | null;
  expires_at?: string | null;
  features?: string[];
  error?: string | null; // why a stored key isn't valid (expired, tampered, no signing key configured)
  key_hint?: string | null; // last characters of the stored key
}

export const fetchLicence = () => getJson<LicenceStatus>("/api/licence");
/** Sent on window when the licence changes, so the status bar's plan follows at once. */
export const LICENCE_EVENT = "wednesday:licence";
export const activateLicence = (key: string) => send<LicenceStatus>("PUT", "/api/licence", { key });
export const removeLicence = () => send<LicenceStatus>("DELETE", "/api/licence");

export const fetchPlugins = () => getJson<PluginsResponse>("/api/plugins");

// ---- Journal (trading accounts, from MT5 history reports) ----

export interface Journal {
  id: string;
  name: string;
  login: string | null; // MT5 account number
  server: string | null;
  company: string | null;
  currency: string | null;
  time_offset: number | null; // hours from the deal clock to the price clock; null = detected
  created_at: string;
  synced_at: string | null; // when the last MT5 report was imported
  sample: boolean; // the made-up sample portfolio: free, can't be imported into
  show_weekends: boolean; // Saturday and Sunday columns in the calendar
}

export interface JournalsResponse {
  available: boolean;
  journals: Journal[];
  multi: boolean; // more than one journal allowed (paid)
}

export interface JournalTrade {
  id: string;
  position: string;
  symbol: string;
  side: "buy" | "sell";
  volume: number;
  open_time: number; // unix, broker server clock
  open_price: number;
  close_time: number | null; // null while open
  close_price: number | null;
  profit: number;
  commission: number;
  swap: number;
  net: number;
  pips: number | null; // 0.1 on gold; null while open
  mae: number | null; // worst floating result, from M1 bars
  mfe: number | null;
  floating: number | null; // open trades
  verified: boolean; // bars for its whole life and its prices match them
  price_ok: boolean | null; // null: no bar to check against
  note: string;
  tags: string[];
  images: string[]; // chart snapshot ids: /api/snapshots/<id>.png
}

export type Point = [number, number];

export interface JournalStats {
  journal: Journal;
  summary: {
    gain: number; // percent, time-weighted
    abs_gain: number | null;
    daily: number | null;
    monthly: number | null;
    drawdown: number; // percent, from the rebuilt equity
    drawdown_at: number | null;
    balance: number;
    equity: number;
    floating: number;
    profit: number;
    deposits: number;
    withdrawals: number;
    credit: number;
    other: number;
    max_floating_loss: number | null;
    start: number | null;
    last: number | null;
  };
  trading: {
    trades: number;
    open: number;
    won: number;
    lost: number;
    win_rate: number | null;
    profit_factor: number | null;
    avg_win: number | null;
    avg_loss: number | null;
    best: number | null;
    worst: number | null;
    lots: number;
    commission: number;
    swap: number;
    avg_hold: number | null; // seconds
  };
  verification: {
    basis: "ohlc" | "closed";
    trades: number;
    verified: number;
    coverage: number | null; // percent of trade time rebuilt from bars
    prices_checked: number;
    prices_ok: number;
    offset_hours: number | null;
    offset_detected: boolean;
    no_prices: string[];
    mismatched: string[];
    prices_from: Record<string, string>;
  };
  series: { growth: Point[]; balance: Point[]; equity: Point[]; drawdown: Point[] };
  monthly: { month: string; gain: number | null; profit: number; pips: number; trades: number }[]; // "2026-03"
  daily: { day: string; profit: number; pips: number; trades: number; won: number }[]; // "2026-03-07", by close
  trades: JournalTrade[];
  cash: { id: string; time: number; kind: string; amount: number; comment: string | null }[];
}

export const fetchJournals = () => getJson<JournalsResponse>("/api/journals");
export const fetchJournal = (id: string) => getJson<JournalStats>(`/api/journals/${encodeURIComponent(id)}`);
export const createJournal = (name: string, login: string | null) =>
  send<Journal>("POST", "/api/journals", { name, login });
export type JournalPatch = { name?: string; time_offset?: number | null; show_weekends?: boolean };
export const updateJournal = (id: string, patch: JournalPatch) =>
  send<Journal>("PATCH", `/api/journals/${encodeURIComponent(id)}`, patch);
/** Bring the sample portfolio back after it was deleted. */
export const deleteJournal = (id: string) => send<{ deleted: boolean }>("DELETE", `/api/journals/${encodeURIComponent(id)}`);
export const saveTradeNote = (id: string, tradeId: string, note: string, tags: string[]) =>
  send<{ note: string; tags: string[] }>("PUT", `/api/journals/${encodeURIComponent(id)}/trades/${encodeURIComponent(tradeId)}/note`, { note, tags });
export const snapshotUrl = (id: string) => `/api/snapshots/${id}.png`;
export async function uploadSnapshot(png: Blob): Promise<{ id: string; url: string }> {
  const res = await apiFetch("/api/snapshots", { method: "POST", body: png, headers: { "Content-Type": "image/png" } });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}
export const attachSnapshot = (journalId: string, tradeId: string, snapshotId: string) =>
  send<{ trade_id: string; images: string[] }>("POST",
    `/api/journals/${encodeURIComponent(journalId)}/trades/${encodeURIComponent(tradeId)}/images`, { snapshot_id: snapshotId });
export const detachSnapshot = (journalId: string, tradeId: string, snapshotId: string) =>
  send<{ trade_id: string; images: string[] }>("DELETE",
    `/api/journals/${encodeURIComponent(journalId)}/trades/${encodeURIComponent(tradeId)}/images/${snapshotId}`);
/** The market as it stood when an entry was written, frozen with it (journal/market.py). */
export interface FrozenMarket {
  at: string;
  symbol: string;
  source: string;
  bar_time: string | null;
  price: number | null;
  bias: { direction: string; note?: string; expires_at?: string | null } | null;
  setups: Record<"sell" | "buy", { timeframe: string; entry: number | null; sl: number | null; risk: string | null; priority: string | null; swing: string | null }[]>;
  structure: { timeframe: string; direction: string; event: string; level: number }[];
  news: { time: string; currency: string; title: string; impact: string }[];
}
export interface JournalEntry {
  id: string;
  kind: "note" | "review" | "setup";
  created_at: string;
  updated_at: string | null;
  text: string;
  tags: string[];
  mood: number | null;
  setup: { tag: string; timeframe: string; side: "sell" | "buy"; entry: number; sl: number | null } | null;
  market: FrozenMarket | null;
}
const entriesUrl = (id: string) => `/api/journals/${encodeURIComponent(id)}/entries`;
export const fetchEntries = (id: string) => getJson<{ entries: JournalEntry[] }>(entriesUrl(id));
export const addEntry = (id: string, entry: Partial<Pick<JournalEntry, "kind" | "text" | "tags" | "mood" | "setup">>) =>
  send<JournalEntry>("POST", entriesUrl(id), entry);
export const updateEntry = (id: string, entryId: string, patch: Partial<Pick<JournalEntry, "text" | "tags" | "mood">>) =>
  send<JournalEntry>("PATCH", `${entriesUrl(id)}/${encodeURIComponent(entryId)}`, patch);
export const deleteEntry = (id: string, entryId: string) =>
  send<{ deleted: boolean }>("DELETE", `${entriesUrl(id)}/${encodeURIComponent(entryId)}`);

export const journalCsvUrl = (id: string) => `/api/journals/${encodeURIComponent(id)}/trades.csv`;
export async function importReport(id: string, file: File): Promise<{ trades: number; cash: number; journal: Journal }> {
  const res = await apiFetch(`/api/journals/${encodeURIComponent(id)}/import`, { method: "POST", body: file });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json();
}


