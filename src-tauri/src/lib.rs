//! Wednesday's desktop shell (Tauri, #92).
//!
//! The shell runs Wednesday's server (`server-rs`, #96) in this process: the scan engine with the
//! TradingView feed, the API and the dashboard build, on 127.0.0.1 (port 8000 when free). It shows
//! the dashboard in native windows: the main one, and a window per popped-out chart, so each can
//! sit on its own monitor. The page calls the shell through four commands (see
//! `web/src/desktop.ts`): open a chart window, report and arrange the windows (workspaces), and
//! save a chart snapshot.
//!
//! Where the windows were when the app last closed is kept in `windows.json` in the app's data
//! folder and restored on the next start. Closing the main window closes everything and stops
//! the server with the process.
//!
//! The installer bundles the dashboard build (`web/`), the TradingView helper (`tradingview/`) and
//! the node that runs it (`node/`) in the app's resources (`tauri.release.conf.json`); a
//! development build uses this checkout's `web/dist` and `tradingview/`.
//!
//! `--forget-secrets` removes the secrets Wednesday saved in the OS credential store and exits:
//! the installer runs it for a fresh install.

mod screens;

use std::fs;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use base64::Engine as _;
use serde::{Deserialize, Serialize};
use tauri::{
    AppHandle, LogicalPosition, LogicalSize, Manager, RunEvent, State, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::DialogExt;

use screens::{fit_on_screen, Rect};

const TITLE: &str = "Wednesday";
const MAIN: &str = "main";
const CHART_PREFIX: &str = "chart-";
const MAIN_MIN: (f64, f64) = (960.0, 600.0);
const CHART_MIN: (f64, f64) = (480.0, 320.0);

/// A window as the dashboard's workspaces store it: route ("#" for the dashboard,
/// "#popout/5M/A" for a chart), logical position and size, and whether it was maximised.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Geometry {
    pub route: String,
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    #[serde(default)]
    pub maximized: bool,
}

#[derive(Default)]
struct Shell {
    base: Mutex<Option<String>>, // http://127.0.0.1:<port>, once the server answers
    next_chart: AtomicU32,
}

impl Shell {
    fn base(&self) -> Result<String, String> {
        self.base.lock().unwrap().clone().ok_or_else(|| "The screener is still starting".to_string())
    }
}

/// A route the dashboard knows: `#`, `#popout/<tf>` or `#popout/<tf>/<A|B|C>`.
fn clean_route(route: &str) -> Option<String> {
    let r = route.strip_prefix('#').unwrap_or(route);
    if r.is_empty() {
        return Some("#".into());
    }
    let parts: Vec<&str> = r.split('/').collect();
    let tf_ok = |tf: &str| !tf.is_empty() && tf.len() <= 8 && tf.chars().all(|c| c.is_ascii_alphanumeric());
    match parts.as_slice() {
        ["popout", tf] if tf_ok(tf) => Some(format!("#popout/{tf}")),
        ["popout", tf, g] if tf_ok(tf) && matches!(*g, "A" | "B" | "C") => Some(format!("#popout/{tf}/{g}")),
        _ => None,
    }
}

fn session_file(app: &AppHandle) -> Option<PathBuf> {
    app.path().app_data_dir().ok().map(|d| d.join("windows.json"))
}

fn load_session(app: &AppHandle) -> Vec<Geometry> {
    session_file(app)
        .and_then(|f| fs::read_to_string(f).ok())
        .and_then(|s| serde_json::from_str::<Vec<Geometry>>(&s).ok())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|g| clean_route(&g.route).map(|route| Geometry { route, ..g }))
        .collect()
}

fn save_session(app: &AppHandle) {
    // Only while the dashboard is open: after it closed, the snapshot would drop it.
    if app.get_webview_window(MAIN).is_none() {
        return;
    }
    let (Some(file), windows) = (session_file(app), snapshot(app)) else { return };
    if let Some(dir) = file.parent() {
        let _ = fs::create_dir_all(dir);
    }
    if let Ok(json) = serde_json::to_string_pretty(&windows) {
        if let Err(e) = fs::write(&file, json) {
            log::warn!("couldn't remember the windows: {e}");
        }
    }
}

fn monitors(app: &AppHandle) -> Vec<Rect> {
    app.available_monitors()
        .unwrap_or_default()
        .iter()
        .map(|m| {
            let s = m.scale_factor();
            Rect {
                x: (m.position().x as f64 / s) as i32,
                y: (m.position().y as f64 / s) as i32,
                width: (m.size().width as f64 / s) as u32,
                height: (m.size().height as f64 / s) as u32,
            }
        })
        .collect()
}

/// Where a window is now, in logical pixels, with the route its page shows.
fn geometry_of(w: &WebviewWindow) -> Option<Geometry> {
    let scale = w.scale_factor().ok()?;
    let pos = w.outer_position().ok()?.to_logical::<f64>(scale);
    let size = w.inner_size().ok()?.to_logical::<f64>(scale); // windows are sized by their inside too
    let route = if w.label() == MAIN {
        "#".to_string()
    } else {
        // A pop-out's timeframe can change: read it from the page's address.
        w.url().ok().and_then(|u| u.fragment().map(|f| format!("#{f}"))).and_then(|r| clean_route(&r))?
    };
    Some(Geometry {
        route,
        x: pos.x.round() as i32,
        y: pos.y.round() as i32,
        width: size.width.round() as u32,
        height: size.height.round() as u32,
        maximized: w.is_maximized().unwrap_or(false),
    })
}

/// Every open window, the dashboard first.
fn snapshot(app: &AppHandle) -> Vec<Geometry> {
    let mut windows: Vec<(bool, Geometry)> = app
        .webview_windows()
        .values()
        .filter_map(|w| geometry_of(w).map(|g| (w.label() != MAIN, g)))
        .collect();
    windows.sort_by_key(|(chart, _)| *chart);
    windows.into_iter().map(|(_, g)| g).collect()
}

fn place(app: &AppHandle, w: &WebviewWindow, g: &Geometry) {
    let g = fit_on_screen(g, &monitors(app));
    if g.maximized {
        let _ = w.maximize();
        return;
    }
    let _ = w.unmaximize();
    let _ = w.set_position(LogicalPosition::new(g.x, g.y));
    let _ = w.set_size(LogicalSize::new(g.width, g.height));
}

/// A chart in its own window, at `geometry` when given (kept on a monitor that exists).
fn open_chart_window(app: &AppHandle, shell: &Shell, route: &str, geometry: Option<&Geometry>) -> Result<(), String> {
    let base = shell.base()?;
    let url = tauri::Url::parse(&format!("{base}/{route}")).map_err(|e| e.to_string())?;
    let tf = route.split('/').nth(1).unwrap_or("");
    let label = format!("{CHART_PREFIX}{}", shell.next_chart.fetch_add(1, Ordering::Relaxed));
    let mut b = WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
        .title(format!("{TITLE} {tf}").trim())
        .min_inner_size(CHART_MIN.0, CHART_MIN.1)
        .inner_size(1100.0, 700.0);
    if let Some(g) = geometry {
        let g = fit_on_screen(g, &monitors(app));
        b = b.position(g.x as f64, g.y as f64).inner_size(g.width as f64, g.height as f64).maximized(g.maximized);
    }
    b.build().map(|_| ()).map_err(|e| e.to_string())
}

// ---- commands the dashboard calls (web/src/desktop.ts) ----

#[tauri::command]
async fn open_chart(app: AppHandle, shell: State<'_, Shell>, tf: String, group: Option<String>) -> Result<bool, String> {
    let tf: String = tf.chars().filter(|c| c.is_ascii_alphanumeric()).take(8).collect();
    let tf = if tf.is_empty() { "1H".to_string() } else { tf };
    let route = match group.as_deref() {
        Some(g @ ("A" | "B" | "C")) => format!("#popout/{tf}/{g}"),
        _ => format!("#popout/{tf}"),
    };
    open_chart_window(&app, &shell, &route, None)?;
    Ok(true)
}

#[tauri::command]
async fn window_layout(app: AppHandle) -> Result<Vec<Geometry>, String> {
    Ok(snapshot(&app))
}

/// Open a workspace's windows: the dashboard moves, the chart windows close and reopen.
#[tauri::command]
async fn arrange(app: AppHandle, shell: State<'_, Shell>, windows: Vec<Geometry>) -> Result<bool, String> {
    for (label, w) in app.webview_windows() {
        if label.starts_with(CHART_PREFIX) {
            let _ = w.destroy();
        }
    }
    for g in windows.iter().take(12) {
        let Some(route) = clean_route(&g.route) else { continue };
        if route == "#" {
            if let Some(main) = app.get_webview_window(MAIN) {
                place(&app, &main, g);
            }
        } else {
            open_chart_window(&app, &shell, &route, Some(g))?;
        }
    }
    Ok(true)
}

/// Save a chart snapshot (base64 PNG) where the trader picks; the path, or None when cancelled.
#[tauri::command]
async fn save_png(app: AppHandle, data: String, name: String) -> Result<Option<String>, String> {
    let file_name = PathBuf::from(&name).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let mut safe: String = file_name.chars().filter(|c| c.is_ascii_alphanumeric() || "-_.".contains(*c)).collect();
    if safe.is_empty() {
        safe = "wednesday.png".into();
    }
    if !safe.to_ascii_lowercase().ends_with(".png") {
        safe.push_str(".png");
    }
    let mut dialog = app.dialog().file().set_file_name(&safe).add_filter("PNG images", &["png"]);
    if let Ok(pictures) = app.path().picture_dir() {
        dialog = dialog.set_directory(pictures);
    }
    let Some(picked) = dialog.blocking_save_file() else { return Ok(None) };
    let path = picked.into_path().map_err(|e| e.to_string())?;
    let bytes = base64::engine::general_purpose::STANDARD.decode(data.as_bytes()).map_err(|e| e.to_string())?;
    fs::write(&path, bytes).map_err(|e| format!("Couldn't save {}: {e}", path.display()))?;
    Ok(Some(path.display().to_string()))
}

/// Development check (`WEDNESDAY_SELFTEST=1`, debug builds only): the dashboard page calls the
/// shell's commands as it would, the result is printed, and the app quits.
#[cfg(debug_assertions)]
fn selftest(app: &AppHandle) {
    use std::time::Duration;
    let Some(main) = app.get_webview_window(MAIN) else { return };
    std::thread::sleep(Duration::from_secs(5)); // the dashboard loads
    if std::env::var("WEDNESDAY_SELFTEST").as_deref() == Ok("channel") {
        // Link groups and drawings go between windows over BroadcastChannel: check one reaches another.
        let shell = app.state::<Shell>();
        let _ = open_chart_window(app, &shell, "#popout/5M", None);
        std::thread::sleep(Duration::from_secs(4));
        if let Some((_, chart)) = app.webview_windows().into_iter().find(|(l, _)| l.starts_with(CHART_PREFIX)) {
            let _ = chart.eval(r#"new BroadcastChannel("wed.selftest").onmessage = (e) =>
                window.__TAURI__.core.invoke("selftest_result", { report: JSON.stringify({ channel: e.data }) });"#);
            std::thread::sleep(Duration::from_secs(1));
            let _ = main.eval(r#"new BroadcastChannel("wed.selftest").postMessage("reached the chart window");"#);
        }
        std::thread::sleep(Duration::from_secs(6));
        eprintln!("SELFTEST {{\"channel\": null}}");
        app.exit(1);
        return;
    }
    let script = r##"(async () => {
      const r = {};
      try {
        const invoke = window.__TAURI__?.core?.invoke;
        r.tauri = !!invoke;
        r.page = location.origin;
        r.popOutButton = !!document.querySelector('[aria-label="Open the chart in its own window"]');
        r.open = await invoke("open_chart", { tf: "5M", group: "A" });
        await new Promise((ok) => setTimeout(ok, 3000));
        r.layout = await invoke("window_layout");
        r.arrange = await invoke("arrange", { windows: [
          { route: "#", x: 40, y: 40, width: 1200, height: 800, maximized: false },
          { route: "#popout/15M/B", x: 9000, y: 9000, width: 900, height: 600, maximized: false },
        ] });
        await new Promise((ok) => setTimeout(ok, 3000));
        r.after = await invoke("window_layout");
      } catch (e) {
        r.error = String(e);
      }
      const report = JSON.stringify(r);
      history.replaceState(null, "", "#selftest=" + encodeURIComponent(report)); // read on a timeout
      await window.__TAURI__?.core?.invoke?.("selftest_result", { report });
    })()"##;
    let _ = main.eval(script);
    std::thread::sleep(Duration::from_secs(20));
    let url = main.url().map(|u| u.to_string()).unwrap_or_default();
    let report = url.split_once("#selftest=").map(|(_, r)| r.replace("%22", "\"").replace("%20", " ")).unwrap_or_default();
    eprintln!("SELFTEST timed out; page at {url}\nreport: {report}");
    app.exit(1);
}

/// The self-test's report (debug builds with WEDNESDAY_SELFTEST only; ignored otherwise).
#[tauri::command]
fn selftest_result(app: AppHandle, report: String) {
    if cfg!(debug_assertions) && std::env::var_os("WEDNESDAY_SELFTEST").is_some() {
        eprintln!("SELFTEST {report}");
        save_session(&app);
        app.exit(0);
    }
}

fn show_failure(app: &AppHandle, message: &str) {
    if let Some(main) = app.get_webview_window(MAIN) {
        let arg = serde_json::to_string(message).unwrap_or_else(|_| "\"\"".into());
        let _ = main.eval(format!("window.wednesdayFailed && window.wednesdayFailed({arg})"));
    }
}

/// The dashboard's address: `preferred` when it's free (the layout is kept per address), else any
/// free local port.
fn bind_front(preferred: u16) -> std::io::Result<TcpListener> {
    TcpListener::bind(("127.0.0.1", preferred)).or_else(|_| TcpListener::bind(("127.0.0.1", 0)))
}

/// The dashboard build: `XAU_UI_DIR`, else the one the installer bundles (`<resources>/web`), else
/// this checkout's `web/dist` (development).
fn ui_dir(resources: Option<&Path>, home: &Path) -> PathBuf {
    let set = wednesday_rs::config::env_setting("XAU_UI_DIR", &home.join(".env")).is_some();
    match resources.map(|r| r.join("web")).filter(|w| !set && w.join("index.html").is_file()) {
        Some(bundled) => bundled,
        None => wednesday_rs::ui::ui_dir(home),
    }
}

/// `--forget-secrets` (the installer's fresh install): remove the saved secrets; true when given.
pub fn forget_secrets(args: &[String]) -> bool {
    if !args.iter().any(|a| a == "--forget-secrets") {
        return false;
    }
    let gone = wednesday_rs::alerts::secrets::forget_all();
    println!("removed {gone} saved secret(s)");
    true
}

pub fn run() {
    let app = tauri::Builder::default()
        // A second copy would scan twice into the same database: bring the first one forward instead.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(main) = app.get_webview_window(MAIN) {
                let _ = main.unminimize();
                let _ = main.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(Shell::default())
        .invoke_handler(tauri::generate_handler![open_chart, window_layout, arrange, save_png, selftest_result])
        .setup(|app| {
            let handle = app.handle().clone();
            let last = load_session(&handle);
            let first = last.iter().find(|g| g.route == "#").cloned();

            // The splash page (src-tauri/splash) shows while the server starts.
            let mut main = WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
                .title(TITLE)
                .min_inner_size(MAIN_MIN.0, MAIN_MIN.1)
                .inner_size(1440.0, 900.0);
            if let Some(g) = &first {
                let g = fit_on_screen(g, &monitors(&handle));
                main = main.position(g.x as f64, g.y as f64).inner_size(g.width as f64, g.height as f64).maximized(g.maximized);
            }
            let main = main.build()?;

            let closing = handle.clone();
            main.on_window_event(move |event| match event {
                WindowEvent::CloseRequested { .. } => save_session(&closing),
                // The server stops with the dashboard, so its chart windows go too.
                WindowEvent::Destroyed => closing.exit(0),
                _ => {}
            });

            // The dashboard's address, bound now so it stays ours while the engine starts.
            let listener = match bind_front(8000) {
                Ok(l) => l,
                Err(e) => {
                    show_failure(&handle, &format!("Couldn't open a local port for the dashboard ({e})."));
                    return Ok(());
                }
            };
            let port = listener.local_addr().map(|a| a.port()).unwrap_or(8000);
            let resources = handle.path().resource_dir().ok();
            if let Some(r) = resources.as_ref().filter(|r| r.join("tradingview").join("feed.mjs").is_file()) {
                wednesday_rs::engine::feed::set_bundle_dir(r.clone());
            }
            let home = wednesday_rs::config::home_dir();
            wednesday_rs::logfile::init(Some(&home.join("logs").join("screener.log")));
            let ui = ui_dir(resources.as_deref(), &home);
            let (ready, started) = std::sync::mpsc::channel::<Result<(), String>>();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = fs::create_dir_all(&home) {
                    let _ = ready.send(Err(format!("Couldn't create the data folder {} ({e}).", home.display())));
                    return;
                }
                let store = wednesday_rs::open_store(&home).await;
                let rt = match wednesday_rs::start_engine(&home, store.clone()).await {
                    Ok(rt) => rt,
                    Err(e) => {
                        let _ = ready.send(Err(format!("The screener couldn't start:\n\n{e}")));
                        return;
                    }
                };
                let _ = ready.send(Ok(()));
                if let Err(e) = wednesday_rs::serve_std_standalone(listener, ui, store, rt).await {
                    log::error!("the server stopped: {e}");
                }
            });
            std::thread::spawn(move || {
                let shell = handle.state::<Shell>();
                match started.recv() {
                    Ok(Ok(())) => {}
                    Ok(Err(e)) => return show_failure(&handle, &e),
                    Err(_) => return show_failure(&handle, "The screener stopped while starting. Restart Wednesday."),
                }
                let base = format!("http://127.0.0.1:{port}");
                *shell.base.lock().unwrap() = Some(base.clone());
                if let (Some(main), Ok(url)) = (handle.get_webview_window(MAIN), tauri::Url::parse(&base)) {
                    let _ = main.navigate(url);
                }
                for g in last.iter().filter(|g| g.route != "#") {
                    if let Err(e) = open_chart_window(&handle, &shell, &g.route, Some(g)) {
                        log::warn!("couldn't reopen {}: {e}", g.route);
                    }
                }
                #[cfg(debug_assertions)]
                if std::env::var_os("WEDNESDAY_SELFTEST").is_some() {
                    selftest(&handle);
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("couldn't start the Wednesday desktop shell");

    app.run(|app, event| {
        if let RunEvent::ExitRequested { .. } = event {
            save_session(app); // quitting (Cmd+Q) skips the main window's CloseRequested
        }
    });
}

#[cfg(test)]
mod tests {
    use super::clean_route;

    #[test]
    fn routes_the_dashboard_knows() {
        assert_eq!(clean_route("#").as_deref(), Some("#"));
        assert_eq!(clean_route("").as_deref(), Some("#"));
        assert_eq!(clean_route("#popout/5M").as_deref(), Some("#popout/5M"));
        assert_eq!(clean_route("popout/15M/B").as_deref(), Some("#popout/15M/B"));
    }

    #[test]
    fn anything_else_is_refused() {
        for bad in ["#popout/", "#popout/5M/D", "#popout/../x", "#settings", "#popout/5M/A/extra", "#popout/123456789"] {
            assert_eq!(clean_route(bad), None, "{bad}");
        }
    }
}
