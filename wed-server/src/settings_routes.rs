//! Workspaces and drawing templates (`workspaces.py`, `drawing_templates.py`): named sets kept in
//! the settings table, validated as the Python server validates them, with its messages.

use serde_json::{json, Map, Value};

use crate::pyjson;
use crate::store::Store;

pub use wednesday_base::Refused;

fn invalid<T>(msg: impl Into<String>) -> Result<T, Refused> {
    Err(Refused::Invalid(msg.into()))
}

pub use wednesday_base::time::now_iso;

/// `" ".join(str(name).split())[:60]`
fn squeeze(name: &str) -> String {
    name.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(60).collect()
}

/// A JSON number that isn't a boolean (Python's `isinstance(v, (int, float))`).
fn number(v: Option<&Value>) -> Option<f64> {
    v.filter(|v| v.is_number()).and_then(Value::as_f64)
}

// ---- workspaces ----

pub const WORKSPACES_KEY: &str = "workspaces";
pub const MAX_WORKSPACES: usize = 20;
pub const MAX_WINDOWS: usize = 12;
pub const MAX_LAYOUT_BYTES: usize = 64 * 1024;
const MIN_SIZE: (i64, i64) = (480, 320);

/// `^#?(popout/[A-Za-z0-9]{1,8}(/[ABC])?)?$`
fn route_ok(route: &str) -> bool {
    let r = route.strip_prefix('#').unwrap_or(route);
    if r.is_empty() {
        return true;
    }
    let Some(rest) = r.strip_prefix("popout/") else { return false };
    let (tf, group) = match rest.split_once('/') {
        Some((tf, g)) => (tf, Some(g)),
        None => (rest, None),
    };
    (1..=8).contains(&tf.len())
        && tf.bytes().all(|b| b.is_ascii_alphanumeric())
        && group.map_or(true, |g| matches!(g, "A" | "B" | "C"))
}

fn clean_window(w: &Map<String, Value>) -> Result<Value, Refused> {
    // str(w.get("route") or "#")
    let route = match w.get("route") {
        v if !truthy(v) => "#".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(true)) => "True".into(),
        Some(other) => other.to_string(),
        None => "#".to_string(),
    };
    if !route_ok(&route) {
        return invalid(format!("Unknown window {}", py_repr(&route)));
    }
    let mut out = Map::new();
    out.insert("route".into(), (if route.starts_with('#') { route } else { format!("#{route}") }).into());
    out.insert("maximized".into(), truthy(w.get("maximized")).into());
    for k in ["x", "y", "width", "height"] {
        let Some(v) = number(w.get(k)) else { return invalid(format!("A window needs a numeric {k}")) };
        out.insert(k.into(), (v.trunc() as i64).into());
    }
    let (wd, ht) = (out["width"].as_i64().unwrap(), out["height"].as_i64().unwrap());
    out.insert("width".into(), wd.max(MIN_SIZE.0).into());
    out.insert("height".into(), ht.max(MIN_SIZE.1).into());
    Ok(Value::Object(out))
}

/// Python's truthiness of a JSON value.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Python's `repr()` of a string, for messages.
fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') { format!("\"{s}\"") } else { format!("'{}'", s.replace('\'', "\\'")) }
}

fn clean_windows(windows: &Value) -> Result<Vec<Value>, Refused> {
    let Value::Array(list) = windows else { return invalid("windows is a list") };
    if list.len() > MAX_WINDOWS {
        return invalid(format!("At most {MAX_WINDOWS} windows in a workspace"));
    }
    list.iter().filter_map(Value::as_object).map(clean_window).collect()
}

fn clean_layout(layout: &Value) -> Result<Map<String, Value>, Refused> {
    let Value::Object(m) = layout else { return invalid("layout is an object") };
    let out: Map<String, Value> =
        m.iter().filter(|(k, _)| k.starts_with("wed.") || k.starts_with("xau.")).map(|(k, v)| (k.clone(), v.clone())).collect();
    if pyjson::dumps(&Value::Object(out.clone())).len() > MAX_LAYOUT_BYTES {
        return invalid("The layout is too large to save");
    }
    Ok(out)
}

/// A setting of one user: `workspaces:<user id>`; the plain key without a login.
pub fn user_key(base: &str, who: Option<&str>) -> String {
    match who {
        Some(uid) => format!("{base}:{uid}"),
        None => base.to_string(),
    }
}

async fn all(store: &Store, key: &str) -> Result<Map<String, Value>, Refused> {
    Ok(store.get_setting(key).await?.and_then(|v| v.as_object().cloned()).unwrap_or_default())
}

pub async fn workspaces_list(store: &Store, who: Option<&str>) -> Result<Value, Refused> {
    let key = user_key(WORKSPACES_KEY, who);
    let items = all(store, &key).await?;
    let mut list: Vec<(&String, &Value)> = items.iter().collect();
    list.sort_by_key(|(n, _)| n.to_lowercase());
    let out: Vec<Value> = list
        .into_iter()
        .map(|(n, w)| {
            json!({"name": n, "saved_at": w.get("saved_at"),
                   "windows": w.get("windows").and_then(Value::as_array).map_or(0, Vec::len)})
        })
        .collect();
    Ok(json!({"workspaces": out}))
}

/// None when there's no such workspace.
pub async fn workspace_get(store: &Store, who: Option<&str>, name: &str) -> Result<Option<Value>, Refused> {
    let key = user_key(WORKSPACES_KEY, who);
    let items = all(store, &key).await?;
    Ok(items.get(name).map(|w| with_first(json!({"name": name}), w)))
}

pub async fn workspace_save(store: &Store, who: Option<&str>, name: &str, body: &Map<String, Value>) -> Result<Value, Refused> {
    let key = user_key(WORKSPACES_KEY, who);
    let name = squeeze(name);
    if name.is_empty() {
        return invalid("Give the workspace a name, e.g. London prep");
    }
    let mut items = all(store, &key).await?;
    if !items.contains_key(&name) && items.len() >= MAX_WORKSPACES {
        return invalid(format!("At most {MAX_WORKSPACES} workspaces: remove one first"));
    }
    // body.get("layout") or {}, body.get("windows") or []
    let layout = body.get("layout").filter(|v| truthy(Some(v))).cloned().unwrap_or(json!({}));
    let windows = body.get("windows").filter(|v| truthy(Some(v))).cloned().unwrap_or(json!([]));
    let entry = json!({"layout": clean_layout(&layout)?, "windows": clean_windows(&windows)?, "saved_at": now_iso()});
    items.insert(name.clone(), entry.clone());
    store.set_setting(&key, &Value::Object(items)).await?;
    Ok(with_first(json!({"name": name}), &entry))
}

pub async fn workspace_delete(store: &Store, who: Option<&str>, name: &str) -> Result<bool, Refused> {
    let key = user_key(WORKSPACES_KEY, who);
    let mut items = all(store, &key).await?;
    if items.shift_remove(name).is_none() {
        return Ok(false);
    }
    store.set_setting(&key, &Value::Object(items)).await?;
    Ok(true)
}

/// `{**a, **b}`
fn with_first(a: Value, b: &Value) -> Value {
    let mut out = a.as_object().cloned().unwrap_or_default();
    if let Some(m) = b.as_object() {
        out.extend(m.clone());
    }
    Value::Object(out)
}

// ---- drawing templates ----

pub const TEMPLATES_KEY: &str = "drawing_templates";
pub const FAMILIES: [&str; 3] = ["line", "rect", "text"];
pub const MAX_TEMPLATES: usize = 100;
pub const DASHES: [&str; 3] = ["solid", "dashed", "dotted"];
pub const MAX_TEXT: usize = 500;

fn clean_template(body: &Map<String, Value>) -> Result<Value, Refused> {
    let Some(Value::Object(style)) = body.get("style") else { return invalid("A template needs a style") };
    let color = style.get("color").cloned().unwrap_or(Value::Null);
    let color_ok = match &color {
        Value::Null => true,
        Value::String(c) => c.len() == 7 && c.starts_with('#') && c[1..].bytes().all(|b| b.is_ascii_hexdigit()),
        _ => false,
    };
    if !color_ok {
        return invalid("style.color must be #rrggbb or null");
    }
    let width = style.get("width").cloned().unwrap_or(json!(1));
    if !number(Some(&width)).is_some_and(|w| (1.0..=4.0).contains(&w)) {
        return invalid("style.width must be 1 to 4");
    }
    let dash = style.get("dash").cloned().unwrap_or(json!("solid"));
    if !dash.as_str().is_some_and(|d| DASHES.contains(&d)) {
        return invalid("style.dash must be one of solid, dashed, dotted");
    }
    let size = style.get("size").cloned().filter(|v| !v.is_null());
    if let Some(s) = &size {
        if !number(Some(s)).is_some_and(|v| (8.0..=48.0).contains(&v)) {
            return invalid("style.size must be 8 to 48");
        }
    }
    let text = match body.get("text") {
        None | Some(Value::Null) => Value::Null,
        Some(Value::String(t)) if t.chars().count() <= MAX_TEXT => {
            let t = t.trim();
            if t.is_empty() { Value::Null } else { t.into() }
        }
        _ => return invalid(format!("text must be at most {MAX_TEXT} characters")),
    };
    let mut style_out = Map::new();
    style_out.insert("color".into(), color);
    style_out.insert("width".into(), width);
    style_out.insert("dash".into(), dash);
    if let Some(s) = size {
        style_out.insert("size".into(), s);
    }
    Ok(json!({"style": style_out, "text": text, "saved_at": now_iso()}))
}

pub async fn templates_list(store: &Store, who: Option<&str>) -> Result<Value, Refused> {
    let key = user_key(TEMPLATES_KEY, who);
    let everything = all(store, &key).await?;
    let mut out: Vec<Value> = Vec::new();
    for (family, named) in &everything {
        for (name, t) in named.as_object().into_iter().flatten() {
            out.push(with_first(json!({"family": family, "name": name}), t));
        }
    }
    out.sort_by_key(|t| (t["family"].as_str().unwrap_or("").to_string(), t["name"].as_str().unwrap_or("").to_lowercase()));
    Ok(json!({"templates": out}))
}

pub async fn template_save(store: &Store, who: Option<&str>, family: &str, name: &str, body: &Map<String, Value>) -> Result<Value, Refused> {
    let key = user_key(TEMPLATES_KEY, who);
    if !FAMILIES.contains(&family) {
        return invalid(format!("Unknown drawing family {}: line, rect or text", py_repr(family)));
    }
    let name = squeeze(name);
    if name.is_empty() {
        return invalid("Give the template a name, e.g. H1 OB");
    }
    let template = clean_template(body)?;
    let mut everything = all(store, &key).await?;
    let named = everything.entry(family.to_string()).or_insert_with(|| json!({}));
    let named = named.as_object_mut().ok_or_else(|| Refused::Store("templates aren't an object".into()))?;
    if !named.contains_key(&name) && named.len() >= MAX_TEMPLATES {
        return invalid(format!("At most {MAX_TEMPLATES} templates per drawing type: delete one first"));
    }
    named.insert(name.clone(), template.clone());
    store.set_setting(&key, &Value::Object(everything)).await?;
    Ok(with_first(json!({"family": family, "name": name}), &template))
}

pub async fn template_delete(store: &Store, who: Option<&str>, family: &str, name: &str) -> Result<bool, Refused> {
    let key = user_key(TEMPLATES_KEY, who);
    let mut everything = all(store, &key).await?;
    let Some(named) = everything.get_mut(family).and_then(Value::as_object_mut) else { return Ok(false) };
    if named.shift_remove(name).is_none() {
        return Ok(false);
    }
    store.set_setting(&key, &Value::Object(everything)).await?;
    Ok(true)
}

// ---- preferences (layout, chart type...) of an account ----

pub const PREFS_KEY: &str = "prefs";
pub const MAX_PREFS_BYTES: usize = 256 * 1024;

pub async fn prefs_get(store: &Store, who: Option<&str>) -> Result<Value, Refused> {
    Ok(json!({"prefs": all(store, &user_key(PREFS_KEY, who)).await?}))
}

/// Merge `{key: value}` into the saved preferences; a null value removes the key. Only the
/// dashboard's own keys (`wed.`, `xau.`) are kept.
pub async fn prefs_put(store: &Store, who: Option<&str>, body: &Map<String, Value>) -> Result<Value, Refused> {
    let key = user_key(PREFS_KEY, who);
    let mut prefs = all(store, &key).await?;
    for (k, v) in body {
        if !(k.starts_with("wed.") || k.starts_with("xau.")) || k.len() > 100 {
            return invalid(format!("Unknown preference {}: they start with wed.", py_repr(k)));
        }
        if v.is_null() {
            prefs.shift_remove(k);
        } else {
            prefs.insert(k.clone(), v.clone());
        }
    }
    if pyjson::dumps(&Value::Object(prefs.clone())).len() > MAX_PREFS_BYTES {
        return invalid("The preferences are too large to save");
    }
    store.set_setting(&key, &Value::Object(prefs.clone())).await?;
    Ok(json!({"prefs": prefs}))
}

/// What a browser kept during the preview, into the account: templates and workspaces whose
/// name the account doesn't have yet, and the preferences the account hasn't set. Returns how
/// many of each were added.
pub async fn import(store: &Store, who: Option<&str>, body: &Map<String, Value>) -> Result<(usize, usize, usize), Refused> {
    let (mut templates, mut workspaces, mut prefs) = (0, 0, 0);
    let have = all(store, &user_key(TEMPLATES_KEY, who)).await?;
    for t in body.get("templates").and_then(Value::as_array).into_iter().flatten().take(MAX_TEMPLATES * FAMILIES.len()) {
        let Some(t) = t.as_object() else { continue };
        let (Some(family), Some(name)) = (t.get("family").and_then(Value::as_str), t.get("name").and_then(Value::as_str)) else {
            continue;
        };
        if have.get(family).and_then(|f| f.get(squeeze(name))).is_some() {
            continue;
        }
        if template_save(store, who, family, name, t).await.is_ok() {
            templates += 1;
        }
    }
    let have = all(store, &user_key(WORKSPACES_KEY, who)).await?;
    for w in body.get("workspaces").and_then(Value::as_array).into_iter().flatten().take(MAX_WORKSPACES) {
        let Some(w) = w.as_object() else { continue };
        let Some(name) = w.get("name").and_then(Value::as_str) else { continue };
        if have.contains_key(&squeeze(name)) {
            continue;
        }
        if workspace_save(store, who, name, w).await.is_ok() {
            workspaces += 1;
        }
    }
    if let Some(sent) = body.get("prefs").and_then(Value::as_object) {
        let have = all(store, &user_key(PREFS_KEY, who)).await?;
        let new: Map<String, Value> = sent.iter()
            .filter(|(k, v)| !have.contains_key(*k) && !v.is_null() && (k.starts_with("wed.") || k.starts_with("xau.")))
            .map(|(k, v)| (k.clone(), v.clone())).collect();
        prefs = new.len();
        if prefs > 0 {
            prefs_put(store, who, &new).await?;
        }
    }
    Ok((templates, workspaces, prefs))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routes_as_the_dashboard_has_them() {
        for ok in ["#", "", "popout/5M", "#popout/15M/B"] {
            assert!(route_ok(ok), "{ok}");
        }
        for bad in ["https://evil.example", "#popout/", "#popout/5M/D", "#popout/123456789", "#settings"] {
            assert!(!route_ok(bad), "{bad}");
        }
    }

    #[test]
    fn names_are_squeezed() {
        assert_eq!(squeeze("  London \t prep "), "London prep");
        assert_eq!(squeeze(" "), "");
        assert_eq!(squeeze(&"x".repeat(80)).len(), 60);
    }

    #[test]
    fn a_template_keeps_the_look_only() {
        let body = json!({"style": {"color": "#3b82f6", "width": 1, "dash": "dotted", "size": 14}, "text": "H1 OB",
                          "points": [{"t": 1, "p": 2}]});
        let Ok(t) = clean_template(body.as_object().unwrap()) else { panic!() };
        assert_eq!(t["style"], json!({"color": "#3b82f6", "width": 1, "dash": "dotted", "size": 14}));
        assert_eq!(t["text"], "H1 OB");
        let Ok(blank) = clean_template(json!({"style": {}, "text": "  "}).as_object().unwrap()) else { panic!() };
        assert_eq!(blank["text"], Value::Null);
        for bad in [json!({}), json!({"style": {"color": "blue"}}), json!({"style": {"width": 9}}),
                    json!({"style": {"width": true}}), json!({"style": {"dash": "wavy"}}), json!({"style": {"size": 2}}),
                    json!({"style": {}, "text": 5})] {
            assert!(clean_template(bad.as_object().unwrap()).is_err(), "{bad}");
        }
    }
}
