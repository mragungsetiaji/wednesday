//! Features a plugin can provide, so the dashboard can show what each one does even when it
//! isn't installed or licensed (`features.py`).
//!
//! Everything else in Wednesday is free and needs no licence: the screener, alerts, the bias, the
//! news card and one trading journal.

use serde_json::{json, Value};

/// id, title, what it does, the issue that tracks it.
pub const CATALOG: &[(&str, &str, &str, u32)] = &[
    ("journal.multi", "Multiple journals",
     "More than one trading journal, e.g. one per MT5 account. One journal is free.", 14),
    ("llm.second_brain", "Second brain",
     "Ask questions of your playbook, journal and briefs, answered with sources.", 15),
    ("llm.recap", "Session recap",
     "A recap after each session and a weekly review: the bias against what happened, rule breaks, patterns.", 16),
    ("llm.scheduled_briefs", "Scheduled briefs",
     "Briefs written before London, before New York and before high-impact news, sent to Telegram.", 17),
];

/// Known features with whether a loaded plugin provides them; unknown ids a plugin provides come
/// last, sorted (`features.catalog`).
pub fn catalog(enabled: &[String]) -> Vec<Value> {
    let on = |id: &str| enabled.iter().any(|f| f == id);
    let mut out: Vec<Value> = CATALOG
        .iter()
        .map(|(id, title, description, issue)| {
            json!({"id": id, "title": title, "description": description, "issue": issue, "enabled": on(id)})
        })
        .collect();
    let mut extra: Vec<&String> = enabled.iter().filter(|f| !CATALOG.iter().any(|(id, ..)| id == f)).collect();
    extra.sort();
    extra.dedup();
    out.extend(extra.into_iter().map(|f| json!({"id": f, "title": f, "description": "", "issue": null, "enabled": true})));
    out
}
