//! Timeframes and M1 to higher-timeframe resampling (`timeframes.py`).

use chrono::Duration;

use super::clock::{unix, Time};

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Bar {
    pub time: Time, // open time, feed clock
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Timeframe {
    pub name: &'static str,
    pub minutes: i64,
}

impl Timeframe {
    pub fn delta(&self) -> Duration {
        Duration::minutes(self.minutes)
    }
}

/// From the highest timeframe to the lowest: the scan order.
pub const TIMEFRAMES: [Timeframe; 5] = [
    Timeframe { name: "4H", minutes: 240 },
    Timeframe { name: "1H", minutes: 60 },
    Timeframe { name: "30M", minutes: 30 },
    Timeframe { name: "15M", minutes: 15 },
    Timeframe { name: "5M", minutes: 5 },
];

/// Closed M1 bars (sorted, unique) into `tf` candles labelled by their open time and aligned to
/// midnight of the feed's clock. The still-forming last candle is dropped: the feed only holds
/// closed M1 bars, so the data runs to the last bar's open plus a minute.
pub fn resample(m1: &[Bar], tf: Timeframe) -> Vec<Bar> {
    let Some(last) = m1.last() else { return Vec::new() };
    let data_end = last.time + Duration::minutes(1);
    let step = tf.minutes * 60;
    let mut out: Vec<Bar> = Vec::new();
    let mut bin = i64::MIN;
    for b in m1 {
        let start = unix(b.time).div_euclid(step) * step;
        if start != bin {
            bin = start;
            let time = chrono::DateTime::from_timestamp(start, 0).expect("in range").naive_utc();
            out.push(Bar { time, ..*b });
        } else {
            let c = out.last_mut().unwrap();
            c.high = c.high.max(b.high);
            c.low = c.low.min(b.low);
            c.close = b.close;
            c.volume += b.volume;
        }
    }
    out.retain(|c| c.time + tf.delta() <= data_end);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    fn bar(h: u32, m: u32, v: f64) -> Bar {
        let time = NaiveDate::from_ymd_opt(2026, 3, 4).unwrap().and_hms_opt(h, m, 0).unwrap();
        Bar { time, open: v, high: v + 1.0, low: v - 1.0, close: v + 0.5, volume: 1.0 }
    }

    #[test]
    fn candles_are_closed_and_aligned_to_midnight() {
        let m1 = vec![bar(9, 58, 10.0), bar(9, 59, 11.0), bar(10, 0, 12.0), bar(10, 4, 13.0), bar(10, 5, 14.0)];
        let five = resample(&m1, TIMEFRAMES[4]);
        // 09:55 (two bars), 10:00 (two bars); 10:05 is still forming.
        assert_eq!(five.len(), 2);
        assert_eq!((five[0].open, five[0].high, five[0].close, five[0].volume), (10.0, 12.0, 11.5, 2.0));
        assert_eq!((five[1].open, five[1].low, five[1].close), (12.0, 11.0, 13.5));
    }
}
