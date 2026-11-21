//! The target triple this server is built for: a downloaded plugin must be built for the same one
//! (`plugins::install::TARGET`).

fn main() {
    let target = std::env::var("TARGET").unwrap_or_default();
    println!("cargo:rustc-env=WEDNESDAY_TARGET={target}");
    println!("cargo:rerun-if-changed=build.rs");
}
