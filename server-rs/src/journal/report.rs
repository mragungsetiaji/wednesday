//! The MT5 history report (HTML) as trades and cash: `src/wednesday/journal/imports.py` in Rust
//! (#96). The output, and every `ReportError` message, must equal Python's `parse_report`,
//! checked by `tests/golden/reports/` (`tests/test_report_golden.py` writes it,
//! `tests/report_golden.rs` checks this side).
//!
//! Python reads the file with `html.parser.HTMLParser` and plain `str`/`float`/`strptime`, so this
//! file copies the parts of those the import goes through:
//! * the decoding (`_decode`): a UTF-16 BOM, UTF-16 LE without one when the second byte is zero,
//!   else UTF-8 (a BOM dropped) with bad bytes replaced;
//! * `HTMLParser.feed` without `close()` (Python 3.11): the tag and attribute regexes as their
//!   greedy matches resolve, `<script>`/`<style>` raw text, comments, declarations, and parsing
//!   stopping for good at a tag that never ends; `html.unescape` with Python's entity table;
//! * `str.split()`/`strip()` whitespace (`str.isspace`, which has `\x1c`-`\x1f` and `\xa0`), `\d`
//!   as any Unicode decimal digit, `int()`/`float()` (underscores, Unicode digits, `inf`/`nan`),
//!   `strptime` (its regexes, first match only) and `repr()` of a string in the error messages.
//!
//! Where Python raises something other than `ReportError` (a UTF-16 file cut mid-character, a
//! `<![...` section `_markupbase` rejects, a table with no Time column) the route answered 500;
//! here it is an `Err` with [`UNREADABLE`]. A `nan`/`inf` number becomes JSON `null` (Python
//! writes `NaN`), and a colspan is capped at [`MAX_SPAN`] (Python would build the whole row).

use serde_json::{json, Map, Value};

/// The message for files Python fails on with an exception other than `ReportError`.
pub const UNREADABLE: &str =
    "Couldn't read that report. Save it again in the terminal: History tab, right click, Report, HTML";
/// The widest colspan expanded.
pub const MAX_SPAN: i64 = 100_000;

const BALANCE: i32 = 2;
const CREDIT: i32 = 3;
const BONUS: i32 = 6;
const SECTIONS: [&str; 7] = ["positions", "orders", "deals", "open positions", "working orders", "results", "summary"];

enum Fail {
    Report(String),
    Other,
}

impl From<String> for Fail {
    fn from(s: String) -> Fail {
        Fail::Report(s)
    }
}

/// `{"account": {...}, "trades": [...], "cash": [...]}` from an MT5 history report (HTML), or the
/// message for the trader.
pub fn parse_report(data: &[u8]) -> Result<Value, String> {
    match parse(data) {
        Ok(v) => Ok(v),
        Err(Fail::Report(m)) => Err(m),
        Err(Fail::Other) => Err(UNREADABLE.to_string()),
    }
}

fn parse(data: &[u8]) -> Result<Value, Fail> {
    let text: Vec<char> = decode(data).ok_or(Fail::Other)?.chars().collect();
    if !contains_ascii_ci(&text, "<table") {
        return Err(Fail::Report(
            "That isn't an MT5 report. In the terminal: History tab, right click, Report, HTML".into(),
        ));
    }
    let rows = table_rows(&text).map_err(|_| Fail::Other)?;

    let mut account = Map::new();
    for row in rows.iter().take(20) {
        let cells: Vec<&String> = row.iter().filter(|c| !c.is_empty()).collect();
        for i in 0..cells.len().saturating_sub(1) {
            let label = cells[i].trim_end_matches(':').to_lowercase();
            if label == "account" {
                if let Some((login, details)) = account_line(cells[i + 1]) {
                    account.insert("login".into(), json!(login));
                    let details = details.unwrap_or_default();
                    let parts: Vec<&str> = details.split(',').map(py_strip).collect();
                    if parts[0].chars().count() == 3 && parts[0].chars().all(|c| c.is_ascii_uppercase()) {
                        account.insert("currency".into(), json!(parts[0]));
                    }
                    if parts.len() > 1 && !parts[1].is_empty() {
                        account.insert("server".into(), json!(parts[1]));
                    }
                }
            } else if label == "company" {
                account.insert("company".into(), json!(cells[i + 1]));
            } else if label == "name" {
                account.insert("name".into(), json!(cells[i + 1]));
            }
        }
    }

    // section name -> its rows, in the order the sections first have one
    let mut sections: Vec<(String, Vec<Record>)> = Vec::new();
    let mut current: Option<String> = None;
    let mut cols: Option<Vec<(String, usize)>> = None;
    for row in &rows {
        let cells: Vec<&String> = row.iter().filter(|c| !c.is_empty()).collect();
        if cells.len() == 1 {
            let name = cells[0].to_lowercase();
            if SECTIONS.contains(&name.as_str()) {
                current = Some(name);
                cols = None;
                continue;
            }
        }
        let Some(section) = &current else { continue };
        let Some(header) = &cols else {
            cols = Some(header_of(row));
            continue;
        };
        if cells.len() < 4 {
            continue; // totals line under a table
        }
        let record = header.iter().map(|(k, i)| (k.clone(), row.get(*i).cloned().unwrap_or_default())).collect();
        match sections.iter_mut().find(|(name, _)| name == section) {
            Some((_, list)) => list.push(record),
            None => sections.push((section.clone(), vec![record])),
        }
    }
    let section = |name: &str| sections.iter().find(|(n, _)| n == name).map(|(_, l)| l.as_slice()).unwrap_or(&[]);
    let (positions, deals) = (section("positions"), section("deals"));
    if positions.is_empty() && deals.is_empty() {
        return Err(Fail::Report(
            "No Positions or Deals table in the report. Save it from the History tab as HTML".into(),
        ));
    }

    let mut trades: Vec<(i64, String, Value)> = Vec::new();
    for p in positions {
        let side = get(p, "type").unwrap_or("").to_lowercase();
        if (side != "buy" && side != "sell") || get(p, "close time").map_or(true, str::is_empty) {
            continue;
        }
        let text = |k: &str| get(p, k).unwrap_or("");
        let commission = num(text("commission"))? + num(text("fee"))?;
        let id = format!("p{}", get(p, "position").unwrap_or("None"));
        let volume = num(text("volume").split('/').next().unwrap_or(""))?;
        let open_time = time(get(p, "time").ok_or(Fail::Other)?)?;
        let open_price = num(text("price"))?;
        let close_time = time(text("close time"))?;
        let close_price = num(text("close price"))?;
        let profit = num(text("profit"))?;
        let swap = num(text("swap"))?;
        let sl = num(text("s/l"))?;
        let tp = num(text("t/p"))?;
        let or_null = |x: f64| if x == 0.0 { Value::Null } else { json!(x) };
        let trade = json!({
            "id": id, "position": text("position"), "symbol": text("symbol"), "side": side, "volume": volume,
            "open_time": open_time, "open_price": open_price, "close_time": close_time, "close_price": close_price,
            "profit": profit, "commission": commission, "swap": swap, "sl": or_null(sl), "tp": or_null(tp),
            "comment": null,
        });
        trades.push((open_time, id, trade));
    }

    let mut cash: Vec<(i64, String, Value)> = Vec::new();
    for d in deals {
        let kind = get(d, "type").unwrap_or("").to_lowercase();
        if kind == "buy" || kind == "sell" || kind.is_empty() {
            continue;
        }
        let text = |k: &str| get(d, k).unwrap_or("");
        let amount = num(text("profit"))? + num(text("commission"))? + num(text("fee"))? + num(text("swap"))?;
        let id = match get(d, "deal") {
            Some(deal) if !deal.is_empty() => deal.to_string(),
            _ => format!("{}:{}", get(d, "time").unwrap_or("None"), float_repr(amount)),
        };
        let at = time(get(d, "time").ok_or(Fail::Other)?)?;
        let deal_type = match kind.as_str() {
            "balance" => BALANCE,
            "credit" => CREDIT,
            "bonus" => BONUS,
            _ => -1,
        };
        let comment = match text("comment") {
            "" => Value::Null,
            c => json!(c),
        };
        cash.push((at, id.clone(), json!({
            "id": id, "time": at, "kind": cash_kind(deal_type, amount), "amount": amount, "comment": comment,
        })));
    }

    if trades.is_empty() && cash.is_empty() {
        return Err(Fail::Report("The report has no closed trades or deposits".into()));
    }
    trades.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    cash.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    Ok(json!({
        "account": account,
        "trades": trades.into_iter().map(|t| t.2).collect::<Vec<_>>(),
        "cash": cash.into_iter().map(|c| c.2).collect::<Vec<_>>(),
    }))
}

/// `cash_kind` in `imports.py`: what a cash deal is, from its MT5 deal type.
pub fn cash_kind(deal_type: i32, amount: f64) -> &'static str {
    match deal_type {
        BALANCE if amount >= 0.0 => "deposit",
        BALANCE => "withdrawal",
        CREDIT | BONUS => "credit",
        _ => "other", // charges, corrections, interest, fees billed as their own deal
    }
}

/// A table row as (column name, cell) in the header's order: a dict in Python.
type Record = Vec<(String, String)>;

fn get<'a>(record: &'a [(String, String)], key: &str) -> Option<&'a str> {
    record.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

/// `_header`: column name -> index; a repeated name gets a "close " prefix.
fn header_of(row: &[String]) -> Vec<(String, usize)> {
    let mut cols: Vec<(String, usize)> = Vec::new();
    for (i, name) in row.iter().enumerate() {
        let key = py_strip(name).to_lowercase().replace(" / ", "/");
        if key.is_empty() {
            continue;
        }
        let key = if cols.iter().any(|(k, _)| *k == key) { format!("close {key}") } else { key };
        match cols.iter_mut().find(|(k, _)| *k == key) {
            Some(col) => col.1 = i,
            None => cols.push((key, i)),
        }
    }
    cols
}

/// `re.match(r"\s*(\d+)(?:\s*\(([^)]*)\))?", text)`: the login and what's in the brackets.
fn account_line(text: &str) -> Option<(String, Option<String>)> {
    let s: Vec<char> = text.chars().collect();
    let mut p = 0;
    while p < s.len() && is_space(s[p]) {
        p += 1;
    }
    let start = p;
    while p < s.len() && decimal(s[p]).is_some() {
        p += 1;
    }
    if p == start {
        return None;
    }
    let login: String = s[start..p].iter().collect();
    while p < s.len() && is_space(s[p]) {
        p += 1;
    }
    let details = if s.get(p) == Some(&'(') {
        s[p + 1..].iter().position(|&c| c == ')').map(|n| s[p + 1..p + 1 + n].iter().collect())
    } else {
        None
    };
    Some((login, details))
}

// ---- numbers and times ---------------------------------------------------------------------------

/// `_num`: a number as the report writes it ("1 000.00", "-7.00"), "" as 0.
fn num(text: &str) -> Result<f64, Fail> {
    let t: String = text.chars().filter(|&c| c != '\u{a0}' && c != ' ' && c != ',').collect();
    if t.is_empty() {
        return Ok(0.0);
    }
    py_float(&t).ok_or_else(|| Fail::Report(format!("Couldn't read the number {} in the report", py_repr(text))))
}

/// `_time`: a report time as unix seconds, the server clock read as UTC.
fn time(text: &str) -> Result<i64, Fail> {
    let t: Vec<char> = py_strip(text).chars().collect();
    for format in [FORMAT_DOTS, FORMAT_DOTS_NO_SECONDS, FORMAT_DASHES] {
        if let Some(seconds) = strptime(&t, format) {
            return Ok(seconds);
        }
    }
    Err(Fail::Report(format!("Couldn't read the time {} in the report", py_repr(text))))
}

/// A piece of a `strptime` regex: a directive's alternatives (tried in order), a literal, `\s+`.
#[derive(Clone, Copy)]
enum Tok {
    Field(usize, &'static [&'static [Class]]),
    Lit(char),
    Spaces,
}

#[derive(Clone, Copy)]
enum Class {
    Is(char),
    Range(char, char),
    Digit, // \d: any Unicode decimal digit
}

impl Class {
    fn matches(self, c: char) -> bool {
        match self {
            Class::Is(x) => c == x,
            Class::Range(a, b) => (a..=b).contains(&c),
            Class::Digit => decimal(c).is_some(),
        }
    }
}

use Class::{Digit, Is, Range};
const Y: &[&[Class]] = &[&[Digit, Digit, Digit, Digit]];
const MO: &[&[Class]] = &[&[Is('1'), Range('0', '2')], &[Is('0'), Range('1', '9')], &[Range('1', '9')]];
const D: &[&[Class]] = &[
    &[Is('3'), Range('0', '1')], &[Range('1', '2'), Digit], &[Is('0'), Range('1', '9')], &[Range('1', '9')],
    &[Is(' '), Range('1', '9')],
];
const H: &[&[Class]] = &[&[Is('2'), Range('0', '3')], &[Range('0', '1'), Digit], &[Digit]];
const MI: &[&[Class]] = &[&[Range('0', '5'), Digit], &[Digit]];
const S: &[&[Class]] = &[&[Is('6'), Range('0', '1')], &[Range('0', '5'), Digit], &[Digit]];
const FORMAT_DOTS: &[Tok] = &[
    Tok::Field(0, Y), Tok::Lit('.'), Tok::Field(1, MO), Tok::Lit('.'), Tok::Field(2, D), Tok::Spaces,
    Tok::Field(3, H), Tok::Lit(':'), Tok::Field(4, MI), Tok::Lit(':'), Tok::Field(5, S),
];
const FORMAT_DOTS_NO_SECONDS: &[Tok] = &[
    Tok::Field(0, Y), Tok::Lit('.'), Tok::Field(1, MO), Tok::Lit('.'), Tok::Field(2, D), Tok::Spaces,
    Tok::Field(3, H), Tok::Lit(':'), Tok::Field(4, MI),
];
const FORMAT_DASHES: &[Tok] = &[
    Tok::Field(0, Y), Tok::Lit('-'), Tok::Field(1, MO), Tok::Lit('-'), Tok::Field(2, D), Tok::Spaces,
    Tok::Field(3, H), Tok::Lit(':'), Tok::Field(4, MI), Tok::Lit(':'), Tok::Field(5, S),
];

/// The regex's first match from the start (Python's `re.match`, backtracking in order), as the
/// end position and each field's text.
fn match_tokens(s: &[char], toks: &[Tok], at: usize, fields: &mut [(usize, usize); 6]) -> Option<usize> {
    let Some((tok, rest)) = toks.split_first() else { return Some(at) };
    match *tok {
        Tok::Lit(c) => (s.get(at) == Some(&c)).then_some(())?,
        Tok::Spaces => {
            let mut end = at;
            while end < s.len() && is_space(s[end]) {
                end += 1;
            }
            for stop in (at + 1..=end).rev() {
                if let Some(e) = match_tokens(s, rest, stop, fields) {
                    return Some(e);
                }
            }
            return None;
        }
        Tok::Field(slot, alternatives) => {
            for alt in alternatives {
                let fits = alt.len() <= s.len() - at && alt.iter().zip(&s[at..]).all(|(class, &c)| class.matches(c));
                if fits {
                    fields[slot] = (at, at + alt.len());
                    if let Some(e) = match_tokens(s, rest, at + alt.len(), fields) {
                        return Some(e);
                    }
                }
            }
            return None;
        }
    }
    match_tokens(s, rest, at + 1, fields)
}

/// `datetime.strptime(text, format)` read as UTC, `None` where Python raises `ValueError`.
fn strptime(s: &[char], format: &[Tok]) -> Option<i64> {
    let mut fields = [(0usize, 0usize); 6];
    if match_tokens(s, format, 0, &mut fields)? != s.len() {
        return None; // "unconverted data remains"
    }
    let has_seconds = format.len() == 11;
    let value = |slot: usize| -> i64 {
        let (a, b) = fields[slot];
        s[a..b].iter().filter_map(|&c| decimal(c)).fold(0, |n, d| n * 10 + d as i64)
    };
    let (year, month, day, hour, minute) = (value(0), value(1), value(2), value(3), value(4));
    let second = if has_seconds { value(5) } else { 0 };
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if !(1..=9999).contains(&year) || day > days_in_month[month as usize - 1] || second > 59 {
        return None;
    }
    Some(days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second)
}

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The text `int()`/`float()` parse: other whitespace as a space, Unicode digits as ASCII, any
/// other non-ASCII character (and DEL) as something neither accepts.
fn number_ascii(text: &str) -> String {
    text.chars()
        .map(|c| match c {
            c if (c as u32) < 127 => c,
            c if is_space(c) => ' ',
            c => decimal(c).map_or('?', |d| char::from(b'0' + d as u8)),
        })
        .collect()
}

fn ascii_space(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r')
}

/// Python's `int(text)` (base 10), saturated to `i64`.
fn py_int(text: &str) -> Option<i64> {
    let t = number_ascii(text);
    let t = t.trim_matches(ascii_space);
    let (negative, digits) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let b = digits.as_bytes();
    if b.is_empty() || !b[0].is_ascii_digit() {
        return None;
    }
    let mut n: i64 = 0;
    for (i, &c) in b.iter().enumerate() {
        match c {
            b'0'..=b'9' => n = n.saturating_mul(10).saturating_add((c - b'0') as i64),
            b'_' if b.get(i + 1).is_some_and(u8::is_ascii_digit) => {}
            _ => return None,
        }
    }
    Some(if negative { -n } else { n })
}

/// Python's `float(text)`: underscores between digits, `inf`/`infinity`/`nan` in any case.
fn py_float(text: &str) -> Option<f64> {
    let t = number_ascii(text);
    let b = t.as_bytes();
    for (i, &c) in b.iter().enumerate() {
        if c == b'_' && !(i > 0 && b[i - 1].is_ascii_digit() && b.get(i + 1).is_some_and(u8::is_ascii_digit)) {
            return None;
        }
    }
    let t: String = t.chars().filter(|&c| c != '_').collect();
    let t = t.trim_matches(ascii_space);
    let (sign, body) = match t.as_bytes().first() {
        Some(b'-') => (-1.0, &t[1..]),
        Some(b'+') => (1.0, &t[1..]),
        _ => (1.0, t),
    };
    match body.to_ascii_lowercase().as_str() {
        "inf" | "infinity" => return Some(sign * f64::INFINITY),
        "nan" => return Some(f64::NAN),
        _ => {}
    }
    let b = body.as_bytes();
    let digits = |mut i: usize| {
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        i
    };
    let int_end = digits(0);
    let (mut frac_start, mut frac_end) = (int_end, int_end);
    if b.get(int_end) == Some(&b'.') {
        frac_start = int_end + 1;
        frac_end = digits(frac_start);
    }
    if int_end == 0 && frac_end == frac_start {
        return None;
    }
    let mut end = frac_end;
    let mut exponent = "";
    if matches!(b.get(end), Some(b'e' | b'E')) {
        let mut e = end + 1;
        if matches!(b.get(e), Some(b'+' | b'-')) {
            e += 1;
        }
        let e_end = digits(e);
        if e_end == e {
            return None;
        }
        exponent = &body[end + 1..e_end];
        end = e_end;
    }
    if end != b.len() {
        return None;
    }
    let int_part = if int_end == 0 { "0" } else { &body[..int_end] };
    let frac = if frac_end > frac_start { &body[frac_start..frac_end] } else { "0" };
    let value: f64 = format!("{int_part}.{frac}e{}", if exponent.is_empty() { "0" } else { exponent }).parse().ok()?;
    Some(sign * value)
}

/// Python's `repr()` of a float (`str()` is the same): the shortest digits that read back,
/// exponent form below 1e-4 and from 1e16.
fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    // Rust's shortest digits break a tie between two (1404196400000000.25: .2 or .3) upwards;
    // Python takes the nearest, ties to even, which is that many digits rounded exactly.
    let shortest = format!("{:e}", x.abs());
    let count = shortest.split('e').next().unwrap_or("").chars().filter(char::is_ascii_digit).count();
    let sci = format!("{:.*e}", count.saturating_sub(1), x.abs());
    let (mantissa, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let exp: i32 = exp.parse().unwrap_or(0);
    let decpt = exp + 1;
    let sign = if x.is_sign_negative() { "-" } else { "" };
    let n = digits.len() as i32;
    let body = if decpt <= -4 || decpt > 16 {
        let rest = if n > 1 { format!(".{}", &digits[1..]) } else { String::new() };
        format!("{}{rest}e{}{:02}", &digits[..1], if exp < 0 { '-' } else { '+' }, exp.abs())
    } else if decpt <= 0 {
        format!("0.{}{digits}", "0".repeat((-decpt) as usize))
    } else if decpt >= n {
        format!("{digits}{}.0", "0".repeat((decpt - n) as usize))
    } else {
        format!("{}.{}", &digits[..decpt as usize], &digits[decpt as usize..])
    };
    format!("{sign}{body}")
}

/// Python's `repr()` of a string.
fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        let u = c as u32;
        match c {
            c if c == quote || c == '\\' => {
                out.push('\\');
                out.push(c);
            }
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if u < 0x20 || u == 0x7f => out.push_str(&format!("\\x{u:02x}")),
            _ if u < 0x7f => out.push(c),
            _ if !in_ranges(NON_PRINTABLE, u) => out.push(c),
            _ if u <= 0xff => out.push_str(&format!("\\x{u:02x}")),
            _ if u <= 0xffff => out.push_str(&format!("\\u{u:04x}")),
            _ => out.push_str(&format!("\\U{u:08x}")),
        }
    }
    out.push(quote);
    out
}

// ---- text ----------------------------------------------------------------------------------------

/// `str.isspace()`, which `str.split()`, `str.strip()` and the regex `\s` go by.
fn is_space(c: char) -> bool {
    matches!(c, '\t'..='\r' | '\x1c'..=' ' | '\u{85}' | '\u{a0}' | '\u{1680}' | '\u{2000}'..='\u{200a}'
        | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}')
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `" ".join(text.split())`.
fn collapse(s: &str) -> String {
    s.split(is_space).filter(|w| !w.is_empty()).collect::<Vec<_>>().join(" ")
}

/// The value of a Unicode decimal digit (`str.isdecimal()`, regex `\d`).
fn decimal(c: char) -> Option<u32> {
    let u = c as u32;
    if c.is_ascii_digit() {
        return Some(u - '0' as u32);
    }
    let i = DECIMAL.partition_point(|&(_, end)| end < u);
    DECIMAL.get(i).filter(|&&(start, _)| start <= u).map(|&(start, _)| (u - start) % 10)
}

fn in_ranges(ranges: &[(u32, u32)], u: u32) -> bool {
    let i = ranges.partition_point(|&(_, end)| end < u);
    ranges.get(i).is_some_and(|&(start, _)| start <= u)
}

fn contains_ascii_ci(s: &[char], needle: &str) -> bool {
    let n: Vec<char> = needle.chars().collect();
    s.windows(n.len()).any(|w| w.iter().zip(&n).all(|(a, b)| a.to_ascii_lowercase() == *b))
}

/// `_decode`: the text of the file, `None` where Python's strict UTF-16 decoding fails.
fn decode(data: &[u8]) -> Option<String> {
    let utf16 = |bytes: &[u8], big: bool| -> Option<String> {
        if bytes.len() % 2 != 0 {
            return None;
        }
        let units = bytes.chunks(2).map(|p| if big { u16::from_be_bytes([p[0], p[1]]) } else { u16::from_le_bytes([p[0], p[1]]) });
        char::decode_utf16(units).collect::<Result<String, _>>().ok()
    };
    if data.starts_with(&[0xff, 0xfe]) || data.starts_with(&[0xfe, 0xff]) {
        return utf16(&data[2..], data[0] == 0xfe);
    }
    if data.len() > 1 && data[1] == 0 {
        return utf16(data, false);
    }
    let body = data.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(data);
    Some(String::from_utf8_lossy(body).into_owned())
}

// ---- the HTML: `_Rows` on Python's `HTMLParser` -----------------------------------------------

/// Every table row as its cell texts, colspans expanded, hidden cells dropped (`_Rows`). `Err`
/// where Python's parser raises (`_markupbase` on a `<![` it doesn't know).
fn table_rows(s: &[char]) -> Result<Vec<Vec<String>>, ()> {
    let mut rows = Rows::default();
    rows.feed(s)?;
    Ok(rows.rows)
}

#[derive(Default)]
struct Rows {
    rows: Vec<Vec<String>>,
    row: Option<Vec<String>>,
    cell: Option<String>,
    span: usize,
    hidden: bool,
    /// `script` or `style` while inside one (`cdata_elem`)
    cdata: Option<String>,
}

type Attrs = Vec<(String, Option<String>)>;

impl Rows {
    fn start(&mut self, tag: &str, attrs: &Attrs) {
        if tag == "tr" {
            self.row = Some(Vec::new());
        } else if (tag == "td" || tag == "th") && self.row.is_some() {
            self.cell = Some(String::new());
            // dict(attrs): the last of a repeated attribute wins
            let attr = |name: &str| attrs.iter().rev().find(|(n, _)| n == name).and_then(|(_, v)| v.as_deref());
            self.hidden = attr("class").is_some_and(|c| c.split(is_space).any(|w| w == "hidden"));
            self.span = match attr("colspan") {
                None | Some("") => 1,
                Some(v) => py_int(v).map_or(1, |n| n.clamp(1, MAX_SPAN) as usize),
            };
        }
    }

    fn end(&mut self, tag: &str) {
        if tag == "td" || tag == "th" {
            if let (Some(cell), Some(row)) = (&self.cell, &mut self.row) {
                if !self.hidden {
                    row.push(collapse(cell));
                    row.extend(std::iter::repeat(String::new()).take(self.span - 1));
                }
                self.cell = None;
            }
        } else if tag == "tr" {
            if let Some(row) = self.row.take() {
                if row.iter().any(|c| !c.is_empty()) {
                    self.rows.push(row);
                }
            }
        }
    }

    fn data(&mut self, text: &str) {
        if let Some(cell) = &mut self.cell {
            cell.push_str(text);
        }
    }

    /// `HTMLParser.goahead(0)`: the whole text fed once and never closed, so whatever is left
    /// unfinished at a `break` is dropped.
    fn feed(&mut self, s: &[char]) -> Result<(), ()> {
        let n = s.len();
        let mut i = 0;
        while i < n {
            let j = match &self.cdata {
                None => match find(s, '<', i) {
                    Some(j) => j,
                    None => {
                        // text at the end that may hold half an entity waits for more
                        let from = i.max(n.saturating_sub(34));
                        if let Some(amp) = s[from..n].iter().rposition(|&c| c == '&').map(|p| p + from) {
                            if !s[amp..].iter().any(|&c| is_space(c) || c == ';') {
                                break;
                            }
                        }
                        n
                    }
                },
                Some(elem) => match find_cdata_end(s, i, elem) {
                    Some(j) => j,
                    None => break,
                },
            };
            if i < j {
                let text = if self.cdata.is_none() { unescape(&s[i..j]) } else { s[i..j].iter().collect() };
                self.data(&text);
            }
            i = j;
            if i == n {
                break;
            }
            let next = s.get(i + 1).copied();
            let k = if next.is_some_and(|c| c.is_ascii_alphabetic()) {
                self.parse_starttag(s, i)
            } else if next == Some('/') {
                self.parse_endtag(s, i)
            } else if starts(s, i, "<!--") {
                parse_comment(s, i)
            } else if next == Some('?') {
                find(s, '>', i + 2).map(|p| p + 1)
            } else if next == Some('!') {
                parse_html_declaration(s, i)?
            } else if i + 1 < n {
                self.data("<");
                Some(i + 1)
            } else {
                break;
            };
            match k {
                Some(k) => i = k,
                None => break,
            }
        }
        Ok(())
    }

    fn parse_starttag(&mut self, s: &[char], i: usize) -> Option<usize> {
        let endpos = check_for_whole_start_tag(s, i)?;
        let (name_end, mut k) = tagfind(s, i + 1);
        let tag = lower(&s[i + 1..name_end]);
        let mut attrs: Attrs = Vec::new();
        while k < endpos {
            let Some((name, value, end)) = attrfind(s, k) else { break };
            let value = value.map(|(a, b)| {
                let mut v = &s[a..b];
                if v.len() >= 2 && (v[0] == '\'' || v[0] == '"') && v[v.len() - 1] == v[0] {
                    v = &v[1..v.len() - 1];
                }
                unescape(v)
            });
            attrs.push((lower(&s[name.0..name.1]), value));
            k = end;
        }
        let rest: String = if k < endpos { s[k..endpos].iter().collect() } else { String::new() };
        let rest = py_strip(&rest);
        if rest != ">" && rest != "/>" {
            let text: String = s[i..endpos].iter().collect();
            self.data(&text);
            return Some(endpos);
        }
        self.start(&tag, &attrs);
        if rest == "/>" {
            self.end(&tag);
        } else if tag == "script" || tag == "style" {
            self.cdata = Some(tag);
        }
        Some(endpos)
    }

    fn parse_endtag(&mut self, s: &[char], i: usize) -> Option<usize> {
        let gtpos = find(s, '>', i + 1)? + 1;
        if let Some((a, b)) = endtagfind(s, i) {
            let elem = lower(&s[a..b]);
            if self.cdata.as_ref().is_some_and(|c| *c != elem) {
                let text: String = s[i..gtpos].iter().collect();
                self.data(&text);
                return Some(gtpos);
            }
            self.end(&elem);
            self.cdata = None;
            return Some(gtpos);
        }
        if self.cdata.is_some() {
            let text: String = s[i..gtpos].iter().collect();
            self.data(&text);
            return Some(gtpos);
        }
        if s.get(i + 2).is_some_and(|c| c.is_ascii_alphabetic()) {
            let (name_end, after) = tagfind(s, i + 2);
            let tag = lower(&s[i + 2..name_end]);
            let gt = find(s, '>', after)?;
            self.end(&tag);
            return Some(gt + 1);
        }
        if starts(s, i, "</>") {
            return Some(i + 3);
        }
        find(s, '>', i + 2).map(|p| p + 1) // bogus comment
    }
}

fn find(s: &[char], c: char, from: usize) -> Option<usize> {
    s.get(from..)?.iter().position(|&x| x == c).map(|p| p + from)
}

fn starts(s: &[char], i: usize, prefix: &str) -> bool {
    (i..).zip(prefix.chars()).all(|(k, c)| s.get(k) == Some(&c))
}

fn lower(s: &[char]) -> String {
    s.iter().collect::<String>().to_lowercase()
}

fn skip_spaces(s: &[char], mut i: usize) -> usize {
    while i < s.len() && is_space(s[i]) {
        i += 1;
    }
    i
}

/// `(?:\s|/(?!>))*`
fn trail(s: &[char], mut i: usize) -> usize {
    while i < s.len() && (is_space(s[i]) || (s[i] == '/' && s.get(i + 1) != Some(&'>'))) {
        i += 1;
    }
    i
}

/// `[^\t\n\r\f />\x00]*`, the rest of a tag name.
fn tag_name_end(s: &[char], mut i: usize) -> usize {
    while i < s.len() && !matches!(s[i], '\t' | '\n' | '\r' | '\x0c' | ' ' | '/' | '>' | '\0') {
        i += 1;
    }
    i
}

/// `tagfind_tolerant` at a letter: where the name ends and where the match does.
fn tagfind(s: &[char], i: usize) -> (usize, usize) {
    let name_end = tag_name_end(s, i + 1);
    (name_end, trail(s, name_end))
}

/// `(?<=['"\s/])[^\s/>][^\s/=>]*`: an attribute name at `i`, as where it ends.
fn attr_name(s: &[char], i: usize) -> Option<usize> {
    let before = *s.get(i.checked_sub(1)?)?;
    if !(before == '\'' || before == '"' || before == '/' || is_space(before)) {
        return None;
    }
    let first = *s.get(i)?;
    if is_space(first) || first == '/' || first == '>' {
        return None;
    }
    let mut k = i + 1;
    while k < s.len() && !(is_space(s[k]) || matches!(s[k], '/' | '=' | '>')) {
        k += 1;
    }
    Some(k)
}

/// `[^>\s]*`
fn bare_end(s: &[char], mut i: usize) -> usize {
    while i < s.len() && s[i] != '>' && !is_space(s[i]) {
        i += 1;
    }
    i
}

/// `\s*=+\s*('[^']*'|"[^"]*"|(?!['"])[^>\s]*)` at `i` as the regex resolves it: the value's span
/// and the match end. An unclosed quote backs off to an empty value before it, or to a bare value
/// from the last `=` when the quote follows `==`.
fn attr_value(s: &[char], i: usize) -> Option<(usize, usize, usize)> {
    let eq = skip_spaces(s, i);
    if s.get(eq) != Some(&'=') {
        return None;
    }
    let mut eq_end = eq;
    while s.get(eq_end) == Some(&'=') {
        eq_end += 1;
    }
    let v = skip_spaces(s, eq_end);
    match s.get(v) {
        Some(&q) if q == '\'' || q == '"' => {
            if let Some(close) = find(s, q, v + 1) {
                Some((v, close + 1, close + 1))
            } else if v > eq_end {
                Some((v - 1, v - 1, v - 1))
            } else if eq_end - eq >= 2 {
                let end = bare_end(s, eq_end - 1);
                Some((eq_end - 1, end, end))
            } else {
                None
            }
        }
        _ => {
            let end = bare_end(s, v);
            Some((v, end, end))
        }
    }
}

/// `attrfind_tolerant` at `i`: the name's span, the value's span if it has one, the match end.
#[allow(clippy::type_complexity)]
fn attrfind(s: &[char], i: usize) -> Option<((usize, usize), Option<(usize, usize)>, usize)> {
    let name_end = attr_name(s, i)?;
    let value = attr_value(s, name_end);
    let end = trail(s, value.map_or(name_end, |v| v.2));
    Some(((i, name_end), value.map(|v| (v.0, v.1)), end))
}

/// `locatestarttagend_tolerant`'s match end, from the `<` of a tag.
fn locate_starttag_end(s: &[char], i: usize) -> usize {
    let mut k = tag_name_end(s, i + 2);
    while k < s.len() && (is_space(s[k]) || s[k] == '/') {
        k += 1;
    }
    while let Some(name_end) = attr_name(s, k) {
        k = name_end;
        if let Some((_, _, end)) = attr_value(s, k) {
            k = skip_spaces(s, end);
        }
        k = trail(s, k);
    }
    skip_spaces(s, k)
}

/// `check_for_whole_start_tag`: where the start tag ends, `None` if it doesn't (in Python, the
/// parser then waits for more text, which never comes).
fn check_for_whole_start_tag(s: &[char], i: usize) -> Option<usize> {
    let j = locate_starttag_end(s, i);
    match s.get(j) {
        Some('>') => Some(j + 1),
        Some('/') => (s.get(j + 1) == Some(&'>')).then_some(j + 2),
        None => None,
        Some(c) if c.is_ascii_alphabetic() || *c == '=' => None,
        Some(_) => Some(j),
    }
}

/// `endtagfind` (`</\s*([a-zA-Z][-.a-zA-Z0-9:_]*)\s*>`) at `i`: the name's span.
fn endtagfind(s: &[char], i: usize) -> Option<(usize, usize)> {
    let a = skip_spaces(s, i + 2);
    if !s.get(a)?.is_ascii_alphabetic() {
        return None;
    }
    let mut b = a + 1;
    while b < s.len() && (s[b].is_ascii_alphanumeric() || matches!(s[b], '-' | '.' | ':' | '_')) {
        b += 1;
    }
    (s.get(skip_spaces(s, b)) == Some(&'>')).then_some((a, b))
}

/// Where `</\s*script\s*>` (or style) starts, matched as `re.I` does: `ſ` is an `s`, `İ` and `ı`
/// are an `i`.
fn find_cdata_end(s: &[char], from: usize, elem: &str) -> Option<usize> {
    let same = |c: char, l: char| {
        c.to_ascii_lowercase() == l || (l == 's' && c == '\u{17f}') || (l == 'i' && matches!(c, '\u{130}' | '\u{131}'))
    };
    (from..s.len()).find(|&p| {
        if s[p] != '<' || s.get(p + 1) != Some(&'/') {
            return false;
        }
        let mut k = skip_spaces(s, p + 2);
        for l in elem.chars() {
            match s.get(k) {
                Some(&c) if same(c, l) => k += 1,
                _ => return false,
            }
        }
        s.get(skip_spaces(s, k)) == Some(&'>')
    })
}

/// `<!--` to the first `--\s*>`.
fn parse_comment(s: &[char], i: usize) -> Option<usize> {
    (i + 4..s.len()).find_map(|p| {
        if s[p] == '-' && s.get(p + 1) == Some(&'-') {
            let k = skip_spaces(s, p + 2);
            (s.get(k) == Some(&'>')).then_some(k + 1)
        } else {
            None
        }
    })
}

/// `parse_html_declaration`: `<![...]>` sections, `<!doctype ...>`, other `<!...>` skipped.
fn parse_html_declaration(s: &[char], i: usize) -> Result<Option<usize>, ()> {
    if starts(s, i, "<![") {
        return parse_marked_section(s, i);
    }
    let doctype: Vec<char> = "<!doctype".chars().collect();
    if s.len() >= i + 9 && s[i..i + 9].iter().zip(&doctype).all(|(a, b)| a.to_ascii_lowercase() == *b) {
        return Ok(find(s, '>', i + 9).map(|p| p + 1));
    }
    Ok(find(s, '>', i + 2).map(|p| p + 1))
}

/// `_markupbase.parse_marked_section`: `Err` where it raises `AssertionError`.
fn parse_marked_section(s: &[char], i: usize) -> Result<Option<usize>, ()> {
    let start = i + 3;
    if start == s.len() {
        return Ok(None);
    }
    // _scan_name: [a-zA-Z][-_.a-zA-Z0-9]*\s*
    if !s[start].is_ascii_alphabetic() {
        return Err(());
    }
    let mut k = start + 1;
    while k < s.len() && (s[k].is_ascii_alphanumeric() || matches!(s[k], '-' | '_' | '.')) {
        k += 1;
    }
    let name = lower(&s[start..k]);
    if skip_spaces(s, k) == s.len() {
        return Ok(None);
    }
    let double = match name.as_str() {
        "temp" | "cdata" | "ignore" | "include" | "rcdata" => true,
        "if" | "else" | "endif" => false,
        _ => return Err(()),
    };
    // ]\s*]\s*> or ]\s*>, searched from after "<!["
    Ok((start..s.len()).find_map(|p| {
        if s[p] != ']' {
            return None;
        }
        let mut k = skip_spaces(s, p + 1);
        if double {
            if s.get(k) != Some(&']') {
                return None;
            }
            k = skip_spaces(s, k + 1);
        }
        (s.get(k) == Some(&'>')).then_some(k + 1)
    }))
}

/// `html.unescape`.
fn unescape(s: &[char]) -> String {
    let mut out = String::new();
    let mut p = 0;
    while p < s.len() {
        if s[p] == '&' {
            if let Some((end, text)) = charref(s, p) {
                out.push_str(&text);
                p = end;
                continue;
            }
        }
        out.push(s[p]);
        p += 1;
    }
    out
}

/// `&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)` at `p` and its replacement.
fn charref(s: &[char], p: usize) -> Option<(usize, String)> {
    let semi = |k: usize| if s.get(k) == Some(&';') { k + 1 } else { k };
    if s.get(p + 1) == Some(&'#') {
        let (start, radix) = match s.get(p + 2) {
            Some(&('x' | 'X')) if s.get(p + 3).is_some_and(|c| c.is_ascii_hexdigit()) => (p + 3, 16),
            Some(c) if c.is_ascii_digit() => (p + 2, 10),
            _ => return None,
        };
        let mut k = start;
        let mut num: u32 = 0;
        while k < s.len() && s[k].is_digit(radix) {
            num = num.saturating_mul(radix).saturating_add(s[k].to_digit(radix).unwrap_or(0));
            k += 1;
        }
        return Some((semi(k), numeric_charref(num)));
    }
    let mut k = p + 1;
    while k < s.len() && k - (p + 1) < 32 && !matches!(s[k], '\t' | '\n' | '\x0c' | ' ' | '<' | '&' | '#' | ';') {
        k += 1;
    }
    if k == p + 1 {
        return None;
    }
    let end = semi(k);
    let name: String = s[p + 1..end].iter().collect();
    if let Some(v) = entity(&name) {
        return Some((end, v.to_string()));
    }
    let chars: Vec<char> = name.chars().collect();
    for x in (2..chars.len()).rev() {
        let prefix: String = chars[..x].iter().collect();
        if let Some(v) = entity(&prefix) {
            return Some((end, format!("{v}{}", chars[x..].iter().collect::<String>())));
        }
    }
    Some((end, format!("&{name}")))
}

fn numeric_charref(num: u32) -> String {
    const WINDOWS_1252: [char; 32] = [
        '\u{20ac}', '\u{81}', '\u{201a}', '\u{192}', '\u{201e}', '\u{2026}', '\u{2020}', '\u{2021}', '\u{2c6}',
        '\u{2030}', '\u{160}', '\u{2039}', '\u{152}', '\u{8d}', '\u{17d}', '\u{8f}', '\u{90}', '\u{2018}',
        '\u{2019}', '\u{201c}', '\u{201d}', '\u{2022}', '\u{2013}', '\u{2014}', '\u{2dc}', '\u{2122}', '\u{161}',
        '\u{203a}', '\u{153}', '\u{9d}', '\u{17e}', '\u{178}',
    ];
    match num {
        0 => "\u{fffd}".into(),
        0x0d => "\r".into(),
        0x80..=0x9f => WINDOWS_1252[(num - 0x80) as usize].to_string(),
        0xd800..=0xdfff => "\u{fffd}".into(),
        n if n > 0x10ffff => "\u{fffd}".into(),
        0x01..=0x08 | 0x0b | 0x0e..=0x1f | 0x7f | 0xfdd0..=0xfdef => String::new(),
        n if n & 0xfffe == 0xfffe => String::new(),
        n => char::from_u32(n).map(String::from).unwrap_or_default(),
    }
}

fn entity(name: &str) -> Option<&'static str> {
    ENTITIES.binary_search_by(|(k, _)| (*k).cmp(name)).ok().map(|i| ENTITIES[i].1)
}

// ---- tables, from Python 3.11 (Unicode 14.0): see tests/test_report_golden.py -------------------

/// Code points `str.isprintable()` is false for (`repr()` escapes them).
#[rustfmt::skip]
const NON_PRINTABLE: &[(u32, u32)] = &[
    (0x0, 0x1f), (0x7f, 0xa0), (0xad, 0xad), (0x378, 0x379), (0x380, 0x383), (0x38b, 0x38b),
    (0x38d, 0x38d), (0x3a2, 0x3a2), (0x530, 0x530), (0x557, 0x558), (0x58b, 0x58c), (0x590, 0x590),
    (0x5c8, 0x5cf), (0x5eb, 0x5ee), (0x5f5, 0x605), (0x61c, 0x61c), (0x6dd, 0x6dd), (0x70e, 0x70f),
    (0x74b, 0x74c), (0x7b2, 0x7bf), (0x7fb, 0x7fc), (0x82e, 0x82f), (0x83f, 0x83f), (0x85c, 0x85d),
    (0x85f, 0x85f), (0x86b, 0x86f), (0x88f, 0x897), (0x8e2, 0x8e2), (0x984, 0x984), (0x98d, 0x98e),
    (0x991, 0x992), (0x9a9, 0x9a9), (0x9b1, 0x9b1), (0x9b3, 0x9b5), (0x9ba, 0x9bb), (0x9c5, 0x9c6),
    (0x9c9, 0x9ca), (0x9cf, 0x9d6), (0x9d8, 0x9db), (0x9de, 0x9de), (0x9e4, 0x9e5), (0x9ff, 0xa00),
    (0xa04, 0xa04), (0xa0b, 0xa0e), (0xa11, 0xa12), (0xa29, 0xa29), (0xa31, 0xa31), (0xa34, 0xa34),
    (0xa37, 0xa37), (0xa3a, 0xa3b), (0xa3d, 0xa3d), (0xa43, 0xa46), (0xa49, 0xa4a), (0xa4e, 0xa50),
    (0xa52, 0xa58), (0xa5d, 0xa5d), (0xa5f, 0xa65), (0xa77, 0xa80), (0xa84, 0xa84), (0xa8e, 0xa8e),
    (0xa92, 0xa92), (0xaa9, 0xaa9), (0xab1, 0xab1), (0xab4, 0xab4), (0xaba, 0xabb), (0xac6, 0xac6),
    (0xaca, 0xaca), (0xace, 0xacf), (0xad1, 0xadf), (0xae4, 0xae5), (0xaf2, 0xaf8), (0xb00, 0xb00),
    (0xb04, 0xb04), (0xb0d, 0xb0e), (0xb11, 0xb12), (0xb29, 0xb29), (0xb31, 0xb31), (0xb34, 0xb34),
    (0xb3a, 0xb3b), (0xb45, 0xb46), (0xb49, 0xb4a), (0xb4e, 0xb54), (0xb58, 0xb5b), (0xb5e, 0xb5e),
    (0xb64, 0xb65), (0xb78, 0xb81), (0xb84, 0xb84), (0xb8b, 0xb8d), (0xb91, 0xb91), (0xb96, 0xb98),
    (0xb9b, 0xb9b), (0xb9d, 0xb9d), (0xba0, 0xba2), (0xba5, 0xba7), (0xbab, 0xbad), (0xbba, 0xbbd),
    (0xbc3, 0xbc5), (0xbc9, 0xbc9), (0xbce, 0xbcf), (0xbd1, 0xbd6), (0xbd8, 0xbe5), (0xbfb, 0xbff),
    (0xc0d, 0xc0d), (0xc11, 0xc11), (0xc29, 0xc29), (0xc3a, 0xc3b), (0xc45, 0xc45), (0xc49, 0xc49),
    (0xc4e, 0xc54), (0xc57, 0xc57), (0xc5b, 0xc5c), (0xc5e, 0xc5f), (0xc64, 0xc65), (0xc70, 0xc76),
    (0xc8d, 0xc8d), (0xc91, 0xc91), (0xca9, 0xca9), (0xcb4, 0xcb4), (0xcba, 0xcbb), (0xcc5, 0xcc5),
    (0xcc9, 0xcc9), (0xcce, 0xcd4), (0xcd7, 0xcdc), (0xcdf, 0xcdf), (0xce4, 0xce5), (0xcf0, 0xcf0),
    (0xcf3, 0xcff), (0xd0d, 0xd0d), (0xd11, 0xd11), (0xd45, 0xd45), (0xd49, 0xd49), (0xd50, 0xd53),
    (0xd64, 0xd65), (0xd80, 0xd80), (0xd84, 0xd84), (0xd97, 0xd99), (0xdb2, 0xdb2), (0xdbc, 0xdbc),
    (0xdbe, 0xdbf), (0xdc7, 0xdc9), (0xdcb, 0xdce), (0xdd5, 0xdd5), (0xdd7, 0xdd7), (0xde0, 0xde5),
    (0xdf0, 0xdf1), (0xdf5, 0xe00), (0xe3b, 0xe3e), (0xe5c, 0xe80), (0xe83, 0xe83), (0xe85, 0xe85),
    (0xe8b, 0xe8b), (0xea4, 0xea4), (0xea6, 0xea6), (0xebe, 0xebf), (0xec5, 0xec5), (0xec7, 0xec7),
    (0xece, 0xecf), (0xeda, 0xedb), (0xee0, 0xeff), (0xf48, 0xf48), (0xf6d, 0xf70), (0xf98, 0xf98),
    (0xfbd, 0xfbd), (0xfcd, 0xfcd), (0xfdb, 0xfff), (0x10c6, 0x10c6), (0x10c8, 0x10cc), (0x10ce, 0x10cf),
    (0x1249, 0x1249), (0x124e, 0x124f), (0x1257, 0x1257), (0x1259, 0x1259), (0x125e, 0x125f), (0x1289, 0x1289),
    (0x128e, 0x128f), (0x12b1, 0x12b1), (0x12b6, 0x12b7), (0x12bf, 0x12bf), (0x12c1, 0x12c1), (0x12c6, 0x12c7),
    (0x12d7, 0x12d7), (0x1311, 0x1311), (0x1316, 0x1317), (0x135b, 0x135c), (0x137d, 0x137f), (0x139a, 0x139f),
    (0x13f6, 0x13f7), (0x13fe, 0x13ff), (0x1680, 0x1680), (0x169d, 0x169f), (0x16f9, 0x16ff), (0x1716, 0x171e),
    (0x1737, 0x173f), (0x1754, 0x175f), (0x176d, 0x176d), (0x1771, 0x1771), (0x1774, 0x177f), (0x17de, 0x17df),
    (0x17ea, 0x17ef), (0x17fa, 0x17ff), (0x180e, 0x180e), (0x181a, 0x181f), (0x1879, 0x187f), (0x18ab, 0x18af),
    (0x18f6, 0x18ff), (0x191f, 0x191f), (0x192c, 0x192f), (0x193c, 0x193f), (0x1941, 0x1943), (0x196e, 0x196f),
    (0x1975, 0x197f), (0x19ac, 0x19af), (0x19ca, 0x19cf), (0x19db, 0x19dd), (0x1a1c, 0x1a1d), (0x1a5f, 0x1a5f),
    (0x1a7d, 0x1a7e), (0x1a8a, 0x1a8f), (0x1a9a, 0x1a9f), (0x1aae, 0x1aaf), (0x1acf, 0x1aff), (0x1b4d, 0x1b4f),
    (0x1b7f, 0x1b7f), (0x1bf4, 0x1bfb), (0x1c38, 0x1c3a), (0x1c4a, 0x1c4c), (0x1c89, 0x1c8f), (0x1cbb, 0x1cbc),
    (0x1cc8, 0x1ccf), (0x1cfb, 0x1cff), (0x1f16, 0x1f17), (0x1f1e, 0x1f1f), (0x1f46, 0x1f47), (0x1f4e, 0x1f4f),
    (0x1f58, 0x1f58), (0x1f5a, 0x1f5a), (0x1f5c, 0x1f5c), (0x1f5e, 0x1f5e), (0x1f7e, 0x1f7f), (0x1fb5, 0x1fb5),
    (0x1fc5, 0x1fc5), (0x1fd4, 0x1fd5), (0x1fdc, 0x1fdc), (0x1ff0, 0x1ff1), (0x1ff5, 0x1ff5), (0x1fff, 0x200f),
    (0x2028, 0x202f), (0x205f, 0x206f), (0x2072, 0x2073), (0x208f, 0x208f), (0x209d, 0x209f), (0x20c1, 0x20cf),
    (0x20f1, 0x20ff), (0x218c, 0x218f), (0x2427, 0x243f), (0x244b, 0x245f), (0x2b74, 0x2b75), (0x2b96, 0x2b96),
    (0x2cf4, 0x2cf8), (0x2d26, 0x2d26), (0x2d28, 0x2d2c), (0x2d2e, 0x2d2f), (0x2d68, 0x2d6e), (0x2d71, 0x2d7e),
    (0x2d97, 0x2d9f), (0x2da7, 0x2da7), (0x2daf, 0x2daf), (0x2db7, 0x2db7), (0x2dbf, 0x2dbf), (0x2dc7, 0x2dc7),
    (0x2dcf, 0x2dcf), (0x2dd7, 0x2dd7), (0x2ddf, 0x2ddf), (0x2e5e, 0x2e7f), (0x2e9a, 0x2e9a), (0x2ef4, 0x2eff),
    (0x2fd6, 0x2fef), (0x2ffc, 0x3000), (0x3040, 0x3040), (0x3097, 0x3098), (0x3100, 0x3104), (0x3130, 0x3130),
    (0x318f, 0x318f), (0x31e4, 0x31ef), (0x321f, 0x321f), (0xa48d, 0xa48f), (0xa4c7, 0xa4cf), (0xa62c, 0xa63f),
    (0xa6f8, 0xa6ff), (0xa7cb, 0xa7cf), (0xa7d2, 0xa7d2), (0xa7d4, 0xa7d4), (0xa7da, 0xa7f1), (0xa82d, 0xa82f),
    (0xa83a, 0xa83f), (0xa878, 0xa87f), (0xa8c6, 0xa8cd), (0xa8da, 0xa8df), (0xa954, 0xa95e), (0xa97d, 0xa97f),
    (0xa9ce, 0xa9ce), (0xa9da, 0xa9dd), (0xa9ff, 0xa9ff), (0xaa37, 0xaa3f), (0xaa4e, 0xaa4f), (0xaa5a, 0xaa5b),
    (0xaac3, 0xaada), (0xaaf7, 0xab00), (0xab07, 0xab08), (0xab0f, 0xab10), (0xab17, 0xab1f), (0xab27, 0xab27),
    (0xab2f, 0xab2f), (0xab6c, 0xab6f), (0xabee, 0xabef), (0xabfa, 0xabff), (0xd7a4, 0xd7af), (0xd7c7, 0xd7ca),
    (0xd7fc, 0xf8ff), (0xfa6e, 0xfa6f), (0xfada, 0xfaff), (0xfb07, 0xfb12), (0xfb18, 0xfb1c), (0xfb37, 0xfb37),
    (0xfb3d, 0xfb3d), (0xfb3f, 0xfb3f), (0xfb42, 0xfb42), (0xfb45, 0xfb45), (0xfbc3, 0xfbd2), (0xfd90, 0xfd91),
    (0xfdc8, 0xfdce), (0xfdd0, 0xfdef), (0xfe1a, 0xfe1f), (0xfe53, 0xfe53), (0xfe67, 0xfe67), (0xfe6c, 0xfe6f),
    (0xfe75, 0xfe75), (0xfefd, 0xff00), (0xffbf, 0xffc1), (0xffc8, 0xffc9), (0xffd0, 0xffd1), (0xffd8, 0xffd9),
    (0xffdd, 0xffdf), (0xffe7, 0xffe7), (0xffef, 0xfffb), (0xfffe, 0xffff), (0x1000c, 0x1000c), (0x10027, 0x10027),
    (0x1003b, 0x1003b), (0x1003e, 0x1003e), (0x1004e, 0x1004f), (0x1005e, 0x1007f), (0x100fb, 0x100ff), (0x10103, 0x10106),
    (0x10134, 0x10136), (0x1018f, 0x1018f), (0x1019d, 0x1019f), (0x101a1, 0x101cf), (0x101fe, 0x1027f), (0x1029d, 0x1029f),
    (0x102d1, 0x102df), (0x102fc, 0x102ff), (0x10324, 0x1032c), (0x1034b, 0x1034f), (0x1037b, 0x1037f), (0x1039e, 0x1039e),
    (0x103c4, 0x103c7), (0x103d6, 0x103ff), (0x1049e, 0x1049f), (0x104aa, 0x104af), (0x104d4, 0x104d7), (0x104fc, 0x104ff),
    (0x10528, 0x1052f), (0x10564, 0x1056e), (0x1057b, 0x1057b), (0x1058b, 0x1058b), (0x10593, 0x10593), (0x10596, 0x10596),
    (0x105a2, 0x105a2), (0x105b2, 0x105b2), (0x105ba, 0x105ba), (0x105bd, 0x105ff), (0x10737, 0x1073f), (0x10756, 0x1075f),
    (0x10768, 0x1077f), (0x10786, 0x10786), (0x107b1, 0x107b1), (0x107bb, 0x107ff), (0x10806, 0x10807), (0x10809, 0x10809),
    (0x10836, 0x10836), (0x10839, 0x1083b), (0x1083d, 0x1083e), (0x10856, 0x10856), (0x1089f, 0x108a6), (0x108b0, 0x108df),
    (0x108f3, 0x108f3), (0x108f6, 0x108fa), (0x1091c, 0x1091e), (0x1093a, 0x1093e), (0x10940, 0x1097f), (0x109b8, 0x109bb),
    (0x109d0, 0x109d1), (0x10a04, 0x10a04), (0x10a07, 0x10a0b), (0x10a14, 0x10a14), (0x10a18, 0x10a18), (0x10a36, 0x10a37),
    (0x10a3b, 0x10a3e), (0x10a49, 0x10a4f), (0x10a59, 0x10a5f), (0x10aa0, 0x10abf), (0x10ae7, 0x10aea), (0x10af7, 0x10aff),
    (0x10b36, 0x10b38), (0x10b56, 0x10b57), (0x10b73, 0x10b77), (0x10b92, 0x10b98), (0x10b9d, 0x10ba8), (0x10bb0, 0x10bff),
    (0x10c49, 0x10c7f), (0x10cb3, 0x10cbf), (0x10cf3, 0x10cf9), (0x10d28, 0x10d2f), (0x10d3a, 0x10e5f), (0x10e7f, 0x10e7f),
    (0x10eaa, 0x10eaa), (0x10eae, 0x10eaf), (0x10eb2, 0x10eff), (0x10f28, 0x10f2f), (0x10f5a, 0x10f6f), (0x10f8a, 0x10faf),
    (0x10fcc, 0x10fdf), (0x10ff7, 0x10fff), (0x1104e, 0x11051), (0x11076, 0x1107e), (0x110bd, 0x110bd), (0x110c3, 0x110cf),
    (0x110e9, 0x110ef), (0x110fa, 0x110ff), (0x11135, 0x11135), (0x11148, 0x1114f), (0x11177, 0x1117f), (0x111e0, 0x111e0),
    (0x111f5, 0x111ff), (0x11212, 0x11212), (0x1123f, 0x1127f), (0x11287, 0x11287), (0x11289, 0x11289), (0x1128e, 0x1128e),
    (0x1129e, 0x1129e), (0x112aa, 0x112af), (0x112eb, 0x112ef), (0x112fa, 0x112ff), (0x11304, 0x11304), (0x1130d, 0x1130e),
    (0x11311, 0x11312), (0x11329, 0x11329), (0x11331, 0x11331), (0x11334, 0x11334), (0x1133a, 0x1133a), (0x11345, 0x11346),
    (0x11349, 0x1134a), (0x1134e, 0x1134f), (0x11351, 0x11356), (0x11358, 0x1135c), (0x11364, 0x11365), (0x1136d, 0x1136f),
    (0x11375, 0x113ff), (0x1145c, 0x1145c), (0x11462, 0x1147f), (0x114c8, 0x114cf), (0x114da, 0x1157f), (0x115b6, 0x115b7),
    (0x115de, 0x115ff), (0x11645, 0x1164f), (0x1165a, 0x1165f), (0x1166d, 0x1167f), (0x116ba, 0x116bf), (0x116ca, 0x116ff),
    (0x1171b, 0x1171c), (0x1172c, 0x1172f), (0x11747, 0x117ff), (0x1183c, 0x1189f), (0x118f3, 0x118fe), (0x11907, 0x11908),
    (0x1190a, 0x1190b), (0x11914, 0x11914), (0x11917, 0x11917), (0x11936, 0x11936), (0x11939, 0x1193a), (0x11947, 0x1194f),
    (0x1195a, 0x1199f), (0x119a8, 0x119a9), (0x119d8, 0x119d9), (0x119e5, 0x119ff), (0x11a48, 0x11a4f), (0x11aa3, 0x11aaf),
    (0x11af9, 0x11bff), (0x11c09, 0x11c09), (0x11c37, 0x11c37), (0x11c46, 0x11c4f), (0x11c6d, 0x11c6f), (0x11c90, 0x11c91),
    (0x11ca8, 0x11ca8), (0x11cb7, 0x11cff), (0x11d07, 0x11d07), (0x11d0a, 0x11d0a), (0x11d37, 0x11d39), (0x11d3b, 0x11d3b),
    (0x11d3e, 0x11d3e), (0x11d48, 0x11d4f), (0x11d5a, 0x11d5f), (0x11d66, 0x11d66), (0x11d69, 0x11d69), (0x11d8f, 0x11d8f),
    (0x11d92, 0x11d92), (0x11d99, 0x11d9f), (0x11daa, 0x11edf), (0x11ef9, 0x11faf), (0x11fb1, 0x11fbf), (0x11ff2, 0x11ffe),
    (0x1239a, 0x123ff), (0x1246f, 0x1246f), (0x12475, 0x1247f), (0x12544, 0x12f8f), (0x12ff3, 0x12fff), (0x1342f, 0x143ff),
    (0x14647, 0x167ff), (0x16a39, 0x16a3f), (0x16a5f, 0x16a5f), (0x16a6a, 0x16a6d), (0x16abf, 0x16abf), (0x16aca, 0x16acf),
    (0x16aee, 0x16aef), (0x16af6, 0x16aff), (0x16b46, 0x16b4f), (0x16b5a, 0x16b5a), (0x16b62, 0x16b62), (0x16b78, 0x16b7c),
    (0x16b90, 0x16e3f), (0x16e9b, 0x16eff), (0x16f4b, 0x16f4e), (0x16f88, 0x16f8e), (0x16fa0, 0x16fdf), (0x16fe5, 0x16fef),
    (0x16ff2, 0x16fff), (0x187f8, 0x187ff), (0x18cd6, 0x18cff), (0x18d09, 0x1afef), (0x1aff4, 0x1aff4), (0x1affc, 0x1affc),
    (0x1afff, 0x1afff), (0x1b123, 0x1b14f), (0x1b153, 0x1b163), (0x1b168, 0x1b16f), (0x1b2fc, 0x1bbff), (0x1bc6b, 0x1bc6f),
    (0x1bc7d, 0x1bc7f), (0x1bc89, 0x1bc8f), (0x1bc9a, 0x1bc9b), (0x1bca0, 0x1ceff), (0x1cf2e, 0x1cf2f), (0x1cf47, 0x1cf4f),
    (0x1cfc4, 0x1cfff), (0x1d0f6, 0x1d0ff), (0x1d127, 0x1d128), (0x1d173, 0x1d17a), (0x1d1eb, 0x1d1ff), (0x1d246, 0x1d2df),
    (0x1d2f4, 0x1d2ff), (0x1d357, 0x1d35f), (0x1d379, 0x1d3ff), (0x1d455, 0x1d455), (0x1d49d, 0x1d49d), (0x1d4a0, 0x1d4a1),
    (0x1d4a3, 0x1d4a4), (0x1d4a7, 0x1d4a8), (0x1d4ad, 0x1d4ad), (0x1d4ba, 0x1d4ba), (0x1d4bc, 0x1d4bc), (0x1d4c4, 0x1d4c4),
    (0x1d506, 0x1d506), (0x1d50b, 0x1d50c), (0x1d515, 0x1d515), (0x1d51d, 0x1d51d), (0x1d53a, 0x1d53a), (0x1d53f, 0x1d53f),
    (0x1d545, 0x1d545), (0x1d547, 0x1d549), (0x1d551, 0x1d551), (0x1d6a6, 0x1d6a7), (0x1d7cc, 0x1d7cd), (0x1da8c, 0x1da9a),
    (0x1daa0, 0x1daa0), (0x1dab0, 0x1deff), (0x1df1f, 0x1dfff), (0x1e007, 0x1e007), (0x1e019, 0x1e01a), (0x1e022, 0x1e022),
    (0x1e025, 0x1e025), (0x1e02b, 0x1e0ff), (0x1e12d, 0x1e12f), (0x1e13e, 0x1e13f), (0x1e14a, 0x1e14d), (0x1e150, 0x1e28f),
    (0x1e2af, 0x1e2bf), (0x1e2fa, 0x1e2fe), (0x1e300, 0x1e7df), (0x1e7e7, 0x1e7e7), (0x1e7ec, 0x1e7ec), (0x1e7ef, 0x1e7ef),
    (0x1e7ff, 0x1e7ff), (0x1e8c5, 0x1e8c6), (0x1e8d7, 0x1e8ff), (0x1e94c, 0x1e94f), (0x1e95a, 0x1e95d), (0x1e960, 0x1ec70),
    (0x1ecb5, 0x1ed00), (0x1ed3e, 0x1edff), (0x1ee04, 0x1ee04), (0x1ee20, 0x1ee20), (0x1ee23, 0x1ee23), (0x1ee25, 0x1ee26),
    (0x1ee28, 0x1ee28), (0x1ee33, 0x1ee33), (0x1ee38, 0x1ee38), (0x1ee3a, 0x1ee3a), (0x1ee3c, 0x1ee41), (0x1ee43, 0x1ee46),
    (0x1ee48, 0x1ee48), (0x1ee4a, 0x1ee4a), (0x1ee4c, 0x1ee4c), (0x1ee50, 0x1ee50), (0x1ee53, 0x1ee53), (0x1ee55, 0x1ee56),
    (0x1ee58, 0x1ee58), (0x1ee5a, 0x1ee5a), (0x1ee5c, 0x1ee5c), (0x1ee5e, 0x1ee5e), (0x1ee60, 0x1ee60), (0x1ee63, 0x1ee63),
    (0x1ee65, 0x1ee66), (0x1ee6b, 0x1ee6b), (0x1ee73, 0x1ee73), (0x1ee78, 0x1ee78), (0x1ee7d, 0x1ee7d), (0x1ee7f, 0x1ee7f),
    (0x1ee8a, 0x1ee8a), (0x1ee9c, 0x1eea0), (0x1eea4, 0x1eea4), (0x1eeaa, 0x1eeaa), (0x1eebc, 0x1eeef), (0x1eef2, 0x1efff),
    (0x1f02c, 0x1f02f), (0x1f094, 0x1f09f), (0x1f0af, 0x1f0b0), (0x1f0c0, 0x1f0c0), (0x1f0d0, 0x1f0d0), (0x1f0f6, 0x1f0ff),
    (0x1f1ae, 0x1f1e5), (0x1f203, 0x1f20f), (0x1f23c, 0x1f23f), (0x1f249, 0x1f24f), (0x1f252, 0x1f25f), (0x1f266, 0x1f2ff),
    (0x1f6d8, 0x1f6dc), (0x1f6ed, 0x1f6ef), (0x1f6fd, 0x1f6ff), (0x1f774, 0x1f77f), (0x1f7d9, 0x1f7df), (0x1f7ec, 0x1f7ef),
    (0x1f7f1, 0x1f7ff), (0x1f80c, 0x1f80f), (0x1f848, 0x1f84f), (0x1f85a, 0x1f85f), (0x1f888, 0x1f88f), (0x1f8ae, 0x1f8af),
    (0x1f8b2, 0x1f8ff), (0x1fa54, 0x1fa5f), (0x1fa6e, 0x1fa6f), (0x1fa75, 0x1fa77), (0x1fa7d, 0x1fa7f), (0x1fa87, 0x1fa8f),
    (0x1faad, 0x1faaf), (0x1fabb, 0x1fabf), (0x1fac6, 0x1facf), (0x1fada, 0x1fadf), (0x1fae8, 0x1faef), (0x1faf7, 0x1faff),
    (0x1fb93, 0x1fb93), (0x1fbcb, 0x1fbef), (0x1fbfa, 0x1ffff), (0x2a6e0, 0x2a6ff), (0x2b739, 0x2b73f), (0x2b81e, 0x2b81f),
    (0x2cea2, 0x2ceaf), (0x2ebe1, 0x2f7ff), (0x2fa1e, 0x2ffff), (0x3134b, 0xe00ff), (0xe01f0, 0x10ffff),
];

/// Unicode decimal digits (`str.isdecimal()`), in runs of ten from a zero.
#[rustfmt::skip]
const DECIMAL: &[(u32, u32)] = &[
    (0x30, 0x39), (0x660, 0x669), (0x6f0, 0x6f9), (0x7c0, 0x7c9), (0x966, 0x96f), (0x9e6, 0x9ef),
    (0xa66, 0xa6f), (0xae6, 0xaef), (0xb66, 0xb6f), (0xbe6, 0xbef), (0xc66, 0xc6f), (0xce6, 0xcef),
    (0xd66, 0xd6f), (0xde6, 0xdef), (0xe50, 0xe59), (0xed0, 0xed9), (0xf20, 0xf29), (0x1040, 0x1049),
    (0x1090, 0x1099), (0x17e0, 0x17e9), (0x1810, 0x1819), (0x1946, 0x194f), (0x19d0, 0x19d9), (0x1a80, 0x1a89),
    (0x1a90, 0x1a99), (0x1b50, 0x1b59), (0x1bb0, 0x1bb9), (0x1c40, 0x1c49), (0x1c50, 0x1c59), (0xa620, 0xa629),
    (0xa8d0, 0xa8d9), (0xa900, 0xa909), (0xa9d0, 0xa9d9), (0xa9f0, 0xa9f9), (0xaa50, 0xaa59), (0xabf0, 0xabf9),
    (0xff10, 0xff19), (0x104a0, 0x104a9), (0x10d30, 0x10d39), (0x11066, 0x1106f), (0x110f0, 0x110f9), (0x11136, 0x1113f),
    (0x111d0, 0x111d9), (0x112f0, 0x112f9), (0x11450, 0x11459), (0x114d0, 0x114d9), (0x11650, 0x11659), (0x116c0, 0x116c9),
    (0x11730, 0x11739), (0x118e0, 0x118e9), (0x11950, 0x11959), (0x11c50, 0x11c59), (0x11d50, 0x11d59), (0x11da0, 0x11da9),
    (0x16a60, 0x16a69), (0x16ac0, 0x16ac9), (0x16b50, 0x16b59), (0x1d7ce, 0x1d7ff), (0x1e140, 0x1e149), (0x1e2f0, 0x1e2f9),
    (0x1e950, 0x1e959), (0x1fbf0, 0x1fbf9),
];

/// `html.entities.html5`, sorted.
#[rustfmt::skip]
const ENTITIES: &[(&str, &str)] = &[
    ("AElig", "\u{c6}"), ("AElig;", "\u{c6}"), ("AMP", "&"), ("AMP;", "&"), ("Aacute", "\u{c1}"),
    ("Aacute;", "\u{c1}"), ("Abreve;", "\u{102}"), ("Acirc", "\u{c2}"), ("Acirc;", "\u{c2}"), ("Acy;", "\u{410}"),
    ("Afr;", "\u{1d504}"), ("Agrave", "\u{c0}"), ("Agrave;", "\u{c0}"), ("Alpha;", "\u{391}"), ("Amacr;", "\u{100}"),
    ("And;", "\u{2a53}"), ("Aogon;", "\u{104}"), ("Aopf;", "\u{1d538}"), ("ApplyFunction;", "\u{2061}"),
    ("Aring", "\u{c5}"), ("Aring;", "\u{c5}"), ("Ascr;", "\u{1d49c}"), ("Assign;", "\u{2254}"), ("Atilde", "\u{c3}"),
    ("Atilde;", "\u{c3}"), ("Auml", "\u{c4}"), ("Auml;", "\u{c4}"), ("Backslash;", "\u{2216}"), ("Barv;", "\u{2ae7}"),
    ("Barwed;", "\u{2306}"), ("Bcy;", "\u{411}"), ("Because;", "\u{2235}"), ("Bernoullis;", "\u{212c}"),
    ("Beta;", "\u{392}"), ("Bfr;", "\u{1d505}"), ("Bopf;", "\u{1d539}"), ("Breve;", "\u{2d8}"), ("Bscr;", "\u{212c}"),
    ("Bumpeq;", "\u{224e}"), ("CHcy;", "\u{427}"), ("COPY", "\u{a9}"), ("COPY;", "\u{a9}"), ("Cacute;", "\u{106}"),
    ("Cap;", "\u{22d2}"), ("CapitalDifferentialD;", "\u{2145}"), ("Cayleys;", "\u{212d}"), ("Ccaron;", "\u{10c}"),
    ("Ccedil", "\u{c7}"), ("Ccedil;", "\u{c7}"), ("Ccirc;", "\u{108}"), ("Cconint;", "\u{2230}"),
    ("Cdot;", "\u{10a}"), ("Cedilla;", "\u{b8}"), ("CenterDot;", "\u{b7}"), ("Cfr;", "\u{212d}"), ("Chi;", "\u{3a7}"),
    ("CircleDot;", "\u{2299}"), ("CircleMinus;", "\u{2296}"), ("CirclePlus;", "\u{2295}"),
    ("CircleTimes;", "\u{2297}"), ("ClockwiseContourIntegral;", "\u{2232}"), ("CloseCurlyDoubleQuote;", "\u{201d}"),
    ("CloseCurlyQuote;", "\u{2019}"), ("Colon;", "\u{2237}"), ("Colone;", "\u{2a74}"), ("Congruent;", "\u{2261}"),
    ("Conint;", "\u{222f}"), ("ContourIntegral;", "\u{222e}"), ("Copf;", "\u{2102}"), ("Coproduct;", "\u{2210}"),
    ("CounterClockwiseContourIntegral;", "\u{2233}"), ("Cross;", "\u{2a2f}"), ("Cscr;", "\u{1d49e}"),
    ("Cup;", "\u{22d3}"), ("CupCap;", "\u{224d}"), ("DD;", "\u{2145}"), ("DDotrahd;", "\u{2911}"),
    ("DJcy;", "\u{402}"), ("DScy;", "\u{405}"), ("DZcy;", "\u{40f}"), ("Dagger;", "\u{2021}"), ("Darr;", "\u{21a1}"),
    ("Dashv;", "\u{2ae4}"), ("Dcaron;", "\u{10e}"), ("Dcy;", "\u{414}"), ("Del;", "\u{2207}"), ("Delta;", "\u{394}"),
    ("Dfr;", "\u{1d507}"), ("DiacriticalAcute;", "\u{b4}"), ("DiacriticalDot;", "\u{2d9}"),
    ("DiacriticalDoubleAcute;", "\u{2dd}"), ("DiacriticalGrave;", "`"), ("DiacriticalTilde;", "\u{2dc}"),
    ("Diamond;", "\u{22c4}"), ("DifferentialD;", "\u{2146}"), ("Dopf;", "\u{1d53b}"), ("Dot;", "\u{a8}"),
    ("DotDot;", "\u{20dc}"), ("DotEqual;", "\u{2250}"), ("DoubleContourIntegral;", "\u{222f}"),
    ("DoubleDot;", "\u{a8}"), ("DoubleDownArrow;", "\u{21d3}"), ("DoubleLeftArrow;", "\u{21d0}"),
    ("DoubleLeftRightArrow;", "\u{21d4}"), ("DoubleLeftTee;", "\u{2ae4}"), ("DoubleLongLeftArrow;", "\u{27f8}"),
    ("DoubleLongLeftRightArrow;", "\u{27fa}"), ("DoubleLongRightArrow;", "\u{27f9}"),
    ("DoubleRightArrow;", "\u{21d2}"), ("DoubleRightTee;", "\u{22a8}"), ("DoubleUpArrow;", "\u{21d1}"),
    ("DoubleUpDownArrow;", "\u{21d5}"), ("DoubleVerticalBar;", "\u{2225}"), ("DownArrow;", "\u{2193}"),
    ("DownArrowBar;", "\u{2913}"), ("DownArrowUpArrow;", "\u{21f5}"), ("DownBreve;", "\u{311}"),
    ("DownLeftRightVector;", "\u{2950}"), ("DownLeftTeeVector;", "\u{295e}"), ("DownLeftVector;", "\u{21bd}"),
    ("DownLeftVectorBar;", "\u{2956}"), ("DownRightTeeVector;", "\u{295f}"), ("DownRightVector;", "\u{21c1}"),
    ("DownRightVectorBar;", "\u{2957}"), ("DownTee;", "\u{22a4}"), ("DownTeeArrow;", "\u{21a7}"),
    ("Downarrow;", "\u{21d3}"), ("Dscr;", "\u{1d49f}"), ("Dstrok;", "\u{110}"), ("ENG;", "\u{14a}"),
    ("ETH", "\u{d0}"), ("ETH;", "\u{d0}"), ("Eacute", "\u{c9}"), ("Eacute;", "\u{c9}"), ("Ecaron;", "\u{11a}"),
    ("Ecirc", "\u{ca}"), ("Ecirc;", "\u{ca}"), ("Ecy;", "\u{42d}"), ("Edot;", "\u{116}"), ("Efr;", "\u{1d508}"),
    ("Egrave", "\u{c8}"), ("Egrave;", "\u{c8}"), ("Element;", "\u{2208}"), ("Emacr;", "\u{112}"),
    ("EmptySmallSquare;", "\u{25fb}"), ("EmptyVerySmallSquare;", "\u{25ab}"), ("Eogon;", "\u{118}"),
    ("Eopf;", "\u{1d53c}"), ("Epsilon;", "\u{395}"), ("Equal;", "\u{2a75}"), ("EqualTilde;", "\u{2242}"),
    ("Equilibrium;", "\u{21cc}"), ("Escr;", "\u{2130}"), ("Esim;", "\u{2a73}"), ("Eta;", "\u{397}"),
    ("Euml", "\u{cb}"), ("Euml;", "\u{cb}"), ("Exists;", "\u{2203}"), ("ExponentialE;", "\u{2147}"),
    ("Fcy;", "\u{424}"), ("Ffr;", "\u{1d509}"), ("FilledSmallSquare;", "\u{25fc}"),
    ("FilledVerySmallSquare;", "\u{25aa}"), ("Fopf;", "\u{1d53d}"), ("ForAll;", "\u{2200}"),
    ("Fouriertrf;", "\u{2131}"), ("Fscr;", "\u{2131}"), ("GJcy;", "\u{403}"), ("GT", ">"), ("GT;", ">"),
    ("Gamma;", "\u{393}"), ("Gammad;", "\u{3dc}"), ("Gbreve;", "\u{11e}"), ("Gcedil;", "\u{122}"),
    ("Gcirc;", "\u{11c}"), ("Gcy;", "\u{413}"), ("Gdot;", "\u{120}"), ("Gfr;", "\u{1d50a}"), ("Gg;", "\u{22d9}"),
    ("Gopf;", "\u{1d53e}"), ("GreaterEqual;", "\u{2265}"), ("GreaterEqualLess;", "\u{22db}"),
    ("GreaterFullEqual;", "\u{2267}"), ("GreaterGreater;", "\u{2aa2}"), ("GreaterLess;", "\u{2277}"),
    ("GreaterSlantEqual;", "\u{2a7e}"), ("GreaterTilde;", "\u{2273}"), ("Gscr;", "\u{1d4a2}"), ("Gt;", "\u{226b}"),
    ("HARDcy;", "\u{42a}"), ("Hacek;", "\u{2c7}"), ("Hat;", "^"), ("Hcirc;", "\u{124}"), ("Hfr;", "\u{210c}"),
    ("HilbertSpace;", "\u{210b}"), ("Hopf;", "\u{210d}"), ("HorizontalLine;", "\u{2500}"), ("Hscr;", "\u{210b}"),
    ("Hstrok;", "\u{126}"), ("HumpDownHump;", "\u{224e}"), ("HumpEqual;", "\u{224f}"), ("IEcy;", "\u{415}"),
    ("IJlig;", "\u{132}"), ("IOcy;", "\u{401}"), ("Iacute", "\u{cd}"), ("Iacute;", "\u{cd}"), ("Icirc", "\u{ce}"),
    ("Icirc;", "\u{ce}"), ("Icy;", "\u{418}"), ("Idot;", "\u{130}"), ("Ifr;", "\u{2111}"), ("Igrave", "\u{cc}"),
    ("Igrave;", "\u{cc}"), ("Im;", "\u{2111}"), ("Imacr;", "\u{12a}"), ("ImaginaryI;", "\u{2148}"),
    ("Implies;", "\u{21d2}"), ("Int;", "\u{222c}"), ("Integral;", "\u{222b}"), ("Intersection;", "\u{22c2}"),
    ("InvisibleComma;", "\u{2063}"), ("InvisibleTimes;", "\u{2062}"), ("Iogon;", "\u{12e}"), ("Iopf;", "\u{1d540}"),
    ("Iota;", "\u{399}"), ("Iscr;", "\u{2110}"), ("Itilde;", "\u{128}"), ("Iukcy;", "\u{406}"), ("Iuml", "\u{cf}"),
    ("Iuml;", "\u{cf}"), ("Jcirc;", "\u{134}"), ("Jcy;", "\u{419}"), ("Jfr;", "\u{1d50d}"), ("Jopf;", "\u{1d541}"),
    ("Jscr;", "\u{1d4a5}"), ("Jsercy;", "\u{408}"), ("Jukcy;", "\u{404}"), ("KHcy;", "\u{425}"), ("KJcy;", "\u{40c}"),
    ("Kappa;", "\u{39a}"), ("Kcedil;", "\u{136}"), ("Kcy;", "\u{41a}"), ("Kfr;", "\u{1d50e}"), ("Kopf;", "\u{1d542}"),
    ("Kscr;", "\u{1d4a6}"), ("LJcy;", "\u{409}"), ("LT", "<"), ("LT;", "<"), ("Lacute;", "\u{139}"),
    ("Lambda;", "\u{39b}"), ("Lang;", "\u{27ea}"), ("Laplacetrf;", "\u{2112}"), ("Larr;", "\u{219e}"),
    ("Lcaron;", "\u{13d}"), ("Lcedil;", "\u{13b}"), ("Lcy;", "\u{41b}"), ("LeftAngleBracket;", "\u{27e8}"),
    ("LeftArrow;", "\u{2190}"), ("LeftArrowBar;", "\u{21e4}"), ("LeftArrowRightArrow;", "\u{21c6}"),
    ("LeftCeiling;", "\u{2308}"), ("LeftDoubleBracket;", "\u{27e6}"), ("LeftDownTeeVector;", "\u{2961}"),
    ("LeftDownVector;", "\u{21c3}"), ("LeftDownVectorBar;", "\u{2959}"), ("LeftFloor;", "\u{230a}"),
    ("LeftRightArrow;", "\u{2194}"), ("LeftRightVector;", "\u{294e}"), ("LeftTee;", "\u{22a3}"),
    ("LeftTeeArrow;", "\u{21a4}"), ("LeftTeeVector;", "\u{295a}"), ("LeftTriangle;", "\u{22b2}"),
    ("LeftTriangleBar;", "\u{29cf}"), ("LeftTriangleEqual;", "\u{22b4}"), ("LeftUpDownVector;", "\u{2951}"),
    ("LeftUpTeeVector;", "\u{2960}"), ("LeftUpVector;", "\u{21bf}"), ("LeftUpVectorBar;", "\u{2958}"),
    ("LeftVector;", "\u{21bc}"), ("LeftVectorBar;", "\u{2952}"), ("Leftarrow;", "\u{21d0}"),
    ("Leftrightarrow;", "\u{21d4}"), ("LessEqualGreater;", "\u{22da}"), ("LessFullEqual;", "\u{2266}"),
    ("LessGreater;", "\u{2276}"), ("LessLess;", "\u{2aa1}"), ("LessSlantEqual;", "\u{2a7d}"),
    ("LessTilde;", "\u{2272}"), ("Lfr;", "\u{1d50f}"), ("Ll;", "\u{22d8}"), ("Lleftarrow;", "\u{21da}"),
    ("Lmidot;", "\u{13f}"), ("LongLeftArrow;", "\u{27f5}"), ("LongLeftRightArrow;", "\u{27f7}"),
    ("LongRightArrow;", "\u{27f6}"), ("Longleftarrow;", "\u{27f8}"), ("Longleftrightarrow;", "\u{27fa}"),
    ("Longrightarrow;", "\u{27f9}"), ("Lopf;", "\u{1d543}"), ("LowerLeftArrow;", "\u{2199}"),
    ("LowerRightArrow;", "\u{2198}"), ("Lscr;", "\u{2112}"), ("Lsh;", "\u{21b0}"), ("Lstrok;", "\u{141}"),
    ("Lt;", "\u{226a}"), ("Map;", "\u{2905}"), ("Mcy;", "\u{41c}"), ("MediumSpace;", "\u{205f}"),
    ("Mellintrf;", "\u{2133}"), ("Mfr;", "\u{1d510}"), ("MinusPlus;", "\u{2213}"), ("Mopf;", "\u{1d544}"),
    ("Mscr;", "\u{2133}"), ("Mu;", "\u{39c}"), ("NJcy;", "\u{40a}"), ("Nacute;", "\u{143}"), ("Ncaron;", "\u{147}"),
    ("Ncedil;", "\u{145}"), ("Ncy;", "\u{41d}"), ("NegativeMediumSpace;", "\u{200b}"),
    ("NegativeThickSpace;", "\u{200b}"), ("NegativeThinSpace;", "\u{200b}"), ("NegativeVeryThinSpace;", "\u{200b}"),
    ("NestedGreaterGreater;", "\u{226b}"), ("NestedLessLess;", "\u{226a}"), ("NewLine;", "\u{a}"),
    ("Nfr;", "\u{1d511}"), ("NoBreak;", "\u{2060}"), ("NonBreakingSpace;", "\u{a0}"), ("Nopf;", "\u{2115}"),
    ("Not;", "\u{2aec}"), ("NotCongruent;", "\u{2262}"), ("NotCupCap;", "\u{226d}"),
    ("NotDoubleVerticalBar;", "\u{2226}"), ("NotElement;", "\u{2209}"), ("NotEqual;", "\u{2260}"),
    ("NotEqualTilde;", "\u{2242}\u{338}"), ("NotExists;", "\u{2204}"), ("NotGreater;", "\u{226f}"),
    ("NotGreaterEqual;", "\u{2271}"), ("NotGreaterFullEqual;", "\u{2267}\u{338}"),
    ("NotGreaterGreater;", "\u{226b}\u{338}"), ("NotGreaterLess;", "\u{2279}"),
    ("NotGreaterSlantEqual;", "\u{2a7e}\u{338}"), ("NotGreaterTilde;", "\u{2275}"),
    ("NotHumpDownHump;", "\u{224e}\u{338}"), ("NotHumpEqual;", "\u{224f}\u{338}"), ("NotLeftTriangle;", "\u{22ea}"),
    ("NotLeftTriangleBar;", "\u{29cf}\u{338}"), ("NotLeftTriangleEqual;", "\u{22ec}"), ("NotLess;", "\u{226e}"),
    ("NotLessEqual;", "\u{2270}"), ("NotLessGreater;", "\u{2278}"), ("NotLessLess;", "\u{226a}\u{338}"),
    ("NotLessSlantEqual;", "\u{2a7d}\u{338}"), ("NotLessTilde;", "\u{2274}"),
    ("NotNestedGreaterGreater;", "\u{2aa2}\u{338}"), ("NotNestedLessLess;", "\u{2aa1}\u{338}"),
    ("NotPrecedes;", "\u{2280}"), ("NotPrecedesEqual;", "\u{2aaf}\u{338}"), ("NotPrecedesSlantEqual;", "\u{22e0}"),
    ("NotReverseElement;", "\u{220c}"), ("NotRightTriangle;", "\u{22eb}"),
    ("NotRightTriangleBar;", "\u{29d0}\u{338}"), ("NotRightTriangleEqual;", "\u{22ed}"),
    ("NotSquareSubset;", "\u{228f}\u{338}"), ("NotSquareSubsetEqual;", "\u{22e2}"),
    ("NotSquareSuperset;", "\u{2290}\u{338}"), ("NotSquareSupersetEqual;", "\u{22e3}"),
    ("NotSubset;", "\u{2282}\u{20d2}"), ("NotSubsetEqual;", "\u{2288}"), ("NotSucceeds;", "\u{2281}"),
    ("NotSucceedsEqual;", "\u{2ab0}\u{338}"), ("NotSucceedsSlantEqual;", "\u{22e1}"),
    ("NotSucceedsTilde;", "\u{227f}\u{338}"), ("NotSuperset;", "\u{2283}\u{20d2}"), ("NotSupersetEqual;", "\u{2289}"),
    ("NotTilde;", "\u{2241}"), ("NotTildeEqual;", "\u{2244}"), ("NotTildeFullEqual;", "\u{2247}"),
    ("NotTildeTilde;", "\u{2249}"), ("NotVerticalBar;", "\u{2224}"), ("Nscr;", "\u{1d4a9}"), ("Ntilde", "\u{d1}"),
    ("Ntilde;", "\u{d1}"), ("Nu;", "\u{39d}"), ("OElig;", "\u{152}"), ("Oacute", "\u{d3}"), ("Oacute;", "\u{d3}"),
    ("Ocirc", "\u{d4}"), ("Ocirc;", "\u{d4}"), ("Ocy;", "\u{41e}"), ("Odblac;", "\u{150}"), ("Ofr;", "\u{1d512}"),
    ("Ograve", "\u{d2}"), ("Ograve;", "\u{d2}"), ("Omacr;", "\u{14c}"), ("Omega;", "\u{3a9}"),
    ("Omicron;", "\u{39f}"), ("Oopf;", "\u{1d546}"), ("OpenCurlyDoubleQuote;", "\u{201c}"),
    ("OpenCurlyQuote;", "\u{2018}"), ("Or;", "\u{2a54}"), ("Oscr;", "\u{1d4aa}"), ("Oslash", "\u{d8}"),
    ("Oslash;", "\u{d8}"), ("Otilde", "\u{d5}"), ("Otilde;", "\u{d5}"), ("Otimes;", "\u{2a37}"), ("Ouml", "\u{d6}"),
    ("Ouml;", "\u{d6}"), ("OverBar;", "\u{203e}"), ("OverBrace;", "\u{23de}"), ("OverBracket;", "\u{23b4}"),
    ("OverParenthesis;", "\u{23dc}"), ("PartialD;", "\u{2202}"), ("Pcy;", "\u{41f}"), ("Pfr;", "\u{1d513}"),
    ("Phi;", "\u{3a6}"), ("Pi;", "\u{3a0}"), ("PlusMinus;", "\u{b1}"), ("Poincareplane;", "\u{210c}"),
    ("Popf;", "\u{2119}"), ("Pr;", "\u{2abb}"), ("Precedes;", "\u{227a}"), ("PrecedesEqual;", "\u{2aaf}"),
    ("PrecedesSlantEqual;", "\u{227c}"), ("PrecedesTilde;", "\u{227e}"), ("Prime;", "\u{2033}"),
    ("Product;", "\u{220f}"), ("Proportion;", "\u{2237}"), ("Proportional;", "\u{221d}"), ("Pscr;", "\u{1d4ab}"),
    ("Psi;", "\u{3a8}"), ("QUOT", "\u{22}"), ("QUOT;", "\u{22}"), ("Qfr;", "\u{1d514}"), ("Qopf;", "\u{211a}"),
    ("Qscr;", "\u{1d4ac}"), ("RBarr;", "\u{2910}"), ("REG", "\u{ae}"), ("REG;", "\u{ae}"), ("Racute;", "\u{154}"),
    ("Rang;", "\u{27eb}"), ("Rarr;", "\u{21a0}"), ("Rarrtl;", "\u{2916}"), ("Rcaron;", "\u{158}"),
    ("Rcedil;", "\u{156}"), ("Rcy;", "\u{420}"), ("Re;", "\u{211c}"), ("ReverseElement;", "\u{220b}"),
    ("ReverseEquilibrium;", "\u{21cb}"), ("ReverseUpEquilibrium;", "\u{296f}"), ("Rfr;", "\u{211c}"),
    ("Rho;", "\u{3a1}"), ("RightAngleBracket;", "\u{27e9}"), ("RightArrow;", "\u{2192}"),
    ("RightArrowBar;", "\u{21e5}"), ("RightArrowLeftArrow;", "\u{21c4}"), ("RightCeiling;", "\u{2309}"),
    ("RightDoubleBracket;", "\u{27e7}"), ("RightDownTeeVector;", "\u{295d}"), ("RightDownVector;", "\u{21c2}"),
    ("RightDownVectorBar;", "\u{2955}"), ("RightFloor;", "\u{230b}"), ("RightTee;", "\u{22a2}"),
    ("RightTeeArrow;", "\u{21a6}"), ("RightTeeVector;", "\u{295b}"), ("RightTriangle;", "\u{22b3}"),
    ("RightTriangleBar;", "\u{29d0}"), ("RightTriangleEqual;", "\u{22b5}"), ("RightUpDownVector;", "\u{294f}"),
    ("RightUpTeeVector;", "\u{295c}"), ("RightUpVector;", "\u{21be}"), ("RightUpVectorBar;", "\u{2954}"),
    ("RightVector;", "\u{21c0}"), ("RightVectorBar;", "\u{2953}"), ("Rightarrow;", "\u{21d2}"), ("Ropf;", "\u{211d}"),
    ("RoundImplies;", "\u{2970}"), ("Rrightarrow;", "\u{21db}"), ("Rscr;", "\u{211b}"), ("Rsh;", "\u{21b1}"),
    ("RuleDelayed;", "\u{29f4}"), ("SHCHcy;", "\u{429}"), ("SHcy;", "\u{428}"), ("SOFTcy;", "\u{42c}"),
    ("Sacute;", "\u{15a}"), ("Sc;", "\u{2abc}"), ("Scaron;", "\u{160}"), ("Scedil;", "\u{15e}"),
    ("Scirc;", "\u{15c}"), ("Scy;", "\u{421}"), ("Sfr;", "\u{1d516}"), ("ShortDownArrow;", "\u{2193}"),
    ("ShortLeftArrow;", "\u{2190}"), ("ShortRightArrow;", "\u{2192}"), ("ShortUpArrow;", "\u{2191}"),
    ("Sigma;", "\u{3a3}"), ("SmallCircle;", "\u{2218}"), ("Sopf;", "\u{1d54a}"), ("Sqrt;", "\u{221a}"),
    ("Square;", "\u{25a1}"), ("SquareIntersection;", "\u{2293}"), ("SquareSubset;", "\u{228f}"),
    ("SquareSubsetEqual;", "\u{2291}"), ("SquareSuperset;", "\u{2290}"), ("SquareSupersetEqual;", "\u{2292}"),
    ("SquareUnion;", "\u{2294}"), ("Sscr;", "\u{1d4ae}"), ("Star;", "\u{22c6}"), ("Sub;", "\u{22d0}"),
    ("Subset;", "\u{22d0}"), ("SubsetEqual;", "\u{2286}"), ("Succeeds;", "\u{227b}"), ("SucceedsEqual;", "\u{2ab0}"),
    ("SucceedsSlantEqual;", "\u{227d}"), ("SucceedsTilde;", "\u{227f}"), ("SuchThat;", "\u{220b}"),
    ("Sum;", "\u{2211}"), ("Sup;", "\u{22d1}"), ("Superset;", "\u{2283}"), ("SupersetEqual;", "\u{2287}"),
    ("Supset;", "\u{22d1}"), ("THORN", "\u{de}"), ("THORN;", "\u{de}"), ("TRADE;", "\u{2122}"), ("TSHcy;", "\u{40b}"),
    ("TScy;", "\u{426}"), ("Tab;", "\u{9}"), ("Tau;", "\u{3a4}"), ("Tcaron;", "\u{164}"), ("Tcedil;", "\u{162}"),
    ("Tcy;", "\u{422}"), ("Tfr;", "\u{1d517}"), ("Therefore;", "\u{2234}"), ("Theta;", "\u{398}"),
    ("ThickSpace;", "\u{205f}\u{200a}"), ("ThinSpace;", "\u{2009}"), ("Tilde;", "\u{223c}"),
    ("TildeEqual;", "\u{2243}"), ("TildeFullEqual;", "\u{2245}"), ("TildeTilde;", "\u{2248}"), ("Topf;", "\u{1d54b}"),
    ("TripleDot;", "\u{20db}"), ("Tscr;", "\u{1d4af}"), ("Tstrok;", "\u{166}"), ("Uacute", "\u{da}"),
    ("Uacute;", "\u{da}"), ("Uarr;", "\u{219f}"), ("Uarrocir;", "\u{2949}"), ("Ubrcy;", "\u{40e}"),
    ("Ubreve;", "\u{16c}"), ("Ucirc", "\u{db}"), ("Ucirc;", "\u{db}"), ("Ucy;", "\u{423}"), ("Udblac;", "\u{170}"),
    ("Ufr;", "\u{1d518}"), ("Ugrave", "\u{d9}"), ("Ugrave;", "\u{d9}"), ("Umacr;", "\u{16a}"), ("UnderBar;", "_"),
    ("UnderBrace;", "\u{23df}"), ("UnderBracket;", "\u{23b5}"), ("UnderParenthesis;", "\u{23dd}"),
    ("Union;", "\u{22c3}"), ("UnionPlus;", "\u{228e}"), ("Uogon;", "\u{172}"), ("Uopf;", "\u{1d54c}"),
    ("UpArrow;", "\u{2191}"), ("UpArrowBar;", "\u{2912}"), ("UpArrowDownArrow;", "\u{21c5}"),
    ("UpDownArrow;", "\u{2195}"), ("UpEquilibrium;", "\u{296e}"), ("UpTee;", "\u{22a5}"), ("UpTeeArrow;", "\u{21a5}"),
    ("Uparrow;", "\u{21d1}"), ("Updownarrow;", "\u{21d5}"), ("UpperLeftArrow;", "\u{2196}"),
    ("UpperRightArrow;", "\u{2197}"), ("Upsi;", "\u{3d2}"), ("Upsilon;", "\u{3a5}"), ("Uring;", "\u{16e}"),
    ("Uscr;", "\u{1d4b0}"), ("Utilde;", "\u{168}"), ("Uuml", "\u{dc}"), ("Uuml;", "\u{dc}"), ("VDash;", "\u{22ab}"),
    ("Vbar;", "\u{2aeb}"), ("Vcy;", "\u{412}"), ("Vdash;", "\u{22a9}"), ("Vdashl;", "\u{2ae6}"), ("Vee;", "\u{22c1}"),
    ("Verbar;", "\u{2016}"), ("Vert;", "\u{2016}"), ("VerticalBar;", "\u{2223}"), ("VerticalLine;", "|"),
    ("VerticalSeparator;", "\u{2758}"), ("VerticalTilde;", "\u{2240}"), ("VeryThinSpace;", "\u{200a}"),
    ("Vfr;", "\u{1d519}"), ("Vopf;", "\u{1d54d}"), ("Vscr;", "\u{1d4b1}"), ("Vvdash;", "\u{22aa}"),
    ("Wcirc;", "\u{174}"), ("Wedge;", "\u{22c0}"), ("Wfr;", "\u{1d51a}"), ("Wopf;", "\u{1d54e}"),
    ("Wscr;", "\u{1d4b2}"), ("Xfr;", "\u{1d51b}"), ("Xi;", "\u{39e}"), ("Xopf;", "\u{1d54f}"), ("Xscr;", "\u{1d4b3}"),
    ("YAcy;", "\u{42f}"), ("YIcy;", "\u{407}"), ("YUcy;", "\u{42e}"), ("Yacute", "\u{dd}"), ("Yacute;", "\u{dd}"),
    ("Ycirc;", "\u{176}"), ("Ycy;", "\u{42b}"), ("Yfr;", "\u{1d51c}"), ("Yopf;", "\u{1d550}"), ("Yscr;", "\u{1d4b4}"),
    ("Yuml;", "\u{178}"), ("ZHcy;", "\u{416}"), ("Zacute;", "\u{179}"), ("Zcaron;", "\u{17d}"), ("Zcy;", "\u{417}"),
    ("Zdot;", "\u{17b}"), ("ZeroWidthSpace;", "\u{200b}"), ("Zeta;", "\u{396}"), ("Zfr;", "\u{2128}"),
    ("Zopf;", "\u{2124}"), ("Zscr;", "\u{1d4b5}"), ("aacute", "\u{e1}"), ("aacute;", "\u{e1}"),
    ("abreve;", "\u{103}"), ("ac;", "\u{223e}"), ("acE;", "\u{223e}\u{333}"), ("acd;", "\u{223f}"),
    ("acirc", "\u{e2}"), ("acirc;", "\u{e2}"), ("acute", "\u{b4}"), ("acute;", "\u{b4}"), ("acy;", "\u{430}"),
    ("aelig", "\u{e6}"), ("aelig;", "\u{e6}"), ("af;", "\u{2061}"), ("afr;", "\u{1d51e}"), ("agrave", "\u{e0}"),
    ("agrave;", "\u{e0}"), ("alefsym;", "\u{2135}"), ("aleph;", "\u{2135}"), ("alpha;", "\u{3b1}"),
    ("amacr;", "\u{101}"), ("amalg;", "\u{2a3f}"), ("amp", "&"), ("amp;", "&"), ("and;", "\u{2227}"),
    ("andand;", "\u{2a55}"), ("andd;", "\u{2a5c}"), ("andslope;", "\u{2a58}"), ("andv;", "\u{2a5a}"),
    ("ang;", "\u{2220}"), ("ange;", "\u{29a4}"), ("angle;", "\u{2220}"), ("angmsd;", "\u{2221}"),
    ("angmsdaa;", "\u{29a8}"), ("angmsdab;", "\u{29a9}"), ("angmsdac;", "\u{29aa}"), ("angmsdad;", "\u{29ab}"),
    ("angmsdae;", "\u{29ac}"), ("angmsdaf;", "\u{29ad}"), ("angmsdag;", "\u{29ae}"), ("angmsdah;", "\u{29af}"),
    ("angrt;", "\u{221f}"), ("angrtvb;", "\u{22be}"), ("angrtvbd;", "\u{299d}"), ("angsph;", "\u{2222}"),
    ("angst;", "\u{c5}"), ("angzarr;", "\u{237c}"), ("aogon;", "\u{105}"), ("aopf;", "\u{1d552}"),
    ("ap;", "\u{2248}"), ("apE;", "\u{2a70}"), ("apacir;", "\u{2a6f}"), ("ape;", "\u{224a}"), ("apid;", "\u{224b}"),
    ("apos;", "'"), ("approx;", "\u{2248}"), ("approxeq;", "\u{224a}"), ("aring", "\u{e5}"), ("aring;", "\u{e5}"),
    ("ascr;", "\u{1d4b6}"), ("ast;", "*"), ("asymp;", "\u{2248}"), ("asympeq;", "\u{224d}"), ("atilde", "\u{e3}"),
    ("atilde;", "\u{e3}"), ("auml", "\u{e4}"), ("auml;", "\u{e4}"), ("awconint;", "\u{2233}"), ("awint;", "\u{2a11}"),
    ("bNot;", "\u{2aed}"), ("backcong;", "\u{224c}"), ("backepsilon;", "\u{3f6}"), ("backprime;", "\u{2035}"),
    ("backsim;", "\u{223d}"), ("backsimeq;", "\u{22cd}"), ("barvee;", "\u{22bd}"), ("barwed;", "\u{2305}"),
    ("barwedge;", "\u{2305}"), ("bbrk;", "\u{23b5}"), ("bbrktbrk;", "\u{23b6}"), ("bcong;", "\u{224c}"),
    ("bcy;", "\u{431}"), ("bdquo;", "\u{201e}"), ("becaus;", "\u{2235}"), ("because;", "\u{2235}"),
    ("bemptyv;", "\u{29b0}"), ("bepsi;", "\u{3f6}"), ("bernou;", "\u{212c}"), ("beta;", "\u{3b2}"),
    ("beth;", "\u{2136}"), ("between;", "\u{226c}"), ("bfr;", "\u{1d51f}"), ("bigcap;", "\u{22c2}"),
    ("bigcirc;", "\u{25ef}"), ("bigcup;", "\u{22c3}"), ("bigodot;", "\u{2a00}"), ("bigoplus;", "\u{2a01}"),
    ("bigotimes;", "\u{2a02}"), ("bigsqcup;", "\u{2a06}"), ("bigstar;", "\u{2605}"), ("bigtriangledown;", "\u{25bd}"),
    ("bigtriangleup;", "\u{25b3}"), ("biguplus;", "\u{2a04}"), ("bigvee;", "\u{22c1}"), ("bigwedge;", "\u{22c0}"),
    ("bkarow;", "\u{290d}"), ("blacklozenge;", "\u{29eb}"), ("blacksquare;", "\u{25aa}"),
    ("blacktriangle;", "\u{25b4}"), ("blacktriangledown;", "\u{25be}"), ("blacktriangleleft;", "\u{25c2}"),
    ("blacktriangleright;", "\u{25b8}"), ("blank;", "\u{2423}"), ("blk12;", "\u{2592}"), ("blk14;", "\u{2591}"),
    ("blk34;", "\u{2593}"), ("block;", "\u{2588}"), ("bne;", "=\u{20e5}"), ("bnequiv;", "\u{2261}\u{20e5}"),
    ("bnot;", "\u{2310}"), ("bopf;", "\u{1d553}"), ("bot;", "\u{22a5}"), ("bottom;", "\u{22a5}"),
    ("bowtie;", "\u{22c8}"), ("boxDL;", "\u{2557}"), ("boxDR;", "\u{2554}"), ("boxDl;", "\u{2556}"),
    ("boxDr;", "\u{2553}"), ("boxH;", "\u{2550}"), ("boxHD;", "\u{2566}"), ("boxHU;", "\u{2569}"),
    ("boxHd;", "\u{2564}"), ("boxHu;", "\u{2567}"), ("boxUL;", "\u{255d}"), ("boxUR;", "\u{255a}"),
    ("boxUl;", "\u{255c}"), ("boxUr;", "\u{2559}"), ("boxV;", "\u{2551}"), ("boxVH;", "\u{256c}"),
    ("boxVL;", "\u{2563}"), ("boxVR;", "\u{2560}"), ("boxVh;", "\u{256b}"), ("boxVl;", "\u{2562}"),
    ("boxVr;", "\u{255f}"), ("boxbox;", "\u{29c9}"), ("boxdL;", "\u{2555}"), ("boxdR;", "\u{2552}"),
    ("boxdl;", "\u{2510}"), ("boxdr;", "\u{250c}"), ("boxh;", "\u{2500}"), ("boxhD;", "\u{2565}"),
    ("boxhU;", "\u{2568}"), ("boxhd;", "\u{252c}"), ("boxhu;", "\u{2534}"), ("boxminus;", "\u{229f}"),
    ("boxplus;", "\u{229e}"), ("boxtimes;", "\u{22a0}"), ("boxuL;", "\u{255b}"), ("boxuR;", "\u{2558}"),
    ("boxul;", "\u{2518}"), ("boxur;", "\u{2514}"), ("boxv;", "\u{2502}"), ("boxvH;", "\u{256a}"),
    ("boxvL;", "\u{2561}"), ("boxvR;", "\u{255e}"), ("boxvh;", "\u{253c}"), ("boxvl;", "\u{2524}"),
    ("boxvr;", "\u{251c}"), ("bprime;", "\u{2035}"), ("breve;", "\u{2d8}"), ("brvbar", "\u{a6}"),
    ("brvbar;", "\u{a6}"), ("bscr;", "\u{1d4b7}"), ("bsemi;", "\u{204f}"), ("bsim;", "\u{223d}"),
    ("bsime;", "\u{22cd}"), ("bsol;", "\u{5c}"), ("bsolb;", "\u{29c5}"), ("bsolhsub;", "\u{27c8}"),
    ("bull;", "\u{2022}"), ("bullet;", "\u{2022}"), ("bump;", "\u{224e}"), ("bumpE;", "\u{2aae}"),
    ("bumpe;", "\u{224f}"), ("bumpeq;", "\u{224f}"), ("cacute;", "\u{107}"), ("cap;", "\u{2229}"),
    ("capand;", "\u{2a44}"), ("capbrcup;", "\u{2a49}"), ("capcap;", "\u{2a4b}"), ("capcup;", "\u{2a47}"),
    ("capdot;", "\u{2a40}"), ("caps;", "\u{2229}\u{fe00}"), ("caret;", "\u{2041}"), ("caron;", "\u{2c7}"),
    ("ccaps;", "\u{2a4d}"), ("ccaron;", "\u{10d}"), ("ccedil", "\u{e7}"), ("ccedil;", "\u{e7}"),
    ("ccirc;", "\u{109}"), ("ccups;", "\u{2a4c}"), ("ccupssm;", "\u{2a50}"), ("cdot;", "\u{10b}"),
    ("cedil", "\u{b8}"), ("cedil;", "\u{b8}"), ("cemptyv;", "\u{29b2}"), ("cent", "\u{a2}"), ("cent;", "\u{a2}"),
    ("centerdot;", "\u{b7}"), ("cfr;", "\u{1d520}"), ("chcy;", "\u{447}"), ("check;", "\u{2713}"),
    ("checkmark;", "\u{2713}"), ("chi;", "\u{3c7}"), ("cir;", "\u{25cb}"), ("cirE;", "\u{29c3}"),
    ("circ;", "\u{2c6}"), ("circeq;", "\u{2257}"), ("circlearrowleft;", "\u{21ba}"),
    ("circlearrowright;", "\u{21bb}"), ("circledR;", "\u{ae}"), ("circledS;", "\u{24c8}"),
    ("circledast;", "\u{229b}"), ("circledcirc;", "\u{229a}"), ("circleddash;", "\u{229d}"), ("cire;", "\u{2257}"),
    ("cirfnint;", "\u{2a10}"), ("cirmid;", "\u{2aef}"), ("cirscir;", "\u{29c2}"), ("clubs;", "\u{2663}"),
    ("clubsuit;", "\u{2663}"), ("colon;", ":"), ("colone;", "\u{2254}"), ("coloneq;", "\u{2254}"), ("comma;", ","),
    ("commat;", "@"), ("comp;", "\u{2201}"), ("compfn;", "\u{2218}"), ("complement;", "\u{2201}"),
    ("complexes;", "\u{2102}"), ("cong;", "\u{2245}"), ("congdot;", "\u{2a6d}"), ("conint;", "\u{222e}"),
    ("copf;", "\u{1d554}"), ("coprod;", "\u{2210}"), ("copy", "\u{a9}"), ("copy;", "\u{a9}"), ("copysr;", "\u{2117}"),
    ("crarr;", "\u{21b5}"), ("cross;", "\u{2717}"), ("cscr;", "\u{1d4b8}"), ("csub;", "\u{2acf}"),
    ("csube;", "\u{2ad1}"), ("csup;", "\u{2ad0}"), ("csupe;", "\u{2ad2}"), ("ctdot;", "\u{22ef}"),
    ("cudarrl;", "\u{2938}"), ("cudarrr;", "\u{2935}"), ("cuepr;", "\u{22de}"), ("cuesc;", "\u{22df}"),
    ("cularr;", "\u{21b6}"), ("cularrp;", "\u{293d}"), ("cup;", "\u{222a}"), ("cupbrcap;", "\u{2a48}"),
    ("cupcap;", "\u{2a46}"), ("cupcup;", "\u{2a4a}"), ("cupdot;", "\u{228d}"), ("cupor;", "\u{2a45}"),
    ("cups;", "\u{222a}\u{fe00}"), ("curarr;", "\u{21b7}"), ("curarrm;", "\u{293c}"), ("curlyeqprec;", "\u{22de}"),
    ("curlyeqsucc;", "\u{22df}"), ("curlyvee;", "\u{22ce}"), ("curlywedge;", "\u{22cf}"), ("curren", "\u{a4}"),
    ("curren;", "\u{a4}"), ("curvearrowleft;", "\u{21b6}"), ("curvearrowright;", "\u{21b7}"), ("cuvee;", "\u{22ce}"),
    ("cuwed;", "\u{22cf}"), ("cwconint;", "\u{2232}"), ("cwint;", "\u{2231}"), ("cylcty;", "\u{232d}"),
    ("dArr;", "\u{21d3}"), ("dHar;", "\u{2965}"), ("dagger;", "\u{2020}"), ("daleth;", "\u{2138}"),
    ("darr;", "\u{2193}"), ("dash;", "\u{2010}"), ("dashv;", "\u{22a3}"), ("dbkarow;", "\u{290f}"),
    ("dblac;", "\u{2dd}"), ("dcaron;", "\u{10f}"), ("dcy;", "\u{434}"), ("dd;", "\u{2146}"), ("ddagger;", "\u{2021}"),
    ("ddarr;", "\u{21ca}"), ("ddotseq;", "\u{2a77}"), ("deg", "\u{b0}"), ("deg;", "\u{b0}"), ("delta;", "\u{3b4}"),
    ("demptyv;", "\u{29b1}"), ("dfisht;", "\u{297f}"), ("dfr;", "\u{1d521}"), ("dharl;", "\u{21c3}"),
    ("dharr;", "\u{21c2}"), ("diam;", "\u{22c4}"), ("diamond;", "\u{22c4}"), ("diamondsuit;", "\u{2666}"),
    ("diams;", "\u{2666}"), ("die;", "\u{a8}"), ("digamma;", "\u{3dd}"), ("disin;", "\u{22f2}"), ("div;", "\u{f7}"),
    ("divide", "\u{f7}"), ("divide;", "\u{f7}"), ("divideontimes;", "\u{22c7}"), ("divonx;", "\u{22c7}"),
    ("djcy;", "\u{452}"), ("dlcorn;", "\u{231e}"), ("dlcrop;", "\u{230d}"), ("dollar;", "$"), ("dopf;", "\u{1d555}"),
    ("dot;", "\u{2d9}"), ("doteq;", "\u{2250}"), ("doteqdot;", "\u{2251}"), ("dotminus;", "\u{2238}"),
    ("dotplus;", "\u{2214}"), ("dotsquare;", "\u{22a1}"), ("doublebarwedge;", "\u{2306}"), ("downarrow;", "\u{2193}"),
    ("downdownarrows;", "\u{21ca}"), ("downharpoonleft;", "\u{21c3}"), ("downharpoonright;", "\u{21c2}"),
    ("drbkarow;", "\u{2910}"), ("drcorn;", "\u{231f}"), ("drcrop;", "\u{230c}"), ("dscr;", "\u{1d4b9}"),
    ("dscy;", "\u{455}"), ("dsol;", "\u{29f6}"), ("dstrok;", "\u{111}"), ("dtdot;", "\u{22f1}"),
    ("dtri;", "\u{25bf}"), ("dtrif;", "\u{25be}"), ("duarr;", "\u{21f5}"), ("duhar;", "\u{296f}"),
    ("dwangle;", "\u{29a6}"), ("dzcy;", "\u{45f}"), ("dzigrarr;", "\u{27ff}"), ("eDDot;", "\u{2a77}"),
    ("eDot;", "\u{2251}"), ("eacute", "\u{e9}"), ("eacute;", "\u{e9}"), ("easter;", "\u{2a6e}"),
    ("ecaron;", "\u{11b}"), ("ecir;", "\u{2256}"), ("ecirc", "\u{ea}"), ("ecirc;", "\u{ea}"), ("ecolon;", "\u{2255}"),
    ("ecy;", "\u{44d}"), ("edot;", "\u{117}"), ("ee;", "\u{2147}"), ("efDot;", "\u{2252}"), ("efr;", "\u{1d522}"),
    ("eg;", "\u{2a9a}"), ("egrave", "\u{e8}"), ("egrave;", "\u{e8}"), ("egs;", "\u{2a96}"), ("egsdot;", "\u{2a98}"),
    ("el;", "\u{2a99}"), ("elinters;", "\u{23e7}"), ("ell;", "\u{2113}"), ("els;", "\u{2a95}"),
    ("elsdot;", "\u{2a97}"), ("emacr;", "\u{113}"), ("empty;", "\u{2205}"), ("emptyset;", "\u{2205}"),
    ("emptyv;", "\u{2205}"), ("emsp13;", "\u{2004}"), ("emsp14;", "\u{2005}"), ("emsp;", "\u{2003}"),
    ("eng;", "\u{14b}"), ("ensp;", "\u{2002}"), ("eogon;", "\u{119}"), ("eopf;", "\u{1d556}"), ("epar;", "\u{22d5}"),
    ("eparsl;", "\u{29e3}"), ("eplus;", "\u{2a71}"), ("epsi;", "\u{3b5}"), ("epsilon;", "\u{3b5}"),
    ("epsiv;", "\u{3f5}"), ("eqcirc;", "\u{2256}"), ("eqcolon;", "\u{2255}"), ("eqsim;", "\u{2242}"),
    ("eqslantgtr;", "\u{2a96}"), ("eqslantless;", "\u{2a95}"), ("equals;", "="), ("equest;", "\u{225f}"),
    ("equiv;", "\u{2261}"), ("equivDD;", "\u{2a78}"), ("eqvparsl;", "\u{29e5}"), ("erDot;", "\u{2253}"),
    ("erarr;", "\u{2971}"), ("escr;", "\u{212f}"), ("esdot;", "\u{2250}"), ("esim;", "\u{2242}"), ("eta;", "\u{3b7}"),
    ("eth", "\u{f0}"), ("eth;", "\u{f0}"), ("euml", "\u{eb}"), ("euml;", "\u{eb}"), ("euro;", "\u{20ac}"),
    ("excl;", "!"), ("exist;", "\u{2203}"), ("expectation;", "\u{2130}"), ("exponentiale;", "\u{2147}"),
    ("fallingdotseq;", "\u{2252}"), ("fcy;", "\u{444}"), ("female;", "\u{2640}"), ("ffilig;", "\u{fb03}"),
    ("fflig;", "\u{fb00}"), ("ffllig;", "\u{fb04}"), ("ffr;", "\u{1d523}"), ("filig;", "\u{fb01}"), ("fjlig;", "fj"),
    ("flat;", "\u{266d}"), ("fllig;", "\u{fb02}"), ("fltns;", "\u{25b1}"), ("fnof;", "\u{192}"),
    ("fopf;", "\u{1d557}"), ("forall;", "\u{2200}"), ("fork;", "\u{22d4}"), ("forkv;", "\u{2ad9}"),
    ("fpartint;", "\u{2a0d}"), ("frac12", "\u{bd}"), ("frac12;", "\u{bd}"), ("frac13;", "\u{2153}"),
    ("frac14", "\u{bc}"), ("frac14;", "\u{bc}"), ("frac15;", "\u{2155}"), ("frac16;", "\u{2159}"),
    ("frac18;", "\u{215b}"), ("frac23;", "\u{2154}"), ("frac25;", "\u{2156}"), ("frac34", "\u{be}"),
    ("frac34;", "\u{be}"), ("frac35;", "\u{2157}"), ("frac38;", "\u{215c}"), ("frac45;", "\u{2158}"),
    ("frac56;", "\u{215a}"), ("frac58;", "\u{215d}"), ("frac78;", "\u{215e}"), ("frasl;", "\u{2044}"),
    ("frown;", "\u{2322}"), ("fscr;", "\u{1d4bb}"), ("gE;", "\u{2267}"), ("gEl;", "\u{2a8c}"), ("gacute;", "\u{1f5}"),
    ("gamma;", "\u{3b3}"), ("gammad;", "\u{3dd}"), ("gap;", "\u{2a86}"), ("gbreve;", "\u{11f}"),
    ("gcirc;", "\u{11d}"), ("gcy;", "\u{433}"), ("gdot;", "\u{121}"), ("ge;", "\u{2265}"), ("gel;", "\u{22db}"),
    ("geq;", "\u{2265}"), ("geqq;", "\u{2267}"), ("geqslant;", "\u{2a7e}"), ("ges;", "\u{2a7e}"),
    ("gescc;", "\u{2aa9}"), ("gesdot;", "\u{2a80}"), ("gesdoto;", "\u{2a82}"), ("gesdotol;", "\u{2a84}"),
    ("gesl;", "\u{22db}\u{fe00}"), ("gesles;", "\u{2a94}"), ("gfr;", "\u{1d524}"), ("gg;", "\u{226b}"),
    ("ggg;", "\u{22d9}"), ("gimel;", "\u{2137}"), ("gjcy;", "\u{453}"), ("gl;", "\u{2277}"), ("glE;", "\u{2a92}"),
    ("gla;", "\u{2aa5}"), ("glj;", "\u{2aa4}"), ("gnE;", "\u{2269}"), ("gnap;", "\u{2a8a}"),
    ("gnapprox;", "\u{2a8a}"), ("gne;", "\u{2a88}"), ("gneq;", "\u{2a88}"), ("gneqq;", "\u{2269}"),
    ("gnsim;", "\u{22e7}"), ("gopf;", "\u{1d558}"), ("grave;", "`"), ("gscr;", "\u{210a}"), ("gsim;", "\u{2273}"),
    ("gsime;", "\u{2a8e}"), ("gsiml;", "\u{2a90}"), ("gt", ">"), ("gt;", ">"), ("gtcc;", "\u{2aa7}"),
    ("gtcir;", "\u{2a7a}"), ("gtdot;", "\u{22d7}"), ("gtlPar;", "\u{2995}"), ("gtquest;", "\u{2a7c}"),
    ("gtrapprox;", "\u{2a86}"), ("gtrarr;", "\u{2978}"), ("gtrdot;", "\u{22d7}"), ("gtreqless;", "\u{22db}"),
    ("gtreqqless;", "\u{2a8c}"), ("gtrless;", "\u{2277}"), ("gtrsim;", "\u{2273}"),
    ("gvertneqq;", "\u{2269}\u{fe00}"), ("gvnE;", "\u{2269}\u{fe00}"), ("hArr;", "\u{21d4}"), ("hairsp;", "\u{200a}"),
    ("half;", "\u{bd}"), ("hamilt;", "\u{210b}"), ("hardcy;", "\u{44a}"), ("harr;", "\u{2194}"),
    ("harrcir;", "\u{2948}"), ("harrw;", "\u{21ad}"), ("hbar;", "\u{210f}"), ("hcirc;", "\u{125}"),
    ("hearts;", "\u{2665}"), ("heartsuit;", "\u{2665}"), ("hellip;", "\u{2026}"), ("hercon;", "\u{22b9}"),
    ("hfr;", "\u{1d525}"), ("hksearow;", "\u{2925}"), ("hkswarow;", "\u{2926}"), ("hoarr;", "\u{21ff}"),
    ("homtht;", "\u{223b}"), ("hookleftarrow;", "\u{21a9}"), ("hookrightarrow;", "\u{21aa}"), ("hopf;", "\u{1d559}"),
    ("horbar;", "\u{2015}"), ("hscr;", "\u{1d4bd}"), ("hslash;", "\u{210f}"), ("hstrok;", "\u{127}"),
    ("hybull;", "\u{2043}"), ("hyphen;", "\u{2010}"), ("iacute", "\u{ed}"), ("iacute;", "\u{ed}"),
    ("ic;", "\u{2063}"), ("icirc", "\u{ee}"), ("icirc;", "\u{ee}"), ("icy;", "\u{438}"), ("iecy;", "\u{435}"),
    ("iexcl", "\u{a1}"), ("iexcl;", "\u{a1}"), ("iff;", "\u{21d4}"), ("ifr;", "\u{1d526}"), ("igrave", "\u{ec}"),
    ("igrave;", "\u{ec}"), ("ii;", "\u{2148}"), ("iiiint;", "\u{2a0c}"), ("iiint;", "\u{222d}"),
    ("iinfin;", "\u{29dc}"), ("iiota;", "\u{2129}"), ("ijlig;", "\u{133}"), ("imacr;", "\u{12b}"),
    ("image;", "\u{2111}"), ("imagline;", "\u{2110}"), ("imagpart;", "\u{2111}"), ("imath;", "\u{131}"),
    ("imof;", "\u{22b7}"), ("imped;", "\u{1b5}"), ("in;", "\u{2208}"), ("incare;", "\u{2105}"),
    ("infin;", "\u{221e}"), ("infintie;", "\u{29dd}"), ("inodot;", "\u{131}"), ("int;", "\u{222b}"),
    ("intcal;", "\u{22ba}"), ("integers;", "\u{2124}"), ("intercal;", "\u{22ba}"), ("intlarhk;", "\u{2a17}"),
    ("intprod;", "\u{2a3c}"), ("iocy;", "\u{451}"), ("iogon;", "\u{12f}"), ("iopf;", "\u{1d55a}"),
    ("iota;", "\u{3b9}"), ("iprod;", "\u{2a3c}"), ("iquest", "\u{bf}"), ("iquest;", "\u{bf}"), ("iscr;", "\u{1d4be}"),
    ("isin;", "\u{2208}"), ("isinE;", "\u{22f9}"), ("isindot;", "\u{22f5}"), ("isins;", "\u{22f4}"),
    ("isinsv;", "\u{22f3}"), ("isinv;", "\u{2208}"), ("it;", "\u{2062}"), ("itilde;", "\u{129}"),
    ("iukcy;", "\u{456}"), ("iuml", "\u{ef}"), ("iuml;", "\u{ef}"), ("jcirc;", "\u{135}"), ("jcy;", "\u{439}"),
    ("jfr;", "\u{1d527}"), ("jmath;", "\u{237}"), ("jopf;", "\u{1d55b}"), ("jscr;", "\u{1d4bf}"),
    ("jsercy;", "\u{458}"), ("jukcy;", "\u{454}"), ("kappa;", "\u{3ba}"), ("kappav;", "\u{3f0}"),
    ("kcedil;", "\u{137}"), ("kcy;", "\u{43a}"), ("kfr;", "\u{1d528}"), ("kgreen;", "\u{138}"), ("khcy;", "\u{445}"),
    ("kjcy;", "\u{45c}"), ("kopf;", "\u{1d55c}"), ("kscr;", "\u{1d4c0}"), ("lAarr;", "\u{21da}"),
    ("lArr;", "\u{21d0}"), ("lAtail;", "\u{291b}"), ("lBarr;", "\u{290e}"), ("lE;", "\u{2266}"), ("lEg;", "\u{2a8b}"),
    ("lHar;", "\u{2962}"), ("lacute;", "\u{13a}"), ("laemptyv;", "\u{29b4}"), ("lagran;", "\u{2112}"),
    ("lambda;", "\u{3bb}"), ("lang;", "\u{27e8}"), ("langd;", "\u{2991}"), ("langle;", "\u{27e8}"),
    ("lap;", "\u{2a85}"), ("laquo", "\u{ab}"), ("laquo;", "\u{ab}"), ("larr;", "\u{2190}"), ("larrb;", "\u{21e4}"),
    ("larrbfs;", "\u{291f}"), ("larrfs;", "\u{291d}"), ("larrhk;", "\u{21a9}"), ("larrlp;", "\u{21ab}"),
    ("larrpl;", "\u{2939}"), ("larrsim;", "\u{2973}"), ("larrtl;", "\u{21a2}"), ("lat;", "\u{2aab}"),
    ("latail;", "\u{2919}"), ("late;", "\u{2aad}"), ("lates;", "\u{2aad}\u{fe00}"), ("lbarr;", "\u{290c}"),
    ("lbbrk;", "\u{2772}"), ("lbrace;", "{"), ("lbrack;", "["), ("lbrke;", "\u{298b}"), ("lbrksld;", "\u{298f}"),
    ("lbrkslu;", "\u{298d}"), ("lcaron;", "\u{13e}"), ("lcedil;", "\u{13c}"), ("lceil;", "\u{2308}"), ("lcub;", "{"),
    ("lcy;", "\u{43b}"), ("ldca;", "\u{2936}"), ("ldquo;", "\u{201c}"), ("ldquor;", "\u{201e}"),
    ("ldrdhar;", "\u{2967}"), ("ldrushar;", "\u{294b}"), ("ldsh;", "\u{21b2}"), ("le;", "\u{2264}"),
    ("leftarrow;", "\u{2190}"), ("leftarrowtail;", "\u{21a2}"), ("leftharpoondown;", "\u{21bd}"),
    ("leftharpoonup;", "\u{21bc}"), ("leftleftarrows;", "\u{21c7}"), ("leftrightarrow;", "\u{2194}"),
    ("leftrightarrows;", "\u{21c6}"), ("leftrightharpoons;", "\u{21cb}"), ("leftrightsquigarrow;", "\u{21ad}"),
    ("leftthreetimes;", "\u{22cb}"), ("leg;", "\u{22da}"), ("leq;", "\u{2264}"), ("leqq;", "\u{2266}"),
    ("leqslant;", "\u{2a7d}"), ("les;", "\u{2a7d}"), ("lescc;", "\u{2aa8}"), ("lesdot;", "\u{2a7f}"),
    ("lesdoto;", "\u{2a81}"), ("lesdotor;", "\u{2a83}"), ("lesg;", "\u{22da}\u{fe00}"), ("lesges;", "\u{2a93}"),
    ("lessapprox;", "\u{2a85}"), ("lessdot;", "\u{22d6}"), ("lesseqgtr;", "\u{22da}"), ("lesseqqgtr;", "\u{2a8b}"),
    ("lessgtr;", "\u{2276}"), ("lesssim;", "\u{2272}"), ("lfisht;", "\u{297c}"), ("lfloor;", "\u{230a}"),
    ("lfr;", "\u{1d529}"), ("lg;", "\u{2276}"), ("lgE;", "\u{2a91}"), ("lhard;", "\u{21bd}"), ("lharu;", "\u{21bc}"),
    ("lharul;", "\u{296a}"), ("lhblk;", "\u{2584}"), ("ljcy;", "\u{459}"), ("ll;", "\u{226a}"),
    ("llarr;", "\u{21c7}"), ("llcorner;", "\u{231e}"), ("llhard;", "\u{296b}"), ("lltri;", "\u{25fa}"),
    ("lmidot;", "\u{140}"), ("lmoust;", "\u{23b0}"), ("lmoustache;", "\u{23b0}"), ("lnE;", "\u{2268}"),
    ("lnap;", "\u{2a89}"), ("lnapprox;", "\u{2a89}"), ("lne;", "\u{2a87}"), ("lneq;", "\u{2a87}"),
    ("lneqq;", "\u{2268}"), ("lnsim;", "\u{22e6}"), ("loang;", "\u{27ec}"), ("loarr;", "\u{21fd}"),
    ("lobrk;", "\u{27e6}"), ("longleftarrow;", "\u{27f5}"), ("longleftrightarrow;", "\u{27f7}"),
    ("longmapsto;", "\u{27fc}"), ("longrightarrow;", "\u{27f6}"), ("looparrowleft;", "\u{21ab}"),
    ("looparrowright;", "\u{21ac}"), ("lopar;", "\u{2985}"), ("lopf;", "\u{1d55d}"), ("loplus;", "\u{2a2d}"),
    ("lotimes;", "\u{2a34}"), ("lowast;", "\u{2217}"), ("lowbar;", "_"), ("loz;", "\u{25ca}"),
    ("lozenge;", "\u{25ca}"), ("lozf;", "\u{29eb}"), ("lpar;", "("), ("lparlt;", "\u{2993}"), ("lrarr;", "\u{21c6}"),
    ("lrcorner;", "\u{231f}"), ("lrhar;", "\u{21cb}"), ("lrhard;", "\u{296d}"), ("lrm;", "\u{200e}"),
    ("lrtri;", "\u{22bf}"), ("lsaquo;", "\u{2039}"), ("lscr;", "\u{1d4c1}"), ("lsh;", "\u{21b0}"),
    ("lsim;", "\u{2272}"), ("lsime;", "\u{2a8d}"), ("lsimg;", "\u{2a8f}"), ("lsqb;", "["), ("lsquo;", "\u{2018}"),
    ("lsquor;", "\u{201a}"), ("lstrok;", "\u{142}"), ("lt", "<"), ("lt;", "<"), ("ltcc;", "\u{2aa6}"),
    ("ltcir;", "\u{2a79}"), ("ltdot;", "\u{22d6}"), ("lthree;", "\u{22cb}"), ("ltimes;", "\u{22c9}"),
    ("ltlarr;", "\u{2976}"), ("ltquest;", "\u{2a7b}"), ("ltrPar;", "\u{2996}"), ("ltri;", "\u{25c3}"),
    ("ltrie;", "\u{22b4}"), ("ltrif;", "\u{25c2}"), ("lurdshar;", "\u{294a}"), ("luruhar;", "\u{2966}"),
    ("lvertneqq;", "\u{2268}\u{fe00}"), ("lvnE;", "\u{2268}\u{fe00}"), ("mDDot;", "\u{223a}"), ("macr", "\u{af}"),
    ("macr;", "\u{af}"), ("male;", "\u{2642}"), ("malt;", "\u{2720}"), ("maltese;", "\u{2720}"), ("map;", "\u{21a6}"),
    ("mapsto;", "\u{21a6}"), ("mapstodown;", "\u{21a7}"), ("mapstoleft;", "\u{21a4}"), ("mapstoup;", "\u{21a5}"),
    ("marker;", "\u{25ae}"), ("mcomma;", "\u{2a29}"), ("mcy;", "\u{43c}"), ("mdash;", "\u{2014}"),
    ("measuredangle;", "\u{2221}"), ("mfr;", "\u{1d52a}"), ("mho;", "\u{2127}"), ("micro", "\u{b5}"),
    ("micro;", "\u{b5}"), ("mid;", "\u{2223}"), ("midast;", "*"), ("midcir;", "\u{2af0}"), ("middot", "\u{b7}"),
    ("middot;", "\u{b7}"), ("minus;", "\u{2212}"), ("minusb;", "\u{229f}"), ("minusd;", "\u{2238}"),
    ("minusdu;", "\u{2a2a}"), ("mlcp;", "\u{2adb}"), ("mldr;", "\u{2026}"), ("mnplus;", "\u{2213}"),
    ("models;", "\u{22a7}"), ("mopf;", "\u{1d55e}"), ("mp;", "\u{2213}"), ("mscr;", "\u{1d4c2}"),
    ("mstpos;", "\u{223e}"), ("mu;", "\u{3bc}"), ("multimap;", "\u{22b8}"), ("mumap;", "\u{22b8}"),
    ("nGg;", "\u{22d9}\u{338}"), ("nGt;", "\u{226b}\u{20d2}"), ("nGtv;", "\u{226b}\u{338}"),
    ("nLeftarrow;", "\u{21cd}"), ("nLeftrightarrow;", "\u{21ce}"), ("nLl;", "\u{22d8}\u{338}"),
    ("nLt;", "\u{226a}\u{20d2}"), ("nLtv;", "\u{226a}\u{338}"), ("nRightarrow;", "\u{21cf}"), ("nVDash;", "\u{22af}"),
    ("nVdash;", "\u{22ae}"), ("nabla;", "\u{2207}"), ("nacute;", "\u{144}"), ("nang;", "\u{2220}\u{20d2}"),
    ("nap;", "\u{2249}"), ("napE;", "\u{2a70}\u{338}"), ("napid;", "\u{224b}\u{338}"), ("napos;", "\u{149}"),
    ("napprox;", "\u{2249}"), ("natur;", "\u{266e}"), ("natural;", "\u{266e}"), ("naturals;", "\u{2115}"),
    ("nbsp", "\u{a0}"), ("nbsp;", "\u{a0}"), ("nbump;", "\u{224e}\u{338}"), ("nbumpe;", "\u{224f}\u{338}"),
    ("ncap;", "\u{2a43}"), ("ncaron;", "\u{148}"), ("ncedil;", "\u{146}"), ("ncong;", "\u{2247}"),
    ("ncongdot;", "\u{2a6d}\u{338}"), ("ncup;", "\u{2a42}"), ("ncy;", "\u{43d}"), ("ndash;", "\u{2013}"),
    ("ne;", "\u{2260}"), ("neArr;", "\u{21d7}"), ("nearhk;", "\u{2924}"), ("nearr;", "\u{2197}"),
    ("nearrow;", "\u{2197}"), ("nedot;", "\u{2250}\u{338}"), ("nequiv;", "\u{2262}"), ("nesear;", "\u{2928}"),
    ("nesim;", "\u{2242}\u{338}"), ("nexist;", "\u{2204}"), ("nexists;", "\u{2204}"), ("nfr;", "\u{1d52b}"),
    ("ngE;", "\u{2267}\u{338}"), ("nge;", "\u{2271}"), ("ngeq;", "\u{2271}"), ("ngeqq;", "\u{2267}\u{338}"),
    ("ngeqslant;", "\u{2a7e}\u{338}"), ("nges;", "\u{2a7e}\u{338}"), ("ngsim;", "\u{2275}"), ("ngt;", "\u{226f}"),
    ("ngtr;", "\u{226f}"), ("nhArr;", "\u{21ce}"), ("nharr;", "\u{21ae}"), ("nhpar;", "\u{2af2}"),
    ("ni;", "\u{220b}"), ("nis;", "\u{22fc}"), ("nisd;", "\u{22fa}"), ("niv;", "\u{220b}"), ("njcy;", "\u{45a}"),
    ("nlArr;", "\u{21cd}"), ("nlE;", "\u{2266}\u{338}"), ("nlarr;", "\u{219a}"), ("nldr;", "\u{2025}"),
    ("nle;", "\u{2270}"), ("nleftarrow;", "\u{219a}"), ("nleftrightarrow;", "\u{21ae}"), ("nleq;", "\u{2270}"),
    ("nleqq;", "\u{2266}\u{338}"), ("nleqslant;", "\u{2a7d}\u{338}"), ("nles;", "\u{2a7d}\u{338}"),
    ("nless;", "\u{226e}"), ("nlsim;", "\u{2274}"), ("nlt;", "\u{226e}"), ("nltri;", "\u{22ea}"),
    ("nltrie;", "\u{22ec}"), ("nmid;", "\u{2224}"), ("nopf;", "\u{1d55f}"), ("not", "\u{ac}"), ("not;", "\u{ac}"),
    ("notin;", "\u{2209}"), ("notinE;", "\u{22f9}\u{338}"), ("notindot;", "\u{22f5}\u{338}"),
    ("notinva;", "\u{2209}"), ("notinvb;", "\u{22f7}"), ("notinvc;", "\u{22f6}"), ("notni;", "\u{220c}"),
    ("notniva;", "\u{220c}"), ("notnivb;", "\u{22fe}"), ("notnivc;", "\u{22fd}"), ("npar;", "\u{2226}"),
    ("nparallel;", "\u{2226}"), ("nparsl;", "\u{2afd}\u{20e5}"), ("npart;", "\u{2202}\u{338}"),
    ("npolint;", "\u{2a14}"), ("npr;", "\u{2280}"), ("nprcue;", "\u{22e0}"), ("npre;", "\u{2aaf}\u{338}"),
    ("nprec;", "\u{2280}"), ("npreceq;", "\u{2aaf}\u{338}"), ("nrArr;", "\u{21cf}"), ("nrarr;", "\u{219b}"),
    ("nrarrc;", "\u{2933}\u{338}"), ("nrarrw;", "\u{219d}\u{338}"), ("nrightarrow;", "\u{219b}"),
    ("nrtri;", "\u{22eb}"), ("nrtrie;", "\u{22ed}"), ("nsc;", "\u{2281}"), ("nsccue;", "\u{22e1}"),
    ("nsce;", "\u{2ab0}\u{338}"), ("nscr;", "\u{1d4c3}"), ("nshortmid;", "\u{2224}"), ("nshortparallel;", "\u{2226}"),
    ("nsim;", "\u{2241}"), ("nsime;", "\u{2244}"), ("nsimeq;", "\u{2244}"), ("nsmid;", "\u{2224}"),
    ("nspar;", "\u{2226}"), ("nsqsube;", "\u{22e2}"), ("nsqsupe;", "\u{22e3}"), ("nsub;", "\u{2284}"),
    ("nsubE;", "\u{2ac5}\u{338}"), ("nsube;", "\u{2288}"), ("nsubset;", "\u{2282}\u{20d2}"),
    ("nsubseteq;", "\u{2288}"), ("nsubseteqq;", "\u{2ac5}\u{338}"), ("nsucc;", "\u{2281}"),
    ("nsucceq;", "\u{2ab0}\u{338}"), ("nsup;", "\u{2285}"), ("nsupE;", "\u{2ac6}\u{338}"), ("nsupe;", "\u{2289}"),
    ("nsupset;", "\u{2283}\u{20d2}"), ("nsupseteq;", "\u{2289}"), ("nsupseteqq;", "\u{2ac6}\u{338}"),
    ("ntgl;", "\u{2279}"), ("ntilde", "\u{f1}"), ("ntilde;", "\u{f1}"), ("ntlg;", "\u{2278}"),
    ("ntriangleleft;", "\u{22ea}"), ("ntrianglelefteq;", "\u{22ec}"), ("ntriangleright;", "\u{22eb}"),
    ("ntrianglerighteq;", "\u{22ed}"), ("nu;", "\u{3bd}"), ("num;", "#"), ("numero;", "\u{2116}"),
    ("numsp;", "\u{2007}"), ("nvDash;", "\u{22ad}"), ("nvHarr;", "\u{2904}"), ("nvap;", "\u{224d}\u{20d2}"),
    ("nvdash;", "\u{22ac}"), ("nvge;", "\u{2265}\u{20d2}"), ("nvgt;", ">\u{20d2}"), ("nvinfin;", "\u{29de}"),
    ("nvlArr;", "\u{2902}"), ("nvle;", "\u{2264}\u{20d2}"), ("nvlt;", "<\u{20d2}"), ("nvltrie;", "\u{22b4}\u{20d2}"),
    ("nvrArr;", "\u{2903}"), ("nvrtrie;", "\u{22b5}\u{20d2}"), ("nvsim;", "\u{223c}\u{20d2}"), ("nwArr;", "\u{21d6}"),
    ("nwarhk;", "\u{2923}"), ("nwarr;", "\u{2196}"), ("nwarrow;", "\u{2196}"), ("nwnear;", "\u{2927}"),
    ("oS;", "\u{24c8}"), ("oacute", "\u{f3}"), ("oacute;", "\u{f3}"), ("oast;", "\u{229b}"), ("ocir;", "\u{229a}"),
    ("ocirc", "\u{f4}"), ("ocirc;", "\u{f4}"), ("ocy;", "\u{43e}"), ("odash;", "\u{229d}"), ("odblac;", "\u{151}"),
    ("odiv;", "\u{2a38}"), ("odot;", "\u{2299}"), ("odsold;", "\u{29bc}"), ("oelig;", "\u{153}"),
    ("ofcir;", "\u{29bf}"), ("ofr;", "\u{1d52c}"), ("ogon;", "\u{2db}"), ("ograve", "\u{f2}"), ("ograve;", "\u{f2}"),
    ("ogt;", "\u{29c1}"), ("ohbar;", "\u{29b5}"), ("ohm;", "\u{3a9}"), ("oint;", "\u{222e}"), ("olarr;", "\u{21ba}"),
    ("olcir;", "\u{29be}"), ("olcross;", "\u{29bb}"), ("oline;", "\u{203e}"), ("olt;", "\u{29c0}"),
    ("omacr;", "\u{14d}"), ("omega;", "\u{3c9}"), ("omicron;", "\u{3bf}"), ("omid;", "\u{29b6}"),
    ("ominus;", "\u{2296}"), ("oopf;", "\u{1d560}"), ("opar;", "\u{29b7}"), ("operp;", "\u{29b9}"),
    ("oplus;", "\u{2295}"), ("or;", "\u{2228}"), ("orarr;", "\u{21bb}"), ("ord;", "\u{2a5d}"), ("order;", "\u{2134}"),
    ("orderof;", "\u{2134}"), ("ordf", "\u{aa}"), ("ordf;", "\u{aa}"), ("ordm", "\u{ba}"), ("ordm;", "\u{ba}"),
    ("origof;", "\u{22b6}"), ("oror;", "\u{2a56}"), ("orslope;", "\u{2a57}"), ("orv;", "\u{2a5b}"),
    ("oscr;", "\u{2134}"), ("oslash", "\u{f8}"), ("oslash;", "\u{f8}"), ("osol;", "\u{2298}"), ("otilde", "\u{f5}"),
    ("otilde;", "\u{f5}"), ("otimes;", "\u{2297}"), ("otimesas;", "\u{2a36}"), ("ouml", "\u{f6}"),
    ("ouml;", "\u{f6}"), ("ovbar;", "\u{233d}"), ("par;", "\u{2225}"), ("para", "\u{b6}"), ("para;", "\u{b6}"),
    ("parallel;", "\u{2225}"), ("parsim;", "\u{2af3}"), ("parsl;", "\u{2afd}"), ("part;", "\u{2202}"),
    ("pcy;", "\u{43f}"), ("percnt;", "%"), ("period;", "."), ("permil;", "\u{2030}"), ("perp;", "\u{22a5}"),
    ("pertenk;", "\u{2031}"), ("pfr;", "\u{1d52d}"), ("phi;", "\u{3c6}"), ("phiv;", "\u{3d5}"),
    ("phmmat;", "\u{2133}"), ("phone;", "\u{260e}"), ("pi;", "\u{3c0}"), ("pitchfork;", "\u{22d4}"),
    ("piv;", "\u{3d6}"), ("planck;", "\u{210f}"), ("planckh;", "\u{210e}"), ("plankv;", "\u{210f}"), ("plus;", "+"),
    ("plusacir;", "\u{2a23}"), ("plusb;", "\u{229e}"), ("pluscir;", "\u{2a22}"), ("plusdo;", "\u{2214}"),
    ("plusdu;", "\u{2a25}"), ("pluse;", "\u{2a72}"), ("plusmn", "\u{b1}"), ("plusmn;", "\u{b1}"),
    ("plussim;", "\u{2a26}"), ("plustwo;", "\u{2a27}"), ("pm;", "\u{b1}"), ("pointint;", "\u{2a15}"),
    ("popf;", "\u{1d561}"), ("pound", "\u{a3}"), ("pound;", "\u{a3}"), ("pr;", "\u{227a}"), ("prE;", "\u{2ab3}"),
    ("prap;", "\u{2ab7}"), ("prcue;", "\u{227c}"), ("pre;", "\u{2aaf}"), ("prec;", "\u{227a}"),
    ("precapprox;", "\u{2ab7}"), ("preccurlyeq;", "\u{227c}"), ("preceq;", "\u{2aaf}"), ("precnapprox;", "\u{2ab9}"),
    ("precneqq;", "\u{2ab5}"), ("precnsim;", "\u{22e8}"), ("precsim;", "\u{227e}"), ("prime;", "\u{2032}"),
    ("primes;", "\u{2119}"), ("prnE;", "\u{2ab5}"), ("prnap;", "\u{2ab9}"), ("prnsim;", "\u{22e8}"),
    ("prod;", "\u{220f}"), ("profalar;", "\u{232e}"), ("profline;", "\u{2312}"), ("profsurf;", "\u{2313}"),
    ("prop;", "\u{221d}"), ("propto;", "\u{221d}"), ("prsim;", "\u{227e}"), ("prurel;", "\u{22b0}"),
    ("pscr;", "\u{1d4c5}"), ("psi;", "\u{3c8}"), ("puncsp;", "\u{2008}"), ("qfr;", "\u{1d52e}"),
    ("qint;", "\u{2a0c}"), ("qopf;", "\u{1d562}"), ("qprime;", "\u{2057}"), ("qscr;", "\u{1d4c6}"),
    ("quaternions;", "\u{210d}"), ("quatint;", "\u{2a16}"), ("quest;", "?"), ("questeq;", "\u{225f}"),
    ("quot", "\u{22}"), ("quot;", "\u{22}"), ("rAarr;", "\u{21db}"), ("rArr;", "\u{21d2}"), ("rAtail;", "\u{291c}"),
    ("rBarr;", "\u{290f}"), ("rHar;", "\u{2964}"), ("race;", "\u{223d}\u{331}"), ("racute;", "\u{155}"),
    ("radic;", "\u{221a}"), ("raemptyv;", "\u{29b3}"), ("rang;", "\u{27e9}"), ("rangd;", "\u{2992}"),
    ("range;", "\u{29a5}"), ("rangle;", "\u{27e9}"), ("raquo", "\u{bb}"), ("raquo;", "\u{bb}"), ("rarr;", "\u{2192}"),
    ("rarrap;", "\u{2975}"), ("rarrb;", "\u{21e5}"), ("rarrbfs;", "\u{2920}"), ("rarrc;", "\u{2933}"),
    ("rarrfs;", "\u{291e}"), ("rarrhk;", "\u{21aa}"), ("rarrlp;", "\u{21ac}"), ("rarrpl;", "\u{2945}"),
    ("rarrsim;", "\u{2974}"), ("rarrtl;", "\u{21a3}"), ("rarrw;", "\u{219d}"), ("ratail;", "\u{291a}"),
    ("ratio;", "\u{2236}"), ("rationals;", "\u{211a}"), ("rbarr;", "\u{290d}"), ("rbbrk;", "\u{2773}"),
    ("rbrace;", "}"), ("rbrack;", "]"), ("rbrke;", "\u{298c}"), ("rbrksld;", "\u{298e}"), ("rbrkslu;", "\u{2990}"),
    ("rcaron;", "\u{159}"), ("rcedil;", "\u{157}"), ("rceil;", "\u{2309}"), ("rcub;", "}"), ("rcy;", "\u{440}"),
    ("rdca;", "\u{2937}"), ("rdldhar;", "\u{2969}"), ("rdquo;", "\u{201d}"), ("rdquor;", "\u{201d}"),
    ("rdsh;", "\u{21b3}"), ("real;", "\u{211c}"), ("realine;", "\u{211b}"), ("realpart;", "\u{211c}"),
    ("reals;", "\u{211d}"), ("rect;", "\u{25ad}"), ("reg", "\u{ae}"), ("reg;", "\u{ae}"), ("rfisht;", "\u{297d}"),
    ("rfloor;", "\u{230b}"), ("rfr;", "\u{1d52f}"), ("rhard;", "\u{21c1}"), ("rharu;", "\u{21c0}"),
    ("rharul;", "\u{296c}"), ("rho;", "\u{3c1}"), ("rhov;", "\u{3f1}"), ("rightarrow;", "\u{2192}"),
    ("rightarrowtail;", "\u{21a3}"), ("rightharpoondown;", "\u{21c1}"), ("rightharpoonup;", "\u{21c0}"),
    ("rightleftarrows;", "\u{21c4}"), ("rightleftharpoons;", "\u{21cc}"), ("rightrightarrows;", "\u{21c9}"),
    ("rightsquigarrow;", "\u{219d}"), ("rightthreetimes;", "\u{22cc}"), ("ring;", "\u{2da}"),
    ("risingdotseq;", "\u{2253}"), ("rlarr;", "\u{21c4}"), ("rlhar;", "\u{21cc}"), ("rlm;", "\u{200f}"),
    ("rmoust;", "\u{23b1}"), ("rmoustache;", "\u{23b1}"), ("rnmid;", "\u{2aee}"), ("roang;", "\u{27ed}"),
    ("roarr;", "\u{21fe}"), ("robrk;", "\u{27e7}"), ("ropar;", "\u{2986}"), ("ropf;", "\u{1d563}"),
    ("roplus;", "\u{2a2e}"), ("rotimes;", "\u{2a35}"), ("rpar;", ")"), ("rpargt;", "\u{2994}"),
    ("rppolint;", "\u{2a12}"), ("rrarr;", "\u{21c9}"), ("rsaquo;", "\u{203a}"), ("rscr;", "\u{1d4c7}"),
    ("rsh;", "\u{21b1}"), ("rsqb;", "]"), ("rsquo;", "\u{2019}"), ("rsquor;", "\u{2019}"), ("rthree;", "\u{22cc}"),
    ("rtimes;", "\u{22ca}"), ("rtri;", "\u{25b9}"), ("rtrie;", "\u{22b5}"), ("rtrif;", "\u{25b8}"),
    ("rtriltri;", "\u{29ce}"), ("ruluhar;", "\u{2968}"), ("rx;", "\u{211e}"), ("sacute;", "\u{15b}"),
    ("sbquo;", "\u{201a}"), ("sc;", "\u{227b}"), ("scE;", "\u{2ab4}"), ("scap;", "\u{2ab8}"), ("scaron;", "\u{161}"),
    ("sccue;", "\u{227d}"), ("sce;", "\u{2ab0}"), ("scedil;", "\u{15f}"), ("scirc;", "\u{15d}"),
    ("scnE;", "\u{2ab6}"), ("scnap;", "\u{2aba}"), ("scnsim;", "\u{22e9}"), ("scpolint;", "\u{2a13}"),
    ("scsim;", "\u{227f}"), ("scy;", "\u{441}"), ("sdot;", "\u{22c5}"), ("sdotb;", "\u{22a1}"),
    ("sdote;", "\u{2a66}"), ("seArr;", "\u{21d8}"), ("searhk;", "\u{2925}"), ("searr;", "\u{2198}"),
    ("searrow;", "\u{2198}"), ("sect", "\u{a7}"), ("sect;", "\u{a7}"), ("semi;", ";"), ("seswar;", "\u{2929}"),
    ("setminus;", "\u{2216}"), ("setmn;", "\u{2216}"), ("sext;", "\u{2736}"), ("sfr;", "\u{1d530}"),
    ("sfrown;", "\u{2322}"), ("sharp;", "\u{266f}"), ("shchcy;", "\u{449}"), ("shcy;", "\u{448}"),
    ("shortmid;", "\u{2223}"), ("shortparallel;", "\u{2225}"), ("shy", "\u{ad}"), ("shy;", "\u{ad}"),
    ("sigma;", "\u{3c3}"), ("sigmaf;", "\u{3c2}"), ("sigmav;", "\u{3c2}"), ("sim;", "\u{223c}"),
    ("simdot;", "\u{2a6a}"), ("sime;", "\u{2243}"), ("simeq;", "\u{2243}"), ("simg;", "\u{2a9e}"),
    ("simgE;", "\u{2aa0}"), ("siml;", "\u{2a9d}"), ("simlE;", "\u{2a9f}"), ("simne;", "\u{2246}"),
    ("simplus;", "\u{2a24}"), ("simrarr;", "\u{2972}"), ("slarr;", "\u{2190}"), ("smallsetminus;", "\u{2216}"),
    ("smashp;", "\u{2a33}"), ("smeparsl;", "\u{29e4}"), ("smid;", "\u{2223}"), ("smile;", "\u{2323}"),
    ("smt;", "\u{2aaa}"), ("smte;", "\u{2aac}"), ("smtes;", "\u{2aac}\u{fe00}"), ("softcy;", "\u{44c}"),
    ("sol;", "/"), ("solb;", "\u{29c4}"), ("solbar;", "\u{233f}"), ("sopf;", "\u{1d564}"), ("spades;", "\u{2660}"),
    ("spadesuit;", "\u{2660}"), ("spar;", "\u{2225}"), ("sqcap;", "\u{2293}"), ("sqcaps;", "\u{2293}\u{fe00}"),
    ("sqcup;", "\u{2294}"), ("sqcups;", "\u{2294}\u{fe00}"), ("sqsub;", "\u{228f}"), ("sqsube;", "\u{2291}"),
    ("sqsubset;", "\u{228f}"), ("sqsubseteq;", "\u{2291}"), ("sqsup;", "\u{2290}"), ("sqsupe;", "\u{2292}"),
    ("sqsupset;", "\u{2290}"), ("sqsupseteq;", "\u{2292}"), ("squ;", "\u{25a1}"), ("square;", "\u{25a1}"),
    ("squarf;", "\u{25aa}"), ("squf;", "\u{25aa}"), ("srarr;", "\u{2192}"), ("sscr;", "\u{1d4c8}"),
    ("ssetmn;", "\u{2216}"), ("ssmile;", "\u{2323}"), ("sstarf;", "\u{22c6}"), ("star;", "\u{2606}"),
    ("starf;", "\u{2605}"), ("straightepsilon;", "\u{3f5}"), ("straightphi;", "\u{3d5}"), ("strns;", "\u{af}"),
    ("sub;", "\u{2282}"), ("subE;", "\u{2ac5}"), ("subdot;", "\u{2abd}"), ("sube;", "\u{2286}"),
    ("subedot;", "\u{2ac3}"), ("submult;", "\u{2ac1}"), ("subnE;", "\u{2acb}"), ("subne;", "\u{228a}"),
    ("subplus;", "\u{2abf}"), ("subrarr;", "\u{2979}"), ("subset;", "\u{2282}"), ("subseteq;", "\u{2286}"),
    ("subseteqq;", "\u{2ac5}"), ("subsetneq;", "\u{228a}"), ("subsetneqq;", "\u{2acb}"), ("subsim;", "\u{2ac7}"),
    ("subsub;", "\u{2ad5}"), ("subsup;", "\u{2ad3}"), ("succ;", "\u{227b}"), ("succapprox;", "\u{2ab8}"),
    ("succcurlyeq;", "\u{227d}"), ("succeq;", "\u{2ab0}"), ("succnapprox;", "\u{2aba}"), ("succneqq;", "\u{2ab6}"),
    ("succnsim;", "\u{22e9}"), ("succsim;", "\u{227f}"), ("sum;", "\u{2211}"), ("sung;", "\u{266a}"),
    ("sup1", "\u{b9}"), ("sup1;", "\u{b9}"), ("sup2", "\u{b2}"), ("sup2;", "\u{b2}"), ("sup3", "\u{b3}"),
    ("sup3;", "\u{b3}"), ("sup;", "\u{2283}"), ("supE;", "\u{2ac6}"), ("supdot;", "\u{2abe}"),
    ("supdsub;", "\u{2ad8}"), ("supe;", "\u{2287}"), ("supedot;", "\u{2ac4}"), ("suphsol;", "\u{27c9}"),
    ("suphsub;", "\u{2ad7}"), ("suplarr;", "\u{297b}"), ("supmult;", "\u{2ac2}"), ("supnE;", "\u{2acc}"),
    ("supne;", "\u{228b}"), ("supplus;", "\u{2ac0}"), ("supset;", "\u{2283}"), ("supseteq;", "\u{2287}"),
    ("supseteqq;", "\u{2ac6}"), ("supsetneq;", "\u{228b}"), ("supsetneqq;", "\u{2acc}"), ("supsim;", "\u{2ac8}"),
    ("supsub;", "\u{2ad4}"), ("supsup;", "\u{2ad6}"), ("swArr;", "\u{21d9}"), ("swarhk;", "\u{2926}"),
    ("swarr;", "\u{2199}"), ("swarrow;", "\u{2199}"), ("swnwar;", "\u{292a}"), ("szlig", "\u{df}"),
    ("szlig;", "\u{df}"), ("target;", "\u{2316}"), ("tau;", "\u{3c4}"), ("tbrk;", "\u{23b4}"), ("tcaron;", "\u{165}"),
    ("tcedil;", "\u{163}"), ("tcy;", "\u{442}"), ("tdot;", "\u{20db}"), ("telrec;", "\u{2315}"),
    ("tfr;", "\u{1d531}"), ("there4;", "\u{2234}"), ("therefore;", "\u{2234}"), ("theta;", "\u{3b8}"),
    ("thetasym;", "\u{3d1}"), ("thetav;", "\u{3d1}"), ("thickapprox;", "\u{2248}"), ("thicksim;", "\u{223c}"),
    ("thinsp;", "\u{2009}"), ("thkap;", "\u{2248}"), ("thksim;", "\u{223c}"), ("thorn", "\u{fe}"),
    ("thorn;", "\u{fe}"), ("tilde;", "\u{2dc}"), ("times", "\u{d7}"), ("times;", "\u{d7}"), ("timesb;", "\u{22a0}"),
    ("timesbar;", "\u{2a31}"), ("timesd;", "\u{2a30}"), ("tint;", "\u{222d}"), ("toea;", "\u{2928}"),
    ("top;", "\u{22a4}"), ("topbot;", "\u{2336}"), ("topcir;", "\u{2af1}"), ("topf;", "\u{1d565}"),
    ("topfork;", "\u{2ada}"), ("tosa;", "\u{2929}"), ("tprime;", "\u{2034}"), ("trade;", "\u{2122}"),
    ("triangle;", "\u{25b5}"), ("triangledown;", "\u{25bf}"), ("triangleleft;", "\u{25c3}"),
    ("trianglelefteq;", "\u{22b4}"), ("triangleq;", "\u{225c}"), ("triangleright;", "\u{25b9}"),
    ("trianglerighteq;", "\u{22b5}"), ("tridot;", "\u{25ec}"), ("trie;", "\u{225c}"), ("triminus;", "\u{2a3a}"),
    ("triplus;", "\u{2a39}"), ("trisb;", "\u{29cd}"), ("tritime;", "\u{2a3b}"), ("trpezium;", "\u{23e2}"),
    ("tscr;", "\u{1d4c9}"), ("tscy;", "\u{446}"), ("tshcy;", "\u{45b}"), ("tstrok;", "\u{167}"),
    ("twixt;", "\u{226c}"), ("twoheadleftarrow;", "\u{219e}"), ("twoheadrightarrow;", "\u{21a0}"),
    ("uArr;", "\u{21d1}"), ("uHar;", "\u{2963}"), ("uacute", "\u{fa}"), ("uacute;", "\u{fa}"), ("uarr;", "\u{2191}"),
    ("ubrcy;", "\u{45e}"), ("ubreve;", "\u{16d}"), ("ucirc", "\u{fb}"), ("ucirc;", "\u{fb}"), ("ucy;", "\u{443}"),
    ("udarr;", "\u{21c5}"), ("udblac;", "\u{171}"), ("udhar;", "\u{296e}"), ("ufisht;", "\u{297e}"),
    ("ufr;", "\u{1d532}"), ("ugrave", "\u{f9}"), ("ugrave;", "\u{f9}"), ("uharl;", "\u{21bf}"),
    ("uharr;", "\u{21be}"), ("uhblk;", "\u{2580}"), ("ulcorn;", "\u{231c}"), ("ulcorner;", "\u{231c}"),
    ("ulcrop;", "\u{230f}"), ("ultri;", "\u{25f8}"), ("umacr;", "\u{16b}"), ("uml", "\u{a8}"), ("uml;", "\u{a8}"),
    ("uogon;", "\u{173}"), ("uopf;", "\u{1d566}"), ("uparrow;", "\u{2191}"), ("updownarrow;", "\u{2195}"),
    ("upharpoonleft;", "\u{21bf}"), ("upharpoonright;", "\u{21be}"), ("uplus;", "\u{228e}"), ("upsi;", "\u{3c5}"),
    ("upsih;", "\u{3d2}"), ("upsilon;", "\u{3c5}"), ("upuparrows;", "\u{21c8}"), ("urcorn;", "\u{231d}"),
    ("urcorner;", "\u{231d}"), ("urcrop;", "\u{230e}"), ("uring;", "\u{16f}"), ("urtri;", "\u{25f9}"),
    ("uscr;", "\u{1d4ca}"), ("utdot;", "\u{22f0}"), ("utilde;", "\u{169}"), ("utri;", "\u{25b5}"),
    ("utrif;", "\u{25b4}"), ("uuarr;", "\u{21c8}"), ("uuml", "\u{fc}"), ("uuml;", "\u{fc}"), ("uwangle;", "\u{29a7}"),
    ("vArr;", "\u{21d5}"), ("vBar;", "\u{2ae8}"), ("vBarv;", "\u{2ae9}"), ("vDash;", "\u{22a8}"),
    ("vangrt;", "\u{299c}"), ("varepsilon;", "\u{3f5}"), ("varkappa;", "\u{3f0}"), ("varnothing;", "\u{2205}"),
    ("varphi;", "\u{3d5}"), ("varpi;", "\u{3d6}"), ("varpropto;", "\u{221d}"), ("varr;", "\u{2195}"),
    ("varrho;", "\u{3f1}"), ("varsigma;", "\u{3c2}"), ("varsubsetneq;", "\u{228a}\u{fe00}"),
    ("varsubsetneqq;", "\u{2acb}\u{fe00}"), ("varsupsetneq;", "\u{228b}\u{fe00}"),
    ("varsupsetneqq;", "\u{2acc}\u{fe00}"), ("vartheta;", "\u{3d1}"), ("vartriangleleft;", "\u{22b2}"),
    ("vartriangleright;", "\u{22b3}"), ("vcy;", "\u{432}"), ("vdash;", "\u{22a2}"), ("vee;", "\u{2228}"),
    ("veebar;", "\u{22bb}"), ("veeeq;", "\u{225a}"), ("vellip;", "\u{22ee}"), ("verbar;", "|"), ("vert;", "|"),
    ("vfr;", "\u{1d533}"), ("vltri;", "\u{22b2}"), ("vnsub;", "\u{2282}\u{20d2}"), ("vnsup;", "\u{2283}\u{20d2}"),
    ("vopf;", "\u{1d567}"), ("vprop;", "\u{221d}"), ("vrtri;", "\u{22b3}"), ("vscr;", "\u{1d4cb}"),
    ("vsubnE;", "\u{2acb}\u{fe00}"), ("vsubne;", "\u{228a}\u{fe00}"), ("vsupnE;", "\u{2acc}\u{fe00}"),
    ("vsupne;", "\u{228b}\u{fe00}"), ("vzigzag;", "\u{299a}"), ("wcirc;", "\u{175}"), ("wedbar;", "\u{2a5f}"),
    ("wedge;", "\u{2227}"), ("wedgeq;", "\u{2259}"), ("weierp;", "\u{2118}"), ("wfr;", "\u{1d534}"),
    ("wopf;", "\u{1d568}"), ("wp;", "\u{2118}"), ("wr;", "\u{2240}"), ("wreath;", "\u{2240}"), ("wscr;", "\u{1d4cc}"),
    ("xcap;", "\u{22c2}"), ("xcirc;", "\u{25ef}"), ("xcup;", "\u{22c3}"), ("xdtri;", "\u{25bd}"),
    ("xfr;", "\u{1d535}"), ("xhArr;", "\u{27fa}"), ("xharr;", "\u{27f7}"), ("xi;", "\u{3be}"), ("xlArr;", "\u{27f8}"),
    ("xlarr;", "\u{27f5}"), ("xmap;", "\u{27fc}"), ("xnis;", "\u{22fb}"), ("xodot;", "\u{2a00}"),
    ("xopf;", "\u{1d569}"), ("xoplus;", "\u{2a01}"), ("xotime;", "\u{2a02}"), ("xrArr;", "\u{27f9}"),
    ("xrarr;", "\u{27f6}"), ("xscr;", "\u{1d4cd}"), ("xsqcup;", "\u{2a06}"), ("xuplus;", "\u{2a04}"),
    ("xutri;", "\u{25b3}"), ("xvee;", "\u{22c1}"), ("xwedge;", "\u{22c0}"), ("yacute", "\u{fd}"),
    ("yacute;", "\u{fd}"), ("yacy;", "\u{44f}"), ("ycirc;", "\u{177}"), ("ycy;", "\u{44b}"), ("yen", "\u{a5}"),
    ("yen;", "\u{a5}"), ("yfr;", "\u{1d536}"), ("yicy;", "\u{457}"), ("yopf;", "\u{1d56a}"), ("yscr;", "\u{1d4ce}"),
    ("yucy;", "\u{44e}"), ("yuml", "\u{ff}"), ("yuml;", "\u{ff}"), ("zacute;", "\u{17a}"), ("zcaron;", "\u{17e}"),
    ("zcy;", "\u{437}"), ("zdot;", "\u{17c}"), ("zeetrf;", "\u{2128}"), ("zeta;", "\u{3b6}"), ("zfr;", "\u{1d537}"),
    ("zhcy;", "\u{436}"), ("zigrarr;", "\u{21dd}"), ("zopf;", "\u{1d56b}"), ("zscr;", "\u{1d4cf}"),
    ("zwj;", "\u{200d}"), ("zwnj;", "\u{200c}"),
];
