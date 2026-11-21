//! Download a paid plugin (Wednesday EE) from the licence server, check it's genuine, unpack it
//! (`plugin_install.py`, for plugin API 2).
//!
//! Wednesday EE isn't published: whoever has a licence key gets it from the licence server.
//! `POST /v1/download {"key", "device_id", "target"}` answers
//!
//! ```text
//! {"kid": "momentum-2026", "manifest": "<base64url JSON>", "signature": "<base64url>",
//!  "package": "<base64 of the .zip>"}
//! ```
//!
//! The manifest names the plugin, its version, plugin API, target triple, the zip's file name and
//! SHA-256; the signature is Ed25519 over `WEDPKG1.<manifest>` with a key listed in [`super::keys`].
//! Only a zip that matches a signed manifest, for this API and this build's target, is unpacked,
//! into `data/plugins/<name>/`. Nothing downloaded runs before that check.

use std::collections::HashMap;
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use base64::Engine as _;
use serde_json::{json, Value};

use crate::store::Store;

pub const PREFIX: &str = "WEDPKG1";
// A copy of DEFAULT_SERVER in src/wednesday/plugin_install.py, which is a TEMP address (a Mac on
// the LAN, for a quick test): change both together when it goes back to the real server.
pub const DEFAULT_SERVER: &str = "http://192.168.100.87:8790";
pub const SERVER_ENV: &str = "WEDNESDAY_LICENCE_SERVER";
pub const PLUGINS_DIR_ENV: &str = "WEDNESDAY_PLUGINS_DIR";
/// Shared with Wednesday EE's licence, which binds tokens to it.
pub const DEVICE_SETTING: &str = "ee_device";
/// Plugins Wednesday will download and run.
pub const ALLOWED: &[&str] = &["wednesday_ee"];
pub const MAX_BYTES: usize = 50 * 1024 * 1024;
/// What the unpacked files may add up to (a zip bomb stops here).
pub const MAX_UNPACKED: u64 = 4 * MAX_BYTES as u64;
/// The plugin API this Wednesday runs.
pub const API: i64 = 2;
/// This build's target triple; a downloaded plugin must be built for it.
pub const TARGET: &str = env!("WEDNESDAY_TARGET");
/// What a plugin's folder holds besides the plugin: the signed manifest it came with.
pub const MANIFEST_FILE: &str = ".wednesday-plugin.json";

/// The plugin couldn't be downloaded or isn't genuine; the message is for the user.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallError(pub String);

impl std::fmt::Display for InstallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for InstallError {}

fn err<T>(msg: impl Into<String>) -> Result<T, InstallError> {
    Err(InstallError(msg.into()))
}

fn damaged<T>() -> Result<T, InstallError> {
    err("The download is damaged; try again")
}

/// A setting from the environment, else `home/.env` (where Python's CLI loaded it from).
fn env(home: &Path, name: &str) -> Option<String> {
    crate::config::env_setting(name, &home.join(".env")).filter(|v| !v.trim().is_empty())
}

/// `WEDNESDAY_PLUGINS_DIR` (relative to `home`), else `<home>/data/plugins`.
pub fn plugins_dir(home: &Path) -> PathBuf {
    match env(home, PLUGINS_DIR_ENV) {
        Some(dir) => home.join(dir),
        None => home.join("data").join("plugins"),
    }
}

/// The licence server: `WEDNESDAY_LICENCE_SERVER`, else [`DEFAULT_SERVER`].
pub fn server_url(home: &Path) -> String {
    licence_server_env(home).unwrap_or_else(|| DEFAULT_SERVER.into()).trim_end_matches('/').to_string()
}

/// `WEDNESDAY_LICENCE_SERVER` when it is set (passed on to plugins).
pub fn licence_server_env(home: &Path) -> Option<String> {
    env(home, SERVER_ENV)
}

/// Random bytes from the OS.
pub fn random_bytes<const N: usize>() -> [u8; N] {
    use ring::rand::SecureRandom;
    let mut b = [0u8; N];
    ring::rand::SystemRandom::new().fill(&mut b).expect("the OS random number generator failed");
    b
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// This install's id, kept in the database (setting `ee_device` `{"id"}`), made on first use as
/// a UUID4's hex, as `plugin_install.device_id` makes it.
pub async fn device_id(store: Option<&Store>) -> String {
    if let Some(store) = store {
        if let Ok(Some(stored)) = store.get_setting(DEVICE_SETTING).await {
            match &stored["id"] {
                Value::String(s) if !s.is_empty() => return s.clone(),
                Value::Number(n) => return n.to_string(),
                _ => {}
            }
        }
    }
    let mut b = random_bytes::<16>();
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let device = hex(&b);
    if let Some(store) = store {
        if let Err(e) = store.set_setting(DEVICE_SETTING, &json!({"id": device})).await {
            log::warn!("couldn't keep this install's device id: {e}");
        }
    }
    device
}

/// The trusted keys ([`super::keys::TRUSTED`]), decoded.
pub fn trusted() -> HashMap<String, Vec<u8>> {
    super::keys::TRUSTED
        .iter()
        .filter_map(|(kid, b64)| Some((kid.to_string(), base64::engine::general_purpose::STANDARD.decode(b64).ok()?)))
        .collect()
}

/// Ask the licence server for the plugin a licence key gives, for this build's target.
pub async fn fetch(key: &str, device: &str, url: &str) -> Result<Value, InstallError> {
    log::info!("downloading Wednesday EE from {url} (set {SERVER_ENV} to change)");
    let unreachable = || InstallError(format!("Can't reach the licence server at {url} to download Wednesday EE"));
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .map_err(|_| unreachable())?;
    let body = json!({"key": key, "device_id": device, "target": TARGET});
    let mut res = client
        .post(format!("{url}/v1/download"))
        .header("content-type", "application/json")
        .body(body.to_string())
        .send().await.map_err(|_| unreachable())?;
    let status = res.status();
    let mut data = Vec::new();
    loop {
        let next: Result<Option<axum::body::Bytes>, reqwest::Error> = res.chunk().await;
        match next {
            Ok(Some(chunk)) => {
                data.extend_from_slice(&chunk);
                if data.len() > MAX_BYTES * 2 {
                    return err("The download is larger than 50 MB");
                }
            }
            Ok(None) => break,
            Err(_) if status.is_success() => return Err(unreachable()),
            Err(_) => break,
        }
    }
    if !status.is_success() {
        let detail = serde_json::from_slice::<Value>(&data).ok().map(|v| v["detail"].clone());
        return Err(InstallError(match detail {
            Some(Value::String(s)) if !s.is_empty() => s,
            Some(v) if !v.is_null() => v.to_string(),
            _ => format!("The licence server refused the download ({})", status.as_u16()),
        }));
    }
    serde_json::from_slice(&data).map_err(|_| unreachable())
}

/// A signed manifest that checked out.
#[derive(Debug, Clone)]
pub struct Manifest {
    pub name: String,
    pub version: String,
    pub raw: Value,
}

fn b64url(text: &str) -> Option<Vec<u8>> {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(text.trim_end_matches('=')).ok()
}

/// Python's `repr` of a JSON value, for "Wednesday doesn't install 'x'".
fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::String(s) => format!("'{s}'"),
        other => other.to_string(),
    }
}

/// What a download is checked against: the keys, the plugin names and the target.
#[derive(Debug, Clone)]
pub struct Trust {
    pub keys: HashMap<String, Vec<u8>>,
    pub allowed: Vec<String>,
    pub target: String,
}

impl Default for Trust {
    /// [`super::keys::TRUSTED`], [`ALLOWED`] and this build's [`TARGET`].
    fn default() -> Self {
        Trust { keys: trusted(), allowed: ALLOWED.iter().map(|s| s.to_string()).collect(), target: TARGET.into() }
    }
}

/// (manifest, zip bytes) when the signature, hash, name, API and target check out.
pub fn verify(answer: &Value, trust: &Trust) -> Result<(Manifest, Vec<u8>), InstallError> {
    let (keys, target) = (&trust.keys, trust.target.as_str());
    let (Some(manifest_b64), Some(signature), Some(package)) =
        (answer["manifest"].as_str(), answer["signature"].as_str(), answer["package"].as_str())
    else {
        return damaged();
    };
    let Some(signature) = b64url(signature) else { return damaged() };
    let Some(manifest) = b64url(manifest_b64).and_then(|m| serde_json::from_slice::<Value>(&m).ok()) else {
        return damaged();
    };
    if !manifest.is_object() {
        return damaged();
    }
    // Base64 is 4 characters for 3 bytes: over this, the zip is over the limit.
    if package.len() > MAX_BYTES / 3 * 4 + 8 {
        return err("The download is larger than 50 MB");
    }
    let Ok(zip) = base64::engine::general_purpose::STANDARD.decode(package.trim()) else { return damaged() };
    // The key id is signed inside the manifest; the answer repeats it.
    let kid = manifest["kid"].as_str().or(answer["kid"].as_str()).unwrap_or("");
    let Some(public) = keys.get(kid) else {
        return err("The download was signed with a key this Wednesday doesn't know; update Wednesday");
    };
    let key = ring::signature::UnparsedPublicKey::new(&ring::signature::ED25519, public);
    if key.verify(format!("{PREFIX}.{manifest_b64}").as_bytes(), &signature).is_err() {
        return err("The download isn't genuine (its signature doesn't match)");
    }
    let sha = hex(ring::digest::digest(&ring::digest::SHA256, &zip).as_ref());
    if manifest["sha256"].as_str() != Some(sha.as_str()) {
        return err("The download doesn't match its signature; try again");
    }
    let name = manifest["name"].as_str().unwrap_or("");
    if !trust.allowed.iter().any(|a| a == name) {
        return Err(InstallError(format!("Wednesday doesn't install {}", py_repr(&manifest["name"]))));
    }
    if manifest["api"].as_i64() != Some(API) {
        return err("The download was made for another version of Wednesday; update Wednesday");
    }
    if manifest["target"].as_str() != Some(target) {
        return Err(InstallError(format!(
            "The download was built for another system ({}), not this one ({target}); try again",
            manifest["target"].as_str().unwrap_or("unknown")
        )));
    }
    if zip.len() > MAX_BYTES {
        return err("The download is larger than 50 MB");
    }
    let version = match &manifest["version"] {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        v => v.to_string(),
    };
    Ok((Manifest { name: name.to_string(), version, raw: manifest }, zip))
}

fn valid_name(name: &str) -> bool {
    !name.is_empty() && name.bytes().all(|c| c.is_ascii_lowercase() || c == b'_')
}

/// A plugin's `plugin.json`: what it is and which file runs.
#[derive(Debug, Clone)]
pub struct PluginJson {
    pub name: String,
    pub version: Option<String>,
    pub api: Option<i64>,
    pub exe: Option<String>,
}

pub fn read_plugin_json(dir: &Path) -> Option<PluginJson> {
    let v: Value = serde_json::from_slice(&std::fs::read(dir.join("plugin.json")).ok()?).ok()?;
    Some(PluginJson {
        name: v["name"].as_str()?.to_string(),
        version: v["version"].as_str().map(String::from),
        api: v["api"].as_i64(),
        exe: v["exe"].as_str().map(String::from),
    })
}

/// The executable's name is a plain file name in the plugin's folder.
pub fn safe_exe(exe: &str) -> bool {
    let p = Path::new(exe);
    !exe.is_empty() && p.components().count() == 1 && matches!(p.components().next(), Some(Component::Normal(_)))
}

/// Unpack a verified zip to `<root>/<name>.new/` and check it holds a plugin; returns that folder.
/// [`swap`] puts it in place.
pub fn extract(manifest: &Manifest, zip: &[u8], root: &Path) -> Result<PathBuf, InstallError> {
    let name = &manifest.name;
    if !valid_name(name) {
        return err("Bad package name");
    }
    std::fs::create_dir_all(root).map_err(|e| InstallError(format!("Can't write to {}: {e}", root.display())))?;
    let staged = root.join(format!("{name}.new"));
    let _ = std::fs::remove_dir_all(&staged);
    let result = unzip(zip, &staged).and_then(|()| check_staged(manifest, &staged));
    if result.is_err() {
        let _ = std::fs::remove_dir_all(&staged);
    }
    result.map(|()| staged)
}

fn unzip(zip: &[u8], dest: &Path) -> Result<(), InstallError> {
    let mut archive = zip::ZipArchive::new(std::io::Cursor::new(zip)).or_else(|_| damaged())?;
    // Every entry first: nothing is written when one of them would land outside the folder.
    for i in 0..archive.len() {
        let file = archive.by_index_raw(i).or_else(|_| damaged())?;
        let raw = file.name();
        let escapes = raw.starts_with(['/', '\\'])
            || raw.contains(':')
            || raw.split(['/', '\\']).any(|part| part == "..")
            || file.enclosed_name().is_none()
            || file.is_symlink();
        if escapes {
            return err("The download has a file outside its folder");
        }
    }
    std::fs::create_dir_all(dest).map_err(|e| InstallError(format!("Can't write to {}: {e}", dest.display())))?;
    let mut total = 0u64;
    for i in 0..archive.len() {
        let mut file = archive.by_index(i).or_else(|_| damaged())?;
        let rel = file.enclosed_name().ok_or_else(|| InstallError("The download has a file outside its folder".into()))?;
        let path = dest.join(rel);
        if file.is_dir() {
            std::fs::create_dir_all(&path).or_else(|_| damaged())?;
            continue;
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).or_else(|_| damaged())?;
        }
        let mut out = std::fs::File::create(&path).or_else(|_| damaged())?;
        let left = MAX_UNPACKED.saturating_sub(total);
        let copied = std::io::copy(&mut (&mut file).take(left + 1), &mut out).or_else(|_| damaged())?;
        total += copied;
        if total > MAX_UNPACKED {
            return err("The download is too large once unpacked");
        }
    }
    Ok(())
}

fn check_staged(manifest: &Manifest, dir: &Path) -> Result<(), InstallError> {
    let Some(pj) = read_plugin_json(dir) else { return damaged() };
    let exe = pj.exe.clone().unwrap_or_default();
    if pj.name != manifest.name || pj.api != Some(API) || !safe_exe(&exe) || !dir.join(&exe).is_file() {
        return damaged();
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir.join(&exe), std::fs::Permissions::from_mode(0o755)).or_else(|_| damaged())?;
    }
    let text = serde_json::to_string_pretty(&manifest.raw).unwrap_or_default();
    std::fs::write(dir.join(MANIFEST_FILE), text).or_else(|_| damaged())?;
    Ok(())
}

/// Put `<root>/<name>.new/` in place as `<root>/<name>/`, replacing an older copy whole. The old
/// copy's program must have stopped first (Windows can't move a running program's folder).
pub fn swap(root: &Path, name: &str) -> Result<PathBuf, InstallError> {
    let staged = root.join(format!("{name}.new"));
    let dest = root.join(name);
    let old = root.join(format!(".old-{name}"));
    let _ = std::fs::remove_dir_all(&old);
    let failed = |e: std::io::Error| InstallError(format!("Can't replace {}: {e}; restart Wednesday and try again", dest.display()));
    if dest.exists() {
        std::fs::rename(&dest, &old).map_err(failed)?;
    }
    if let Err(e) = std::fs::rename(&staged, &dest) {
        let _ = std::fs::rename(&old, &dest);
        return Err(failed(e));
    }
    let _ = std::fs::remove_dir_all(&old);
    Ok(dest)
}

/// Unpack a verified zip to `<root>/<name>/` ([`extract`] then [`swap`]).
pub fn unpack(manifest: &Manifest, zip: &[u8], root: &Path) -> Result<PathBuf, InstallError> {
    extract(manifest, zip, root)?;
    swap(root, &manifest.name)
}
