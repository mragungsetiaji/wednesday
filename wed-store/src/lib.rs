//! The database, as `storage.py` keeps it (#96): SQLite by default, PostgreSQL with `XAU_DB_URL`.
//!
//! The tables are made from `schema.json`, which `tests/test_rust_schema.py` generates from
//! `storage.metadata`, so both servers create and read the same schema; a column a newer version
//! added is added to an older database, as Python does. Rows come back as JSON objects typed by
//! that schema (booleans as booleans, whatever the database stores).
//!
//! Python and Rust share the database while routes move over: SQLite runs in WAL mode with a busy
//! timeout, so one can read while the other writes.

use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::time::Duration;

use serde_json::{Map, Value};
use sqlx::postgres::{PgPoolOptions, PgRow};
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteRow, SqliteSynchronous};
use sqlx::{PgPool, Row, SqlitePool};

const SCHEMA: &str = include_str!("../schema.json");

/// The data migrations, as `MIGRATIONS` in storage.py: never renumber or edit an applied one.
const MIGRATIONS: [(i64, &str); 2] = [(1, "bars_from_m1_bars"), (2, "seed_markets")];

/// The dropdown's first markets (`DEFAULT_MARKETS` in storage.py): symbol, label, name, venue.
pub const DEFAULT_MARKETS: [(&str, &str, &str, &str); 7] = [
    ("FX:XAUUSD", "XAU", "Gold spot", "FXCM"),
    ("OANDA:XAUUSD", "XAU", "Gold spot", "OANDA"),
    ("FX:XAGUSD", "XAG", "Silver spot", "FXCM"),
    ("TVC:DXY", "DXY", "US Dollar Index", "TVC"),
    ("OANDA:NAS100USD", "NQ", "Nasdaq 100", "OANDA"),
    ("OANDA:SPX500USD", "S&P", "S&P 500", "OANDA"),
    ("OANDA:US30USD", "DJI", "Dow Jones 30", "OANDA"),
];

#[derive(Clone, Debug)]
pub enum Arg {
    Str(Option<String>),
    Int(Option<i64>),
    Float(Option<f64>),
    Bool(bool),
}

#[derive(Clone)]
enum Pool {
    Sqlite(SqlitePool),
    Postgres(PgPool),
}

#[derive(Clone)]
pub struct Store {
    pool: Pool,
    schema: Value,
    home: PathBuf,
    url: String,
}

/// `postgresql+psycopg://u:p@host/db` -> `postgres://u:p@host/db`; Some for a PostgreSQL URL.
fn postgres_url(url: &str) -> Option<String> {
    let (scheme, rest) = url.split_once("://")?;
    let base = scheme.split('+').next()?;
    matches!(base, "postgresql" | "postgres").then(|| format!("postgres://{rest}"))
}

/// The SQLite file of a SQLAlchemy URL (`sqlite:///data/xau.db` relative to `home`,
/// `sqlite:////abs/x.db` absolute); None for an in-memory database.
fn sqlite_path(url: &str, home: &Path) -> Option<std::path::PathBuf> {
    let rest = url.strip_prefix("sqlite://")?;
    let file = rest.strip_prefix('/').unwrap_or(rest);
    let file = file.split('?').next().unwrap_or("");
    if file.is_empty() || file == ":memory:" {
        return None;
    }
    let p = Path::new(file);
    Some(if p.is_absolute() { p.to_path_buf() } else { home.join(p) })
}

impl Store {
    /// Open the database at a SQLAlchemy URL, relative paths from `home`, and make missing tables.
    pub async fn open(url: &str, home: &Path) -> Result<Store, String> {
        let store = Store::connect(url, home).await?;
        store.create_tables().await?;
        store.migrate().await?;
        Ok(store)
    }

    /// Connect without touching the tables (to administer a server, say).
    pub async fn connect(url: &str, home: &Path) -> Result<Store, String> {
        let schema: Value = serde_json::from_str(SCHEMA).expect("schema.json");
        let pool = if let Some(pg) = postgres_url(url) {
            let pool = PgPoolOptions::new()
                .max_connections(4)
                .acquire_timeout(Duration::from_secs(10))
                .connect(&pg)
                .await
                .map_err(|e| format!("couldn't open the PostgreSQL database: {e}"))?;
            Pool::Postgres(pool)
        } else if url.starts_with("sqlite:") {
            let options = match sqlite_path(url, home) {
                Some(path) => {
                    if let Some(dir) = path.parent() {
                        let _ = std::fs::create_dir_all(dir);
                    }
                    SqliteConnectOptions::new().filename(path).create_if_missing(true)
                }
                None => SqliteConnectOptions::from_str("sqlite::memory:").map_err(|e| e.to_string())?,
            }
            .journal_mode(SqliteJournalMode::Wal) // the API reads while the scan writes
            .synchronous(SqliteSynchronous::Normal)
            .busy_timeout(Duration::from_secs(5));
            // One connection for an in-memory database, which is per connection.
            let max = if sqlite_path(url, home).is_some() { 4 } else { 1 };
            let pool = SqlitePoolOptions::new()
                .max_connections(max)
                .connect_with(options)
                .await
                .map_err(|e| format!("couldn't open the SQLite database: {e}"))?;
            Pool::Sqlite(pool)
        } else {
            return Err(format!("unsupported database URL scheme: {}", url.split(':').next().unwrap_or("")));
        };
        Ok(Store { pool, schema, home: home.to_path_buf(), url: url.to_string() })
    }

    /// The home folder: relative paths (`data/snapshots`) resolve in it, as Python's working folder.
    pub fn home(&self) -> &Path {
        &self.home
    }

    /// Backend and URL without the password, for the settings page (`Store.describe`).
    pub fn describe(&self) -> Value {
        let url = match self.url.split_once("://") {
            Some((scheme, rest)) => match rest.split_once('@') {
                Some((auth, host)) if auth.contains(':') => {
                    format!("{scheme}://{}:***@{host}", auth.split(':').next().unwrap_or(""))
                }
                _ => self.url.clone(),
            },
            None => self.url.clone(),
        };
        serde_json::json!({"backend": self.backend(), "url": url})
    }

    pub fn backend(&self) -> &'static str {
        match self.pool {
            Pool::Sqlite(_) => "sqlite",
            Pool::Postgres(_) => "postgresql",
        }
    }

    fn tables(&self) -> &Vec<Value> {
        self.schema["tables"].as_array().unwrap()
    }

    fn table(&self, name: &str) -> &Value {
        self.tables().iter().find(|t| t["name"] == name).unwrap_or_else(|| panic!("no table {name} in schema.json"))
    }

    async fn create_tables(&self) -> Result<(), String> {
        let d = self.backend();
        for t in self.tables().clone() {
            self.execute(t["create"][d].as_str().unwrap(), vec![]).await?;
            for ix in t["indexes"][d].as_array().unwrap() {
                self.execute(ix.as_str().unwrap(), vec![]).await?;
            }
            // Nullable columns a newer version added: add them to an older table.
            let have = self.columns(t["name"].as_str().unwrap()).await?;
            for c in t["columns"].as_array().unwrap() {
                let name = c["name"].as_str().unwrap();
                if c["nullable"] == true && !have.iter().any(|h| h == name) {
                    let sql = format!("ALTER TABLE {} ADD COLUMN \"{name}\" {}", t["name"].as_str().unwrap(),
                                      c[d].as_str().unwrap());
                    self.execute(&sql, vec![]).await?;
                }
            }
        }
        Ok(())
    }

    /// Apply the data migrations this database hasn't had yet (`MIGRATIONS` in storage.py, the
    /// same versions and names), each in one transaction with its `schema_migrations` row.
    async fn migrate(&self) -> Result<(), String> {
        let done: Vec<i64> = self
            .rows("schema_migrations", "SELECT version FROM schema_migrations", vec![])
            .await?
            .iter()
            .filter_map(|r| r.get("version")?.as_i64())
            .collect();
        for (version, name) in MIGRATIONS {
            if done.contains(&version) {
                continue;
            }
            let mut stmts = match version {
                // Versions before 0.5 kept only M1 bars, in `m1_bars`: move them to `bars` as "1m".
                // (The WHERE: SQLite reads ON after a bare SELECT as a join.)
                1 if !self.columns("m1_bars").await?.is_empty() => vec![
                    ("INSERT INTO bars (source, symbol, tf, time, open, high, low, close, volume) \
                      SELECT source, symbol, '1m', time, open, high, low, close, volume FROM m1_bars WHERE 1 = 1 \
                      ON CONFLICT DO NOTHING".to_string(), vec![]),
                    ("DROP TABLE m1_bars".to_string(), vec![]),
                ],
                // The dropdown's first markets, when the list is empty.
                2 if self.rows("markets", "SELECT symbol FROM markets LIMIT 1", vec![]).await?.is_empty() =>
                {
                    let now = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string();
                    DEFAULT_MARKETS.iter().enumerate().map(|(i, (symbol, label, name, venue))| (
                        "INSERT INTO markets (source, symbol, label, name, venue, position, keep_live, added_at) \
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)".to_string(),
                        vec![Arg::Str(Some("tradingview".into())), Arg::Str(Some((*symbol).into())),
                             Arg::Str(Some((*label).into())), Arg::Str(Some((*name).into())),
                             Arg::Str(Some((*venue).into())), Arg::Int(Some(i as i64)), Arg::Bool(false),
                             Arg::Str(Some(now.clone()))],
                    )).collect()
                }
                _ => vec![],
            };
            let now = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string();
            stmts.push(("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)".into(),
                        vec![Arg::Int(Some(version)), Arg::Str(Some(name.into())), Arg::Str(Some(now))]));
            self.transaction(stmts).await?;
        }
        Ok(())
    }

    async fn columns(&self, table: &str) -> Result<Vec<String>, String> {
        let err = |e: sqlx::Error| e.to_string();
        Ok(match &self.pool {
            Pool::Sqlite(p) => sqlx::query(&format!("PRAGMA table_info(\"{table}\")"))
                .fetch_all(p)
                .await
                .map_err(err)?
                .iter()
                .map(|r| r.get::<String, _>("name"))
                .collect(),
            Pool::Postgres(p) => sqlx::query(
                "SELECT column_name::text FROM information_schema.columns WHERE table_name = $1 AND table_schema = current_schema()",
            )
            .bind(table)
            .fetch_all(p)
            .await
            .map_err(err)?
            .iter()
            .map(|r| r.get::<String, _>(0))
            .collect(),
        })
    }

    /// `?` placeholders, as PostgreSQL's `$1, $2, ...` there.
    fn sql(&self, sql: &str) -> String {
        match self.pool {
            Pool::Sqlite(_) => sql.to_string(),
            Pool::Postgres(_) => {
                let mut out = String::with_capacity(sql.len() + 8);
                let mut n = 0;
                for ch in sql.chars() {
                    if ch == '?' {
                        n += 1;
                        out.push_str(&format!("${n}"));
                    } else {
                        out.push(ch);
                    }
                }
                out
            }
        }
    }

    /// Run a statement; the number of rows it changed.
    pub async fn execute(&self, sql: &str, args: Vec<Arg>) -> Result<u64, String> {
        let sql = self.sql(sql);
        macro_rules! run {
            ($pool:expr) => {{
                let mut q = sqlx::query(&sql);
                for a in args {
                    q = match a {
                        Arg::Str(v) => q.bind(v),
                        Arg::Int(v) => q.bind(v),
                        Arg::Float(v) => q.bind(v),
                        Arg::Bool(v) => q.bind(v),
                    };
                }
                q.execute($pool).await.map(|r| r.rows_affected()).map_err(|e| e.to_string())
            }};
        }
        match &self.pool {
            Pool::Sqlite(p) => run!(p),
            Pool::Postgres(p) => run!(p),
        }
    }

    /// Run statements in one transaction: all of them, or none.
    pub async fn transaction(&self, stmts: Vec<(String, Vec<Arg>)>) -> Result<(), String> {
        macro_rules! run {
            ($pool:expr) => {{
                let mut tx = $pool.begin().await.map_err(|e| e.to_string())?;
                for (sql, args) in stmts {
                    let sql = self.sql(&sql);
                    let mut q = sqlx::query(&sql);
                    for a in args {
                        q = match a {
                            Arg::Str(v) => q.bind(v),
                            Arg::Int(v) => q.bind(v),
                            Arg::Float(v) => q.bind(v),
                            Arg::Bool(v) => q.bind(v),
                        };
                    }
                    q.execute(&mut *tx).await.map_err(|e| e.to_string())?;
                }
                tx.commit().await.map_err(|e| e.to_string())
            }};
        }
        match &self.pool {
            Pool::Sqlite(p) => run!(p),
            Pool::Postgres(p) => run!(p),
        }
    }

    /// Rows of `table` as JSON objects, typed by its schema.
    pub async fn rows(&self, table: &str, sql: &str, args: Vec<Arg>) -> Result<Vec<Map<String, Value>>, String> {
        let sql = self.sql(sql);
        let cols: Vec<(String, String)> = self.table(table)["columns"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| (c["name"].as_str().unwrap().to_string(), c["sqlite"].as_str().unwrap().to_uppercase()))
            .collect();
        macro_rules! fetch {
            ($pool:expr, $row:ty) => {{
                let mut q = sqlx::query(&sql);
                for a in args {
                    q = match a {
                        Arg::Str(v) => q.bind(v),
                        Arg::Int(v) => q.bind(v),
                        Arg::Float(v) => q.bind(v),
                        Arg::Bool(v) => q.bind(v),
                    };
                }
                let rows: Vec<$row> = q.fetch_all($pool).await.map_err(|e| e.to_string())?;
                rows.iter()
                    .map(|r| {
                        let mut m = Map::new();
                        for (name, kind) in &cols {
                            let Ok(_) = r.try_column(name.as_str()) else { continue };
                            let v: Value = if kind.starts_with("BOOL") {
                                r.try_get::<Option<bool>, _>(name.as_str()).map_err(|e| e.to_string())?.into()
                            } else if kind.contains("INT") {
                                r.try_get::<Option<i64>, _>(name.as_str()).map_err(|e| e.to_string())?.into()
                            } else if kind.starts_with("FLOAT") || kind.starts_with("REAL") || kind.starts_with("DOUBLE") {
                                r.try_get::<Option<f64>, _>(name.as_str()).map_err(|e| e.to_string())?.into()
                            } else {
                                r.try_get::<Option<String>, _>(name.as_str()).map_err(|e| e.to_string())?.into()
                            };
                            m.insert(name.clone(), v);
                        }
                        Ok(m)
                    })
                    .collect::<Result<Vec<_>, String>>()
            }};
        }
        match &self.pool {
            Pool::Sqlite(p) => fetch!(p, SqliteRow),
            Pool::Postgres(p) => fetch!(p, PgRow),
        }
    }

    /// Insert or update by `keys` (`_upsert` in storage.py).
    pub async fn upsert(&self, table: &str, row: Vec<(&str, Arg)>, keys: &[&str]) -> Result<(), String> {
        let names: Vec<&str> = row.iter().map(|(k, _)| *k).collect();
        let sql = Self::upsert_sql(table, &names, 1, keys);
        self.execute(&sql, row.into_iter().map(|(_, a)| a).collect()).await.map(|_| ())
    }

    /// `INSERT ... VALUES (...) x rows ... ON CONFLICT (keys) DO UPDATE` for `names` columns.
    fn upsert_sql(table: &str, names: &[&str], rows: usize, keys: &[&str]) -> String {
        let quoted = |n: &str| format!("\"{n}\"");
        let updates: Vec<String> = names
            .iter()
            .filter(|n| !keys.contains(n))
            .map(|n| format!("{} = excluded.{}", quoted(n), quoted(n)))
            .collect();
        let one = format!("({})", vec!["?"; names.len()].join(", "));
        format!(
            "INSERT INTO {table} ({}) VALUES {} ON CONFLICT ({}) DO {}",
            names.iter().map(|n| quoted(n)).collect::<Vec<_>>().join(", "),
            vec![one; rows].join(", "),
            keys.iter().map(|n| quoted(n)).collect::<Vec<_>>().join(", "),
            if updates.is_empty() { "NOTHING".to_string() } else { format!("UPDATE SET {}", updates.join(", ")) },
        )
    }

    /// Upserts of many rows with the same columns, as statements for [`Store::transaction`]
    /// (a few dozen rows a statement, under SQLite's 999 parameters).
    pub fn upsert_many(table: &str, names: &[&str], rows: Vec<Vec<Arg>>, keys: &[&str]) -> Vec<(String, Vec<Arg>)> {
        let per = (900 / names.len().max(1)).max(1);
        rows.chunks(per)
            .map(|chunk| (Self::upsert_sql(table, names, chunk.len(), keys), chunk.iter().flatten().cloned().collect()))
            .collect()
    }

    /// The tables as the database has them (columns with type, nullability and key; indexes),
    /// to compare a database Python made with one Rust made.
    pub async fn describe_schema(&self) -> Result<Value, String> {
        let mut out = Map::new();
        let err = |e: sqlx::Error| e.to_string();
        for t in self.tables() {
            let name = t["name"].as_str().unwrap();
            let (columns, indexes): (Vec<Value>, Vec<Value>) = match &self.pool {
                Pool::Sqlite(p) => {
                    let cols = sqlx::query(&format!("PRAGMA table_info(\"{name}\")")).fetch_all(p).await.map_err(err)?;
                    let columns = cols.iter().map(|r| serde_json::json!([
                        r.get::<String, _>("name"), r.get::<String, _>("type"), r.get::<i64, _>("notnull"), r.get::<i64, _>("pk")
                    ])).collect();
                    let mut indexes = Vec::new();
                    for ix in sqlx::query(&format!("PRAGMA index_list(\"{name}\")")).fetch_all(p).await.map_err(err)? {
                        let ix_name: String = ix.get("name");
                        let cols: Vec<String> = sqlx::query(&format!("PRAGMA index_info(\"{ix_name}\")"))
                            .fetch_all(p).await.map_err(err)?.iter().map(|r| r.get::<String, _>("name")).collect();
                        indexes.push(serde_json::json!([ix_name, ix.get::<i64, _>("unique"), cols]));
                    }
                    indexes.sort_by_key(|v| v.to_string());
                    (columns, indexes)
                }
                Pool::Postgres(p) => {
                    let cols = sqlx::query(
                        "SELECT column_name::text, data_type::text, character_maximum_length, is_nullable::text \
                         FROM information_schema.columns WHERE table_name = $1 AND table_schema = current_schema() \
                         ORDER BY ordinal_position")
                        .bind(name).fetch_all(p).await.map_err(err)?;
                    let columns = cols.iter().map(|r| serde_json::json!([
                        r.get::<String, _>(0), r.get::<String, _>(1), r.get::<Option<i32>, _>(2), r.get::<String, _>(3)
                    ])).collect();
                    let mut indexes: Vec<Value> = sqlx::query(
                        "SELECT indexname::text, indexdef::text FROM pg_indexes WHERE tablename = $1 AND schemaname = current_schema()")
                        .bind(name).fetch_all(p).await.map_err(err)?
                        .iter().map(|r| serde_json::json!([r.get::<String, _>(0), r.get::<String, _>(1)])).collect();
                    indexes.sort_by_key(|v| v.to_string());
                    (columns, indexes)
                }
            };
            out.insert(name.into(), serde_json::json!({"columns": columns, "indexes": indexes}));
        }
        Ok(Value::Object(out))
    }

    // ---- bars ----

    /// Per stored M1 stream: source, symbol, bars, and the first and last bar's open time (`bar_stats`).
    pub async fn bar_stats(&self) -> Result<Vec<Value>, String> {
        let sql = "SELECT source, symbol, COUNT(*), MIN(time), MAX(time) FROM bars WHERE tf = '1m' \
                   GROUP BY source, symbol ORDER BY source, symbol";
        let err = |e: sqlx::Error| e.to_string();
        let iso = |t: Option<i64>| -> Value {
            t.and_then(|t| chrono::DateTime::from_timestamp(t, 0))
                .map(|d| d.naive_utc().format("%Y-%m-%dT%H:%M:%S").to_string().into())
                .unwrap_or(Value::Null)
        };
        macro_rules! go {
            ($pool:expr) => {
                sqlx::query(sql)
                    .fetch_all($pool)
                    .await
                    .map_err(err)?
                    .iter()
                    .map(|r| {
                        Ok(serde_json::json!({
                            "source": r.try_get::<String, _>(0).map_err(err)?,
                            "symbol": r.try_get::<String, _>(1).map_err(err)?,
                            "bars": r.try_get::<i64, _>(2).map_err(err)?,
                            "first": iso(r.try_get::<Option<i64>, _>(3).map_err(err)?),
                            "last": iso(r.try_get::<Option<i64>, _>(4).map_err(err)?),
                        }))
                    })
                    .collect()
            };
        }
        match &self.pool {
            Pool::Sqlite(p) => go!(p),
            Pool::Postgres(p) => go!(p),
        }
    }

    /// (source, symbol, bars) per stored stream, by source and symbol (`bar_stats` in storage.py).
    pub async fn bar_counts(&self) -> Result<Vec<(String, String, i64)>, String> {
        let sql = "SELECT source, symbol, COUNT(*) FROM bars WHERE tf = '1m' GROUP BY source, symbol ORDER BY source, symbol";
        let err = |e: sqlx::Error| e.to_string();
        macro_rules! go {
            ($pool:expr) => {
                sqlx::query(sql)
                    .fetch_all($pool)
                    .await
                    .map_err(err)?
                    .iter()
                    .map(|r| Ok((r.try_get::<String, _>(0).map_err(err)?, r.try_get::<String, _>(1).map_err(err)?,
                                 r.try_get::<i64, _>(2).map_err(err)?)))
                    .collect()
            };
        }
        match &self.pool {
            Pool::Sqlite(p) => go!(p),
            Pool::Postgres(p) => go!(p),
        }
    }

    // ---- the alert log (order block and drawing alerts) ----

    /// Whether this key was sent (`alert_sent`): a failed one may be retried.
    pub async fn alert_sent(&self, key: &str) -> Result<bool, String> {
        let rows = self.rows("alert_log", "SELECT status FROM alert_log WHERE key = ?", vec![Arg::Str(Some(key.into()))]).await?;
        Ok(rows.first().and_then(|r| r.get("status")).and_then(Value::as_str) == Some("sent"))
    }

    /// Whether this key was logged at all, sent or not (`alert_logged`).
    pub async fn alert_logged(&self, key: &str) -> Result<bool, String> {
        let rows = self.rows("alert_log", "SELECT key FROM alert_log WHERE key = ?", vec![Arg::Str(Some(key.into()))]).await?;
        Ok(!rows.is_empty())
    }

    /// Insert or update an alert row by its key (`log_alert`); the row's columns as JSON values
    /// (numbers go in as floats, the rest as text).
    pub async fn log_alert(&self, row: &Map<String, Value>) -> Result<(), String> {
        let args = row
            .iter()
            .map(|(k, v)| {
                let a = match v {
                    Value::Null => Arg::Str(None),
                    Value::Number(n) => Arg::Float(n.as_f64()),
                    Value::String(s) => Arg::Str(Some(s.clone())),
                    other => Arg::Str(Some(other.to_string())),
                };
                (k.as_str(), a)
            })
            .collect();
        self.upsert("alert_log", args, &["key"]).await
    }

    /// The latest alerts, newest first (`recent_alerts`): every column of `alert_log`.
    pub async fn recent_alerts(&self, limit: usize) -> Result<Vec<Map<String, Value>>, String> {
        self.rows("alert_log", "SELECT * FROM alert_log ORDER BY sent_at DESC LIMIT ?", vec![Arg::Int(Some(limit as i64))])
            .await
    }

    // ---- settings ----

    pub async fn get_setting(&self, key: &str) -> Result<Option<Value>, String> {
        let rows = self.rows("settings", "SELECT value FROM settings WHERE key = ?", vec![Arg::Str(Some(key.into()))]).await?;
        Ok(rows.first().and_then(|r| r["value"].as_str()).and_then(|s| serde_json::from_str(s).ok()))
    }

    pub async fn set_setting(&self, key: &str, value: &Value) -> Result<(), String> {
        self.upsert("settings", vec![("key", Arg::Str(Some(key.into()))), ("value", Arg::Str(Some(wednesday_base::pyjson::dumps(value))))],
                    &["key"])
            .await
    }

    pub async fn delete_setting(&self, key: &str) -> Result<(), String> {
        self.execute("DELETE FROM settings WHERE key = ?", vec![Arg::Str(Some(key.into()))]).await.map(|_| ())
    }

    // ---- markets: the dropdown's list (Settings > Markets) ----

    /// The markets of `source` in menu order (`Store.markets` in storage.py).
    pub async fn markets(&self, source: &str) -> Result<Vec<Map<String, Value>>, String> {
        let mut rows = self
            .rows("markets", "SELECT symbol, label, name, venue, position, keep_live FROM markets \
                              WHERE source = ? ORDER BY position, symbol", vec![Arg::Str(Some(source.into()))])
            .await?;
        for r in &mut rows {
            let live = r.get("keep_live").and_then(Value::as_bool).unwrap_or(false);
            r.insert("keep_live".into(), live.into());
        }
        Ok(rows)
    }

    /// Add a market at the end of the list (`add_market`).
    pub async fn add_market(&self, source: &str, m: &Map<String, Value>) -> Result<(), String> {
        let last = self.markets(source).await?.iter().filter_map(|r| r.get("position")?.as_i64()).max();
        let s = |k: &str| Arg::Str(m.get(k).and_then(Value::as_str).map(String::from));
        let now = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string();
        self.execute("INSERT INTO markets (source, symbol, label, name, venue, position, keep_live, added_at) \
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     vec![Arg::Str(Some(source.into())), s("symbol"), s("label"), s("name"), s("venue"),
                          Arg::Int(Some(last.map_or(0, |p| p + 1))),
                          Arg::Bool(m.get("keep_live").and_then(Value::as_bool).unwrap_or(false)), Arg::Str(Some(now))])
            .await
            .map(|_| ())
    }

    /// Rewrite the listed markets' label, name, venue and keep_live, in the given order (`save_markets`).
    pub async fn save_markets(&self, source: &str, markets: &[Map<String, Value>]) -> Result<(), String> {
        let stmts = markets.iter().enumerate().map(|(i, m)| {
            let s = |k: &str| Arg::Str(m.get(k).and_then(Value::as_str).map(String::from));
            ("UPDATE markets SET label = ?, name = ?, venue = ?, position = ?, keep_live = ? \
              WHERE source = ? AND symbol = ?".to_string(),
             vec![s("label"), s("name"), s("venue"), Arg::Int(Some(i as i64)),
                  Arg::Bool(m.get("keep_live").and_then(Value::as_bool).unwrap_or(false)),
                  Arg::Str(Some(source.into())), s("symbol")])
        }).collect();
        self.transaction(stmts).await
    }

    /// Take a market off the list; its stored bars stay (`delete_market`).
    pub async fn delete_market(&self, source: &str, symbol: &str) -> Result<bool, String> {
        Ok(self.execute("DELETE FROM markets WHERE source = ? AND symbol = ?",
                        vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into()))]).await? > 0)
    }

    // ---- drawings (per source and symbol) ----

    const DRAWING_JSON: [&'static str; 4] = ["points", "style", "props", "timeframes"];

    pub async fn drawings(&self, source: &str, symbol: &str) -> Result<Vec<Map<String, Value>>, String> {
        let mut rows = self
            .rows("drawings", "SELECT * FROM drawings WHERE source = ? AND symbol = ? ORDER BY updated_at",
                  vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into()))])
            .await?;
        for r in &mut rows {
            for k in Self::DRAWING_JSON {
                if let Some(Value::String(s)) = r.get(k) {
                    let parsed = serde_json::from_str(s).map_err(|e| format!("drawing {k}: {e}"))?;
                    r.insert(k.into(), parsed);
                }
            }
        }
        Ok(rows)
    }

    /// A drawing row as `drawings.clean_drawing` makes it, plus source and symbol.
    pub async fn drawing_put(&self, row: &Map<String, Value>) -> Result<(), String> {
        let s = |k: &str| Arg::Str(row.get(k).and_then(Value::as_str).map(String::from));
        let j = |k: &str| Arg::Str(row.get(k).filter(|v| !v.is_null()).map(wednesday_base::pyjson::dumps));
        let b = |k: &str| Arg::Bool(row.get(k).and_then(Value::as_bool).unwrap_or(false));
        self.upsert("drawings", vec![("id", s("id")), ("source", s("source")), ("symbol", s("symbol")), ("kind", s("kind")),
                                     ("points", j("points")), ("style", j("style")), ("props", j("props")),
                                     ("timeframes", j("timeframes")), ("locked", b("locked")), ("hidden", b("hidden")),
                                     ("updated_at", s("updated_at"))], &["id"])
            .await
    }

    pub async fn drawing_delete(&self, id: &str, source: &str, symbol: &str) -> Result<bool, String> {
        let args = vec![Arg::Str(Some(id.into())), Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into()))];
        Ok(self.execute("DELETE FROM drawings WHERE id = ? AND source = ? AND symbol = ?", args).await? > 0)
    }

    pub async fn drawing_alerts(&self, source: &str, symbol: &str) -> Result<Vec<Map<String, Value>>, String> {
        self.rows("drawing_alerts", "SELECT * FROM drawing_alerts WHERE source = ? AND symbol = ?",
                  vec![Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into()))])
            .await
    }

    /// An alert row as `drawing_alerts.clean_alert` makes it, plus drawing_id, source and symbol.
    pub async fn drawing_alert_put(&self, row: &Map<String, Value>) -> Result<(), String> {
        let s = |k: &str| Arg::Str(row.get(k).and_then(Value::as_str).map(String::from));
        let b = |k: &str| Arg::Bool(row.get(k).and_then(Value::as_bool).unwrap_or(false));
        self.upsert("drawing_alerts", vec![
            ("drawing_id", s("drawing_id")), ("source", s("source")), ("symbol", s("symbol")),
            ("condition", s("condition")), ("mode", s("mode")), ("note", s("note")), ("expires_at", s("expires_at")),
            ("intrabar", b("intrabar")), ("armed", b("armed")),
            ("armed_bar", Arg::Int(row.get("armed_bar").and_then(Value::as_i64))), ("fired_at", s("fired_at")),
            ("fired_price", Arg::Float(row.get("fired_price").and_then(Value::as_f64))),
            ("fires", Arg::Int(Some(row.get("fires").and_then(Value::as_i64).unwrap_or(0)))),
            ("created_at", s("created_at")),
        ], &["drawing_id"])
        .await
    }

    /// Set some columns of a drawing's alert (`drawing_alert_update`: disarm, a firing).
    pub async fn drawing_alert_update(&self, drawing_id: &str, values: Vec<(&str, Arg)>) -> Result<(), String> {
        if values.is_empty() {
            return Ok(());
        }
        let sets: Vec<String> = values.iter().map(|(k, _)| format!("\"{k}\" = ?")).collect();
        let sql = format!("UPDATE drawing_alerts SET {} WHERE drawing_id = ?", sets.join(", "));
        let mut args: Vec<Arg> = values.into_iter().map(|(_, a)| a).collect();
        args.push(Arg::Str(Some(drawing_id.into())));
        self.execute(&sql, args).await.map(|_| ())
    }

    pub async fn drawing_alert_delete(&self, id: &str, source: &str, symbol: &str) -> Result<bool, String> {
        let args = vec![Arg::Str(Some(id.into())), Arg::Str(Some(source.into())), Arg::Str(Some(symbol.into()))];
        Ok(self.execute("DELETE FROM drawing_alerts WHERE drawing_id = ? AND source = ? AND symbol = ?", args).await? > 0)
    }
}

// ---- the economic calendar's history (calendar/, `calendar_events`) ----

impl Store {
    /// Add or update past and coming releases (`calendar_put`); a known figure isn't wiped by a
    /// row without it. A key given twice ends as Python's one-row-at-a-time upserts leave it.
    pub async fn calendar_put(&self, rows: &[CalRow]) -> Result<(), String> {
        let mut merged: Vec<CalRow> = Vec::with_capacity(rows.len());
        let mut at: std::collections::HashMap<(i64, &str, &str), usize> = Default::default();
        for r in rows {
            match at.get(&(r.time, r.currency.as_str(), r.title.as_str())) {
                Some(&i) => {
                    let old = &merged[i];
                    let keep = |new: &Option<String>, old: &Option<String>| new.clone().or_else(|| old.clone());
                    merged[i] = CalRow { forecast: keep(&r.forecast, &old.forecast),
                                                          previous: keep(&r.previous, &old.previous),
                                                          actual: keep(&r.actual, &old.actual), ..r.clone() };
                }
                None => {
                    at.insert((r.time, r.currency.as_str(), r.title.as_str()), merged.len());
                    merged.push(r.clone());
                }
            }
        }
        let names = ["time", "currency", "title", "impact", "forecast", "previous", "actual", "source"];
        let one = format!("({})", vec!["?"; names.len()].join(", "));
        let stmts = merged
            .chunks(900 / names.len())
            .map(|chunk| {
                let sql = format!(
                    "INSERT INTO calendar_events ({}) VALUES {} ON CONFLICT (\"time\", currency, title) DO UPDATE SET \
                     impact = excluded.impact, forecast = COALESCE(excluded.forecast, calendar_events.forecast), \
                     previous = COALESCE(excluded.previous, calendar_events.previous), \
                     actual = COALESCE(excluded.actual, calendar_events.actual), source = excluded.source",
                    names.iter().map(|n| format!("\"{n}\"")).collect::<Vec<_>>().join(", "),
                    vec![one.as_str(); chunk.len()].join(", "));
                let args = chunk
                    .iter()
                    .flat_map(|r| [Arg::Int(Some(r.time)), Arg::Str(Some(r.currency.clone())), Arg::Str(Some(r.title.clone())),
                                   Arg::Str(r.impact.clone()), Arg::Str(r.forecast.clone()), Arg::Str(r.previous.clone()),
                                   Arg::Str(r.actual.clone()), Arg::Str(Some(r.source.clone()))])
                    .collect();
                (sql, args)
            })
            .collect();
        self.transaction(stmts).await
    }

    /// Stored releases, newest first (`calendar_history`); an imported release without an impact
    /// counts under any. Empty lists don't filter.
    pub async fn calendar_history(&self, currencies: &[String], impacts: &[String], before: Option<i64>)
        -> Result<Vec<CalRow>, String> {
        let mut sql = String::from("SELECT * FROM calendar_events WHERE 1 = 1");
        let mut args = Vec::new();
        let marks = |n: usize| vec!["?"; n].join(", ");
        if !currencies.is_empty() {
            sql += &format!(" AND currency IN ({})", marks(currencies.len()));
            args.extend(currencies.iter().map(|c| Arg::Str(Some(c.clone()))));
        }
        if !impacts.is_empty() {
            sql += &format!(" AND (impact IN ({}) OR impact IS NULL)", marks(impacts.len()));
            args.extend(impacts.iter().map(|c| Arg::Str(Some(c.clone()))));
        }
        if let Some(b) = before {
            sql += " AND \"time\" < ?";
            args.push(Arg::Int(Some(b)));
        }
        sql += " ORDER BY \"time\" DESC";
        let rows = self.rows("calendar_events", &sql, args).await?;
        Ok(rows.iter().filter_map(CalRow::from_json).collect())
    }

    /// Stored releases: count, first and last time (unix seconds), as `calendar_bounds`.
    pub async fn calendar_bounds(&self) -> Result<(i64, Option<i64>, Option<i64>), String> {
        let sql = "SELECT COUNT(*), MIN(\"time\"), MAX(\"time\") FROM calendar_events";
        let err = |e: sqlx::Error| e.to_string();
        macro_rules! go {
            ($pool:expr) => {{
                let r = sqlx::query(sql).fetch_one($pool).await.map_err(err)?;
                Ok((r.try_get::<i64, _>(0).map_err(err)?, r.try_get::<Option<i64>, _>(1).map_err(err)?,
                    r.try_get::<Option<i64>, _>(2).map_err(err)?))
            }};
        }
        match &self.pool {
            Pool::Sqlite(p) => go!(p),
            Pool::Postgres(p) => go!(p),
        }
    }
}

/// A `calendar_events` row.
#[derive(Clone, Debug, PartialEq)]
pub struct CalRow {
    pub time: i64, // the release, unix seconds UTC
    pub currency: String,
    pub title: String,
    pub impact: Option<String>,
    pub forecast: Option<String>,
    pub previous: Option<String>,
    pub actual: Option<String>,
    pub source: String,
}

impl CalRow {
    pub fn to_json(&self) -> Value {
        serde_json::json!({"time": self.time, "currency": self.currency, "title": self.title, "impact": self.impact,
               "forecast": self.forecast, "previous": self.previous, "actual": self.actual, "source": self.source})
    }

    /// A row as the store gives it back.
    pub fn from_json(r: &Map<String, Value>) -> Option<CalRow> {
        let s = |k: &str| r.get(k).and_then(Value::as_str).map(String::from);
        Some(CalRow { time: r.get("time")?.as_i64()?, currency: s("currency")?, title: s("title")?, impact: s("impact"),
                      forecast: s("forecast"), previous: s("previous"), actual: s("actual"),
                      source: s("source").unwrap_or_default() })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls_as_sqlalchemy_writes_them() {
        let home = Path::new("/home/me/.wednesday");
        assert_eq!(sqlite_path("sqlite:///data/xau.db", home), Some(home.join("data/xau.db")));
        assert_eq!(sqlite_path("sqlite:////tmp/x.db", home), Some("/tmp/x.db".into()));
        assert_eq!(sqlite_path("sqlite://", home), None);
        assert_eq!(sqlite_path("sqlite:///:memory:", home), None);
        assert_eq!(postgres_url("postgresql+psycopg://u:p@h:5432/db?sslmode=require").as_deref(),
                   Some("postgres://u:p@h:5432/db?sslmode=require"));
        assert_eq!(postgres_url("sqlite:///x.db"), None);
    }
}
