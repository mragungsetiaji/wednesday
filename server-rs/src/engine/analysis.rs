//! The analysis routes on the engine's bars (#96, step 9d part 2), ported from `server.py`:
//! `GET /api/quarters` (the quarterly blocks), `GET /api/sessions` (killzones and reference
//! levels) and `GET /api/distribution` (`distribution.py`: how unusual the current move is).
//!
//! The handlers take the query as a map and answer as FastAPI does: the same bodies, statuses and
//! messages, and FastAPI's validation list (status 422) for a query it would refuse. The
//! distribution's work ([`tables`], [`values`], [`move_distribution`]) is public so
//! `tests/it/distribution_golden.rs` can check it against `tests/golden/distribution/`.
//!
//! The distribution copies numpy and pandas to the last bit: numpy's pairwise sum for the mean and
//! the variance, pandas' Kahan-compensated rolling mean for the ATR, `np.linspace` and
//! `np.histogram` for the bins, and numpy's (unstable) introsort for the "like this" order, which
//! decides the order of periods on the same day.
//!
//! This is context for the trader, not a signal: nothing here sets the bias or trades.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::time::{Duration as StdDuration, Instant};

use chrono::{Datelike, Duration, NaiveDate};
use serde_json::{json, Value};

use super::{load_bars, Answer, Command, Engine, Refusal, Runtime};
use crate::scan::quarters::{quarter_blocks, quarters_payload, Block, SESSIONS, WEEKDAYS};
use crate::scan::sessions::{reference_levels, MAX_DAYS};
use crate::scan::{unix, Bar, Clock, Time};
use crate::store::Store;

/// Seconds the stored history's periods are kept; the live buffer's are fresh.
pub const DIST_TTL: StdDuration = StdDuration::from_secs(6 * 3600);
/// Stored M1 bars read for the distribution: about five years.
pub const DIST_BARS: usize = 2_000_000;
/// Daily bars asked of the feed, for lookbacks past the M1 history.
pub const DIST_DAYS: usize = 3000;
/// Seconds the stored bars before the live buffer are kept for the reference levels.
pub const LEVELS_TTL: StdDuration = StdDuration::from_secs(6 * 3600);
/// Calendar days of M1 the reference levels need: the whole previous month.
pub const LEVELS_DAYS: i64 = 70;
/// How long the feed gets to answer for the daily bars (`engine.call(..., timeout=60)`).
const DAILY_TIMEOUT: StdDuration = StdDuration::from_secs(60);
/// Stored bars read per query, so five years never sit in memory as JSON rows at once.
const CHUNK: usize = 50_000;

pub const PERIODS: [&str; 4] = ["day", "week", "session", "q90"];
pub const MEASURES: [&str; 3] = ["change", "range_pct", "range_atr"];
pub const LOOKBACKS: [(&str, Option<i64>); 3] = [("1y", Some(365)), ("5y", Some(1826)), ("all", None)];
pub const MIN_SAMPLES: usize = 30;
pub const ATR_DAYS: usize = 14;
/// Past periods listed as "like this".
pub const LIKE_THIS: usize = 20;

// ---- the period tables (distribution.tables) ----

/// One period: a row of a `distribution.py` table.
#[derive(Clone, Debug, PartialEq)]
pub struct Period {
    pub day: NaiveDate, // trading day
    pub label: String,
    pub start: Time, // first M1 bar (feed clock), or the daily bar's time
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub live: bool,
}

impl From<&Block> for Period {
    fn from(b: &Block) -> Period {
        Period { day: b.day, label: b.label.to_string(), start: b.start, open: b.open, high: b.high, low: b.low,
                 close: b.close, live: b.live }
    }
}

/// Quarter blocks per row (`week`: one per day, `session`, `q90`), oldest first.
#[derive(Clone, Debug, Default)]
pub struct BlockRows {
    pub week: Vec<Period>,
    pub session: Vec<Period>,
    pub q90: Vec<Period>,
}

impl BlockRows {
    pub fn of(m1: &[Bar], clock: &Clock) -> BlockRows {
        let mut out = BlockRows::default();
        for (row, blocks) in quarter_blocks(m1, clock) {
            let periods: Vec<Period> = blocks.iter().map(Period::from).collect();
            match row {
                "week" => out.week = periods,
                "session" => out.session = periods,
                _ => out.q90 = periods,
            }
        }
        out
    }

    fn rows_mut(&mut self) -> [&mut Vec<Period>; 3] {
        [&mut self.week, &mut self.session, &mut self.q90]
    }
}

/// One table per period, oldest first.
#[derive(Clone, Debug, Default)]
pub struct Tables {
    pub day: Vec<Period>,
    pub week: Vec<Period>,
    pub session: Vec<Period>,
    pub q90: Vec<Period>,
}

impl Tables {
    pub fn get(&self, period: &str) -> &[Period] {
        match period {
            "day" => &self.day,
            "week" => &self.week,
            "session" => &self.session,
            _ => &self.q90,
        }
    }
}

fn weekday(d: NaiveDate) -> usize {
    d.weekday().num_days_from_monday() as usize
}

/// `daily_from_d1`: daily bars from the feed (indexed by trading day) as a day table, weekdays only.
pub fn daily_from_d1(d1: Option<&[Bar]>) -> Vec<Period> {
    d1.unwrap_or_default()
        .iter()
        .filter(|b| weekday(b.time.date()) < 5)
        .map(|b| Period { day: b.time.date(), label: WEEKDAYS[weekday(b.time.date())].to_string(), start: b.time,
                          open: b.open, high: b.high, low: b.low, close: b.close, live: false })
        .collect()
}

/// `tables`: one table per period. Daily bars from the feed extend the days (and weeks) back
/// before the M1 history.
pub fn tables(blocks: &BlockRows, d1: Option<&[Bar]>) -> Tables {
    let mut day = blocks.week.clone(); // the quarter pane's "week" row is one block per day
    let mut older = daily_from_d1(d1);
    if !older.is_empty() {
        if let Some(first) = day.iter().map(|p| p.day).min() {
            older.retain(|p| p.day < first);
        }
        older.extend(day);
        day = older;
    }

    // Weeks: the days grouped by their Monday (`groupby(monday, sort=True)`).
    let mut groups: BTreeMap<NaiveDate, Vec<usize>> = BTreeMap::new();
    for (i, p) in day.iter().enumerate() {
        groups.entry(p.day - Duration::days(weekday(p.day) as i64)).or_default().push(i);
    }
    let mut week: Vec<Period> = groups
        .values()
        .map(|rows| {
            let (first, last) = (&day[rows[0]], &day[*rows.last().unwrap()]);
            Period {
                day: first.day,
                label: format!("Week of {}", first.day.format("%d %b")),
                start: first.start,
                open: first.open,
                high: rows.iter().map(|&i| day[i].high).fold(f64::NAN, nan_max),
                low: rows.iter().map(|&i| day[i].low).fold(f64::NAN, nan_min),
                close: last.close,
                live: false,
            }
        })
        .collect();
    // The last week still runs until its Friday closes.
    if let (Some(w), Some(rows)) = (week.last_mut(), groups.values().last()) {
        let last = &day[*rows.last().unwrap()];
        w.live = last.live || weekday(last.day) < 4;
    }

    let session = blocks.session.clone();
    let mut q90 = blocks.q90.clone();
    if !q90.is_empty() && !session.is_empty() {
        // Each 90-minute block's session: the session that started last at or before it.
        let mut s: Vec<&Period> = session.iter().collect();
        s.sort_by_key(|p| p.start);
        q90.sort_by_key(|p| p.start);
        for q in &mut q90 {
            let n = s.partition_point(|p| p.start <= q.start);
            let name = if n > 0 { s[n - 1].label.as_str() } else { "" };
            q.label = format!("{name} {}", q.label);
        }
    }
    Tables { day, week, session, q90 }
}

/// pandas' `max` of a group: NaN skipped.
fn nan_max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b > a { b } else { a }
}

fn nan_min(a: f64, b: f64) -> f64 {
    if a.is_nan() || b < a { b } else { a }
}

/// `np.maximum` / `np.minimum`: NaN propagates.
fn np_max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}

fn np_min(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b < a { b } else { a }
}

// ---- numpy and pandas arithmetic ----

/// numpy's pairwise summation (`pairwise_sum` in `loops_utils.h`): 8 accumulators up to 128
/// items, halves (on a multiple of 8) above.
fn pairwise(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for x in a {
            res += x;
        }
        res
    } else if n <= 128 {
        let mut r = [0.0; 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        while i < n - n % 8 {
            for k in 0..8 {
                r[k] += a[i + k];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise(&a[..n2]) + pairwise(&a[n2..])
    }
}

/// `np.add.reduce` of a float array: the identity plus the pairwise sum.
pub fn np_sum(a: &[f64]) -> f64 {
    0.0 + pairwise(a)
}

pub fn np_mean(a: &[f64]) -> f64 {
    np_sum(a) / a.len() as f64
}

/// `a.std(ddof=1)`: the mean, the squared deviations, their sum over n - 1, the root.
pub fn np_std1(a: &[f64], mean: f64) -> f64 {
    let sq: Vec<f64> = a.iter().map(|x| (x - mean) * (x - mean)).collect();
    (np_sum(&sq) / (a.len() - 1) as f64).sqrt()
}

/// `np.median`: the middle value, or the mean of the two middle ones.
pub fn np_median(sorted: &[f64]) -> f64 {
    let n = sorted.len();
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        np_sum(&sorted[n / 2 - 1..n / 2 + 1]) / 2.0
    }
}

/// `pd.Series.rolling(window, min_periods=window).mean()`: pandas' running sum with Kahan
/// compensation (`roll_mean`), the same value repeated read back as itself, and no sign flips.
pub fn rolling_mean(values: &[f64], window: usize) -> Vec<f64> {
    let (mut nobs, mut neg, mut same) = (0usize, 0usize, 0usize);
    let (mut sum, mut comp_add, mut comp_rem) = (0.0f64, 0.0f64, 0.0f64);
    let mut prev = values.first().copied().unwrap_or(0.0);
    let mut out = Vec::with_capacity(values.len());
    for i in 0..values.len() {
        if i >= window {
            let val = values[i - window];
            if !val.is_nan() {
                nobs -= 1;
                let y = -val - comp_rem;
                let t = sum + y;
                comp_rem = t - sum - y;
                sum = t;
                if val.is_sign_negative() {
                    neg -= 1;
                }
            }
        }
        let val = values[i];
        if !val.is_nan() {
            nobs += 1;
            let y = val - comp_add;
            let t = sum + y;
            comp_add = t - sum - y;
            sum = t;
            if val.is_sign_negative() {
                neg += 1;
            }
            if val == prev {
                same += 1;
            } else {
                same = 1;
            }
            prev = val;
        }
        out.push(if nobs >= window.max(1) {
            let mean = sum / nobs as f64;
            if same >= nobs {
                prev
            } else if neg == 0 && mean < 0.0 {
                0.0
            } else if neg == nobs && mean > 0.0 {
                0.0
            } else {
                mean
            }
        } else {
            f64::NAN
        });
    }
    out
}

/// `np.linspace(lo, hi, num)` (endpoint included).
pub fn linspace(lo: f64, hi: f64, num: usize) -> Vec<f64> {
    let div = (num - 1) as f64;
    let delta = hi - lo;
    let step = delta / div;
    let mut y: Vec<f64> = (0..num)
        .map(|i| if step == 0.0 { i as f64 / div * delta } else { i as f64 * step } + lo)
        .collect();
    if num > 1 {
        y[num - 1] = hi;
    }
    y
}

/// `np.histogram(v, bins=edges)`: half-open bins, the last one closed.
pub fn histogram(v: &[f64], edges: &[f64]) -> Vec<usize> {
    let mut sorted = v.to_vec();
    sorted.sort_by(f64::total_cmp);
    let n = edges.len();
    let cum: Vec<usize> = edges
        .iter()
        .enumerate()
        .map(|(i, &e)| if i + 1 < n { sorted.partition_point(|&x| x < e) } else { sorted.partition_point(|&x| x <= e) })
        .collect();
    cum.windows(2).map(|w| w[1] - w[0]).collect()
}

/// numpy's `argsort(kind="quicksort")` for 64-bit times (`aquicksort_` with heapsort past the depth
/// limit, insertion sort for 16 or fewer): not stable, so equal keys come out in its order.
pub fn np_argsort(v: &[i64]) -> Vec<usize> {
    const SMALL: usize = 15;
    let num = v.len();
    let mut t: Vec<usize> = (0..num).collect();
    if num < 2 {
        return t;
    }
    let mut stack: Vec<(usize, usize, i32)> = Vec::new();
    let (mut pl, mut pr) = (0usize, num - 1);
    let mut cdepth = (usize::BITS - 1 - num.leading_zeros()) as i32 * 2;
    loop {
        if cdepth < 0 {
            heapsort(v, &mut t[pl..=pr]);
        } else {
            while pr - pl > SMALL {
                let pm = pl + ((pr - pl) >> 1);
                if v[t[pm]] < v[t[pl]] {
                    t.swap(pm, pl);
                }
                if v[t[pr]] < v[t[pm]] {
                    t.swap(pr, pm);
                }
                if v[t[pm]] < v[t[pl]] {
                    t.swap(pm, pl);
                }
                let vp = v[t[pm]];
                let (mut pi, mut pj) = (pl, pr - 1);
                t.swap(pm, pj);
                loop {
                    pi += 1;
                    while v[t[pi]] < vp {
                        pi += 1;
                    }
                    pj -= 1;
                    while vp < v[t[pj]] {
                        pj -= 1;
                    }
                    if pi >= pj {
                        break;
                    }
                    t.swap(pi, pj);
                }
                t.swap(pi, pr - 1);
                cdepth -= 1;
                // The larger side waits on the stack.
                if pi - pl < pr - pi {
                    stack.push((pi + 1, pr, cdepth));
                    pr = pi - 1;
                } else {
                    stack.push((pl, pi - 1, cdepth));
                    pl = pi + 1;
                }
            }
            for pi in pl + 1..=pr {
                let vi = t[pi];
                let vp = v[vi];
                let mut pj = pi;
                while pj > pl && vp < v[t[pj - 1]] {
                    t[pj] = t[pj - 1];
                    pj -= 1;
                }
                t[pj] = vi;
            }
        }
        let Some((l, r, d)) = stack.pop() else { break };
        (pl, pr, cdepth) = (l, r, d);
    }
    t
}

/// numpy's `aheapsort_` on a slice of indices.
fn heapsort(v: &[i64], t: &mut [usize]) {
    let mut n = t.len();
    // 1-based, as numpy writes it.
    let get = |t: &[usize], k: usize| t[k - 1];
    let mut l = n >> 1;
    while l > 0 {
        let tmp = get(t, l);
        let (mut i, mut j) = (l, l << 1);
        while j <= n {
            if j < n && v[get(t, j)] < v[get(t, j + 1)] {
                j += 1;
            }
            if v[tmp] < v[get(t, j)] {
                t[i - 1] = get(t, j);
                i = j;
                j += j;
            } else {
                break;
            }
        }
        t[i - 1] = tmp;
        l -= 1;
    }
    while n > 1 {
        let tmp = get(t, n);
        t[n - 1] = get(t, 1);
        n -= 1;
        let (mut i, mut j) = (1, 2);
        while j <= n {
            if j < n && v[get(t, j)] < v[get(t, j + 1)] {
                j += 1;
            }
            if v[tmp] < v[get(t, j)] {
                t[i - 1] = get(t, j);
                i = j;
                j += j;
            } else {
                break;
            }
        }
        t[i - 1] = tmp;
    }
}

/// pandas' `sort_values(ascending=False)` order (`nargsort`): reversed, sorted, reversed back.
fn descending(keys: &[i64]) -> Vec<usize> {
    let n = keys.len();
    let rev: Vec<i64> = keys.iter().rev().copied().collect();
    let mut order: Vec<usize> = np_argsort(&rev).into_iter().map(|k| n - 1 - k).collect();
    order.reverse();
    order
}

// ---- the measures and the distribution ----

/// `_daily_atr`: ATR(14) of the days before each day (never the day itself), by trading day.
fn daily_atr(day: &[Period]) -> HashMap<NaiveDate, f64> {
    let tr: Vec<f64> = day
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let prev = if i > 0 { day[i - 1].close } else { f64::NAN };
            let (ph, pl) = if prev.is_nan() { (p.high, p.low) } else { (prev, prev) };
            np_max(p.high, ph) - np_min(p.low, pl)
        })
        .collect();
    let atr = rolling_mean(&tr, ATR_DAYS);
    day.iter().enumerate().map(|(i, p)| (p.day, if i > 0 { atr[i - 1] } else { f64::NAN })).collect()
}

/// `values`: each row's value for the measure (NaN where it can't be had). Err for an unknown
/// measure, which a table with no rows never checks.
pub fn values(t: &Tables, period: &str, measure: &str) -> Result<Vec<f64>, String> {
    let df = t.get(period);
    if df.is_empty() {
        return Ok(Vec::new());
    }
    Ok(match measure {
        "change" => df
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let base = if matches!(period, "day" | "week") {
                    if i > 0 { df[i - 1].close } else { f64::NAN }
                } else {
                    p.open
                };
                (p.close / base - 1.0) * 100.0
            })
            .collect(),
        "range_pct" => df.iter().map(|p| (p.high - p.low) / p.open * 100.0).collect(),
        "range_atr" => {
            let atr = daily_atr(&t.day);
            df.iter().map(|p| (p.high - p.low) / atr.get(&p.day).copied().unwrap_or(f64::NAN)).collect()
        }
        _ => return Err(format!("measure is one of {}", MEASURES.join(", "))),
    })
}

fn session_of(label: &str) -> &str {
    SESSIONS.iter().find(|s| label.starts_with(**s)).copied().unwrap_or(label)
}

fn finite(x: f64) -> Option<f64> {
    x.is_finite().then_some(x)
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// `distribution.distribution`: the current (last) period's move against the past closed ones. Err (the
/// route's 422) for an unknown period, lookback or measure.
pub fn move_distribution(t: &Tables, period: &str, measure: &str, lookback: &str, same_weekday: bool,
                    same_session: bool, bins: i64) -> Result<Value, String> {
    if !PERIODS.contains(&period) {
        return Err(format!("period is one of {}", PERIODS.join(", ")));
    }
    let Some(&(_, days)) = LOOKBACKS.iter().find(|(k, _)| *k == lookback) else {
        return Err(format!("lookback is one of {}", LOOKBACKS.map(|(k, _)| k).join(", ")));
    };
    let value = values(t, period, measure)?;
    let df = t.get(period);
    let unit = if measure == "range_atr" { "×ATR" } else { "%" };
    let mut out = json!({"period": period, "measure": measure, "unit": unit, "lookback": lookback,
                         "min_samples": MIN_SAMPLES, "samples": 0, "current": null, "stats": null,
                         "percentile": null, "z": null, "histogram": {"edges": [], "counts": []}, "like_this": [],
                         "filters": {"weekday": null, "session": null}, "coverage": null});
    if df.is_empty() {
        return Ok(out);
    }
    let last = df.len() - 1;
    let cur = &df[last];
    // The last period is the marker, forming or (market closed) the latest closed one.
    let mut closed: Vec<usize> = (0..last).collect();
    if let Some(days) = days {
        let from = cur.day - Duration::days(days);
        closed.retain(|&i| df[i].day >= from);
    }
    if same_weekday && period != "week" {
        let wd = weekday(cur.day);
        closed.retain(|&i| weekday(df[i].day) == wd);
        out["filters"]["weekday"] = if wd < 5 { json!(WEEKDAYS[wd]) } else { Value::Null };
    }
    if same_session && matches!(period, "session" | "q90") {
        if period == "q90" {
            closed.retain(|&i| df[i].label == cur.label);
        } else {
            let s = session_of(&cur.label);
            closed.retain(|&i| session_of(&df[i].label) == s);
        }
        out["filters"]["session"] = json!(cur.label);
    }
    closed.retain(|&i| value[i].is_finite());
    let v: Vec<f64> = closed.iter().map(|&i| value[i]).collect();
    let x = finite(value[last]);
    out["samples"] = json!(v.len());
    out["coverage"] = json!({"from": closed.iter().map(|&i| df[i].day).min().map(iso),
                             "to": closed.iter().map(|&i| df[i].day).max().map(iso)});
    out["current"] = json!({"day": iso(cur.day), "label": cur.label, "value": x, "forming": cur.live,
                            "start_unix": unix(cur.start)});
    if v.is_empty() {
        return Ok(out);
    }

    let mut sorted = v.clone();
    sorted.sort_by(f64::total_cmp);
    let mean = np_mean(&v);
    let median = np_median(&sorted);
    let std = if v.len() > 1 { np_std1(&v, mean) } else { 0.0 };
    let (vmin, vmax) = (sorted[0], sorted[sorted.len() - 1]);
    out["stats"] = json!({"mean": mean, "median": median, "std": std, "min": vmin, "max": vmax});
    // Python's min(a, b) keeps a unless b is smaller.
    let lo = match x { Some(x) if x < vmin => x, _ => vmin };
    let hi = match x { Some(x) if x > vmax => x, _ => vmax };
    let n_bins = bins.clamp(10, 80) as usize;
    let edges = if hi > lo { linspace(lo, hi, n_bins + 1) } else { vec![lo - 0.5, hi + 0.5] };
    out["histogram"] = json!({"edges": edges, "counts": histogram(&v, &edges)});

    if let Some(x) = x {
        if v.len() >= MIN_SAMPLES {
            let n = v.len() as f64;
            let share = |f: &dyn Fn(f64) -> bool| v.iter().filter(|&&y| f(y)).count() as f64 / n;
            let low_side = x <= median;
            let tail = if low_side { share(&|y| y <= x) } else { share(&|y| y >= x) };
            out["percentile"] = json!({"below": share(&|y| y < x), "above": share(&|y| y > x), "tail": tail,
                                       "side": if low_side { "low" } else { "high" }});
            out["z"] = if std > 0.0 { json!((x - mean) / std) } else { Value::Null };
            let like: Vec<usize> =
                closed.iter().copied().filter(|&i| if low_side { value[i] <= x } else { value[i] >= x }).collect();
            let keys: Vec<i64> = like.iter().map(|&i| df[i].day.num_days_from_ce() as i64).collect();
            out["like_this"] = descending(&keys)
                .into_iter()
                .take(LIKE_THIS)
                .map(|k| {
                    let i = like[k];
                    let next = value.get(i + 1).copied().and_then(finite);
                    json!({"day": iso(df[i].day), "label": df[i].label, "value": value[i], "next": next})
                })
                .collect();
        }
    }
    Ok(out)
}

// ---- the caches, per runtime ----

/// The cached answers and history of one runtime (`quarters_cache`, `levels_cache`, `dist_cache`).
/// Engines are held weakly: a cached answer only matches while its engine is the running one.
#[derive(Default)]
struct Caches {
    quarters: Mutex<Option<(Weak<Engine>, u64, String, Arc<Value>)>>,
    levels: tokio::sync::Mutex<Levels>,
    dist: tokio::sync::Mutex<Option<DistBase>>,
}

type Key = (String, String, String); // source, symbol, clock

#[derive(Default)]
struct Levels {
    base: Option<(Key, Instant, Arc<Vec<Bar>>)>,
    value: Option<(Weak<Engine>, Key, u64, Value)>,
}

struct DistBase {
    key: Key,
    at: Instant,
    blocks: Arc<BlockRows>,
    d1: Option<Arc<Vec<Bar>>>,
}

fn caches(rt: &Arc<Runtime>) -> Arc<Caches> {
    static ALL: OnceLock<Mutex<Vec<(Weak<Runtime>, Arc<Caches>)>>> = OnceLock::new();
    let mut all = ALL.get_or_init(Default::default).lock().unwrap();
    all.retain(|(w, _)| w.strong_count() > 0);
    let me = Arc::downgrade(rt);
    if let Some((_, c)) = all.iter().find(|(w, _)| Weak::ptr_eq(w, &me)) {
        return c.clone();
    }
    let c = Arc::new(Caches::default());
    all.push((me, c.clone()));
    c
}

/// An exception the Python route doesn't catch: FastAPI's 500.
fn internal() -> Refusal {
    Refusal::new(500, "Internal Server Error")
}

fn no_data() -> Refusal {
    Refusal::new(503, "no data yet")
}

fn clock_of(engine: &Engine) -> Result<Clock, Refusal> {
    Clock::parse(&engine.clock).map_err(|_| internal())
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Result<T, Refusal> {
    tokio::task::spawn_blocking(f).await.map_err(|_| internal())
}

// ---- stored bars ----

/// `Store.load_bars(source, symbol, limit)`: the latest `limit` stored bars, oldest first, read a
/// chunk at a time.
async fn latest_stored(store: &Store, source: &str, symbol: &str, limit: usize, chunk_size: usize) -> Result<Vec<Bar>, String> {
    let mut chunks: Vec<Vec<Bar>> = Vec::new();
    let (mut total, mut before) = (0usize, None);
    while total < limit {
        let want = chunk_size.min(limit - total);
        let chunk = load_bars(store, source, symbol, want, before).await?;
        let Some(first) = chunk.first() else { break };
        before = Some(unix(first.time));
        total += chunk.len();
        let short = chunk.len() < want;
        chunks.push(chunk);
        if short {
            break;
        }
    }
    Ok(chunks.into_iter().rev().flatten().collect())
}

/// `Store.load_bar_range`: stored bars opening in [start, end], oldest first.
async fn stored_range(store: &Store, source: &str, symbol: &str, start: i64, end: i64, chunk_size: usize) -> Result<Vec<Bar>, String> {
    let mut chunks: Vec<Vec<Bar>> = Vec::new();
    let mut before = end.saturating_add(1);
    loop {
        let chunk = load_bars(store, source, symbol, chunk_size, Some(before)).await?;
        let Some(first) = chunk.first() else { break };
        let done = chunk.len() < chunk_size || unix(first.time) <= start;
        before = unix(first.time);
        chunks.push(chunk);
        if done {
            break;
        }
    }
    Ok(chunks.into_iter().rev().flatten().filter(|b| unix(b.time) >= start).collect())
}

// ---- the routes ----

/// `GET /api/quarters`: quarterly theory blocks (week, session, 90 minutes) over the engine's M1 bars.
pub async fn quarters(rt: &Arc<Runtime>, _q: &HashMap<String, String>) -> Answer {
    let engine = rt.engine();
    let st = engine.state();
    if st.version == 0 {
        return Err(no_data());
    }
    let cache = caches(rt);
    let me = Arc::downgrade(&engine);
    if let Some((e, version, clock, value)) = cache.quarters.lock().unwrap().as_ref() {
        if Weak::ptr_eq(e, &me) && *version == st.version && *clock == engine.clock {
            return Ok((**value).clone());
        }
    }
    let clock = clock_of(&engine)?;
    let name = engine.clock.clone();
    let m1 = st.m1.clone();
    let value = Arc::new(blocking(move || quarters_payload(&m1, &name, &clock)).await?);
    *cache.quarters.lock().unwrap() = Some((me, st.version, engine.clock.clone(), value.clone()));
    Ok((*value).clone())
}

/// `GET /api/sessions`: killzones and reference levels (previous day / week / month, Asia range,
/// opens) for the chart: the live buffer, reaching back into the stored bars far enough to cover
/// the previous month.
pub async fn sessions(rt: &Arc<Runtime>, _q: &HashMap<String, String>) -> Answer {
    let engine = rt.engine();
    let st = engine.state();
    let (Some(first), Some(last)) = (st.m1.first(), st.m1.last()) else { return Err(no_data()) };
    if st.version == 0 {
        return Err(no_data());
    }
    let key: Key = (engine.source.clone(), engine.symbol.clone(), engine.clock.clone());
    let me = Arc::downgrade(&engine);
    let cache = caches(rt);
    let mut levels = cache.levels.lock().await;
    if let Some((e, k, version, value)) = &levels.value {
        if Weak::ptr_eq(e, &me) && *k == key && *version == st.version {
            return Ok(value.clone());
        }
    }
    let fresh = match &levels.base {
        Some((k, at, _)) => *k != key || at.elapsed() > LEVELS_TTL,
        None => true,
    };
    if fresh {
        let older = match (&rt.store, engine.persist) {
            (Some(store), true) => {
                let start = unix(last.time) - LEVELS_DAYS * 86400;
                stored_range(store, &engine.source, &engine.symbol, start, unix(first.time) - 60, CHUNK).await.map_err(|e| {
                    log::error!("sessions: reading stored bars: {e}");
                    internal()
                })?
            }
            _ => Vec::new(),
        };
        levels.base = Some((key.clone(), Instant::now(), Arc::new(older)));
    }
    let older = levels.base.as_ref().map(|b| b.2.clone()).unwrap_or_default();
    let clock = clock_of(&engine)?;
    let (name, m1) = (engine.clock.clone(), st.m1.clone());
    let value = blocking(move || {
        let start = first_time(&m1);
        let mut bars: Vec<Bar> = older.iter().filter(|b| Some(b.time) < start).copied().collect();
        bars.extend_from_slice(&m1);
        reference_levels(&bars, &name, &clock, MAX_DAYS)
    })
    .await?;
    levels.value = Some((me, key, st.version, value.clone()));
    Ok(value)
}

fn first_time(m1: &[Bar]) -> Option<Time> {
    m1.first().map(|b| b.time)
}

/// The feed's daily bars (`engine.call(feed.daily_history)`), None when it has none or fails.
async fn daily_history(engine: &Engine, clock: &str) -> Option<Vec<Bar>> {
    let (reply, rx) = tokio::sync::oneshot::channel();
    if let Err(e) = engine.call(Command::DailyHistory { clock: clock.into(), count: DIST_DAYS, reply }).await {
        log::debug!("no daily history: {e}");
        return None;
    }
    match tokio::time::timeout(DAILY_TIMEOUT, rx).await {
        Ok(Ok(Ok(bars))) => bars,
        Ok(Ok(Err(e))) => {
            log::debug!("no daily history: {e}");
            None
        }
        _ => {
            log::debug!("no daily history: the feed didn't answer");
            None
        }
    }
}

/// `distribution_tables`: the stored history's periods (slow to read, kept a while) with the live
/// buffer's recent days fresh, and the feed's daily bars before them.
async fn distribution_tables(rt: &Arc<Runtime>) -> Result<Tables, Refusal> {
    let engine = rt.engine();
    let st = engine.state();
    if st.version == 0 || st.m1.is_empty() {
        return Err(no_data());
    }
    let key: Key = (engine.source.clone(), engine.symbol.clone(), engine.clock.clone());
    let clock = clock_of(&engine)?;
    let cache = caches(rt);
    let (blocks, d1) = {
        let mut base = cache.dist.lock().await;
        let stale = match base.as_ref() {
            Some(b) => b.key != key || b.at.elapsed() > DIST_TTL,
            None => true,
        };
        if stale {
            let stored = match (&rt.store, engine.persist) {
                (Some(store), true) => latest_stored(store, &engine.source, &engine.symbol, DIST_BARS, CHUNK).await.map_err(|e| {
                    log::error!("distribution: reading stored bars: {e}");
                    internal()
                })?,
                _ => Vec::new(),
            };
            let d1 = daily_history(&engine, &engine.clock).await.map(Arc::new);
            let m1 = st.m1.clone();
            let blocks = blocking(move || {
                let start = first_time(&m1);
                let mut union: Vec<Bar> = stored.into_iter().filter(|b| Some(b.time) < start).collect();
                union.extend_from_slice(&m1);
                BlockRows::of(&union, &clock)
            })
            .await?;
            *base = Some(DistBase { key, at: Instant::now(), blocks: Arc::new(blocks), d1 });
        }
        let b = base.as_ref().expect("just set");
        (b.blocks.clone(), b.d1.clone())
    };
    let m1 = st.m1.clone();
    blocking(move || tables(&merge_blocks(&blocks, BlockRows::of(&m1, &clock)), d1.as_deref().map(Vec::as_slice)))
        .await
}

/// The cached history's blocks before the live buffer's second day (the first one it holds whole),
/// the buffer's own from there on.
pub fn merge_blocks(cached: &BlockRows, mut fresh: BlockRows) -> BlockRows {
    let days = &fresh.week;
    let cutoff = if days.len() > 1 { Some(days[1].day) } else { days.first().map(|d| d.day) };
    let mut merged = BlockRows::default();
    let old = [&cached.week, &cached.session, &cached.q90];
    for ((out, old), new) in merged.rows_mut().into_iter().zip(old).zip(fresh.rows_mut()) {
        out.extend(old.iter().filter(|b| cutoff.is_none_or(|c| b.day < c)).cloned());
        out.extend(new.drain(..).filter(|b| cutoff.is_some_and(|c| b.day >= c)));
    }
    merged
}

// ---- FastAPI's query validation ----

/// Pydantic's lax `int` from a query string: surrounding whitespace, a sign, digits with single
/// underscores between them, and an optional `.000` part.
fn py_int(s: &str) -> Option<i128> {
    let s = s.trim();
    let (neg, body) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let (int, frac) = match body.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (body, None),
    };
    if frac.is_some_and(|f| f.is_empty() || !f.bytes().all(|b| b == b'0')) {
        return None;
    }
    if int.is_empty() || int.starts_with('_') || int.ends_with('_') || int.contains("__") {
        return None;
    }
    let digits: String = int.chars().filter(|&c| c != '_').collect();
    if !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let v = digits.parse::<i128>().unwrap_or(i128::MAX);
    Some(if neg { -v } else { v })
}

/// Pydantic's lax `bool` from a query string (any case, no surrounding space).
fn py_bool(s: &str) -> Option<bool> {
    match s.to_lowercase().as_str() {
        "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
        "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
        _ => None,
    }
}

fn flag(q: &HashMap<String, String>, name: &str, errors: &mut Vec<Value>) -> bool {
    let Some(raw) = q.get(name) else { return false };
    py_bool(raw).unwrap_or_else(|| {
        errors.push(json!({"type": "bool_parsing", "loc": ["query", name],
                           "msg": "Input should be a valid boolean, unable to interpret input", "input": raw}));
        false
    })
}

/// `bins: int = Query(40, ge=10, le=80)`.
fn bins(q: &HashMap<String, String>, errors: &mut Vec<Value>) -> i64 {
    let Some(raw) = q.get("bins") else { return 40 };
    match py_int(raw) {
        None => errors.push(json!({"type": "int_parsing", "loc": ["query", "bins"],
            "msg": "Input should be a valid integer, unable to parse string as an integer", "input": raw})),
        Some(v) if v < 10 => errors.push(json!({"type": "greater_than_equal", "loc": ["query", "bins"],
            "msg": "Input should be greater than or equal to 10", "input": raw, "ctx": {"ge": 10}})),
        Some(v) if v > 80 => errors.push(json!({"type": "less_than_equal", "loc": ["query", "bins"],
            "msg": "Input should be less than or equal to 80", "input": raw, "ctx": {"le": 80}})),
        Some(v) => return v as i64,
    }
    40
}

/// `GET /api/distribution?period=day&measure=change&lookback=1y&weekday=false&session=false&bins=40`:
/// how unusual the current period's move is against past closed periods.
pub async fn distribution(rt: &Arc<Runtime>, q: &HashMap<String, String>) -> Answer {
    let text = |k: &str, default: &str| q.get(k).cloned().unwrap_or_else(|| default.to_string());
    let (period, measure, lookback) = (text("period", "day"), text("measure", "change"), text("lookback", "1y"));
    let mut errors = Vec::new();
    let weekday = flag(q, "weekday", &mut errors);
    let session = flag(q, "session", &mut errors);
    let bins = bins(q, &mut errors);
    if !errors.is_empty() {
        return Err(Refusal::new(422, Value::Array(errors)));
    }
    let t = distribution_tables(rt).await?;
    blocking(move || move_distribution(&t, &period, &measure, &lookback, weekday, session, bins))
        .await?
        .map_err(|e| Refusal::new(422, e))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_values_parse_as_pydantic_does() {
        for (s, v) in [("12", Some(12)), (" 12 ", Some(12)), ("+12", Some(12)), ("1_0", Some(10)),
                       ("10.00", Some(10)), ("1_0.0", Some(10)), ("\u{a0}20", Some(20)), ("10.", None),
                       ("10.0_0", None), ("_10", None), ("10_", None), ("1__0", None), ("1e1", None), ("", None),
                       ("\u{1c}20", None)] {
            assert_eq!(py_int(s), v, "{s:?}");
        }
        for (s, v) in [("true", Some(true)), ("ON", Some(true)), ("Yes", Some(true)), ("0", Some(false)),
                       ("f", Some(false)), ("", None), (" true", None), ("2", None)] {
            assert_eq!(py_bool(s), v, "{s:?}");
        }
    }

    #[test]
    fn the_cache_gives_the_days_before_the_buffers_second_day() {
        let bar = |day: u32, hour: u32, price: f64| Bar {
            time: NaiveDate::from_ymd_opt(2026, 3, day).unwrap().and_hms_opt(hour, 0, 0).unwrap(),
            open: price, high: price + 1.0, low: price - 1.0, close: price, volume: 1.0,
        };
        // Cached history: Mon 2 to Thu 5 March at 100; the buffer: from Tue 3 midday to Fri 6 at 200.
        let cached: Vec<Bar> = (2..=5).flat_map(|d| (0..24).map(move |h| bar(d, h, 100.0))).collect();
        let buffer: Vec<Bar> = (3..=6).flat_map(|d| (0..24).map(move |h| bar(d, h, 200.0)))
            .filter(|b| b.time.format("%d%H").to_string() >= "0312".to_string()).collect();
        let clock = Clock::parse("UTC").unwrap();
        let merged = merge_blocks(&BlockRows::of(&cached, &clock), BlockRows::of(&buffer, &clock));
        let days: Vec<(String, f64)> = merged.week.iter().map(|p| (p.day.format("%d").to_string(), p.open)).collect();
        // The buffer starts in the trading day of the 3rd (18:00 NY the evening before): its first day
        // is partial, so the 3rd comes from the cache and the buffer counts from the 4th.
        let want: Vec<(String, f64)> = [("02", 100.0), ("03", 100.0), ("04", 200.0), ("05", 200.0), ("06", 200.0)]
            .iter()
            .map(|(d, p)| (d.to_string(), *p)).collect();
        assert_eq!(days, want);
        assert!(merged.q90.iter().all(|p| (p.open == 100.0) == (p.day < merged.week[2].day)));
        // No days in the buffer: all of the cache.
        assert_eq!(merge_blocks(&BlockRows::of(&cached, &clock), BlockRows::default()).week.len(), 5);
    }

    #[tokio::test]
    async fn stored_bars_are_read_a_chunk_at_a_time() {
        let dir = std::env::temp_dir().join(format!("wed-analysis-store-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(&format!("sqlite:///{}", dir.join("t.db").display()), &dir).await.unwrap();
        let t0 = 1_772_000_000 / 60 * 60;
        let bars: Vec<Bar> = (0..103i64)
            .map(|i| Bar { time: chrono::DateTime::from_timestamp(t0 + i * 60, 0).unwrap().naive_utc(),
                           open: i as f64, high: i as f64 + 1.0, low: i as f64 - 1.0, close: i as f64, volume: 0.0 })
            .collect();
        super::super::save_bars(&store, "tradingview", "FX:XAUUSD", &bars).await.unwrap();
        super::super::save_bars(&store, "tradingview", "OANDA:XAUUSD", &bars[..5]).await.unwrap();
        let opens = |v: Vec<Bar>| v.iter().map(|b| b.open as i64).collect::<Vec<_>>();
        for chunk in [7, 10, 103, 1000] {
            let latest = latest_stored(&store, "tradingview", "FX:XAUUSD", 25, chunk).await.unwrap();
            assert_eq!(opens(latest), (78..103).collect::<Vec<_>>(), "chunk {chunk}");
            let all = latest_stored(&store, "tradingview", "FX:XAUUSD", 500, chunk).await.unwrap();
            assert_eq!(opens(all), (0..103).collect::<Vec<_>>(), "chunk {chunk}");
            // [start, end], both included
            let range = stored_range(&store, "tradingview", "FX:XAUUSD", t0 + 20 * 60, t0 + 61 * 60, chunk).await.unwrap();
            assert_eq!(opens(range), (20..=61).collect::<Vec<_>>(), "chunk {chunk}");
            let none = stored_range(&store, "tradingview", "FX:XAUUSD", t0 - 600, t0 - 60, chunk).await.unwrap();
            assert!(none.is_empty());
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn argsort_is_numpys_quicksort() {
        // Small arrays are insertion sorted (stable); larger ones partition, which moves ties.
        assert_eq!(np_argsort(&[3, 1, 2, 1]), vec![1, 3, 2, 0]);
        let keys: Vec<i64> = (0..40).map(|i| i % 3).collect();
        let order = np_argsort(&keys);
        assert!(order.windows(2).all(|w| keys[w[0]] <= keys[w[1]]));
        let mut seen = order.clone();
        seen.sort();
        assert_eq!(seen, (0..40).collect::<Vec<_>>());
    }

    #[test]
    fn pairwise_sum_and_rolling_mean() {
        let v: Vec<f64> = (0..300).map(|i| 0.1 * i as f64).collect();
        assert!((np_sum(&v) - 4485.0).abs() < 1e-9);
        let r = rolling_mean(&[1.0, 2.0, 3.0, 4.0], 2);
        assert!(r[0].is_nan());
        assert_eq!(&r[1..], &[1.5, 2.5, 3.5]);
        assert_eq!(rolling_mean(&[0.1; 20], 14)[19], 0.1); // a repeated value reads back as itself
        // pandas 3.0's bits, where a plain running sum drifts (Kahan compensation, signs, repeats).
        let v = [0.1, 0.2, 0.3, 1e8 + 0.7, 0.11, 0.13, -5.5, 3.3, 1e-3, 7.77, 0.7, 0.7, 0.7, -0.0, 1.1];
        let want: [u64; 13] = [0x3fc9999999999999, 0x417fca055bbbbbbc, 0x417fca055b40da74, 0x417fca055a58bf25,
                               0xbffc0da740da740d, 0xbfe6147ae147ae15, 0xbfe774bc6a7ef9dc, 0x400d85cd7b900aec,
                               0x400696de8ca11bfd, 0x4008740da740da74, 0x3fe6666666666666, 0x3fdddddddddddddd,
                               0x3fe3333333333333];
        let got: Vec<u64> = rolling_mean(&v, 3)[2..].iter().map(|x| x.to_bits()).collect();
        assert_eq!(got, want);
    }
}
