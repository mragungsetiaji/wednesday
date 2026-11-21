//! The log: info and above, to stderr and, when given, a file (the desktop app's
//! `<home>/logs/screener.log`, as the Python server wrote it). The file starts over once it
//! passes 5 MB, at the next start.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

const MAX_BYTES: u64 = 5 * 1024 * 1024;

struct Logger {
    file: Option<Mutex<File>>,
}

impl log::Log for Logger {
    fn enabled(&self, meta: &log::Metadata) -> bool {
        meta.level() <= log::Level::Info
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!(
            "{} {} {}: {}\n",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S"),
            record.level(),
            record.target(),
            record.args()
        );
        eprint!("{line}");
        if let Some(f) = &self.file {
            if let Ok(mut f) = f.lock() {
                let _ = f.write_all(line.as_bytes());
            }
        }
    }

    fn flush(&self) {}
}

/// Start logging; a second call does nothing. A file that can't be opened leaves stderr only.
pub fn init(file: Option<&Path>) {
    let file = file.and_then(|path| {
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let too_big = std::fs::metadata(path).is_ok_and(|m| m.len() > MAX_BYTES);
        OpenOptions::new().create(true).append(!too_big).write(true).truncate(too_big).open(path).ok()
    });
    let logger: &'static Logger = Box::leak(Box::new(Logger { file: file.map(Mutex::new) }));
    if log::set_logger(logger).is_ok() {
        log::set_max_level(log::LevelFilter::Info);
    }
}
