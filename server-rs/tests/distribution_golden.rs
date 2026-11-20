//! The Rust move distribution against the reference output in `tests/golden/distribution/` (#96):
//! the same M1 and daily bars in as `tests/test_distribution_golden.py` gives Python, the same
//! period tables and answers out. Floats must match to the last bit, and ints stay ints.

use std::io::Read;
use std::path::{Path, PathBuf};

use chrono::{NaiveDate, NaiveDateTime};
use serde_json::{json, Value};
use wednesday_rs::engine::analysis::{move_distribution, tables, BlockRows, Period};
use wednesday_rs::scan::{unix, Bar, Clock};

fn golden() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden/distribution")
}

fn read_bars(path: &Path, daily: bool) -> Vec<Bar> {
    let mut text = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(path).unwrap()).read_to_string(&mut text).unwrap();
    let mut lines = text.lines();
    assert_eq!(lines.next(), Some("time,open,high,low,close,volume"));
    lines
        .map(|l| {
            let f: Vec<&str> = l.split(',').collect();
            let n = |i: usize| f[i].parse::<f64>().unwrap();
            let time = if daily {
                NaiveDate::parse_from_str(f[0], "%Y-%m-%d").unwrap().and_hms_opt(0, 0, 0).unwrap()
            } else {
                NaiveDateTime::parse_from_str(f[0], "%Y-%m-%dT%H:%M").unwrap()
            };
            Bar { time, open: n(1), high: n(2), low: n(3), close: n(4), volume: n(5) }
        })
        .collect()
}

fn rows(periods: &[Period]) -> Value {
    periods
        .iter()
        .map(|p| json!([p.day.format("%Y-%m-%d").to_string(), p.label, unix(p.start), p.open, p.high, p.low, p.close,
                        p.live]))
        .collect()
}

/// Exact: the same keys, ints as ints, floats with the same bits.
fn differences(e: &Value, a: &Value, path: &str, out: &mut Vec<String>) {
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
                    differences(x, y, &format!("{path}.{k}"), out);
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
            if x.is_f64() != y.is_f64() {
                out.push(format!("{path}: {x} became {y} (int vs float)"));
            } else if x.as_f64().unwrap().to_bits() != y.as_f64().unwrap().to_bits() {
                out.push(format!("{path}: {x} became {y}"));
            }
        }
        _ if e != a => out.push(format!("{path}: {e} became {a}")),
        _ => {}
    }
}

#[test]
fn the_rust_distribution_matches_the_reference_output() {
    let mut cases: Vec<PathBuf> = std::fs::read_dir(golden()).unwrap().map(|e| e.unwrap().path())
        .filter(|p| p.join("expected.json").exists()).collect();
    cases.sort();
    assert!(cases.len() >= 4, "the reference cases are missing");
    let mut failed = Vec::new();
    let mut answers = 0;
    for dir in &cases {
        let expected: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("expected.json")).unwrap()).unwrap();
        let clock = Clock::parse(expected["case"]["clock"].as_str().unwrap()).unwrap();
        let m1 = read_bars(&dir.join("m1.csv.gz"), false);
        let d1 = dir.join("d1.csv.gz").exists().then(|| read_bars(&dir.join("d1.csv.gz"), true));
        let t = tables(&BlockRows::of(&m1, &clock), d1.as_deref());
        let mut problems = Vec::new();
        let actual = json!({"day": rows(&t.day), "week": rows(&t.week), "session": rows(&t.session), "q90": rows(&t.q90)});
        differences(&expected["tables"], &actual, "tables", &mut problems);
        for (i, a) in expected["answers"].as_array().unwrap().iter().enumerate() {
            let q = &a["query"];
            let got = move_distribution(&t, q["period"].as_str().unwrap(), q["measure"].as_str().unwrap(),
                                        q["lookback"].as_str().unwrap(), q["weekday"].as_bool().unwrap(),
                                        q["session"].as_bool().unwrap(), q["bins"].as_i64().unwrap());
            let got = match got {
                Ok(out) => json!({"query": q, "out": out}),
                Err(e) => json!({"query": q, "error": e}),
            };
            differences(a, &got, &format!("answers[{i}] {q}"), &mut problems);
            answers += 1;
        }
        if !problems.is_empty() {
            failed.push(format!("{}:\n  {}", dir.file_name().unwrap().to_string_lossy(), problems.join("\n  ")));
        }
    }
    assert!(failed.is_empty(), "The Rust distribution differs from tests/golden/distribution:\n{}", failed.join("\n"));
    assert!(answers > 200, "only {answers} answers checked");
}
