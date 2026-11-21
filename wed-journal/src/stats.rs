//! Account numbers with the drawdown rebuilt from M1 prices: `src/wednesday/journal/stats.py` in
//! Rust (#96, step 9c). The output must equal Python's `analyse` to the bit, checked by
//! `tests/golden/journal/` (`tests/test_journal_golden.py` writes it, `tests/it/journal_golden.rs`
//! checks this side).
//!
//! * **Gain** is time-weighted: each closed trade grows the account by its result over the
//!   balance it was taken on, and those growths are chained.
//! * **Drawdown** is the deepest fall of the equity from its high, the equity rebuilt minute by
//!   minute from each open trade's worst price in the bar.
//! * **Verification**: every deal price is checked against the M1 bar it was made in; when the
//!   deal clock offset isn't set, the whole-hour shift most deals agree with is used.
//!
//! Parity notes, since Python's numbers come from three libraries:
//! * Python's `sum()` starts at the int 0, so an empty sum is the int `0` (and `round(0, 2)` stays
//!   an int); Python 3.11's float sum is a plain left-to-right add ([`Num`]).
//! * `round()` on a Python float rounds the exact binary value half-even ([`py_round`]), but on a
//!   numpy float64 it is `rint(x * 10**n) / 10**n` ([`np_round`]): the growth series and the
//!   monthly gain are numpy floats.
//! * pandas' `groupby().sum()` is a Kahan sum per group, starting at 0.0; `groupby().agg("min")`
//!   keeps the first of equal values.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde_json::{json, Map, Value};

/// A deal price may sit this share of the price outside its bar (spread, bid vs ask).
pub const TOLERANCE: f64 = 0.0003;
/// Seconds of missing bars allowed at either end of a trade.
pub const EDGE: i64 = 300;
/// Trades used to find the clock offset.
pub const SAMPLE: usize = 400;

/// M1 bars of one symbol, unix seconds on the price feed's clock, sorted.
#[derive(Debug, Clone, Default)]
pub struct Bars {
    pub t: Vec<i64>,
    pub open: Vec<f64>,
    pub high: Vec<f64>,
    pub low: Vec<f64>,
    pub close: Vec<f64>,
}

/// numpy `searchsorted(a, v, "right")`.
fn right(a: &[i64], v: i64) -> usize {
    a.partition_point(|&x| x <= v)
}

/// numpy `searchsorted(a, v, "left")`.
fn left(a: &[i64], v: i64) -> usize {
    a.partition_point(|&x| x < v)
}

impl Bars {
    /// Index of the bar containing `when`, if there is one.
    pub fn at(&self, when: i64) -> Option<usize> {
        let i = right(&self.t, when).checked_sub(1)?;
        (when - self.t[i] < 60).then_some(i)
    }
}

/// Whether `price` sits inside the bar at `when` (with [`TOLERANCE`]); None without a bar.
pub fn price_ok(bars: &Bars, when: i64, price: f64) -> Option<bool> {
    let i = bars.at(when)?;
    let tol = price * TOLERANCE;
    Some(bars.low[i] - tol <= price && price <= bars.high[i] + tol)
}

// ---- Python number semantics ----

/// A Python number: an int or a float, since `sum([])` is the int 0 and JSON keeps the difference.
#[derive(Debug, Clone, Copy)]
enum Num {
    I(i64),
    F(f64),
}

impl Num {
    fn add(self, other: Num) -> Num {
        match (self, other) {
            (Num::I(a), Num::I(b)) => Num::I(a + b),
            (Num::I(a), Num::F(b)) => Num::F(a as f64 + b),
            (Num::F(a), Num::I(b)) => Num::F(a + b as f64),
            (Num::F(a), Num::F(b)) => Num::F(a + b),
        }
    }

    fn neg(self) -> Num {
        match self {
            Num::I(a) => Num::I(-a),
            Num::F(a) => Num::F(-a),
        }
    }

    fn f(self) -> f64 {
        match self {
            Num::I(a) => a as f64,
            Num::F(a) => a,
        }
    }

    fn truthy(self) -> bool {
        match self {
            Num::I(a) => a != 0,
            Num::F(a) => a != 0.0,
        }
    }

    /// Python `round(x, digits)`: an int stays an int.
    fn round(self, digits: usize) -> Value {
        match self {
            Num::I(a) => json!(a),
            Num::F(a) => json!(py_round(a, digits)),
        }
    }
}

/// Python 3.11's `sum()`: from the int 0, a plain left-to-right add.
fn py_sum(items: impl IntoIterator<Item = Num>) -> Num {
    items.into_iter().fold(Num::I(0), Num::add)
}

fn floats(items: impl IntoIterator<Item = f64>) -> Num {
    py_sum(items.into_iter().map(Num::F))
}

/// Python's `round(x, digits)` on a float: the exact binary value, rounded half-even.
fn py_round(x: f64, digits: usize) -> f64 {
    wednesday_scan::round_to(x, digits)
}

/// `round(x, digits)` on a numpy float64: `rint(x * 10**digits) / 10**digits`.
fn np_round(x: f64, digits: u32) -> f64 {
    let mut f = 1.0;
    for _ in 0..digits {
        f *= 10.0;
    }
    (x * f).round_ties_even() / f
}

/// Python `min(a, b)`: `a` unless `b < a`.
fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

/// Python `max(a, b)`: `a` unless `b > a`.
fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// numpy `minimum`/`maximum` on two numbers: NaN wins.
fn np_minimum(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if b < a {
        b
    } else {
        a
    }
}

fn np_maximum(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if b > a {
        b
    } else {
        a
    }
}

fn pct(x: f64) -> f64 {
    py_round(x * 100.0, 2)
}

/// `YYYY-MM-DD` or `YYYY-MM` of unix seconds, UTC (pandas `Timestamp(t, unit="s").strftime`).
fn ymd(t: i64, fmt: &str) -> String {
    chrono::DateTime::from_timestamp(t, 0).map(|d| d.format(fmt).to_string()).unwrap_or_default()
}

// ---- the trades ----

/// One stored trade row, with its numbers read once.
struct Trade<'a> {
    row: &'a Map<String, Value>,
    symbol: &'a str,
    buy: bool,
    volume: f64,
    open_time: i64,
    open_price: f64,
    close_time: Option<i64>,
    close_price: Option<f64>,
    profit: f64,
    commission: f64,
    swap: f64,
}

fn num(v: Option<&Value>) -> Option<f64> {
    v.and_then(Value::as_f64)
}

fn int(v: Option<&Value>) -> Option<i64> {
    v.and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)))
}

impl<'a> Trade<'a> {
    fn of(row: &'a Map<String, Value>) -> Self {
        Trade {
            row,
            symbol: row.get("symbol").and_then(Value::as_str).unwrap_or(""),
            buy: row.get("side").and_then(Value::as_str) == Some("buy"),
            volume: num(row.get("volume")).unwrap_or(0.0),
            open_time: int(row.get("open_time")).unwrap_or(0),
            open_price: num(row.get("open_price")).unwrap_or(0.0),
            close_time: int(row.get("close_time")),
            close_price: num(row.get("close_price")),
            profit: num(row.get("profit")).unwrap_or(0.0),
            commission: num(row.get("commission")).unwrap_or(0.0),
            swap: num(row.get("swap")).unwrap_or(0.0),
        }
    }

    fn sign(&self) -> f64 {
        if self.buy {
            1.0
        } else {
            -1.0
        }
    }

    /// The deals: the open, and the close when there is one.
    fn ends(&self) -> Vec<(i64, f64)> {
        let mut ends = vec![(self.open_time, self.open_price)];
        if let (Some(t), Some(p)) = (self.close_time, self.close_price) {
            ends.push((t, p));
        }
        ends
    }
}

type Arrays<'a> = BTreeMap<&'a str, &'a Bars>;

/// (deal prices checked, prices inside their bar) with the deal clock shifted by `offset` seconds.
fn match_rate<'a>(trades: impl IntoIterator<Item = &'a Trade<'a>>, bars: &Arrays, offset: i64) -> (i64, i64) {
    let (mut checked, mut ok) = (0, 0);
    for tr in trades {
        let Some(b) = bars.get(tr.symbol) else { continue };
        for (when, price) in tr.ends() {
            if let Some(res) = price_ok(b, when - offset, price) {
                checked += 1;
                ok += res as i64;
            }
        }
    }
    (checked, ok)
}

/// The deal-clock minus price-clock offset (whole hours, in seconds) most deal prices agree with.
fn detect_offset(trades: &[Trade], bars: &Arrays) -> Option<i64> {
    let usable: Vec<&Trade> = trades.iter().filter(|t| bars.contains_key(t.symbol)).collect();
    if usable.is_empty() {
        return None;
    }
    let step = (usable.len() / SAMPLE).max(1);
    let sample: Vec<&Trade> = usable.into_iter().step_by(step).collect();
    let (mut best, mut best_score) = (None, (0.0f64, 0i64));
    // ties go to the smaller shift: 0, -1, 1, -2, 2, ...
    let hours = std::iter::once(0).chain((1..=14).flat_map(|h| [-h, h]));
    for h in hours {
        let (checked, ok) = match_rate(sample.iter().copied(), bars, h * 3600);
        let score = (if checked > 0 { ok as f64 / checked as f64 } else { 0.0 }, checked);
        let better = score.0 > best_score.0 || (score.0 == best_score.0 && score.1 > best_score.1);
        if checked >= 2 && better {
            best = Some(h * 3600);
            best_score = score;
        }
    }
    best.filter(|_| best_score.0 >= 0.6)
}

/// numpy `median` of a non-empty list.
fn median(mut v: Vec<f64>) -> f64 {
    v.sort_by(|a, b| a.total_cmp(b));
    let n = v.len();
    if n % 2 == 1 {
        v[n / 2]
    } else {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    }
}

/// Account currency per 1.0 price move per lot, per symbol, read off the trades' own profits.
fn multipliers(trades: &[Trade]) -> HashMap<String, f64> {
    let mut seen: HashMap<String, Vec<f64>> = HashMap::new();
    for tr in trades {
        let Some(close) = tr.close_price else { continue };
        let mv = (close - tr.open_price) * tr.sign() * tr.volume;
        if mv.abs() > 1e-9 && tr.profit != 0.0 {
            let m = tr.profit / mv;
            if m > 0.0 {
                seen.entry(tr.symbol.to_string()).or_default().push(m);
            }
        }
    }
    seen.into_iter().map(|(s, v)| (s, median(v))).collect()
}

/// A verified trade's minutes: times on the deal clock, worst floating, floating at the close
/// and best floating.
struct Floating {
    t: Vec<i64>,
    worst: Vec<f64>,
    mark: Vec<f64>,
    best: Vec<f64>,
}

/// Per bar the trade was open, its floating result; None when bars are missing at either end.
fn floating(tr: &Trade, b: &Bars, offset: i64, mult: f64, last: Option<i64>) -> Option<Floating> {
    let t0 = tr.open_time - offset;
    let t1 = match tr.close_time {
        Some(c) => c - offset,
        None => last?,
    };
    if b.t.is_empty() {
        return None;
    }
    let i0 = right(&b.t, t0) as i64 - 1;
    let i1 = right(&b.t, t1) as i64 - 1;
    if i0 < 0 || t0 - b.t[i0 as usize] >= EDGE || i1 < i0 || t1 - b.t[i1 as usize] >= EDGE {
        return None;
    }
    let (i0, i1) = (i0 as usize, i1 as usize);
    let buy = tr.buy;
    let mut worst = if buy { &b.low } else { &b.high }[i0..=i1].to_vec();
    let mut best = if buy { &b.high } else { &b.low }[i0..=i1].to_vec();
    let mut mark = b.close[i0..=i1].to_vec();
    type Pick = fn(f64, f64) -> f64;
    let (worse, better): (Pick, Pick) =
        if buy { (np_minimum, np_maximum) } else { (np_maximum, np_minimum) };
    let entry = tr.open_price;
    let exit = if tr.close_time.is_some() { tr.close_price } else { None };
    // the entry minute: from the deal price to the bar's close
    worst[0] = worse(entry, mark[0]);
    best[0] = better(entry, mark[0]);
    if let Some(exit) = exit {
        let start = if i1 == i0 { entry } else { b.open[i1] };
        let n = worst.len() - 1;
        worst[n] = worse(start, exit);
        best[n] = better(start, exit);
        mark[n] = exit;
        if i1 == i0 {
            worst[n] = worse(worst[n], entry);
            best[n] = better(best[n], entry);
        }
    }
    let k = tr.sign() * tr.volume * mult;
    let f = |v: Vec<f64>| v.into_iter().map(|x| (x - entry) * k).collect::<Vec<f64>>();
    Some(Floating {
        t: b.t[i0..=i1].iter().map(|t| t + offset).collect(),
        worst: f(worst),
        mark: f(mark),
        best: f(best),
    })
}

/// What a pip is, the way trading journals count them: 0.1 on gold, 0.01 on yen pairs and silver.
pub fn pip_size(symbol: &str, price: f64) -> f64 {
    let s = symbol.to_uppercase();
    if s.contains("XAU") || s.contains("GOLD") {
        0.1
    } else if s.contains("JPY") || s.contains("XAG") || s.contains("SILVER") {
        0.01
    } else if price < 20.0 {
        0.0001
    } else if price < 1000.0 {
        0.01
    } else {
        0.1
    }
}

fn pips(tr: &Trade) -> Option<f64> {
    let close = tr.close_price?;
    let mv = (close - tr.open_price) * tr.sign();
    Some(py_round(mv / pip_size(tr.symbol, tr.open_price), 1))
}

/// A trade row's numbers the summaries read.
struct Row {
    open_time: i64,
    close_time: Option<i64>,
    volume: f64,
    commission: f64,
    swap: f64,
    net: f64,
    pips: Option<f64>,
    symbol: String,
    price_ok: Option<bool>,
    verified: bool,
    floating: Option<f64>,
}

impl Row {
    /// Python `r["pips"] or 0`: the int 0 when there are none or they are zero.
    fn pips_or_int(&self) -> Num {
        match self.pips {
            Some(p) if p != 0.0 => Num::F(p),
            _ => Num::I(0),
        }
    }
}

/// Closed trades per day (deal clock, by the close): result in money and pips.
fn daily(rows: &[&Row]) -> Vec<Value> {
    let mut days: BTreeMap<String, (f64, f64, i64, i64)> = BTreeMap::new();
    for r in rows {
        let Some(ct) = r.close_time else { continue };
        let d = days.entry(ymd(ct, "%Y-%m-%d")).or_insert((0.0, 0.0, 0, 0));
        d.0 += r.net;
        d.1 += r.pips.filter(|p| *p != 0.0).unwrap_or(0.0);
        d.2 += 1;
        d.3 += (r.net > 0.0) as i64;
    }
    days.into_iter()
        .map(|(day, (profit, pips, trades, won))| {
            json!({"day": day, "profit": py_round(profit, 2), "pips": py_round(pips, 1), "trades": trades, "won": won})
        })
        .collect()
}

/// Gain per calendar month (time-weighted, like the total) with the money and pips made in it.
fn monthly(ev_t: &[i64], fac_after: &[f64], rows: &[&Row]) -> Vec<Value> {
    if ev_t.is_empty() {
        return vec![];
    }
    let months: Vec<String> = ev_t.iter().map(|&t| ymd(t, "%Y-%m")).collect();
    let mut out = vec![];
    let mut prev = 1.0;
    for m in months.iter().collect::<BTreeSet<_>>() {
        let last = months.iter().rposition(|x| x == m).unwrap();
        let end = fac_after[last];
        let mine: Vec<&&Row> =
            rows.iter().filter(|r| r.close_time.is_some_and(|c| &ymd(c, "%Y-%m") == m)).collect();
        out.push(json!({
            "month": m,
            "gain": if prev > 0.0 { json!(np_round((end / prev - 1.0) * 100.0, 2)) } else { Value::Null },
            "profit": floats(mine.iter().map(|r| r.net)).round(2),
            "pips": py_sum(mine.iter().map(|r| r.pips_or_int())).round(1),
            "trades": mine.len(),
        }));
        prev = end;
    }
    out
}

/// pandas `Series(v).groupby(t // 3600 * 3600).agg("min")`, rounded to 4 places.
fn hourly_min(t: &[i64], v: &[f64]) -> Vec<Value> {
    let mut groups: BTreeMap<i64, Option<f64>> = BTreeMap::new();
    for (&t, &x) in t.iter().zip(v) {
        let g = groups.entry(t.div_euclid(3600) * 3600).or_insert(None);
        if x.is_nan() {
            continue;
        }
        match g {
            Some(cur) if x >= *cur => {} // NaN was skipped above
            _ => *g = Some(x),
        }
    }
    groups
        .into_iter()
        .map(|(k, x)| json!([k, x.map(|x| py_round(x, 4))]))
        .collect()
}

/// pandas `DataFrame.groupby("t").sum()` on two columns: keys sorted, a Kahan sum per group in
/// row order.
fn group_sum(t: &[i64], w: &[f64], m: &[f64]) -> (Vec<i64>, Vec<f64>, Vec<f64>) {
    #[derive(Default)]
    struct Kahan {
        sum: f64,
        comp: f64,
    }
    impl Kahan {
        fn add(&mut self, val: f64) {
            if val.is_nan() {
                return;
            }
            let y = val - self.comp;
            let t = self.sum + y;
            self.comp = t - self.sum - y;
            if self.comp.is_nan() {
                self.comp = 0.0;
            }
            self.sum = t;
        }
    }
    let mut groups: BTreeMap<i64, (Kahan, Kahan)> = BTreeMap::new();
    for i in 0..t.len() {
        let g = groups.entry(t[i]).or_default();
        g.0.add(w[i]);
        g.1.add(m[i]);
    }
    let mut out = (vec![], vec![], vec![]);
    for (k, (a, b)) in groups {
        out.0.push(k);
        out.1.push(a.sum);
        out.2.push(b.sum);
    }
    out
}

/// Everything the journal page shows. `bars` maps a trade symbol to its M1 bars (price clock);
/// `offset` is the deal clock minus the price clock in seconds, None to detect it.
pub fn analyse(
    trades: &[Map<String, Value>],
    cash: &[Map<String, Value>],
    bars: &BTreeMap<String, Bars>,
    offset: Option<i64>,
) -> Value {
    let arrays: Arrays = bars.iter().filter(|(_, b)| !b.t.is_empty()).map(|(s, b)| (s.as_str(), b)).collect();
    let trs: Vec<Trade> = trades.iter().map(Trade::of).collect();
    let detected = offset.is_none();
    let offset = if detected { detect_offset(&trs, &arrays) } else { offset };
    let mut mult = multipliers(&trs);
    for s in arrays.keys() {
        // gold without a closed trade to learn from: 100 oz a lot
        let up = s.to_uppercase();
        if !mult.contains_key(*s) && (up.contains("XAU") || up.contains("GOLD")) {
            mult.insert(s.to_string(), 100.0);
        }
    }

    // ---- per trade: price check and floating P/L ----
    let mut out_rows: Vec<Value> = vec![];
    let mut rows: Vec<Row> = vec![];
    let mut pieces: Vec<(Floating, i64, Option<i64>)> = vec![];
    let (mut total_time, mut verified_time) = (0i64, 0i64);
    for tr in &trs {
        let net = tr.profit + tr.commission + tr.swap;
        let b = arrays.get(tr.symbol).copied();
        let checks: Vec<Option<bool>> = match (b, offset) {
            (Some(b), Some(off)) => tr.ends().into_iter().map(|(w, p)| price_ok(b, w - off, p)).collect(),
            _ => vec![],
        };
        let ok = if checks.iter().all(Option::is_none) { None } else { Some(checks.iter().all(|c| *c != Some(false))) };
        let mut fl = None;
        if let (Some(b), Some(off), Some(m)) = (b, offset, mult.get(tr.symbol)) {
            if ok != Some(false) {
                let last = if tr.close_time.is_none() { Some(*b.t.last().unwrap()) } else { None };
                fl = floating(tr, b, off, *m, last);
            }
        }
        let end = match (tr.close_time, b, offset) {
            (Some(c), _, _) => c,
            (None, Some(b), Some(off)) => b.t.last().unwrap() + off,
            _ => tr.open_time,
        };
        let span = (end - tr.open_time).max(60);
        total_time += span;
        let mut row = tr.row.clone();
        let p = pips(tr);
        row.insert("net".into(), json!(net));
        row.insert("pips".into(), json!(p));
        row.insert("price_ok".into(), json!(ok));
        row.insert("verified".into(), json!(fl.is_some()));
        row.insert("mae".into(), Value::Null);
        row.insert("mfe".into(), Value::Null);
        row.insert("floating".into(), Value::Null);
        let mut floating_v = None;
        let is_verified = fl.is_some();
        if let Some(f) = fl {
            verified_time += span;
            let wmin = f.worst.iter().copied().fold(f64::INFINITY, |a, x| if x < a { x } else { a });
            let bmax = f.best.iter().copied().fold(f64::NEG_INFINITY, |a, x| if x > a { x } else { a });
            row.insert("mae".into(), json!(py_round(py_min(wmin, 0.0), 2)));
            row.insert("mfe".into(), json!(py_round(py_max(bmax, 0.0), 2)));
            if tr.close_time.is_none() {
                let v = py_round(*f.mark.last().unwrap(), 2);
                floating_v = Some(v);
                row.insert("floating".into(), json!(v));
                row.insert("close_price".into(), json!(*b.unwrap().close.last().unwrap()));
            }
            pieces.push((f, tr.open_time, tr.close_time));
        } else if tr.close_time.is_none() {
            let v = tr.profit + tr.swap;
            floating_v = Some(v);
            row.insert("floating".into(), json!(v));
        }
        out_rows.push(Value::Object(row));
        rows.push(Row {
            open_time: tr.open_time,
            close_time: tr.close_time,
            volume: tr.volume,
            commission: tr.commission,
            swap: tr.swap,
            net,
            pips: p,
            symbol: tr.symbol.to_string(),
            price_ok: ok,
            verified: is_verified,
            floating: floating_v,
        });
    }

    let closed: Vec<&Row> = rows.iter().filter(|r| r.close_time.is_some()).collect();
    let open: Vec<&Row> = rows.iter().filter(|r| r.close_time.is_none()).collect();

    // ---- realised timeline: cash and closed trades ----
    let mut events: Vec<(i64, u8, f64, String)> = cash
        .iter()
        .map(|c| {
            (int(c.get("time")).unwrap_or(0), 0, num(c.get("amount")).unwrap_or(0.0),
             c.get("kind").and_then(Value::as_str).unwrap_or("").to_string())
        })
        .chain(closed.iter().map(|r| (r.close_time.unwrap(), 1, r.net, "trade".to_string())))
        .collect();
    events.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then(a.1.cmp(&b.1))
            .then(a.2.partial_cmp(&b.2).unwrap_or(std::cmp::Ordering::Equal))
            .then(a.3.cmp(&b.3))
    });
    let n_ev = events.len();
    let ev_t: Vec<i64> = events.iter().map(|e| e.0).collect();
    let (mut bal_after, mut fac_after) = (vec![0.0; n_ev], vec![1.0; n_ev]);
    let (mut balance, mut factor) = (0.0f64, 1.0f64);
    for (i, (_, _, amount, kind)) in events.iter().enumerate() {
        if kind == "trade" && balance > 0.0 {
            factor *= py_max(1.0 + amount / balance, 0.0);
        }
        balance += amount;
        bal_after[i] = balance;
        fac_after[i] = factor;
    }

    // ---- equity at every minute a verified trade was open ----
    let (ft, fw, fm) = {
        let t: Vec<i64> = pieces.iter().flat_map(|p| p.0.t.iter().copied()).collect();
        let w: Vec<f64> = pieces.iter().flat_map(|p| p.0.worst.iter().copied()).collect();
        let m: Vec<f64> = pieces.iter().flat_map(|p| p.0.mark.iter().copied()).collect();
        group_sum(&t, &w, &m)
    };
    let nf = ft.len();
    let (mut fbal, mut idx_w, mut idx_m) = (vec![0.0; nf], vec![0.0; nf], vec![0.0; nf]);
    for i in 0..nf {
        let k = left(&ev_t, ft[i]); // events strictly before the bar
        let (bal, fac) = if k > 0 { (bal_after[k - 1], fac_after[k - 1]) } else { (0.0, 1.0) };
        fbal[i] = bal;
        if bal > 0.0 {
            idx_w[i] = fac * np_maximum(1.0 + fw[i] / bal, 0.0);
            idx_m[i] = fac * np_maximum(1.0 + fm[i] / bal, 0.0);
        } else {
            idx_w[i] = fac;
            idx_m[i] = fac;
        }
    }

    // at each cash or close event: the floating result of the other trades still open then
    let mut ev_float = vec![0.0; n_ev];
    for (p, opened, closes) in &pieces {
        let until = closes.unwrap_or(i64::MAX);
        for (i, &t) in ev_t.iter().enumerate() {
            if t >= *opened && t < until {
                let j = right(&p.t, t);
                ev_float[i] += if j > 0 { p.mark[j - 1] } else { 0.0 };
            }
        }
    }
    let ev_idx: Vec<f64> = (0..n_ev)
        .map(|i| {
            if bal_after[i] > 0.0 {
                fac_after[i] * np_maximum(1.0 + ev_float[i] / bal_after[i], 0.0)
            } else {
                fac_after[i]
            }
        })
        .collect();

    let pt_raw: Vec<i64> = ev_t.iter().chain(ft.iter()).copied().collect();
    let mut order: Vec<usize> = (0..pt_raw.len()).collect();
    order.sort_by_key(|&i| pt_raw[i]); // stable
    let pick = |a: &[f64], b: &[f64]| -> Vec<f64> {
        let all: Vec<f64> = a.iter().chain(b).copied().collect();
        order.iter().map(|&i| all[i]).collect()
    };
    let pt: Vec<i64> = order.iter().map(|&i| pt_raw[i]).collect();
    let pw = pick(&ev_idx, &idx_w);
    let pm = pick(&ev_idx, &idx_m);
    let eq_ev: Vec<f64> = (0..n_ev).map(|i| bal_after[i] + ev_float[i]).collect();
    let eq_f: Vec<f64> = (0..nf).map(|i| fbal[i] + fw[i]).collect();
    let peq = pick(&eq_ev, &eq_f);
    let (dd, max_dd, dd_at) = if !pt.is_empty() {
        let mut dd = Vec::with_capacity(pt.len());
        let mut peak = 1.0f64;
        for i in 0..pt.len() {
            if i > 0 {
                peak = np_maximum(peak, pm[i - 1]);
            }
            let x = 1.0 - pw[i] / np_maximum(peak, 1e-12);
            dd.push(x.clamp(0.0, 1.0)); // NaN stays NaN, as in np.clip
        }
        // numpy argmax: the first of equal values, or the first NaN
        let mut worst_i = 0;
        for i in 0..dd.len() {
            if dd[i].is_nan() {
                worst_i = i;
                break;
            }
            if dd[i] > dd[worst_i] {
                worst_i = i;
            }
        }
        let (m, at) = (dd[worst_i], pt[worst_i]);
        (dd, m, Some(at))
    } else {
        (vec![], 0.0, None)
    };

    // ---- summary ----
    let kind = |c: &Map<String, Value>, k: &str| c.get("kind").and_then(Value::as_str) == Some(k);
    let amounts = |k: &str| floats(cash.iter().filter(|c| kind(c, k)).map(|c| num(c.get("amount")).unwrap_or(0.0)));
    let deposits = amounts("deposit");
    let withdrawals = amounts("withdrawal").neg();
    let profit = floats(closed.iter().map(|r| r.net));
    let floating_sum = floats(open.iter().map(|r| r.floating.filter(|f| *f != 0.0).unwrap_or(0.0)));
    let gain = factor - 1.0;
    let cash_times = || cash.iter().map(|c| int(c.get("time")).unwrap_or(0));
    let start = cash_times().chain(rows.iter().map(|r| r.open_time)).min();
    let last = cash_times()
        .chain(closed.iter().map(|r| r.close_time.unwrap()))
        .chain(open.iter().map(|r| r.open_time))
        .max();
    let days = py_max((last.unwrap_or(0) - start.unwrap_or(0)) as f64 / 86400.0, 1.0);
    let wins: Vec<f64> = closed.iter().map(|r| r.net).filter(|n| *n > 0.0).collect();
    let losses: Vec<f64> = closed.iter().map(|r| r.net).filter(|n| *n < 0.0).collect();
    let verified = rows.iter().filter(|r| r.verified).count() as i64;
    let (checked, matched) = match offset {
        Some(off) => match_rate(trs.iter(), &arrays, off),
        None => (0, 0),
    };
    let first_max = |v: &[f64]| v.iter().copied().fold(None, |a: Option<f64>, x| Some(a.map_or(x, |a| py_max(a, x))));
    let first_min = |v: &[f64]| v.iter().copied().fold(None, |a: Option<f64>, x| Some(a.map_or(x, |a| py_min(a, x))));
    let opt = |v: Option<f64>| v.map_or(Value::Null, |x| json!(x));
    let no_prices: BTreeSet<&str> =
        rows.iter().map(|r| r.symbol.as_str()).filter(|s| !arrays.contains_key(s)).collect();
    let mismatched: Vec<Value> = trs
        .iter()
        .zip(&rows)
        .filter(|(_, r)| r.price_ok == Some(false))
        .map(|(t, _)| t.row.get("id").cloned().unwrap_or(Value::Null))
        .take(50)
        .collect();
    let fw_min = fw.iter().copied().fold(f64::INFINITY, |a, x| if x < a { x } else { a });
    let sum_wins = floats(wins.iter().copied());
    let sum_losses = floats(losses.iter().copied());

    json!({
        "summary": {
            "gain": pct(gain),
            "abs_gain": if deposits.truthy() { json!(pct(profit.f() / deposits.f())) } else { Value::Null },
            "daily": opt((gain > -1.0).then(|| pct((1.0 + gain).powf(1.0 / days) - 1.0))),
            "monthly": opt((gain > -1.0).then(|| pct((1.0 + gain).powf(30.4375 / days) - 1.0))),
            "drawdown": pct(max_dd),
            "drawdown_at": dd_at,
            "balance": py_round(balance, 2),
            "equity": py_round(balance + floating_sum.f(), 2),
            "floating": floating_sum.round(2),
            "profit": profit.round(2),
            "deposits": deposits.round(2),
            "withdrawals": withdrawals.round(2),
            "credit": amounts("credit").round(2),
            "other": amounts("other").round(2),
            "max_floating_loss": opt((!fw.is_empty()).then(|| py_round(py_min(fw_min, 0.0), 2))),
            "start": start,
            "last": last,
        },
        "trading": {
            "trades": closed.len(),
            "open": open.len(),
            "won": wins.len(),
            "lost": losses.len(),
            "win_rate": opt((!closed.is_empty()).then(|| pct(wins.len() as f64 / closed.len() as f64))),
            "profit_factor": opt((!losses.is_empty()).then(|| py_round(sum_wins.f() / -sum_losses.f(), 2))),
            "avg_win": opt((!wins.is_empty()).then(|| py_round(sum_wins.f() / wins.len() as f64, 2))),
            "avg_loss": opt((!losses.is_empty()).then(|| py_round(sum_losses.f() / losses.len() as f64, 2))),
            "best": opt(first_max(&wins).map(|x| py_round(x, 2))),
            "worst": opt(first_min(&losses).map(|x| py_round(x, 2))),
            "lots": floats(closed.iter().map(|r| r.volume)).round(2),
            "commission": floats(rows.iter().map(|r| r.commission)).round(2),
            "swap": floats(rows.iter().map(|r| r.swap)).round(2),
            // int(np.mean(...)): the hold times are whole seconds, so the float sum is exact
            "avg_hold": (!closed.is_empty()).then(|| {
                let total: i64 = closed.iter().map(|r| r.close_time.unwrap() - r.open_time).sum();
                (total as f64 / closed.len() as f64) as i64
            }),
        },
        "verification": {
            "basis": if verified > 0 { "ohlc" } else { "closed" },
            "trades": rows.len(),
            "verified": verified,
            "coverage": opt((total_time != 0).then(|| pct(verified_time as f64 / total_time as f64))),
            "prices_checked": checked,
            "prices_ok": matched,
            "offset_hours": offset.map(|o| o as f64 / 3600.0),
            "offset_detected": detected,
            "no_prices": no_prices,
            "mismatched": mismatched,
        },
        "series": {
            "growth": ev_t.iter().zip(&fac_after).map(|(t, f)| json!([t, np_round((f - 1.0) * 100.0, 4)])).collect::<Vec<_>>(),
            "balance": ev_t.iter().zip(&bal_after).map(|(t, b)| json!([t, py_round(*b, 2)])).collect::<Vec<_>>(),
            "equity": hourly_min(&pt, &peq),
            "drawdown": hourly_min(&pt, &dd.iter().map(|d| -d * 100.0).collect::<Vec<_>>()),
        },
        "monthly": monthly(&ev_t, &fac_after, &closed),
        "daily": daily(&closed),
        "trades": out_rows,
    })
}

