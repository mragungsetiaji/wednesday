//! What every Wednesday crate shares: Python-identical JSON and text (`pyjson`, `py`), the
//! settings read from the environment and `.env` (`config`), how an alert is sent (`notify`), and
//! a route's answer or refusal ([`Answer`], [`Refusal`]).

pub mod config;
pub mod notify;
pub mod py;
pub mod pyjson;
pub mod time;

use serde_json::Value;

/// A route's refusal: the HTTP status and FastAPI's `detail` (a message, or its validation list).
#[derive(Debug)]
pub struct Refusal {
    pub status: u16,
    pub detail: Value,
}

impl Refusal {
    pub fn new(status: u16, detail: impl Into<Value>) -> Refusal {
        Refusal { status, detail: detail.into() }
    }
}

/// What a ported route gives: its JSON body, or a refusal.
pub type Answer = Result<Value, Refusal>;
