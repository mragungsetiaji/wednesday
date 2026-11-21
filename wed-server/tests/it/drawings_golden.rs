//! The Rust drawings and drawing alerts against `tests/golden/drawings/golden.json`, which
//! `tests/test_drawings_golden.py` writes from Python's `drawings.py` and `drawing_alerts.py`:
//! the same rows and errors, events, messages, and a full `DrawingAlerts` run on a SQLite store.
//! Values must match exactly: the same keys in the same order, ints as ints, floats to the bit.

use std::path::Path;
use std::sync::{Arc, Mutex};

use chrono::{DateTime, Utc};
use serde_json::{json, Map, Value};
use wednesday_rs::drawings::{bar_events, clean_alert_at, clean_drawing_at, format_message, tick_event, Drawing,
                             DrawingAlerts};
use wednesday_rs::notify::Sender;
use wednesday_rs::scan::Bar;
use wednesday_rs::store::Store;

fn golden() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden/drawings/golden.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// Exact: the same text once written (key order, ints vs floats, the float's digits).
fn same(what: &str, expected: &Value, actual: &Value, problems: &mut Vec<String>) {
    let (e, a) = (serde_json::to_string(expected).unwrap(), serde_json::to_string(actual).unwrap());
    if e != a && problems.len() < 30 {
        problems.push(format!("{what}:\n  expected {e}\n  got      {a}"));
    }
}

fn time(t: f64) -> chrono::NaiveDateTime {
    let secs = t.floor() as i64;
    let nanos = ((t - t.floor()) * 1e9).round() as u32;
    DateTime::from_timestamp(secs, nanos).unwrap().naive_utc()
}

fn bars(rows: &Value) -> Vec<Bar> {
    rows.as_array().unwrap().iter().map(|r| {
        let n = |i: usize| r[i].as_f64().unwrap();
        // [t, high, low, close] or [t, open, high, low, close]
        let r = r.as_array().unwrap();
        if r.len() == 4 {
            Bar { time: time(n(0)), open: n(3), high: n(1), low: n(2), close: n(3), volume: 1.0 }
        } else {
            Bar { time: time(n(0)), open: n(1), high: n(2), low: n(3), close: n(4), volume: 1.0 }
        }
    }).collect()
}

fn drawing(v: &Value) -> Drawing {
    Drawing::from_row(v.as_object().unwrap()).unwrap()
}

#[test]
fn clean_drawing_matches_python() {
    let mut problems = Vec::new();
    let cases = golden()["clean_drawing"].as_array().unwrap().clone();
    for (i, c) in cases.iter().enumerate() {
        let got = match clean_drawing_at(c["id"].as_str().unwrap(), c["body"].as_object().unwrap()) {
            Ok(row) => json!({"out": row}),
            Err(e) => json!({"error": e}),
        };
        let want = if c.get("out").is_some() { json!({"out": c["out"]}) } else { json!({"error": c["error"]}) };
        same(&format!("clean_drawing[{i}] {}", c["body"]), &want, &got, &mut problems);
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

#[test]
fn clean_alert_matches_python() {
    let mut problems = Vec::new();
    for (i, c) in golden()["clean_alert"].as_array().unwrap().iter().enumerate() {
        let got = match clean_alert_at(c["kind"].as_str().unwrap(), c["body"].as_object().unwrap(), c["armed_bar"].as_i64(),
                                       Utc::now()) {
            Ok(row) => json!({"out": row}),
            Err(e) => json!({"error": e}),
        };
        let want = if c.get("out").is_some() { json!({"out": c["out"]}) } else { json!({"error": c["error"]}) };
        same(&format!("clean_alert[{i}] {}", c["body"]), &want, &got, &mut problems);
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

#[test]
fn events_match_python() {
    let g = golden();
    let mut problems = Vec::new();
    for (i, c) in g["bar_events"].as_array().unwrap().iter().enumerate() {
        let got: Vec<Value> = bar_events(&drawing(&c["drawing"]), c["condition"].as_str().unwrap(), &bars(&c["bars"]),
                                         c["prev_close"].as_f64())
            .into_iter()
            .map(|(t, level, what)| json!([t, level, what]))
            .collect();
        same(&format!("bar_events[{i}] {} {}", c["drawing"]["kind"], c["condition"]), &c["out"], &json!(got), &mut problems);
    }
    for (i, c) in g["tick_event"].as_array().unwrap().iter().enumerate() {
        let got = tick_event(&drawing(&c["drawing"]), c["condition"].as_str().unwrap(), c["before"].as_f64().unwrap(),
                             c["now"].as_f64().unwrap(), c["t"].as_i64().unwrap())
            .map(|(level, what)| json!([level, what]))
            .unwrap_or(Value::Null);
        same(&format!("tick_event[{i}]"), &c["out"], &got, &mut problems);
    }
    for (i, c) in g["format_message"].as_array().unwrap().iter().enumerate() {
        let got = format_message(c["symbol"].as_str().unwrap(), &drawing(&c["drawing"]), c["alert"]["note"].as_str(),
                                 c["alert"]["mode"].as_str().unwrap(), c["level"].as_f64().unwrap(),
                                 c["what"].as_str().unwrap(), c["price"].as_f64().unwrap(), c["bar_time"].as_str(),
                                 c["intrabar"].as_bool().unwrap());
        same(&format!("format_message[{i}]"), &c["out"], &json!(got), &mut problems);
    }
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}

fn marked(mut row: Map<String, Value>, key: &str) -> Value {
    if row.get(key).is_some_and(|v| !v.is_null()) {
        row.insert(key.into(), json!("<now>"));
    }
    Value::Object(row)
}

#[tokio::test]
async fn the_checker_runs_as_python() {
    let run = golden()["run"].clone();
    let dir = std::env::temp_dir().join(format!("wed-drawings-golden-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let store = Store::open(&format!("sqlite:///{}", dir.join("run.db").display()), &dir).await.unwrap();
    for d in run["drawings"].as_array().unwrap() {
        store.drawing_put(d.as_object().unwrap()).await.unwrap();
    }
    for a in run["alerts"].as_array().unwrap() {
        store.drawing_alert_put(a.as_object().unwrap()).await.unwrap();
    }
    let m1 = bars(&run["bars"]);
    let checker = DrawingAlerts::new();
    let mut problems = Vec::new();
    for (i, (step, want)) in run["steps"].as_array().unwrap().iter().zip(run["results"].as_array().unwrap()).enumerate() {
        let sent = Arc::new(Mutex::new(Vec::<String>::new()));
        let fail = step.get("fail").and_then(Value::as_str).map(String::from);
        let sink = sent.clone();
        let sender: Sender = Arc::new(move |text: String| {
            let (sink, fail) = (sink.clone(), fail.clone());
            Box::pin(async move {
                match fail {
                    Some(e) => Err(e),
                    None => {
                        sink.lock().unwrap().push(text);
                        Ok(())
                    }
                }
            })
        });
        let send = (step.get("send") != Some(&json!(false))).then_some(&sender);
        let keys = if step.get("invalidate").is_some() {
            checker.invalidate();
            vec![]
        } else if let Some(n) = step.get("check").and_then(Value::as_u64) {
            checker.check(&store, "synthetic", "XAUUSD", &m1[..n as usize], send).await
        } else {
            let price = step["tick"].as_f64().unwrap_or(f64::NAN);
            checker.check_tick(&store, "synthetic", "XAUUSD", price, time(step["at"].as_f64().unwrap()), send).await
        };
        let got = json!({"keys": keys, "sent": *sent.lock().unwrap(), "last_error": checker.last_error()});
        same(&format!("step {i} {step}"), want, &got, &mut problems);
    }
    let mut alerts = store.drawing_alerts("synthetic", "XAUUSD").await.unwrap();
    alerts.extend(store.drawing_alerts("synthetic", "OTHER").await.unwrap());
    alerts.sort_by_key(|a| a["drawing_id"].as_str().unwrap().to_string());
    let alerts: Vec<Value> = alerts.into_iter().map(|a| marked(a, "fired_at")).collect();
    same("final alerts", &run["final_alerts"], &json!(alerts), &mut problems);
    let mut log = store.recent_alerts(1000).await.unwrap();
    log.sort_by_key(|r| r["key"].as_str().unwrap().to_string());
    let log: Vec<Value> = log.into_iter().map(|r| marked(r, "sent_at")).collect();
    same("alert log", &run["log"], &json!(log), &mut problems);
    let _ = std::fs::remove_dir_all(&dir);
    assert!(problems.is_empty(), "{}", problems.join("\n"));
}
