//! `wednesday-rs --port 8000 --backend 8001 [--home DIR] [--db URL] [--engine]`: the Rust server on
//! 127.0.0.1:<port>, in front of a Python server (`wednesday --serve --port 8001`) that answers
//! what isn't ported yet. `--home` is the folder the Python server runs in (its `.env` and
//! relative database path); `--db` overrides the database URL (`none`: no database here).
//! `--engine` runs the scan engine here; start the Python server with
//! `WEDNESDAY_FOLLOW=http://127.0.0.1:<port>` so it follows it, as the desktop app does.

use std::net::SocketAddr;
use std::path::PathBuf;

fn arg(args: &[String], name: &str) -> Option<String> {
    let at = args.iter().position(|a| a == name)?;
    args.get(at + 1).cloned()
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let port = arg(&args, "--port").map_or(Some(8000), |p| p.parse::<u16>().ok());
    let backend = arg(&args, "--backend").and_then(|p| p.parse::<u16>().ok());
    let (Some(port), Some(backend)) = (port, backend) else {
        eprintln!("usage: wednesday-rs [--port 8000] --backend <port of the Python server> [--home DIR] [--db URL]");
        std::process::exit(2);
    };
    let home = arg(&args, "--home").map(PathBuf::from).unwrap_or_else(wednesday_rs::config::home_dir);
    let store = match arg(&args, "--db") {
        Some(url) if url.eq_ignore_ascii_case("none") => None,
        Some(url) => match wednesday_rs::store::Store::open(&url, &home).await {
            Ok(s) => Some(s),
            Err(e) => {
                eprintln!("{e}; the database routes go to Python");
                None
            }
        },
        None => wednesday_rs::open_store(&home).await,
    };
    // Loopback only: the dashboard has no login.
    let listener = match tokio::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], port))).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("can't listen on 127.0.0.1:{port}: {e}");
            std::process::exit(1);
        }
    };
    eprintln!(
        "wednesday-rs {} on http://127.0.0.1:{port}, the rest from 127.0.0.1:{backend}, database: {}",
        wednesday_rs::VERSION,
        store.as_ref().map_or("none (Python serves it)", |s| s.backend())
    );
    let backend = SocketAddr::from(([127, 0, 0, 1], backend));
    let served = if args.iter().any(|a| a == "--engine") {
        match wednesday_rs::start_engine(&home, store.clone()).await {
            Ok(rt) => wednesday_rs::serve_with_engine(listener, backend, store, rt).await,
            Err(e) => {
                eprintln!("the scan engine didn't start: {e}");
                std::process::exit(1);
            }
        }
    } else {
        wednesday_rs::serve(listener, backend, store).await
    };
    if let Err(e) = served {
        eprintln!("{e}");
        std::process::exit(1);
    }
}
