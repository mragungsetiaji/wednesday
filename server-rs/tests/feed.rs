//! The TradingView feed against a fake helper: a tiny Node script answering feed.mjs's JSON-line
//! protocol. Skipped, with a message, when `node` isn't on PATH.

use std::path::{Path, PathBuf};

use chrono::{DurationRound, TimeDelta, Utc};
use serde_json::{json, Map};
use wednesday_rs::engine::feed::{tradingview_command, Feed, NodeHelper, TradingViewFeed};

/// Answers one request at a time, in order, as feed.mjs does.
const FAKE: &str = r#"
import { createInterface } from 'node:readline';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reply = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');
const ops = {
  async info({ symbol }) {
    if (symbol === 'BAD:X') throw Object.assign(new Error('invalid symbol'), { code: 'SYMBOL_ERROR' });
    return { description: 'Gold Spot', exchange: 'FXCM' };
  },
  async candles({ timeframe, count, timeoutMs }) {
    const now = Math.floor(Date.now() / 1000);
    const step = timeframe === 'D' ? 86400 : 60;
    const last = now - (now % 60);
    const rows = [];
    for (let k = count - 1; k >= 0; k--) rows.push([last - k * step, 1, 2, 0.5, 1.5, timeoutMs]);
    rows.reverse(); // newest first: the client sorts
    rows.push([last - step, 1, 2, 0.5, 999]); // a repeat (without volume): the last one wins
    return rows;
  },
  async quote() { return { price: 2650, time: 1763632800 }; },
  async slow({ ms }) { await sleep(ms); return 'late'; },
  async die() { console.error('first line'); console.error('boom: fake failure'); await sleep(50); process.exit(3); },
};
let queue = Promise.resolve();
createInterface({ input: process.stdin }).on('line', (line) => {
  queue = queue.then(async () => {
    const req = JSON.parse(line);
    try { reply({ id: req.id, result: await ops[req.op](req) }); }
    catch (e) { reply({ id: req.id, error: { code: e.code ?? 'ERROR', message: e.message } }); }
  });
}).on('close', async () => { await queue; process.exit(0); });
console.log('not json: logged and skipped');
"#;

fn which_node() -> Option<PathBuf> {
    let name = if cfg!(windows) { "node.exe" } else { "node" };
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join(name)).find(|p| p.is_file())
}

/// A helper folder with the fake feed.mjs (and an empty `@mathieuc/tradingview`, which
/// `tradingview_command` checks for).
fn fake_dir(name: &str, with_modules: bool) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("wednesday-feed-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("feed.mjs"), FAKE).unwrap();
    if with_modules {
        std::fs::create_dir_all(dir.join("node_modules").join("@mathieuc").join("tradingview")).unwrap();
    }
    dir
}

fn node_or_skip(test: &str) -> Option<PathBuf> {
    let node = which_node();
    if node.is_none() {
        eprintln!("skipping {test}: node isn't on PATH");
    }
    node
}

fn args(pairs: serde_json::Value) -> Map<String, serde_json::Value> {
    pairs.as_object().cloned().unwrap_or_default()
}

#[tokio::test]
async fn helper_answers_skips_late_replies_times_out_and_restarts() {
    let Some(node) = node_or_skip("helper test") else { return };
    let dir = fake_dir("helper", true);
    let mut helper = NodeHelper::new(node, dir.join("feed.mjs"));

    let info = helper.call("info", 10.0, args(json!({"symbol": "FX:XAUUSD"}))).await.unwrap();
    assert_eq!(info["exchange"], "FXCM");

    // A slow answer times out with Python's message; its late reply is skipped by the next call.
    let err = helper.call("slow", 0.5, args(json!({"ms": 1000}))).await.unwrap_err();
    assert_eq!(err.to_string(), "RuntimeError: TradingView didn't answer within 0 s. Check the internet connection.");
    let err = helper.call("slow", 1.4, args(json!({"ms": 1500}))).await.unwrap_err();
    assert_eq!(err.message, "TradingView didn't answer within 1 s. Check the internet connection.");
    let quote = helper.call("quote", 10.0, Map::new()).await.unwrap();
    assert_eq!(quote["price"], 2650);

    // Errors the helper reports keep their code.
    let err = helper.call("info", 10.0, args(json!({"symbol": "BAD:X"}))).await.unwrap_err();
    assert_eq!((err.kind, err.code.as_deref()), ("TradingViewError", Some("SYMBOL_ERROR")));
    assert_eq!(err.message, "TradingView: invalid symbol");

    // The helper dies: its last stderr line says why, and the next call starts a new one.
    let err = helper.call("die", 10.0, Map::new()).await.unwrap_err();
    assert_eq!(err.to_string(), "RuntimeError: The TradingView helper stopped: boom: fake failure");
    let quote = helper.call("quote", 10.0, Map::new()).await.unwrap();
    assert_eq!(quote["time"], 1763632800);

    helper.close().await;
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn helper_that_cannot_run_says_so() {
    let Some(node) = node_or_skip("helper exit test") else { return };
    let dir = std::env::temp_dir().join(format!("wednesday-feed-broken-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(dir.join("feed.mjs"), "process.exit(1);\n").unwrap();
    let mut helper = NodeHelper::new(node, dir.join("feed.mjs"));
    let err = helper.call("info", 10.0, Map::new()).await.unwrap_err();
    assert_eq!(err.message, "The TradingView helper stopped: no output. Is Node 20+ installed?");
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn tradingview_feed_over_the_fake_helper() {
    let Some(node) = node_or_skip("feed test") else { return };
    let dir = fake_dir("feed", true);

    let mut bad = TradingViewFeed::with_command("BAD:X", node.clone(), dir.join("feed.mjs"));
    let err = bad.connect().await.unwrap_err();
    assert_eq!(
        err.to_string(),
        "RuntimeError: TradingView has no symbol 'BAD:X'. Use the exchange prefix, e.g. FX:XAUUSD (FXCM) or OANDA:XAUUSD."
    );
    bad.close().await;

    let mut feed = Feed::TradingView(Box::new(TradingViewFeed::with_command("FX:XAUUSD", node, dir.join("feed.mjs"))));
    feed.connect().await.unwrap();

    let bars = feed.fetch_m1(50).await.unwrap();
    let minute = Utc::now().naive_utc().duration_trunc(TimeDelta::minutes(1)).unwrap();
    assert_eq!(bars.len(), 50);
    assert!(bars.windows(2).all(|w| w[0].time < w[1].time), "sorted and unique");
    assert!(bars.last().unwrap().time < minute + TimeDelta::minutes(1), "no forming minute");
    assert!(bars.iter().any(|b| b.close == 999.0 && b.volume == 0.0), "a repeat keeps its last row");
    // timeoutMs as Python sends it, int((30 + 51/200) * 1000) - 5000, came back as the volume.
    let timeout_ms = ((30.0 + 51.0 / 200.0) * 1000.0) as i64 - 5000;
    assert!(bars.iter().any(|b| b.volume == timeout_ms as f64));

    let days = feed.daily_history("UTC", 5).await.unwrap().unwrap();
    assert!(days.len() <= 5 && !days.is_empty());
    assert!(days.iter().all(|d| d.time.and_utc().timestamp() % 86400 == 0));

    let ticks = feed.new_ticks().await.unwrap();
    assert_eq!(ticks.len(), 1);
    assert_eq!(ticks[0].price, 2650.0);
    assert_eq!(ticks[0].time.and_utc().timestamp(), 1763632800);
    assert_eq!(feed.last_price().await.unwrap(), Some(2650.0));

    let d = feed.describe();
    assert_eq!(d["name"], "Gold Spot");
    assert_eq!(d["exchange"], "FXCM");
    assert_eq!(d["last"], "2650.0 at 2025-11-20 10:00:00 UTC");

    feed.close().await;
    let _ = std::fs::remove_dir_all(&dir);
}

/// The only test that sets WEDNESDAY_TRADINGVIEW_DIR / WEDNESDAY_NODE (the environment is
/// shared by the tests of this file).
#[tokio::test]
async fn finding_the_helper() {
    let Some(node) = node_or_skip("discovery test") else { return };
    let set = |dir: &Path| std::env::set_var("WEDNESDAY_TRADINGVIEW_DIR", dir);

    let empty = std::env::temp_dir().join(format!("wednesday-feed-empty-{}", std::process::id()));
    std::fs::create_dir_all(&empty).unwrap();
    set(&empty);
    std::env::set_var("WEDNESDAY_NODE", &node);
    assert_eq!(
        tradingview_command().unwrap_err(),
        "The TradingView helper is missing from this install. Reinstall Wednesday, or run it from source."
    );

    let bare = fake_dir("bare", false);
    set(&bare);
    assert_eq!(tradingview_command().unwrap_err(), "Install it with: npm install --prefix tradingview (or make install)");

    std::env::set_var("WEDNESDAY_NODE", bare.join("no-such-node"));
    assert_eq!(tradingview_command().unwrap_err(), "Needs Node 20+ (nodejs.org), then: npm install --prefix tradingview");

    let full = fake_dir("full", true);
    set(&full);
    std::env::set_var("WEDNESDAY_NODE", &node);
    assert_eq!(tradingview_command().unwrap(), (node.clone(), full.join("feed.mjs")));

    // A feed without its own command finds the helper the same way.
    let mut feed = Feed::build("tradingview", "FX:XAUUSD").unwrap();
    feed.connect().await.unwrap();
    assert_eq!(feed.describe()["exchange"], "FXCM");
    feed.close().await;

    std::env::set_var("WEDNESDAY_NODE", bare.join("no-such-node"));
    let mut feed = Feed::build("tradingview", "FX:XAUUSD").unwrap();
    let err = feed.connect().await.unwrap_err();
    assert_eq!(
        err.message,
        "TradingView source unavailable. Needs Node 20+ (nodejs.org), then: npm install --prefix tradingview"
    );

    std::env::remove_var("WEDNESDAY_TRADINGVIEW_DIR");
    std::env::remove_var("WEDNESDAY_NODE");
    for d in [empty, bare, full] {
        let _ = std::fs::remove_dir_all(d);
    }
}

/// Real FX:XAUUSD bars through this checkout's tradingview/ helper (needs `npm install --prefix
/// tradingview` and the network): `cargo test --test feed -- --ignored`.
#[tokio::test]
#[ignore]
async fn live_tradingview_bars() {
    let mut feed = Feed::build("tradingview", "FX:XAUUSD").unwrap();
    feed.connect().await.unwrap();
    let bars = feed.fetch_m1(5).await.unwrap();
    assert!(!bars.is_empty() && bars.len() <= 5);
    let minute = Utc::now().naive_utc().duration_trunc(TimeDelta::minutes(1)).unwrap();
    assert!(bars.iter().all(|b| b.time < minute && b.high >= b.low));
    println!("{bars:?}\n{}", feed.describe());
    let days = feed.daily_history("UTC", 3).await.unwrap();
    println!("{days:?}");
    println!("{:?}", feed.last_price().await.unwrap());
    feed.close().await;
}
