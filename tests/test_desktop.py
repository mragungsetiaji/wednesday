import json
import logging
import socket
import sys
import types
import urllib.request
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
    monkeypatch.chdir(tmp_path)  # run() changes directory; restore it
    path = tmp_path / "home"
    monkeypatch.setenv("WEDNESDAY_HOME", str(path))
    root = logging.getLogger()
    handlers = list(root.handlers)
    yield path
    for h in root.handlers[:]:
        if h not in handlers:
            root.removeHandler(h)
            h.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_pick_port_keeps_preferred_or_falls_back():
    port = _free_port()
    assert desktop._pick_port("127.0.0.1", port) == port
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", port))
        busy.listen()
        other = desktop._pick_port("127.0.0.1", port)
    assert other not in (port, 0)


def test_single_instance(tmp_path):
    first = desktop._single_instance(tmp_path)
    assert first is not None
    assert desktop._single_instance(tmp_path) is None
    first.close()
    again = desktop._single_instance(tmp_path)
    assert again is not None
    again.close()


class _Event(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


class FakeWindow:
    def __init__(self):
        self.events = types.SimpleNamespace(closed=_Event())
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def test_open_chart_opens_a_window_on_the_same_server(monkeypatch):
    made = []

    def create_window(title, url, **kwargs):
        made.append((title, url, kwargs))
        return FakeWindow()

    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(create_window=create_window))
    api = desktop.DesktopApi("http://127.0.0.1:8000")
    assert api.open_chart("5M", "B") is True
    assert api.open_chart("1H<script>", "Z") is True  # only letters and digits reach the address
    assert [m[1] for m in made] == ["http://127.0.0.1:8000/#popout/5M/B", "http://127.0.0.1:8000/#popout/1Hscript"]
    assert made[0][0] == "Wednesday 5M" and made[0][2]["js_api"] is api


def test_run_serves_dashboard_in_window(home, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("XAU_SOURCE", "synthetic")
    monkeypatch.setenv("XAU_PORT", str(port))
    seen = {}

    def create_window(title, url, **kwargs):
        seen["title"], seen["url"] = title, url
        return FakeWindow()

    def start(**kwargs):
        # The "window" is open: the server must answer while it is.
        with urllib.request.urlopen(seen["url"] + "/api/openapi.json", timeout=5) as resp:
            seen["api"] = json.load(resp)["info"]["title"]
        seen["start"] = kwargs

    monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(create_window=create_window, start=start))
    desktop.run()

    assert seen["url"] == f"http://127.0.0.1:{port}"
    assert seen["api"] == "Wednesday"
    assert seen["start"]["private_mode"] is False
    assert not (home / ".env").exists()  # the desktop app is set up from the dashboard, not a .env
    assert (home / "data" / "xau.db").is_file()
    assert (home / "logs" / "screener.log").is_file()
    # The server stopped with the window, and the next run can have the same port.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert desktop._pick_port("127.0.0.1", port) == port
