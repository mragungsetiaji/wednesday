//! The Rust order block alerts against the reference output in `tests/golden/alerts/` (#96, step
//! 9e): the same order blocks, settings and M1 bars in, the same texts, keys and checks out. The
//! Python side is `tests/test_alerts_golden.py`, which writes the files.

use crate::fake_telegram;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::NaiveDateTime;
use serde_json::Value;
use wednesday_rs::alerts::{bias_text, format_alert, ob_key, AlertSettings, Alerts, Keyring, Secrets};
use wednesday_rs::scan::scanner::{LevelSet, TimeframeResult};
use wednesday_rs::scan::{Bar, Detector, Level, ScanResult, TIMEFRAMES};

fn golden(name: &str) -> Value {
    let p: PathBuf = Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden/alerts").join(name);
    serde_json::from_str(&std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()))).unwrap()
}

fn time(v: &Value) -> NaiveDateTime {
    NaiveDateTime::parse_from_str(v.as_str().unwrap(), "%Y-%m-%dT%H:%M:%S").unwrap()
}

fn level(v: &Value) -> Level {
    let kind: &'static str = if v["kind"] == "bullish" { "bullish" } else { "bearish" };
    let mut lv = Level::new("ob", kind, v["top"].as_f64().unwrap(), v["bottom"].as_f64().unwrap(), time(&v["time"]),
                            time(&v["confirmed_time"]), "");
    lv.touches = v["touches"].as_u64().unwrap() as u32;
    lv.meta = v["meta"].as_object().unwrap().clone();
    lv
}

#[test]
fn alert_texts_match_python() {
    let cases = golden("format.json");
    for (i, c) in cases.as_array().unwrap().iter().enumerate() {
        let bias = bias_text(Some(&c["bias"]));
        let got = format_alert(c["symbol"].as_str().unwrap(), c["tf"].as_str().unwrap(), &level(&c["ob"]),
                               c["price"].as_f64().unwrap(), bias.as_deref(), c["digits"].as_u64().unwrap() as usize,
                               c["trade_bias"].as_str());
        assert_eq!(got, c["text"].as_str().unwrap(), "format case {i}");
    }
}

#[test]
fn keys_match_python() {
    for c in golden("keys.json").as_array().unwrap() {
        let got = ob_key(c["source"].as_str().unwrap(), c["symbol"].as_str().unwrap(), c["tf"].as_str().unwrap(), &level(&c["ob"]));
        assert_eq!(got, c["key"].as_str().unwrap());
    }
}

#[test]
fn settings_match_python() {
    for c in golden("settings.json").as_array().unwrap() {
        let got = AlertSettings::from_json(Some(&c["body"]));
        if c.get("error").is_some() {
            assert!(got.is_err(), "{}: wanted an error", c["body"]);
            continue;
        }
        let s = got.unwrap_or_else(|e| panic!("{}: {e}", c["body"]));
        assert_eq!(serde_json::to_string(&s.to_json()).unwrap(), serde_json::to_string(&c["settings"]).unwrap(), "{}", c["body"]);
        let errors: Vec<Value> = s.validate().into_iter().map(Value::from).collect();
        assert_eq!(Value::Array(errors), c["errors"], "{}", c["body"]);
    }
}

struct NoKeyring;

impl Keyring for NoKeyring {
    fn get(&self, _: &str, _: &str) -> Result<Option<String>, String> {
        Err("none".into())
    }
    fn set(&self, _: &str, _: &str, _: &str) -> Result<(), String> {
        Err("none".into())
    }
    fn delete(&self, _: &str, _: &str) -> Result<(), String> {
        Err("none".into())
    }
}

fn bars(v: &Value) -> Vec<Bar> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let n = |i: usize| r[i].as_f64().unwrap();
            Bar { time: time(&r[0]), open: n(1), high: n(2), low: n(3), close: n(4), volume: n(5) }
        })
        .collect()
}

#[tokio::test]
async fn checks_match_python() {
    for case in golden("check.json").as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let fake = fake_telegram::start().await;
        let env: wednesday_rs::alerts::Env = Arc::new(|k: &str| (k == "TELEGRAM_CHAT_ID").then(|| "42".to_string()));
        let secrets = Secrets::with(Box::new(NoKeyring), |k| (k == "TELEGRAM_BOT_TOKEN").then(|| "t".to_string()));
        let settings = AlertSettings::from_json(Some(&case["settings"])).unwrap();
        let alerts = Alerts::new(None, secrets, env, fake.url.clone(), settings);
        let results = case["timeframes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| {
                let tf = *TIMEFRAMES.iter().find(|x| x.name == t["tf"].as_str().unwrap()).unwrap();
                let active: Vec<Level> = t["obs"].as_array().unwrap().iter().map(level).collect();
                TimeframeResult {
                    timeframe: tf,
                    candles: 100,
                    sets: vec![(Detector::OrderBlock, LevelSet { active, above: None, below: None, inside: vec![], recent: vec![] })],
                    bias: (!t["bias"].is_null()).then(|| t["bias"].clone()),
                    swings: vec![],
                }
            })
            .collect();
        let m1 = bars(&case["m1"]);
        let result = ScanResult { time: m1[0].time, price: case["price"].as_f64().unwrap(), detectors: vec![Detector::OrderBlock],
                                  results };
        let (source, symbol) = (case["source"].as_str().unwrap(), case["symbol"].as_str().unwrap());
        for (i, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let n = step["bars"].as_u64().unwrap() as usize;
            let before = fake.sent().len();
            let keys = alerts.check(source, symbol, &result, &m1[..n], step["trade_bias"].as_str()).await;
            let want: Vec<String> = step["keys"].as_array().unwrap().iter().map(|k| k.as_str().unwrap().to_string()).collect();
            assert_eq!(keys, want, "{name} step {i}: keys");
            let msgs: Vec<String> = fake.sent()[before..].to_vec();
            let want: Vec<String> = step["messages"].as_array().unwrap().iter().map(|k| k.as_str().unwrap().to_string()).collect();
            assert_eq!(msgs, want, "{name} step {i}: messages");
        }
        for r in fake.requests.lock().unwrap().iter() {
            assert_eq!(r.path, "/bott/sendMessage");
            assert_eq!(r.field("chat_id"), Some(if name == "filters" { "7" } else { "42" }));
            assert_eq!(r.field("parse_mode"), Some("HTML"));
            assert_eq!(r.field("disable_web_page_preview"), Some("true"));
        }
    }
}
