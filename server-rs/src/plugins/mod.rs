//! Plugins (plugin API 2): extra features in a separate program the server runs (#96, step 10).
//!
//! A plugin is a folder in `data/plugins/` with a `plugin.json` (`name`, `version`, `api: 2`,
//! `exe`) and that program. The [`Host`] starts each one as a child process with a token for this
//! launch, reads the port it listens on from its first line of output, and talks to it over HTTP on
//! 127.0.0.1: `GET /plugin` for what it is and the features it provides, `/licence` when it
//! handles licences, and `/routes/...` for the dashboard's `/api/ee/<name>/...`. A plugin calls
//! back the server's `/api/plugin-host/settings/<key>` with its own token, for its settings.
//!
//! Wednesday runs the same without any plugin. Licence checks and paid code live in the plugin
//! (Wednesday EE, private); the core only relays. A plugin that doesn't start, or stops, is listed
//! with why; it is restarted a few times, and never takes the server down.

pub mod catalog;
pub mod install;
pub mod keys;
pub mod routes;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::{header, HeaderMap, Method, StatusCode};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::{oneshot, watch};

use crate::store::Store;

/// The plugin API this Wednesday runs (API 1 was Python packages loaded into the Python server).
pub const API: i64 = install::API;
/// The dashboard reaches a plugin's routes under `/api/ee/<name>/`.
pub const ROUTE_PREFIX: &str = "/api/ee";
/// The line a plugin prints when it listens: `WEDNESDAY-PLUGIN-LISTEN <port>`.
pub const HANDSHAKE: &str = "WEDNESDAY-PLUGIN-LISTEN";
/// A licence call reaches the licence server through the plugin.
const CALL_TIMEOUT: Duration = Duration::from_secs(90);
const MAX_RESTARTS: u32 = 3;
/// A plugin that ran this long before stopping gets its restarts back.
const STABLE: Duration = Duration::from_secs(300);
/// Variables a plugin doesn't get from the server's environment.
const NOT_PASSED: &[&str] = &["TELEGRAM_BOT_TOKEN", "WEDNESDAY_EE_TEST_KEYS"];

/// A plugin's name without `wednesday_`: its segment in `/api/ee/<short>/...` (Wednesday EE's
/// routes stay at `/api/ee/ee/...`, as with Python) and the prefix of its settings (`ee_...`).
pub fn short_name(name: &str) -> &str {
    name.strip_prefix("wednesday_").unwrap_or(name)
}

/// A plugin as Settings > Plugins lists it (`plugins.PluginInfo`).
#[derive(Debug, Clone, PartialEq)]
pub struct PluginInfo {
    pub name: String,
    pub version: Option<String>,
    pub api: Option<i64>,
    pub features: Vec<String>,
    pub loaded: bool,
    pub error: Option<String>,
}

impl PluginInfo {
    pub fn to_json(&self) -> Value {
        json!({"name": self.name, "version": self.version, "api": self.api, "features": self.features,
               "loaded": self.loaded, "error": self.error})
    }
}

/// How the host runs: where plugins are, how they reach the server, what downloads are checked against.
pub struct Options {
    /// `data/plugins`.
    pub dir: PathBuf,
    /// `http://127.0.0.1:<port>` of this server, for the plugins' calls back.
    pub core_url: String,
    /// `WEDNESDAY_LICENCE_SERVER` when set, passed on to plugins.
    pub licence_server: Option<String>,
    /// Where licence keys download Wednesday EE from.
    pub download_url: String,
    pub store: Option<Store>,
    pub trust: install::Trust,
    /// How often the plugins' features are read again.
    pub poll: Duration,
    /// How long a plugin has to say it listens.
    pub start_timeout: Duration,
    /// The wait before the first restart of a plugin that stopped; it doubles each time.
    pub restart_backoff: Duration,
}

impl Options {
    /// The plugins of the server in `home`, which listens on 127.0.0.1:`core_port`.
    pub fn new(home: &Path, core_port: u16, store: Option<Store>) -> Options {
        Options {
            dir: install::plugins_dir(home),
            core_url: format!("http://127.0.0.1:{core_port}"),
            licence_server: install::licence_server_env(home),
            download_url: install::server_url(home),
            store,
            trust: install::Trust::default(),
            poll: Duration::from_secs(60),
            start_timeout: Duration::from_secs(10),
            restart_backoff: Duration::from_secs(1),
        }
    }
}

#[derive(Clone)]
struct Conn {
    port: u16,
    token: String,
}

struct Entry {
    id: u64,
    info: PluginInfo,
    conn: Option<Conn>,
    /// The token this plugin calls the server with; only while it runs.
    core_token: Option<String>,
    licence: bool,
    pid: Option<u32>,
    stop: Option<watch::Sender<bool>>,
    task: Option<tokio::task::JoinHandle<()>>,
}

/// Runs the plugins in `data/plugins` and keeps their registry. Dropping it (or [`Host::shutdown`])
/// stops them; a plugin also exits when its standard input closes, so one never outlives the server.
pub struct Host {
    opts: Options,
    client: Client<HttpConnector, Body>,
    entries: Mutex<Vec<Entry>>,
    ids: AtomicU64,
    /// One load or install at a time.
    gate: tokio::sync::Mutex<()>,
    stopping: watch::Sender<bool>,
}

impl Host {
    pub fn new(opts: Options) -> Arc<Host> {
        let mut connector = HttpConnector::new();
        connector.set_nodelay(true);
        Arc::new(Host {
            opts,
            client: Client::builder(TokioExecutor::new()).build(connector),
            entries: Mutex::new(Vec::new()),
            ids: AtomicU64::new(1),
            gate: tokio::sync::Mutex::new(()),
            stopping: watch::channel(false).0,
        })
    }

    /// Start the plugins in the background, and read their features every `poll`. Call it once the
    /// server listens: a plugin may call it back while starting.
    pub fn start(self: &Arc<Self>) {
        let host = self.clone();
        tokio::spawn(async move { host.load_all().await });
        let weak = Arc::downgrade(self);
        let mut stopping = self.stopping.subscribe();
        let every = self.opts.poll;
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = tokio::time::sleep(every) => {}
                    _ = stopping.changed() => return,
                }
                let Some(host) = weak.upgrade() else { return };
                host.refresh_all().await;
            }
        });
    }

    /// Start every plugin in the folder and wait until each has started or failed to.
    pub async fn load_all(self: &Arc<Self>) {
        let _gate = self.gate.lock().await;
        let mut dirs: Vec<PathBuf> = match std::fs::read_dir(&self.opts.dir) {
            Ok(rd) => rd.filter_map(|e| e.ok()).map(|e| e.path()).filter(|p| p.is_dir()).collect(),
            Err(_) => return,
        };
        dirs.sort();
        for dir in dirs {
            let name = dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
            // `.old-x`, `x.new`: an install in progress or left over.
            if name.is_empty() || name.starts_with('.') || name.contains('.') {
                continue;
            }
            let known = self.entries.lock().unwrap().iter().any(|e| e.info.name == name);
            if !known {
                self.load_folder(&dir).await;
            }
        }
    }

    /// The registry, in folder order.
    pub fn plugins(&self) -> Vec<PluginInfo> {
        self.entries.lock().unwrap().iter().map(|e| e.info.clone()).collect()
    }

    /// The features the loaded plugins provide, sorted (`plugins.features`).
    pub fn features(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .entries
            .lock()
            .unwrap()
            .iter()
            .filter(|e| e.info.loaded)
            .flat_map(|e| e.info.features.clone())
            .collect();
        out.sort();
        out.dedup();
        out
    }

    /// The process ids of the running plugins (for tests and the log).
    pub fn pids(&self) -> Vec<u32> {
        self.entries.lock().unwrap().iter().filter_map(|e| e.pid).collect()
    }

    /// The plugin that handles licences, if one runs.
    pub fn licence_provider(&self) -> Option<String> {
        self.entries.lock().unwrap().iter().find(|e| e.licence && e.info.loaded).map(|e| e.info.name.clone())
    }

    /// Stop every plugin and wait until they have exited.
    pub async fn shutdown(&self) {
        let _ = self.stopping.send(true);
        let tasks: Vec<_> = {
            let mut entries = self.entries.lock().unwrap();
            entries.iter_mut().filter_map(|e| {
                if let Some(stop) = &e.stop {
                    let _ = stop.send(true);
                }
                e.task.take()
            }).collect()
        };
        for t in tasks {
            let _ = t.await;
        }
    }

    fn update(&self, id: u64, f: impl FnOnce(&mut Entry)) {
        if let Some(e) = self.entries.lock().unwrap().iter_mut().find(|e| e.id == id) {
            f(e);
        }
    }

    fn insert(&self, entry: Entry) {
        let mut entries = self.entries.lock().unwrap();
        entries.push(entry);
        entries.sort_by(|a, b| a.info.name.cmp(&b.info.name));
    }

    /// The full name of the plugin whose routes are at `/api/ee/<short>/`.
    fn by_short_name(&self, short: &str) -> Option<String> {
        self.entries.lock().unwrap().iter().find(|e| short_name(&e.info.name) == short).map(|e| e.info.name.clone())
    }

    fn conn_of(&self, name: &str) -> Result<Conn, String> {
        let entries = self.entries.lock().unwrap();
        let Some(e) = entries.iter().find(|e| e.info.name == name) else { return Err(String::new()) };
        match (&e.conn, e.info.loaded) {
            (Some(c), true) => Ok(c.clone()),
            _ => Err(e.info.error.clone().unwrap_or_else(|| "isn't running".into())),
        }
    }

    /// Stop the plugin called `name` and take it out of the registry (before replacing its folder).
    async fn unload(&self, name: &str) {
        let gone: Vec<Entry> = {
            let mut entries = self.entries.lock().unwrap();
            let (gone, keep) = std::mem::take(&mut *entries).into_iter().partition(|e| e.info.name == name);
            *entries = keep;
            gone
        };
        for mut e in gone {
            if let Some(stop) = &e.stop {
                let _ = stop.send(true);
            }
            if let Some(t) = e.task.take() {
                let _ = t.await;
            }
        }
    }

    /// List the plugin in `dir` and start it; waits until it has started or failed to. None when
    /// the folder holds no plugin.
    async fn load_folder(self: &Arc<Self>, dir: &Path) -> Option<PluginInfo> {
        let folder = dir.file_name()?.to_str()?.to_string();
        let id = self.ids.fetch_add(1, Ordering::Relaxed);
        let entry = |info: PluginInfo| Entry { id, info, conn: None, core_token: None, licence: false, pid: None,
                                                stop: None, task: None };
        let Some(pj) = install::read_plugin_json(dir) else {
            // A plugin API 1 download: a Python wheel unpacked with its signed manifest.
            let manifest: Value = serde_json::from_slice(&std::fs::read(dir.join(install::MANIFEST_FILE)).ok()?).ok()?;
            let info = PluginInfo {
                name: manifest["package"].as_str().or(manifest["name"].as_str()).unwrap_or(&folder).to_string(),
                version: manifest["version"].as_str().map(String::from),
                api: Some(manifest["api"].as_i64().unwrap_or(1)),
                features: vec![],
                loaded: false,
                error: Some(format!("written for plugin API {}, this Wednesday has {API}", manifest["api"].as_i64().unwrap_or(1))),
            };
            log::warn!("plugin {} skipped: {}", info.name, info.error.as_deref().unwrap_or(""));
            self.insert(entry(info.clone()));
            return Some(info);
        };
        let mut info = PluginInfo { name: pj.name.clone(), version: pj.version.clone(), api: pj.api, features: vec![],
                                    loaded: false, error: None };
        let exe = pj.exe.clone().unwrap_or_default();
        if pj.api != Some(API) {
            let api = pj.api.map_or("None".to_string(), |a| a.to_string());
            info.error = Some(format!("written for plugin API {api}, this Wednesday has {API}"));
        } else if !install::safe_exe(&exe) || !dir.join(&exe).is_file() {
            info.error = Some("has no program to run (\"exe\" in its plugin.json)".into());
        } else if self.entries.lock().unwrap().iter().any(|e| e.info.name == pj.name) {
            info.error = Some(format!("another plugin is already called {}", pj.name));
        }
        if let Some(why) = &info.error {
            log::warn!("plugin {} skipped: {why}", info.name);
            self.insert(entry(info.clone()));
            return Some(info);
        }
        let (stop_tx, stop_rx) = watch::channel(false);
        let (ready_tx, ready_rx) = oneshot::channel();
        let mut e = entry(info);
        e.stop = Some(stop_tx);
        self.insert(e);
        let run = Run { id, name: pj.name.clone(), dir: dir.to_path_buf(), exe: dir.join(&exe) };
        let task = tokio::spawn(supervise(Arc::downgrade(self), run, stop_rx, ready_tx));
        self.update(id, |e| e.task = Some(task));
        let _ = ready_rx.await;
        self.entries.lock().unwrap().iter().find(|e| e.id == id).map(|e| e.info.clone())
    }

    /// Read every running plugin's features again.
    pub async fn refresh_all(&self) {
        let names: Vec<String> = self.entries.lock().unwrap().iter().filter(|e| e.info.loaded).map(|e| e.info.name.clone()).collect();
        for name in names {
            self.refresh(&name).await;
        }
    }

    /// Read a plugin's `/plugin` again: its version and features change with its licence.
    pub async fn refresh(&self, name: &str) {
        let Ok(conn) = self.conn_of(name) else { return };
        match self.call(&conn, Method::GET, "/plugin", None).await {
            Ok((StatusCode::OK, about)) => {
                let mut entries = self.entries.lock().unwrap();
                if let Some(e) = entries.iter_mut().find(|e| e.info.name == name && e.info.loaded) {
                    apply_about(&mut e.info, &about);
                }
            }
            Ok((status, _)) => log::warn!("plugin {name} answered /plugin with {status}"),
            Err(e) => log::warn!("plugin {name} didn't answer /plugin: {e}"),
        }
    }

    /// A JSON call to a plugin: its status and JSON body (null when it isn't JSON).
    async fn call(&self, conn: &Conn, method: Method, path: &str, body: Option<&Value>) -> Result<(StatusCode, Value), String> {
        let mut req = axum::http::Request::builder()
            .method(method)
            .uri(format!("http://127.0.0.1:{}{path}", conn.port))
            .header(header::AUTHORIZATION, format!("Bearer {}", conn.token));
        let body = match body {
            Some(v) => {
                req = req.header(header::CONTENT_TYPE, "application/json");
                Body::from(v.to_string())
            }
            None => Body::empty(),
        };
        let req = req.body(body).map_err(|e| e.to_string())?;
        let res = tokio::time::timeout(CALL_TIMEOUT, self.client.request(req))
            .await
            .map_err(|_| "timed out".to_string())?
            .map_err(|e| e.to_string())?;
        let status = res.status();
        let bytes = tokio::time::timeout(CALL_TIMEOUT, axum::body::to_bytes(Body::new(res.into_body()), 16 * 1024 * 1024))
            .await
            .map_err(|_| "timed out".to_string())?
            .map_err(|e| e.to_string())?;
        Ok((status, serde_json::from_slice(&bytes).unwrap_or(Value::Null)))
    }

    /// The plugin a `/api/plugin-host` request comes from, by its token (compared in constant time).
    fn authorize(&self, headers: &HeaderMap) -> Option<String> {
        let given = headers.get(header::AUTHORIZATION)?.to_str().ok()?.strip_prefix("Bearer ")?.trim().as_bytes().to_vec();
        let entries = self.entries.lock().unwrap();
        let mut found = None;
        for e in entries.iter() {
            if let Some(token) = &e.core_token {
                if same(token.as_bytes(), &given) {
                    found = Some(e.info.name.clone());
                }
            }
        }
        found
    }

    /// Download, check and unpack the plugin a licence key gives, then start it (in place of an
    /// older copy). Returns its manifest and how it started.
    pub async fn install(self: &Arc<Self>, key: &str) -> Result<(install::Manifest, PluginInfo), install::InstallError> {
        let _gate = self.gate.lock().await;
        let device = install::device_id(self.opts.store.as_ref()).await;
        let answer = install::fetch(key, &device, &self.opts.download_url).await?;
        let (manifest, zip) = install::verify(&answer, &self.opts.trust)?;
        let (m, root) = (manifest.clone(), self.opts.dir.clone());
        tokio::task::spawn_blocking(move || install::extract(&m, &zip, &root))
            .await
            .map_err(|_| install::InstallError("The download is damaged; try again".into()))??;
        self.unload(&manifest.name).await;
        let dest = install::swap(&self.opts.dir, &manifest.name)?;
        log::info!("{} {} installed in {}", manifest.name, manifest.version, dest.display());
        let info = self.load_folder(&dest).await.unwrap_or_else(|| PluginInfo {
            name: manifest.name.clone(), version: Some(manifest.version.clone()), api: Some(API), features: vec![],
            loaded: false, error: Some("its folder holds no plugin".into()),
        });
        Ok((manifest, info))
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.stopping.send(true);
        if let Ok(entries) = self.entries.lock() {
            for e in entries.iter() {
                if let Some(stop) = &e.stop {
                    let _ = stop.send(true);
                }
            }
        }
    }
}

/// Constant-time equality, for tokens.
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn apply_about(info: &mut PluginInfo, about: &Value) {
    if let Some(v) = about["version"].as_str() {
        info.version = Some(v.to_string());
    }
    let mut features: Vec<String> = Vec::new();
    for f in about["features"].as_array().into_iter().flatten().filter_map(|f| f.as_str()) {
        if !features.iter().any(|x| x == f) {
            features.push(f.to_string());
        }
    }
    info.features = features;
}

struct Run {
    id: u64,
    name: String,
    dir: PathBuf,
    exe: PathBuf,
}

struct Launched {
    child: tokio::process::Child,
    /// Kept open while it runs: the plugin exits when it closes.
    _stdin: Option<tokio::process::ChildStdin>,
}

/// Why a launch failed, and whether trying again could help.
struct Failed(String, bool);

/// Run one plugin: start it, wait for it to stop, restart it a few times with a growing wait.
async fn supervise(host: Weak<Host>, run: Run, mut stop: watch::Receiver<bool>, ready: oneshot::Sender<()>) {
    let mut ready = Some(ready);
    let mut restarts = 0u32;
    loop {
        let Some(h) = host.upgrade() else { return };
        let launched = launch(&h, &run).await;
        let backoff = h.opts.restart_backoff;
        drop(h);
        let mut launched = match launched {
            Ok(l) => l,
            Err(Failed(why, again)) => {
                log::warn!("plugin {} didn't start: {why}", run.name);
                if let Some(h) = host.upgrade() {
                    h.update(run.id, |e| {
                        e.info.loaded = false;
                        e.info.error = Some(why);
                        e.info.features.clear();
                        e.conn = None;
                        e.core_token = None;
                        e.pid = None;
                        e.licence = false;
                    });
                }
                if let Some(r) = ready.take() {
                    let _ = r.send(());
                }
                if !again || !wait_restart(&mut restarts, backoff, &mut stop).await {
                    return;
                }
                continue;
            }
        };
        if let Some(r) = ready.take() {
            let _ = r.send(());
        }
        let started = Instant::now();
        let status = tokio::select! {
            status = launched.child.wait() => status,
            _ = stop.changed() => {
                let _ = launched.child.kill().await;
                if let Some(h) = host.upgrade() {
                    h.update(run.id, |e| { e.info.loaded = false; e.conn = None; e.core_token = None; e.pid = None; });
                }
                return;
            }
        };
        let why = match status {
            Ok(s) => format!("stopped ({s})"),
            Err(e) => format!("stopped ({e})"),
        };
        log::warn!("plugin {} {why}", run.name);
        let Some(h) = host.upgrade() else { return };
        h.update(run.id, |e| {
            e.info.loaded = false;
            e.info.error = Some(why);
            e.info.features.clear();
            e.conn = None;
            e.core_token = None;
            e.pid = None;
            e.licence = false;
        });
        drop(h);
        if started.elapsed() > STABLE {
            restarts = 0;
        }
        if !wait_restart(&mut restarts, backoff, &mut stop).await {
            return;
        }
    }
}

/// Wait before a restart; false when out of restarts or asked to stop.
async fn wait_restart(restarts: &mut u32, backoff: Duration, stop: &mut watch::Receiver<bool>) -> bool {
    if *restarts >= MAX_RESTARTS || *stop.borrow() {
        return false;
    }
    *restarts += 1;
    let wait = backoff * 2u32.pow(*restarts - 1);
    tokio::select! {
        _ = tokio::time::sleep(wait) => !*stop.borrow(),
        _ = stop.changed() => false,
    }
}

/// Start the plugin, read the port it listens on, and ask what it is.
async fn launch(host: &Arc<Host>, run: &Run) -> Result<Launched, Failed> {
    let token = install::hex(&install::random_bytes::<32>());
    let core_token = install::hex(&install::random_bytes::<32>());
    let device = install::device_id(host.opts.store.as_ref()).await;
    let state = run.dir.join("state");
    let _ = std::fs::create_dir_all(&state);
    host.update(run.id, |e| e.core_token = Some(core_token.clone()));

    let mut cmd = tokio::process::Command::new(&run.exe);
    cmd.current_dir(&run.dir)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    for name in NOT_PASSED {
        cmd.env_remove(name);
    }
    cmd.env("WEDNESDAY_PLUGIN_TOKEN", &token)
        .env("WEDNESDAY_CORE_URL", &host.opts.core_url)
        .env("WEDNESDAY_CORE_TOKEN", &core_token)
        .env("WEDNESDAY_DEVICE_ID", &device)
        .env("WEDNESDAY_DATA_DIR", &state);
    match &host.opts.licence_server {
        Some(url) => cmd.env(install::SERVER_ENV, url),
        None => cmd.env_remove(install::SERVER_ENV),
    };
    #[cfg(windows)]
    cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console window pops up
    let mut child = cmd.spawn().map_err(|e| Failed(format!("didn't start: {e}"), false))?;
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().expect("stdout is piped");
    if let Some(stderr) = child.stderr.take() {
        let name = run.name.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                log::info!("plugin {name}: {line}");
            }
        });
    }
    let mut lines = BufReader::new(stdout).lines();
    let name = run.name.clone();
    let port = tokio::time::timeout(host.opts.start_timeout, async {
        while let Ok(Some(line)) = lines.next_line().await {
            match line.trim().strip_prefix(HANDSHAKE) {
                Some(rest) => return rest.trim().parse::<u16>().ok(),
                None => log::info!("plugin {name}: {line}"),
            }
        }
        None
    })
    .await;
    let port = match port {
        Ok(Some(p)) if p > 0 => p,
        Ok(_) => {
            let _ = child.kill().await;
            let status = child.wait().await.map(|s| s.to_string()).unwrap_or_default();
            return Err(Failed(if status.is_empty() { "didn't start".into() } else { format!("didn't start ({status})") }, true));
        }
        Err(_) => {
            let _ = child.kill().await;
            return Err(Failed("didn't start".into(), true));
        }
    };
    let name = run.name.clone();
    tokio::spawn(async move {
        while let Ok(Some(line)) = lines.next_line().await {
            log::info!("plugin {name}: {line}");
        }
    });
    let conn = Conn { port, token };
    let about = match host.call(&conn, Method::GET, "/plugin", None).await {
        Ok((StatusCode::OK, about)) if about.is_object() => about,
        Ok((status, _)) => {
            let _ = child.kill().await;
            return Err(Failed(format!("didn't answer /plugin ({status})"), true));
        }
        Err(e) => {
            let _ = child.kill().await;
            return Err(Failed(format!("didn't answer /plugin: {e}"), true));
        }
    };
    if about["api"].as_i64() != Some(API) {
        let _ = child.kill().await;
        let api = about["api"].as_i64().map_or("None".to_string(), |a| a.to_string());
        return Err(Failed(format!("written for plugin API {api}, this Wednesday has {API}"), false));
    }
    if about["name"].as_str().is_some_and(|n| n != run.name) {
        log::warn!("plugin {} calls itself {} in /plugin", run.name, about["name"]);
    }
    let licence = matches!(host.call(&conn, Method::GET, "/licence", None).await, Ok((StatusCode::OK, _)));
    let pid = child.id();
    let mut entries = host.entries.lock().unwrap();
    let taken = licence && entries.iter().any(|e| e.id != run.id && e.licence && e.info.loaded);
    if taken {
        log::warn!("plugin {} handles licences, but another plugin already does; its licence screen is off", run.name);
    }
    if let Some(e) = entries.iter_mut().find(|e| e.id == run.id) {
        apply_about(&mut e.info, &about);
        e.info.loaded = true;
        e.info.error = None;
        e.conn = Some(conn);
        e.licence = licence && !taken;
        e.pid = pid;
    }
    drop(entries);
    log::info!("plugin {} {} running on 127.0.0.1:{port}", run.name, about["version"].as_str().unwrap_or(""));
    Ok(Launched { child, _stdin: stdin })
}
