//! The multi-timeframe scan (`scanner.py`): every detector on every timeframe, high to low, the
//! nearest active level above and below price, and the order block limit setups ranked.

use serde_json::{json, Map, Value};

use super::candles::{resample, Bar, Timeframe, TIMEFRAMES};
use super::clock::Time;
use super::detectors::{priority_rank, Detector, DetectorParams, DETECTORS};
use super::levels::{iso, Level};
use super::structure::{label_swings, latest_bias, Context};

#[derive(Clone, Debug)]
pub struct ScanConfig {
    pub timeframes: Vec<Timeframe>,
    pub lookback: usize, // closed candles per timeframe handed to the detectors
    pub detectors: Vec<Detector>,
    pub params: DetectorParams,
    pub recent_bars: usize, // levels that ended within this many candles are reported as events
}

impl Default for ScanConfig {
    fn default() -> Self {
        ScanConfig { timeframes: TIMEFRAMES.to_vec(), lookback: 200, detectors: DETECTORS.to_vec(),
                     params: DetectorParams::default(), recent_bars: 3 }
    }
}

impl ScanConfig {
    /// M1 history needed so every timeframe gets `lookback` closed candles.
    pub fn required_m1_bars(&self) -> usize {
        self.timeframes.iter().map(|t| t.minutes as usize).max().unwrap_or(0) * (self.lookback + 1)
    }

    fn ordered(&self) -> Vec<Timeframe> {
        let mut tfs = self.timeframes.clone();
        tfs.sort_by_key(|t| std::cmp::Reverse(t.minutes));
        tfs
    }
}

/// One detector's output on one timeframe, split relative to price.
pub struct LevelSet {
    pub active: Vec<Level>,
    pub above: Option<usize>, // index in `active`: nearest level fully above price
    pub below: Option<usize>, // nearest fully below
    pub inside: Vec<usize>,   // levels containing price
    pub recent: Vec<Level>,   // ended within the last `recent_bars` candles
}

impl LevelSet {
    pub fn above(&self) -> Option<&Level> {
        self.above.map(|i| &self.active[i])
    }

    pub fn below(&self) -> Option<&Level> {
        self.below.map(|i| &self.active[i])
    }

    fn to_json(&self) -> Value {
        json!({
            "active_count": self.active.len(),
            "active": self.active.iter().map(Level::to_json).collect::<Vec<_>>(),
            "above": self.above().map(Level::to_json),
            "below": self.below().map(Level::to_json),
            "inside": self.inside.iter().map(|&i| self.active[i].to_json()).collect::<Vec<_>>(),
            "recent": self.recent.iter().map(Level::to_json).collect::<Vec<_>>(),
        })
    }
}

pub struct TimeframeResult {
    pub timeframe: Timeframe,
    pub candles: usize,
    pub sets: Vec<(Detector, LevelSet)>,
    pub bias: Option<Value>,   // latest break of structure (external swings)
    pub swings: Vec<Value>,    // labelled HH/LH/HL/LL
}

pub struct ScanResult {
    pub time: Time,
    pub price: f64,
    pub detectors: Vec<Detector>,
    pub results: Vec<TimeframeResult>, // high TF to low
}

/// Nearest fully above, nearest fully below (the first of equals), and levels containing price.
pub fn split_by_price(levels: &[Level], price: f64) -> (Option<usize>, Option<usize>, Vec<usize>) {
    let mut above: Option<usize> = None;
    let mut below: Option<usize> = None;
    let mut inside = Vec::new();
    for (i, lv) in levels.iter().enumerate() {
        if lv.bottom > price && above.map_or(true, |a| lv.bottom - price < levels[a].bottom - price) {
            above = Some(i);
        }
        if lv.top < price && below.map_or(true, |b| price - lv.top < price - levels[b].top) {
            below = Some(i);
        }
        if lv.contains(price) {
            inside.push(i);
        }
    }
    (above, below, inside)
}

/// The closed candles a timeframe scans: the last `lookback` of them.
pub fn candles_for(m1: &[Bar], tf: Timeframe, lookback: usize) -> Vec<Bar> {
    let all = resample(m1, tf);
    all[all.len().saturating_sub(lookback)..].to_vec()
}

pub fn scan_timeframe(m1: &[Bar], tf: Timeframe, price: f64, cfg: &ScanConfig) -> TimeframeResult {
    let candles = candles_for(m1, tf, cfg.lookback);
    let ctx = Context::new(&candles);
    let recent_from =
        (cfg.recent_bars > 0 && candles.len() >= cfg.recent_bars).then(|| candles[candles.len() - cfg.recent_bars].time);
    let sets = cfg
        .detectors
        .iter()
        .map(|&det| {
            let levels = if candles.is_empty() { Vec::new() } else { det.detect(&ctx, &cfg.params) };
            let recent = levels
                .iter()
                .filter(|lv| matches!((lv.ended_time, recent_from), (Some(e), Some(r)) if e >= r))
                .cloned()
                .collect();
            let active: Vec<Level> = levels.into_iter().filter(Level::active).collect();
            let (above, below, inside) = split_by_price(&active, price);
            (det, LevelSet { active, above, below, inside, recent })
        })
        .collect();
    let (bias, swings) = if candles.is_empty() {
        (None, Vec::new())
    } else {
        (latest_bias(&ctx, cfg.params.swing_length), label_swings(&ctx.structure(cfg.params.swing_length), &ctx.times))
    };
    TimeframeResult { timeframe: tf, candles: candles.len(), sets, bias, swings }
}

/// Scan every timeframe from high to low against `price` (the last close when None).
pub fn scan(m1: &[Bar], cfg: &ScanConfig, price: Option<f64>) -> Result<ScanResult, String> {
    let last = m1.last().ok_or("No M1 data to scan")?;
    let price = price.unwrap_or(last.close);
    let results = cfg.ordered().into_iter().map(|tf| scan_timeframe(m1, tf, price, cfg)).collect();
    Ok(ScanResult { time: last.time, price, detectors: cfg.detectors.clone(), results })
}

/// How a setup sits with the trader's bias (`bias.setup_risk`): `on`, `off`, `no_trade`, or
/// None without a bias. `bias` is "bullish", "bearish" or "neutral".
pub fn setup_risk(side: &str, bias: Option<&str>) -> Option<&'static str> {
    match bias? {
        "neutral" => Some("no_trade"),
        b => Some(if (b == "bearish") == (side == "sell") { "on" } else { "off" }),
    }
}

impl ScanResult {
    fn set(r: &TimeframeResult, det: Detector) -> Option<&LevelSet> {
        r.sets.iter().find(|(d, _)| *d == det).map(|(_, s)| s)
    }

    /// The nearest level of `det` across all timeframes, `above` or below price.
    pub fn nearest(&self, det: Detector, above: bool) -> Option<(Timeframe, &Level)> {
        let mut best: Option<(Timeframe, &Level, f64)> = None;
        for r in &self.results {
            let Some(s) = Self::set(r, det) else { continue };
            let Some(lv) = (if above { s.above() } else { s.below() }) else { continue };
            let d = if above { lv.bottom - self.price } else { self.price - lv.top };
            if best.map_or(true, |b| d < b.2) {
                best = Some((r.timeframe, lv, d));
            }
        }
        best.map(|(tf, lv, _)| (tf, lv))
    }

    /// Order block limit setups on one side ("sell": above price, "buy": below), best first:
    /// extreme before middle, then the nearest entry; with the bias on that side, order blocks
    /// at a lower high (sells) or higher low (buys) first. (timeframe, level, distance to entry).
    pub fn setups(&self, side: &str, limit: usize, bias: Option<&str>) -> Vec<(Timeframe, &Level, f64)> {
        let focus = if side == "sell" { "LH" } else { "HL" };
        let prefer_swing = setup_risk(side, bias) == Some("on");
        let want = if side == "sell" { "bearish" } else { "bullish" };
        let mut out = Vec::new();
        for r in &self.results {
            let Some(obs) = Self::set(r, Detector::OrderBlock) else { continue };
            for lv in &obs.active {
                let Some(entry) = lv.meta_f64("entry") else { continue };
                if lv.kind != want {
                    continue;
                }
                let dist = if side == "sell" { entry - self.price } else { self.price - entry };
                if dist > 0.0 {
                    out.push((r.timeframe, lv, dist));
                }
            }
        }
        out.sort_by(|a, b| {
            let key = |t: &(Timeframe, &Level, f64)| {
                (prefer_swing && t.1.meta_str("swing") != Some(focus), priority_rank(t.1.meta_str("priority")))
            };
            key(a).cmp(&key(b)).then(a.2.total_cmp(&b.2))
        });
        out.truncate(limit);
        out
    }

    /// The scan as the API gives it; `bias` (an active trade bias) labels each setup.
    pub fn to_json(&self, bias: Option<&str>) -> Value {
        let near = |det: Detector, above: bool| {
            self.nearest(det, above).map(|(tf, lv)| with(json!({"timeframe": tf.name}), lv.to_json()))
        };
        let nearest: Map<String, Value> = self
            .detectors
            .iter()
            .map(|&d| (d.name().to_string(), json!({"above": near(d, true), "below": near(d, false)})))
            .collect();
        let setups: Map<String, Value> = ["sell", "buy"]
            .iter()
            .map(|&side| {
                let list: Vec<Value> = self
                    .setups(side, 5, bias)
                    .into_iter()
                    .map(|(tf, lv, dist)| {
                        with(json!({"timeframe": tf.name, "distance": dist, "risk": setup_risk(side, bias)}), lv.to_json())
                    })
                    .collect();
                (side.to_string(), Value::from(list))
            })
            .collect();
        let timeframes: Vec<Value> = self
            .results
            .iter()
            .map(|r| {
                let bias = r.bias.as_ref().map(|b| {
                    let break_time = chrono::DateTime::from_timestamp(b["break_time_unix"].as_i64().unwrap(), 0)
                        .unwrap()
                        .naive_utc();
                    with(b.clone(), json!({"break_close_time": iso(break_time + r.timeframe.delta())}))
                });
                let detectors: Map<String, Value> =
                    r.sets.iter().map(|(d, s)| (d.name().to_string(), s.to_json())).collect();
                json!({"timeframe": r.timeframe.name, "candles": r.candles, "bias": bias, "detectors": detectors})
            })
            .collect();
        json!({
            "time": iso(self.time),
            "price": self.price,
            "detectors": self.detectors.iter().map(|d| d.name()).collect::<Vec<_>>(),
            "nearest": nearest,
            "setups": setups,
            "timeframes": timeframes,
        })
    }
}

/// `{**a, **b}`
fn with(a: Value, b: Value) -> Value {
    let mut out = a.as_object().cloned().unwrap_or_default();
    out.extend(b.as_object().cloned().unwrap_or_default());
    Value::Object(out)
}
