// No console window behind the app in release builds on Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // The installer's fresh install runs `Wednesday.exe --forget-secrets`: no window, no server.
    let args: Vec<String> = std::env::args().collect();
    if wednesday_desktop_lib::forget_secrets(&args) {
        return;
    }
    wednesday_desktop_lib::run()
}
