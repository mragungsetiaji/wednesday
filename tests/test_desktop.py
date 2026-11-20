import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wednesday import desktop

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


@pytest.fixture
def home(tmp_path, monkeypatch):
    # Keys from the copied .env land in os.environ; setenv first so monkeypatch removes them afterwards.
    for line in EXAMPLE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key = line.split("=", 1)[0].strip()
            monkeypatch.setenv(key, "")
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # run_sidecar() changes directory; restore it
    path = tmp_path / "home"
    monkeypatch.setenv("WEDNESDAY_HOME", str(path))
    root = logging.getLogger()
    handlers = list(root.handlers)
    yield path
    for h in root.handlers[:]:
        if h not in handlers:
            root.removeHandler(h)
            h.close()


def test_single_instance(tmp_path):
    first = desktop._single_instance(tmp_path)
    assert first is not None
    assert desktop._single_instance(tmp_path) is None
    first.close()
    again = desktop._single_instance(tmp_path)
    assert again is not None
    again.close()


def test_sidecar_serves_from_the_home_folder(home, monkeypatch):
    monkeypatch.setenv("XAU_LOG_FILE", "")  # so monkeypatch removes the one run_sidecar sets
    monkeypatch.delenv("XAU_LOG_FILE")
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        seen["cwd"] = Path.cwd()
        # While it runs, a second copy is refused: two would scan twice and alert twice.
        with pytest.raises(SystemExit, match="already running"):
            desktop.run_sidecar(["--port", "8123"])

    monkeypatch.setattr(desktop, "main", fake_main)
    desktop.run_sidecar(["--port", "8123"])

    assert seen["cwd"] == home
    assert seen["argv"] == ["--serve", "--env-file", str(home / ".env"), "--host", "127.0.0.1", "--port", "8123"]
    assert os.environ["XAU_LOG_FILE"] == "logs/screener.log"
    assert not (home / ".env").exists()  # set up from the dashboard, not a .env


def test_the_sidecar_notices_when_a_process_is_gone():
    # run_sidecar stops once the Tauri shell that started it is gone (--parent-pid).
    assert desktop._process_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not desktop._process_alive(child.pid)
