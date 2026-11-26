//! The routes of #96 step 9f: the terms (`terms.py`), the bias (`PUT/GET /api/bias`), and writing
//! a journal entry with the market frozen into it. The snapshot files are tested in
//! `src/snapshots.rs`, the bias's expiry and messages against Python in `market_golden.rs`.

use serde_json::{json, Map, Value};
use wednesday_rs::engine::Answer;
use wednesday_rs::journal::service::{self, Fail};
use wednesday_rs::store::Store;
use wednesday_rs::{bias, snapshots, terms};

async fn store() -> Store {
    Store::open("sqlite://", &std::env::temp_dir()).await.unwrap()
}

fn obj(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}

fn refusal(answer: Answer) -> (u16, Value) {
    let r = answer.unwrap_err();
    (r.status, r.detail)
}

fn failure(r: Result<Value, Fail>) -> (u16, String) {
    match r {
        Err(Fail::Status(code, msg)) => (code, msg),
        other => panic!("not refused: {other:?}"),
    }
}

#[tokio::test]
async fn terms_are_accepted_once_per_version() {
    let s = store().await;
    let before = terms::status(&s, None).await.unwrap();
    assert_eq!(before["version"], terms::TERMS_VERSION);
    assert_eq!((before["accepted"].clone(), before["accepted_at"].clone()), (json!(false), Value::Null));
    assert_eq!(before["text"], terms::DISCLAIMER);
    let keys: Vec<&str> = before.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["version", "accepted", "accepted_at", "text"]);

    let after = terms::accept(&s, None).await.unwrap();
    assert_eq!(after["accepted"], true);
    let at = after["accepted_at"].as_str().unwrap();
    assert!(at.ends_with("+00:00") && at.len() >= 25, "{at}");
    let saved = s.get_setting(terms::TERMS_KEY).await.unwrap().unwrap();
    assert_eq!(saved, json!({"version": terms::TERMS_VERSION, "accepted_at": at}));
    assert_eq!(terms::status(&s, None).await.unwrap(), after);

    // Terms of an older version ask again.
    s.set_setting(terms::TERMS_KEY, &json!({"version": "2020-01-01", "accepted_at": at})).await.unwrap();
    assert_eq!(terms::status(&s, None).await.unwrap()["accepted"], false);
}

#[tokio::test]
async fn the_bias_is_set_read_and_cleared() {
    let s = store().await;
    assert_eq!(bias::get(Some(&s)).await.unwrap(), json!({"bias": null}));
    assert_eq!(bias::get(None).await.unwrap(), json!({"bias": null}));

    assert_eq!(refusal(bias::put(&s, &obj(json!({"direction": "up"}))).await), (422, json!("Unknown bias 'up'")));
    assert_eq!(refusal(bias::put(&s, &obj(json!({"direction": "bullish", "expiry": "month"}))).await),
               (422, json!("Unknown expiry 'month'")));
    assert_eq!(s.get_setting("bias").await.unwrap(), None);

    let out = bias::put(&s, &obj(json!({"direction": "bearish", "note": " CPI hot ", "expiry": "week"}))).await.unwrap();
    let b = &out["bias"];
    let keys: Vec<&str> = b.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["direction", "note", "expiry", "set_at", "expires_at", "expired"]);
    assert_eq!((b["direction"].clone(), b["note"].clone(), b["expired"].clone()), (json!("bearish"), json!("CPI hot"), json!(false)));
    assert!(b["expires_at"].as_str().unwrap().ends_with("+00:00"));
    assert_eq!(s.get_setting("bias").await.unwrap().unwrap(), *b); // as Python stores to_dict()
    assert_eq!(bias::get(Some(&s)).await.unwrap(), out);
    assert_eq!(wednesday_rs::engine::bias(Some(&s)).await.1.as_deref(), Some("bearish"));

    assert_eq!(bias::put(&s, &obj(json!({"direction": null}))).await.unwrap(), json!({"bias": null}));
    assert_eq!(s.get_setting("bias").await.unwrap(), None);
}

#[tokio::test]
async fn an_expired_bias_reads_as_expired() {
    let s = store().await;
    let old = json!({"direction": "bullish", "note": "", "expiry": "day", "set_at": "2026-01-05T14:00:00+00:00",
                     "expires_at": "2026-01-05T22:00:00+00:00", "expired": false});
    s.set_setting("bias", &old).await.unwrap();
    let out = bias::get(Some(&s)).await.unwrap();
    assert_eq!(out["bias"]["expired"], true);
    assert_eq!(wednesday_rs::engine::bias(Some(&s)).await.1, None); // not active: the scan has no bias
}

#[tokio::test]
async fn entries_keep_the_market_they_were_written_in() {
    let s = store().await;
    let j = service::create(&s, &obj(json!({"name": "Main"})), &[], None).await.unwrap();
    let id = j["id"].as_str().unwrap();

    assert_eq!(failure(service::add_entry(&s, "nope", &obj(json!({"text": "x"})), None).await),
               (404, "No such journal".into()));
    assert_eq!(failure(service::add_entry(&s, id, &obj(json!({"kind": "rant", "text": "x"})), None).await),
               (422, "An entry is one of note, review, setup".into()));
    assert_eq!(failure(service::add_entry(&s, id, &obj(json!({"text": "   "})), None).await),
               (422, "Write something first".into()));
    assert_eq!(failure(service::add_entry(&s, id, &obj(json!({"kind": "setup", "setup": [1]})), None).await),
               (422, "Journal a setup from a row of the ladder".into()));
    assert_eq!(failure(service::add_entry(&s, id, &obj(json!({"text": "x", "mood": 9})), None).await),
               (422, "Mood is 1 to 5".into()));

    let market = json!({"at": "2026-03-04T15:00:00+00:00", "symbol": "FX:XAUUSD", "price": 2164.5});
    let row = service::add_entry(&s, id, &obj(json!({"text": " Waited for the sweep ", "tags": ["Patience", "patience", " "],
                                                     "mood": "4"})), Some(market.clone())).await.unwrap();
    let keys: Vec<&str> = row.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["id", "journal_id", "kind", "created_at", "updated_at", "text", "tags", "mood", "setup", "market"]);
    assert_eq!(row["id"].as_str().unwrap().len(), 12);
    assert_eq!((row["kind"].clone(), row["text"].clone(), row["tags"].clone(), row["mood"].clone()),
               (json!("note"), json!("Waited for the sweep"), json!(["patience"]), json!(4)));
    assert_eq!((row["setup"].clone(), row["market"].clone(), row["updated_at"].clone()), (Value::Null, market, Value::Null));

    let setup = json!({"timeframe": "15M", "side": "sell", "entry": 2170.1});
    let with_setup = service::add_entry(&s, id, &obj(json!({"kind": "setup", "setup": setup})), None).await.unwrap();
    assert_eq!((with_setup["text"].clone(), with_setup["market"].clone()), (json!(""), Value::Null));

    let listed = service::list_entries(&s, id).await.unwrap();
    let listed = listed["entries"].as_array().unwrap();
    assert_eq!(listed.len(), 2);
    let back = listed.iter().find(|e| e["id"] == row["id"]).unwrap();
    assert_eq!(back, &row); // stored and read back as written
    assert_eq!(listed.iter().find(|e| e["id"] == with_setup["id"]).unwrap()["setup"], setup);
}

#[tokio::test]
async fn trade_images_use_the_snapshot_folder() {
    // A snapshot uploaded through snapshots.rs is one the journal can attach (same folder, same ids).
    let home = std::env::temp_dir().join(format!("wednesday-9f-{}", std::process::id()));
    let s = Store::open("sqlite://", &home).await.unwrap();
    let png = [snapshots::PNG_SIGNATURE, b"image"].concat();
    let sid = snapshots::save(&snapshots::dir(&home), &png).unwrap()["id"].as_str().unwrap().to_string();
    let j = service::create(&s, &obj(json!({"name": "Main"})), &[], None).await.unwrap();
    let cache = service::Cache::default();
    let err = failure(service::attach_image(&s, &cache, j["id"].as_str().unwrap(), "t1", &obj(json!({"snapshot_id": "../x"}))).await);
    assert_eq!(err, (404, "No such snapshot: upload it first".into()));
    // The snapshot exists, so the refusal is about the trade, not the image.
    let err = failure(service::attach_image(&s, &cache, j["id"].as_str().unwrap(), "t1", &obj(json!({"snapshot_id": sid}))).await);
    assert_eq!(err.0, 404);
    assert_ne!(err.1, "No such snapshot: upload it first");
    let _ = std::fs::remove_dir_all(home);
}
