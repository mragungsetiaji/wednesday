//! The market-data store's ingest (`ingest.py`): the source's own candles per timeframe, kept in
//! the `bars` table and in memory for the scan and the charts.
//!
//! TradingView caps a series by bar count (about 5 days of 1m), so 4H built from 1m reaches only
//! as far back as the stored 1m, and its candles open on a midnight grid instead of TradingView's
//! sessions. Each timeframe is therefore fetched as the source builds it. The engine calls
//! [`Ingest::refresh`] after every minute's M1 update (a timeframe is fetched again only once a
//! new candle of it has closed) and [`Ingest::backfill`] once a minute, which loads one older page
//! of one series until the source has nothing older. Only closed candles are kept.
//!
//! A source without its own timeframes (synthetic) has none: the scan resamples M1 as before.

use std::collections::HashMap;

use chrono::{Duration, Utc};
use serde_json::Value;

use wednesday_scan::{unix, Bar, Native, Time};
use wednesday_store::{Arg, Store};

use crate::feed::Feed;
use crate::{load_series, save_series};

/// A timeframe the source builds itself: the scan's name for it, the `bars.tf` it is stored
/// under, TradingView's resolution and its length.
#[derive(Clone, Copy, Debug)]
pub struct NativeTf {
    pub scan: &'static str,
    pub tf: &'static str,
    pub resolution: &'static str,
    pub minutes: i64,
}

/// The stored timeframes beside 1m, highest first. 1D isn't scanned; it is kept for the charts
/// and the daily levels.
pub const NATIVE: [NativeTf; 6] = [
    NativeTf { scan: "1D", tf: "1d", resolution: "D", minutes: 1440 },
    NativeTf { scan: "4H", tf: "4h", resolution: "240", minutes: 240 },
    NativeTf { scan: "1H", tf: "1h", resolution: "60", minutes: 60 },
    NativeTf { scan: "30M", tf: "30m", resolution: "30", minutes: 30 },
    NativeTf { scan: "15M", tf: "15m", resolution: "15", minutes: 15 },
    NativeTf { scan: "5M", tf: "5m", resolution: "5", minutes: 5 },
];

/// 1m as a series: stored by the engine's M1 buffer, backfilled here like the others.
pub const M1: NativeTf = NativeTf { scan: "1M", tf: "1m", resolution: "1", minutes: 1 };

/// The native timeframe a scan or chart timeframe ("4H") reads, if any.
pub fn native_tf(scan_name: &str) -> Option<NativeTf> {
    NATIVE.iter().copied().find(|n| n.scan.eq_ignore_ascii_case(scan_name))
}

/// Closed candles kept in memory per timeframe: enough for the scan's lookback and the chart's
/// latest page (`limit` up to 2000).
pub const KEEP: usize = 2000;
/// Candles per request; TradingView sends history in chunks of this size.
pub const PAGE: usize = 5000;

/// Minutes before a timeframe is fetched again after a fetch that failed or brought nothing new.
const RETRY_MINUTES: i64 = 5;

/// The closed candles of `bars` at `now`: a candle is closed once a later one exists or its
/// length has passed.
pub fn closed(bars: Vec<Bar>, minutes: i64, now: Time) -> Vec<Bar> {
    let n = bars.len();
    bars.into_iter()
        .enumerate()
        .filter(|(i, b)| *i + 1 < n || b.time + Duration::minutes(minutes) <= now)
        .map(|(_, b)| b)
        .collect()
}

/// `older` then `newer`, sorted by time, a repeated time keeping the newer bar, the last `keep`.
pub fn merge(older: &[Bar], newer: &[Bar], keep: usize) -> Vec<Bar> {
    let mut all: Vec<Bar> = older.iter().chain(newer.iter()).copied().collect();
    all.sort_by_key(|b| b.time); // stable: of equal times, the newer one comes last
    let mut out: Vec<Bar> = Vec::with_capacity(all.len());
    for b in all {
        match out.last_mut() {
            Some(prev) if prev.time == b.time => *prev = b,
            _ => out.push(b),
        }
    }
    let start = out.len().saturating_sub(keep);
    out.split_off(start)
}

/// The forming candle(s) of a native timeframe from the M1 bars after its last closed candle,
/// on its grid (anchored at that candle), so the chart ends at the live price.
pub fn forming(native: &[Bar], m1: &[Bar], minutes: i64) -> Vec<Bar> {
    let Some(last) = native.last() else { return Vec::new() };
    let step = minutes * 60;
    let anchor = unix(last.time) + step;
    let mut out: Vec<Bar> = Vec::new();
    for b in m1.iter().filter(|b| unix(b.time) >= anchor) {
        let start = anchor + (unix(b.time) - anchor).div_euclid(step) * step;
        match out.last_mut() {
            Some(c) if unix(c.time) == start => {
                c.high = c.high.max(b.high);
                c.low = c.low.min(b.low);
                c.close = b.close;
                c.volume += b.volume;
            }
            _ => {
                let time = chrono::DateTime::from_timestamp(start, 0).expect("in range").naive_utc();
                out.push(Bar { time, ..*b });
            }
        }
    }
    out
}

/// A series' backfill state as the `series` table keeps it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Backfill {
    Pending,
    Running,
    Done,
}

impl Backfill {
    pub fn as_str(self) -> &'static str {
        match self {
            Backfill::Pending => "pending",
            Backfill::Running => "running",
            Backfill::Done => "done",
        }
    }

    fn parse(s: &str) -> Backfill {
        match s {
            "done" => Backfill::Done,
            "running" => Backfill::Running,
            _ => Backfill::Pending,
        }
    }
}

/// The ingest of one (source, symbol): native candles in memory, their series in the store.
pub struct Ingest {
    pub source: String,
    pub symbol: String,
    store: Option<Store>,
    /// Closed native candles by scan timeframe name, the last [`KEEP`].
    pub native: Native,
    /// No fetch of a timeframe before this time (UTC): after one that brought nothing new.
    next_check: HashMap<&'static str, Time>,
    backfill: HashMap<&'static str, Backfill>,
    started: bool,
    /// The source builds no timeframes of its own (synthetic): nothing to do.
    unsupported: bool,
}

impl Ingest {
    pub fn new(source: &str, symbol: &str, store: Option<Store>) -> Ingest {
        Ingest { source: source.into(), symbol: symbol.into(), store, native: Native::new(), next_check: HashMap::new(),
                 backfill: HashMap::new(), started: false, unsupported: false }
    }

    /// Load what the store has (once), then fetch every timeframe with a newly closed candle.
    /// True when a timeframe got new candles. A timeframe that fails is logged in its series row
    /// and tried again next minute; the scan resamples M1 for it meanwhile.
    pub async fn refresh(&mut self, feed: &mut Feed) -> bool {
        if self.unsupported {
            return false;
        }
        let now = Utc::now().naive_utc();
        if !self.started {
            self.started = true;
            self.load().await;
        }
        let mut changed = false;
        for n in NATIVE {
            if self.next_check.get(n.scan).is_some_and(|t| now < *t) {
                continue;
            }
            let have = self.native.get(n.scan).cloned().unwrap_or_default();
            let dur = Duration::minutes(n.minutes);
            let count = match have.last() {
                None => KEEP + 1,
                Some(last) => {
                    if now < last.time + dur * 2 && self.next_check.contains_key(n.scan) {
                        continue; // the next candle is still forming
                    }
                    (((now - last.time).num_minutes() / n.minutes) as usize + 3).min(PAGE)
                }
            };
            let fetched = match feed.fetch_native(n.resolution, count, None).await {
                Ok(Some(bars)) => closed(bars, n.minutes, now),
                Ok(None) => {
                    self.unsupported = true;
                    return false;
                }
                Err(e) => {
                    // Noted in the series row and tried again later; the scan resamples M1 for
                    // this timeframe meanwhile. A timeout or a dropped connection ends this round:
                    // the price fetch after it tells the engine whether to reconnect.
                    log::warn!("{} {}: {e}", self.symbol, n.tf);
                    self.note(n, Some(&e.to_string())).await;
                    self.next_check.insert(n.scan, now + Duration::minutes(RETRY_MINUTES));
                    if e.code.is_some() {
                        continue;
                    }
                    return changed;
                }
            };
            let newest = have.last().map(|b| b.time);
            let new = fetched.last().is_some_and(|b| Some(b.time) > newest);
            if let Some(store) = &self.store {
                if let Err(e) = save_series(store, &self.source, &self.symbol, n.tf, &fetched).await {
                    log::warn!("storing {} {}: {e}", self.symbol, n.tf);
                }
            }
            self.native.insert(n.scan.into(), merge(&have, &fetched, KEEP));
            self.note(n, None).await;
            // Nothing new (the market is closed, say): look again in a while, not every minute.
            let wait = if new { Duration::zero() } else { Duration::minutes(RETRY_MINUTES.min(n.minutes)) };
            self.next_check.insert(n.scan, now + wait);
            changed |= new;
        }
        changed
    }

    /// Native candles in memory from the store, and each series' backfill state.
    async fn load(&mut self) {
        let Some(store) = self.store.clone() else { return };
        for n in NATIVE {
            match load_series(&store, &self.source, &self.symbol, n.tf, KEEP, None).await {
                Ok(bars) if !bars.is_empty() => {
                    self.native.insert(n.scan.into(), bars);
                }
                Ok(_) => {}
                Err(e) => log::warn!("loading {} {}: {e}", self.symbol, n.tf),
            }
        }
        let rows = store
            .rows("series", "SELECT tf, backfill FROM series WHERE source = ? AND symbol = ?",
                  vec![Arg::Str(Some(self.source.clone())), Arg::Str(Some(self.symbol.clone()))])
            .await
            .unwrap_or_default();
        for n in NATIVE.iter().chain([&M1]) {
            let state = rows
                .iter()
                .find(|r| r.get("tf").and_then(Value::as_str) == Some(n.tf))
                .and_then(|r| r.get("backfill")?.as_str())
                .map_or(Backfill::Pending, Backfill::parse);
            self.backfill.insert(n.tf, state);
        }
    }

    /// One page of older history for the first series still being backfilled; true when it
    /// did something. Highest timeframes first: they reach furthest back for the fewest bars.
    pub async fn backfill(&mut self, feed: &mut Feed) -> bool {
        let Some(store) = self.store.clone() else { return false };
        if self.unsupported || !self.started {
            return false;
        }
        let Some(n) = NATIVE.iter().chain([&M1]).copied().find(|n| self.backfill.get(n.tf) != Some(&Backfill::Done))
        else {
            return false;
        };
        let (first, _) = bounds(&store, &self.source, &self.symbol, n.tf).await.unwrap_or((None, None));
        let Some(first) = first else { return false }; // nothing stored yet: refresh comes first
        let page = match feed.fetch_native(n.resolution, PAGE, Some(first)).await {
            Ok(Some(bars)) => bars,
            Ok(None) => return false,
            Err(e) => {
                // TradingView refusing older history ends the backfill; a timeout tries again.
                log::warn!("backfilling {} {}: {e}", self.symbol, n.tf);
                if e.code.is_some() {
                    self.backfill.insert(n.tf, Backfill::Done);
                }
                self.note(n, Some(&e.to_string())).await;
                return true;
            }
        };
        let older: Vec<Bar> = page.into_iter().filter(|b| unix(b.time) < first).collect();
        let state = if older.is_empty() { Backfill::Done } else { Backfill::Running };
        if let Err(e) = save_series(&store, &self.source, &self.symbol, n.tf, &older).await {
            log::warn!("storing {} {}: {e}", self.symbol, n.tf);
        }
        log::info!("{} {}: {} older candles{}", self.symbol, n.tf, older.len(),
                   if state == Backfill::Done { ", the source has nothing older" } else { "" });
        self.backfill.insert(n.tf, state);
        self.note(n, None).await;
        true
    }

    /// The engine stopped: its series are no longer kept up to date.
    pub async fn stopped(&self) {
        if let Some(store) = &self.store {
            deactivate(store, &self.source, &self.symbol).await;
        }
    }

    /// Record a series' reach and state in the `series` table.
    async fn note(&self, n: NativeTf, error: Option<&str>) {
        note_series(self.store.as_ref(), &self.source, &self.symbol, n.tf,
                    self.backfill.get(n.tf).copied().unwrap_or(Backfill::Pending), error).await;
    }
}

/// The first and last stored candle of a series (unix seconds).
pub async fn bounds(store: &Store, source: &str, symbol: &str, tf: &str) -> Result<(Option<i64>, Option<i64>), String> {
    let rows = store
        .rows("series", "SELECT MIN(time) AS first_time, MAX(time) AS last_time FROM bars \
                         WHERE source = ? AND symbol = ? AND tf = ?",
              vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into())), Arg::Str(Some(tf.into()))])
        .await?;
    let get = |k: &str| rows.first().and_then(|r| r.get(k)?.as_i64());
    Ok((get("first_time"), get("last_time")))
}

/// Upsert a `series` row: its reach from `bars`, the backfill state and the last error.
pub async fn note_series(store: Option<&Store>, source: &str, symbol: &str, tf: &str, backfill: Backfill,
                         error: Option<&str>) {
    let Some(store) = store else { return };
    let (first, last) = bounds(store, source, symbol, tf).await.unwrap_or((None, None));
    let row = vec![
        ("source", Arg::Str(Some(source.into()))),
        ("symbol", Arg::Str(Some(symbol.into()))),
        ("tf", Arg::Str(Some(tf.into()))),
        ("active", Arg::Bool(true)),
        ("first_time", Arg::Int(first)),
        ("last_time", Arg::Int(last)),
        ("backfill", Arg::Str(Some(backfill.as_str().into()))),
        ("updated_at", Arg::Str(Some(wednesday_base::time::now_iso()))),
        ("error", Arg::Str(error.map(String::from))),
    ];
    if let Err(e) = store.upsert("series", row, &["source", "symbol", "tf"]).await {
        log::warn!("series {symbol} {tf}: {e}");
    }
}

/// Mark every series of a stream inactive (its engine stopped).
pub async fn deactivate(store: &Store, source: &str, symbol: &str) {
    let _ = store
        .execute("UPDATE series SET active = ? WHERE source = ? AND symbol = ?",
                 vec![Arg::Bool(false), Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into()))])
        .await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    fn t(h: u32, m: u32) -> Time {
        NaiveDate::from_ymd_opt(2026, 11, 24).unwrap().and_hms_opt(h, m, 0).unwrap()
    }

    fn bar(time: Time, close: f64) -> Bar {
        Bar { time, open: close, high: close + 1.0, low: close - 1.0, close, volume: 1.0 }
    }

    #[test]
    fn only_closed_candles_are_kept() {
        let bars = vec![bar(t(3, 0), 1.0), bar(t(7, 0), 2.0), bar(t(11, 0), 3.0)];
        assert_eq!(closed(bars.clone(), 240, t(12, 0)).len(), 2); // 11:00 forms until 15:00
        assert_eq!(closed(bars, 240, t(15, 0)).len(), 3);
    }

    #[test]
    fn merge_keeps_the_newer_bar_and_the_last_few() {
        let out = merge(&[bar(t(3, 0), 1.0), bar(t(7, 0), 2.0)], &[bar(t(7, 0), 9.0), bar(t(11, 0), 3.0)], 2);
        assert_eq!(out.iter().map(|b| b.close).collect::<Vec<_>>(), vec![9.0, 3.0]);
    }

    #[test]
    fn the_forming_candle_follows_the_native_grid() {
        let native = vec![bar(t(3, 0), 1.0), bar(t(7, 0), 2.0)];
        let m1: Vec<Bar> = [(10, 59, 5.0), (11, 0, 6.0), (11, 1, 4.0), (15, 2, 8.0)]
            .iter()
            .map(|&(h, m, c)| bar(t(h, m), c))
            .collect();
        let out = forming(&native, &m1, 240);
        assert_eq!(out.iter().map(|b| b.time).collect::<Vec<_>>(), vec![t(11, 0), t(15, 0)]);
        assert_eq!((out[0].open, out[0].close, out[0].high, out[0].low), (6.0, 4.0, 7.0, 3.0));
    }
}
