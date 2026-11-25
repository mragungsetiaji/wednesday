//! `wednesday-rs [--host 127.0.0.1] [--port 8000] [--home DIR] [--db URL] [--ui-dir DIR] [--source ID]`: Wednesday's
//! server. It runs the scan engine and serves the API and the built dashboard on
//! http://<host>:<port>. `--home` is the data folder (its `.env` and relative database path;
//! default `WEDNESDAY_HOME`, `%LOCALAPPDATA%\Wednesday` or `~/.wednesday`); `--db` overrides the
//! database URL (`none`: no database); `--ui-dir` the dashboard build (default `XAU_UI_DIR`, else
//! `web/dist`); `--source` the data source over the saved one (`synthetic` for development).
//! `--host` and `--port` default to `XAU_HOST` and `XAU_PORT`. On anything but a loopback host
//! the dashboard asks for a login (`auth.rs`); `--insecure` turns it off (development only),
//! `--login` (`XAU_LOGIN=1`) turns it on for 127.0.0.1 behind a reverse proxy. `--create-user
//! NAME` adds an account, or sets a new password on it, and exits.
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

const USAGE: &str = "usage: wednesday-rs [--host 127.0.0.1] [--port 8000] [--insecure | --login] [--home DIR] [--db URL] \
                     [--ui-dir DIR] [--source tradingview|synthetic] [--backend <port of a Python server> [--engine]]\n       \
                     wednesday-rs [--home DIR] [--db URL] --create-user NAME";

fn is_loopback(host: &str) -> bool {
    host.eq_ignore_ascii_case("localhost")
        || host.trim_matches(['[', ']']).parse::<std::net::IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

/// The login is on for a network host (unless `--insecure`) and with `--login`; off on 127.0.0.1.
fn login_on(host: &str, insecure: bool, login: bool) -> bool {
    if is_loopback(host) { login } else { !insecure }
}

/// `--create-user NAME`: read the password twice from the terminal, then add or reset the account.
async fn create_user(store: Option<wednesday_rs::store::Store>, name: &str) -> i32 {
    let auth = match wednesday_rs::auth::Auth::new(store) {
        Ok(a) => a,
        Err(_) => {
            eprintln!("Accounts live in the database: remove XAU_DB_URL=none from .env");
            return 2;
        }
    };
    let ask = |prompt: &str| rpassword::prompt_password(prompt).unwrap_or_default();
    let password = ask(&format!("Password for {name}: "));
    if ask("Again: ") != password {
        eprintln!("The passwords differ; nothing changed");
        return 2;
    }
    let shown = name.trim().to_lowercase();
    match auth.create_user(name, &password).await {
        Ok(true) => println!("Created the account {shown}."),
        Ok(false) => println!("Set a new password for {shown}; its devices are logged out."),
        Err(e) => {
            eprintln!("{}", e.message);
            return 2;
        }
    }
    0
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
    let flag = |name: &str| args.iter().any(|a| a == name);
    let login = login_on(&host, flag("--insecure"),
                         flag("--login") || wednesday_rs::config::env_setting("XAU_LOGIN", &env_file).as_deref() == Some("1"));
    if !is_loopback(&host) && !login {
        eprintln!("dashboard on {host} with no login, reachable by anyone who can reach the port (--insecure)");
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
    if let Some(name) = arg(&args, "--create-user") {
        std::process::exit(create_user(store, &name).await);
    }
    let auth = if login {
        match wednesday_rs::auth::Auth::new(store.clone()) {
            Ok(a) => a,
            Err(e) => {
                eprintln!("{e}");
                std::process::exit(2);
            }
        }
    } else {
        wednesday_rs::auth::Auth::off()
    };
    if let Err(e) = auth.start(&|k: &str| wednesday_rs::config::env_setting(k, &env_file)).await {
        eprintln!("the login can't start: {e}");
        std::process::exit(1);
    }
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
                Ok(rt) => wednesday_rs::serve_standalone_with(listener, ui, store, rt, std::sync::Arc::new(auth)).await,
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

    #[test]
    fn login_on_a_network_host() {
        assert!(!super::login_on("127.0.0.1", false, false));
        assert!(super::login_on("127.0.0.1", false, true));
        assert!(super::login_on("0.0.0.0", false, false));
        assert!(!super::login_on("0.0.0.0", true, false));
    }
}
