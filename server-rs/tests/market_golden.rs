//! The bias `PUT /api/bias` saves and the market a journal entry freezes, against the reference
//! output in `tests/golden/market/frozen.json` that `tests/test_market_golden.py` writes (#96, 9f).
//! `freeze` of Python's scan must match exactly; `freeze` of Rust's own scan of the same input
//! within the scan golden's 1e-9.

use std::io::Read;
use std::path::{Path, PathBuf};

use chrono::{DateTime, NaiveDateTime, Utc};
use serde_json::Value;
use wednesday_rs::bias::stamp;
use wednesday_rs::journal::market::freeze;
use wednesday_rs::scan::{scan, Bar, Detector, DetectorParams, ScanConfig};

fn golden() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden")
}

fn reference() -> Value {
    serde_json::from_str(&std::fs::read_to_string(golden().join("market/frozen.json")).unwrap()).unwrap()
}

fn at(s: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
}

/// Exact: the same keys in the same order, ints as ints, floats with the same bits; or, with
/// `tolerance`, floats within it.
fn differences(e: &Value, a: &Value, path: &str, tolerance: f64, out: &mut Vec<String>) {
    if out.len() >= 20 {
        return;
    }
    match (e, a) {
        (Value::Object(e), Value::Object(a)) => {
            let (ke, ka): (Vec<_>, Vec<_>) = (e.keys().collect(), a.keys().collect());
            if ke != ka {
                out.push(format!("{path}: keys {ke:?} became {ka:?}"));
            }
            for (k, x) in e {
                if let Some(y) = a.get(k) {
                    differences(x, y, &format!("{path}.{k}"), tolerance, out);
                }
            }
        }
        (Value::Array(e), Value::Array(a)) => {
            if e.len() != a.len() {
                out.push(format!("{path}: {} items became {}", e.len(), a.len()));
            }
            for (i, (x, y)) in e.iter().zip(a).enumerate() {
                differences(x, y, &format!("{path}[{i}]"), tolerance, out);
            }
        }
        (Value::Number(x), Value::Number(y)) if tolerance > 0.0 => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            if (x - y).abs() > tolerance.max(tolerance * x.abs().max(y.abs())) {
                out.push(format!("{path}: {x} became {y}"));
            }
        }
        (Value::Number(x), Value::Number(y)) => {
            let same = match (x.as_i64(), y.as_i64()) {
                (Some(i), Some(j)) => i == j,
                (None, None) => x.as_f64().unwrap().to_bits() == y.as_f64().unwrap().to_bits(),
                _ => false,
            };
            if !same {
                out.push(format!("{path}: {x} became {y}"));
            }
        }
        _ if e != a => out.push(format!("{path}: {e} became {a}")),
        _ => {}
    }
}

#[test]
fn the_saved_bias_matches_python() {
    let reference = reference();
    let cases = reference["bias"].as_array().unwrap();
    assert!(cases.len() > 40);
    let mut failed = Vec::new();
    for c in cases {
        let got = stamp(c["body"].as_object().unwrap(), at(c["now"].as_str().unwrap()));
        let (saved, error) = match got {
            Ok(s) => (s.unwrap_or(Value::Null), Value::Null),
            Err(e) => (Value::Null, Value::String(e)),
        };
        let mut problems = Vec::new();
        differences(&c["saved"], &saved, "saved", 0.0, &mut problems);
        differences(&c["error"], &error, "error", 0.0, &mut problems);
        if !problems.is_empty() {
            failed.push(format!("{} at {}: {}", c["body"], c["now"], problems.join("; ")));
        }
    }
    assert!(failed.is_empty(), "{}", failed.join("\n"));
}

fn read_input(dir: &Path) -> Vec<Bar> {
    let mut text = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(dir.join("m1.csv.gz")).unwrap()).read_to_string(&mut text).unwrap();
    text.lines()
        .skip(1)
        .map(|l| {
            let f: Vec<&str> = l.split(',').collect();
            let n = |i: usize| f[i].parse::<f64>().unwrap();
            Bar { time: NaiveDateTime::parse_from_str(f[0], "%Y-%m-%dT%H:%M").unwrap(), open: n(1), high: n(2),
                  low: n(3), close: n(4), volume: n(5) }
        })
        .collect()
}

/// Rust's scan of a golden input with the case's settings, as `to_json(active)` gives it.
fn rust_scan(input: &str, direction: Option<&str>) -> Value {
    let dir = golden().join(input);
    let expected: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("expected.json")).unwrap()).unwrap();
    let case = &expected["case"];
    let cfg = ScanConfig {
        lookback: case["lookback"].as_u64().unwrap() as usize,
        detectors: case["detectors"].as_array().unwrap().iter().map(|d| Detector::from_name(d.as_str().unwrap()).unwrap()).collect(),
        recent_bars: case["recent_bars"].as_u64().unwrap() as usize,
        params: DetectorParams::from_json(&case["params"]).unwrap(),
        ..ScanConfig::default()
    };
    let m1 = read_input(&dir);
    let price = m1.last().unwrap().close;
    scan(&m1, &cfg, Some(price)).unwrap().to_json(direction)
}

#[test]
fn the_frozen_market_matches_python() {
    let reference = reference();
    let cases = reference["market"].as_array().unwrap();
    assert!(cases.len() >= 5);
    let mut failed = Vec::new();
    for c in cases {
        let name = c["name"].as_str().unwrap();
        let python_scan = c["scan"].as_str().map(|k| &reference["scans"][k]);
        let bias = c["bias"].as_object().map(|_| &c["bias"]);
        let news = c["news"].as_array().unwrap();
        let (symbol, source) = (c["symbol"].as_str().unwrap(), c["source"].as_str().unwrap());
        let price = c["price"].as_f64();
        let now = at(c["now"].as_str().unwrap());
        let mut problems = Vec::new();
        let frozen = freeze(python_scan, bias, news, symbol, source, price, now);
        differences(&c["expected"], &frozen, "freeze(python scan)", 0.0, &mut problems);
        if let Some(input) = c["input"].as_str() {
            let scan = rust_scan(input, bias.and_then(|b| b["direction"].as_str()));
            let frozen = freeze(Some(&scan), bias, news, symbol, source, price, now);
            differences(&c["expected"], &frozen, "freeze(rust scan)", 1e-9, &mut problems);
        }
        if !problems.is_empty() {
            failed.push(format!("{name}:\n  {}", problems.join("\n  ")));
        }
    }
    assert!(failed.is_empty(), "{}", failed.join("\n"));
}
