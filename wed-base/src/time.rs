//! Times written the way Python writes them.

use chrono::{DateTime, NaiveDateTime, Timelike, Utc};

/// `datetime.isoformat()` of an aware UTC time (microseconds only when there are some).
pub fn py_iso_utc(t: DateTime<Utc>) -> String {
    if t.nanosecond() / 1000 == 0 {
        t.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        t.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
    }
}

/// `pd.Timestamp.isoformat()` of a naive bar time.
pub fn py_iso_naive(t: NaiveDateTime) -> String {
    t.format("%Y-%m-%dT%H:%M:%S").to_string()
}

/// `datetime.now(timezone.utc).isoformat()`, always with microseconds.
pub fn now_iso() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
}
