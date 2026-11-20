//! Order blocks, liquidity (BSL / SSL, EQH / EQL) and inducement (`detectors/`). See the Python
//! modules' docstrings for what each one finds and why; this is the same logic, line by line.

use serde_json::{json, Value};

use super::levels::Level;
use super::structure::{Context, Side, Structure, Swing};

/// Settings shared by all detectors (`detectors/base.py`).
#[derive(Clone, Debug, PartialEq)]
pub struct DetectorParams {
    pub swing_length: usize, // bars each side for swing highs / lows (external structure)
    pub zone_body: bool,     // OB level covers the body (default) or the full candle ("wick")
    pub mitigation_close: bool, // OB taken by a close beyond it, or (default) a wick through the body
    pub max_sl: f64,         // cap on the OB stop distance, in price units
    pub eq_tolerance: f64,   // equal highs / lows: max gap as a multiple of ATR(14)
    pub idm_length: usize,   // bars each side for the internal swings inducement uses
}

impl Default for DetectorParams {
    fn default() -> Self {
        DetectorParams { swing_length: 5, zone_body: true, mitigation_close: false, max_sl: 3.0, eq_tolerance: 0.1,
                         idm_length: 2 }
    }
}

impl DetectorParams {
    /// From the settings' JSON (`{"swing_length": 5, "zone": "body", "mitigation": "wick", ...}`).
    pub fn from_json(v: &Value) -> Result<DetectorParams, String> {
        let d = DetectorParams::default();
        let int = |k: &str, def: usize| v.get(k).and_then(Value::as_u64).map(|x| x as usize).unwrap_or(def);
        let num = |k: &str, def: f64| v.get(k).and_then(Value::as_f64).unwrap_or(def);
        let zone = v.get("zone").and_then(Value::as_str).unwrap_or("body");
        let mitigation = v.get("mitigation").and_then(Value::as_str).unwrap_or("wick");
        if !matches!(zone, "body" | "wick") || !matches!(mitigation, "wick" | "close") {
            return Err(format!("unknown zone {zone:?} or mitigation {mitigation:?}"));
        }
        let p = DetectorParams {
            swing_length: int("swing_length", d.swing_length),
            zone_body: zone == "body",
            mitigation_close: mitigation == "close",
            max_sl: num("max_sl", d.max_sl),
            eq_tolerance: num("eq_tolerance", d.eq_tolerance),
            idm_length: int("idm_length", d.idm_length),
        };
        if p.swing_length < 1 || p.idm_length < 1 {
            return Err("swing lengths are 1 or more".into());
        }
        Ok(p)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Detector {
    OrderBlock,
    Liquidity,
    Inducement,
}

/// In the registry's order (`detectors/__init__.py`).
pub const DETECTORS: [Detector; 3] = [Detector::OrderBlock, Detector::Liquidity, Detector::Inducement];

impl Detector {
    pub fn name(self) -> &'static str {
        match self {
            Detector::OrderBlock => "ob",
            Detector::Liquidity => "liquidity",
            Detector::Inducement => "idm",
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Detector::OrderBlock => "Order blocks",
            Detector::Liquidity => "Liquidity (BSL / SSL)",
            Detector::Inducement => "Inducement (IDM)",
        }
    }

    pub fn from_name(name: &str) -> Option<Detector> {
        DETECTORS.into_iter().find(|d| d.name() == name.trim().to_lowercase())
    }

    /// Every level found in the window, active or ended.
    pub fn detect(self, ctx: &Context, p: &DetectorParams) -> Vec<Level> {
        match self {
            Detector::OrderBlock => order_blocks(ctx, p),
            Detector::Liquidity => liquidity(ctx, p),
            Detector::Inducement => inducement(ctx, p),
        }
    }
}

// ---- order blocks (orderblock.py) ----

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Priority {
    Extreme = 0,
    Middle = 1,
}

pub fn priority_rank(priority: Option<&str>) -> u8 {
    match priority {
        Some("extreme") => 0,
        Some("middle") => 1,
        _ => 9,
    }
}

struct Found {
    priority: Priority,
    bullish: bool,
    brk: usize,
    swing: Option<&'static str>,
}

fn order_blocks(ctx: &Context, p: &DetectorParams) -> Vec<Level> {
    if ctx.len() < 2 * p.swing_length + 2 {
        return Vec::new();
    }
    let (o, h, l, c) = (&ctx.open, &ctx.high, &ctx.low, &ctx.close);
    let red = |k: usize| c[k] < o[k];
    let green = |k: usize| c[k] > o[k];
    // candle index -> what made it an OB; extreme wins over middle
    let mut found: std::collections::BTreeMap<usize, Found> = Default::default();
    let mut add = |idx: usize, f: Found| match found.get(&idx) {
        Some(prev) if prev.priority <= f.priority => {}
        _ => {
            found.insert(idx, f);
        }
    };
    let structure = ctx.structure(p.swing_length);
    for brk in &structure.breaks {
        let (start, end) = (brk.swing.index, brk.index);
        let (ext, opposite, impulsive): (usize, &dyn Fn(usize) -> bool, &dyn Fn(usize) -> bool) = if brk.bullish {
            // Lowest low of the leg; the last one on a tie.
            let ext = (start..end).fold(start, |best, k| if l[k] <= l[best] { k } else { best });
            (ext, &red, &|k| green(k + 1) && c[k + 1] > o[k])
        } else {
            let ext = (start..end).fold(start, |best, k| if h[k] >= h[best] { k } else { best });
            (ext, &green, &|k| red(k + 1) && c[k + 1] < o[k])
        };
        if let Some(ob) = (start..=ext).rev().find(|&k| opposite(k)) {
            let swing = swing_tag(&structure, brk.bullish, ext, l, h);
            add(ob, Found { priority: Priority::Extreme, bullish: brk.bullish, brk: brk.index, swing });
        }
        for k in ext + 1..end {
            if opposite(k) && impulsive(k) {
                add(k, Found { priority: Priority::Middle, bullish: brk.bullish, brk: brk.index, swing: None });
            }
        }
    }
    found.into_iter().map(|(idx, f)| ob_level(ctx, p, idx, &f)).collect()
}

/// LH/HH for the high a sell leg started from, HL/LL for the low of a buy leg.
fn swing_tag(structure: &Structure, bullish: bool, ext: usize, low: &[f64], high: &[f64]) -> Option<&'static str> {
    let kind = if bullish { Side::Low } else { Side::High };
    // The latest earlier swing of that kind; the first listed on a tie, as Python's max.
    let mut prev: Option<&Swing> = None;
    for s in structure.swings.iter().filter(|s| s.kind == kind && s.index < ext) {
        if prev.map_or(true, |p| s.index > p.index) {
            prev = Some(s);
        }
    }
    let r = prev?.price;
    Some(if bullish { if low[ext] > r { "HL" } else { "LL" } } else if high[ext] < r { "LH" } else { "HH" })
}

fn ob_level(ctx: &Context, p: &DetectorParams, idx: usize, f: &Found) -> Level {
    let (o, h, l, c, times) = (&ctx.open, &ctx.high, &ctx.low, &ctx.close, &ctx.times);
    let (body_top, body_bottom) = (o[idx].max(c[idx]), o[idx].min(c[idx]));
    let (entry, stop) = if f.bullish {
        (body_top, body_bottom.max(body_top - p.max_sl))
    } else {
        (body_bottom, body_top.min(body_bottom + p.max_sl))
    };
    let (top, bottom) = if p.zone_body { (body_top, body_bottom) } else { (h[idx], l[idx]) };
    let tag = if f.bullish { "BULL OB" } else { "BEAR OB" };
    let extreme = f.priority == Priority::Extreme;
    let mut level = Level::new("ob", if f.bullish { "bullish" } else { "bearish" }, top, bottom, times[idx],
                               times[f.brk], if extreme { tag.to_string() } else { format!("{tag} (mid)") });
    level.meta = json!({
        "priority": if extreme { "extreme" } else { "middle" },
        "entry": entry,
        "sl": stop,
        "risk": super::round_to((entry - stop).abs(), 10),
        "body": super::round_to(body_top - body_bottom, 10),
        "sl_capped": body_top - body_bottom > p.max_sl,
        "swing": f.swing,
    })
    .as_object()
    .cloned()
    .unwrap();
    for j in f.brk + 1..c.len() {
        let (taken, touched) = if f.bullish {
            (if p.mitigation_close { c[j] < body_bottom } else { l[j] <= body_bottom }, l[j] <= entry)
        } else {
            (if p.mitigation_close { c[j] > body_top } else { h[j] >= body_top }, h[j] >= entry)
        };
        if taken {
            level.ended_time = Some(times[j]);
            break;
        }
        if touched {
            level.touches += 1;
        }
    }
    level
}

// ---- liquidity (liquidity.py) ----

fn liquidity(ctx: &Context, p: &DetectorParams) -> Vec<Level> {
    let (h, l, c, times) = (&ctx.high, &ctx.low, &ctx.close, &ctx.times);
    let mut ended = Vec::new();
    let (mut bsl, mut ssl): (Vec<(Swing, Level)>, Vec<(Swing, Level)>) = (Vec::new(), Vec::new());
    for sw in ctx.structure(p.swing_length).swings {
        let high = sw.kind == Side::High;
        let mut level = Level::new("liquidity", if high { "bsl" } else { "ssl" }, sw.price, sw.price, times[sw.index],
                                   times[sw.confirmed], if high { "BSL" } else { "SSL" });
        for j in sw.index + 1..c.len() {
            let through = if high { h[j] > sw.price } else { l[j] < sw.price };
            if through {
                level.ended_time = Some(times[j]);
                let grab = if high { c[j] <= sw.price } else { c[j] >= sw.price };
                level.meta.insert("grab".into(), grab.into());
                break;
            }
        }
        match (level.active(), high) {
            (true, true) => bsl.push((sw, level)),
            (true, false) => ssl.push((sw, level)),
            (false, _) => ended.push(level),
        }
    }
    ended.extend(merge_equal(bsl, ctx, p));
    ended.extend(merge_equal(ssl, ctx, p));
    ended
}

/// Active swings whose prices are within the tolerance of each other become one pool.
fn merge_equal(mut items: Vec<(Swing, Level)>, ctx: &Context, p: &DetectorParams) -> Vec<Level> {
    if items.is_empty() {
        return Vec::new();
    }
    items.sort_by(|a, b| a.0.price.total_cmp(&b.0.price)); // stable
    let mut groups: Vec<Vec<(Swing, Level)>> = Vec::new();
    for (sw, lvl) in items {
        match groups.last_mut() {
            Some(g) if {
                let prev = g.last().unwrap().0;
                sw.price - prev.price <= p.eq_tolerance * ctx.atr[sw.index.max(prev.index)]
            } => g.push((sw, lvl)),
            _ => groups.push(vec![(sw, lvl)]),
        }
    }
    groups
        .into_iter()
        .map(|mut group| {
            if group.len() == 1 {
                return group.pop().unwrap().1;
            }
            let kind = group[0].1.kind;
            let swings: Vec<Swing> = group.iter().map(|(s, _)| *s).collect();
            // Python's min / max keep the first of equal keys.
            let first = swings.iter().fold(swings[0], |a, s| if s.index < a.index { *s } else { a });
            let last_confirmed = swings.iter().fold(swings[0], |a, s| if s.confirmed > a.confirmed { *s } else { a });
            let top = swings.iter().map(|s| s.price).fold(f64::MIN, f64::max);
            let bottom = swings.iter().map(|s| s.price).fold(f64::MAX, f64::min);
            let label = format!("{} x{}", if kind == "bsl" { "EQH" } else { "EQL" }, group.len());
            let mut level = Level::new("liquidity", kind, top, bottom, ctx.times[first.index],
                                       ctx.times[last_confirmed.confirmed], label);
            level.meta.insert("equal".into(), group.len().into());
            level
        })
        .collect()
}

// ---- inducement (inducement.py) ----

fn inducement(ctx: &Context, p: &DetectorParams) -> Vec<Level> {
    let (h, l, c, times) = (&ctx.high, &ctx.low, &ctx.close, &ctx.times);
    let breaks = ctx.structure(p.swing_length).breaks;
    let internal = ctx.structure(p.idm_length).swings;
    // The latest IDM per direction, in the order each direction first appeared (a dict in Python).
    let mut latest: Vec<(bool, Level, usize)> = Vec::new();
    for brk in &breaks {
        let want = if brk.bullish { Side::Low } else { Side::High };
        let Some(sw) = internal.iter().find(|s| s.kind == want && s.index > brk.index) else { continue };
        let mut level = Level::new("idm", brk.direction(), sw.price, sw.price, times[sw.index], times[sw.confirmed],
                                   if brk.bullish { "BULL IDM" } else { "BEAR IDM" });
        level.meta.insert("bos_time".into(), super::levels::iso(times[brk.index]).into());
        match latest.iter_mut().find(|(b, _, _)| *b == brk.bullish) {
            None => latest.push((brk.bullish, level, sw.index)),
            Some(prev) if level.time >= prev.1.time => *prev = (brk.bullish, level, sw.index),
            Some(_) => {}
        }
    }
    for (bullish, level, at) in latest.iter_mut() {
        for j in *at + 1..c.len() {
            let through = if *bullish { l[j] < level.bottom } else { h[j] > level.top };
            if through {
                level.ended_time = Some(times[j]);
                let grab = if *bullish { c[j] >= level.bottom } else { c[j] <= level.top };
                level.meta.insert("grab".into(), grab.into());
                break;
            }
        }
    }
    latest.into_iter().map(|(_, level, _)| level).collect()
}
