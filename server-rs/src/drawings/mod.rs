//! The trader's drawings and their price alerts (step 9e of #96): `drawings.py`,
//! `drawing_alerts.py` and the `/api/drawings` routes of `server.py`.
//!
//! * `clean`: checking what the dashboard sends (`clean_drawing`, `clean_alert`).
//! * `events`: when an alert fires (`line_price`, `bar_events`, `tick_event`) and its message.
//! * `checker`: `DrawingAlerts`, run after each scan and on each tick.
//! * `routes`: the route logic, as functions giving the answer or a refusal.
//!
//! `tests/golden/drawings/` holds Python's output for all of these (`tests/test_drawings_golden.py`),
//! which `tests/drawings_golden.rs` replays.

pub mod checker;
pub mod clean;
pub mod events;
pub mod py;
pub mod routes;

pub use checker::DrawingAlerts;
pub use clean::{clean_alert, clean_alert_at, clean_drawing, clean_drawing_at, drawing_payload};
pub use events::{bar_events, describe, format_message, line_price, tick_event, Drawing};
