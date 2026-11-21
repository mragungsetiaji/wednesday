//! A past calendar from CSV (`news_stats.parse_csv`, `POST /api/calendar/import`): Python's `csv`
//! reader (the excel dialect), the header lookup, and the release times as `pd.Timestamp` reads
//! them for the usual forms (ISO, `01/09/2026 8:30am`, `Jan 9, 2026 08:30`, `9 Jan 2026 8:30`).

use chrono::{DateTime, Datelike, FixedOffset, Local, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use chrono_tz::Tz;
use serde_json::{json, Value};

use super::events::{py_capitalize, py_digit, py_space, py_strip, store_rows, CalRow};
use super::py_repr;

pub const MAX_CSV_BYTES: usize = 5 * 1024 * 1024;
pub const MAX_CSV_ROWS: usize = 50_000;
pub const CSV_ZONES: [&str; 2] = ["UTC", "America/New_York"];
const FIELD_LIMIT: usize = 131_072; // csv.field_size_limit()

/// Why a file wasn't imported.
#[derive(Debug, PartialEq)]
pub enum ImportError {
    /// `CalendarImportError`: what to fix, answered with status 422.
    Invalid(String),
    /// An error Python doesn't catch (a broken CSV, a time the zone skips or repeats): status 500.
    /// The message starts with Python's exception class (`Error: ...` from `csv`, `ValueError: ...`).
    Internal(String),
}

// ---- Python's csv reader (excel dialect, not strict) ----

#[derive(Clone, Copy, PartialEq)]
enum St {
    StartRecord,
    StartField,
    InField,
    InQuoted,
    QuoteInQuoted,
    EatCrnl,
}

/// The records of `csv.reader(io.StringIO(text))`: lines split at `\n` only, blank lines as empty
/// records, quoted fields across lines; `Err` where Python raises `csv.Error`.
pub fn csv_records(text: &str) -> Result<Vec<Vec<String>>, String> {
    let mut out = Vec::new();
    let (mut fields, mut field, mut state) = (Vec::<String>::new(), String::new(), St::StartRecord);
    let mut field_len = 0usize;
    let mut lines = text.split_inclusive('\n').peekable();
    loop {
        let Some(line) = lines.next() else {
            if field_len != 0 || state == St::InQuoted {
                fields.push(std::mem::take(&mut field));
                out.push(std::mem::take(&mut fields));
            }
            return Ok(out);
        };
        for c in line.chars().map(Some).chain(std::iter::once(None)) {
            // None is the end of the line (EOL).
            macro_rules! save {
                () => {{
                    fields.push(std::mem::take(&mut field));
                    field_len = 0;
                }};
            }
            macro_rules! add {
                ($c:expr) => {{
                    if field_len >= FIELD_LIMIT {
                        return Err(format!("field larger than field limit ({FIELD_LIMIT})"));
                    }
                    field.push($c);
                    field_len += 1;
                }};
            }
            let nl = matches!(c, Some('\n' | '\r'));
            let after_line = |c: Option<char>| if c.is_none() { St::StartRecord } else { St::EatCrnl };
            match state {
                St::StartRecord if c.is_none() => {}
                St::StartRecord if nl => state = St::EatCrnl,
                St::StartRecord | St::StartField => {
                    if nl || c.is_none() {
                        save!();
                        state = after_line(c);
                    } else if c == Some('"') {
                        state = St::InQuoted;
                    } else if c == Some(',') {
                        save!();
                        state = St::StartField;
                    } else {
                        add!(c.unwrap());
                        state = St::InField;
                    }
                }
                St::InField => {
                    if nl || c.is_none() {
                        save!();
                        state = after_line(c);
                    } else if c == Some(',') {
                        save!();
                        state = St::StartField;
                    } else {
                        add!(c.unwrap());
                    }
                }
                St::InQuoted => match c {
                    None => {}
                    Some('"') => state = St::QuoteInQuoted,
                    Some(c) => add!(c),
                },
                St::QuoteInQuoted => {
                    if c == Some('"') {
                        add!('"');
                        state = St::InQuoted;
                    } else if c == Some(',') {
                        save!();
                        state = St::StartField;
                    } else if nl || c.is_none() {
                        save!();
                        state = after_line(c);
                    } else {
                        add!(c.unwrap());
                        state = St::InField;
                    }
                }
                St::EatCrnl => {
                    if c.is_none() {
                        state = St::StartRecord;
                    } else if !nl {
                        return Err("new-line character seen in unquoted field - do you need to open the file with newline=''?".into());
                    }
                }
            }
            if c.is_none() && state == St::StartRecord {
                out.push(std::mem::take(&mut fields));
            }
        }
    }
}

// ---- release times (pd.Timestamp of a string) ----

const MONTHS: [&str; 12] = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
const MONTH_NAMES: [&str; 12] = ["january", "february", "march", "april", "may", "june", "july", "august",
                                 "september", "october", "november", "december"];
const WEEKDAYS: [&str; 7] = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

fn month_of(word: &str) -> Option<u32> {
    let w = word.to_lowercase();
    if w == "sept" {
        return Some(9);
    }
    MONTHS.iter().position(|m| *m == w).or_else(|| MONTH_NAMES.iter().position(|m| *m == w)).map(|i| i as u32 + 1)
}

fn is_weekday(word: &str) -> bool {
    let w = word.to_lowercase();
    WEEKDAYS.iter().any(|d| *d == w || d[..3] == w)
}

/// A time read from a release stamp: naive, or with an offset.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Stamp {
    Naive(NaiveDateTime),
    Aware(DateTime<FixedOffset>),
}

/// dateutil's two-digit year: this century, within 50 years of now.
fn convert_year(year: i64, century_specified: bool) -> i64 {
    if year >= 100 || century_specified {
        return year;
    }
    let now = Local::now().year() as i64;
    let mut y = year + now / 100 * 100;
    if y >= now + 50 {
        y -= 100;
    } else if y < now - 50 {
        y += 100;
    }
    y
}

/// The date part's tokens -> (year, month, day), as dateutil resolves them (month first).
fn resolve_date(tokens: &[(i64, usize, bool)]) -> Option<(i64, u32, u32)> {
    // (value, digits, is month name); a number with more than two digits is the year.
    let m_idx = tokens.iter().position(|t| t.2);
    let y_idx = tokens.iter().position(|t| !t.2 && t.1 > 2);
    let v: Vec<i64> = tokens.iter().map(|t| t.0).collect();
    if tokens.len() != 3 {
        return None; // a day, month and year are needed (pandas fills in others in ways not copied)
    }
    let (y, m, d) = match (m_idx, y_idx) {
        (Some(mi), Some(yi)) => (v[yi], v[mi], v[3 - mi - yi]),
        (Some(0), None) => {
            if v[1] > 31 { (v[1], v[0], v[2]) } else { (v[2], v[0], v[1]) }
        }
        (Some(1), None) => {
            if v[0] > 31 { (v[0], v[1], v[2]) } else { (v[2], v[1], v[0]) }
        }
        (Some(_), None) => {
            if v[1] > 31 { (v[1], v[2], v[0]) } else { (v[0], v[2], v[1]) }
        }
        (None, yi) => {
            if v[0] > 31 || yi == Some(0) {
                (v[0], v[1], v[2])
            } else if v[0] > 12 {
                (v[2], v[1], v[0])
            } else {
                (v[2], v[0], v[1])
            }
        }
    };
    let y = convert_year(y, y_idx.is_some());
    if y < 1 {
        return None;
    }
    Some((y, u32::try_from(m).ok()?, u32::try_from(d).ok()?))
}

/// `pd.Timestamp(stamp)` for the usual calendar forms; None where pandas raises a `ValueError`
/// (and also for rarer forms pandas reads, such as a month and year without a day).
pub fn parse_stamp(stamp: &str) -> Option<Stamp> {
    let s: Vec<char> = stamp.chars().collect();
    let n = s.len();
    // The time: h:mm[:ss[.f]] or an hour with am/pm.
    let mut i = 0;
    let (mut t_start, mut t_end, mut hms, mut frac) = (None, 0, [0i64; 3], 0u32);
    while i < n {
        if s[i].is_ascii_digit() && (i == 0 || !s[i - 1].is_ascii_digit()) {
            let mut j = i;
            while j < n && s[j].is_ascii_digit() {
                j += 1;
            }
            let h_len = j - i;
            if h_len <= 2 && s.get(j) == Some(&':') {
                let mm: String = s[j + 1..(j + 3).min(n)].iter().collect();
                if mm.len() == 2 && mm.bytes().all(|b| b.is_ascii_digit()) && !s.get(j + 3).is_some_and(char::is_ascii_digit) {
                    let hour: i64 = s[i..j].iter().collect::<String>().parse().ok()?;
                    let mut k = j + 3;
                    let mut sec = 0;
                    if s.get(k) == Some(&':') {
                        let ss: String = s[k + 1..(k + 3).min(n)].iter().collect();
                        if ss.len() != 2 || !ss.bytes().all(|b| b.is_ascii_digit()) || s.get(k + 3).is_some_and(char::is_ascii_digit) {
                            return None;
                        }
                        sec = ss.parse().ok()?;
                        k += 3;
                        if s.get(k) == Some(&'.') && s.get(k + 1).is_some_and(char::is_ascii_digit) {
                            let mut e = k + 1;
                            while e < n && s[e].is_ascii_digit() {
                                e += 1;
                            }
                            let f: String = s[k + 1..e].iter().chain(std::iter::repeat(&'0')).take(9).collect();
                            frac = f.parse().ok()?;
                            k = e;
                        }
                    }
                    hms = [hour, mm.parse().ok()?, sec];
                    t_start = Some(i);
                    t_end = k;
                    break;
                }
            }
            // An hour with am/pm right after it ("8am", "8 pm").
            let mut k = j;
            while k < n && s[k] == ' ' {
                k += 1;
            }
            if h_len <= 2 && ampm_at(&s, k).is_some() {
                hms = [s[i..j].iter().collect::<String>().parse().ok()?, 0, 0];
                t_start = Some(i);
                t_end = j;
                break;
            }
            i = j;
            continue;
        }
        i += 1;
    }
    let t_start = t_start?;
    // After the time: am/pm, then an offset or zone name, then nothing.
    let mut k = t_end;
    let skip = |k: &mut usize| {
        while *k < n && s[*k] == ' ' {
            *k += 1;
        }
    };
    skip(&mut k);
    let mut hour = hms[0];
    if let Some((pm, len)) = ampm_at(&s, k) {
        if !(0..=12).contains(&hour) {
            return None;
        }
        if pm && hour < 12 {
            hour += 12;
        } else if !pm && hour == 12 {
            hour = 0;
        }
        k += len;
        skip(&mut k);
    }
    let rest: String = s[k..].iter().collect();
    let offset = match rest.as_str() {
        "" => None,
        "Z" | "UTC" | "GMT" => Some(0),
        r if r.starts_with(['+', '-']) => {
            let sign = if r.starts_with('-') { -1 } else { 1 };
            let body = r[1..].replace(':', "");
            if !(body.len() == 2 || body.len() == 4) || !body.bytes().all(|b| b.is_ascii_digit()) {
                return None;
            }
            let (h, m): (i32, i32) = (body[..2].parse().ok()?, if body.len() == 4 { body[2..].parse().ok()? } else { 0 });
            Some(sign * (h * 3600 + m * 60))
        }
        _ => return None,
    };
    let time = NaiveTime::from_hms_nano_opt(u32::try_from(hour).ok()?, hms[1] as u32, hms[2] as u32, frac)?;

    // The date: numbers and month names between separators; weekdays and a few words are skipped.
    let head: String = s[..t_start].iter().collect();
    let head = head.trim_end_matches(|c: char| c == 'T' || c == ' ' || c == ',');
    let mut tokens: Vec<(i64, usize, bool)> = Vec::new();
    for word in head.split(|c: char| matches!(c, ' ' | ',' | '/' | '-' | '.' | ';' | '\'')).filter(|w| !w.is_empty()) {
        if word.bytes().all(|b| b.is_ascii_digit()) {
            if word.len() == 8 && tokens.is_empty() && head.trim() == word {
                tokens = vec![(word[..4].parse().ok()?, 4, false), (word[4..6].parse().ok()?, 2, false),
                              (word[6..].parse().ok()?, 2, false)];
                continue;
            }
            if word.len() > 4 {
                return None;
            }
            tokens.push((word.parse().ok()?, word.len(), false));
        } else if let Some(m) = month_of(word) {
            tokens.push((m as i64, 0, true));
        } else if is_weekday(word) || matches!(word.to_lowercase().as_str(), "at" | "on" | "of" | "and") {
            continue;
        } else {
            // "5th": a day with its suffix.
            let digits: String = word.chars().take_while(char::is_ascii_digit).collect();
            let suffix = word[digits.len()..].to_lowercase();
            if !digits.is_empty() && digits.len() <= 2 && matches!(suffix.as_str(), "st" | "nd" | "rd" | "th") {
                tokens.push((digits.parse().ok()?, digits.len(), false));
            } else {
                return None;
            }
        }
    }
    if tokens.iter().filter(|t| t.2).count() > 1 {
        return None;
    }
    let date = if tokens.is_empty() {
        Local::now().date_naive()
    } else {
        let (y, m, d) = resolve_date(&tokens)?;
        NaiveDate::from_ymd_opt(i32::try_from(y).ok()?, m, d)?
    };
    let naive = date.and_time(time);
    Some(match offset {
        None => Stamp::Naive(naive),
        Some(secs) => Stamp::Aware(FixedOffset::east_opt(secs)?.from_local_datetime(&naive).single()?),
    })
}

/// "am", "pm", "a.m." or "p.m." (any case) at `k`, ending a word: (pm, length).
fn ampm_at(s: &[char], k: usize) -> Option<(bool, usize)> {
    let low = |i: usize| s.get(k + i).map(|c| c.to_ascii_lowercase());
    let pm = match low(0)? {
        'a' => false,
        'p' => true,
        _ => return None,
    };
    let len = if low(1) == Some('m') {
        2
    } else if low(1) == Some('.') && low(2) == Some('m') && low(3) == Some('.') {
        4
    } else {
        return None;
    };
    if s.get(k + len).is_some_and(|c| c.is_alphanumeric() || *c == '_') {
        return None;
    }
    Some((pm, len))
}

/// Whether the stamp has a clock time: `\d{1,2}:\d{2}|\d{1,2}\s*(am|pm)` (any case).
fn has_clock_time(stamp: &str) -> bool {
    let s: Vec<char> = stamp.chars().collect();
    let d = |c: &char| py_digit(*c).is_some();
    for i in 0..s.len() {
        if !d(&s[i]) {
            continue;
        }
        for len in 1..=2 {
            if i + len > s.len() || !s[i..i + len].iter().all(d) {
                break;
            }
            let k = i + len;
            if s.get(k) == Some(&':') && s.get(k + 1).is_some_and(d) && s.get(k + 2).is_some_and(d) {
                return true;
            }
            let mut j = k;
            while j < s.len() && py_space(s[j]) {
                j += 1;
            }
            let pair: String = s[j..(j + 2).min(s.len())].iter().collect::<String>().to_lowercase();
            if pair == "am" || pair == "pm" {
                return true;
            }
        }
    }
    false
}

// ---- the import ----

/// `parse_csv(data, zone)`: a past calendar as CSV -> `calendar_events` rows. Columns (header
/// names, any case): `currency` (or `country`), `title` (or `event`), and either `datetime` or
/// `date` + `time`; optional `impact`, `actual`, `forecast`, `previous`. Times without an offset
/// are read in `zone`; rows without a clock time (All Day, Tentative) are skipped.
pub fn parse_csv(data: &[u8], zone: &str) -> Result<Vec<CalRow>, ImportError> {
    let invalid = |m: String| Err(ImportError::Invalid(m));
    if !CSV_ZONES.contains(&zone) {
        return invalid(format!("The time zone is one of {}", CSV_ZONES.join(", ")));
    }
    if data.len() > MAX_CSV_BYTES {
        return invalid("The file is larger than 5 MB; split it".into());
    }
    let text = match std::str::from_utf8(data) {
        Ok(t) => t.strip_prefix('\u{feff}').unwrap_or(t).to_string(),
        Err(_) => data.iter().map(|&b| b as char).collect(), // latin-1
    };
    let records = csv_records(&text).map_err(|e| ImportError::Internal(format!("Error: {e}")))?;
    let mut records = records.into_iter();
    let header = records.next().unwrap_or_default();
    let mut fields: Vec<(String, usize)> = Vec::new(); // lower-cased name -> its last column
    for (i, f) in header.iter().enumerate() {
        let key = py_strip(f).to_lowercase();
        match fields.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = i,
            None => fields.push((key, i)),
        }
    }
    // A row is a dict by header name: a repeated name holds its last column.
    let column_of = |name: &str| header.iter().rposition(|h| h == name);
    let col = |aliases: &[&str]| -> Option<usize> {
        aliases.iter().find_map(|a| fields.iter().find(|(k, _)| k == a)).and_then(|(_, i)| column_of(&header[*i]))
    };
    let c_datetime = col(&["datetime", "date_time", "timestamp"]);
    let c_date = col(&["date", "day"]);
    let c_time = col(&["time"]);
    let c_currency = col(&["currency", "country", "ccy"]);
    let c_title = col(&["title", "event", "name"]);
    let c_impact = col(&["impact", "importance"]);
    let c_actual = col(&["actual"]);
    let c_forecast = col(&["forecast", "consensus"]);
    let c_previous = col(&["previous", "prior"]);
    if c_currency.is_none() || c_title.is_none() || (c_datetime.is_none() && c_date.is_none()) {
        return invalid("The CSV needs a header with currency (or country), title (or event), and datetime, or date and time".into());
    }
    let tz: Tz = zone.parse().expect("a known zone");
    let mut out = Vec::new();
    for (i, row) in records.filter(|r| !r.is_empty()).enumerate() {
        if i >= MAX_CSV_ROWS {
            return invalid(format!("More than {MAX_CSV_ROWS} rows; split the file"));
        }
        let get = |c: Option<usize>| c.and_then(|c| row.get(c)).map(|v| py_strip(v).to_string()).unwrap_or_default();
        let stamp = match get(c_datetime) {
            s if !s.is_empty() => s,
            _ => py_strip(&format!("{} {}", get(c_date), get(c_time))).to_string(),
        };
        if !has_clock_time(&stamp) {
            continue; // all day, tentative, or no time
        }
        let Some(when) = parse_stamp(&stamp) else {
            return invalid(format!("Row {}: can't read the time {}", i + 2, py_repr(&stamp)));
        };
        let utc = match when {
            Stamp::Aware(d) => d.with_timezone(&Utc),
            Stamp::Naive(n) => match tz.from_local_datetime(&n) {
                chrono::LocalResult::Single(d) => d.with_timezone(&Utc),
                _ => return Err(ImportError::Internal(format!("ValueError: {n} doesn't exist once in {zone}"))),
            },
        };
        let impact = py_capitalize(&get(c_impact));
        let event = json!({
            "time": super::events::iso_utc(utc),
            "currency": get(c_currency),
            "title": get(c_title),
            "impact": if impact.is_empty() { Value::Null } else { Value::String(impact) },
            "actual": get(c_actual),
            "forecast": get(c_forecast),
            "previous": get(c_previous),
        });
        let mut rows = store_rows(&[event], "csv");
        // pandas keeps nanoseconds, which isoformat() writes and fromisoformat() cuts to microseconds.
        for r in &mut rows {
            r.time = super::events::int_timestamp(utc);
        }
        out.extend(rows);
    }
    if out.is_empty() {
        return invalid("No row with a release time was found".into());
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn recs(t: &str) -> Vec<Vec<String>> {
        csv_records(t).unwrap()
    }

    #[test]
    fn csv_as_python_reads_it() {
        assert_eq!(recs("a,b\r\n1,2\n\n3\n"), vec![vec!["a", "b"], vec!["1", "2"], vec![], vec!["3"]]);
        assert_eq!(recs("\"x, \"\"y\"\"\nz\",2\n"), vec![vec!["x, \"y\"\nz", "2"]]);
        assert_eq!(recs("\"ab\"cd,e"), vec![vec!["abcd", "e"]]);
        assert_eq!(recs("a,\n"), vec![vec!["a", ""]]);
        assert_eq!(recs("\"open"), vec![vec!["open"]]);
        assert!(csv_records("a\rb\n").is_err());
        assert_eq!(recs(""), Vec::<Vec<String>>::new());
    }

    #[test]
    fn stamps_as_pandas_reads_them() {
        let naive = |s: &str| match parse_stamp(s) {
            Some(Stamp::Naive(n)) => n.format("%Y-%m-%d %H:%M:%S").to_string(),
            other => format!("{other:?}"),
        };
        assert_eq!(naive("01/09/2026 8:30am"), "2026-01-09 08:30:00");
        assert_eq!(naive("01-09-2026 8:30 pm"), "2026-01-09 20:30:00");
        assert_eq!(naive("13/01/2024 08:30"), "2024-01-13 08:30:00");
        assert_eq!(naive("Jan 5, 2024 8:30 am"), "2024-01-05 08:30:00");
        assert_eq!(naive("Friday, January 5, 2024 8:30am"), "2024-01-05 08:30:00");
        assert_eq!(naive("05-Jan-2024 12:00am"), "2024-01-05 00:00:00");
        assert_eq!(naive("1/5/24 8am"), "2024-01-05 08:00:00");
        assert_eq!(naive("24/01/05 8:30"), "2005-01-24 08:30:00");
        assert_eq!(naive("20240105 08:30"), "2024-01-05 08:30:00");
        assert_eq!(naive("2024-01-05T08:30"), "2024-01-05 08:30:00");
        assert_eq!(parse_stamp("2024-01-05 13:00 pm"), None);
        assert_eq!(parse_stamp("2024-01-05 08:30 EST"), None);
        assert_eq!(parse_stamp("2024-02-30 08:30"), None);
        assert!(matches!(parse_stamp("2024-01-05 08:30:00 +05:30"), Some(Stamp::Aware(_))));
        assert!(has_clock_time("8 PM") && has_clock_time("x12:30") && !has_clock_time("All Day"));
    }
}
