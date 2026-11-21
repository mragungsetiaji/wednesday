//! The journal: the crate `wednesday-journal` (wed-journal), plus freezing the market into an
//! entry, which needs the engine (`market`).

pub mod market;
pub use wednesday_journal::{report, service, stats};
