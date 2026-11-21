//! The Rust calendar against the reference output in `tests/golden/calendar/` (#96, step 9f): the
//! same input as `tests/test_calendar_golden.py` gives Python's `news.py` and `news_stats.py` (the
//! weekly feed parsed, history rows, upcoming events, figures, the past reactions on the UTC and
//! NY+7 clocks, CSV imports and release times), the same output. Exact: key order, ints as ints,
//! floats with the same bits.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, Duration, Utc};
use serde_json::{json, Map, Value};
use wednesday_rs::calendar::events::{aware_time, CalRow};
use wednesday_rs::calendar::reactions::{number, reaction, surprise, NewsReactions};
use wednesday_rs::calendar::{history_summary, matching_of, parse_csv, parse_events, store_rows, upcoming_of,
                             CalendarSettings, ImportError, Market};
use wednesday_rs::scan::{Bar, Clock};
use wednesday_rs::store::Store;

fn golden() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden/calendar")
}

fn read_json(path: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn read_bars(path: &Path) -> Vec<Bar> {
    let mut text = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(path).unwrap()).read_to_string(&mut text).unwrap();
    let rows: Vec<Vec<f64>> = serde_json::from_str(&text).unwrap();
    rows.iter()
        .map(|r| Bar { time: DateTime::from_timestamp(r[0] as i64, 0).unwrap().naive_utc(), open: r[1], high: r[2],
                       low: r[3], close: r[4], volume: r[5] })
        .collect()
}

/// Exact: the same keys in the same order, ints as ints, floats with the same bits.
fn differences(e: &Value, a: &Value, path: &str, out: &mut Vec<String>) {
    if out.len() >= 30 {
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

fn rows_json(rows: &[CalRow]) -> Value {
    rows.iter().map(CalRow::to_json).collect()
}

fn import_json(data: &[u8], zone: &str) -> Value {
    match parse_csv(data, zone) {
        Ok(rows) => json!({"rows": rows_json(&rows)}),
        Err(ImportError::Invalid(m)) => json!({"error": m}),
        Err(ImportError::Internal(m)) => json!({"crash": m.split(':').next().unwrap()}),
    }
}

/// `csv.writer` of one row: fields with a comma, quote or line break quoted.
fn csv_line(fields: &[&str]) -> String {
    let quoted: Vec<String> = fields
        .iter()
        .map(|f| {
            if f.contains([',', '"', '\n', '\r']) {
                format!("\"{}\"", f.replace('"', "\"\""))
            } else {
                f.to_string()
            }
        })
        .collect();
    quoted.join(",") + "\n"
}

fn time(v: &Value) -> DateTime<Utc> {
    aware_time(Some(v)).unwrap()
}

#[tokio::test]
async fn the_calendar_matches_pythons() {
    let inp = read_json(&golden().join("input.json"));
    let want = read_json(&golden().join("output.json"));
    let mut got = Map::new();

    let events = parse_events(inp["feed"].as_array().unwrap());
    got.insert("parse".into(), json!(events));
    let mut rows = store_rows(&events, "feed");
    rows.extend(store_rows(inp["odd_events"].as_array().unwrap(), "csv"));
    got.insert("store_rows".into(), rows_json(&rows));

    let upcoming: Vec<Value> = inp["upcoming"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let s = CalendarSettings::from_dict(c["settings"].as_object().unwrap()).unwrap();
            json!({"upcoming": upcoming_of(&events, &s, time(&c["now"])), "matching": matching_of(&events, &s)})
        })
        .collect();
    got.insert("upcoming".into(), json!(upcoming));
    got.insert("numbers".into(),
               inp["numbers"].as_array().unwrap().iter().map(|x| json!(number(x.as_str()))).collect());
    got.insert("surprises".into(),
               inp["surprises"].as_array().unwrap().iter().map(|p| json!(surprise(p[0].as_str(), p[1].as_str()))).collect());

    let store = Store::open("sqlite://", Path::new(".")).await.unwrap();
    let rows = store_rows(inp["calendar"].as_array().unwrap(), "csv");
    store.calendar_put(&rows).await.unwrap();
    got.insert("history".into(), rows_json(&store.calendar_history(&[], &[], None).await.unwrap()));
    got.insert("history_summary".into(), history_summary(&store).await.unwrap());

    let bars = |key: &str| Arc::new(read_bars(&golden().join(format!("m1-{key}.json.gz"))));
    let (utc, ny7) = (bars("utc"), bars("ny7"));
    let mut singles = Vec::new();
    for (clock, m1) in [("UTC", &utc), ("NY+7", &ny7)] {
        let c = Clock::parse(clock).unwrap();
        for r in &rows {
            let t0 = c.utc_to_feed(DateTime::from_timestamp(r.time, 0).unwrap());
            let window: Vec<Bar> = m1.iter().filter(|b| b.time >= t0 - Duration::minutes(240) && b.time <= t0 + Duration::minutes(61))
                .copied().collect();
            singles.push(json!({"clock": clock, "time": r.time, "title": r.title,
                                "reaction": reaction(&window, t0).map(|r| r.to_json())}));
        }
    }
    got.insert("reactions".into(), json!(singles));

    let now = time(&inp["now"]).timestamp() as f64;
    let mut stats = Vec::new();
    for case in inp["reactions"].as_array().unwrap() {
        let key = case["bars"].as_str().unwrap();
        let market = Market { clock: case["clock"].as_str().unwrap().into(), source: "golden".into(), symbol: key.into(),
                              m1: if key == "utc" { utc.clone() } else { ny7.clone() }, persist: false, store: None };
        let list = |k: &str| -> Vec<String> {
            case[k].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect()
        };
        let out = NewsReactions::default()
            .stats(&store, |a, b| market.bars(a, b), &market.clock, &list("currencies"), &list("impacts"),
                   ("golden".into(), key.into()), now)
            .await
            .unwrap();
        stats.push(Value::Object(out));
    }
    got.insert("stats".into(), json!(stats));

    let imports: Vec<Value> = inp["imports"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let data = std::fs::read(golden().join("import").join(c["file"].as_str().unwrap())).unwrap();
            import_json(&data, c["zone"].as_str().unwrap())
        })
        .collect();
    got.insert("imports".into(), json!(imports));

    let stamps: Vec<Value> = inp["stamps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| {
            let s = s.as_str().unwrap();
            let file = csv_line(&["datetime", "currency", "title"]) + &csv_line(&[s, "USD", "X"]);
            let mut out = json!({"stamp": s});
            let got = import_json(file.as_bytes(), "UTC");
            let m = out.as_object_mut().unwrap();
            match got.get("rows") {
                Some(rows) => {
                    m.insert("time".into(), rows[0]["time"].clone());
                }
                None => m.extend(got.as_object().unwrap().clone()),
            }
            out
        })
        .collect();
    got.insert("stamps".into(), json!(stamps));

    let got = Value::Object(got);
    let mut problems = Vec::new();
    differences(&want, &got, "", &mut problems);
    assert!(problems.is_empty(), "The Rust calendar differs from Python's:\n  {}", problems.join("\n  "));
}
