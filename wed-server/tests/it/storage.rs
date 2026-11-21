//! The Rust store against Python's `storage.py` (#96), on SQLite and, when PostgreSQL's
//! `initdb` is installed, on a throwaway PostgreSQL server:
//!
//! * a database Python made and one Rust made have the same tables, columns and indexes;
//! * what Python writes, Rust reads as Python reads it, and the other way round;
//! * a nullable column a newer version added comes back on an older database.
//!
//! It runs Python through `uv` from this checkout (PostgreSQL with `--with psycopg[binary]`); without
//! uv it says so and passes.

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::{json, Value};
use wednesday_rs::store::Store;

const PY: &str = r##"
import json, sys
from wednesday.storage import Store
url, action = sys.argv[1], sys.argv[2]
s = Store(url)
if action == "write":
    s.set_setting("probe", {"a": [1, 2.5, None, True], "é": "\U0001F600", "n": {"x": 1e-05}})
    s.drawing_put({"id": "py-0000001", "source": "synthetic", "symbol": "XAUUSD", "kind": "long",
                   "points": [{"t": 1772629200, "p": 2150.5}, {"t": 1772632800, "p": 2150.5}],
                   "style": {"color": None, "width": 2, "dash": "dashed"}, "props": {"stop": 2148.0, "target": 2160.25},
                   "timeframes": ["15M", "1H"], "locked": True, "hidden": False, "updated_at": "2026-03-04T12:00:00+00:00"})
    s.drawing_put({"id": "py-0000002", "source": "synthetic", "symbol": "XAUUSD", "kind": "hline",
                   "points": [{"t": 1772629200, "p": 2149.0}], "style": {"color": "#ff0000", "width": 1, "dash": "solid"},
                   "props": None, "timeframes": None, "locked": False, "hidden": True,
                   "updated_at": "2026-03-04T11:00:00+00:00"})
    s.drawing_alert_put({"drawing_id": "py-0000002", "source": "synthetic", "symbol": "XAUUSD", "condition": "cross",
                         "mode": "every", "note": "line", "expires_at": None, "intrabar": False, "armed": True,
                         "armed_bar": 1772629140, "fired_at": None, "fired_price": None, "fires": 0,
                         "created_at": "2026-03-04T11:00:00+00:00"})
print(json.dumps({"probe": s.get_setting("probe"), "probe_rust": s.get_setting("probe_rust"),
                  "drawings": s.drawings("synthetic", "XAUUSD"), "alerts": s.drawing_alerts("synthetic", "XAUUSD")}))
"##;

fn repo() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..").canonicalize().unwrap()
}

fn have(program: &str) -> bool {
    Command::new(program).arg("--version").stdout(Stdio::null()).stderr(Stdio::null()).status().is_ok()
}

/// Run the Python side; its JSON output, or None when it couldn't run (said on stderr).
fn python(url: &str, action: &str, dir: &Path, postgres: bool) -> Option<Value> {
    let mut cmd = Command::new("uv");
    cmd.args(["run", "--quiet", "--project"]).arg(repo());
    if postgres {
        cmd.args(["--with", "psycopg[binary]>=3.1"]);
    }
    let out = cmd.args(["python", "-c", PY, url, action]).current_dir(dir).output().ok()?;
    if !out.status.success() {
        eprintln!("the Python side failed:\n{}", String::from_utf8_lossy(&out.stderr));
        return None;
    }
    Some(serde_json::from_slice(&out.stdout).expect("Python's JSON"))
}

async fn check(python_url: &str, rust_url: &str, dir: &Path, postgres: bool) {
    // Python makes its database and writes to it.
    let Some(written) = python(python_url, "write", dir, postgres) else {
        assert!(postgres, "the Python side must run for SQLite");
        return;
    };
    // Same schema as a database Rust makes.
    let theirs = Store::open(python_url, dir).await.unwrap();
    let ours = Store::open(rust_url, dir).await.unwrap();
    assert_eq!(theirs.describe_schema().await.unwrap(), ours.describe_schema().await.unwrap(),
               "the tables Rust makes differ from Python's");
    // Rust reads what Python wrote as Python reads it.
    assert_eq!(theirs.get_setting("probe").await.unwrap().as_ref(), Some(&written["probe"]));
    assert_eq!(Value::from(theirs.drawings("synthetic", "XAUUSD").await.unwrap()), written["drawings"]);
    assert_eq!(Value::from(theirs.drawing_alerts("synthetic", "XAUUSD").await.unwrap()), written["alerts"]);
    // Python reads what Rust writes, as Rust wrote it.
    let value = json!({"b": [false, 3, -0.5, "\u{e9}\u{1F600}"], "nested": {"k": null}});
    theirs.set_setting("probe_rust", &value).await.unwrap();
    let mut row = written["drawings"][0].as_object().unwrap().clone();
    row.insert("id".into(), "rs-0000003".into());
    row.insert("source".into(), "synthetic".into());
    row.insert("symbol".into(), "XAUUSD".into());
    row.insert("updated_at".into(), "2026-03-04T13:00:00+00:00".into());
    row.insert("props".into(), Value::Null);
    theirs.drawing_put(&row).await.unwrap();
    assert!(theirs.drawing_delete("py-0000002", "synthetic", "XAUUSD").await.unwrap());
    assert!(!theirs.drawing_delete("py-0000002", "synthetic", "XAUUSD").await.unwrap());
    let mut alert = written["alerts"][0].as_object().unwrap().clone();
    alert.insert("drawing_id".into(), "rs-0000003".into());
    alert.insert("note".into(), Value::Null);
    alert.insert("expires_at".into(), "2099-06-01T03:00:00+00:00".into());
    alert.insert("armed_bar".into(), Value::Null);
    theirs.drawing_alert_put(&alert).await.unwrap();
    theirs
        .drawing_alert_update("rs-0000003", vec![
            ("armed", wednesday_rs::store::Arg::Bool(false)),
            ("fired_at", wednesday_rs::store::Arg::Str(Some("2026-03-04T13:01:00.250000+00:00".into()))),
            ("fired_price", wednesday_rs::store::Arg::Float(Some(2151.5))),
            ("fires", wednesday_rs::store::Arg::Int(Some(1))),
        ])
        .await
        .unwrap();
    for (k, v) in [("armed", json!(false)), ("fired_at", json!("2026-03-04T13:01:00.250000+00:00")),
                   ("fired_price", json!(2151.5)), ("fires", json!(1))] {
        alert.insert(k.into(), v);
    }
    let read = python(python_url, "read", dir, postgres).unwrap();
    // A deleted drawing keeps its alert (for undo); Python reads the alert Rust wrote as Rust wrote it.
    let alerts = read["alerts"].as_array().unwrap();
    assert_eq!(alerts.len(), 2);
    assert_eq!(alerts.iter().find(|a| a["drawing_id"] == "py-0000002"), Some(&written["alerts"][0]));
    assert_eq!(alerts.iter().find(|a| a["drawing_id"] == "rs-0000003"), Some(&Value::Object(alert)));
    assert_eq!(read["probe_rust"], value);
    let ids: Vec<&str> = read["drawings"].as_array().unwrap().iter().map(|d| d["id"].as_str().unwrap()).collect();
    assert_eq!(ids, ["py-0000001", "rs-0000003"], "by updated_at");
    let mut rust_row = row.clone();
    rust_row.remove("source");
    rust_row.remove("symbol");
    let mut python_row = read["drawings"][1].as_object().unwrap().clone();
    python_row.remove("source");
    python_row.remove("symbol");
    assert_eq!(python_row, rust_row);
    // A column a newer version added comes back on a database that lacks it.
    ours.execute("ALTER TABLE drawings DROP COLUMN timeframes", vec![]).await.unwrap();
    drop(ours);
    let reopened = Store::open(rust_url, dir).await.unwrap();
    assert_eq!(theirs.describe_schema().await.unwrap()["drawings"]["columns"].as_array().unwrap().len(),
               reopened.describe_schema().await.unwrap()["drawings"]["columns"].as_array().unwrap().len());
}

fn temp(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("wed-storage-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[tokio::test(flavor = "multi_thread")]
async fn sqlite_as_python_has_it() {
    if !have("uv") {
        eprintln!("uv isn't installed: skipped");
        return;
    }
    let dir = temp("sqlite");
    let py = format!("sqlite:///{}", dir.join("python.db").display());
    check(&py, "sqlite:///data/rust.db", &dir, false).await;
}

struct Postgres {
    data: PathBuf,
}

impl Drop for Postgres {
    fn drop(&mut self) {
        let _ = Command::new("pg_ctl").args(["-D"]).arg(&self.data).args(["-m", "immediate", "stop"])
            .stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
}

/// A PostgreSQL server to make the two databases on: `WEDNESDAY_TEST_POSTGRES` (an admin URL,
/// e.g. `postgresql://postgres@127.0.0.1:5432/postgres`), else a throwaway one from `initdb`.
fn postgres_server(dir: &Path) -> Option<(String, Option<Postgres>)> {
    if let Ok(url) = std::env::var("WEDNESDAY_TEST_POSTGRES") {
        return Some((url, None));
    }
    let data = dir.join("data");
    let init = Command::new("initdb").arg("-D").arg(&data).args(["-U", "wed", "--auth=trust", "-E", "UTF8"]).output();
    match init {
        Ok(out) if out.status.success() => {}
        Ok(out) => {
            let why = String::from_utf8_lossy(&out.stderr);
            eprintln!("PostgreSQL's initdb doesn't run here, so PostgreSQL is skipped:\n{}", why.lines().last().unwrap_or(""));
            return None;
        }
        Err(_) => {
            eprintln!("PostgreSQL (initdb) isn't installed and WEDNESDAY_TEST_POSTGRES isn't set: skipped");
            return None;
        }
    }
    let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let opts = format!("-p {port} -c listen_addresses=127.0.0.1 -k {}", dir.display());
    let ok = Command::new("pg_ctl").arg("-D").arg(&data).args(["-o", &opts, "-w", "-l"]).arg(dir.join("log"))
        .arg("start").stdout(Stdio::null()).stderr(Stdio::null()).status().is_ok_and(|s| s.success());
    let server = Postgres { data };
    assert!(ok, "pg_ctl start failed: see {}", dir.join("log").display());
    Some((format!("postgresql+psycopg://wed@127.0.0.1:{port}/postgres"), Some(server)))
}

#[tokio::test(flavor = "multi_thread")]
async fn postgresql_as_python_has_it() {
    if !have("uv") {
        eprintln!("uv isn't installed: skipped");
        return;
    }
    let dir = temp("pg");
    let Some((admin_url, _server)) = postgres_server(&dir) else { return };
    let admin = Store::connect(&admin_url, &dir).await.unwrap();
    let suffix = std::process::id();
    let (py_db, rs_db) = (format!("wed_python_{suffix}"), format!("wed_rust_{suffix}"));
    for db in [&py_db, &rs_db] {
        admin.execute(&format!("DROP DATABASE IF EXISTS {db}"), vec![]).await.unwrap();
        admin.execute(&format!("CREATE DATABASE {db}"), vec![]).await.unwrap();
    }
    let base = admin_url.rsplit_once('/').unwrap().0;
    check(&format!("{base}/{py_db}"), &format!("{base}/{rs_db}"), &dir, true).await;
}
