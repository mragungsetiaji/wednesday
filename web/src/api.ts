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
/** Lot size for a setup's stop (Settings > Risk); `below_min` when even the minimum lot risks too much. */
export interface SetupSize {
  lots: number;
  risk: number;
  reward_2r: number;
  budget: number; // after the RISK OFF multiplier
  below_min: boolean;
  min_lot_risk: number;
}

export type Setup = Level & { timeframe: string; distance: number; risk: Risk; size?: SetupSize | null };

export interface Sizing {
  balance: number | null;
  currency: string;
  budget: number;
  per_point: number;
  min_lot: number;
  lot_step: number;
  max_lot: number | null;
  off_multiplier: number;
  source: "mt5" | "settings";
}

export interface Scan {
  time: string;
  price: number;
  detectors: string[];
  setups: { sell: Setup[]; buy: Setup[] };
  sizing?: Sizing | null; // null when Settings > Risk is off
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

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const fetchScan = () => getJson<ScanResponse>("/api/scan");

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

/** connecting -> connected; reconnecting while a lost connection is retried; failed once it gave up. */
export type FeedConn = "connecting" | "connected" | "reconnecting" | "failed";

export interface SettingsResponse {
  editable: boolean;
  settings: DataSettings | null;
  sources: SourceInfo[];
  /** Where the MT5 password for the saved account comes from; null = none. Never the password itself. */
  mt5_password: "saved" | "session" | "env" | null;
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

/** Save and restart the feed. A password is sent only when typed; it's kept by the OS, never returned. */
export const saveSettings = (settings: DataSettings, secret: { mt5_password?: string; forget_mt5_password?: boolean } = {}) =>
  send<SettingsResponse>("PUT", "/api/settings", { ...settings, ...secret });

/** Restart the feed with the saved settings, e.g. after it gave up connecting. */
export const reconnectFeed = () => send<SettingsResponse>("POST", "/api/settings/reconnect");

export interface Mt5Terminal {
  path: string;
  name: string;
  running: boolean;
}

export const fetchMt5Terminals = () => getJson<{ terminals: Mt5Terminal[] }>("/api/mt5/terminals");

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
  status: "sent" | "failed";
  error: string | null;
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
  secret: "anthropic_api_key" | "openai_api_key";
  installed: boolean;
  key_set: boolean;
  key_source: SecretSource;
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
export const saveBrief = (s: BriefSettings, secrets: SecretUpdate = {}) =>
  send<BriefResponse>("PUT", "/api/brief", { ...s, ...secrets });
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

// ---- Risk: position size per setup ----

export interface RiskSettings {
  enabled: boolean;
  mode: "percent" | "amount";
  value: number;
  use_mt5: boolean;
  balance: number | null;
  currency: string;
  contract_size: number;
  min_lot: number;
  lot_step: number;
  risk_off_multiplier: number;
}

export interface Mt5Spec {
  balance: number;
  currency: string;
  per_point: number;
  contract_size: number;
  min_lot: number;
  lot_step: number;
  max_lot: number | null;
}

export interface RiskResponse {
  editable: boolean;
  settings: RiskSettings | null;
  mt5: Mt5Spec | null; // what the terminal reports, when MT5 is the source
  sizer: Sizing | null;
}

export const fetchRisk = () => getJson<RiskResponse>("/api/risk");
export const saveRisk = (s: RiskSettings) => send<RiskResponse>("PUT", "/api/risk", s);

// ---- Lab: labels, training, models, reviews ----

export type LabShape = "body" | "high" | "low";
export interface LabTag {
  id: string; // ob_bull, ob_bear, bsl, ssl, idm_bull, idm_bear
  title: string;
  shape: LabShape;
}

export interface LabLabel {
  id: string;
  timeframe: string;
  tag: string;
  value: 0 | 1; // 1 = it is one, 0 = marked "not"
  start: number; // open time of the first candle (unix, feed clock)
  end: number;
  top: number | null;
  bottom: number | null;
  origin: "manual" | "detector" | "review";
  created_at: string;
}

export interface LabReviewed {
  id: string;
  timeframe: string;
  start: number;
  end: number;
  tags: string[];
}

export interface LabSuggestion {
  id: string;
  tag: string;
  time_unix: number;
  top: number;
  bottom: number;
  label: string;
  priority: "extreme" | "middle" | null;
}

export interface MlBlock {
  id: string;
  tag: string;
  title: string;
  timeframe: string;
  time_unix: number;
  available_unix: number; // when the model could call it (close of the confirming candles)
  prob: number;
  top: number;
  bottom: number;
  outcome_prob: number | null; // order blocks: chance the limit reaches the target before the stop
  model_id?: string;
  verdict?: "valid" | "invalid" | null;
  outcome?: string | null;
}

export interface LabWindow {
  timeframe: string;
  candles: Candle[];
  labels: LabLabel[];
  reviewed: LabReviewed[];
  suggestions: LabSuggestion[];
  predictions: MlBlock[];
  has_more: boolean;
  is_latest: boolean;
  history: { first_unix: number; last_unix: number; bars: number } | null;
}

export interface TagMetrics {
  title?: string;
  samples: number;
  positives: number;
  skipped?: string;
  trained?: boolean;
  threshold?: number; // probability cut tuned before the held-out part
  test_samples?: number;
  test_positives?: number;
  test_from?: string | null;
  precision?: number;
  recall?: number;
  auc?: number;
  avg_precision?: number;
  base_rate?: number;
  avg_r_all?: number;
  avg_r_picked?: number | null;
  picked?: number;
  from_labels?: number;
  from_detector?: number;
}

export interface ModelManifest {
  id: string;
  name: string;
  author: string;
  note: string;
  created_at: string;
  symbol: string;
  timeframes: string[];
  tags: Record<string, TagMetrics>;
  outcome: TagMetrics | null;
  params: { lookback: number; confirm: number; rr: number; horizon_hours: number; max_sl: number };
  data: { first: string; last: string; m1_bars: number; labels: number };
  sklearn: string;
  sha256: string;
}

export interface TrainParams {
  timeframes: string[];
  tags: string[];
  lookback: number;
  confirm: number;
  rr: number;
  horizon_hours: number;
  outcome_from_detector: boolean;
  test_fraction: number;
  name: string;
  author: string;
  note: string;
}

export interface LabStatus {
  editable: boolean;
  available: boolean;
  reason: string | null;
  symbol?: string;
  history?: { first_unix: number; last_unix: number; bars: number } | null;
  tags?: LabTag[];
  counts?: Record<string, { tags: Record<string, { yes: number; no: number }>; reviewed: number }>;
  training?: { running: boolean; stage: string | null; error: string | null; last: ModelManifest | null; started_at: string | null };
  models?: ModelManifest[];
  active?: string | null;
}

export const fetchLab = () => getJson<LabStatus>("/api/lab");
export const fetchLabWindow = (tf: string, end: number | null, limit: number, model: boolean) =>
  getJson<LabWindow>(`/api/lab/candles?tf=${encodeURIComponent(tf)}&limit=${limit}&model=${model}${end ? `&end=${end}` : ""}`);
export const addLabel = (l: { timeframe: string; tag: string; start: number; end: number; value: 0 | 1; top?: number | null; bottom?: number | null; origin?: string }) =>
  send<LabLabel>("POST", "/api/lab/labels", l);
export const deleteLabel = (id: string) => send<{ deleted: boolean }>("DELETE", `/api/lab/labels/${encodeURIComponent(id)}`);
export const addReviewed = (r: { timeframe: string; start: number; end: number; tags: string[] }) =>
  send<LabReviewed>("POST", "/api/lab/reviewed", r);
export const deleteReviewed = (id: string) => send<{ deleted: boolean }>("DELETE", `/api/lab/reviewed/${encodeURIComponent(id)}`);
export const startTraining = (p: TrainParams) => send<LabStatus>("POST", "/api/lab/train", p);
export const setActiveModel = (id: string | null) => send<LabStatus>("PUT", "/api/lab/active", { id });
export const deleteModel = (id: string) => send<LabStatus>("DELETE", `/api/lab/models/${encodeURIComponent(id)}`);
export const modelFileUrl = (id: string) => `/api/lab/models/${encodeURIComponent(id)}/file`;
export const confirmImport = (token: string) => send<LabStatus & { imported: ModelManifest }>("POST", `/api/lab/models/import/${token}`);
export async function stageImport(file: File): Promise<{ token: string; manifest: ModelManifest; exists: boolean }> {
  const res = await fetch("/api/lab/models/import", { method: "POST", body: file });
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
// ---- Lab > Data: more M1 history ----

export interface BackfillState {
  running: boolean;
  start: string | null;
  reached: string | null; // oldest date pulled so far
  added: number;
  error: string | null;
  note: string | null;
  finished_at: string | null;
}

export interface LabData {
  source: string;
  symbol: string;
  clock: string; // the feed's clock; imported bars are moved to it
  can_import: boolean;
  can_backfill: boolean;
  stored: { bars: number; first_unix: number | null; last_unix: number | null };
  days: [number, number][]; // [day (unix midnight), M1 bars]
  history_bars: number;
  history_bars_max: number;
  backfill: BackfillState;
}

export interface BarsPreview {
  token: string;
  format: "mt5" | "dukascopy" | "histdata" | "csv";
  suggested_clock: string | null;
  bars: number;
  first: string;
  last: string;
  sample: { time: string; open: number; high: number; low: number; close: number; volume: number }[];
}

export interface BarsImported {
  read: number;
  added: number;
  first: string;
  last: string;
  stored: number;
}

export const fetchLabData = () => getJson<LabData>("/api/lab/data");
export const saveLabHistory = (history_bars: number) => send<LabData>("PUT", "/api/lab/data", { history_bars });
export const startBackfill = (start: string) => send<LabData>("POST", "/api/lab/data/backfill", { start });
export const stopBackfill = () => send<LabData>("DELETE", "/api/lab/data/backfill");
export const confirmBars = (token: string, clock: string) =>
  send<LabData & { imported: BarsImported }>("POST", `/api/lab/data/import/${token}`, { clock });
export async function stageBars(file: File): Promise<BarsPreview> {
  const res = await fetch("/api/lab/data/import", { method: "POST", body: file });
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

export const fetchPredictions = (tf: string, limit: number, threshold: number) =>
  getJson<{ model: { id: string; name: string } | null; blocks: MlBlock[] }>(
    `/api/lab/predictions?tf=${encodeURIComponent(tf)}&limit=${limit}&threshold=${threshold}`);
export const reviewBlock = (b: MlBlock, verdict: "valid" | "invalid") =>
  send<{ verdict: string; outcome: string | null }>("POST", "/api/lab/reviews", { ...b, verdict });

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
export const activateLicence = (key: string) => send<LicenceStatus>("PUT", "/api/licence", { key });
export const removeLicence = () => send<LicenceStatus>("DELETE", "/api/licence");

export const fetchPlugins = () => getJson<PluginsResponse>("/api/plugins");

// ---- Journal (trading accounts imported from MT5) ----

export interface Journal {
  id: string;
  name: string;
  login: string | null; // MT5 account number
  server: string | null;
  company: string | null;
  currency: string | null;
  source: "mt5" | "report" | null; // how it was last filled
  account: { balance: number | null; equity: number | null; at: string } | null; // what the terminal said at the last sync
  time_offset: number | null; // hours from the deal clock to the price clock; null = detected
  created_at: string;
  synced_at: string | null;
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
    balance_reported?: number;
    balance_matches?: boolean;
  };
  series: { growth: Point[]; balance: Point[]; equity: Point[]; drawdown: Point[] };
  monthly: { month: string; gain: number | null; profit: number; pips: number; trades: number }[]; // "2026-03"
  daily: { day: string; profit: number; pips: number; trades: number; won: number }[]; // "2026-03-07", by close
  trades: JournalTrade[];
  cash: { id: string; time: number; kind: string; amount: number; comment: string | null }[];
}

export const fetchJournals = () => getJson<JournalsResponse>("/api/journals");
export const fetchJournal = (id: string) => getJson<JournalStats>(`/api/journals/${encodeURIComponent(id)}`);
export const createJournal = (name: string) => send<Journal>("POST", "/api/journals", { name });
export const updateJournal = (id: string, patch: { name?: string; time_offset?: number | null }) =>
  send<Journal>("PATCH", `/api/journals/${encodeURIComponent(id)}`, patch);
export const deleteJournal = (id: string) => send<{ deleted: boolean }>("DELETE", `/api/journals/${encodeURIComponent(id)}`);
export const syncJournal = (id: string) =>
  send<{ trades: number; cash: number; journal: Journal }>("POST", `/api/journals/${encodeURIComponent(id)}/sync`);
export const saveTradeNote = (id: string, tradeId: string, note: string, tags: string[]) =>
  send<{ note: string; tags: string[] }>("PUT", `/api/journals/${encodeURIComponent(id)}/trades/${encodeURIComponent(tradeId)}/note`, { note, tags });
export const journalCsvUrl = (id: string) => `/api/journals/${encodeURIComponent(id)}/trades.csv`;
export async function importReport(id: string, file: File): Promise<{ trades: number; cash: number; journal: Journal }> {
  const res = await fetch(`/api/journals/${encodeURIComponent(id)}/import`, { method: "POST", body: file });
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
