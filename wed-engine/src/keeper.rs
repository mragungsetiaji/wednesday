//! Markets kept live (Settings > Markets, `keep_live`; `keeper.py`): their M1 bars and their
//! candles per timeframe are stored even while another market is scanned, so switching to one
//! finds its history without a gap. Nothing about them is scanned.
//!
//! Once a minute the keeper reads the list, opens a TradingView connection per kept market
//! (closing the ones no longer kept), stores the new M1 bars and runs the market's [`Ingest`]
//! (refresh, then one page of backfill), as the engine does for the scanned one. The scanned
//! market is left to the engine. A failure is logged and retried the next minute; it never stops
//! the engine.

use std::collections::HashMap;
use std::sync::{Arc, Weak};
use std::time::Duration as StdDuration;

use chrono::Duration;
use serde_json::Value;

use super::feed::Feed;
use super::ingest::Ingest;
use super::{load_bars, save_bars, Runtime, UPDATE_BARS};

/// The M1 bars fetched the first time, as many as a scan reads.
const FIRST_BARS: usize = 5000;

struct Kept {
    feed: Feed,
    ingest: Ingest,
    connected: bool,
    last: Option<chrono::NaiveDateTime>,
}

impl Kept {
    async fn step(&mut self, store: &wednesday_store::Store, symbol: &str) -> Result<(), String> {
        if !self.connected {
            self.feed.connect().await.map_err(|e| e.to_string())?;
            self.connected = true;
        }
        if self.last.is_none() {
            self.last = load_bars(store, "tradingview", symbol, 1, None).await?.last().map(|b| b.time);
        }
        let mut fresh = self.feed.fetch_m1(if self.last.is_some() { UPDATE_BARS } else { FIRST_BARS }).await
            .map_err(|e| e.to_string())?;
        if let (Some(last), Some(first)) = (self.last, fresh.first()) {
            if first.time > last + Duration::minutes(1) {
                fresh = self.feed.fetch_m1(FIRST_BARS).await.map_err(|e| e.to_string())?; // a gap: all the feed has
            }
        }
        if let Some(l) = self.last {
            fresh.retain(|b| b.time > l - Duration::minutes(5));
        }
        if !fresh.is_empty() {
            save_bars(store, "tradingview", symbol, &fresh).await?;
            self.last = fresh.last().map(|b| b.time);
        }
        self.ingest.refresh(&mut self.feed).await;
        self.ingest.backfill(&mut self.feed).await;
        Ok(())
    }
}

/// Run the keeper beside `rt` until the runtime is dropped.
pub fn spawn(rt: Weak<Runtime>) {
    tokio::spawn(async move {
        let mut kept: HashMap<String, Kept> = HashMap::new();
        loop {
            let Some(runtime) = rt.upgrade() else { break };
            round(&runtime, &mut kept).await;
            drop(runtime);
            tokio::time::sleep(StdDuration::from_secs(60)).await;
        }
        for (_, mut k) in kept.drain() {
            k.feed.close().await;
        }
    });
}

/// One minute's work: follow the list, then keep each market.
async fn round(rt: &Arc<Runtime>, kept: &mut HashMap<String, Kept>) {
    let Some(store) = rt.store.clone() else { return };
    let settings = rt.settings();
    let wanted: Vec<String> = if settings.source != "tradingview" {
        Vec::new() // synthetic runs are tests: nothing to keep
    } else {
        match store.markets("tradingview").await {
            Ok(rows) => rows
                .iter()
                .filter(|r| r.get("keep_live").and_then(Value::as_bool) == Some(true))
                .filter_map(|r| r.get("symbol")?.as_str().map(String::from))
                .collect(),
            Err(e) => {
                log::warn!("keep live: reading the markets: {e}");
                return;
            }
        }
    };
    let scanned = rt.engine().symbol.clone();
    let gone: Vec<String> = kept.keys().filter(|s| !wanted.contains(s) || **s == scanned).cloned().collect();
    for symbol in gone {
        let mut k = kept.remove(&symbol).unwrap();
        if !wanted.contains(&symbol) {
            k.ingest.stopped().await; // the engine marks its own series active when it scans this one
        }
        k.feed.close().await;
    }
    for symbol in wanted.into_iter().filter(|s| *s != scanned) {
        let k = match kept.get_mut(&symbol) {
            Some(k) => k,
            None => {
                let Ok(feed) = Feed::build("tradingview", &symbol) else { continue };
                let ingest = Ingest::new("tradingview", &symbol, Some(store.clone()));
                log::info!("keep live: {symbol}");
                kept.entry(symbol.clone()).or_insert(Kept { feed, ingest, connected: false, last: None })
            }
        };
        if let Err(e) = k.step(&store, &symbol).await {
            log::warn!("keep live {symbol}: {e}; trying again next minute");
            k.feed.close().await;
            k.connected = false;
        }
    }
}
