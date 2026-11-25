//! The login for a dashboard reached over the network (`auth.py`, docs/design-storage-users.md).
//!
//! On 127.0.0.1 (the desktop app, `make serve`) there is no login. On any other host it is on
//! (`--insecure` turns it off, for development; `--login` turns it on behind a proxy on this
//! machine), and every `/api` route but [`PUBLIC`] needs a session.
//!
//! Accounts live in `users` with argon2id hashes (an old scrypt hash from
//! `XAU_AUTH_PASSWORD_HASH` works and is rehashed at the next login). The first account comes
//! from a one-time setup code in the log, `wednesday-rs --create-user`, or once from
//! `XAU_AUTH_PASSWORD(_HASH)`. A session is a random token in an HttpOnly, SameSite=Strict
//! cookie; `sessions` keeps only its SHA-256. Sessions last 30 days, are renewed as they are
//! used and end when the password changes. Failed logins are limited per address and username.

use std::collections::{HashMap, VecDeque};
use std::net::SocketAddr;
use std::sync::Mutex;
use std::time::{Duration as StdDuration, Instant};

use argon2::password_hash::{rand_core::OsRng, PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use axum::extract::{ConnectInfo, Path, Request, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine as _;
use chrono::{DateTime, Duration, Utc};
use serde_json::{json, Map, Value};

use crate::store::{Arg, Store};
use crate::{detail, object_body, App};

pub const SESSION_COOKIE: &str = "wednesday_session";
const SESSION_DAYS: i64 = 30;
/// A session's expiry moves at most this often: one write an hour, not one per request.
const RENEW_SECONDS: i64 = 3600;
/// The routes open without a session.
pub const PUBLIC: [&str; 4] = ["/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup"];
const MAX_FAILURES: usize = 10;
const FAILURE_WINDOW: StdDuration = StdDuration::from_secs(15 * 60);
const SETUP_LIFE: StdDuration = StdDuration::from_secs(3600);
const MIN_PASSWORD: usize = 8;
const MAX_PASSWORD: usize = 256;
const CODE_ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
pub const NO_LOGIN: &str = "This dashboard has no login: it is served on this computer only (127.0.0.1)";

/// A refused auth request: the status and what to do next.
#[derive(Debug)]
pub struct AuthError {
    pub status: StatusCode,
    pub message: String,
    pub retry_after: Option<u64>,
}

fn refuse(status: StatusCode, message: impl Into<String>) -> AuthError {
    AuthError { status, message: message.into(), retry_after: None }
}

fn internal(e: String) -> AuthError {
    log::error!("login: {e}");
    refuse(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        let mut res = detail(self.status, self.message);
        if let Some(wait) = self.retry_after {
            res.headers_mut().insert(header::RETRY_AFTER, HeaderValue::from(wait));
        }
        res
    }
}

/// The timestamps both servers write (`auth.now_iso`), so they compare as text.
pub fn now_iso(when: DateTime<Utc>) -> String {
    when.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
}

/// Equal bytes, in a time that doesn't depend on where they differ.
fn same_bytes(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn sha(token: &str) -> String {
    ring::digest::digest(&ring::digest::SHA256, token.as_bytes()).as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

fn random_bytes<const N: usize>() -> [u8; N] {
    let mut out = [0u8; N];
    ring::rand::SecureRandom::fill(&ring::rand::SystemRandom::new(), &mut out).expect("the OS random source");
    out
}

// ---- passwords ----

fn hasher() -> argon2::Argon2<'static> {
    let params = argon2::Params::new(19456, 2, 1, Some(32)).expect("argon2 parameters");
    argon2::Argon2::new(argon2::Algorithm::Argon2id, argon2::Version::V0x13, params)
}

/// An argon2id hash in the PHC form, as argon2-cffi writes it.
pub fn hash_password(password: &str) -> String {
    let salt = SaltString::generate(&mut OsRng);
    hasher().hash_password(password.as_bytes(), &salt).expect("argon2 hash").to_string()
}

/// Whether `password` matches an argon2id hash or an old `scrypt$n$r$p$salt$hash`.
pub fn check_password(password: &str, encoded: &str) -> bool {
    if encoded.starts_with("$argon2") {
        return PasswordHash::new(encoded).is_ok_and(|h| hasher().verify_password(password.as_bytes(), &h).is_ok());
    }
    let parts: Vec<&str> = encoded.split('$').collect();
    let [algo, n, r, p, salt, digest] = parts[..] else { return false };
    let b64 = base64::engine::general_purpose::STANDARD;
    let (Ok(n), Ok(r), Ok(p), Ok(salt), Ok(want)) =
        (n.parse::<u64>(), r.parse::<u32>(), p.parse::<u32>(), b64.decode(salt), b64.decode(digest))
    else {
        return false;
    };
    if algo != "scrypt" || !n.is_power_of_two() || n < 2 || want.is_empty() {
        return false;
    }
    let Ok(params) = scrypt::Params::new(n.trailing_zeros() as u8, r, p, want.len()) else { return false };
    let mut got = vec![0u8; want.len()];
    if scrypt::scrypt(password.as_bytes(), &salt, &params, &mut got).is_err() {
        return false;
    }
    same_bytes(&got, &want)
}

/// [`hash_password`] off the async threads: argon2 takes tens of milliseconds on purpose.
async fn hash_blocking(password: String) -> String {
    tokio::task::spawn_blocking(move || hash_password(&password)).await.expect("hash task")
}

async fn check_blocking(password: String, encoded: String) -> bool {
    tokio::task::spawn_blocking(move || check_password(&password, &encoded)).await.unwrap_or(false)
}

pub fn clean_username(value: &str) -> Result<String, AuthError> {
    let name = value.trim().to_lowercase();
    let ok = (2..=64).contains(&name.len())
        && name.chars().next().is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-'));
    if ok {
        Ok(name)
    } else {
        Err(refuse(StatusCode::UNPROCESSABLE_ENTITY,
                   "Use 2 to 64 letters, digits, dots, dashes or underscores for the username"))
    }
}

pub fn clean_password(value: Option<&Value>) -> Result<String, AuthError> {
    let password = value.and_then(Value::as_str).unwrap_or("");
    if (MIN_PASSWORD..=MAX_PASSWORD).contains(&password.chars().count()) {
        Ok(password.to_string())
    } else {
        Err(refuse(StatusCode::UNPROCESSABLE_ENTITY,
                   format!("Use a password of {MIN_PASSWORD} to {MAX_PASSWORD} characters")))
    }
}

/// "Chrome on Windows" from a User-Agent, for the devices list (`auth.device_name`).
pub fn device_name(user_agent: Option<&str>) -> Option<String> {
    let ua = user_agent.unwrap_or("");
    let os = [("Windows", "Windows"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
              ("Mac OS", "macOS"), ("Linux", "Linux")]
        .into_iter()
        .find(|(k, _)| ua.contains(k))
        .map(|(_, n)| n);
    let browser = [("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")]
        .into_iter()
        .find(|(k, _)| ua.contains(k))
        .map(|(_, n)| n);
    match (browser, os) {
        (Some(b), Some(o)) => Some(format!("{b} on {o}")),
        (Some(n), None) | (None, Some(n)) => Some(n.to_string()),
        (None, None) => (!ua.is_empty()).then(|| ua.chars().take(100).collect()),
    }
}

fn setup_code() -> String {
    let raw: String = random_bytes::<12>().iter().map(|b| CODE_ALPHABET[*b as usize % CODE_ALPHABET.len()] as char).collect();
    format!("{}-{}-{}", &raw[..4], &raw[4..8], &raw[8..])
}

fn same_code(given: &str, code: &str) -> bool {
    let norm = |s: &str| s.chars().filter(|c| !c.is_whitespace() && *c != '-').collect::<String>().to_uppercase();
    same_bytes(norm(given).as_bytes(), norm(code).as_bytes())
}

// ---- failed logins ----

/// Failed attempts per key (`ip:...`, `user:...`).
#[derive(Default)]
pub struct RateLimiter {
    failures: Mutex<HashMap<String, VecDeque<Instant>>>,
}

impl RateLimiter {
    fn check(&self, keys: &[String]) -> Result<(), AuthError> {
        let mut all = self.failures.lock().unwrap();
        for key in keys {
            let q = all.entry(key.clone()).or_default();
            while q.front().is_some_and(|t| t.elapsed() > FAILURE_WINDOW) {
                q.pop_front();
            }
            if q.len() >= MAX_FAILURES {
                let wait = (FAILURE_WINDOW.saturating_sub(q[0].elapsed())).as_secs() + 1;
                return Err(AuthError {
                    status: StatusCode::TOO_MANY_REQUESTS,
                    message: format!("Too many failed attempts. Try again in {} min.", wait.div_ceil(60)),
                    retry_after: Some(wait),
                });
            }
        }
        Ok(())
    }

    fn fail(&self, keys: &[String]) {
        let mut all = self.failures.lock().unwrap();
        for key in keys {
            all.entry(key.clone()).or_default().push_back(Instant::now());
        }
    }

    fn clear(&self, keys: &[String]) {
        let mut all = self.failures.lock().unwrap();
        for key in keys {
            all.remove(key);
        }
    }
}

// ---- the login ----

fn s(v: &str) -> Arg {
    Arg::Str(Some(v.into()))
}

fn field<'a>(row: &'a Map<String, Value>, key: &str) -> &'a str {
    row.get(key).and_then(Value::as_str).unwrap_or("")
}

fn public_user(user: &Map<String, Value>) -> Value {
    json!({"username": user.get("username"), "name": user.get("name"), "email": user.get("email"),
           "role": user.get("role")})
}

/// Accounts and sessions on the store; `enabled` is false on loopback (no login).
pub struct Auth {
    store: Option<Store>,
    pub enabled: bool,
    limiter: RateLimiter,
    code: Mutex<Option<(String, Instant)>>,
    setup_lock: tokio::sync::Mutex<()>,
}

type Found = (Map<String, Value>, Map<String, Value>);

impl Auth {
    /// No login (127.0.0.1, the desktop app).
    pub fn off() -> Auth {
        Auth { store: None, enabled: false, limiter: Default::default(), code: Mutex::new(None),
               setup_lock: Default::default() }
    }

    /// The login on `store`; it needs one.
    pub fn new(store: Option<Store>) -> Result<Auth, String> {
        if store.is_none() {
            return Err("The login needs a database: remove XAU_DB_URL=none from .env".into());
        }
        Ok(Auth { store, enabled: true, ..Auth::off() })
    }

    fn store(&self) -> Result<&Store, AuthError> {
        match (&self.store, self.enabled) {
            (Some(s), true) => Ok(s),
            _ => Err(refuse(StatusCode::CONFLICT, NO_LOGIN)),
        }
    }

    /// With no account yet: create it from `XAU_AUTH_PASSWORD(_HASH)`, else log a setup code.
    pub async fn start(&self, env: &dyn Fn(&str) -> Option<String>) -> Result<(), String> {
        let Ok(store) = self.store() else { return Ok(()) };
        if store.users_count().await? > 0 {
            return Ok(());
        }
        let hashed = env("XAU_AUTH_PASSWORD_HASH").map(|h| h.trim().to_string()).filter(|h| !h.is_empty());
        let plain = env("XAU_AUTH_PASSWORD").filter(|p| !p.is_empty());
        if hashed.is_some() || plain.is_some() {
            let hash = match (&hashed, plain) {
                (Some(h), _) => h.clone(),
                (None, Some(p)) => hash_blocking(p).await,
                (None, None) => unreachable!(),
            };
            self.add_user(store, "admin", &hash).await?;
            log::warn!("created the account 'admin' from {}; remove it from .env and log in as admin",
                       if hashed.is_some() { "XAU_AUTH_PASSWORD_HASH" } else { "XAU_AUTH_PASSWORD" });
            return Ok(());
        }
        let code = setup_code();
        log::warn!("no account yet: open the dashboard, choose Create account and enter the setup code {code} \
                    (it works once, for an hour)");
        *self.code.lock().unwrap() = Some((code, Instant::now() + SETUP_LIFE));
        Ok(())
    }

    async fn add_user(&self, store: &Store, username: &str, hash: &str) -> Result<Map<String, Value>, String> {
        let now = now_iso(Utc::now());
        let id = uuid::Uuid::new_v4().to_string();
        store.add_user(vec![("id", s(&id)), ("username", s(username)), ("email", Arg::Str(None)), ("name", Arg::Str(None)),
                            ("password_hash", s(hash)), ("role", s("owner")), ("disabled", Arg::Bool(false)),
                            ("created_at", s(&now)), ("password_at", s(&now)), ("last_login_at", Arg::Str(None))])
            .await?;
        store.user(true, &id).await?.ok_or_else(|| "the new user is missing".to_string())
    }

    /// `--create-user`: add the account, or set a new password on it (true when new). A new
    /// password ends the account's sessions.
    pub async fn create_user(&self, username: &str, password: &str) -> Result<bool, AuthError> {
        let store = self.store()?;
        let username = clean_username(username)?;
        let password = clean_password(Some(&Value::from(password)))?;
        let hash = hash_blocking(password).await;
        match store.user(false, &username).await.map_err(internal)? {
            None => {
                self.add_user(store, &username, &hash).await.map_err(internal)?;
                Ok(true)
            }
            Some(user) => {
                let id = field(&user, "id");
                store.update_user(id, vec![("password_hash", s(&hash)), ("password_at", s(&now_iso(Utc::now()))),
                                           ("disabled", Arg::Bool(false))])
                    .await
                    .map_err(internal)?;
                store.delete_sessions(None, Some(id), None, None).await.map_err(internal)?;
                Ok(false)
            }
        }
    }

    async fn new_session(&self, store: &Store, user: &Map<String, Value>, device: Option<String>)
                         -> Result<String, AuthError> {
        let now = Utc::now();
        store.delete_sessions(None, None, None, Some(&now_iso(now))).await.map_err(internal)?;
        let token = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(random_bytes::<32>());
        let device = device.map(|d| d.chars().take(100).collect::<String>()).filter(|d| !d.is_empty());
        store.add_session(&sha(&token), field(user, "id"), device.as_deref(), &now_iso(now),
                          &now_iso(now + Duration::days(SESSION_DAYS)))
            .await
            .map_err(internal)?;
        store.update_user(field(user, "id"), vec![("last_login_at", s(&now_iso(now)))]).await.map_err(internal)?;
        Ok(token)
    }

    /// The (user, session) a cookie belongs to, renewing it; None when it is no session.
    pub async fn who(&self, token: Option<&str>) -> Result<Option<Found>, AuthError> {
        let (Ok(store), Some(token)) = (self.store(), token.filter(|t| !t.is_empty())) else { return Ok(None) };
        let Some(session) = store.session(&sha(token)).await.map_err(internal)? else { return Ok(None) };
        let now = Utc::now();
        let user = store.user(true, field(&session, "user_id")).await.map_err(internal)?;
        let valid = user.as_ref().is_some_and(|u| {
            field(&session, "expires_at") >= now_iso(now).as_str()
                && u.get("disabled").and_then(Value::as_bool) != Some(true)
                && field(&session, "created_at") >= field(u, "password_at")
        });
        let hash = field(&session, "token_hash").to_string();
        if !valid {
            store.delete_sessions(Some(&hash), None, None, None).await.map_err(internal)?;
            return Ok(None);
        }
        let seen = Some(field(&session, "last_seen_at")).filter(|v| !v.is_empty()).unwrap_or(field(&session, "created_at"));
        if seen < now_iso(now - Duration::seconds(RENEW_SECONDS)).as_str() {
            store.touch_session(&hash, &now_iso(now), &now_iso(now + Duration::days(SESSION_DAYS)))
                .await
                .map_err(internal)?;
        }
        Ok(Some((user.unwrap(), session)))
    }

    pub async fn status(&self, token: Option<&str>) -> Result<Value, AuthError> {
        let found = self.who(token).await?;
        let setup = match self.store() {
            Ok(store) if self.code.lock().unwrap().is_some() => store.users_count().await.map_err(internal)? == 0,
            _ => false,
        };
        Ok(json!({"login": self.enabled, "logged_in": found.is_some(), "setup": setup,
                  "user": found.map(|(u, _)| public_user(&u))}))
    }

    pub async fn login(&self, username: Option<&Value>, password: Option<&Value>, client: &str,
                       device: Option<String>) -> Result<String, AuthError> {
        let store = self.store()?;
        let name = username.and_then(Value::as_str).unwrap_or("").trim().to_lowercase();
        let keys = [format!("ip:{client}"), format!("user:{name}")];
        self.limiter.check(&keys)?;
        let user = if name.is_empty() { None } else { store.user(false, &name).await.map_err(internal)? };
        let given = password.and_then(Value::as_str).unwrap_or("").to_string();
        let ok = match &user {
            Some(u) if u.get("disabled").and_then(Value::as_bool) != Some(true) =>
                check_blocking(given.clone(), field(u, "password_hash").to_string()).await,
            Some(_) => false,
            // The same time whether the name exists or not.
            None => check_blocking(given.clone(), dummy_hash().to_string()).await && false,
        };
        if !ok {
            self.limiter.fail(&keys);
            return Err(refuse(StatusCode::UNAUTHORIZED, "Wrong username or password"));
        }
        self.limiter.clear(&keys);
        let user = user.unwrap();
        if !field(&user, "password_hash").starts_with("$argon2id$") {
            let hash = hash_blocking(given).await;
            store.update_user(field(&user, "id"), vec![("password_hash", s(&hash))]).await.map_err(internal)?;
        }
        self.new_session(store, &user, device).await
    }

    pub async fn setup(&self, body: &Map<String, Value>, client: &str, device: Option<String>)
                       -> Result<String, AuthError> {
        let store = self.store()?;
        let keys = [format!("ip:{client}"), "setup".to_string()];
        self.limiter.check(&keys)?;
        let _one = self.setup_lock.lock().await;
        if store.users_count().await.map_err(internal)? > 0 {
            return Err(refuse(StatusCode::CONFLICT, "The account exists already: log in"));
        }
        let code = match &*self.code.lock().unwrap() {
            Some((code, until)) if Instant::now() <= *until => code.clone(),
            _ => return Err(refuse(StatusCode::GONE,
                                   "The setup code expired: restart Wednesday for a new one in its log")),
        };
        if !same_code(body.get("code").and_then(Value::as_str).unwrap_or(""), &code) {
            self.limiter.fail(&keys);
            return Err(refuse(StatusCode::UNAUTHORIZED, "Wrong setup code: copy it from the server's log"));
        }
        let username = clean_username(body.get("username").and_then(Value::as_str).unwrap_or(""))?;
        let password = clean_password(body.get("password"))?;
        let user = self.add_user(store, &username, &hash_blocking(password).await).await.map_err(internal)?;
        *self.code.lock().unwrap() = None;
        log::info!("created the account {username}");
        self.new_session(store, &user, device).await
    }

    pub async fn logout(&self, token: Option<&str>) -> Result<(), AuthError> {
        if let (Ok(store), Some(token)) = (self.store(), token) {
            store.delete_sessions(Some(&sha(token)), None, None, None).await.map_err(internal)?;
        }
        Ok(())
    }

    /// The devices logged in to this account, this one marked `current`.
    pub async fn sessions(&self, token: Option<&str>) -> Result<Vec<Value>, AuthError> {
        let Some((user, current)) = self.who(token).await? else { return Ok(Vec::new()) };
        let rows = self.store()?.sessions(field(&user, "id")).await.map_err(internal)?;
        Ok(rows
            .iter()
            .map(|r| {
                let hash = field(r, "token_hash");
                json!({"id": &hash[..hash.len().min(16)], "device": r.get("device"), "created_at": r.get("created_at"),
                       "last_seen_at": r.get("last_seen_at"), "current": hash == field(&current, "token_hash")})
            })
            .collect())
    }

    pub async fn end_session(&self, token: Option<&str>, id: &str) -> Result<(), AuthError> {
        let store = self.store()?;
        let Some((user, _)) = self.who(token).await? else {
            return Err(refuse(StatusCode::UNAUTHORIZED, "Log in first"));
        };
        let rows = store.sessions(field(&user, "id")).await.map_err(internal)?;
        let hits: Vec<&str> = rows.iter().map(|r| field(r, "token_hash")).filter(|h| !id.is_empty() && h.starts_with(id)).collect();
        let [hash] = hits[..] else { return Err(refuse(StatusCode::NOT_FOUND, "No such device: reload the list")) };
        store.delete_sessions(Some(hash), None, None, None).await.map_err(internal)?;
        Ok(())
    }

    /// A new password; every other device is logged out.
    pub async fn change_password(&self, token: Option<&str>, body: &Map<String, Value>) -> Result<(), AuthError> {
        let store = self.store()?;
        let Some((user, session)) = self.who(token).await? else {
            return Err(refuse(StatusCode::UNAUTHORIZED, "Log in first"));
        };
        let keys = [format!("user:{}", field(&user, "username"))];
        self.limiter.check(&keys)?;
        let current = body.get("current").and_then(Value::as_str).unwrap_or("").to_string();
        if !check_blocking(current, field(&user, "password_hash").to_string()).await {
            self.limiter.fail(&keys);
            return Err(refuse(StatusCode::FORBIDDEN, "The current password is wrong"));
        }
        let new = clean_password(body.get("new"))?;
        // This session still counts: it was made at password_at, not before.
        store.update_user(field(&user, "id"), vec![("password_hash", s(&hash_blocking(new).await)),
                                                   ("password_at", s(field(&session, "created_at")))])
            .await
            .map_err(internal)?;
        store.delete_sessions(None, Some(field(&user, "id")), Some(field(&session, "token_hash")), None)
            .await
            .map_err(internal)?;
        Ok(())
    }
}

fn dummy_hash() -> &'static str {
    static HASH: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    HASH.get_or_init(|| hash_password(&base64::engine::general_purpose::STANDARD.encode(random_bytes::<16>())))
}

// ---- HTTP ----

/// The session cookie a request carries.
pub fn cookie(headers: &HeaderMap) -> Option<String> {
    headers.get_all(header::COOKIE).iter().filter_map(|v| v.to_str().ok()).flat_map(|v| v.split(';')).find_map(|c| {
        let (k, v) = c.trim().split_once('=')?;
        (k == SESSION_COOKIE).then(|| v.to_string())
    })
}

fn is_https(headers: &HeaderMap) -> bool {
    headers.get("x-forwarded-proto").and_then(|v| v.to_str().ok()).and_then(|v| v.split(',').next())
        .is_some_and(|v| v.trim() == "https")
}

/// The client's address; behind a proxy on this machine, the address the proxy saw.
fn client_address(req: &Request) -> String {
    let peer = req.extensions().get::<ConnectInfo<SocketAddr>>().map(|c| c.0.ip());
    if peer.is_none_or(|ip| ip.is_loopback()) {
        if let Some(fwd) = req.headers().get("x-forwarded-for").and_then(|v| v.to_str().ok()) {
            if let Some(last) = fwd.rsplit(',').next().map(str::trim).filter(|v| !v.is_empty()) {
                return last.to_string();
            }
        }
    }
    peer.map_or_else(|| "unknown".into(), |ip| ip.to_string())
}

fn with_cookie(mut res: Response, value: String) -> Response {
    if let Ok(v) = HeaderValue::from_str(&value) {
        res.headers_mut().append(header::SET_COOKIE, v);
    }
    res
}

fn session_cookie(token: &str, https: bool) -> String {
    format!("{SESSION_COOKIE}={token}; HttpOnly; Max-Age={}; Path=/; SameSite=strict{}",
            SESSION_DAYS * 86400, if https { "; Secure" } else { "" })
}

fn device_of(user_agent: Option<&str>, body: &Map<String, Value>) -> Option<String> {
    match body.get("device").and_then(Value::as_str).map(str::trim).filter(|d| !d.is_empty()) {
        Some(d) => Some(d.chars().take(100).collect()),
        None => device_name(user_agent),
    }
}

/// Whether a path needs a session when the login is on. The plugins' calls back
/// (`/api/plugin-host/`) carry their own token.
pub fn guarded(path: &str) -> bool {
    path.starts_with("/api") && !PUBLIC.contains(&path) && !path.starts_with("/api/plugin-host/")
}

/// The middleware: a 401 for a guarded route without a session, when the login is on.
pub(crate) async fn need_session(State(app): State<App>, req: Request, next: Next) -> Response {
    if app.auth.enabled && guarded(req.uri().path()) {
        match app.auth.who(cookie(req.headers()).as_deref()).await {
            Ok(Some(_)) => {}
            Ok(None) => return detail(StatusCode::UNAUTHORIZED, "Log in first"),
            Err(e) => return e.into_response(),
        }
    }
    next.run(req).await
}

fn answer(result: Result<Value, AuthError>) -> Response {
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => e.into_response(),
    }
}

pub(crate) async fn status(State(app): State<App>, headers: HeaderMap) -> Response {
    answer(app.auth.status(cookie(&headers).as_deref()).await)
}

/// `POST /api/auth/login` and `/api/auth/setup`: a session cookie and the status.
async fn start_session(app: App, req: Request, setup: bool) -> Response {
    if let Some(refused) = app.guard(req.method(), req.headers()) {
        return refused;
    }
    let (client, https) = (client_address(&req), is_https(req.headers()));
    let user_agent = req.headers().get(header::USER_AGENT).and_then(|v| v.to_str().ok()).map(String::from);
    let body = match object_body(req).await {
        Ok(b) => b,
        Err(res) => return res,
    };
    let device = device_of(user_agent.as_deref(), &body);
    let token = if setup {
        app.auth.setup(&body, &client, device).await
    } else {
        app.auth.login(body.get("username"), body.get("password"), &client, device).await
    };
    match token {
        Ok(token) => with_cookie(answer(app.auth.status(Some(&token)).await), session_cookie(&token, https)),
        Err(e) => e.into_response(),
    }
}

pub(crate) async fn login(State(app): State<App>, req: Request) -> Response {
    start_session(app, req, false).await
}

pub(crate) async fn setup(State(app): State<App>, req: Request) -> Response {
    start_session(app, req, true).await
}

pub(crate) async fn logout(State(app): State<App>, req: Request) -> Response {
    if let Some(refused) = app.guard(req.method(), req.headers()) {
        return refused;
    }
    if let Err(e) = app.auth.logout(cookie(req.headers()).as_deref()).await {
        return e.into_response();
    }
    let gone = format!("{SESSION_COOKIE}=\"\"; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; Path=/; SameSite=lax");
    with_cookie(answer(app.auth.status(None).await), gone)
}

pub(crate) async fn sessions(State(app): State<App>, headers: HeaderMap) -> Response {
    answer(app.auth.sessions(cookie(&headers).as_deref()).await.map(|s| json!({"sessions": s})))
}

pub(crate) async fn end_session(State(app): State<App>, Path(id): Path<String>, req: Request) -> Response {
    if let Some(refused) = app.guard(req.method(), req.headers()) {
        return refused;
    }
    let token = cookie(req.headers());
    if let Err(e) = app.auth.end_session(token.as_deref(), &id).await {
        return e.into_response();
    }
    answer(app.auth.sessions(token.as_deref()).await.map(|s| json!({"sessions": s})))
}

pub(crate) async fn password(State(app): State<App>, req: Request) -> Response {
    if let Some(refused) = app.guard(req.method(), req.headers()) {
        return refused;
    }
    let token = cookie(req.headers());
    let body = match object_body(req).await {
        Ok(b) => b,
        Err(res) => return res,
    };
    if let Err(e) = app.auth.change_password(token.as_deref(), &body).await {
        return e.into_response();
    }
    answer(app.auth.sessions(token.as_deref()).await.map(|s| json!({"sessions": s})))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passwords() {
        let h = hash_password("correct horse battery");
        assert!(h.starts_with("$argon2id$v=19$m=19456,t=2,p=1$"), "{h}");
        assert!(check_password("correct horse battery", &h));
        assert!(!check_password("wrong", &h));
        assert!(!check_password("x", "garbage"));
        // hashlib.scrypt(b"correct horse battery", salt=b"saltsaltsaltsalt", n=1024, r=8, p=1, dklen=32)
        let b64 = base64::engine::general_purpose::STANDARD;
        let mut want = [0u8; 32];
        scrypt::scrypt(b"correct horse battery", b"saltsaltsaltsalt", &scrypt::Params::new(10, 8, 1, 32).unwrap(), &mut want).unwrap();
        let old = format!("scrypt$1024$8$1${}${}", b64.encode(b"saltsaltsaltsalt"), b64.encode(want));
        assert!(check_password("correct horse battery", &old));
        assert!(!check_password("wrong", &old));
    }

    #[test]
    fn names_codes_devices() {
        assert_eq!(clean_username(" Trader ").unwrap(), "trader");
        assert!(clean_username("x").is_err() && clean_username("a b").is_err() && clean_username("-ab").is_err());
        assert!(same_code("abcd efgh-jklm", "ABCD-EFGH-JKLM"));
        assert!(!same_code("ABCD-EFGH-JKLN", "ABCD-EFGH-JKLM"));
        assert_eq!(setup_code().len(), 14);
        assert_eq!(device_name(Some("Mozilla/5.0 (Windows NT 10.0) Chrome/130 Safari/537 Edg/130")).as_deref(),
                   Some("Edge on Windows"));
        assert_eq!(device_name(None), None);
    }

    #[test]
    fn cookies() {
        let mut h = HeaderMap::new();
        h.insert(header::COOKIE, HeaderValue::from_static("a=1; wednesday_session=tok; b=2"));
        assert_eq!(cookie(&h).as_deref(), Some("tok"));
        assert!(guarded("/api/status") && !guarded("/api/health") && !guarded("/") && !guarded("/api/plugin-host/settings/x"));
    }
}
