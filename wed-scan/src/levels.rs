//! The common output of every detector: a price zone, or a line when top == bottom (`levels.py`).

use serde_json::{json, Map, Value};

use super::clock::{unix, Time};

pub fn iso(t: Time) -> String {
    t.format("%Y-%m-%dT%H:%M:%S").to_string()
}

#[derive(Clone, Debug)]
pub struct Level {
    pub detector: &'static str, // "ob", "liquidity", "idm"
    pub kind: &'static str,     // "bullish"/"bearish" for OB and IDM, "bsl"/"ssl" for liquidity
    pub top: f64,
    pub bottom: f64,
    pub time: Time,           // open time of the origin candle
    pub confirmed_time: Time, // when the level became known
    pub label: String,
    pub touches: u32, // candles that traded into the level without ending it
    pub ended_time: Option<Time>,
    pub meta: Map<String, Value>,
}

impl Level {
    pub fn new(detector: &'static str, kind: &'static str, top: f64, bottom: f64, time: Time, confirmed: Time,
               label: impl Into<String>) -> Level {
        Level { detector, kind, top, bottom, time, confirmed_time: confirmed, label: label.into(), touches: 0,
                ended_time: None, meta: Map::new() }
    }

    pub fn active(&self) -> bool {
        self.ended_time.is_none()
    }

    pub fn contains(&self, price: f64) -> bool {
        self.bottom <= price && price <= self.top
    }

    pub fn meta_f64(&self, key: &str) -> Option<f64> {
        self.meta.get(key).and_then(Value::as_f64)
    }

    pub fn meta_str(&self, key: &str) -> Option<&str> {
        self.meta.get(key).and_then(Value::as_str)
    }

    pub fn to_json(&self) -> Value {
        json!({
            "detector": self.detector,
            "kind": self.kind,
            "label": self.label,
            "top": self.top,
            "bottom": self.bottom,
            "time": iso(self.time),
            "time_unix": unix(self.time),
            "confirmed_time": iso(self.confirmed_time),
            "touches": self.touches,
            "ended_time": self.ended_time.map(iso),
            "ended_time_unix": self.ended_time.map(unix),
            "meta": self.meta,
        })
    }
}
