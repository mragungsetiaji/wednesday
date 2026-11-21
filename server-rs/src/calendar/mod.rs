//! The economic calendar (`news.py`, `news_stats.py`), #96 step 9f: the settings (which news the
//! risk-time card and the chart show), ForexFactory's weekly feed fetched at most hourly and kept in
//! the store, the history of every fetched or imported week (`calendar_events`), and how gold moved
//! after past releases. Messages and answers are Python's.
//!
//! The calendar describes scheduled news and past moves; it is not advice and nothing here trades.

pub mod events;
pub mod import;
pub mod reactions;

use std::sync::{Arc, Mutex, Weak};
use std::time::Duration as StdDuration;

use chrono::{DateTime, Duration, Utc};
use serde_json::{json, Map, Value};

use crate::engine::{Answer, Refusal, Runtime};
use crate::scan::{unix, Bar, Clock, Time};
use crate::settings_routes::Refused;
use crate::store::{Arg, Store};
use events::{py_strip, truthy};

pub use events::{event_key, parse_events, store_rows, CalRow};
pub use import::{parse_csv, ImportError};
pub use reactions::NewsReactions;

pub const CALENDAR_KEY: &str = "calendar";
pub const DEFAULT_URL: &str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";
pub const IMPACTS: [&str; 3] = ["High", "Medium", "Low"];

pub use wednesday_base::py::py_repr;

// ---- the settings (CalendarSettings) ----

/// Which news the risk-time card and the chart show (`news.CalendarSettings`).
#[derive(Clone, Debug, PartialEq)]
pub struct CalendarSettings {
    pub enabled: bool,
    pub url: String,
    pub currencies: Vec<String>,
    pub impacts: Vec<String>,
}

impl Default for CalendarSettings {
    fn default() -> Self {
        CalendarSettings { enabled: true, url: DEFAULT_URL.into(), currencies: vec!["USD".into()],
                           impacts: vec!["High".into()] }
    }
}

/// A list of strings, or None for anything else.
fn strings(v: &Value) -> Option<Vec<&str>> {
    v.as_array()?.iter().map(Value::as_str).collect()
}

impl CalendarSettings {
    /// `from_dict`: settings from a saved or sent dict; Err for a field of the wrong type.
    pub fn from_dict(body: &Map<String, Value>) -> Result<CalendarSettings, String> {
        let url = match body.get("url") {
            None | Some(Value::Null) => DEFAULT_URL.to_string(),
            Some(Value::String(s)) if s.is_empty() => DEFAULT_URL.to_string(),
            Some(Value::String(s)) => py_strip(s).to_string(),
            Some(_) => return Err("The calendar URL must be text".into()),
        };
        let currencies = match body.get("currencies") {
            None => vec!["USD".to_string()],
            Some(v) => match strings(v) {
                Some(c) => c.iter().map(|c| py_strip(c)).filter(|c| !c.is_empty()).map(str::to_uppercase).collect(),
                None => return Err("Currencies must be a list of currency codes, like USD".into()),
            },
        };
        let impacts: Vec<String> = match body.get("impacts") {
            None => vec!["High".to_string()],
            Some(v) => match strings(v) {
                Some(i) => i.into_iter().map(String::from).collect(),
                None => return Err("Impacts must be a list of High, Medium or Low".into()),
            },
        };
        let enabled = body.get("enabled").map_or(true, truthy);
        Ok(CalendarSettings { enabled, url, currencies, impacts })
    }

    pub fn validate(&self) -> Vec<String> {
        let mut errors = Vec::new();
        if !(self.url.starts_with("http://") || self.url.starts_with("https://")) {
            errors.push("The calendar URL must start with http:// or https://".to_string());
        }
        for i in self.impacts.iter().filter(|i| !IMPACTS.contains(&i.as_str())) {
            errors.push(format!("Unknown impact {}", py_repr(i)));
        }
        if self.currencies.is_empty() {
            errors.push("Pick at least one currency".to_string());
        }
        errors
    }

    pub fn to_json(&self) -> Value {
        json!({"enabled": self.enabled, "url": self.url, "currencies": self.currencies, "impacts": self.impacts})
    }

    fn shows(&self, e: &Value) -> bool {
        let has = |list: &[String], k: &str| e.get(k).and_then(Value::as_str).is_some_and(|v| list.iter().any(|x| x == v));
        has(&self.currencies, "currency") && has(&self.impacts, "impact")
    }
}

/// `CalendarSettings.from_dict(body)` then `.validate()`: the settings as `to_dict()` gives them.
pub fn settings(body: &Map<String, Value>) -> Result<Value, Refused> {
    checked(body).map(|s| s.to_json())
}

fn checked(body: &Map<String, Value>) -> Result<CalendarSettings, Refused> {
    let s = CalendarSettings::from_dict(body).map_err(Refused::Invalid)?;
    let errors = s.validate();
    if !errors.is_empty() {
        return Err(Refused::Invalid(errors.join("; ")));
    }
    Ok(s)
}

/// Check and save the settings (PUT /api/calendar) without a [`Calendar`]: the Python one reads them
/// from the store. With a running [`Calendar`], [`put`] does this and answers too.
pub async fn save(store: &Store, body: &Map<String, Value>) -> Result<Value, Refused> {
    let new = settings(body)?;
    store.set_setting(CALENDAR_KEY, &new).await?;
    Ok(new)
}

// ---- the calendar (news.Calendar) ----

pub const CALENDAR_CACHE_KEY: &str = "calendar_cache";
/// How old the cached week may get before it's fetched again.
pub const REFRESH: Duration = Duration::hours(1);
/// How long after a failed fetch the next try waits.
pub const RETRY: Duration = Duration::minutes(10);
/// How often the background task looks whether the week is stale.
pub const TICK: StdDuration = StdDuration::from_secs(60);
const FETCH_TIMEOUT: StdDuration = StdDuration::from_secs(15);
const USER_AGENT: &str = "Mozilla/5.0 (Wednesday calendar)";

/// The innermost cause of a request error (Python shows the socket error's reason).
fn reason(e: &reqwest::Error) -> String {
    if e.is_timeout() {
        return "timed out".into();
    }
    let mut src: &dyn std::error::Error = e;
    while let Some(s) = src.source() {
        src = s;
    }
    src.to_string()
}

/// `fetch_events`: the feed at `url`, parsed; Err with what went wrong, in Python's words.
pub async fn fetch_events(client: &reqwest::Client, url: &str) -> Result<Vec<Value>, String> {
    let reach = |e: reqwest::Error| format!("Can't reach the calendar source: {}", reason(&e));
    let resp = client.get(url).header(reqwest::header::USER_AGENT, USER_AGENT).timeout(FETCH_TIMEOUT).send().await
        .map_err(reach)?;
    if !resp.status().is_success() {
        return Err(format!("Calendar source returned HTTP {}", resp.status().as_u16()));
    }
    let body = resp.bytes().await.map_err(reach)?;
    let payload: Value = serde_json::from_str(&String::from_utf8_lossy(&body))
        .map_err(|_| "The calendar source didn't return JSON".to_string())?;
    match payload {
        Value::Array(rows) => Ok(parse_events(&rows)),
        _ => Err("Unexpected calendar format (expected a list of events)".into()),
    }
}

#[derive(Default)]
struct State {
    settings: CalendarSettings,
    saved: Option<Value>, // the stored value `settings` was read from
    events: Vec<Value>,
    fetched_at: Option<String>,
    source: Option<String>,
    error: Option<String>,
    last_try: Option<DateTime<Utc>>,
    busy: bool,
}

/// The cached week (`news.Calendar`): answers never wait on the network; a stale week is fetched
/// in the background, at most hourly, and a failed fetch is retried after ten minutes while the
/// last good copy stays. The settings are read from the store each time, so a change saved
/// elsewhere (the Python server) shows at once.
pub struct Calendar {
    store: Option<Store>,
    client: reqwest::Client,
    state: Mutex<State>,
    /// The past reactions' cache; [`NewsReactions::invalidate`] after the history changes.
    pub reactions: NewsReactions,
}

/// Clears `busy` when a refresh ends, also when it panics.
struct Done(Arc<Calendar>);

impl Drop for Done {
    fn drop(&mut self) {
        self.0.state.lock().unwrap().busy = false;
    }
}

impl Calendar {
    /// The calendar with the week cached in the store (which goes into the history again), not
    /// fetching by itself; [`Calendar::start`] also keeps it fresh.
    pub async fn new(store: Option<Store>) -> Arc<Calendar> {
        let mut state = State::default();
        if let Some(store) = &store {
            match store.get_setting(CALENDAR_CACHE_KEY).await {
                Ok(Some(Value::Object(cache))) => {
                    state.events = cache.get("events").and_then(Value::as_array).cloned().unwrap_or_default();
                    state.fetched_at = cache.get("fetched_at").and_then(Value::as_str).map(String::from);
                    state.source = cache.get("url").and_then(Value::as_str).map(String::from);
                }
                Ok(_) => {}
                Err(e) => log::warn!("calendar: can't read the cached week: {e}"),
            }
            if !state.events.is_empty() {
                if let Err(e) = store.calendar_put(&store_rows(&state.events, "feed")).await {
                    log::warn!("calendar: can't add the cached week to the history: {e}");
                }
            }
        }
        Arc::new(Calendar { store, client: reqwest::Client::new(), state: Mutex::new(state),
                            reactions: NewsReactions::default() })
    }

    /// [`Calendar::new`], plus a task that fetches the week when it's stale (looking every
    /// minute; it ends when the calendar is dropped).
    pub async fn start(store: Option<Store>) -> Arc<Calendar> {
        let cal = Calendar::new(store).await;
        let weak: Weak<Calendar> = Arc::downgrade(&cal);
        tokio::spawn(async move {
            loop {
                let Some(cal) = weak.upgrade() else { break };
                if let Some(job) = cal.maybe_refresh(Utc::now()).await {
                    drop(cal);
                    let _ = job.await;
                } else {
                    drop(cal);
                }
                tokio::time::sleep(TICK).await;
            }
        });
        cal
    }

    pub fn store(&self) -> Option<&Store> {
        self.store.as_ref()
    }

    /// The saved settings, read from the store each time (a store error keeps the last known ones,
    /// invalid saved ones are ignored).
    pub async fn settings(&self) -> CalendarSettings {
        let Some(store) = &self.store else { return self.state.lock().unwrap().settings.clone() };
        let saved = match store.get_setting(CALENDAR_KEY).await {
            Ok(v) => v,
            Err(e) => {
                log::warn!("calendar: can't read the settings: {e}");
                return self.state.lock().unwrap().settings.clone();
            }
        };
        let mut st = self.state.lock().unwrap();
        if let Some(saved) = saved {
            if st.saved.as_ref() != Some(&saved) {
                let empty = Map::new();
                let parsed = match &saved {
                    Value::Object(m) => CalendarSettings::from_dict(m),
                    v if !truthy(v) => CalendarSettings::from_dict(&empty),
                    _ => Err("not a dict".into()),
                };
                match parsed {
                    Ok(s) => st.settings = s,
                    Err(_) => log::warn!("calendar: ignoring invalid saved settings"),
                }
                st.saved = Some(saved);
            }
        }
        st.settings.clone()
    }

    /// Use and save `new`.
    pub async fn set_settings(&self, new: CalendarSettings) -> Result<(), String> {
        let value = new.to_json();
        if let Some(store) = &self.store {
            store.set_setting(CALENDAR_KEY, &value).await?;
        }
        let mut st = self.state.lock().unwrap();
        st.settings = new;
        st.saved = self.store.as_ref().map(|_| value);
        Ok(())
    }

    fn stale(st: &State, settings: &CalendarSettings, now: DateTime<Utc>) -> bool {
        if st.source.as_deref() != Some(settings.url.as_str()) {
            return true;
        }
        match st.fetched_at.as_deref().filter(|f| !f.is_empty()) {
            None => true,
            Some(f) => match events::fromisoformat(f).and_then(events::IsoTime::aware) {
                Some(at) => now - at >= REFRESH,
                None => true,
            },
        }
    }

    /// Start a fetch in the background when the week is stale, enabled, not already being fetched,
    /// and not within ten minutes of a failed try. The task, when one started.
    pub async fn maybe_refresh(self: &Arc<Self>, now: DateTime<Utc>) -> Option<tokio::task::JoinHandle<()>> {
        let settings = self.settings().await;
        {
            let mut st = self.state.lock().unwrap();
            if !settings.enabled || !Self::stale(&st, &settings, now) {
                return None;
            }
            if st.busy || (st.error.is_some() && st.last_try.is_some_and(|t| now - t < RETRY)) {
                return None;
            }
            st.busy = true;
            st.last_try = Some(now);
        }
        let done = Done(self.clone());
        Some(tokio::spawn(async move {
            done.0.refresh().await;
            drop(done);
        }))
    }

    /// Fetch now, and wait for it (`refresh`).
    pub async fn refresh(&self) {
        let url = self.settings().await.url;
        let events = match fetch_events(&self.client, &url).await {
            Ok(events) => events,
            Err(e) => {
                log::warn!("calendar: {e}");
                self.state.lock().unwrap().error = Some(e);
                return;
            }
        };
        let fetched_at = events::iso_utc(Utc::now());
        {
            let mut st = self.state.lock().unwrap();
            st.events = events.clone();
            st.fetched_at = Some(fetched_at.clone());
            st.source = Some(url.clone());
            st.error = None;
        }
        if let Some(store) = &self.store {
            let cache = json!({"events": events, "fetched_at": fetched_at, "url": url});
            if let Err(e) = store.set_setting(CALENDAR_CACHE_KEY, &cache).await {
                log::warn!("calendar: can't keep the fetched week: {e}");
            }
            if let Err(e) = store.calendar_put(&store_rows(&events, "feed")).await {
                log::warn!("calendar: can't add the fetched week to the history: {e}");
            }
        }
    }

    /// Matching events from 30 minutes ago onwards (so a release stays visible a while), as
    /// `Calendar.upcoming()` gives them: what a journal entry freezes as its news.
    pub async fn upcoming(&self) -> Vec<Value> {
        let settings = self.settings().await;
        let st = self.state.lock().unwrap();
        upcoming_of(&st.events, &settings, Utc::now())
    }

    /// The events as the Python calendar holds them (cached or fetched).
    pub fn events(&self) -> Vec<Value> {
        self.state.lock().unwrap().events.clone()
    }

    /// `snapshot()` at `now`: settings, upcoming events, the week, when it was fetched, the last
    /// error, and whether a fetch is running (a stale week starts one).
    pub async fn snapshot(self: &Arc<Self>, now: DateTime<Utc>) -> Map<String, Value> {
        let _ = self.maybe_refresh(now).await;
        let s = self.settings().await;
        let st = self.state.lock().unwrap();
        let source_matches = st.source.as_deref() == Some(s.url.as_str());
        let show = s.enabled && source_matches;
        let mut out = Map::new();
        out.insert("settings".into(), s.to_json());
        out.insert("events".into(), if show { upcoming_of(&st.events, &s, now).into() } else { json!([]) });
        out.insert("week".into(), if show { matching_of(&st.events, &s).into() } else { json!([]) });
        out.insert("fetched_at".into(), if source_matches { json!(st.fetched_at) } else { Value::Null });
        out.insert("error".into(), json!(st.error));
        out.insert("loading".into(), json!(st.busy));
        out
    }
}

/// `upcoming(now)`: the events matching the settings, from 30 minutes before `now` on.
pub fn upcoming_of(events: &[Value], s: &CalendarSettings, now: DateTime<Utc>) -> Vec<Value> {
    events
        .iter()
        .filter(|e| s.shows(e) && events::aware_time(e.get("time")).is_some_and(|t| t >= now - Duration::minutes(30)))
        .cloned()
        .collect()
}

/// `matching()`: every event in the week that matches the settings (for the chart).
pub fn matching_of(events: &[Value], s: &CalendarSettings) -> Vec<Value> {
    events.iter().filter(|e| s.shows(e)).cloned().collect()
}

// ---- the market the reactions read ----

/// What the past reactions read: the feed's clock, its stream, and its M1 bars (the live buffer,
/// and the stored bars before it).
#[derive(Clone)]
pub struct Market {
    pub clock: String,
    pub source: String,
    pub symbol: String,
    pub m1: Arc<Vec<Bar>>,
    pub persist: bool,
    pub store: Option<Store>,
}

impl Market {
    /// The running engine's.
    pub fn of(rt: &Runtime) -> Market {
        let engine = rt.engine();
        let st = engine.state();
        Market { clock: engine.clock.clone(), source: engine.source.clone(), symbol: engine.symbol.clone(), m1: st.m1,
                 persist: engine.persist, store: rt.store.clone() }
    }

    /// `news_bars`: M1 bars opening in [start, end], from the live buffer and, before it, the store.
    pub async fn bars(&self, start: Time, end: Time) -> Result<Vec<Bar>, String> {
        let part: Vec<Bar> = self.m1.iter().filter(|b| b.time >= start && b.time <= end).copied().collect();
        if part.first().is_some_and(|b| b.time <= start) || !self.persist {
            return Ok(part);
        }
        let Some(store) = &self.store else { return Ok(part) };
        let stored = load_bar_range(store, &self.source, &self.symbol, unix(start), unix(end)).await?;
        if stored.is_empty() {
            return Ok(part);
        }
        let Some(first) = part.first().map(|b| b.time) else { return Ok(stored) };
        let mut out: Vec<Bar> = stored.into_iter().filter(|b| b.time < first).collect();
        out.extend(part);
        Ok(out)
    }
}

/// `Store.load_bar_range`: stored M1 bars with open times in [start, end] (feed-clock unix seconds).
pub async fn load_bar_range(store: &Store, source: &str, symbol: &str, start: i64, end: i64) -> Result<Vec<Bar>, String> {
    let rows = store
        .rows("m1_bars", "SELECT time, open, high, low, close, volume FROM m1_bars \
                          WHERE source = ? AND symbol = ? AND time >= ? AND time <= ? ORDER BY time",
              vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into())), Arg::Int(Some(start)), Arg::Int(Some(end))])
        .await?;
    Ok(rows
        .iter()
        .filter_map(|r| {
            let f = |k: &str| r.get(k).and_then(Value::as_f64).unwrap_or(f64::NAN);
            Some(Bar { time: DateTime::from_timestamp(r.get("time")?.as_i64()?, 0)?.naive_utc(), open: f("open"),
                       high: f("high"), low: f("low"), close: f("close"), volume: f("volume") })
        })
        .collect())
}

// ---- the routes ----

fn internal(e: impl std::fmt::Display) -> Refusal {
    log::error!("calendar: {e}");
    Refusal::new(500, "Internal Server Error")
}

fn store_failed(e: String) -> Refusal {
    log::error!("database: {e}");
    Refusal::new(500, "The database couldn't be read or written. Try again, and restart Wednesday if this stays.")
}

/// `history_summary`: how many releases the history holds, and the first and last one's time.
pub async fn history_summary(store: &Store) -> Result<Value, String> {
    let (n, first, last) = store.calendar_bounds().await?;
    let iso = |t: Option<i64>| t.and_then(|t| DateTime::from_timestamp(t, 0)).map(events::iso_utc);
    Ok(json!({"stored": n, "first": iso(first), "last": iso(last)}))
}

/// `news_stats`: the past reactions per event type for the calendar's currencies and impacts.
pub async fn news_stats(cal: &Calendar, market: &Market, now: f64) -> Result<Map<String, Value>, String> {
    let Some(store) = &cal.store else { return Ok(Map::new()) };
    let s = cal.settings().await;
    cal.reactions
        .stats(store, |a, b| market.bars(a, b), &market.clock, &s.currencies, &s.impacts,
               (market.source.clone(), market.symbol.clone()), now)
        .await
}

fn now_seconds(now: DateTime<Utc>) -> f64 {
    now.timestamp_micros() as f64 / 1e6
}

/// The `GET /api/calendar` answer at `now`: the snapshot with each event's time on the chart's
/// axis (feed clock) and how gold moved after its past releases, and the history's summary.
pub async fn payload(cal: &Arc<Calendar>, market: &Market, now: DateTime<Utc>) -> Answer {
    let mut snap = cal.snapshot(now).await;
    let clock = Clock::parse(&market.clock).map_err(internal)?;
    let empty = |k: &str| snap[k].as_array().is_none_or(Vec::is_empty);
    let stats = if empty("events") && empty("week") {
        Map::new()
    } else {
        news_stats(cal, market, now_seconds(now)).await.map_err(internal)?
    };
    for key in ["events", "week"] {
        let list = snap[key].as_array().cloned().unwrap_or_default();
        let list: Vec<Value> = list
            .into_iter()
            .map(|mut e| {
                let chart = events::aware_time(e.get("time")).map(|t| unix(clock.utc_to_feed(t)));
                let k = events::event_key(e["currency"].as_str().unwrap_or(""), e["title"].as_str().unwrap_or(""));
                if let Value::Object(m) = &mut e {
                    m.insert("chart_time_unix".into(), json!(chart));
                    m.insert("reaction".into(), stats.get(&k).cloned().unwrap_or(Value::Null));
                }
                e
            })
            .collect();
        snap.insert(key.into(), Value::Array(list));
    }
    if let Some(store) = &cal.store {
        snap.insert("history".into(), history_summary(store).await.map_err(store_failed)?);
    }
    let mut out = Map::new();
    out.insert("editable".into(), json!(true));
    out.extend(snap);
    Ok(Value::Object(out))
}

/// `GET /api/calendar`: upcoming news matching the settings (fetched in the background, at most hourly).
pub async fn get(cal: &Arc<Calendar>, rt: &Runtime) -> Answer {
    payload(cal, &Market::of(rt), Utc::now()).await
}

/// `PUT /api/calendar`: check and save the settings, then answer as GET does.
pub async fn put(cal: &Arc<Calendar>, rt: &Runtime, body: &Map<String, Value>) -> Answer {
    let new = checked(body).map_err(|e| match e {
        Refused::Invalid(m) => Refusal::new(422, m),
        Refused::Store(m) => store_failed(m),
    })?;
    cal.set_settings(new).await.map_err(store_failed)?;
    get(cal, rt).await
}

/// `GET /api/calendar/reactions`: per event type, gold's move after past releases.
pub async fn reactions(cal: &Arc<Calendar>, rt: &Runtime) -> Answer {
    reactions_at(cal, &Market::of(rt), Utc::now()).await
}

pub async fn reactions_at(cal: &Arc<Calendar>, market: &Market, now: DateTime<Utc>) -> Answer {
    let Some(store) = &cal.store else {
        return Ok(json!({"available": false, "types": [], "history": null}));
    };
    let stats = news_stats(cal, market, now_seconds(now)).await.map_err(internal)?;
    let mut types: Vec<Value> = stats.into_iter().map(|(_, v)| v).collect();
    let text = |v: &Value, k: &str| v[k].as_str().unwrap_or("").to_string();
    types.sort_by(|a, b| {
        let key = |v: &Value| (-(v["count"].as_i64().unwrap_or(0)), text(v, "currency"), text(v, "title"));
        key(a).cmp(&key(b))
    });
    Ok(json!({"available": true, "types": types, "history": history_summary(store).await.map_err(store_failed)?}))
}

/// `POST /api/calendar/import?zone=UTC`: add a past calendar (CSV, the raw body) to the history.
pub async fn import(cal: &Calendar, zone: Option<&str>, body: &[u8]) -> Answer {
    let Some(store) = &cal.store else {
        return Err(Refusal::new(409, "Importing a calendar needs the server running with --serve and a database"));
    };
    let rows = match import::parse_csv(body, zone.unwrap_or("UTC")) {
        Ok(rows) => rows,
        Err(ImportError::Invalid(m)) => return Err(Refusal::new(422, m)),
        Err(ImportError::Internal(m)) => return Err(internal(m)),
    };
    store.calendar_put(&rows).await.map_err(store_failed)?;
    cal.reactions.invalidate();
    Ok(json!({"imported": rows.len(), "history": history_summary(store).await.map_err(store_failed)?}))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The routes' futures can run on axum's (multi-threaded) runtime.
    #[allow(dead_code)]
    fn routes_are_send(cal: Arc<Calendar>, rt: Arc<Runtime>, m: Market, body: Map<String, Value>) {
        fn send<T: Send>(_: T) {}
        send(get(&cal, &rt));
        send(put(&cal, &rt, &body));
        send(reactions(&cal, &rt));
        send(import(&cal, None, b""));
        send(payload(&cal, &m, Utc::now()));
        send(cal.upcoming());
        send(Calendar::start(None));
    }

    fn check(body: Value) -> Result<Value, String> {
        settings(body.as_object().unwrap()).map_err(|e| match e {
            Refused::Invalid(m) | Refused::Store(m) => m,
        })
    }

    #[test]
    fn defaults_and_cleaning() {
        assert_eq!(check(json!({})).unwrap(),
                   json!({"enabled": true, "url": DEFAULT_URL, "currencies": ["USD"], "impacts": ["High"]}));
        let s = check(json!({"enabled": 0, "url": " https://x.test/c.json ", "currencies": [" usd ", "", "eur"]})).unwrap();
        assert_eq!(s, json!({"enabled": false, "url": "https://x.test/c.json", "currencies": ["USD", "EUR"],
                             "impacts": ["High"]}));
        assert_eq!(check(json!({"enabled": "no", "url": null})).unwrap()["enabled"], json!(true)); // bool("no")
        assert_eq!(check(json!({"enabled": null})).unwrap()["enabled"], json!(false));
    }

    #[test]
    fn errors_as_python_words_them() {
        assert_eq!(check(json!({"url": "ftp://x", "impacts": ["Huge", "it's"], "currencies": [" "]})).unwrap_err(),
                   "The calendar URL must start with http:// or https://; Unknown impact 'Huge'; \
                    Unknown impact \"it's\"; Pick at least one currency");
        assert_eq!(check(json!({"url": 5})).unwrap_err(), "The calendar URL must be text");
        assert_eq!(check(json!({"currencies": "USD"})).unwrap_err(),
                   "Currencies must be a list of currency codes, like USD");
        assert_eq!(check(json!({"impacts": null})).unwrap_err(), "Impacts must be a list of High, Medium or Low");
        assert_eq!(py_repr("a\tb\\'\u{a0}"), "\"a\\tb\\\\'\\xa0\"");
    }
}
