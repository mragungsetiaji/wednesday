//! What the plugin tests share: the fake plugin (`examples/fake_plugin.rs`), signed packages made
//! with a key pair made here, and small HTTP helpers.

#![allow(dead_code)]

use std::io::Write;
use std::path::{Path, PathBuf};

use base64::Engine as _;
use serde_json::{json, Value};
use wednesday_rs::plugins::install::{self, Trust};

/// The fake plugin's program, which `cargo test` builds with the examples.
pub fn fake_exe() -> PathBuf {
    let deps = std::env::current_exe().unwrap().parent().unwrap().to_path_buf();
    let exe = deps.parent().unwrap().join("examples").join(format!("fake_plugin{}", std::env::consts::EXE_SUFFIX));
    assert!(exe.is_file(), "{} is missing: run `cargo test` (it builds examples/fake_plugin.rs)", exe.display());
    exe
}

pub const EXE: &str = if cfg!(windows) { "wednesday-ee.exe" } else { "wednesday-ee" };

/// A temporary folder of its own.
pub fn temp(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("wed-plugins-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Put the fake plugin in `<plugins>/<name>/`, with `fake.json` as given.
pub fn put_fake(plugins: &Path, name: &str, fake: Value) -> PathBuf {
    let dir = plugins.join(name);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::copy(fake_exe(), dir.join(EXE)).unwrap();
    std::fs::write(dir.join("plugin.json"), json!({"name": name, "version": "0.2.0", "api": 2, "exe": EXE}).to_string()).unwrap();
    std::fs::write(dir.join("fake.json"), fake.to_string()).unwrap();
    dir
}

pub enum Entry<'a> {
    File(&'a str, Vec<u8>),
    Symlink(&'a str, &'a str),
}

pub fn zip_of(entries: Vec<Entry>) -> Vec<u8> {
    let mut w = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
    let opts = zip::write::SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    for e in entries {
        match e {
            Entry::File(name, data) => {
                w.start_file(name, opts).unwrap();
                w.write_all(&data).unwrap();
            }
            Entry::Symlink(name, target) => w.add_symlink(name, target, opts).unwrap(),
        }
    }
    w.finish().unwrap().into_inner()
}

/// The fake plugin packaged as Wednesday EE.
pub fn plugin_zip() -> Vec<u8> {
    zip_of(vec![
        Entry::File("plugin.json", json!({"name": "wednesday_ee", "version": "0.2.0", "api": 2, "exe": EXE}).to_string().into_bytes()),
        Entry::File(EXE, std::fs::read(fake_exe()).unwrap()),
    ])
}

/// Signs packages as the licence server does.
pub struct Signer {
    pub kid: String,
    pair: ring::signature::Ed25519KeyPair,
}

impl Signer {
    pub fn new(kid: &str) -> Signer {
        let rng = ring::rand::SystemRandom::new();
        let pkcs8 = ring::signature::Ed25519KeyPair::generate_pkcs8(&rng).unwrap();
        Signer { kid: kid.into(), pair: ring::signature::Ed25519KeyPair::from_pkcs8(pkcs8.as_ref()).unwrap() }
    }

    pub fn public(&self) -> Vec<u8> {
        use ring::signature::KeyPair;
        self.pair.public_key().as_ref().to_vec()
    }

    /// Wednesday trusting this key for Wednesday EE on this build's target.
    pub fn trust(&self) -> Trust {
        Trust {
            keys: [(self.kid.clone(), self.public())].into_iter().collect(),
            allowed: vec!["wednesday_ee".into()],
            target: install::TARGET.into(),
        }
    }

    /// The licence server's answer for `zip`, its manifest changed by `overrides`.
    pub fn answer(&self, zip: &[u8], overrides: Value) -> Value {
        let sha: String = install::hex(ring::digest::digest(&ring::digest::SHA256, zip).as_ref());
        let mut manifest = json!({"kid": self.kid, "name": "wednesday_ee", "version": "0.2.0", "api": 2,
                                  "target": install::TARGET, "file": "wednesday_ee-0.2.0.zip", "sha256": sha});
        for (k, v) in overrides.as_object().unwrap() {
            manifest[k] = v.clone();
        }
        let b64url = |b: &[u8]| base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(b);
        let body = b64url(manifest.to_string().as_bytes());
        let signature = self.pair.sign(format!("WEDPKG1.{body}").as_bytes());
        json!({"kid": self.kid, "manifest": body, "signature": b64url(signature.as_ref()),
               "package": base64::engine::general_purpose::STANDARD.encode(zip)})
    }
}

/// `(status, JSON body)` of a request; headers as given.
pub async fn send(method: &str, url: impl Into<String>, body: Option<Value>, headers: &[(&str, &str)]) -> (u16, Value) {
    let client = reqwest::Client::builder().no_proxy().build().unwrap();
    let mut req = client.request(method.parse().unwrap(), url.into());
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    if let Some(b) = body {
        req = req.header("content-type", "application/json").body(b.to_string());
    }
    let res = req.send().await.unwrap();
    let status = res.status().as_u16();
    let text = res.text().await.unwrap_or_default();
    (status, serde_json::from_str(&text).unwrap_or(Value::String(text)))
}
