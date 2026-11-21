//! Chart snapshots kept for the journal (`snapshots.py`), #96 step 9f: PNG files under
//! `<home>/data/snapshots`, uploaded with `POST /api/snapshots` (raw PNG body) and served by
//! `GET /api/snapshots/{id}.png`.
//!
//! The dashboard makes the image; the server only checks it is a PNG of reasonable size and stores
//! it under a random id. Files never leave the snapshot folder: an id is 32 lowercase hex
//! characters, nothing else is accepted.

use std::path::{Path, PathBuf};

use serde_json::json;

use wednesday_base::{Answer, Refusal};

pub const MAX_BYTES: usize = 12 * 1024 * 1024;
pub const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";
/// The `Cache-Control` of a served snapshot: a file never changes under its id.
pub const CACHE_CONTROL: &str = "private, max-age=31536000, immutable";

/// The snapshot folder of the home folder Python runs in (`data/snapshots`, relative to it).
pub fn dir(home: &Path) -> PathBuf {
    home.join("data").join("snapshots")
}

/// Whether `sid` is a snapshot id (`^[0-9a-f]{32}$`).
pub fn valid(sid: &str) -> bool {
    sid.len() == 32 && sid.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The file of snapshot `sid` in `dir`, or None (unknown, or not an id at all).
pub fn path(dir: &Path, sid: &str) -> Option<PathBuf> {
    if !valid(sid) {
        return None;
    }
    let p = dir.join(format!("{sid}.png"));
    p.is_file().then_some(p)
}

/// Delete snapshot `sid`, if it is one.
pub fn delete(dir: &Path, sid: &str) {
    if let Some(p) = path(dir, sid) {
        let _ = std::fs::remove_file(p);
    }
}

/// `uuid.uuid4().hex`: 128 random bits with the version and variant bits set.
fn new_id() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let mut bytes = [0u8; 16];
    for half in bytes.chunks_mut(8) {
        // RandomState is seeded from the OS; the time and a counter keep every draw distinct.
        let mut h = RandomState::new().build_hasher();
        h.write_u128(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos());
        h.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
        half.copy_from_slice(&h.finish().to_le_bytes());
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `POST /api/snapshots`: check and store the PNG in `dir`; answers `{id, url}`. The caller reads
/// at most [`MAX_BYTES`] + 1 bytes of the body, so a larger one still gets this refusal.
pub fn save(dir: &Path, data: &[u8]) -> Answer {
    if data.len() > MAX_BYTES {
        return Err(Refusal::new(422, "The image is larger than 12 MB"));
    }
    if !data.starts_with(PNG_SIGNATURE) {
        return Err(Refusal::new(422, "A snapshot is a PNG image"));
    }
    let failed = |e: std::io::Error| {
        log::error!("saving a snapshot in {}: {e}", dir.display());
        Refusal::new(500, "Internal Server Error")
    };
    std::fs::create_dir_all(dir).map_err(failed)?;
    let sid = new_id();
    std::fs::write(dir.join(format!("{sid}.png")), data).map_err(failed)?;
    Ok(json!({"id": sid, "url": format!("/api/snapshots/{sid}.png")}))
}

/// The id in a `GET /api/snapshots/{segment}` path segment (`<id>.png`), as FastAPI's
/// `{snapshot_id}.png` route takes it; None when the segment doesn't end in `.png` (Python: 404).
pub fn id_of(segment: &str) -> Option<&str> {
    segment.strip_suffix(".png")
}

/// `GET /api/snapshots/{id}.png`: the PNG's bytes, served as `image/png` with [`CACHE_CONTROL`];
/// 404 "No such snapshot" for an unknown id or anything that isn't one.
pub fn read(dir: &Path, sid: &str) -> Result<Vec<u8>, Refusal> {
    let missing = || Refusal::new(404, "No such snapshot");
    let p = path(dir, sid).ok_or_else(missing)?;
    std::fs::read(p).map_err(|_| missing())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp() -> PathBuf {
        let d = std::env::temp_dir().join(format!("wednesday-snapshots-{}", new_id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn png() -> Vec<u8> {
        [PNG_SIGNATURE, b"rest of the image"].concat()
    }

    #[test]
    fn a_png_is_kept_under_a_new_id() {
        let home = temp();
        let d = dir(&home);
        let out = save(&d, &png()).unwrap();
        let sid = out["id"].as_str().unwrap().to_string();
        assert!(valid(&sid) && &sid[12..13] == "4");
        assert_eq!(out["url"], format!("/api/snapshots/{sid}.png"));
        assert_eq!(read(&d, &sid).unwrap(), png());
        assert_eq!(id_of(&format!("{sid}.png")), Some(sid.as_str()));
        let other = save(&d, &png()).unwrap();
        assert_ne!(other["id"], out["id"]);
        delete(&d, &sid);
        assert_eq!(read(&d, &sid).unwrap_err().status, 404);
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn only_pngs_of_reasonable_size() {
        let home = temp();
        let d = dir(&home);
        let e = save(&d, b"GIF89a....").unwrap_err();
        assert_eq!((e.status, e.detail), (422, json!("A snapshot is a PNG image")));
        let mut big = png();
        big.resize(MAX_BYTES + 1, 0);
        let e = save(&d, &big).unwrap_err();
        assert_eq!((e.status, e.detail), (422, json!("The image is larger than 12 MB")));
        assert!(!d.exists());
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn nothing_outside_the_folder() {
        let home = temp();
        let d = dir(&home);
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(home.join("data").join("secret.png"), png()).unwrap();
        let upper = "A".repeat(32);
        std::fs::write(d.join(format!("{upper}.png")), png()).unwrap();
        for sid in ["../secret", "..%2fsecret", "", "x", &upper, &format!("{}/../../secret", "0".repeat(32)),
                    &format!("{}.png", "0".repeat(28))] {
            assert!(path(&d, sid).is_none(), "{sid}");
            assert_eq!(read(&d, sid).unwrap_err().detail, json!("No such snapshot"));
        }
        assert!(id_of("abc").is_none());
        std::fs::remove_dir_all(home).unwrap();
    }
}
