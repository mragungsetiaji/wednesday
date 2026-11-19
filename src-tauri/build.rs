// The dashboard is served by the local server (http://127.0.0.1:<port>), a remote origin to
// Tauri, so its commands must be declared here and granted in capabilities/default.json.
const COMMANDS: &[&str] = &["open_chart", "window_layout", "arrange", "save_png", "selftest_result"];

fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)))
        .expect("tauri-build failed");
}
