//! The Telegram bot token (`secret_store.py`): in the OS credential store, exactly where Python's
//! `keyring` keeps it, so a token saved by either server is found by the other. Without a usable
//! store it is kept in memory until the app closes ("session"); `TELEGRAM_BOT_TOKEN` (the
//! environment or the home folder's `.env`) still works as a fallback, for VPS setups.
//!
//! - macOS: a Keychain generic password, service `Wednesday`, account `telegram_bot_token`
//!   (keyring's macOS backend: delete, then add).
//! - Windows: Credential Manager, as keyring's `WinVaultKeyring` does it: a generic credential
//!   with target `Wednesday` and that user name, else target `telegram_bot_token@Wednesday`; the
//!   blob is the value in UTF-16-LE without a terminator.
//! - Elsewhere (Linux): no credential store here (Python's SecretService isn't ported), so a token
//!   entered in Settings lasts for the session; `.env` is the way on a server.
//!
//! The token never goes into the database, logs or API answers: callers only see its source.

use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::{Map, Value};

pub const SERVICE: &str = "Wednesday";
pub const TELEGRAM_BOT_TOKEN: &str = "telegram_bot_token";

/// The secrets set from Settings, with the environment variable each falls back to (`APP_SECRETS`).
pub const APP_SECRETS: &[(&str, &str)] = &[(TELEGRAM_BOT_TOKEN, "TELEGRAM_BOT_TOKEN")];

/// Services of older versions, removed by [`forget_all`]: the old MT5 password.
pub const OLD_SERVICES: &[&str] = &["Wednesday MT5"];
/// Secrets of removed features (the LLM news brief), still cleared by [`forget_all`].
pub const OLD_SECRETS: &[&str] = &["anthropic_api_key", "openai_api_key"];

/// Remove every secret Wednesday saved in the OS credential store: the installer's fresh install
/// (`Wednesday.exe --forget-secrets`, `secret_store.forget_all`). Known names go first; on Windows,
/// Credential Manager is also searched for any other entry of these services (plugin secrets, the
/// old MT5 password). How many went.
pub fn forget_all() -> usize {
    let gone = forget_known(&OsKeyring);
    #[cfg(windows)]
    let gone = {
        let services: Vec<&str> = std::iter::once(SERVICE).chain(OLD_SERVICES.iter().copied()).collect();
        gone + winvault::forget_services(&services)
    };
    gone
}

/// The app's known secrets, removed from `keyring`; how many were there.
pub fn forget_known(keyring: &dyn Keyring) -> usize {
    let names = APP_SECRETS.iter().map(|(n, _)| *n).chain(OLD_SECRETS.iter().copied());
    names
        .filter(|name| matches!(keyring.get(SERVICE, name), Ok(Some(_))))
        .filter(|name| keyring.delete(SERVICE, name).is_ok())
        .count()
}

/// A credential store. `Err` is "no usable store" (or a broken one), treated as nothing saved.
pub trait Keyring: Send + Sync {
    fn get(&self, service: &str, account: &str) -> Result<Option<String>, String>;
    fn set(&self, service: &str, account: &str, value: &str) -> Result<(), String>;
    fn delete(&self, service: &str, account: &str) -> Result<(), String>;
}

type Env = Box<dyn Fn(&str) -> Option<String> + Send + Sync>;

/// The app's secrets: the credential store, the session fallback and the environment.
pub struct Secrets {
    keyring: Box<dyn Keyring>,
    session: Mutex<HashMap<(String, String), String>>,
    env: Env,
}

impl Secrets {
    /// The OS credential store, with `env` for the fallback variables.
    pub fn os(env: impl Fn(&str) -> Option<String> + Send + Sync + 'static) -> Secrets {
        Secrets::with(Box::new(OsKeyring), env)
    }

    pub fn with(keyring: Box<dyn Keyring>, env: impl Fn(&str) -> Option<String> + Send + Sync + 'static) -> Secrets {
        Secrets { keyring, session: Mutex::new(HashMap::new()), env: Box::new(env) }
    }

    fn key(service: &str, account: &str) -> (String, String) {
        (service.to_string(), account.to_string())
    }

    /// Store a secret; where it went: "saved" (credential store) or "session" (memory).
    pub fn remember(&self, service: &str, account: &str, value: &str) -> &'static str {
        let mut session = self.session.lock().unwrap();
        if self.keyring.set(service, account, value).is_ok() {
            session.remove(&Self::key(service, account));
            return "saved";
        }
        session.insert(Self::key(service, account), value.to_string());
        "session"
    }

    /// The secret and where it came from ("session" or "saved").
    pub fn recall(&self, service: &str, account: &str) -> (Option<String>, Option<&'static str>) {
        if let Some(v) = self.session.lock().unwrap().get(&Self::key(service, account)).filter(|v| !v.is_empty()) {
            return (Some(v.clone()), Some("session"));
        }
        match self.keyring.get(service, account) {
            Ok(Some(v)) if !v.is_empty() => (Some(v), Some("saved")),
            _ => (None, None),
        }
    }

    pub fn forget(&self, service: &str, account: &str) {
        self.session.lock().unwrap().remove(&Self::key(service, account));
        let _ = self.keyring.delete(service, account); // not saved: nothing to forget
    }

    /// An app secret and its source: "session", "saved" or "env"; (None, None) when unset.
    pub fn get_secret(&self, name: &str) -> (Option<String>, Option<&'static str>) {
        let (value, source) = self.recall(SERVICE, name);
        if value.is_some() {
            return (value, source);
        }
        let var = APP_SECRETS.iter().find(|(n, _)| *n == name).map(|(_, v)| *v);
        match var.and_then(|v| (self.env)(v)).filter(|v| !v.is_empty()) {
            Some(v) => (Some(v), Some("env")),
            None => (None, None),
        }
    }

    pub fn set_secret(&self, name: &str, value: &str) -> &'static str {
        self.remember(SERVICE, name, value)
    }

    pub fn forget_secret(&self, name: &str) {
        self.forget(SERVICE, name)
    }

    /// Write-only secret fields of a request: `<name>` sets it, `forget_<name>` removes it.
    /// Whether anything changed.
    pub fn update_secrets(&self, body: &Map<String, Value>, names: &[&str]) -> bool {
        let mut changed = false;
        for name in names {
            if super::py_truthy(body.get(&format!("forget_{name}"))) {
                self.forget_secret(name);
                changed = true;
            }
            let value = super::py_str_or_empty(body.get(*name));
            let value = value.trim();
            if !value.is_empty() {
                self.set_secret(name, value);
                changed = true;
            }
        }
        changed
    }
}

/// The operating system's credential store, as Python's keyring uses it.
pub struct OsKeyring;

#[cfg(target_os = "macos")]
impl Keyring for OsKeyring {
    fn get(&self, service: &str, account: &str) -> Result<Option<String>, String> {
        use security_framework::passwords::get_generic_password;
        const NOT_FOUND: i32 = -25300; // errSecItemNotFound
        match get_generic_password(service, account) {
            Ok(data) => String::from_utf8(data).map(Some).map_err(|_| "not UTF-8".to_string()),
            Err(e) if e.code() == NOT_FOUND => Ok(None),
            Err(e) => Err(format!("keychain error {}", e.code())),
        }
    }

    fn set(&self, service: &str, account: &str, value: &str) -> Result<(), String> {
        use security_framework::passwords::{delete_generic_password, set_generic_password};
        let _ = delete_generic_password(service, account); // as keyring: delete, then add
        set_generic_password(service, account, value.as_bytes()).map_err(|e| format!("keychain error {}", e.code()))
    }

    fn delete(&self, service: &str, account: &str) -> Result<(), String> {
        security_framework::passwords::delete_generic_password(service, account)
            .map_err(|e| format!("keychain error {}", e.code()))
    }
}

#[cfg(windows)]
impl Keyring for OsKeyring {
    fn get(&self, service: &str, account: &str) -> Result<Option<String>, String> {
        Ok(winvault::resolve(service, account)?.map(|c| c.value))
    }

    fn set(&self, service: &str, account: &str, value: &str) -> Result<(), String> {
        // Like WinVaultKeyring.set_password: whatever is saved under the service name moves to
        // `<its user>@<service>` first, then the new value takes the service name.
        if let Some(existing) = winvault::read(service)? {
            let target = winvault::compound(&existing.user, service);
            winvault::write(&target, &existing.user, &existing.value)?;
        }
        winvault::write(service, account, value)
    }

    fn delete(&self, service: &str, account: &str) -> Result<(), String> {
        let mut deleted = false;
        for target in [service.to_string(), winvault::compound(account, service)] {
            if winvault::read(&target)?.is_some_and(|c| c.user == account) {
                winvault::delete(&target)?;
                deleted = true;
            }
        }
        if deleted { Ok(()) } else { Err("not saved".into()) }
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
impl Keyring for OsKeyring {
    fn get(&self, _: &str, _: &str) -> Result<Option<String>, String> {
        Err("no credential store".into())
    }

    fn set(&self, _: &str, _: &str, _: &str) -> Result<(), String> {
        Err("no credential store".into())
    }

    fn delete(&self, _: &str, _: &str) -> Result<(), String> {
        Err("no credential store".into())
    }
}

/// How Python's keyring reads a Windows credential blob: UTF-16 (a BOM picks the byte order,
/// little-endian without one), else UTF-8.
pub fn decode_blob(blob: &[u8]) -> Option<String> {
    if blob.len() % 2 == 0 {
        let (le, body) = match blob {
            [0xff, 0xfe, rest @ ..] => (true, rest),
            [0xfe, 0xff, rest @ ..] => (false, rest),
            _ => (true, blob),
        };
        let units: Vec<u16> = body
            .chunks_exact(2)
            .map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) })
            .collect();
        if let Ok(s) = String::from_utf16(&units) {
            return Some(s);
        }
    }
    String::from_utf8(blob.to_vec()).ok()
}

/// A value as keyring writes it into a Windows credential: UTF-16-LE, no terminator.
pub fn encode_blob(value: &str) -> Vec<u8> {
    value.encode_utf16().flat_map(u16::to_le_bytes).collect()
}

#[cfg(windows)]
mod winvault {
    use windows_sys::Win32::Foundation::{GetLastError, ERROR_NOT_FOUND};
    use windows_sys::Win32::Security::Credentials::{
        CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_PERSIST_ENTERPRISE, CRED_TYPE_GENERIC,
    };

    pub struct Cred {
        pub user: String,
        pub value: String,
    }

    pub fn compound(user: &str, service: &str) -> String {
        format!("{user}@{service}")
    }

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    unsafe fn from_wide(p: *const u16) -> String {
        if p.is_null() {
            return String::new();
        }
        let mut n = 0;
        while *p.add(n) != 0 {
            n += 1;
        }
        String::from_utf16_lossy(std::slice::from_raw_parts(p, n))
    }

    /// `_resolve_credential`: the service name if its user matches, else `<account>@<service>`.
    pub fn resolve(service: &str, account: &str) -> Result<Option<Cred>, String> {
        match read(service)? {
            Some(c) if account.is_empty() || c.user == account => Ok(Some(c)),
            _ => read(&compound(account, service)),
        }
    }

    pub fn read(target: &str) -> Result<Option<Cred>, String> {
        let name = wide(target);
        let mut cred: *mut CREDENTIALW = std::ptr::null_mut();
        unsafe {
            if CredReadW(name.as_ptr(), CRED_TYPE_GENERIC, 0, &mut cred) == 0 {
                let err = GetLastError();
                return if err == ERROR_NOT_FOUND { Ok(None) } else { Err(format!("CredRead failed ({err})")) };
            }
            let c = &*cred;
            let blob = if c.CredentialBlob.is_null() {
                Vec::new()
            } else {
                std::slice::from_raw_parts(c.CredentialBlob, c.CredentialBlobSize as usize).to_vec()
            };
            let user = from_wide(c.UserName);
            CredFree(cred as *const _);
            let value = super::decode_blob(&blob).ok_or("unreadable credential")?;
            Ok(Some(Cred { user, value }))
        }
    }

    pub fn write(target: &str, user: &str, value: &str) -> Result<(), String> {
        let mut name = wide(target);
        let mut user = wide(user);
        let mut comment = wide("Stored using python-keyring");
        let mut blob = super::encode_blob(value);
        let cred = CREDENTIALW {
            Type: CRED_TYPE_GENERIC,
            TargetName: name.as_mut_ptr(),
            UserName: user.as_mut_ptr(),
            Comment: comment.as_mut_ptr(),
            CredentialBlobSize: blob.len() as u32,
            CredentialBlob: blob.as_mut_ptr(),
            Persist: CRED_PERSIST_ENTERPRISE,
            ..Default::default()
        };
        unsafe {
            if CredWriteW(&cred, 0) == 0 {
                return Err(format!("CredWrite failed ({})", GetLastError()));
            }
        }
        Ok(())
    }

    /// Delete the generic credentials named `<service>` or `<account>@<service>` (keyring's
    /// naming) for any of `services`; how many went (`_forget_windows_credentials`).
    pub fn forget_services(services: &[&str]) -> usize {
        use windows_sys::Win32::Security::Credentials::CredEnumerateW;
        let mut count: u32 = 0;
        let mut list: *mut *mut CREDENTIALW = std::ptr::null_mut();
        let mut targets = Vec::new();
        unsafe {
            // No filter: Windows only takes a wildcard at the end of one, so match here.
            if CredEnumerateW(std::ptr::null(), 0, &mut count, &mut list) == 0 {
                return 0; // nothing saved at all
            }
            for i in 0..count as usize {
                let c = &**list.add(i);
                if c.Type != CRED_TYPE_GENERIC {
                    continue;
                }
                let target = from_wide(c.TargetName);
                if services.iter().any(|s| target == *s || target.ends_with(&format!("@{s}"))) {
                    targets.push(target);
                }
            }
            CredFree(list as *const _);
        }
        targets.iter().filter(|t| delete(t).is_ok()).count()
    }

    pub fn delete(target: &str) -> Result<(), String> {
        let name = wide(target);
        unsafe {
            if CredDeleteW(name.as_ptr(), CRED_TYPE_GENERIC, 0) == 0 {
                let err = GetLastError();
                if err != ERROR_NOT_FOUND {
                    return Err(format!("CredDelete failed ({err})"));
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    /// An in-memory credential store; `broken` acts as no store at all.
    #[derive(Clone, Default)]
    pub struct FakeKeyring {
        pub items: Arc<Mutex<HashMap<(String, String), String>>>,
        pub broken: bool,
    }

    impl Keyring for FakeKeyring {
        fn get(&self, s: &str, a: &str) -> Result<Option<String>, String> {
            if self.broken {
                return Err("none".into());
            }
            Ok(self.items.lock().unwrap().get(&(s.into(), a.into())).cloned())
        }
        fn set(&self, s: &str, a: &str, v: &str) -> Result<(), String> {
            if self.broken {
                return Err("none".into());
            }
            self.items.lock().unwrap().insert((s.into(), a.into()), v.into());
            Ok(())
        }
        fn delete(&self, s: &str, a: &str) -> Result<(), String> {
            if self.broken {
                return Err("none".into());
            }
            self.items.lock().unwrap().remove(&(s.into(), a.into())).map(|_| ()).ok_or("not saved".into())
        }
    }

    fn body(v: Value) -> Map<String, Value> {
        v.as_object().unwrap().clone()
    }

    #[test]
    fn saved_then_env_and_forget() {
        let kr = FakeKeyring::default();
        let s = Secrets::with(Box::new(kr.clone()), |k| (k == "TELEGRAM_BOT_TOKEN").then(|| "from-env".to_string()));
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (Some("from-env".into()), Some("env")));
        assert!(s.update_secrets(&body(serde_json::json!({"telegram_bot_token": "  abc  "})), &[TELEGRAM_BOT_TOKEN]));
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (Some("abc".into()), Some("saved")));
        assert_eq!(kr.items.lock().unwrap().get(&("Wednesday".into(), "telegram_bot_token".into())).unwrap(), "abc");
        assert!(!s.update_secrets(&body(serde_json::json!({"telegram_bot_token": " ", "forget_telegram_bot_token": false})),
                                  &[TELEGRAM_BOT_TOKEN]));
        assert!(s.update_secrets(&body(serde_json::json!({"forget_telegram_bot_token": true})), &[TELEGRAM_BOT_TOKEN]));
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (Some("from-env".into()), Some("env")));
        assert!(kr.items.lock().unwrap().is_empty());
    }

    #[test]
    fn forget_known_clears_the_app_and_old_secrets() {
        let kr = FakeKeyring::default();
        for name in ["telegram_bot_token", "openai_api_key"] {
            kr.set(SERVICE, name, "x").unwrap();
        }
        kr.set("Other", "telegram_bot_token", "keep").unwrap();
        assert_eq!(forget_known(&kr), 2);
        assert_eq!(kr.items.lock().unwrap().len(), 1); // another service's entry stays
        assert_eq!(forget_known(&kr), 0);
    }

    #[test]
    fn session_without_a_store() {
        let kr = FakeKeyring { broken: true, ..Default::default() };
        let s = Secrets::with(Box::new(kr), |_| None);
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (None, None));
        assert_eq!(s.set_secret(TELEGRAM_BOT_TOKEN, "x"), "session");
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (Some("x".into()), Some("session")));
        s.forget_secret(TELEGRAM_BOT_TOKEN);
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (None, None));
    }

    #[test]
    fn session_wins_over_saved() {
        let kr = FakeKeyring::default();
        kr.set(SERVICE, TELEGRAM_BOT_TOKEN, "saved").unwrap();
        let s = Secrets::with(Box::new(kr), |_| Some("env".into()));
        s.session.lock().unwrap().insert((SERVICE.into(), TELEGRAM_BOT_TOKEN.into()), "mem".into());
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (Some("mem".into()), Some("session")));
        // Saving to the store clears the session copy.
        assert_eq!(s.set_secret(TELEGRAM_BOT_TOKEN, "new"), "saved");
        assert_eq!(s.get_secret(TELEGRAM_BOT_TOKEN), (Some("new".into()), Some("saved")));
    }

    /// The real credential store against Python's keyring, by hand: save a value with
    /// `keyring.set_password("Wednesday", "<account>", "<value>")`, then
    /// `WED_KEYRING_ACCOUNT=<account> WED_KEYRING_VALUE=<value> cargo test -- --ignored os_keyring`.
    #[test]
    #[ignore]
    fn os_keyring_reads_what_python_saved() {
        let (Ok(account), Ok(value)) = (std::env::var("WED_KEYRING_ACCOUNT"), std::env::var("WED_KEYRING_VALUE")) else {
            return;
        };
        assert_eq!(OsKeyring.get(SERVICE, &account).unwrap().as_deref(), Some(value.as_str()));
    }

    #[test]
    fn windows_blobs_as_keyring_writes_and_reads_them() {
        assert_eq!(encode_blob("ab"), vec![b'a', 0, b'b', 0]);
        assert_eq!(decode_blob(&encode_blob("123:AAé")).as_deref(), Some("123:AAé"));
        assert_eq!(decode_blob(&[0xff, 0xfe, b'x', 0]).as_deref(), Some("x"));
        assert_eq!(decode_blob(&[0xfe, 0xff, 0, b'x']).as_deref(), Some("x"));
        assert_eq!(decode_blob(b"abc").as_deref(), Some("abc")); // odd length: UTF-8
    }
}
