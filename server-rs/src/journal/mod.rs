//! The journal (`src/wednesday/journal/`): trading accounts filled from uploaded MT5 history
//! reports, with stats rebuilt from M1 prices (#96, step 9c).

pub mod market;
pub mod report;
pub mod service;
pub mod stats;
