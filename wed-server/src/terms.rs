//! The disclaimer and risk agreement the trader accepts before using the dashboard (`terms.py`),
//! #96 step 9f: `GET /api/terms` and `POST /api/terms/accept`.
//!
//! The text is the repository's `DISCLAIMER.md`, built into the binary: the desktop bundle ships
//! the same file to Python from the same checkout, so both servers show one text. Acceptance is
//! stored (setting `terms`) with the version of the terms, so a change to them asks again.

use chrono::Utc;
use serde_json::{json, Value};

use crate::engine::{py_iso_utc, Answer, Refusal};
use crate::store::Store;

pub const TERMS_KEY: &str = "terms";
/// `terms.TERMS_VERSION`. Bump both when DISCLAIMER.md changes in substance; a test checks that
/// this one equals Python's.
pub const TERMS_VERSION: &str = "2026-09-26";
/// `DISCLAIMER.md`, as Python's `disclaimer_text()` reads it.
pub const DISCLAIMER: &str = include_str!("../../DISCLAIMER.md");

fn store_failed(e: String) -> Refusal {
    log::error!("database: {e}");
    Refusal::new(500, "The database couldn't be read or written. Try again, and restart Wednesday if this stays.")
}

/// The saved acceptance, if it is of these terms (`Terms.accepted`).
async fn accepted(store: &Store) -> Result<Option<Value>, String> {
    Ok(store
        .get_setting(TERMS_KEY)
        .await?
        .filter(|s| s.as_object().is_some_and(|o| !o.is_empty()) && s.get("version").and_then(Value::as_str) == Some(TERMS_VERSION)))
}

/// `GET /api/terms`: the disclaimer, its version, and whether (and when) it was accepted.
pub async fn status(store: &Store) -> Answer {
    let accepted = accepted(store).await.map_err(store_failed)?;
    Ok(json!({
        "version": TERMS_VERSION,
        "accepted": accepted.is_some(),
        "accepted_at": accepted.as_ref().and_then(|a| a.get("accepted_at")).cloned().unwrap_or(Value::Null),
        "text": DISCLAIMER,
    }))
}

/// `POST /api/terms/accept`: store the acceptance of these terms, now; answers as [`status`].
pub async fn accept(store: &Store) -> Answer {
    let record = json!({"version": TERMS_VERSION, "accepted_at": py_iso_utc(Utc::now())});
    store.set_setting(TERMS_KEY, &record).await.map_err(store_failed)?;
    status(store).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_version_is_pythons() {
        let py = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../src/wednesday/terms.py")).unwrap();
        let line = py.lines().find(|l| l.starts_with("TERMS_VERSION = ")).expect("TERMS_VERSION in terms.py");
        assert_eq!(line, format!("TERMS_VERSION = \"{TERMS_VERSION}\""),
                   "bump TERMS_VERSION in terms.rs and terms.py together");
    }

    #[test]
    fn the_text_is_the_disclaimer() {
        assert!(DISCLAIMER.contains("not financial advice") || DISCLAIMER.to_lowercase().contains("financial advice"));
    }
}
