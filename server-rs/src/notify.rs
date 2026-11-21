//! How an alert reaches the trader: a function that sends one Telegram message (HTML text).
//! The order block alerts (`alerts/`) make it from the bot token and chat id; the drawing alerts
//! (`drawings/`) take it, and without one (alerts off or not set up) only log their firings.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

/// Sends one message; `Err` carries the message for the trader (`last_error`).
pub type Sender = Arc<dyn Fn(String) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send>> + Send + Sync>;
