//! The scan in Rust, step 7 of the move off Python (#96): M1 bars to 4H-5M candles (closed
//! candles only), swings and breaks of structure, the detectors (order blocks, liquidity,
//! inducement), the ranked setups, the session reference levels and the quarterly blocks.
//!
//! A line-by-line port of `timeframes.py`, `structure.py`, `levels.py`, `detectors/`,
//! `scanner.py`, `sessions.py` and `quarters.py`. Its output is checked against the same
//! reference files as the Python scan (`tests/golden/`, see `tests/it/golden.rs`): change one and
//! the other must follow.
//!
//! Times are naive feed-clock times ([`Time`]); a unix second reads one as UTC, as the chart does.

pub mod candles;
pub mod clock;
pub mod detectors;
pub mod levels;
pub mod quarters;
pub mod scanner;
pub mod sessions;
pub mod structure;

pub use candles::{resample, Bar, Timeframe, TIMEFRAMES};
pub use clock::{unix, Clock, Time};
pub use detectors::{DetectorParams, Detector, DETECTORS};
pub use levels::Level;
pub use scanner::{scan, ScanConfig, ScanResult};

/// Python's `round(x, digits)`: the nearest decimal, read from the float's exact value.
pub(crate) fn round_to(x: f64, digits: usize) -> f64 {
    format!("{x:.digits$}").parse().unwrap_or(x)
}
