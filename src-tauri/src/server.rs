//! The Python sidecar: `wednesday-server` (see `run_sidecar` in `src/wednesday/desktop.py`).
//!
//! Which program runs it, in order: `WEDNESDAY_SERVER` (a path, for testing), the PyInstaller
//! build the installer bundles (`server/wednesday-server` in the app's resources, see
//! `tauri.release.conf.json`), one next to the shell's executable, else `uv run wednesday-server`
//! in this checkout (development).

use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// How long the first scan may take before the shell gives up (history loads on the first start).
const START_TIMEOUT: Duration = Duration::from_secs(180);

pub struct Server {
    child: Child,
    stderr: Arc<Mutex<Vec<String>>>, // the last lines, to explain a failed start
}

/// Whether the Rust server (`server-rs`, #96) sits in front of the Python one: yes, unless
/// `WEDNESDAY_GATEWAY=0`, which shows the Python server directly (to rule the front out).
pub fn gateway_enabled() -> bool {
    std::env::var("WEDNESDAY_GATEWAY").map(|v| v != "0").unwrap_or(true)
}

/// The dashboard's address, bound now so it stays ours while the Python server starts:
/// `preferred` when it's free (the layout is kept per address), else any free port.
pub fn bind_front(preferred: u16) -> std::io::Result<TcpListener> {
    TcpListener::bind(("127.0.0.1", preferred)).or_else(|_| TcpListener::bind(("127.0.0.1", 0)))
}

/// Any free local port (for the Python server behind the Rust one).
pub fn free_port() -> Option<u16> {
    TcpListener::bind(("127.0.0.1", 0)).and_then(|l| l.local_addr()).map(|a| a.port()).ok()
}

/// `preferred` when it's free (the dashboard's layout is kept per address), else any free port.
pub fn pick_port(preferred: u16) -> u16 {
    if TcpListener::bind(("127.0.0.1", preferred)).is_ok() {
        return preferred;
    }
    TcpListener::bind(("127.0.0.1", 0)).and_then(|l| l.local_addr()).map(|a| a.port()).unwrap_or(preferred)
}

fn program(resources: Option<PathBuf>) -> Command {
    if let Ok(path) = std::env::var("WEDNESDAY_SERVER") {
        return Command::new(path);
    }
    let name = if cfg!(windows) { "wednesday-server.exe" } else { "wednesday-server" };
    let bundled = resources.map(|r| r.join("server").join(name));
    let beside = std::env::current_exe().ok().and_then(|e| e.parent().map(|d| d.join(name)));
    if let Some(found) = [bundled, beside].into_iter().flatten().find(|p| p.is_file()) {
        return Command::new(found);
    }
    let checkout: PathBuf = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let mut uv = Command::new("uv");
    uv.arg("run").arg("--project").arg(checkout).arg("wednesday-server");
    uv
}

impl Server {
    pub fn start(port: u16, resources: Option<PathBuf>) -> Result<Server, String> {
        let mut cmd = program(resources);
        cmd.arg("--port")
            .arg(port.to_string())
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd.spawn().map_err(|e| {
            format!("Couldn't start the screener ({e}).\n\nReinstall Wednesday, or in a checkout run `make install` first.")
        })?;
        let stderr = Arc::new(Mutex::new(Vec::new()));
        if let Some(pipe) = child.stderr.take() {
            let lines = stderr.clone();
            std::thread::spawn(move || {
                for line in BufReader::new(pipe).lines().map_while(Result::ok) {
                    let mut l = lines.lock().unwrap();
                    l.push(line);
                    let excess = l.len().saturating_sub(20);
                    l.drain(..excess);
                }
            });
        }
        Ok(Server { child, stderr })
    }

    pub fn stop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }

    /// None while running, else why it stopped (its last error lines).
    fn exited(&mut self) -> Option<String> {
        let status = self.child.try_wait().ok()??;
        let tail = self.stderr.lock().unwrap().join("\n");
        Some(if tail.trim().is_empty() { format!("The screener stopped ({status}).") } else { tail })
    }
}

/// Wait until the server answers on `port`; Err with the reason when it stopped or never came up.
/// Locks `server` only briefly per check, so quitting isn't held up.
pub fn wait_ready(port: u16, server: &Mutex<Option<Server>>) -> Result<(), String> {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    let start = Instant::now();
    loop {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok() {
            return Ok(());
        }
        match server.lock().unwrap().as_mut() {
            None => return Err("The screener was stopped.".into()),
            Some(s) => {
                if let Some(why) = s.exited() {
                    return Err(format!("The screener couldn't start:\n\n{why}"));
                }
            }
        }
        if start.elapsed() > START_TIMEOUT {
            return Err(format!("The screener didn't answer on port {port} within {} s.", START_TIMEOUT.as_secs()));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}
