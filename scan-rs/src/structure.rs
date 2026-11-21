//! Market structure shared by all detectors: swings, breaks of structure, ATR (`structure.py`).

use serde_json::{json, Value};

use super::candles::Bar;
use super::clock::{unix, Time};
use super::levels::iso;

pub const ATR_LENGTH: usize = 14;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    High,
    Low,
}

#[derive(Clone, Copy, Debug)]
pub struct Swing {
    pub kind: Side,
    pub index: usize, // candle index of the pivot
    pub price: f64,
    pub confirmed: usize, // index of the candle whose close confirms it (index + length)
}

#[derive(Clone, Copy, Debug)]
pub struct Break {
    pub bullish: bool,
    pub index: usize, // candle that closed beyond the swing
    pub swing: Swing,
}

impl Break {
    pub fn direction(&self) -> &'static str {
        if self.bullish { "bullish" } else { "bearish" }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Structure {
    pub swings: Vec<Swing>, // in confirmation order
    pub breaks: Vec<Break>, // in time order
}

/// Bars whose value is the extreme of `length` bars on each side (strict on the left).
pub fn pivot_mask(values: &[f64], length: usize, find_high: bool) -> Vec<bool> {
    let n = values.len();
    let mut mask = vec![false; n];
    if n < 2 * length + 1 {
        return mask;
    }
    for i in length..n - length {
        let window = &values[i - length..=i + length];
        let left = &values[i - length..i];
        let v = values[i];
        mask[i] = if find_high {
            v >= window.iter().cloned().fold(f64::MIN, f64::max) && v > left.iter().cloned().fold(f64::MIN, f64::max)
        } else {
            v <= window.iter().cloned().fold(f64::MAX, f64::min) && v < left.iter().cloned().fold(f64::MAX, f64::min)
        };
    }
    mask
}

/// Swings and breaks of structure without look-ahead: a swing is usable once `length` bars
/// after it have closed; a bullish break is a close above the latest unbroken swing high
/// (bearish: below the latest unbroken swing low); each swing breaks once.
pub fn analyze_structure(high: &[f64], low: &[f64], close: &[f64], length: usize) -> Structure {
    assert!(length >= 1, "swing length must be >= 1");
    let hi = pivot_mask(high, length, true);
    let lo = pivot_mask(low, length, false);
    let mut s = Structure::default();
    let (mut last_high, mut last_low): (Option<Swing>, Option<Swing>) = (None, None);
    for i in 0..close.len() {
        if i >= length {
            let p = i - length;
            if hi[p] {
                let sw = Swing { kind: Side::High, index: p, price: high[p], confirmed: i };
                last_high = Some(sw);
                s.swings.push(sw);
            }
            if lo[p] {
                let sw = Swing { kind: Side::Low, index: p, price: low[p], confirmed: i };
                last_low = Some(sw);
                s.swings.push(sw);
            }
        }
        if let Some(sw) = last_high.filter(|sw| close[i] > sw.price) {
            s.breaks.push(Break { bullish: true, index: i, swing: sw });
            last_high = None;
        }
        if let Some(sw) = last_low.filter(|sw| close[i] < sw.price) {
            s.breaks.push(Break { bullish: false, index: i, swing: sw });
            last_low = None;
        }
    }
    s
}

/// HH/LH for swing highs, HL/LL for swing lows, each against the previous one of its kind.
pub fn label_swings(structure: &Structure, times: &[Time]) -> Vec<Value> {
    let mut swings = structure.swings.clone();
    swings.sort_by_key(|s| s.index); // stable, as Python's sorted
    let (mut prev_high, mut prev_low): (Option<f64>, Option<f64>) = (None, None);
    swings
        .iter()
        .map(|s| {
            let (prev, kind) = match s.kind {
                Side::High => (&mut prev_high, "high"),
                Side::Low => (&mut prev_low, "low"),
            };
            let label = prev.map(|r| match (s.kind, s.price > r) {
                (Side::High, true) => "HH",
                (Side::High, false) => "LH",
                (Side::Low, true) => "HL",
                (Side::Low, false) => "LL",
            });
            *prev = Some(s.price);
            let t = times[s.index];
            json!({"kind": kind, "time": iso(t), "time_unix": unix(t), "price": s.price, "label": label})
        })
        .collect()
}

/// Closed candles of one timeframe plus the analysis every detector shares.
pub struct Context {
    pub times: Vec<Time>,
    pub open: Vec<f64>,
    pub high: Vec<f64>,
    pub low: Vec<f64>,
    pub close: Vec<f64>,
    pub atr: Vec<f64>,
}

impl Context {
    pub fn new(candles: &[Bar]) -> Context {
        let col = |f: fn(&Bar) -> f64| candles.iter().map(f).collect::<Vec<f64>>();
        let (high, low, close) = (col(|b| b.high), col(|b| b.low), col(|b| b.close));
        let atr = average_true_range(&high, &low, &close, ATR_LENGTH);
        Context { times: candles.iter().map(|b| b.time).collect(), open: col(|b| b.open), high, low, close, atr }
    }

    pub fn len(&self) -> usize {
        self.close.len()
    }

    pub fn is_empty(&self) -> bool {
        self.close.is_empty()
    }

    pub fn structure(&self, length: usize) -> Structure {
        analyze_structure(&self.high, &self.low, &self.close, length)
    }
}

/// Mean of the true range over `length` bars ending at each bar (the first bars average what
/// there is). The first bar's true range is its high minus its low.
pub fn average_true_range(high: &[f64], low: &[f64], close: &[f64], length: usize) -> Vec<f64> {
    let n = close.len();
    let tr: Vec<f64> = (0..n)
        .map(|i| {
            let pc = if i == 0 { close[0] } else { close[i - 1] };
            (high[i] - low[i]).max((high[i] - pc).abs().max((low[i] - pc).abs()))
        })
        .collect();
    (0..n)
        .map(|i| {
            let from = (i + 1).saturating_sub(length);
            tr[from..=i].iter().sum::<f64>() / (i + 1).min(length) as f64
        })
        .collect()
}

/// Direction of the latest break of structure; None until the first break in the window.
pub fn latest_bias(ctx: &Context, length: usize) -> Option<Value> {
    let breaks = ctx.structure(length).breaks;
    let last = *breaks.last()?;
    let streak = 1 + breaks[..breaks.len() - 1].iter().rev().take_while(|b| b.bullish == last.bullish).count();
    // The first break against the previous direction is a change of character.
    let event = if streak == 1 && breaks.len() > 1 { "CHoCH" } else { "BOS" };
    let (st, bt) = (ctx.times[last.swing.index], ctx.times[last.index]);
    Some(json!({
        "direction": last.direction(),
        "event": event,
        "level": last.swing.price,
        "swing_time": iso(st),
        "swing_time_unix": unix(st),
        "break_time": iso(bt),
        "break_time_unix": unix(bt),
        "bars_ago": ctx.len() - 1 - last.index,
        "streak": streak,
    }))
}
