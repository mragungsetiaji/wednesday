//! The drawings routes' logic (`server.py`'s `/api/drawings...`): answers, refusals and how an
//! alert follows its drawing.

use chrono::NaiveDateTime;
use serde_json::{json, Map, Value};
use wednesday_rs::drawings::{routes, DrawingAlerts};
use wednesday_rs::scan::Bar;
use wednesday_rs::store::Store;

const ID: &str = "d1a2b3c4-0000-4000-8000-000000000001";
const T0: i64 = 1_767_261_600; // 2026-01-01 10:00 on the feed's clock

async fn store() -> Store {
    Store::open("sqlite://", &std::env::temp_dir()).await.unwrap()
}

fn obj(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}

fn bar(t: i64, high: f64, low: f64, close: f64) -> Bar {
    let time = NaiveDateTime::parse_from_str("1970-01-01 00:00", "%Y-%m-%d %H:%M").unwrap() + chrono::Duration::seconds(t);
    Bar { time, open: close, high, low, close, volume: 1.0 }
}

fn refusal(answer: wednesday_rs::engine::Answer) -> (u16, Value) {
    let r = answer.unwrap_err();
    (r.status, r.detail)
}

#[tokio::test]
async fn drawings_are_saved_listed_and_deleted() {
    let s = store().await;
    let (src, sym) = ("synthetic", "XAUUSD");
    let empty = routes::list(&s, src, sym).await.unwrap();
    assert_eq!(empty, json!({"source": src, "symbol": sym, "drawings": [], "alerts": []}));

    let body = obj(json!({"kind": "hline", "points": [{"t": T0, "p": 2000}], "style": {"width": 2}}));
    let saved = routes::put(&s, src, sym, ID, &body, None).await.unwrap();
    let keys: Vec<&str> = saved.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["id", "kind", "points", "style", "props", "timeframes", "locked", "hidden", "updated_at"]);
    assert_eq!(serde_json::to_string(&saved["points"]).unwrap(), r#"[{"t":1767261600,"p":2000.0}]"#);
    let listed = routes::list(&s, src, sym).await.unwrap();
    assert_eq!(listed["drawings"], json!([saved]));
    // Another symbol has its own drawings.
    assert_eq!(routes::list(&s, src, "OTHER").await.unwrap()["drawings"], json!([]));

    assert_eq!(refusal(routes::put(&s, src, sym, "bad", &body, None).await), (422, json!("Bad drawing id")));
    let bad = obj(json!({"kind": "rect", "points": [{"t": T0, "p": 1}]}));
    assert_eq!(refusal(routes::put(&s, src, sym, ID, &bad, None).await), (422, json!("A rect takes 2 point(s)")));
    let bad = obj(json!({"kind": "zigzag"}));
    assert_eq!(refusal(routes::put(&s, src, sym, ID, &bad, None).await), (422, json!("Unknown drawing kind 'zigzag'")));

    assert_eq!(routes::delete(&s, src, "OTHER", ID, None).await.unwrap_err().status, 404);
    assert_eq!(routes::delete(&s, src, sym, ID, None).await.unwrap(), json!({"deleted": ID}));
    assert_eq!(refusal(routes::delete(&s, src, sym, ID, None).await), (404, json!("No such drawing")));
}

#[tokio::test]
async fn an_alert_follows_its_drawing() {
    let s = store().await;
    let (src, sym) = ("synthetic", "XAUUSD");
    let checker = DrawingAlerts::new();
    let line = |p: f64| obj(json!({"kind": "hline", "points": [{"t": T0, "p": p}]}));
    let cross = obj(json!({"condition": "cross_up", "mode": "every"}));

    assert_eq!(refusal(routes::alert_put(&s, src, sym, ID, None, &cross, Some(&checker)).await),
               (404, json!("No such drawing: save it first")));
    routes::put(&s, src, sym, ID, &line(2000.0), Some(&checker)).await.unwrap();
    let bad = obj(json!({"condition": "enter"}));
    assert_eq!(refusal(routes::alert_put(&s, src, sym, ID, None, &bad, None).await),
               (422, json!("A hline alert's condition is one of cross_up, cross_down, cross")));
    let past = obj(json!({"condition": "cross", "expires_at": "2001-01-01"}));
    assert_eq!(refusal(routes::alert_put(&s, src, sym, ID, None, &past, None).await),
               (422, json!("The expiry is in the past")));

    let m1 = vec![bar(T0, 1999.0, 1995.0, 1998.0), bar(T0 + 60, 1999.5, 1997.0, 1999.0)];
    let armed_bar = routes::armed_bar(&m1);
    assert_eq!(armed_bar, Some(T0 + 60));
    let alert = routes::alert_put(&s, src, sym, ID, armed_bar, &cross, Some(&checker)).await.unwrap();
    let keys: Vec<&str> = alert.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["drawing_id", "condition", "mode", "note", "expires_at", "intrabar", "armed", "armed_bar",
                      "fired_at", "fired_price", "fires", "created_at"]);
    assert_eq!(routes::alerts(&s, src, sym).await.unwrap(), json!({"alerts": [alert]}));

    // The first check only remembers where the stream is.
    assert!(checker.check(&s, src, sym, &m1, None).await.is_empty());
    // The line moves up to 2005: a bar reaching 2003 crosses the old level, not the new one.
    routes::put(&s, src, sym, ID, &line(2005.0), Some(&checker)).await.unwrap();
    let mut m1 = m1;
    m1.push(bar(T0 + 120, 2003.0, 1998.0, 2002.0));
    assert!(checker.check(&s, src, sym, &m1, None).await.is_empty());
    m1.push(bar(T0 + 180, 2006.0, 2001.0, 2004.0));
    let fired = checker.check(&s, src, sym, &m1, None).await;
    assert_eq!(fired, [format!("drawing|{src}|{sym}|{ID}|bar{}", T0 + 180)]);
    let row = &routes::alerts(&s, src, sym).await.unwrap()["alerts"][0];
    assert_eq!((row["fires"].clone(), row["fired_price"].clone(), row["armed"].clone()), (json!(1), json!(2004.0), json!(true)));

    // Deleting the drawing keeps its alert (for undo); deleting the alert removes it.
    routes::delete(&s, src, sym, ID, Some(&checker)).await.unwrap();
    assert_eq!(routes::alerts(&s, src, sym).await.unwrap()["alerts"].as_array().unwrap().len(), 1);
    assert_eq!(routes::alert_delete(&s, src, sym, ID, Some(&checker)).await.unwrap(), json!({"deleted": ID}));
    assert_eq!(refusal(routes::alert_delete(&s, src, sym, ID, None).await), (404, json!("That drawing has no alert")));
    assert_eq!(routes::alerts(&s, src, sym).await.unwrap(), json!({"alerts": []}));
}
