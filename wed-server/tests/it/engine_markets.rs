//! Other-symbol panes (`engine/markets.rs`, `GET /api/markets/candles` and `/tick`) against a fake
//! helper: `tests/golden/markets/fake_feed.mjs`, the one Python's `tests/test_markets_parity.py`
//! runs. Every case in `tests/golden/markets/cases.json` must answer as Python did
//! (`answers.json`). Skipped, with a message, when `node` isn't on PATH.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use wednesday_rs::engine::markets::{self, Markets};
use wednesday_rs::engine::{Answer, DataSettings, Options, Runtime};

const NOW: i64 = 1763632800; // the fake helper's clock: 2025-11-20 10:00 UTC

fn golden(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("tests").join("golden").join("markets").join(name)
}

fn which_node() -> Option<PathBuf> {
    let name = if cfg!(windows) { "node.exe" } else { "node" };
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join(name)).find(|p| p.is_file())
}

fn node_or_skip(test: &str) -> Option<PathBuf> {
    let node = which_node();
    if node.is_none() {
        eprintln!("skipping {test}: node isn't on PATH");
    }
    node
}

fn fake(node: &Path) -> Markets {
    let (node, script) = (node.to_path_buf(), golden("fake_feed.mjs"));
    Markets::with_command(move || Ok((node.clone(), script.clone())))
}

fn runtime(clock: &str) -> Arc<Runtime> {
    let settings = DataSettings { source: "synthetic".into(), clock: Some(clock.into()), ..Default::default() };
    Runtime::start(settings, Options { poll: false, delay: 0.0, ..Default::default() }, None).unwrap()
}

fn query(q: Value) -> HashMap<String, String> {
    q.as_object().unwrap().iter().map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string())).collect()
}

/// What the route answers: status and body, a refusal as FastAPI's `{"detail": ...}`.
fn answered(a: Answer) -> (u16, Value) {
    match a {
        Ok(body) => (200, body),
        Err(r) => (r.status, json!({"detail": r.detail})),
    }
}

async fn get(m: &Markets, rt: &Arc<Runtime>, route: &str, q: Value) -> (u16, Value) {
    let q = query(q);
    answered(match route {
        "candles" => markets::candles(m, rt, &q).await,
        "tick" => markets::tick(m, rt, &q).await,
        _ => unreachable!(),
    })
}

#[tokio::test(flavor = "multi_thread")]
async fn answers_match_python() {
    let Some(node) = node_or_skip("markets parity") else { return };
    let cases: Value = serde_json::from_str(&std::fs::read_to_string(golden("cases.json")).unwrap()).unwrap();
    let expected: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(golden("answers.json")).unwrap()).unwrap();
    let mut want = expected.iter();
    let mut checked = 0;
    for clock in cases["clocks"].as_array().unwrap() {
        let clock = clock.as_str().unwrap();
        let (m, rt) = (fake(&node), runtime(clock));
        for route in ["candles", "tick"] {
            for q in cases[route].as_array().unwrap() {
                let exp = want.next().expect("answers.json is shorter than cases.json: rerun the Python test");
                assert_eq!((exp["clock"].as_str(), exp["route"].as_str(), &exp["query"]), (Some(clock), Some(route), q));
                let (status, body) = get(&m, &rt, route, q.clone()).await;
                assert_eq!(status as u64, exp["status"].as_u64().unwrap(), "{clock} {route} {q}: {body}");
                assert_eq!(body, exp["body"], "{clock} {route} {q}");
                checked += 1;
            }
        }
        m.close().await;
        rt.engine().stop();
    }
    assert!(want.next().is_none(), "answers.json has more answers than cases.json asks for");
    assert!(checked > 300);
}

#[tokio::test(flavor = "multi_thread")]
async fn candles_and_ticks_on_the_feed_clock() {
    let Some(node) = node_or_skip("markets clock test") else { return };
    let m = fake(&node);
    // NOW + 5 s in November: New York is UTC-5, so NY+7 is UTC+2; Tokyo is UTC+9.
    for (clock, shift) in [("UTC", 0), ("NY+7", 7200), ("Asia/Tokyo", 9 * 3600), ("UTC-3", -3 * 3600)] {
        let rt = runtime(clock);
        let (status, body) = get(&m, &rt, "tick", json!({"symbol": "fx:xagusd"})).await;
        assert_eq!((status, &body), (200, &json!({"symbol": "FX:XAGUSD", "price": 31.987, "time": NOW + 5 + shift})));

        let (status, body) = get(&m, &rt, "candles", json!({"symbol": "FX:XAGUSD", "tf": "15m", "limit": "10"})).await;
        assert_eq!(status, 200);
        assert_eq!((&body["symbol"], &body["timeframe"], &body["precision"]), (&json!("FX:XAGUSD"), &json!("15M"), &json!(3)));
        let c = body["candles"].as_array().unwrap();
        assert_eq!(c.len(), 10);
        assert_eq!(c[9]["time"], NOW + shift);
        assert_eq!(c[0]["time"], NOW + shift - 9 * 900);
        assert_eq!(c[9]["close"], body["price"]);
        assert_eq!((&body["has_more"], &body["levels"], &body["swings"]), (&json!(true), &json!([]), &json!([])));

        // Paging back: only candles opening before `before` (feed clock).
        let before = NOW + shift - 3600;
        let (_, older) = get(&m, &rt, "candles", json!({"symbol": "FX:XAGUSD", "tf": "15M", "limit": "10",
                                                         "before": before.to_string()})).await;
        let c = older["candles"].as_array().unwrap();
        assert!(!c.is_empty() && c.iter().all(|r| r["time"].as_i64().unwrap() < before));
        assert_eq!(c.last().unwrap()["time"], before - 900);
        rt.engine().stop();
    }

    // A quote without a time traded now.
    let rt = runtime("Asia/Tokyo");
    let (_, body) = get(&m, &rt, "tick", json!({"symbol": "FX:NOTIME"})).await;
    let now = chrono::Utc::now().timestamp() + 9 * 3600;
    assert_eq!(body["price"], 31.5);
    assert!((body["time"].as_i64().unwrap() - now).abs() <= 2, "{body}");
    m.close().await;
}

#[tokio::test(flavor = "multi_thread")]
async fn requests_are_shared_for_a_moment() {
    let Some(node) = node_or_skip("markets cache test") else { return };
    let (m, rt) = (fake(&node), runtime("UTC"));
    let tick = || get(&m, &rt, "tick", json!({"symbol": "FX:COUNT"}));
    let candles = |limit: &str| get(&m, &rt, "candles", json!({"symbol": "FX:COUNT", "limit": limit}));
    let open = |b: &Value| b["candles"][0]["open"].clone();

    // The fake counts its calls: the count is the price or the open.
    assert_eq!(tick().await.1["price"], 1.0);
    assert_eq!(tick().await.1["price"], 1.0); // within TICK_SECONDS: the same quote
    assert_eq!(open(&candles("10").await.1), 1);
    assert_eq!(open(&candles("10").await.1), 1); // within CACHE_SECONDS: the same request
    assert_eq!(open(&candles("11").await.1), 2); // another limit is another request
    tokio::time::sleep(Duration::from_secs_f64(markets::TICK_SECONDS + 0.1)).await;
    assert_eq!(tick().await.1["price"], 2.0);
    tokio::time::sleep(Duration::from_secs_f64(markets::CACHE_SECONDS)).await;
    assert_eq!(open(&candles("10").await.1), 3);

    // Failures aren't kept.
    let (status, _) = get(&m, &rt, "tick", json!({"symbol": "FX:NOPE"})).await;
    assert_eq!(status, 404);
    m.close().await;
    rt.engine().stop();
}

#[tokio::test(flavor = "multi_thread")]
async fn helper_failures_and_their_statuses() {
    let Some(node) = node_or_skip("markets error test") else { return };
    let (m, rt) = (fake(&node), runtime("UTC"));
    let (status, body) = get(&m, &rt, "candles", json!({"symbol": "FX:NOPE"})).await;
    assert_eq!((status, body["detail"].as_str().unwrap()), (404, "TradingView has no symbol FX:NOPE. Use the exchange \
               prefix, e.g. FX:XAGUSD (FXCM silver) or OANDA:XAUUSD."));
    assert_eq!(get(&m, &rt, "tick", json!({"symbol": "FX:FAIL"})).await, (502, json!({"detail": "TradingView: boom"})));
    assert_eq!(get(&m, &rt, "candles", json!({"symbol": "xagusd"})).await,
               (400, json!({"detail": "'xagusd' isn't a TradingView symbol. Use the exchange prefix, e.g. FX:XAGUSD."})));

    // The helper dies: a 503 saying why, and the next request starts another.
    assert_eq!(get(&m, &rt, "candles", json!({"symbol": "FX:DIE"})).await,
               (503, json!({"detail": "The TradingView helper stopped: boom: fake failure"})));
    assert_eq!(get(&m, &rt, "tick", json!({"symbol": "FX:XAGUSD"})).await.0, 200);
    m.close().await;

    // No helper to run.
    let none = Markets::with_command(|| Err("Needs Node 20+ (nodejs.org), then: npm install --prefix tradingview".into()));
    assert_eq!(get(&none, &rt, "tick", json!({"symbol": "FX:XAGUSD"})).await,
               (503, json!({"detail": "Other symbols come from TradingView. Needs Node 20+ (nodejs.org), then: npm \
                                       install --prefix tradingview"})));
    // Bad requests are refused before the helper is looked for.
    assert_eq!(get(&none, &rt, "tick", json!({})).await.0, 422);
    assert_eq!(get(&none, &rt, "tick", json!({"symbol": "nope"})).await.0, 400);
    rt.engine().stop();
}

/// `Markets::new()` finds the helper as the feed does: `WEDNESDAY_TRADINGVIEW_DIR` and
/// `WEDNESDAY_NODE` (the only test here that sets them).
#[tokio::test(flavor = "multi_thread")]
async fn the_default_helper_comes_from_the_environment() {
    let Some(node) = node_or_skip("markets helper lookup test") else { return };
    let dir = std::env::temp_dir().join(format!("wednesday-markets-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::copy(golden("fake_feed.mjs"), dir.join("feed.mjs")).unwrap();
    let _env = crate::env_lock();
    std::env::set_var("WEDNESDAY_TRADINGVIEW_DIR", &dir);
    std::env::set_var("WEDNESDAY_NODE", &node);
    let rt = runtime("UTC");

    let m = Markets::new();
    assert_eq!(get(&m, &rt, "tick", json!({"symbol": "FX:XAGUSD"})).await,
               (503, json!({"detail": "Other symbols come from TradingView. Install it with: npm install --prefix \
                                       tradingview (or make install)"})));
    std::fs::create_dir_all(dir.join("node_modules").join("@mathieuc").join("tradingview")).unwrap();
    let m = Markets::default();
    assert_eq!(get(&m, &rt, "tick", json!({"symbol": "FX:XAGUSD"})).await,
               (200, json!({"symbol": "FX:XAGUSD", "price": 31.987, "time": NOW + 5})));
    m.close().await;
    rt.engine().stop();
    let _ = std::fs::remove_dir_all(&dir);
}

/// Real TradingView: `cargo test --test it engine_markets -- --ignored` (needs the network and
/// `npm install --prefix tradingview`).
#[tokio::test(flavor = "multi_thread")]
#[ignore]
async fn live_silver_from_tradingview() {
    let (m, rt) = (Markets::new(), runtime("NY+7"));
    let (status, body) = get(&m, &rt, "candles", json!({"symbol": "FX:XAGUSD", "tf": "1H", "limit": "50"})).await;
    assert_eq!(status, 200, "{body}");
    assert_eq!(body["precision"], 3);
    let c = body["candles"].as_array().unwrap();
    assert_eq!(c.len(), 50);
    assert!(c.windows(2).all(|w| w[0]["time"].as_i64() < w[1]["time"].as_i64()));
    let first = c[0]["time"].as_i64().unwrap();
    let (status, older) = get(&m, &rt, "candles", json!({"symbol": "FX:XAGUSD", "tf": "1H", "limit": "50",
                                                         "before": first.to_string()})).await;
    assert_eq!(status, 200, "{older}");
    assert!(older["candles"].as_array().unwrap().iter().all(|r| r["time"].as_i64().unwrap() < first));

    let (status, tick) = get(&m, &rt, "tick", json!({"symbol": "FX:XAGUSD"})).await;
    assert_eq!(status, 200, "{tick}");
    assert!(tick["price"].as_f64().is_some_and(|p| p > 0.0), "{tick}");
    // As in Python: candles know an unknown symbol (404), the quote only fails (502).
    let (status, _) = get(&m, &rt, "candles", json!({"symbol": "FX:NO_SUCH_SYMBOL_X"})).await;
    assert_eq!(status, 404);
    let (status, body) = get(&m, &rt, "tick", json!({"symbol": "FX:NO_SUCH_SYMBOL_X"})).await;
    assert_eq!(status, 502, "{body}");
    m.close().await;
    rt.engine().stop();
}
