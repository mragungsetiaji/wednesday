//! Where the data is, found as the Python server finds it (`desktop.home_dir`, `cli.load_env_file`):
//! the home folder, its `.env`, and the database URL in `XAU_DB_URL`.

use std::path::{Path, PathBuf};

pub const DEFAULT_DB_URL: &str = "sqlite:///data/xau.db";

/// `WEDNESDAY_HOME`, else `%LOCALAPPDATA%\Wednesday` on Windows, else `~/.wednesday`.
pub fn home_dir() -> PathBuf {
    if let Some(custom) = std::env::var_os("WEDNESDAY_HOME").filter(|v| !v.is_empty()) {
        return PathBuf::from(custom);
    }
    if cfg!(windows) {
        if let Some(local) = std::env::var_os("LOCALAPPDATA").filter(|v| !v.is_empty()) {
            return PathBuf::from(local).join("Wednesday");
        }
    }
    let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).unwrap_or_default();
    PathBuf::from(home).join(".wednesday")
}

/// A setting from the environment, else from the `.env` file (UTF-8 with or without a BOM, or
/// UTF-16), as `load_env_file` reads it: a variable already set wins, quotes are stripped.
pub fn env_setting(name: &str, env_file: &Path) -> Option<String> {
    if let Ok(v) = std::env::var(name) {
        if !v.is_empty() {
            return Some(v);
        }
    }
    let raw = std::fs::read(env_file).ok()?;
    let text = if raw.starts_with(&[0xff, 0xfe]) || raw.starts_with(&[0xfe, 0xff]) {
        let le = raw.starts_with(&[0xff, 0xfe]);
        let units: Vec<u16> = raw[2..]
            .chunks_exact(2)
            .map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) })
            .collect();
        String::from_utf16_lossy(&units)
    } else {
        let s = String::from_utf8_lossy(&raw).into_owned();
        s.strip_prefix('\u{feff}').map(String::from).unwrap_or(s)
    };
    let mut found = None;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else { continue };
        // The first line sets it; later ones don't override (the variable is set by then).
        if k.trim() == name && found.is_none() {
            let v = v.trim().trim_matches(|c| c == '\'' || c == '"').to_string();
            if !v.is_empty() {
                found = Some(v);
            }
        }
    }
    found
}

/// The database URL the Python server in `home` uses; None with `XAU_DB_URL=none` (no storage).
pub fn database_url(home: &Path) -> Option<String> {
    let url = env_setting("XAU_DB_URL", &home.join(".env")).unwrap_or_else(|| DEFAULT_DB_URL.into());
    (url.to_lowercase() != "none").then_some(url)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_env_file_as_python_reads_it() {
        let dir = std::env::temp_dir().join(format!("wed-env-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join(".env");
        std::fs::write(&f, "\u{feff}# comment\nXAU_TEST_A = 'quoted'\nXAU_TEST_A=second\nXAU_TEST_B=\n").unwrap();
        assert_eq!(env_setting("XAU_TEST_A", &f).as_deref(), Some("quoted"));
        assert_eq!(env_setting("XAU_TEST_B", &f), None);
        let utf16: Vec<u8> = [0xff, 0xfe].into_iter()
            .chain("XAU_TEST_C=x\r\n".encode_utf16().flat_map(|u| u.to_le_bytes())).collect();
        std::fs::write(&f, utf16).unwrap();
        assert_eq!(env_setting("XAU_TEST_C", &f).as_deref(), Some("x"));
        assert_eq!(database_url(&dir).as_deref(), Some(DEFAULT_DB_URL));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
