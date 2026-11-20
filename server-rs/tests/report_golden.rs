//! The Rust MT5 report import against the reference output in `tests/golden/reports/` (#96): the
//! same file in, the same account, trades and cash out, or the same message for the trader. The
//! Python side is `tests/test_report_golden.py`, which writes the files. The comparison is exact:
//! key order, ints vs floats, and every float's bits (`-0.0` included).

use std::path::{Path, PathBuf};

use serde_json::Value;
use wednesday_rs::journal::report::{parse_report, UNREADABLE};

fn golden() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden/reports")
}

/// Where two JSON values differ, compared strictly; `None` if they're the same.
fn diff(path: &str, want: &Value, got: &Value) -> Option<String> {
    match (want, got) {
        (Value::Object(a), Value::Object(b)) => {
            let (ka, kb): (Vec<_>, Vec<_>) = (a.keys().collect(), b.keys().collect());
            if ka != kb {
                return Some(format!("{path}: keys {ka:?} vs {kb:?}"));
            }
            a.iter().find_map(|(k, v)| diff(&format!("{path}.{k}"), v, &b[k]))
        }
        (Value::Array(a), Value::Array(b)) => {
            if a.len() != b.len() {
                return Some(format!("{path}: {} items vs {}", a.len(), b.len()));
            }
            a.iter().zip(b).enumerate().find_map(|(i, (x, y))| diff(&format!("{path}[{i}]"), x, y))
        }
        (Value::Number(a), Value::Number(b)) => {
            let same = if a.is_f64() || b.is_f64() {
                a.is_f64() && b.is_f64() && a.as_f64().map(f64::to_bits) == b.as_f64().map(f64::to_bits)
            } else {
                a == b
            };
            (!same).then(|| format!("{path}: {a} vs {b}"))
        }
        _ => (want != got).then(|| format!("{path}: {want} vs {got}")),
    }
}

#[test]
fn reports_match_python() {
    let mut cases: Vec<PathBuf> = std::fs::read_dir(golden()).unwrap().map(|e| e.unwrap().path()).filter(|p| p.is_dir()).collect();
    cases.sort();
    assert!(cases.len() >= 20, "no golden reports in {}", golden().display());
    let mut failures = Vec::new();
    for dir in &cases {
        let name = dir.file_name().unwrap().to_string_lossy().to_string();
        let data = std::fs::read(dir.join("report.html")).unwrap();
        let expected: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("expected.json")).unwrap()).unwrap();
        let got = parse_report(&data);
        let problem = match (&got, expected.as_object().and_then(|o| o.iter().next())) {
            (Ok(v), Some((k, want))) if k == "ok" => diff("", want, v),
            (Err(m), Some((k, want))) if k == "error" => (want.as_str() != Some(m)).then(|| format!("message {m:?} vs {want}")),
            (Err(m), Some((k, _))) if k == "exception" => (m != UNREADABLE).then(|| format!("message {m:?}")),
            _ => Some(format!("{got:?} vs {expected}")),
        };
        if let Some(p) = problem {
            failures.push(format!("{name}: {p}"));
        }
    }
    assert!(failures.is_empty(), "{} of {} reports differ:\n{}", failures.len(), cases.len(), failures.join("\n"));
}
