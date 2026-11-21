//! Journals as the server sees them (`journal/service.py`): rows in the database, the stats
//! computed on demand and cached, notes, chart snapshots and entries. Same checks and messages
//! as Python, so the dashboard can't tell which server answered.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::hash::{BuildHasher, Hasher};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use super::report;
use super::stats::{self, Bars};
use wednesday_base::pyjson;
use wednesday_base::time::now_iso;
use wednesday_store::{Arg, Store};

pub const MULTI_FEATURE: &str = "journal.multi";
const MARGIN: i64 = 15 * 3600; // load bars this far past a trade's ends: the clocks can differ by hours
const MAX_IMAGES: usize = 12;
const CACHE_TTL: Duration = Duration::from_secs(120);
pub const MAX_REPORT_BYTES: usize = 50 * 1024 * 1024;
const SAMPLE_KEY: &str = "journal.sample";
const SAMPLE_SOURCE: &str = "sample";
const ENTRY_KINDS: [&str; 3] = ["note", "review", "setup"];

/// Why a request was refused: an HTTP status and the message, or a database failure.
#[derive(Debug)]
pub enum Fail {
    Status(u16, String),
    Store(String),
}

impl From<String> for Fail {
    fn from(e: String) -> Self {
        Fail::Store(e)
    }
}

fn fail<T>(status: u16, msg: impl Into<String>) -> Result<T, Fail> {
    Err(Fail::Status(status, msg.into()))
}

/// The stats cache and the per-journal versions that invalidate it (`Journals._cache`).
#[derive(Clone, Default)]
pub struct Cache(Arc<Mutex<CacheInner>>);

#[derive(Default)]
struct CacheInner {
    version: HashMap<String, u64>,
    stats: HashMap<String, (Instant, u64, Value)>,
}

impl Cache {
    fn changed(&self, id: &str) {
        let mut c = self.0.lock().unwrap();
        *c.version.entry(id.to_string()).or_insert(0) += 1;
    }
}

// ---- Python's string helpers ----

/// `str(v)` for a JSON value (strings as they are, numbers as Python prints them).
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(_) => pyjson::dumps(v),
        other => pyjson::dumps(other).replace('"', "'"),
    }
}

/// Python's truthiness.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(m)) => !m.is_empty(),
    }
}

/// `str(v or "")`
fn text_or_empty(v: Option<&Value>) -> String {
    if truthy(v) { py_str(v.unwrap()) } else { String::new() }
}

/// `s[:n]` in characters.
fn take(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn random_hex(n: usize) -> String {
    let mut out = String::new();
    while out.len() < n {
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u128(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos());
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out.truncate(n);
    out
}

/// `list(dict.fromkeys(str(t).strip().lower()[:40] for t in tags if str(t).strip()))[:20]`
fn clean_tags(tags: &[Value]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for t in tags {
        let s = py_str(t);
        let s = s.trim();
        if s.is_empty() {
            continue;
        }
        let tag = take(&s.to_lowercase(), 40);
        if !out.contains(&tag) {
            out.push(tag);
        }
    }
    out.truncate(20);
    out
}

/// What iterating `tags or []` gives in Python: a list's items, a string's characters, a dict's
/// keys; anything else can't be iterated.
fn iterate(v: Option<&Value>) -> Result<Vec<Value>, Fail> {
    if !truthy(v) {
        return Ok(vec![]);
    }
    match v.unwrap() {
        Value::Array(a) => Ok(a.clone()),
        Value::String(s) => Ok(s.chars().map(|c| Value::String(c.to_string())).collect()),
        Value::Object(m) => Ok(m.keys().map(|k| Value::String(k.clone())).collect()),
        Value::Number(n) => fail(422, format!("'{}' object is not iterable", if n.is_f64() { "float" } else { "int" })),
        Value::Bool(_) => fail(422, "'bool' object is not iterable"),
        Value::Null => Ok(vec![]),
    }
}

/// Python's `float(v)` for a JSON value, with its error messages.
fn py_float(v: &Value) -> Result<f64, Fail> {
    match v {
        Value::Number(n) => Ok(n.as_f64().unwrap_or(0.0)),
        Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        Value::String(s) => {
            let t = s.trim().replace('_', "");
            let lower = t.to_ascii_lowercase();
            let parsed = match lower.trim_start_matches(['+', '-']) {
                "inf" | "infinity" | "nan" => lower.parse::<f64>().ok(),
                _ if t.chars().all(|c| c.is_ascii_digit() || "+-.eE".contains(c)) && !t.is_empty() => t.parse::<f64>().ok(),
                _ => None,
            };
            parsed.ok_or_else(|| Fail::Status(422, format!("could not convert string to float: {}",
                                                           wednesday_base::py::py_repr(s))))
        }
        Value::Null => fail(422, "float() argument must be a string or a real number, not 'NoneType'"),
        Value::Array(_) => fail(422, "float() argument must be a string or a real number, not 'list'"),
        Value::Object(_) => fail(422, "float() argument must be a string or a real number, not 'dict'"),
    }
}

/// Python's `int(v)` for a mood: numbers truncate, strings must be whole numbers.
fn py_int(v: &Value) -> Result<i64, Fail> {
    match v {
        Value::Number(n) => match n.as_i64() {
            Some(i) => Ok(i),
            None => {
                let f = n.as_f64().unwrap_or(0.0);
                if f.is_finite() { Ok(f.trunc() as i64) } else { fail(422, "cannot convert float infinity to integer") }
            }
        },
        Value::Bool(b) => Ok(*b as i64),
        Value::String(s) => s.trim().replace('_', "").parse::<i64>().map_err(|_| {
            Fail::Status(422, format!("invalid literal for int() with base 10: {}", wednesday_base::py::py_repr(s)))
        }),
        Value::Null => fail(422, "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"),
        Value::Array(_) => fail(422, "int() argument must be a string, a bytes-like object or a real number, not 'list'"),
        Value::Object(_) => fail(422, "int() argument must be a string, a bytes-like object or a real number, not 'dict'"),
    }
}

/// Python's `round(x)` to an integer: halves go to the even neighbour.
fn round_even(x: f64) -> i64 {
    let r = x.round();
    if (x - x.trunc()).abs() == 0.5 && r % 2.0 != 0.0 { (r - x.signum()) as i64 } else { r as i64 }
}

/// Broker symbol without its suffix: XAUUSD.m, XAUUSDm and XAUUSD# are all XAUUSD; GOLD is XAUUSD.
pub fn canonical(symbol: &str) -> String {
    let s = symbol.trim();
    let first = s.split(['.', '_', '#', '-', '+', '!']).next().unwrap_or("");
    let mut base: String = if first.is_empty() { symbol.to_string() } else { first.to_string() };
    // (?<=[A-Z]{6})[a-z]+$
    let chars: Vec<char> = base.chars().collect();
    let tail = chars.iter().rev().take_while(|c| c.is_ascii_lowercase()).count();
    if tail > 0 && chars.len() >= tail + 6 && chars[chars.len() - tail - 6..chars.len() - tail].iter().all(|c| c.is_ascii_uppercase()) {
        base = chars[..chars.len() - tail].iter().collect();
    }
    let base = base.to_uppercase();
    if ["XAUUSD", "XAU", "GOLD"].contains(&base.as_str()) { "XAUUSD".into() } else { base }
}

// ---- the database ----

fn s(v: Option<&Value>) -> Arg {
    Arg::Str(v.and_then(|v| match v {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        other => Some(py_str(other)),
    }))
}

fn f(v: Option<&Value>) -> Arg {
    Arg::Float(v.and_then(Value::as_f64))
}

fn i(v: Option<&Value>) -> Arg {
    Arg::Int(v.and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))))
}

pub async fn journals(store: &Store) -> Result<Vec<Map<String, Value>>, String> {
    let mut rows = store.rows("journals", "SELECT * FROM journals ORDER BY created_at", vec![]).await?;
    for r in &mut rows {
        let settings = match r.get("settings") {
            Some(Value::String(t)) if !t.is_empty() => serde_json::from_str(t).map_err(|e| format!("journal settings: {e}"))?,
            _ => json!({}),
        };
        r.insert("settings".into(), settings);
    }
    Ok(rows)
}

async fn journal_put(store: &Store, row: &Map<String, Value>) -> Result<(), String> {
    let settings = Arg::Str(truthy(row.get("settings")).then(|| pyjson::dumps(&row["settings"])));
    let mut cols = vec![("id", s(row.get("id"))), ("name", s(row.get("name"))), ("login", s(row.get("login"))),
                        ("server", s(row.get("server"))), ("company", s(row.get("company"))),
                        ("currency", s(row.get("currency"))), ("time_offset", i(row.get("time_offset"))),
                        ("created_at", s(row.get("created_at"))), ("synced_at", s(row.get("synced_at")))];
    if let Some(Value::Bool(b)) = row.get("sample") {
        cols.push(("sample", Arg::Bool(*b)));
    }
    cols.push(("settings", settings));
    store.upsert("journals", cols, &["id"]).await
}

/// Trades, cash or notes of a journal, oldest first.
pub async fn journal_rows(store: &Store, table: &str, id: &str) -> Result<Vec<Map<String, Value>>, String> {
    let order = match table {
        "journal_trades" => " ORDER BY open_time",
        "journal_cash" => " ORDER BY time",
        _ => "",
    };
    let mut rows = store
        .rows(table, &format!("SELECT * FROM {table} WHERE journal_id = ?{order}"), vec![Arg::Str(Some(id.into()))])
        .await?;
    if table == "journal_notes" {
        for r in &mut rows {
            let tags = serde_json::from_str(r["tags"].as_str().unwrap_or("[]")).map_err(|e| format!("note tags: {e}"))?;
            let images = match r.get("images") {
                Some(Value::String(t)) if !t.is_empty() => serde_json::from_str(t).map_err(|e| format!("note images: {e}"))?,
                _ => json!([]),
            };
            r.insert("tags".into(), tags);
            r.insert("images".into(), images);
        }
    }
    Ok(rows)
}

const TRADE_COLS: [&str; 16] = ["journal_id", "id", "position", "symbol", "side", "volume", "open_time", "open_price",
                                "close_time", "close_price", "profit", "commission", "swap", "sl", "tp", "comment"];
const CASH_COLS: [&str; 6] = ["journal_id", "id", "time", "kind", "amount", "comment"];

/// Save imported trades and cash, replacing what was there, in one transaction.
async fn journal_fill(store: &Store, id: &str, trades: &[Value], cash: &[Value]) -> Result<(), String> {
    let jid = Value::String(id.into());
    let mut stmts = vec![
        ("DELETE FROM journal_trades WHERE journal_id = ?".to_string(), vec![Arg::Str(Some(id.into()))]),
        ("DELETE FROM journal_cash WHERE journal_id = ?".to_string(), vec![Arg::Str(Some(id.into()))]),
    ];
    let trade_rows = trades
        .iter()
        .map(|t| {
            let g = |k: &str| if k == "journal_id" { Some(&jid) } else { t.get(k) };
            TRADE_COLS
                .iter()
                .map(|k| match *k {
                    "volume" | "open_price" | "close_price" | "profit" | "commission" | "swap" | "sl" | "tp" => f(g(k)),
                    "open_time" | "close_time" => i(g(k)),
                    _ => s(g(k)),
                })
                .collect()
        })
        .collect();
    stmts.extend(Store::upsert_many("journal_trades", &TRADE_COLS, trade_rows, &["journal_id", "id"]));
    let cash_rows = cash
        .iter()
        .map(|c| {
            let g = |k: &str| if k == "journal_id" { Some(&jid) } else { c.get(k) };
            CASH_COLS
                .iter()
                .map(|k| match *k {
                    "amount" => f(g(k)),
                    "time" => i(g(k)),
                    _ => s(g(k)),
                })
                .collect()
        })
        .collect();
    stmts.extend(Store::upsert_many("journal_cash", &CASH_COLS, cash_rows, &["journal_id", "id"]));
    store.transaction(stmts).await
}

async fn journal_note(store: &Store, id: &str, trade_id: &str, note: &str, tags: &[String], images: Option<Vec<String>>)
    -> Result<(), String> {
    let key = || vec![Arg::Str(Some(id.into())), Arg::Str(Some(trade_id.into()))];
    let images = match images {
        Some(i) => i,
        None => {
            let had = store
                .rows("journal_notes", "SELECT images FROM journal_notes WHERE journal_id = ? AND trade_id = ?", key())
                .await?;
            match had.first().and_then(|r| r.get("images")).and_then(Value::as_str) {
                Some(t) if !t.is_empty() => serde_json::from_str(t).map_err(|e| format!("note images: {e}"))?,
                _ => vec![],
            }
        }
    };
    if note.is_empty() && tags.is_empty() && images.is_empty() {
        store.execute("DELETE FROM journal_notes WHERE journal_id = ? AND trade_id = ?", key()).await?;
        return Ok(());
    }
    let images = (!images.is_empty()).then(|| pyjson::dumps(&json!(images)));
    store
        .upsert("journal_notes", vec![("journal_id", Arg::Str(Some(id.into()))), ("trade_id", Arg::Str(Some(trade_id.into()))),
                                      ("note", Arg::Str(Some(note.into()))), ("tags", Arg::Str(Some(pyjson::dumps(&json!(tags))))),
                                      ("updated_at", Arg::Str(Some(now_iso()))), ("images", Arg::Str(images))],
                &["journal_id", "trade_id"])
        .await
}

pub async fn entries(store: &Store, id: &str) -> Result<Vec<Map<String, Value>>, String> {
    let mut rows = store
        .rows("journal_entries", "SELECT * FROM journal_entries WHERE journal_id = ? ORDER BY created_at DESC",
              vec![Arg::Str(Some(id.into()))])
        .await?;
    for r in &mut rows {
        let tags = serde_json::from_str(r["tags"].as_str().unwrap_or("[]")).map_err(|e| format!("entry tags: {e}"))?;
        r.insert("tags".into(), tags);
        for k in ["setup", "market"] {
            let v = match r.get(k) {
                Some(Value::String(t)) if !t.is_empty() => serde_json::from_str(t).map_err(|e| format!("entry {k}: {e}"))?,
                _ => Value::Null,
            };
            r.insert(k.into(), v);
        }
    }
    Ok(rows)
}

async fn entry_put(store: &Store, row: &Map<String, Value>) -> Result<(), String> {
    let j = |k: &str| Arg::Str(row.get(k).filter(|v| !v.is_null()).map(pyjson::dumps));
    store
        .upsert("journal_entries", vec![("id", s(row.get("id"))), ("journal_id", s(row.get("journal_id"))),
                                        ("kind", s(row.get("kind"))), ("created_at", s(row.get("created_at"))),
                                        ("updated_at", s(row.get("updated_at"))), ("text", s(row.get("text"))),
                                        ("tags", Arg::Str(Some(pyjson::dumps(row.get("tags").unwrap_or(&json!([])))))),
                                        ("mood", i(row.get("mood"))), ("setup", j("setup")), ("market", j("market"))],
                &["id"])
        .await
}

/// (source, symbol, bars) of every stored bar stream.
async fn bar_streams(store: &Store) -> Result<Vec<(String, String, i64)>, String> {
    store.bar_counts().await
}

async fn load_bars(store: &Store, source: &str, symbol: &str, start: i64, end: i64) -> Result<Bars, String> {
    let rows = store
        .rows("m1_bars", "SELECT time, open, high, low, close FROM m1_bars WHERE source = ? AND symbol = ? AND time >= ? AND time <= ? ORDER BY time",
              vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into())), Arg::Int(Some(start)), Arg::Int(Some(end))])
        .await?;
    let col = |k: &str| rows.iter().map(|r| r[k].as_f64().unwrap_or(f64::NAN)).collect::<Vec<_>>();
    Ok(Bars { t: rows.iter().map(|r| r["time"].as_i64().unwrap_or(0)).collect(), open: col("open"), high: col("high"),
              low: col("low"), close: col("close") })
}

// ---- journals ----

/// What the API shows of a journal (`Journals._public`).
pub fn public(j: &Map<String, Value>) -> Value {
    let mut out = j.clone();
    let weekends = j.get("settings").and_then(|s| s.get("show_weekends")).map_or(false, |v| truthy(Some(v)));
    out.insert("show_weekends".into(), Value::Bool(weekends));
    out.insert("settings".into(), Value::Null);
    let off = j.get("time_offset").and_then(Value::as_i64);
    out.insert("time_offset".into(), off.map_or(Value::Null, |o| json!(o as f64 / 3600.0)));
    out.insert("sample".into(), Value::Bool(truthy(j.get("sample"))));
    Value::Object(out)
}

pub async fn get(store: &Store, id: &str) -> Result<Map<String, Value>, Fail> {
    match journals(store).await?.into_iter().find(|j| j["id"] == id) {
        Some(j) => Ok(j),
        None => fail(404, "No such journal"),
    }
}

pub async fn list(store: &Store, features: &[String]) -> Result<Value, Fail> {
    let all: Vec<Value> = journals(store).await?.iter().map(public).collect();
    Ok(json!({"available": true, "journals": all, "multi": features.iter().any(|f| f == MULTI_FEATURE)}))
}

pub async fn create(store: &Store, body: &Map<String, Value>, features: &[String]) -> Result<Value, Fail> {
    let name = take(text_or_empty(body.get("name")).trim(), 120);
    if name.is_empty() {
        return fail(422, "Give the journal a name");
    }
    let login = text_or_empty(body.get("login")).trim().to_string();
    let login = (!login.is_empty()).then_some(login);
    if let Some(l) = &login {
        if !l.chars().all(char::is_numeric) {
            return fail(422, "The account number is the digits MT5 shows for the login, e.g. 51234567");
        }
    }
    let existing = journals(store).await?;
    if existing.iter().any(|j| !truthy(j.get("sample"))) && !features.iter().any(|f| f == MULTI_FEATURE) {
        return fail(402, "One journal is free. More than one needs a plan with multiple journals");
    }
    if let Some(l) = &login {
        if let Some(taken) = existing.iter().find(|j| text_or_empty(j.get("login")) == *l) {
            return fail(422, format!("{} already follows account {l}. Open it instead, or use another account",
                                     py_str(&taken["name"])));
        }
    }
    let row: Map<String, Value> = json!({"id": random_hex(12), "name": name, "login": login, "server": null,
                                         "company": null, "currency": null, "time_offset": null,
                                         "created_at": now_iso(), "synced_at": null, "settings": {}})
        .as_object()
        .unwrap()
        .clone();
    journal_put(store, &row).await?;
    Ok(public(&row))
}

pub async fn update(store: &Store, cache: &Cache, id: &str, body: &Map<String, Value>) -> Result<Value, Fail> {
    let mut j = get(store, id).await?;
    if let Some(v) = body.get("name") {
        let name = take(text_or_empty(Some(v)).trim(), 120);
        if name.is_empty() {
            return fail(422, "Give the journal a name");
        }
        j.insert("name".into(), name.into());
    }
    if let Some(off) = body.get("time_offset") {
        if off.is_null() || off == "" {
            j.insert("time_offset".into(), Value::Null);
        } else {
            let hours = py_float(off)?;
            if !(-14.0..=14.0).contains(&hours) {
                return fail(422, "The clock offset is between -14 and 14 hours");
            }
            j.insert("time_offset".into(), json!(round_even(hours * 3600.0)));
        }
    }
    if let Some(w) = body.get("show_weekends") {
        let Value::Bool(w) = w else { return fail(422, "show_weekends is true or false") };
        let mut settings = j.get("settings").and_then(Value::as_object).cloned().unwrap_or_default();
        settings.insert("show_weekends".into(), Value::Bool(*w));
        j.insert("settings".into(), Value::Object(settings));
    }
    journal_put(store, &j).await?;
    cache.changed(id);
    Ok(public(&j))
}

/// Every snapshot the journal's notes hold.
async fn images(store: &Store, id: &str) -> Result<Vec<String>, String> {
    Ok(journal_rows(store, "journal_notes", id)
        .await?
        .iter()
        .flat_map(|n| n["images"].as_array().cloned().unwrap_or_default())
        .filter_map(|i| i.as_str().map(String::from))
        .collect())
}

pub async fn delete(store: &Store, cache: &Cache, id: &str) -> Result<Value, Fail> {
    let j = get(store, id).await?;
    let shots = images(store, id).await?;
    cache.changed(id);
    let key = || vec![Arg::Str(Some(id.into()))];
    let mut stmts: Vec<(String, Vec<Arg>)> = ["journal_trades", "journal_cash", "journal_notes", "journal_entries"]
        .iter()
        .map(|t| (format!("DELETE FROM {t} WHERE journal_id = ?"), key()))
        .collect();
    stmts.push(("DELETE FROM journals WHERE id = ?".into(), key()));
    store.transaction(stmts).await?;
    if truthy(j.get("sample")) {
        store.execute("DELETE FROM m1_bars WHERE source = ?", vec![Arg::Str(Some(SAMPLE_SOURCE.into()))]).await?;
        store.set_setting(SAMPLE_KEY, &json!({"deleted": true})).await?;
    }
    for sid in shots {
        delete_snapshot(store, &sid);
    }
    Ok(json!({"deleted": true}))
}

// ---- chart snapshots (snapshots.py) ----

fn snapshot_path(store: &Store, sid: &str) -> Option<PathBuf> {
    crate::snapshots::path(&crate::snapshots::dir(store.home()), sid)
}

fn delete_snapshot(store: &Store, sid: &str) {
    crate::snapshots::delete(&crate::snapshots::dir(store.home()), sid)
}

// ---- import ----

pub fn check_writable(j: &Map<String, Value>) -> Result<(), Fail> {
    if truthy(j.get("sample")) {
        return fail(409, "The sample portfolio is made-up data, so a report can't be imported into it. \
                          Make a journal of your own for your account");
    }
    Ok(())
}

pub async fn import_report(store: &Store, cache: &Cache, id: &str, data: &[u8]) -> Result<Value, Fail> {
    let mut j = get(store, id).await?;
    check_writable(&j)?;
    if data.len() > MAX_REPORT_BYTES {
        return fail(413, "The report is larger than 50 MB");
    }
    let parsed = report::parse_report(data).map_err(|e| Fail::Status(422, e))?;
    let acc = parsed["account"].as_object().cloned().unwrap_or_default();
    let login = acc.get("login").filter(|v| truthy(Some(v)));
    if let (true, Some(l)) = (truthy(j.get("login")), login) {
        if py_str(l) != py_str(&j["login"]) {
            return fail(422, format!("This journal is account {}, but that history is account {}. Make a journal per account",
                                     py_str(&j["login"]), py_str(l)));
        }
    }
    let trades = parsed["trades"].as_array().cloned().unwrap_or_default();
    let cash = parsed["cash"].as_array().cloned().unwrap_or_default();
    journal_fill(store, id, &trades, &cash).await?;
    for k in ["login", "server", "company", "currency"] {
        if let Some(v) = acc.get(k).filter(|v| truthy(Some(v))) {
            j.insert(k.into(), v.clone());
        }
    }
    j.insert("synced_at".into(), now_iso().into());
    journal_put(store, &j).await?;
    cache.changed(id);
    Ok(json!({"trades": trades.len(), "cash": cash.len(), "journal": public(&j)}))
}

// ---- notes and snapshots on trades ----

pub async fn set_note(store: &Store, cache: &Cache, id: &str, trade_id: &str, body: &Map<String, Value>) -> Result<Value, Fail> {
    get(store, id).await?;
    let tags = match body.get("tags") {
        v if !truthy(v) => vec![],
        Some(Value::Array(a)) => a.clone(),
        _ => return fail(422, "tags is a list"),
    };
    let note = take(text_or_empty(body.get("note")).trim(), 5000);
    let tags = clean_tags(&tags);
    journal_note(store, id, trade_id, &note, &tags, None).await?;
    cache.changed(id);
    Ok(json!({"trade_id": trade_id, "note": note, "tags": tags}))
}

async fn note_of(store: &Store, id: &str, trade_id: &str) -> Result<(String, Vec<String>, Vec<String>), String> {
    let rows = journal_rows(store, "journal_notes", id).await?;
    Ok(match rows.iter().find(|n| n["trade_id"] == trade_id) {
        Some(n) => {
            let strs = |k: &str| n[k].as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
            (n["note"].as_str().unwrap_or("").to_string(), strs("tags"), strs("images"))
        }
        None => (String::new(), vec![], vec![]),
    })
}

pub async fn attach_image(store: &Store, cache: &Cache, id: &str, trade_id: &str, body: &Map<String, Value>) -> Result<Value, Fail> {
    get(store, id).await?;
    let sid = text_or_empty(body.get("snapshot_id"));
    if snapshot_path(store, &sid).is_none() {
        return fail(404, "No such snapshot: upload it first");
    }
    if !journal_rows(store, "journal_trades", id).await?.iter().any(|t| t["id"] == trade_id) {
        return fail(404, "No such trade in this journal");
    }
    let (note, tags, mut images) = note_of(store, id, trade_id).await?;
    if !images.contains(&sid) {
        images.push(sid);
        let n = images.len();
        images = images.split_off(n.saturating_sub(MAX_IMAGES));
    }
    journal_note(store, id, trade_id, &note, &tags, Some(images.clone())).await?;
    cache.changed(id);
    Ok(json!({"trade_id": trade_id, "images": images}))
}

pub async fn detach_image(store: &Store, cache: &Cache, id: &str, trade_id: &str, sid: &str) -> Result<Value, Fail> {
    get(store, id).await?;
    let (note, tags, images) = note_of(store, id, trade_id).await?;
    if !images.iter().any(|i| i == sid) {
        return fail(404, "That trade has no such snapshot");
    }
    let images: Vec<String> = images.into_iter().filter(|i| i != sid).collect();
    journal_note(store, id, trade_id, &note, &tags, Some(images.clone())).await?;
    cache.changed(id);
    delete_snapshot(store, sid);
    Ok(json!({"trade_id": trade_id, "images": images}))
}

// ---- entries ----

/// `_clean_entry`: the text, tags and mood as saved.
fn clean_entry(text: Option<&Value>, tags: Option<&Value>, mood: Option<&Value>) -> Result<(String, Vec<String>, Option<i64>), Fail> {
    let text = take(text_or_empty(text).trim(), 10_000);
    let tags = clean_tags(&iterate(tags)?);
    let mood = match mood {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.is_empty() => None,
        Some(m) => {
            let m = py_int(m)?;
            if !(1..=5).contains(&m) {
                return fail(422, "Mood is 1 to 5");
            }
            Some(m)
        }
    };
    Ok((text, tags, mood))
}

/// `POST .../entries` (`Journals.add_entry`): a note, a session review, or a setup from the
/// ladder, with `market` (the market now, `journal::market::freeze` in server-rs) frozen into it.
pub async fn add_entry(store: &Store, id: &str, body: &Map<String, Value>, market: Option<Value>) -> Result<Value, Fail> {
    get(store, id).await?;
    let kind = if truthy(body.get("kind")) { py_str(&body["kind"]) } else { "note".into() };
    if !entry_kind_ok(&kind) {
        return fail(422, format!("An entry is one of {}", ENTRY_KINDS.join(", ")));
    }
    let (text, tags, mood) = clean_entry(body.get("text"), body.get("tags"), body.get("mood"))?;
    if kind != "setup" && text.is_empty() {
        return fail(422, "Write something first");
    }
    let setup = body.get("setup").cloned().unwrap_or(Value::Null);
    if kind == "setup" && !setup.is_object() {
        return fail(422, "Journal a setup from a row of the ladder");
    }
    let mut row = Map::new();
    row.insert("id".into(), random_hex(12).into());
    row.insert("journal_id".into(), id.into());
    row.insert("kind".into(), kind.into());
    row.insert("created_at".into(), wednesday_base::time::py_iso_utc(chrono::Utc::now()).into());
    row.insert("updated_at".into(), Value::Null);
    row.insert("text".into(), text.into());
    row.insert("tags".into(), json!(tags));
    row.insert("mood".into(), json!(mood));
    row.insert("setup".into(), setup);
    row.insert("market".into(), market.unwrap_or(Value::Null));
    entry_put(store, &row).await?;
    Ok(Value::Object(row))
}

pub async fn list_entries(store: &Store, id: &str) -> Result<Value, Fail> {
    get(store, id).await?;
    Ok(json!({"entries": entries(store, id).await?}))
}

pub async fn update_entry(store: &Store, id: &str, entry_id: &str, body: &Map<String, Value>) -> Result<Value, Fail> {
    get(store, id).await?;
    let Some(mut found) = entries(store, id).await?.into_iter().find(|e| e["id"] == entry_id) else {
        return fail(404, "No such entry");
    };
    let pick = |k: &str| body.get(k).or_else(|| found.get(k)).cloned();
    let (text, tags, mood) = clean_entry(pick("text").as_ref(), pick("tags").as_ref(), pick("mood").as_ref())?;
    found.insert("text".into(), text.into());
    found.insert("tags".into(), json!(tags));
    found.insert("mood".into(), json!(mood));
    found.insert("updated_at".into(), now_iso().into());
    entry_put(store, &found).await?;
    Ok(Value::Object(found))
}

pub async fn delete_entry(store: &Store, id: &str, entry_id: &str) -> Result<Value, Fail> {
    get(store, id).await?;
    let n = store
        .execute("DELETE FROM journal_entries WHERE journal_id = ? AND id = ?",
                 vec![Arg::Str(Some(id.into())), Arg::Str(Some(entry_id.into()))])
        .await?;
    Ok(json!({"deleted": n > 0}))
}

/// The kinds an entry can be, for the check Python makes before writing one.
pub fn entry_kind_ok(kind: &str) -> bool {
    ENTRY_KINDS.contains(&kind)
}

// ---- stats ----

/// (symbol -> M1 bars, symbol -> "source:symbol") for the trades' symbols. The sample portfolio
/// reads its own bars only, and real journals never read them.
async fn price_bars(store: &Store, trades: &[Map<String, Value>], sample: bool)
    -> Result<(BTreeMap<String, Bars>, Map<String, Value>), String> {
    let stored: Vec<(String, String, i64)> =
        bar_streams(store).await?.into_iter().filter(|(src, _, _)| (src == SAMPLE_SOURCE) == sample).collect();
    let symbols: BTreeSet<String> = trades.iter().filter_map(|t| t["symbol"].as_str().map(String::from)).collect();
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs() as i64;
    let (mut found, mut used) = (BTreeMap::new(), Map::new());
    for sym in symbols {
        let want = canonical(&sym);
        let mut matches: Vec<&(String, String, i64)> = stored.iter().filter(|s| s.1 == sym).collect();
        if matches.is_empty() {
            matches = stored.iter().filter(|s| canonical(&s.1) == want).collect();
        }
        // Python's max(): the first of the largest.
        let Some(best) = matches.iter().fold(None::<&&(String, String, i64)>, |b, m| match b {
            Some(b) if b.2 >= m.2 => Some(b),
            _ => Some(m),
        }) else { continue };
        let mine: Vec<&Map<String, Value>> = trades.iter().filter(|t| t["symbol"] == sym.as_str()).collect();
        let start = mine.iter().filter_map(|t| t["open_time"].as_i64()).min().unwrap_or(0) - MARGIN;
        let end = mine.iter().map(|t| t["close_time"].as_i64().filter(|c| *c != 0).unwrap_or(now + 86400)).max().unwrap_or(0) + MARGIN;
        let bars = load_bars(store, &best.0, &best.1, start, end).await?;
        if !bars.t.is_empty() {
            used.insert(sym.clone(), format!("{}:{}", best.0, best.1).into());
            found.insert(sym, bars);
        }
    }
    Ok((found, used))
}

pub async fn stats(store: &Store, cache: &Cache, id: &str) -> Result<Value, Fail> {
    let j = get(store, id).await?;
    let version = {
        let c = cache.0.lock().unwrap();
        let v = c.version.get(id).copied().unwrap_or(0);
        if let Some((at, ver, hit)) = c.stats.get(id) {
            if *ver == v && at.elapsed() < CACHE_TTL {
                return Ok(hit.clone());
            }
        }
        v
    };
    let trades = journal_rows(store, "journal_trades", id).await?;
    let mut cash = journal_rows(store, "journal_cash", id).await?;
    let notes: HashMap<String, Map<String, Value>> = journal_rows(store, "journal_notes", id)
        .await?
        .into_iter()
        .map(|n| (n["trade_id"].as_str().unwrap_or("").to_string(), n))
        .collect();
    let (bars, used) = price_bars(store, &trades, truthy(j.get("sample"))).await?;
    let mut result = stats::analyse(&trades, &cash, &bars, j.get("time_offset").and_then(Value::as_i64));
    if let Some(rows) = result.get_mut("trades").and_then(Value::as_array_mut) {
        for t in rows {
            let Some(t) = t.as_object_mut() else { continue };
            let n = t.get("id").and_then(Value::as_str).and_then(|i| notes.get(i));
            let (note, tags, images) = match n {
                Some(n) => (n["note"].clone(), n["tags"].clone(), n["images"].clone()),
                None => (json!(""), json!([]), json!([])),
            };
            t.insert("note".into(), note);
            t.insert("tags".into(), tags);
            t.insert("images".into(), images);
            t.shift_remove("journal_id");
        }
    }
    if let Some(v) = result.get_mut("verification").and_then(Value::as_object_mut) {
        v.insert("prices_from".into(), Value::Object(used));
    }
    for c in &mut cash {
        c.shift_remove("journal_id");
    }
    let obj = result.as_object_mut().expect("analyse gives an object");
    obj.insert("journal".into(), public(&j));
    obj.insert("cash".into(), json!(cash));
    cache.0.lock().unwrap().stats.insert(id.to_string(), (Instant::now(), version, result.clone()));
    Ok(result)
}

// ---- the trades as CSV ----

/// A value as Python's `csv` module writes it.
fn csv_field(v: Option<&Value>) -> String {
    let raw = match v {
        None | Some(Value::Null) => String::new(),
        Some(v) => py_str(v),
    };
    if raw.contains([',', '"', '\r', '\n']) {
        format!("\"{}\"", raw.replace('"', "\"\""))
    } else {
        raw
    }
}

fn iso_utc(t: Option<&Value>) -> Value {
    match t.and_then(Value::as_i64).filter(|t| *t != 0) {
        Some(t) => chrono::DateTime::from_timestamp(t, 0)
            .map(|d| d.format("%Y-%m-%d %H:%M:%S").to_string())
            .unwrap_or_default()
            .into(),
        None => "".into(),
    }
}

/// The trades with their notes as CSV, and a file name from the journal's.
pub async fn trades_csv(store: &Store, cache: &Cache, id: &str) -> Result<(String, String), Fail> {
    let j = get(store, id).await?;
    let data = stats(store, cache, id).await?;
    let cols = ["id", "position", "symbol", "side", "volume", "open_time", "open_price", "close_time", "close_price",
                "profit", "commission", "swap", "net", "mae", "mfe", "verified", "price_ok", "tags", "note"];
    let mut out = cols.join(",") + "\r\n";
    for t in data["trades"].as_array().cloned().unwrap_or_default() {
        let mut row = t.as_object().cloned().unwrap_or_default();
        row.insert("open_time".into(), iso_utc(t.get("open_time")));
        row.insert("close_time".into(), iso_utc(t.get("close_time")));
        let tags: Vec<String> = t["tags"].as_array().map(|a| a.iter().map(py_str).collect()).unwrap_or_default();
        row.insert("tags".into(), tags.join(" ").into());
        out += &cols.iter().map(|c| csv_field(row.get(*c))).collect::<Vec<_>>().join(",");
        out += "\r\n";
    }
    let name: String = py_str(&j["name"]).chars().map(|c| if c.is_alphanumeric() { c } else { '-' }).collect();
    let name = name.trim_matches('-');
    Ok((out, format!("wednesday-{}-trades.csv", if name.is_empty() { "journal" } else { name })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn broker_symbols_as_python_reads_them() {
        for (sym, want) in [("XAUUSD.m", "XAUUSD"), ("XAUUSDm", "XAUUSD"), ("XAUUSD#", "XAUUSD"), ("GOLD", "XAUUSD"),
                            ("gold", "XAUUSD"), ("EURUSD.pro", "EURUSD"), ("eurusd", "EURUSD"), (".x", ".X"),
                            ("XAUUSDmicro", "XAUUSD"), ("XAUUSmicro", "XAUUSMICRO")] {
            assert_eq!(canonical(sym), want, "{sym}");
        }
    }

    #[test]
    fn python_numbers_and_tags() {
        assert_eq!(round_even(2.5), 2);
        assert_eq!(round_even(3.5), 4);
        assert_eq!(round_even(-2.5), -2);
        assert_eq!(round_even(1.4), 1);
        assert!(matches!(py_float(&json!("3.5")), Ok(f) if f == 3.5));
        assert!(matches!(py_float(&json!("x")), Err(Fail::Status(422, m)) if m == "could not convert string to float: 'x'"));
        assert_eq!(clean_tags(&[json!(" Breakout "), json!("breakout"), json!(""), json!(5)]), vec!["breakout", "5"]);
        assert_eq!(csv_field(Some(&json!("a,b \"c\""))), "\"a,b \"\"c\"\"\"");
        assert_eq!(csv_field(Some(&json!(2.0))), "2.0");
        assert_eq!(csv_field(Some(&json!(true))), "True");
    }
}
