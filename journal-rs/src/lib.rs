//! The journal (`src/wednesday/journal/`): trading accounts filled from uploaded MT5 history
//! reports, with stats rebuilt from M1 prices (#96, step 9c), and the chart snapshots its trades
//! keep. Freezing the market into an entry needs the engine, so that part stays in server-rs.

pub mod report;
pub mod service;
pub mod snapshots;
pub mod stats;
