//! Every integration test of the Rust server in one test binary: each file used to be its own
//! binary, linked against the whole crate, so `cargo test` linked it twenty-odd times.
//!
//! The modules share one process now. A test that changes the process environment
//! (`std::env::set_var`) holds [`ENV`] for as long as it relies on it, so two such tests never
//! run at the same time.

/// Held by the tests that set environment variables.
pub static ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// Take [`ENV`], even after another test panicked while holding it.
pub fn env_lock() -> std::sync::MutexGuard<'static, ()> {
    ENV.lock().unwrap_or_else(|e| e.into_inner())
}

mod fake_telegram;
mod plugin_kit;

mod alerts;
mod alerts_golden;
mod calendar_fetch;
mod calendar_golden;
mod conformance;
mod distribution_golden;
mod drawings_golden;
mod drawings_routes;
mod engine_analysis;
mod engine_charts;
mod engine_markets;
mod feed;
mod gateway;
mod golden;
mod journal_golden;
mod market_golden;
mod plugins_host;
mod plugins_install;
mod report_golden;
mod storage;
mod terms_bias_snapshots;
