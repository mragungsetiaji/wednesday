//! `wednesday-rs --port 8000 --backend 8001`: the Rust server on 127.0.0.1:<port>, in front of a
//! Python server (`wednesday --serve --port 8001`) that answers what isn't ported yet.

use std::net::SocketAddr;

fn arg(args: &[String], name: &str) -> Option<u16> {
    let at = args.iter().position(|a| a == name)?;
    args.get(at + 1)?.parse().ok()
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    let (Some(port), Some(backend)) = (arg(&args, "--port").or(Some(8000)), arg(&args, "--backend")) else {
        eprintln!("usage: wednesday-rs [--port 8000] --backend <port of the Python server>");
        std::process::exit(2);
    };
    // Loopback only: the login, and the client address it counts failed logins by, are still Python's.
    let listener = match tokio::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], port))).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("can't listen on 127.0.0.1:{port}: {e}");
            std::process::exit(1);
        }
    };
    eprintln!("wednesday-rs {} on http://127.0.0.1:{port}, the rest from 127.0.0.1:{backend}", wednesday_rs::VERSION);
    if let Err(e) = wednesday_rs::serve(listener, SocketAddr::from(([127, 0, 0, 1], backend))).await {
        eprintln!("{e}");
        std::process::exit(1);
    }
}
