"""PyInstaller entry point for the Tauri shell's server (see wednesday.spec with WEDNESDAY_BUILD=server)."""

from wednesday.desktop import run_sidecar

run_sidecar()
