//! The feed's clock (`quarters.py`): `UTC`, `NY+7` (New York time plus 7 hours, many MT5
//! brokers), `UTC+3` style fixed offsets, or an IANA zone such as `Europe/London`.

use chrono::{DateTime, Duration, LocalResult, NaiveDateTime, TimeZone, Utc};
use chrono_tz::Tz;

/// A naive time on the feed's clock (or New York wall time, where said).
pub type Time = NaiveDateTime;

pub const NEW_YORK: Tz = chrono_tz::America::New_York;

/// Seconds since 1970 with a naive time read as UTC, as the charts show it.
pub fn unix(t: Time) -> i64 {
    t.and_utc().timestamp()
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Clock {
    /// UTC (`ny: false`) or New York wall time (`ny: true`) plus a fixed offset.
    Offset { ny: bool, minutes: i64 },
    Zone(Tz),
}

impl Clock {
    pub fn parse(clock: &str) -> Result<Clock, String> {
        let bad = || format!("Unknown clock {clock:?}: use UTC, NY+7, UTC+3 or a time zone like Europe/London");
        for (prefix, ny) in [("UTC", false), ("NY", true)] {
            let Some(rest) = clock.strip_prefix(prefix) else { continue };
            if rest.is_empty() {
                return Ok(Clock::Offset { ny, minutes: 0 });
            }
            let (sign, num) = match rest.as_bytes()[0] {
                b'+' => (1, &rest[1..]),
                b'-' => (-1, &rest[1..]),
                _ => break,
            };
            let (hours, half) = match num.strip_suffix(".5") {
                Some(h) => (h, 30),
                None => (num, 0),
            };
            if hours.is_empty() || hours.len() > 2 || !hours.bytes().all(|b| b.is_ascii_digit()) {
                return Err(bad());
            }
            let minutes = hours.parse::<i64>().unwrap() * 60 + half;
            return Ok(Clock::Offset { ny, minutes: sign * minutes });
        }
        clock.parse::<Tz>().map(Clock::Zone).map_err(|_| bad())
    }

    /// A feed time as New York wall time; None for a time the clock's zone repeats (DST fall-back).
    pub fn to_new_york(&self, t: Time) -> Option<Time> {
        match *self {
            Clock::Offset { ny: true, minutes } => Some(t - Duration::minutes(minutes)),
            Clock::Offset { ny: false, minutes } => {
                Some((t - Duration::minutes(minutes)).and_utc().with_timezone(&NEW_YORK).naive_local())
            }
            Clock::Zone(tz) => localize(tz, t, false).map(|u| u.with_timezone(&NEW_YORK).naive_local()),
        }
    }

    /// An instant as the feed's naive time (what the chart shows).
    pub fn utc_to_feed(&self, utc: DateTime<Utc>) -> Time {
        match *self {
            Clock::Offset { ny, minutes } => {
                let wall = if ny { utc.with_timezone(&NEW_YORK).naive_local() } else { utc.naive_utc() };
                wall + Duration::minutes(minutes)
            }
            Clock::Zone(tz) => utc.with_timezone(&tz).naive_local(),
        }
    }

    /// A New York wall time as the feed's unix seconds: a repeated time takes its first
    /// occurrence, a skipped one moves on to the change (02:30 on the spring change is 03:00).
    pub fn ny_wall_to_feed_unix(&self, wall: Time) -> i64 {
        unix(self.utc_to_feed(localize(NEW_YORK, wall, true).expect("first occurrence")))
    }
}

/// pandas' `tz_localize(nonexistent="shift_forward")`, with `ambiguous` either the first
/// occurrence (`true`) or none (`"NaT"`).
pub fn localize(tz: Tz, wall: Time, first_if_repeated: bool) -> Option<DateTime<Utc>> {
    match tz.from_local_datetime(&wall) {
        LocalResult::Single(d) => Some(d.with_timezone(&Utc)),
        LocalResult::Ambiguous(a, b) => first_if_repeated.then(|| a.min(b).with_timezone(&Utc)),
        LocalResult::None => {
            // In a gap: the first wall minute after it, which is the instant of the change.
            let mut w = wall;
            for _ in 0..24 * 60 {
                w += Duration::minutes(1);
                if let LocalResult::Single(d) = tz.from_local_datetime(&w) {
                    return Some(d.with_timezone(&Utc));
                }
            }
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    fn at(y: i32, m: u32, d: u32, h: u32, min: u32) -> Time {
        NaiveDate::from_ymd_opt(y, m, d).unwrap().and_hms_opt(h, min, 0).unwrap()
    }

    #[test]
    fn clocks_parse_like_python() {
        assert_eq!(Clock::parse("UTC"), Ok(Clock::Offset { ny: false, minutes: 0 }));
        assert_eq!(Clock::parse("NY+7"), Ok(Clock::Offset { ny: true, minutes: 420 }));
        assert_eq!(Clock::parse("UTC-3.5"), Ok(Clock::Offset { ny: false, minutes: -210 }));
        assert!(matches!(Clock::parse("Europe/London"), Ok(Clock::Zone(_))));
        for bad in ["NY+123", "UTC+", "UTC7", "Mars/Base"] {
            assert!(Clock::parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn new_york_wall_times_on_the_dst_changes() {
        let utc = Clock::parse("UTC").unwrap();
        // Spring 2026-03-08: 02:30 doesn't exist and moves to 03:00 EDT (07:00 UTC).
        assert_eq!(utc.ny_wall_to_feed_unix(at(2026, 3, 8, 2, 30)), unix(at(2026, 3, 8, 7, 0)));
        // Autumn 2026-11-01: 01:30 happens twice; the first is EDT (05:30 UTC).
        assert_eq!(utc.ny_wall_to_feed_unix(at(2026, 11, 1, 1, 30)), unix(at(2026, 11, 1, 5, 30)));
        let ny7 = Clock::parse("NY+7").unwrap();
        assert_eq!(ny7.to_new_york(at(2026, 3, 4, 0, 0)), Some(at(2026, 3, 3, 17, 0)));
    }
}
