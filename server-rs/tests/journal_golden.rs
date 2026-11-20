//! The Rust journal stats against the reference output in `tests/golden/journal/` (#96): the
//! same trades, cash and M1 bars as Python's `analyse` gets, exactly the same JSON out. The
//! Python side is `tests/test_journal_golden.py`, which writes the files. The comparison is
//! exact: the same keys in the same order, floats to the bit (so `-0.0` is not `0.0`), and an
//! int where Python has an int.

use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};
use wednesday_rs::journal::stats::{analyse, Bars};

fn golden() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/golden/journal")
}

fn read_bars(dir: &Path) -> BTreeMap<String, Bars> {
    let mut text = String::new();
    flate2::read::GzDecoder::new(std::fs::File::open(dir.join("bars.csv.gz")).unwrap())
        .read_to_string(&mut text)
        .unwrap();
    let mut lines = text.lines();
    assert_eq!(lines.next(), Some("symbol,time,open,high,low,close"));
    let mut out: BTreeMap<String, Bars> = BTreeMap::new();
    for line in lines.filter(|l| !l.is_empty()) {
        let f: Vec<&str> = line.split(',').collect();
        let b = out.entry(f[0].to_string()).or_default();
        let n = |i: usize| f[i].parse::<f64>().unwrap();
        b.t.push(f[1].parse().unwrap());
        b.open.push(n(2));
        b.high.push(n(3));
        b.low.push(n(4));
        b.close.push(n(5));
    }
    out
}

/// JSON text to a Value with every float correctly rounded. serde_json's own parser (without its
/// `float_roundtrip` feature) can land one ulp off, e.g. reading `27.599999999999998` as `27.6`,
/// which an exact comparison can't afford.
fn parse(text: &str) -> Value {
    let mut p = Parser { s: text.as_bytes(), i: 0 };
    let v = p.value();
    p.ws();
    assert_eq!(p.i, p.s.len(), "trailing text after the JSON");
    v
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.i < self.s.len() && self.s[self.i].is_ascii_whitespace() {
            self.i += 1;
        }
    }

    fn eat(&mut self, lit: &str) -> bool {
        let ok = self.s[self.i..].starts_with(lit.as_bytes());
        if ok {
            self.i += lit.len();
        }
        ok
    }

    fn value(&mut self) -> Value {
        self.ws();
        let c = self.s[self.i];
        match c {
            b'{' => {
                self.i += 1;
                let mut m = Map::new();
                self.ws();
                if self.eat("}") {
                    return Value::Object(m);
                }
                loop {
                    self.ws();
                    let k = self.string();
                    self.ws();
                    assert!(self.eat(":"));
                    let v = self.value();
                    m.insert(k, v);
                    self.ws();
                    if self.eat("}") {
                        return Value::Object(m);
                    }
                    assert!(self.eat(","), "expected , at {}", self.i);
                }
            }
            b'[' => {
                self.i += 1;
                let mut a = vec![];
                self.ws();
                if self.eat("]") {
                    return Value::Array(a);
                }
                loop {
                    a.push(self.value());
                    self.ws();
                    if self.eat("]") {
                        return Value::Array(a);
                    }
                    assert!(self.eat(","), "expected , at {}", self.i);
                }
            }
            b'"' => Value::String(self.string()),
            _ if self.eat("null") => Value::Null,
            _ if self.eat("true") => Value::Bool(true),
            _ if self.eat("false") => Value::Bool(false),
            _ => {
                let start = self.i;
                while self.i < self.s.len() && matches!(self.s[self.i], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9') {
                    self.i += 1;
                }
                let t = std::str::from_utf8(&self.s[start..self.i]).unwrap();
                if t.contains(['.', 'e', 'E']) {
                    Value::from(t.parse::<f64>().unwrap())
                } else {
                    Value::from(t.parse::<i64>().unwrap())
                }
            }
        }
    }

    fn string(&mut self) -> String {
        assert!(self.eat("\""));
        let mut units: Vec<u16> = vec![];
        let mut out = String::new();
        let flush = |units: &mut Vec<u16>, out: &mut String| {
            out.push_str(&String::from_utf16(units).unwrap());
            units.clear();
        };
        loop {
            let c = self.s[self.i];
            if c == b'"' {
                self.i += 1;
                flush(&mut units, &mut out);
                return out;
            }
            if c == b'\\' {
                let e = self.s[self.i + 1];
                self.i += 2;
                let ch = match e {
                    b'n' => '\n',
                    b't' => '\t',
                    b'r' => '\r',
                    b'b' => '\u{8}',
                    b'f' => '\u{c}',
                    b'u' => {
                        let hex = std::str::from_utf8(&self.s[self.i..self.i + 4]).unwrap();
                        units.push(u16::from_str_radix(hex, 16).unwrap());
                        self.i += 4;
                        continue;
                    }
                    other => other as char,
                };
                flush(&mut units, &mut out);
                out.push(ch);
                continue;
            }
            flush(&mut units, &mut out);
            let len = match c {
                0..=0x7f => 1,
                0xc0..=0xdf => 2,
                0xe0..=0xef => 3,
                _ => 4,
            };
            out.push_str(std::str::from_utf8(&self.s[self.i..self.i + len]).unwrap());
            self.i += len;
        }
    }
}

fn rows(v: &Value) -> Vec<Map<String, Value>> {
    v.as_array().unwrap().iter().map(|r| r.as_object().unwrap().clone()).collect()
}

/// The first place `actual` differs from `expected`, exactly.
fn first_difference(e: &Value, a: &Value, path: &str) -> Option<String> {
    let here = || Some(format!("{path}: expected {e}, got {a}"));
    match (e, a) {
        (Value::Object(x), Value::Object(y)) => {
            let (kx, ky): (Vec<&String>, Vec<&String>) = (x.keys().collect(), y.keys().collect());
            if kx != ky {
                return Some(format!("{path}: keys {kx:?} became {ky:?}"));
            }
            x.iter().find_map(|(k, v)| first_difference(v, &y[k], &format!("{path}.{k}")))
        }
        (Value::Array(x), Value::Array(y)) => {
            if x.len() != y.len() {
                return Some(format!("{path}: {} items became {}", x.len(), y.len()));
            }
            x.iter().zip(y).enumerate().find_map(|(i, (v, w))| first_difference(v, w, &format!("{path}[{i}]")))
        }
        (Value::Number(x), Value::Number(y)) => {
            let same = match (x.is_f64(), y.is_f64()) {
                (true, true) => x.as_f64().unwrap().to_bits() == y.as_f64().unwrap().to_bits(),
                (false, false) => x.as_i64() == y.as_i64() && x.as_u64() == y.as_u64(),
                _ => false, // 3 vs 3.0: Python wrote one, Rust the other
            };
            if same {
                None
            } else {
                here()
            }
        }
        _ if e == a => None,
        _ => here(),
    }
}

#[test]
fn journal_stats_match_python() {
    let mut cases: Vec<PathBuf> = std::fs::read_dir(golden())
        .expect("tests/golden/journal: run uv run pytest tests/test_journal_golden.py")
        .map(|e| e.unwrap().path())
        .filter(|p| p.join("expected.json").exists())
        .collect();
    cases.sort();
    assert!(cases.len() >= 8, "expected the journal golden cases, found {}", cases.len());
    let mut failed = vec![];
    for dir in &cases {
        let name = dir.file_name().unwrap().to_string_lossy().to_string();
        let input = parse(&std::fs::read_to_string(dir.join("input.json")).unwrap());
        let expected = parse(&std::fs::read_to_string(dir.join("expected.json")).unwrap());
        let bars = read_bars(dir);
        let actual = analyse(&rows(&input["trades"]), &rows(&input["cash"]), &bars, input["offset"].as_i64());
        if let Some(diff) = first_difference(&expected, &actual, &name) {
            failed.push(diff);
        }
    }
    assert!(failed.is_empty(), "the Rust journal stats differ from Python:\n{}", failed.join("\n"));
}
