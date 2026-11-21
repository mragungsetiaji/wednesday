//! The calendar's weekly fetch (#96, step 9f) against a local fake source, no network: the week
//! parsed and kept (in memory, the store's cache and the history), HTTP errors and bad JSON keeping
//! the last good copy, at most one fetch an hour, a retry ten minutes after a failure, a restart
//! reading the cached week; and the routes' answers built on it.

use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use chrono::{DateTime, Duration, NaiveDateTime, Utc};
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use wednesday_rs::calendar::{self, Calendar, CalendarSettings, Market, CALENDAR_CACHE_KEY};
use wednesday_rs::scan::Bar;
use wednesday_rs::store::Store;

struct Fake {
    url: String,
    hits: Arc<AtomicUsize>,
    reply: Arc<Mutex<(u16, String, u64)>>, // status, body, delay in ms
    agent: Arc<Mutex<String>>,
}

impl Fake {
    fn reply(&self, status: u16, body: &str) {
        *self.reply.lock().unwrap() = (status, body.into(), 0);
    }
    fn hits(&self) -> usize {
        self.hits.load(Ordering::SeqCst)
    }
}

async fn fake() -> Fake {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/ff_calendar_thisweek.json", l.local_addr().unwrap());
    let fake = Fake { url, hits: Default::default(), reply: Arc::new(Mutex::new((200, "[]".into(), 0))),
                      agent: Default::default() };
    let (hits, reply, agent) = (fake.hits.clone(), fake.reply.clone(), fake.agent.clone());
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = l.accept().await else { break };
            let (hits, reply, agent) = (hits.clone(), reply.clone(), agent.clone());
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 1024];
                while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                    match sock.read(&mut chunk).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => buf.extend_from_slice(&chunk[..n]),
                    }
                }
                let head = String::from_utf8_lossy(&buf).to_string();
                if let Some(line) = head.lines().find(|l| l.to_lowercase().starts_with("user-agent:")) {
                    *agent.lock().unwrap() = line[11..].trim().to_string();
                }
                hits.fetch_add(1, Ordering::SeqCst);
                let (status, body, delay) = reply.lock().unwrap().clone();
                tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
                let res = format!("HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                                   Connection: close\r\n\r\n{body}", body.len());
                let _ = sock.write_all(res.as_bytes()).await;
            });
        }
    });
    fake
}

const WEEK: &str = r#"[
  {"title": "CPI m/m", "country": "USD", "date": "2026-03-18T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
  {"title": "ECB President Speaks", "country": "EUR", "date": "2026-03-18T09:00:00-04:00", "impact": "High"},
  {"title": "Bank Holiday", "country": "USD", "date": "2026-03-19", "impact": "Holiday"},
  {"title": "Unemployment Claims", "country": "USD", "date": "2099-03-19T08:30:00-04:00", "impact": "High"}
]"#;

async fn store() -> Store {
    Store::open("sqlite://", Path::new(".")).await.unwrap()
}

async fn calendar_at(store: Option<Store>, url: &str) -> Arc<Calendar> {
    let cal = Calendar::new(store).await;
    cal.set_settings(CalendarSettings { url: url.into(), ..Default::default() }).await.unwrap();
    cal
}

async fn refreshed(cal: &Arc<Calendar>, now: DateTime<Utc>) -> bool {
    match cal.maybe_refresh(now).await {
        Some(job) => {
            job.await.unwrap();
            true
        }
        None => false,
    }
}

async fn snapshot(cal: &Arc<Calendar>, now: DateTime<Utc>) -> Value {
    Value::Object(cal.snapshot(now).await)
}

#[tokio::test]
async fn a_fetched_week_is_kept_and_refetched_hourly() {
    let src = fake().await;
    src.reply(200, WEEK);
    let store = store().await;
    let cal = calendar_at(Some(store.clone()), &src.url).await;
    let now = Utc::now();
    assert!(refreshed(&cal, now).await);
    assert_eq!(src.hits(), 1);
    assert_eq!(*src.agent.lock().unwrap(), "Mozilla/5.0 (Wednesday calendar)");
    let titles: Vec<String> = cal.events().iter().map(|e| e["title"].as_str().unwrap().to_string()).collect();
    assert_eq!(titles, ["CPI m/m", "ECB President Speaks", "Unemployment Claims"]); // the dateless holiday dropped
    // Kept in the store, and added to the history.
    let cache = store.get_setting(CALENDAR_CACHE_KEY).await.unwrap().unwrap();
    assert_eq!(cache["url"], json!(src.url));
    assert_eq!(cache["events"].as_array().unwrap().len(), 3);
    assert_eq!(store.calendar_history(&[], &[], None).await.unwrap().len(), 3);
    let snap = snapshot(&cal, now).await;
    assert_eq!(snap["error"], Value::Null);
    assert_eq!(snap["loading"], json!(false));
    assert_eq!(snap["fetched_at"], cache["fetched_at"]);
    assert_eq!(snap["events"].as_array().unwrap().len(), 1); // USD, High, from 30 minutes ago
    assert_eq!(snap["week"].as_array().unwrap().len(), 2);

    // Not again within the hour; again after it.
    assert!(!refreshed(&cal, now + Duration::minutes(59)).await);
    assert_eq!(src.hits(), 1);
    assert!(refreshed(&cal, now + Duration::minutes(61)).await);
    assert_eq!(src.hits(), 2);

    // A restart reads the cached week and doesn't fetch it again.
    let again = Calendar::new(Some(store.clone())).await;
    assert_eq!(again.events().len(), 3);
    assert!(!refreshed(&again, now).await);
    assert_eq!(src.hits(), 2);
    // Another URL is stale at once; a disabled calendar never fetches.
    again.set_settings(CalendarSettings { url: format!("{}?v=2", src.url), ..Default::default() }).await.unwrap();
    let snap = snapshot(&again, now).await;
    assert_eq!((snap["fetched_at"].clone(), snap["loading"].clone()), (Value::Null, json!(true))); // not this source's week
    for _ in 0..100 {
        if snapshot(&again, now).await["loading"] == json!(false) {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    assert_eq!(src.hits(), 3);
    assert_eq!(snapshot(&again, now).await["fetched_at"].as_str().map(|_| ()), Some(()));
    again.set_settings(CalendarSettings { enabled: false, url: "https://x.test/".into(), ..Default::default() })
        .await.unwrap();
    assert!(!refreshed(&again, now).await);
    assert_eq!(src.hits(), 3);
}

#[tokio::test]
async fn failures_keep_the_last_good_week_and_retry_after_ten_minutes() {
    let src = fake().await;
    src.reply(200, WEEK);
    let store = store().await;
    let cal = calendar_at(Some(store.clone()), &src.url).await;
    assert!(refreshed(&cal, Utc::now()).await);
    let cached = store.get_setting(CALENDAR_CACHE_KEY).await.unwrap();

    let t = Utc::now() + Duration::hours(2);
    for (status, body, error) in [
        (500, "oops", "Calendar source returned HTTP 500"),
        (404, "[]", "Calendar source returned HTTP 404"),
        (200, "<html>", "The calendar source didn't return JSON"),
        (200, "{\"events\": []}", "Unexpected calendar format (expected a list of events)"),
    ] {
        src.reply(status, body);
        let hits = src.hits();
        // The first try after a failure waits ten minutes from the last one.
        let at = t + Duration::minutes(11 * (hits as i64));
        assert!(refreshed(&cal, at).await, "{error}");
        let snap = snapshot(&cal, at + Duration::minutes(1)).await;
        assert_eq!(snap["error"], json!(error));
        assert_eq!(cal.events().len(), 3, "the last good copy stays");
        assert_eq!(store.get_setting(CALENDAR_CACHE_KEY).await.unwrap(), cached);
        assert!(!refreshed(&cal, at + Duration::minutes(9)).await);
        assert_eq!(src.hits(), hits + 1);
    }
    // A good answer clears the error.
    src.reply(200, WEEK);
    assert!(refreshed(&cal, t + Duration::hours(1)).await);
    assert_eq!(snapshot(&cal, t + Duration::hours(1)).await["error"], Value::Null);
}

#[tokio::test]
async fn an_unreachable_source_and_a_fetch_in_progress() {
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let closed = format!("http://{}/week.json", l.local_addr().unwrap());
    drop(l);
    let cal = calendar_at(None, &closed).await;
    cal.refresh().await;
    let e = snapshot(&cal, Utc::now()).await["error"].as_str().unwrap().to_string();
    assert!(e.starts_with("Can't reach the calendar source: "), "{e}");

    // While a fetch runs, the answer says so and no second one starts.
    let src = fake().await;
    *src.reply.lock().unwrap() = (200, WEEK.into(), 300);
    let cal = calendar_at(None, &src.url).await;
    let snap = snapshot(&cal, Utc::now()).await;
    assert_eq!(snap["loading"], json!(true));
    assert!(cal.maybe_refresh(Utc::now()).await.is_none());
    tokio::time::sleep(std::time::Duration::from_millis(600)).await;
    let snap = snapshot(&cal, Utc::now()).await;
    assert_eq!(snap["loading"], json!(false));
    assert_eq!(src.hits(), 1);
    assert_eq!(cal.upcoming().await.len(), 1);
}

fn bars_around(t0: NaiveDateTime) -> Vec<Bar> {
    (-240..90)
        .map(|m| {
            let c = if m < 0 { 2000.0 + (m % 3) as f64 * 0.1 } else { 2000.0 + m as f64 * 0.1 };
            Bar { time: t0 + Duration::minutes(m), open: c, high: c + 0.3, low: c - 0.3, close: c, volume: 1.0 }
        })
        .collect()
}

#[tokio::test]
async fn the_routes_answer_as_python_does() {
    let src = fake().await;
    src.reply(200, WEEK);
    let store = store().await;
    let cal = calendar_at(Some(store.clone()), &src.url).await;
    cal.refresh().await;
    // A past CPI release with bars around it on the NY+7 clock (08:30 NY is 15:30 there).
    let past = DateTime::parse_from_rfc3339("2026-02-11T13:30:00Z").unwrap().with_timezone(&Utc);
    let t0 = NaiveDateTime::parse_from_str("2026-02-11 15:30", "%Y-%m-%d %H:%M").unwrap();
    let market = Market { clock: "NY+7".into(), source: "tradingview".into(), symbol: "XAUUSD".into(),
                          m1: Arc::new(bars_around(t0)), persist: false, store: Some(store.clone()) };
    let now = DateTime::parse_from_rfc3339("2026-03-18T12:00:00Z").unwrap().with_timezone(&Utc);

    // Import: refused without a database, then a bad file, then a good one.
    let bare = Calendar::new(None).await;
    let e = calendar::import(&bare, None, b"x").await.unwrap_err();
    assert_eq!((e.status, e.detail), (409, json!("Importing a calendar needs the server running with --serve and a database")));
    let e = calendar::import(&cal, Some("UTC"), b"nope").await.unwrap_err();
    assert_eq!(e.status, 422);
    let e = calendar::import(&cal, Some("Asia/Jakarta"), b"").await.unwrap_err();
    assert_eq!(e.detail, json!("The time zone is one of UTC, America/New_York"));
    let e = calendar::import(&cal, Some("UTC"), b"datetime,currency,title\r2026-01-09 08:30,USD,X\r").await.unwrap_err();
    assert_eq!((e.status, e.detail), (500, json!("Internal Server Error")));
    let before = calendar::reactions_at(&cal, &market, now).await.unwrap();
    assert_eq!(before["types"], json!([])); // the fetched week's releases are still to come
    let body = format!("datetime,currency,title,impact,actual,forecast\n{},USD,CPI m/m,High,0.4%,0.3%\n", past.to_rfc3339());
    let done = calendar::import(&cal, None, body.as_bytes()).await.unwrap();
    assert_eq!(done["imported"], json!(1));
    assert_eq!(done["history"], json!({"stored": 4, "first": "2026-02-11T13:30:00+00:00", "last": "2099-03-19T12:30:00+00:00"}));

    // The reactions: the import invalidated the cache, so the CPI now has its release.
    let r = calendar::reactions_at(&cal, &market, now).await.unwrap();
    let keys: Vec<&str> = r.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["available", "types", "history"]);
    let cpi = &r["types"][0];
    assert_eq!((cpi["title"].clone(), cpi["count"].clone(), cpi["stored"].clone()), (json!("CPI m/m"), json!(1), json!(1)));
    assert_eq!(cpi["move"]["60"], json!(6.0)); // from 1999.90 just before to 2005.90
    assert_eq!(cpi["surprise"]["above"]["count"], json!(1));
    assert_eq!(calendar::reactions_at(&Calendar::new(None).await, &market, now).await.unwrap(),
               json!({"available": false, "types": [], "history": null}));

    // GET /api/calendar: the snapshot, each event on the chart's clock with its reaction, the history.
    let p = calendar::payload(&cal, &market, now).await.unwrap();
    let keys: Vec<&str> = p.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["editable", "settings", "events", "week", "fetched_at", "error", "loading", "history"]);
    let cpi = &p["week"][0];
    let keys: Vec<&str> = cpi.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["title", "currency", "impact", "time", "forecast", "previous", "chart_time_unix", "reaction"]);
    // 12:30 UTC is 08:30 in New York (summer time from 8 March), so 15:30 on NY+7.
    assert_eq!(cpi["chart_time_unix"], json!(NaiveDateTime::parse_from_str("2026-03-18 15:30", "%Y-%m-%d %H:%M")
        .unwrap().and_utc().timestamp()));
    assert_eq!(cpi["reaction"]["count"], json!(1));
    assert!(cpi["reaction"]["summary"].as_str().unwrap().starts_with("CPI m/m: median 15m range"));
    assert_eq!(p["history"]["stored"], json!(4));
    assert_eq!(p["events"].as_array().unwrap().len(), 2); // this morning's CPI (within 30 minutes) and 2099's
}

/// SQLite, and PostgreSQL when `WEDNESDAY_TEST_POSTGRES` names a server (an admin URL) where a
/// database may be made.
async fn stores() -> Vec<Store> {
    let mut out = vec![store().await];
    if let Ok(url) = std::env::var("WEDNESDAY_TEST_POSTGRES") {
        let admin = Store::connect(&url, Path::new(".")).await.unwrap();
        let db = format!("wed_calendar_{}", std::process::id());
        admin.execute(&format!("DROP DATABASE IF EXISTS {db}"), vec![]).await.unwrap();
        admin.execute(&format!("CREATE DATABASE {db}"), vec![]).await.unwrap();
        out.push(Store::open(&format!("{}/{db}", url.rsplit_once('/').unwrap().0), Path::new(".")).await.unwrap());
    }
    out
}

#[tokio::test]
async fn the_history_keeps_known_figures_and_big_files_are_refused() {
    for store in stores().await {
        history_on(store).await;
    }
}

async fn history_on(store: Store) {
    let ev = json!({"time": "2026-03-04T13:30:00+00:00", "currency": "USD", "title": "CPI m/m", "impact": "High",
                    "forecast": "0.3%"});
    let mut with_actual = ev.clone();
    with_actual["actual"] = json!("0.4%");
    store.calendar_put(&calendar::store_rows(&[with_actual], "csv")).await.unwrap();
    store.calendar_put(&calendar::store_rows(&[ev.clone()], "feed")).await.unwrap(); // the feed has no actual
    let rows = store.calendar_history(&[], &[], None).await.unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!((rows[0].actual.as_deref(), rows[0].source.as_str()), (Some("0.4%"), "feed"));
    // The same release twice in one batch: as one upsert after the other.
    let mut a = ev.clone();
    a["previous"] = json!("0.1%");
    let mut b = ev.clone();
    b["forecast"] = json!(null);
    b["impact"] = json!("Medium");
    store.calendar_put(&calendar::store_rows(&[a, b], "csv")).await.unwrap();
    let r = &store.calendar_history(&["USD".into()], &["Medium".into()], None).await.unwrap()[0];
    assert_eq!((r.forecast.as_deref(), r.previous.as_deref(), r.actual.as_deref(), r.impact.as_deref()),
               (Some("0.3%"), Some("0.1%"), Some("0.4%"), Some("Medium")));
    assert!(store.calendar_history(&["USD".into()], &["High".into()], None).await.unwrap().is_empty());
    assert_eq!(store.calendar_bounds().await.unwrap(), (1, Some(1772631000), Some(1772631000)));

    let big = vec![b' '; calendar::import::MAX_CSV_BYTES + 1];
    assert_eq!(calendar::parse_csv(&big, "UTC"),
               Err(calendar::ImportError::Invalid("The file is larger than 5 MB; split it".into())));
    let mut many = String::from("datetime,currency,title\n");
    for _ in 0..=calendar::import::MAX_CSV_ROWS {
        many.push_str("2026-01-09 08:30,USD,X\n");
    }
    assert_eq!(calendar::parse_csv(many.as_bytes(), "UTC"),
               Err(calendar::ImportError::Invalid("More than 50000 rows; split the file".into())));
}
