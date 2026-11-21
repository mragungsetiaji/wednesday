//! `wednesday-rs [--host 127.0.0.1] [--port 8000] [--home DIR] [--db URL] [--ui-dir DIR] [--source ID]`: Wednesday's
//! server. It runs the scan engine and serves the API and the built dashboard on
//! http://<host>:<port>. `--home` is the data folder (its `.env` and relative database path;
//! default `WEDNESDAY_HOME`, `%LOCALAPPDATA%\Wednesday` or `~/.wednesday`); `--db` overrides the
//! database URL (`none`: no database); `--ui-dir` the dashboard build (default `XAU_UI_DIR`, else
//! `web/dist`); `--source` the data source over the saved one (`synthetic` for development).
//! `--host` and `--port` default to `XAU_HOST` and `XAU_PORT`. Anything but a
//! loopback host needs `--insecure`: the dashboard has no login.
//!
//! For development only, `--backend <port>` puts it in front of a Python server
//! (`wednesday --serve --port <port>`) that answers the routes not handled here; with `--engine`
//! the engine runs here and Python follows it (start Python with
//! `WEDNESDAY_FOLLOW=http://127.0.0.1:<port>`). Without `--backend` the engine always runs and
//! `--engine` changes nothing.

use std::path::PathBuf;

fn arg(args: &[String], name: &str) -> Option<String> {
    let at = args.iter().position(|a| a == name)?;
    args.get(at + 1).cloned()
}

const USAGE: &str = "usage: wednesday-rs [--host 127.0.0.1] [--port 8000] [--insecure] [--home DIR] [--db URL] \
                     [--ui-dir DIR] [--source tradingview|synthetic] [--backend <port of a Python server> [--engine]]";

fn is_loopback(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host.trim_matches(['[', ']']).parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "-h" || a == "--help") {
        println!("{USAGE}");
        return;
    }
    let home = arg(&args, "--home").map(PathBuf::from).unwrap_or_else(wednesday_rs::config::home_dir);
    wednesday_rs::logfile::init(None);
    let env_file = home.join(".env");
    let setting = |flag: &str, var: &str| arg(&args, flag).or_else(|| wednesday_rs::config::env_setting(var, &env_file));
    let host = setting("--host", "XAU_HOST").unwrap_or_else(|| "127.0.0.1".into());
    let port = setting("--port", "XAU_PORT").map_or(Some(8000), |p| p.parse::<u16>().ok());
    let backend = match arg(&args, "--backend").map(|p| p.parse::<u16>()) {
        None => None,
        Some(Ok(p)) => Some(p),
        Some(Err(_)) => {
            eprintln!("{USAGE}");
            std::process::exit(2);
        }
    };
    let Some(port) = port else {
        eprintln!("{USAGE}");
        std::process::exit(2);
    };
    // No login: refuse a network-reachable dashboard unless asked (cli.check_host).
    if !is_loopback(&host) {
        if !args.iter().any(|a| a == "--insecure") {
            eprintln!(
                "Refusing to serve the dashboard on {host}: it has no login, so anyone who can reach the port could \
                 change settings and delete data. Bind to 127.0.0.1, reach it through an SSH tunnel or a VPN (see \
                 docs/deploy-windows.md), or pass --insecure."
            );
            std::process::exit(2);
        }
        eprintln!("dashboard on {host}, reachable by anyone who can reach the port (--insecure)");
    }
    let store = match arg(&args, "--db") {
        Some(url) if url.eq_ignore_ascii_case("none") => None,
        Some(url) => match wednesday_rs::store::Store::open(&url, &home).await {
            Ok(s) => Some(s),
            Err(e) => {
                eprintln!("{e}; running without a database");
                None
            }
        },
        None => wednesday_rs::open_store(&home).await,
    };
    let bind_host = if host.eq_ignore_ascii_case("localhost") { "127.0.0.1" } else { host.trim_matches(['[', ']']) };
    let listener = match tokio::net::TcpListener::bind((bind_host, port)).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("can't listen on {host}:{port}: {e}");
            std::process::exit(1);
        }
    };
    let database = store.as_ref().map_or("none", |s| s.backend()).to_string();
    let served = match backend {
        None => {
            let ui = arg(&args, "--ui-dir").map(PathBuf::from).unwrap_or_else(|| wednesday_rs::ui::ui_dir(&home));
            eprintln!(
                "wednesday-rs {} on http://{host}:{port}, dashboard from {}, database: {database}",
                wednesday_rs::VERSION,
                ui.display()
            );
            match wednesday_rs::start_engine_on(&home, store.clone(), arg(&args, "--source").as_deref()).await {
                Ok(rt) => wednesday_rs::serve_standalone(listener, ui, store, rt).await,
                Err(e) => {
                    eprintln!("the scan engine didn't start: {e}");
                    std::process::exit(1);
                }
            }
        }
        Some(backend) => {
            eprintln!(
                "wednesday-rs {} on http://{host}:{port}, the rest from 127.0.0.1:{backend}, database: {database}",
                wednesday_rs::VERSION
            );
            let backend = std::net::SocketAddr::from(([127, 0, 0, 1], backend));
            if args.iter().any(|a| a == "--engine") {
                match wednesday_rs::start_engine_on(&home, store.clone(), arg(&args, "--source").as_deref()).await {
                    Ok(rt) => wednesday_rs::serve_with_engine(listener, backend, store, rt).await,
                    Err(e) => {
                        eprintln!("the scan engine didn't start: {e}");
                        std::process::exit(1);
                    }
                }
            } else {
                wednesday_rs::serve(listener, backend, store).await
            }
        }
    };
    if let Err(e) = served {
        eprintln!("{e}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn loopback_hosts() {
        for ok in ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"] {
            assert!(super::is_loopback(ok), "{ok}");
        }
        for bad in ["0.0.0.0", "192.168.1.5", "example.com", "::"] {
            assert!(!super::is_loopback(bad), "{bad}");
        }
    }
}
