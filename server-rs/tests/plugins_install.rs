//! Downloading a plugin (plugins/install.rs): only a genuine package for this API and target is
//! unpacked, and never outside its folder.

mod plugin_kit;

use plugin_kit::*;
use serde_json::{json, Value};
use wednesday_rs::plugins::install::{self, InstallError, Manifest};

#[test]
fn trusted_keys_match_python() {
    let py = std::fs::read_to_string(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../src/wednesday/plugin_keys.py")).unwrap();
    let block = py.split("TRUSTED").nth(1).unwrap().split('{').nth(1).unwrap().split('}').next().unwrap();
    let mut python: Vec<(String, String)> = block
        .lines()
        .filter_map(|l| {
            let parts: Vec<&str> = l.split('"').collect();
            (parts.len() >= 5).then(|| (parts[1].to_string(), parts[3].to_string()))
        })
        .collect();
    python.sort();
    let mut rust: Vec<(String, String)> =
        wednesday_rs::plugins::keys::TRUSTED.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    rust.sort();
    assert!(!rust.is_empty());
    assert_eq!(rust, python, "plugins/keys.rs and plugin_keys.py trust different keys");
    assert_eq!(install::trusted().values().next().unwrap().len(), 32);
}

fn refused(answer: &Value, signer: &Signer) -> String {
    match install::verify(answer, &signer.trust()) {
        Err(InstallError(msg)) => msg,
        Ok(_) => panic!("accepted"),
    }
}

#[test]
fn only_a_genuine_download_checks_out() {
    let signer = Signer::new("test");
    let zip = zip_of(vec![Entry::File("plugin.json", b"{}".to_vec())]);
    let (manifest, got) = install::verify(&signer.answer(&zip, json!({})), &signer.trust()).unwrap();
    assert_eq!((manifest.name.as_str(), manifest.version.as_str(), got), ("wednesday_ee", "0.2.0", zip.clone()));

    let other = Signer::new("test");
    assert!(refused(&other.answer(&zip, json!({})), &signer).contains("isn't genuine"));
    let mut tampered = signer.answer(&zip, json!({}));
    tampered["package"] = json!(base64::Engine::encode(&base64::engine::general_purpose::STANDARD, b"other"));
    assert_eq!(refused(&tampered, &signer), "The download doesn't match its signature; try again");
    assert!(refused(&signer.answer(&zip, json!({"kid": "other"})), &signer).contains("doesn't know"));
    assert_eq!(refused(&signer.answer(&zip, json!({"name": "evil"})), &signer), "Wednesday doesn't install 'evil'");
    assert!(refused(&signer.answer(&zip, json!({"api": 1})), &signer).contains("another version of Wednesday"));
    assert!(refused(&signer.answer(&zip, json!({"target": "sparc-sun-solaris"})), &signer).contains("sparc-sun-solaris"));
    assert_eq!(refused(&json!({"manifest": "x"}), &signer), "The download is damaged; try again");
    let mut big = signer.answer(&zip, json!({}));
    big["package"] = json!("A".repeat(install::MAX_BYTES / 3 * 4 + 16));
    assert_eq!(refused(&big, &signer), "The download is larger than 50 MB");
}

fn manifest() -> Manifest {
    Manifest { name: "wednesday_ee".into(), version: "0.2.0".into(), raw: json!({"name": "wednesday_ee", "version": "0.2.0"}) }
}

fn plugin_json() -> Entry<'static> {
    Entry::File("plugin.json", json!({"name": "wednesday_ee", "version": "0.2.0", "api": 2, "exe": EXE}).to_string().into_bytes())
}

#[test]
fn a_package_unpacks_into_its_folder_only() {
    let root = temp("unpack");
    let good = zip_of(vec![plugin_json(), Entry::File(EXE, b"#!/bin/sh\n".to_vec()), Entry::File("lib/x.txt", b"x".to_vec())]);
    let dest = install::unpack(&manifest(), &good, &root).unwrap();
    assert_eq!(dest, root.join("wednesday_ee"));
    assert!(dest.join("lib/x.txt").is_file() && dest.join(install::MANIFEST_FILE).is_file());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(dest.join(EXE)).unwrap().permissions().mode() & 0o777, 0o755);
    }
    std::fs::write(dest.join("stale"), "old").unwrap();
    install::unpack(&manifest(), &good, &root).unwrap(); // again: replaces the old copy whole
    assert!(!dest.join("stale").exists() && !root.join("wednesday_ee.new").exists());

    for (bad, what) in [
        (zip_of(vec![plugin_json(), Entry::File(EXE, vec![]), Entry::File("../escaped", b"x".to_vec())]), "outside"),
        (zip_of(vec![plugin_json(), Entry::File(EXE, vec![]), Entry::File("/abs", b"x".to_vec())]), "outside"),
        (zip_of(vec![plugin_json(), Entry::File(EXE, vec![]), Entry::Symlink("link", "/etc/passwd")]), "outside"),
        (zip_of(vec![Entry::File(EXE, vec![])]), "damaged"),
        (b"not a zip".to_vec(), "damaged"),
    ] {
        let err = install::unpack(&manifest(), &bad, &root).unwrap_err();
        assert!(err.0.contains(what), "{what}: {err}");
        assert!(!root.join("escaped").exists() && !root.join("wednesday_ee.new").exists());
        assert!(dest.join(EXE).is_file(), "a refused download leaves the installed copy alone");
    }
    let mut evil = manifest();
    evil.name = "../x".into();
    assert_eq!(install::unpack(&evil, &good, &root).unwrap_err().0, "Bad package name");
}

#[tokio::test]
async fn fetch_reports_what_the_licence_server_says() {
    use axum::routing::post;
    let app = axum::Router::new().route("/v1/download", post(|body: String| async move {
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["target"], install::TARGET);
        assert_eq!(v["device_id"], "dev1");
        if v["key"] == "WEDK-OK" {
            (axum::http::StatusCode::OK, json!({"ok": true}).to_string())
        } else {
            (axum::http::StatusCode::FORBIDDEN, json!({"detail": "This licence was revoked"}).to_string())
        }
    }));
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    assert_eq!(install::fetch("WEDK-OK", "dev1", &url).await.unwrap(), json!({"ok": true}));
    assert_eq!(install::fetch("WEDK-NO", "dev1", &url).await.unwrap_err().0, "This licence was revoked");
    let err = install::fetch("WEDK-OK", "dev1", "http://127.0.0.1:9").await.unwrap_err();
    assert_eq!(err.0, "Can't reach the licence server at http://127.0.0.1:9 to download Wednesday EE");
}

#[tokio::test]
async fn the_device_id_is_kept() {
    let home = temp("device");
    let store = wednesday_rs::store::Store::open("sqlite:///data/xau.db", &home).await.unwrap();
    let first = install::device_id(Some(&store)).await;
    assert_eq!(first.len(), 32);
    assert_eq!(install::device_id(Some(&store)).await, first);
    assert_eq!(store.get_setting(install::DEVICE_SETTING).await.unwrap().unwrap(), json!({"id": first}));
}
