//! The plugin host (plugins/): plugins run as separate programs with a token per launch, list their
//! features, handle the licence, serve `/api/ee/<name>/...`, keep their settings in the server's
//! (only their own), are restarted when they stop and stopped with the server.

use crate::plugin_kit;

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use plugin_kit::*;
use serde_json::{json, Value};
use wednesday_rs::plugins::{Host, Options};
use wednesday_rs::store::Store;

struct Core {
    addr: SocketAddr,
    host: Arc<Host>,
    store: Store,
    plugins: PathBuf,
}

impl Core {
    fn url(&self, path: &str) -> String {
        format!("http://{}{path}", self.addr)
    }
}

/// The server with a plugin host over `<home>/plugins`; `tune` changes its options.
async fn core(home: &Path, tune: impl FnOnce(&mut Options)) -> Core {
    let store = Store::open("sqlite:///data/xau.db", home).await.unwrap();
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let mut opts = Options::new(home, addr.port(), Some(store.clone()));
    opts.dir = home.join("plugins");
    opts.licence_server = Some("http://licence.test".into());
    opts.start_timeout = Duration::from_secs(3);
    opts.restart_backoff = Duration::from_millis(100);
    tune(&mut opts);
    let plugins = opts.dir.clone();
    let host = Host::new(opts);
    let app = wednesday_rs::app("127.0.0.1:9".parse().unwrap(), Some(store.clone())).layer(axum::Extension(host.clone()));
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    host.load_all().await;
    Core { addr, host, store, plugins }
}

fn state(c: &Core, name: &str, file: &str) -> String {
    std::fs::read_to_string(c.plugins.join(name).join("state").join(file)).unwrap()
}

async fn wait_for(what: &str, mut ok: impl FnMut() -> bool) {
    let start = std::time::Instant::now();
    while !ok() {
        assert!(start.elapsed() < Duration::from_secs(20), "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

const CROSS_SITE: &[(&str, &str)] = &[("origin", "http://evil.example")];

#[tokio::test]
async fn a_plugin_runs_with_its_own_token() {
    let _env = crate::env_lock();
    let home = temp("runs");
    put_fake(&home.join("plugins"), "wednesday_ee", json!({}));
    std::env::set_var("TELEGRAM_BOT_TOKEN", "123:secret");
    let c = core(&home, |_| {}).await;

    let (status, list) = send("GET", &c.url("/api/plugins"), None, &[]).await;
    assert_eq!(status, 200);
    assert_eq!(list["api"], 2);
    assert_eq!(list["plugins"], json!([{"name": "wednesday_ee", "version": "0.2.0", "api": 2, "features": [],
                                        "loaded": true, "error": null}]));
    assert_eq!(list["features"], json!([]));
    assert_eq!(list["catalog"][0]["id"], "journal.multi");

    // Nobody but the server reaches it.
    let port = state(&c, "wednesday_ee", "port");
    assert_eq!(send("GET", &format!("http://127.0.0.1:{port}/plugin"), None, &[]).await.0, 401);
    assert_eq!(send("GET", &format!("http://127.0.0.1:{port}/plugin"), None, &[("authorization", "Bearer nope")]).await.0, 401);

    let env: Value = serde_json::from_str(&state(&c, "wednesday_ee", "env.json")).unwrap();
    let device = c.store.get_setting("ee_device").await.unwrap().unwrap();
    assert_eq!(env["device"], device["id"]);
    assert_eq!(env["core"], format!("http://{}", c.addr));
    assert_eq!(env["licence_server"], "http://licence.test");
    assert_eq!(env["telegram"], Value::Null, "the bot token isn't passed on");
    assert_eq!(std::fs::canonicalize(env["cwd"].as_str().unwrap()).unwrap(),
               std::fs::canonicalize(c.plugins.join("wednesday_ee")).unwrap());
    c.host.shutdown().await;
}

#[tokio::test]
async fn the_licence_goes_through_the_plugin() {
    let home = temp("licence");
    put_fake(&home.join("plugins"), "wednesday_ee", json!({}));
    let c = core(&home, |_| {}).await;
    let journals = || send("GET", c.url("/api/journals"), None, &[]);

    let (status, lic) = send("GET", &c.url("/api/licence"), None, &[]).await;
    assert_eq!((status, &lic["available"], &lic["valid"]), (200, &json!(true), &json!(false)));
    assert_eq!(send("PUT", &c.url("/api/licence"), Some(json!({"key": " "})), &[]).await,
               (422, json!({"detail": "Paste a licence key"})));
    assert_eq!(send("PUT", &c.url("/api/licence"), Some(json!({"key": "WEDK-BAD"})), &[]).await,
               (422, json!({"detail": "That licence key isn't known"})));
    assert_eq!(journals().await.1["multi"], false);
    assert_eq!(send("PUT", &c.url("/api/licence"), Some(json!({"key": "WEDK-GOOD-1"})), CROSS_SITE).await.0, 403);

    let (status, lic) = send("PUT", &c.url("/api/licence"), Some(json!({"key": "WEDK-GOOD-1"})), &[]).await;
    assert_eq!((status, &lic["available"], &lic["valid"], &lic["plan"]), (200, &json!(true), &json!(true), &json!("pro")));
    let list = send("GET", &c.url("/api/plugins"), None, &[]).await.1;
    assert_eq!(list["features"], json!(["journal.multi"]));
    assert_eq!(list["catalog"][0]["enabled"], true);
    assert_eq!(journals().await.1["multi"], true, "the journal reads the registry");
    // The plugin kept it in the server's settings, under its own prefix.
    assert_eq!(c.store.get_setting("ee_licence").await.unwrap(), Some(json!({"plan": "pro"})));

    let (status, lic) = send("DELETE", &c.url("/api/licence"), None, &[]).await;
    assert_eq!((status, &lic["valid"]), (200, &json!(false)));
    assert_eq!(send("GET", &c.url("/api/plugins"), None, &[]).await.1["features"], json!([]));
    assert_eq!(c.store.get_setting("ee_licence").await.unwrap(), None);
    c.host.shutdown().await;
}

#[tokio::test]
async fn routes_are_forwarded_and_settings_kept_to_the_plugins_own() {
    let home = temp("routes");
    put_fake(&home.join("plugins"), "wednesday_ee", json!({}));
    let c = core(&home, |_| {}).await;

    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    let res = client.post(c.url("/api/ee/ee/echo?x=1&y=two"))
        .header("content-type", "text/plain").header("cookie", "a=b").body("hi").send().await.unwrap();
    assert_eq!(res.status(), 200);
    let echo: Value = serde_json::from_str(&res.text().await.unwrap()).unwrap();
    assert_eq!(echo, json!({"method": "POST", "path": "/routes/echo", "query": "x=1&y=two", "content_type": "text/plain",
                            "origin": null, "cookie": null, "body": "hi"}));
    assert_eq!(send("POST", &c.url("/api/ee/ee/echo"), Some(json!({})), CROSS_SITE).await,
               (403, json!({"detail": "Cross-site request refused"})));
    assert_eq!(send("GET", &c.url("/api/ee/ee/echo"), None, CROSS_SITE).await.0, 200, "reads aren't refused");
    assert_eq!(send("GET", &c.url("/api/ee/nope/echo"), None, &[]).await.0, 404);
    assert_eq!(send("GET", &c.url("/api/ee/wednesday_ee/echo"), None, &[]).await.0, 404, "routes go by the short name");

    // Through the plugin, with its token: its own keys only.
    let via = |key: &str| send("GET", c.url(&format!("/api/ee/ee/settings?key={key}")), None, &[]);
    let own = via("ee_device").await.1;
    assert_eq!(own["status"], 200);
    assert_eq!(own["body"]["value"]["id"].as_str().unwrap().len(), 32);
    assert_eq!(via("ee_nothing").await.1["body"], json!({"value": null}));
    assert_eq!(via("journal_x").await.1["status"], 403);
    assert_eq!(via("ee_").await.1["status"], 403);
    assert_eq!(via("alerts").await.1["status"], 403);
    // Without it: nothing.
    let settings = c.url("/api/plugin-host/settings/ee_device");
    assert_eq!(send("GET", &settings, None, &[]).await.0, 401);
    assert_eq!(send("GET", &settings, None, &[("authorization", "Bearer nope")]).await.0, 401);
    assert_eq!(send("PUT", &settings, Some(json!(1)), &[]).await.0, 401);
    c.host.shutdown().await;
}

#[tokio::test]
async fn a_plugin_that_stops_is_restarted_and_stopped_with_the_server() {
    let home = temp("restart");
    put_fake(&home.join("plugins"), "wednesday_ee", json!({}));
    let c = core(&home, |_| {}).await;
    let first = c.host.pids();
    assert_eq!(first.len(), 1);

    let _ = send("GET", &c.url("/api/ee/ee/crash"), None, &[]).await;
    wait_for("the restart", || {
        let p = c.host.pids();
        p.len() == 1 && p != first && c.host.plugins()[0].loaded
    }).await;
    assert_eq!(send("GET", &c.url("/api/ee/ee/echo"), None, &[]).await.0, 200);

    let port = state(&c, "wednesday_ee", "port");
    c.host.shutdown().await;
    assert!(c.host.pids().is_empty());
    assert!(!c.host.plugins()[0].loaded);
    assert!(tokio::net::TcpStream::connect(format!("127.0.0.1:{port}")).await.is_err(), "the plugin is gone");
}

#[tokio::test]
async fn dropping_the_host_stops_its_plugins() {
    let home = temp("drop");
    put_fake(&home.join("plugins"), "wednesday_ee", json!({}));
    let store = Store::open("sqlite:///data/xau.db", &home).await.unwrap();
    let mut opts = Options::new(&home, 9, Some(store));
    opts.dir = home.join("plugins");
    let host = Host::new(opts);
    host.load_all().await;
    assert!(host.plugins()[0].loaded);
    let port = std::fs::read_to_string(home.join("plugins/wednesday_ee/state/port")).unwrap();
    drop(host);
    let addr = format!("127.0.0.1:{port}");
    let start = std::time::Instant::now();
    while tokio::net::TcpStream::connect(&addr).await.is_ok() {
        assert!(start.elapsed() < Duration::from_secs(10), "the plugin outlived its host");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

#[tokio::test]
async fn plugins_that_cant_run_are_listed_with_why() {
    let home = temp("broken");
    let plugins = home.join("plugins");
    // An API 1 download (a Python wheel), a plugin for a later API, one that never listens, one
    // that says another API, and one without a licence screen.
    std::fs::create_dir_all(plugins.join("wednesday_ee")).unwrap();
    std::fs::write(plugins.join("wednesday_ee/.wednesday-plugin.json"), json!({"package": "wednesday_ee", "version": "0.1.0"}).to_string()).unwrap();
    put_fake(&plugins, "future", json!({}));
    std::fs::write(plugins.join("future/plugin.json"), json!({"name": "future", "api": 3, "exe": EXE}).to_string()).unwrap();
    put_fake(&plugins, "silent", json!({"handshake": false}));
    put_fake(&plugins, "liar", json!({"api": 1}));
    put_fake(&plugins, "free", json!({"licence": false}));
    std::fs::create_dir_all(plugins.join("free.new")).unwrap(); // a download in progress: not a plugin
    let c = core(&home, |o| o.start_timeout = Duration::from_secs(1)).await;

    let list = send("GET", &c.url("/api/plugins"), None, &[]).await.1;
    let by: std::collections::HashMap<String, Value> =
        list["plugins"].as_array().unwrap().iter().map(|p| (p["name"].as_str().unwrap().to_string(), p.clone())).collect();
    assert_eq!(by.len(), 5, "{list}");
    assert_eq!(by["wednesday_ee"]["error"], "written for plugin API 1, this Wednesday has 2");
    assert_eq!(by["wednesday_ee"]["version"], "0.1.0");
    assert_eq!(by["future"]["error"], "written for plugin API 3, this Wednesday has 2");
    assert_eq!(by["silent"]["error"], "didn't start");
    assert_eq!(by["liar"]["error"], "written for plugin API 1, this Wednesday has 2");
    assert_eq!((&by["free"]["loaded"], &by["free"]["error"]), (&json!(true), &Value::Null));
    for name in ["wednesday_ee", "future", "silent", "liar"] {
        assert_eq!(by[name]["loaded"], false, "{name}");
    }

    // No licence screen; a key that isn't the licence server's says how to get Wednesday EE.
    assert_eq!(send("GET", &c.url("/api/licence"), None, &[]).await, (200, json!({"available": false})));
    assert_eq!(send("DELETE", &c.url("/api/licence"), None, &[]).await, (409, json!({"detail": "No plugin handles licences"})));
    assert_eq!(send("PUT", &c.url("/api/licence"), Some(json!({"key": "abc"})), &[]).await.1["detail"],
               "Wednesday EE isn't installed. Enter a licence key (WEDK-...) and Wednesday downloads it.");
    assert_eq!(send("GET", &c.url("/api/ee/silent/echo"), None, &[]).await.0, 503);
    c.host.shutdown().await;
}

#[tokio::test]
async fn a_licence_key_downloads_wednesday_ee() {
    use axum::routing::post;
    let signer = Arc::new(Signer::new("test"));
    let zip = Arc::new(plugin_zip());
    let (s, z) = (signer.clone(), zip.clone());
    let licence_server = axum::Router::new().route("/v1/download", post(move |body: String| {
        let (s, z) = (s.clone(), z.clone());
        async move {
            let v: Value = serde_json::from_str(&body).unwrap();
            assert_eq!(v["target"], wednesday_rs::plugins::install::TARGET);
            assert_eq!(v["device_id"].as_str().unwrap().len(), 32);
            match v["key"].as_str().unwrap() {
                "WEDK-EXPIRED" => (axum::http::StatusCode::FORBIDDEN,
                                   json!({"detail": "This licence expired on 01 Jan 2026"}).to_string()),
                "WEDK-FORGED" => (axum::http::StatusCode::OK, Signer::new("test").answer(&z, json!({})).to_string()),
                _ => (axum::http::StatusCode::OK, s.answer(&z, json!({})).to_string()),
            }
        }
    }));
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(l, licence_server).await.unwrap() });

    let home = temp("download");
    let c = core(&home, |o| {
        o.download_url = url.clone();
        o.trust = signer.trust();
    }).await;
    assert_eq!(send("PUT", &c.url("/api/licence"), Some(json!({"key": "WEDK-EXPIRED"})), &[]).await,
               (422, json!({"detail": "This licence expired on 01 Jan 2026"})));
    assert_eq!(send("PUT", &c.url("/api/licence"), Some(json!({"key": "WEDK-FORGED"})), &[]).await,
               (422, json!({"detail": "The download isn't genuine (its signature doesn't match)"})));
    assert!(!c.plugins.join("wednesday_ee").exists(), "nothing unpacked");

    let (status, lic) = send("PUT", &c.url("/api/licence"), Some(json!({"key": "WEDK-GOOD-1"})), &[]).await;
    assert_eq!((status, &lic["available"], &lic["valid"]), (200, &json!(true), &json!(true)), "{lic}");
    let list = send("GET", &c.url("/api/plugins"), None, &[]).await.1;
    assert_eq!(list["plugins"][0]["name"], "wednesday_ee");
    assert_eq!(list["plugins"][0]["loaded"], true);
    assert_eq!(list["features"], json!(["journal.multi"]));

    // A new download replaces the running copy, which keeps its licence in the server's settings.
    let before = c.host.pids();
    let (_, info) = c.host.install("WEDK-GOOD-2").await.unwrap();
    assert!(info.loaded && c.host.pids() != before);
    assert_eq!(info.features, vec!["journal.multi".to_string()]);
    c.host.shutdown().await;
}

#[tokio::test]
async fn without_a_host_there_are_no_plugins() {
    let home = temp("none");
    let store = Store::open("sqlite:///data/xau.db", &home).await.unwrap();
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(wednesday_rs::serve(l, "127.0.0.1:9".parse().unwrap(), Some(store)));
    let list = send("GET", &format!("http://{addr}/api/plugins"), None, &[]).await.1;
    assert_eq!((&list["api"], &list["plugins"], &list["features"]), (&json!(2), &json!([]), &json!([])));
    assert_eq!(send("GET", &format!("http://{addr}/api/licence"), None, &[]).await.1, json!({"available": false}));
    assert_eq!(send("PUT", &format!("http://{addr}/api/licence"), Some(json!({"key": "WEDK-X"})), &[]).await,
               (409, json!({"detail": "The licence needs the server running with --serve"})));
    assert_eq!(send("GET", &format!("http://{addr}/api/plugin-host/settings/ee_x"), None, &[]).await.0, 401);
}
