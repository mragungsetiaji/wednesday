//! The order block alerts' Telegram client and routes (#96, step 9e), against a fake Telegram on a
//! local port and a credential store in memory: the same answers and messages as `alerts.py` and
//! `server.py`, and the bot token in none of them.

use crate::fake_telegram;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use chrono::NaiveDateTime;
use serde_json::{json, Map, Value};
use wednesday_rs::alerts::{AlertSettings, Alerts, Env, Keyring, Secrets, TelegramClient, TelegramError};
use wednesday_rs::scan::scanner::{LevelSet, TimeframeResult};
use wednesday_rs::scan::{Bar, Detector, Level, ScanResult, TIMEFRAMES};
use wednesday_rs::store::Store;

const TOKEN: &str = "123456:SECRET-token";

#[derive(Clone, Default)]
struct MemKeyring(Arc<Mutex<HashMap<(String, String), String>>>);

impl Keyring for MemKeyring {
    fn get(&self, s: &str, a: &str) -> Result<Option<String>, String> {
        Ok(self.0.lock().unwrap().get(&(s.into(), a.into())).cloned())
    }
    fn set(&self, s: &str, a: &str, v: &str) -> Result<(), String> {
        self.0.lock().unwrap().insert((s.into(), a.into()), v.into());
        Ok(())
    }
    fn delete(&self, s: &str, a: &str) -> Result<(), String> {
        self.0.lock().unwrap().remove(&(s.into(), a.into())).map(|_| ()).ok_or_else(|| "not saved".into())
    }
}

fn no_env() -> Env {
    Arc::new(|_: &str| None)
}

async fn store() -> (Store, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!("wed-alerts-{}-{}", std::process::id(), rand_suffix()));
    std::fs::create_dir_all(&dir).unwrap();
    let s = Store::open(&format!("sqlite:///{}", dir.join("a.db").display()), &dir).await.unwrap();
    (s, dir)
}

fn rand_suffix() -> u128 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
}

fn body(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}

// ---- the client ----

#[tokio::test]
async fn sends_as_python_does() {
    let fake = fake_telegram::start().await;
    let c = TelegramClient::new(TOKEN, Some("42".into()), fake.url.clone());
    c.send("<b>hi</b> & more").await.unwrap();
    let r = fake.requests.lock().unwrap()[0].clone();
    assert_eq!(r.path, format!("/bot{TOKEN}/sendMessage"));
    assert_eq!(r.form, vec![("chat_id".into(), "42".into()), ("text".into(), "<b>hi</b> & more".into()),
                            ("parse_mode".into(), "HTML".into()), ("disable_web_page_preview".into(), "true".into())]);
    let no_chat = TelegramClient::new(TOKEN, None, fake.url.clone());
    assert_eq!(no_chat.send("x").await, Err(TelegramError("TELEGRAM_CHAT_ID is not set".into())));
}

#[tokio::test]
async fn telegram_errors_as_python_words_them() {
    let fake = fake_telegram::start().await;
    let c = TelegramClient::new(TOKEN, Some("42".into()), fake.url.clone());
    fake.reply("sendMessage", 400, r#"{"ok": false, "error_code": 400, "description": "Bad Request: chat not found"}"#);
    assert_eq!(c.send("x").await.unwrap_err().0, "Telegram sendMessage failed: Bad Request: chat not found");
    fake.reply("sendMessage", 404, "<html>nope</html>");
    assert_eq!(c.send("x").await.unwrap_err().0, "Telegram sendMessage failed: HTTP Error 404: Not Found");
    fake.reply("sendMessage", 200, r#"{"ok": false, "description": "odd"}"#);
    assert_eq!(c.send("x").await.unwrap_err().0, "Telegram sendMessage failed: odd");
    fake.reply("sendMessage", 200, r#"{"ok": false}"#);
    assert_eq!(c.send("x").await.unwrap_err().0, "Telegram sendMessage failed: {'ok': False}");
    // A description quoting the token (as a URL would) never carries it out.
    fake.reply("sendMessage", 401, &format!(r#"{{"ok": false, "description": "Unauthorized bot{TOKEN}"}}"#));
    let e = c.send("x").await.unwrap_err().0;
    assert!(!e.contains(TOKEN) && e.starts_with("Telegram sendMessage failed: Unauthorized bot"), "{e}");
}

#[tokio::test]
async fn unreachable_telegram() {
    // A port nothing listens on.
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    drop(l);
    let c = TelegramClient::new(TOKEN, Some("42".into()), url);
    let e = c.send("x").await.unwrap_err().0;
    assert!(e.starts_with("Can't reach Telegram: "), "{e}");
    assert!(!e.contains(TOKEN) && !e.contains("/bot"), "{e}");
}

#[tokio::test]
async fn chats_from_updates() {
    let fake = fake_telegram::start().await;
    fake.reply("getUpdates", 200, r#"{"ok": true, "result": [
        {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Ann", "username": "ann"}}},
        {"update_id": 2, "channel_post": {"chat": {"id": -100, "type": "channel", "title": "Gold"}}},
        {"update_id": 3, "my_chat_member": {"chat": {"id": -5, "type": "group", "title": ""}}},
        {"update_id": 4, "message": {"chat": {"id": 42, "type": "private", "first_name": "Ann B"}}},
        {"update_id": 5, "edited_message": {"chat": {"id": 9}}}
    ]}"#);
    let c = TelegramClient::new(TOKEN, None, fake.url.clone());
    let chats = c.chats().await.unwrap();
    assert_eq!(Value::Array(chats), json!([
        {"id": 42, "type": "private", "name": "Ann B"},
        {"id": -100, "type": "channel", "name": "Gold"},
        {"id": -5, "type": "group", "name": null},
    ]));
    assert_eq!(fake.requests.lock().unwrap()[0].path, format!("/bot{TOKEN}/getUpdates"));
}

// ---- the routes ----

fn alerts_with(store: Option<Store>, kr: MemKeyring, api: &str) -> Alerts {
    Alerts::new(store, Secrets::with(Box::new(kr), |_| None), no_env(), api, AlertSettings::default())
}

#[tokio::test]
async fn put_validates_and_keeps_the_token_out_of_answers() {
    let fake = fake_telegram::start().await;
    let (st, dir) = store().await;
    let kr = MemKeyring::default();
    let alerts = alerts_with(Some(st.clone()), kr.clone(), &fake.url);

    let a = alerts.get(None).await.unwrap();
    assert_eq!(a, json!({"editable": true, "token_set": false, "token_source": null, "chat_id_set": false,
                         "configured": false, "settings": AlertSettings::default().to_json(), "last_error": null,
                         "recent": []}));

    let bad = alerts.put(&body(json!({"timeframes": ["2H"], "chat_id": "my chat", "priorities": ["low"]})), None).await.unwrap_err();
    assert_eq!(bad.status, 422);
    assert_eq!(bad.detail, json!("Unknown timeframe '2H'; The chat id is a number like 123456789 (or -100… for a group), or @channelname; Unknown priority 'low'"));
    assert!(kr.0.lock().unwrap().is_empty(), "a refused request saves nothing");

    let a = alerts.put(&body(json!({"telegram_bot_token": TOKEN, "chat_id": "42", "timeframes": ["5M"]})), None).await.unwrap();
    assert!(!a.to_string().contains(TOKEN));
    assert_eq!(a["token_set"], true);
    assert_eq!(a["token_source"], "saved");
    assert_eq!(a["configured"], true);
    assert_eq!(a["settings"], json!({"enabled": true, "timeframes": ["5M"], "priorities": ["extreme", "middle"],
                                     "neutral_alerts": false, "chat_id": "42"}));
    assert_eq!(kr.0.lock().unwrap().get(&("Wednesday".into(), "telegram_bot_token".into())).map(String::as_str), Some(TOKEN));
    // Saved under "alerts", without the token.
    let saved = st.get_setting("alerts").await.unwrap().unwrap();
    assert_eq!(saved, a["settings"]);
    let raw = st.rows("settings", "SELECT * FROM settings", vec![]).await.unwrap();
    assert!(!serde_json::to_string(&raw).unwrap().contains(TOKEN));

    // A drawing alert's error shows when the order blocks have none.
    let a = alerts.get(Some("drawing failed".into())).await.unwrap();
    assert_eq!(a["last_error"], "drawing failed");

    alerts.test("XAU<USD>").await.unwrap();
    assert_eq!(fake.sent(), vec!["<b>XAU&lt;USD&gt; screener</b>\nTest message: alerts are connected."]);

    let a = alerts.put(&body(json!({"forget_telegram_bot_token": true, "chat_id": "42"})), None).await.unwrap();
    assert_eq!((a["token_set"].clone(), a["token_source"].clone(), a["configured"].clone()), (json!(false), json!(null), json!(false)));
    let e = alerts.test("XAUUSD").await.unwrap_err();
    assert_eq!((e.status, e.detail), (502, json!("Add the bot token and chat id in Settings > Telegram alerts first")));
    let e = alerts.chats().await.unwrap_err();
    assert_eq!((e.status, e.detail), (409, json!("Save the bot token first")));
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test]
async fn a_body_python_cant_read_is_a_server_error() {
    let alerts = alerts_with(None, MemKeyring::default(), "http://127.0.0.1:9");
    let e = alerts.put(&body(json!({"timeframes": null})), None).await.unwrap_err();
    assert_eq!(e.status, 500);
}

#[tokio::test]
async fn chats_and_test_pass_telegram_errors_on() {
    let fake = fake_telegram::start().await;
    let kr = MemKeyring::default();
    kr.set("Wednesday", "telegram_bot_token", TOKEN).unwrap();
    let alerts = alerts_with(None, kr, &fake.url);
    fake.reply("getUpdates", 200, r#"{"ok": true, "result": []}"#);
    assert_eq!(alerts.chats().await.unwrap(), json!({"chats": []}));
    fake.reply("getUpdates", 409, r#"{"ok": false, "description": "Conflict: webhook is active"}"#);
    let e = alerts.chats().await.unwrap_err();
    assert_eq!((e.status, e.detail), (502, json!("Telegram getUpdates failed: Conflict: webhook is active")));
    // No chat id yet: not configured, no sender.
    assert!(alerts.sender().is_none());
    alerts.put(&body(json!({"chat_id": "@gold"})), None).await.unwrap();
    let send = alerts.sender().expect("configured and on");
    send("hello".into()).await.unwrap();
    assert_eq!(fake.sent(), vec!["hello"]);
    alerts.put(&body(json!({"chat_id": "@gold", "enabled": false})), None).await.unwrap();
    assert!(alerts.sender().is_none(), "paused");
}

// ---- the check, with the log in the database ----

fn t(s: &str) -> NaiveDateTime {
    NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M").unwrap()
}

fn bar(at: &str, low: f64, high: f64) -> Bar {
    Bar { time: t(at), open: high, high, low, close: low, volume: 1.0 }
}

fn result() -> ScanResult {
    let mut ob = Level::new("ob", "bullish", 2471.0, 2469.5, t("2026-03-02 08:40"), t("2026-03-02 09:00"), "BULL OB");
    ob.meta = body(json!({"priority": "extreme", "entry": 2471.0, "sl": 2469.5, "risk": 1.5, "body": 1.5, "sl_capped": false}));
    let tf = *TIMEFRAMES.iter().find(|x| x.name == "5M").unwrap();
    ScanResult { time: t("2026-03-02 10:00"), price: 2472.0, detectors: vec![Detector::OrderBlock], results: vec![TimeframeResult {
        timeframe: tf, candles: 100,
        sets: vec![(Detector::OrderBlock, LevelSet { active: vec![ob], above: None, below: None, inside: vec![], recent: vec![] })],
        bias: None, swings: vec![],
    }] }
}

#[tokio::test]
async fn alerts_once_across_restarts_and_retries_a_failed_one() {
    let fake = fake_telegram::start().await;
    let (st, dir) = store().await;
    let kr = MemKeyring::default();
    kr.set("Wednesday", "telegram_bot_token", TOKEN).unwrap();
    let mk = || Alerts::new(Some(st.clone()), Secrets::with(Box::new(kr.clone()), |_| None), no_env(), fake.url.clone(),
                            AlertSettings::from_json(Some(&json!({"chat_id": "42"}))).unwrap());
    let alerts = mk();
    let mut m1 = vec![bar("2026-03-02 10:00", 2473.0, 2475.0)];
    assert!(alerts.check("tradingview", "XAUUSD", &result(), &m1, None).await.is_empty());

    // Telegram refuses: logged as failed, the error shown, retried on the next bar.
    fake.reply("sendMessage", 400, r#"{"ok": false, "description": "Bad Request: chat not found"}"#);
    m1.push(bar("2026-03-02 10:01", 2470.8, 2473.0));
    assert!(alerts.check("tradingview", "XAUUSD", &result(), &m1, Some("bullish")).await.is_empty());
    let a = alerts.get(None).await.unwrap();
    assert_eq!(a["last_error"], "Telegram sendMessage failed: Bad Request: chat not found");
    let row = a["recent"][0].as_object().unwrap();
    let key = "tradingview|XAUUSD|5M|bullish|2026-03-02T08:40:00|2471.00000";
    assert_eq!(row.keys().collect::<Vec<_>>(),
               ["key", "source", "symbol", "timeframe", "kind", "priority", "entry", "price", "sent_at", "status", "error", "summary"]);
    assert_eq!((row["key"].as_str(), row["status"].as_str(), row["entry"].as_f64(), row["price"].as_f64()),
               (Some(key), Some("failed"), Some(2471.0), Some(2472.0)));
    assert!(row["sent_at"].as_str().unwrap().ends_with("+00:00"));

    fake.reply("sendMessage", 200, r#"{"ok": true, "result": {}}"#);
    m1.push(bar("2026-03-02 10:02", 2470.5, 2471.5));
    assert_eq!(alerts.check("tradingview", "XAUUSD", &result(), &m1, Some("bullish")).await, vec![key.to_string()]);
    assert_eq!(alerts.last_error(), None);
    let msg = fake.sent().pop().unwrap();
    assert!(msg.ends_with("<b>RISK ON</b> (with bullish bias)"), "{msg}");
    let a = alerts.get(None).await.unwrap();
    assert_eq!((a["recent"].as_array().unwrap().len(), a["recent"][0]["status"].as_str()), (1, Some("sent")));

    // A restart (a new manager on the same database) doesn't repeat it.
    let again = mk();
    assert!(again.check("tradingview", "XAUUSD", &result(), &m1[..2], None).await.is_empty());
    m1.push(bar("2026-03-02 10:03", 2470.0, 2471.0));
    assert!(again.check("tradingview", "XAUUSD", &result(), &m1, None).await.is_empty());
    assert_eq!(fake.sent().len(), 2); // the failed try and the one that went
    assert!(st.alert_sent(key).await.unwrap() && st.alert_logged(key).await.unwrap());
    std::fs::remove_dir_all(dir).unwrap();
}
