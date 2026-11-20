//! The Rust scan against the reference output in `tests/golden/` (#96): the same M1 input as the
//! Python scan gets, the same candles, swings, levels, setups, sessions and quarters out. The
//! Python side is `tests/test_golden.py`, which writes the files; floats agree within 1e-9.

use std::io::Read;
use std::path::{Path, PathBuf};

use chrono::NaiveDateTime;
use serde_json::{json, Map, Value};
use wednesday_rs::scan::quarters::quarters_payload;
use wednesday_rs::scan::scanner::candles_for;
use wednesday_rs::scan::sessions::{reference_levels, MAX_DAYS};
use wednesday_rs::scan::structure::{label_swings, latest_bias, Context};
use wednesday_rs::scan::{scan, unix, Bar, Clock, Detector, DetectorParams, ScanConfig, TIMEFRAMES};

fn golden() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden")
}

fn read_input(dir: &Path) -> Vec<Bar> {
    let mut text = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(dir.join("m1.csv.gz")).unwrap())
        .read_to_string(&mut text)
        .unwrap();
    let mut lines = text.lines();
    assert_eq!(lines.next(), Some("time,open,high,low,close,volume"));
    lines
        .map(|l| {
            let f: Vec<&str> = l.split(',').collect();
            let n = |i: usize| f[i].parse::<f64>().unwrap();
            Bar { time: NaiveDateTime::parse_from_str(f[0], "%Y-%m-%dT%H:%M").unwrap(), open: n(1), high: n(2),
                  low: n(3), close: n(4), volume: n(5) }
        })
        .collect()
}

fn output(case: &Value, m1: &[Bar]) -> Value {
    let params = DetectorParams::from_json(&case["params"]).unwrap();
    let cfg = ScanConfig {
        lookback: case["lookback"].as_u64().unwrap() as usize,
        detectors: case["detectors"].as_array().unwrap().iter().map(|d| Detector::from_name(d.as_str().unwrap()).unwrap()).collect(),
        recent_bars: case["recent_bars"].as_u64().unwrap() as usize,
        params: params.clone(),
        ..ScanConfig::default()
    };
    let clock_name = case["clock"].as_str().unwrap();
    let clock = Clock::parse(clock_name).unwrap();
    let mut timeframes = Map::new();
    for tf in TIMEFRAMES {
        let candles = candles_for(m1, tf, cfg.lookback);
        let ctx = Context::new(&candles);
        let levels: Map<String, Value> = cfg
            .detectors
            .iter()
            .map(|d| (d.name().to_string(), d.detect(&ctx, &params).iter().map(|l| l.to_json()).collect()))
            .collect();
        timeframes.insert(tf.name.into(), json!({
            "candles": candles.iter().map(|c| json!([unix(c.time), c.open, c.high, c.low, c.close, c.volume])).collect::<Vec<_>>(),
            "swings": label_swings(&ctx.structure(params.swing_length), &ctx.times),
            "bias": latest_bias(&ctx, params.swing_length),
            "levels": levels,
        }));
    }
    let result = scan(m1, &cfg, Some(case["price"].as_f64().unwrap())).unwrap();
    let setups: Map<String, Value> = ["bullish", "bearish", "neutral"]
        .iter()
        .map(|b| (b.to_string(), result.to_json(Some(b))["setups"].clone()))
        .collect();
    json!({
        "timeframes": timeframes,
        "scan": result.to_json(None),
        "setups": setups,
        "sessions": reference_levels(m1, clock_name, &clock, MAX_DAYS),
        "quarters": quarters_payload(m1, clock_name, &clock),
    })
}

/// What differs, as `tests/test_golden.py` reports it (floats within 1e-9).
fn differences(e: &Value, a: &Value, path: &str, out: &mut Vec<String>) {
    if out.len() >= 20 {
        return;
    }
    match (e, a) {
        (Value::Object(e), Value::Object(a)) => {
            let mut keys: Vec<&String> = e.keys().chain(a.keys()).collect();
            keys.sort();
            keys.dedup();
            for k in keys {
                match (e.get(k), a.get(k)) {
                    (Some(x), Some(y)) => differences(x, y, &format!("{path}.{k}"), out),
                    (Some(_), None) => out.push(format!("{path}.{k}: missing")),
                    _ => out.push(format!("{path}.{k}: new")),
                }
            }
        }
        (Value::Array(e), Value::Array(a)) => {
            if e.len() != a.len() {
                out.push(format!("{path}: {} items became {}", e.len(), a.len()));
            }
            for (i, (x, y)) in e.iter().zip(a).enumerate() {
                differences(x, y, &format!("{path}[{i}]"), out);
            }
        }
        (Value::Number(x), Value::Number(y)) => {
            let (x, y) = (x.as_f64().unwrap(), y.as_f64().unwrap());
            if (x - y).abs() > 1e-9_f64.max(1e-9 * x.abs().max(y.abs())) {
                out.push(format!("{path}: {x} became {y}"));
            }
        }
        _ if e != a => out.push(format!("{path}: {} became {}", trim(e), trim(a))),
        _ => {}
    }
}

fn trim(v: &Value) -> String {
    v.to_string().chars().take(80).collect()
}

#[test]
fn the_rust_scan_matches_the_reference_output() {
    let mut cases: Vec<PathBuf> = std::fs::read_dir(golden()).unwrap().map(|e| e.unwrap().path())
        .filter(|p| p.join("expected.json").exists()).collect();
    cases.sort();
    assert!(cases.len() >= 3, "the reference cases are missing");
    let mut failed = Vec::new();
    for dir in &cases {
        let expected: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("expected.json")).unwrap()).unwrap();
        let m1 = read_input(dir);
        assert_eq!(m1.len() as u64, expected["case"]["m1_bars"].as_u64().unwrap());
        let actual = output(&expected["case"], &m1);
        let mut want = expected.clone();
        want.as_object_mut().unwrap().remove("case");
        let mut problems = Vec::new();
        differences(&want, &actual, "", &mut problems);
        if !problems.is_empty() {
            failed.push(format!("{}:\n  {}", dir.file_name().unwrap().to_string_lossy(), problems.join("\n  ")));
        }
        if let Ok(to) = std::env::var("GOLDEN_DUMP") {
            // Write what Rust made, to compare with Python's tools: GOLDEN_DUMP=/some/dir cargo test
            let name = dir.file_name().unwrap().to_string_lossy();
            std::fs::write(Path::new(&to).join(format!("{name}.json")), actual.to_string()).unwrap();
        }
    }
    assert!(failed.is_empty(), "the Rust scan differs from tests/golden/:\n{}", failed.join("\n"));
}
