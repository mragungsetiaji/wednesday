//! Python's built-ins as the drawing routes use them on JSON values: `bool(x)`, `int(x)`,
//! `float(x)`, `isinstance(x, (int, float))`, `str.strip()`, `repr(x)` and `html.escape`.

use serde_json::Value;

/// `str.isspace()` for one character (Unicode whitespace plus the separators `\x1c`-`\x1f`).
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// `s.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `len(s)`: code points.
pub fn len(s: &str) -> usize {
    s.chars().count()
}

/// `bool(v)`.
pub fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => !matches!(n.as_f64(), Some(f) if f == 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

/// `isinstance(v, (int, float))` (a bool is an int), as its value.
pub fn number(v: &Value) -> Option<f64> {
    match v {
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

/// Digits with single underscores between them (`1_000`), as Python's `int()` and `float()` take them.
fn without_underscores(s: &str) -> Option<String> {
    let b = s.as_bytes();
    for (i, &c) in b.iter().enumerate() {
        if c == b'_' {
            let ok = i > 0 && i + 1 < b.len() && b[i - 1].is_ascii_digit() && b[i + 1].is_ascii_digit();
            if !ok {
                return None;
            }
        }
    }
    Some(s.replace('_', ""))
}

/// `float(v)`; None where Python raises TypeError or ValueError.
pub fn float(v: &Value) -> Option<f64> {
    match v {
        Value::String(s) => {
            let s = without_underscores(strip(s))?;
            let lower = s.to_ascii_lowercase();
            let body = lower.trim_start_matches(['+', '-']);
            // Rust also takes these; Python only these spellings of them.
            if body.starts_with("inf") || body.starts_with("nan") {
                if !matches!(body, "inf" | "infinity" | "nan") || lower.len() - body.len() > 1 {
                    return None;
                }
            } else if !body.bytes().all(|c| c.is_ascii_digit() || matches!(c, b'.' | b'e' | b'+' | b'-')) {
                return None;
            }
            s.parse::<f64>().ok()
        }
        _ => number(v),
    }
}

/// `int(v)`; None where Python raises (or for a number past 64 bits, which Python would take).
pub fn int(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(b) => Some(*b as i64),
        Value::Number(n) => match n.as_i64() {
            Some(i) => Some(i),
            None => {
                let f = n.as_f64()?.trunc();
                (f.is_finite() && f.abs() < 9.2e18).then_some(f as i64)
            }
        },
        Value::String(s) => {
            let s = without_underscores(strip(s))?;
            let digits = s.strip_prefix(['+', '-']).unwrap_or(&s);
            if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
                return None;
            }
            s.parse::<i64>().ok()
        }
        _ => None,
    }
}

/// `repr(v)` of a JSON value as Python holds it.
pub fn repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => (if *b { "True" } else { "False" }).into(),
        Value::Number(_) => crate::pyjson::dumps(v),
        Value::String(s) => py_repr(s),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter().map(|(k, x)| format!("{}: {}", py_repr(k), repr(x))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// `html.escape(s)`.
pub fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// `f"{v:,.{digits}f}"`: fixed decimals with thousands commas.
pub fn grouped(v: f64, digits: usize) -> String {
    let s = format!("{v:.digits$}");
    let (sign, rest) = match s.strip_prefix('-') {
        Some(r) => ("-", r),
        None => ("", s.as_str()),
    };
    let (int, frac) = match rest.split_once('.') {
        Some((i, f)) => (i, Some(f)),
        None => (rest, None),
    };
    if !int.bytes().all(|c| c.is_ascii_digit()) {
        return s; // inf, nan
    }
    let mut out = String::from(sign);
    for (i, c) in int.chars().enumerate() {
        if i > 0 && (int.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if let Some(f) = frac {
        out.push('.');
        out.push_str(f);
    }
    out
}

/// Python's `repr(s)` for a string.
pub fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_control() || (c.is_whitespace() && c != ' ') => {
                let n = c as u32;
                if n < 0x100 {
                    out.push_str(&format!("\\x{n:02x}"));
                } else if n < 0x10000 {
                    out.push_str(&format!("\\u{n:04x}"));
                } else {
                    out.push_str(&format!("\\U{n:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `str(v)` for a JSON value (strings as they are, numbers as Python prints them).
pub fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(_) => crate::pyjson::dumps(v),
        other => crate::pyjson::dumps(other).replace('"', "'"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn as_python() {
        assert_eq!(float(&json!(" 1_000.5 ")), Some(1000.5));
        assert_eq!(float(&json!("1__0")), None);
        assert!(float(&json!("-Infinity")).unwrap().is_infinite());
        assert_eq!(float(&json!("0x10")), None);
        assert_eq!(float(&json!(null)), None);
        assert_eq!(int(&json!("1.5")), None);
        assert_eq!(int(&json!(" -12 ")), Some(-12));
        assert_eq!(int(&json!(60.9)), Some(60));
        assert_eq!(repr(&json!({"a": [1, null, true, 2.0, "it's"]})), r#"{'a': [1, None, True, 2.0, "it's"]}"#);
        assert_eq!(grouped(1234567.875, 2), "1,234,567.88");
        assert_eq!(grouped(-0.004, 2), "-0.00");
        assert_eq!(grouped(999.999, 2), "1,000.00");
        assert_eq!(escape("<a href='x'>&\""), "&lt;a href=&#x27;x&#x27;&gt;&amp;&quot;");
    }
}
