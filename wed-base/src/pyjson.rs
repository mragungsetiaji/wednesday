//! JSON written as Python's `json.dumps` writes it (`", "` and `": "` separators, non-ASCII
//! escaped), so values Rust stores read the same as those Python stores, and size limits that
//! Python checks on `json.dumps(...)` give the same answer.

use serde_json::Value;

pub fn dumps(v: &Value) -> String {
    let mut out = String::new();
    write(v, &mut out);
    out
}

fn write(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&number(n)),
        Value::String(s) => string(s, out),
        Value::Array(items) => {
            out.push('[');
            for (i, x) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            out.push('{');
            for (i, (k, x)) in m.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                string(k, out);
                out.push_str(": ");
                write(x, out);
            }
            out.push('}');
        }
    }
}

/// Python's float repr: `1.0`, `0.0001`, `1e-05`, `1e+16`.
fn number(n: &serde_json::Number) -> String {
    if n.is_i64() || n.is_u64() {
        return n.to_string();
    }
    let f = n.as_f64().unwrap_or(0.0);
    if f == f.trunc() && f.abs() < 1e16 {
        return format!("{f:.1}");
    }
    let exp = f.abs().log10().floor() as i32;
    if (-4..16).contains(&exp) {
        return format!("{f}");
    }
    // Shortest digits, in Python's exponent form.
    let s = format!("{f:e}"); // e.g. 1.5e-7
    let (mant, e) = s.split_once('e').unwrap();
    let e: i32 = e.parse().unwrap();
    format!("{mant}e{}{:02}", if e < 0 { '-' } else { '+' }, e.abs())
}

fn string(s: &str, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7f => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::dumps;
    use serde_json::json;

    #[test]
    fn as_python_writes_it() {
        // json.dumps({"a": [1, 2.5, 1.0, None, True], "é": "x\"y\n", "big": 1e16, "small": 1e-05, "n": 0.0001})
        let v = json!({"a": [1, 2.5, 1.0, null, true], "é": "x\"y\n", "big": 1e16, "small": 1e-05, "n": 0.0001});
        assert_eq!(dumps(&v), r#"{"a": [1, 2.5, 1.0, null, true], "\u00e9": "x\"y\n", "big": 1e+16, "small": 1e-05, "n": 0.0001}"#);
        assert_eq!(dumps(&json!("\u{1F600}")), r#""\ud83d\ude00""#);
        assert_eq!(dumps(&json!({})), "{}");
    }
}
