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

    def fire(self):
        for fn in list(self):
            fn()


class FakeWindow:
    def __init__(self, url="", x=100, y=100, width=1440, height=900, **kw):
        self.url, self.x, self.y, self.width, self.height = url, x, y, width, height
        self.events = types.SimpleNamespace(closed=_Event(), closing=_Event(), maximized=_Event(), restored=_Event())
        self.destroyed = False
        self.js_api = kw.get("js_api")

    def get_current_url(self):
        return self.url

    def move(self, x, y):
        self.x, self.y = x, y

    def resize(self, w, h):
        self.width, self.height = w, h

    def maximize(self):
        self.events.maximized.fire()

    def destroy(self):
        self.destroyed = True
        self.events.closed.fire()


class FakeWebview:
    """create_window, start, windows and screens, enough for the desktop app's window handling."""

    def __init__(self, screens=((0, 0, 1920, 1080),), during=None):
        self.windows = []
        self.screens = [types.SimpleNamespace(x=x, y=y, width=w, height=h) for x, y, w, h in screens]
        self.during = during or (lambda wv: None)
        self.started = {}

    def create_window(self, title, url, **kwargs):
        w = FakeWindow(url, **{k: v for k, v in kwargs.items() if k in ("x", "y", "width", "height", "js_api")})
        w.title = title
        self.windows.append(w)
        return w

    def start(self, func=None, **kwargs):
        self.started = kwargs
        if func:
            func()
        self.during(self)  # the trader uses the app
        main = self.windows[0]
        main.events.closing.fire()
        main.events.closed.fire()


def test_open_chart_opens_a_window_on_the_same_server(monkeypatch):
    wv = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", wv)
    api = desktop.DesktopApi("http://127.0.0.1:8000")
    assert api.open_chart("5M", "B") is True
    assert api.open_chart("1H<script>", "Z") is True  # only letters and digits reach the address
    assert [w.url for w in wv.windows] == ["http://127.0.0.1:8000/#popout/5M/B", "http://127.0.0.1:8000/#popout/1Hscript"]
    assert wv.windows[0].title == "Wednesday 5M" and wv.windows[0].js_api is api


def test_run_serves_dashboard_in_window(home, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("XAU_SOURCE", "synthetic")
    monkeypatch.setenv("XAU_PORT", str(port))
    seen = {}

    def during(wv):
        # The "window" is open: the server must answer while it is.
        with urllib.request.urlopen(wv.windows[0].url + "/api/openapi.json", timeout=5) as resp:
            seen["api"] = json.load(resp)["info"]["title"]

    wv = FakeWebview(during=during)
    monkeypatch.setitem(sys.modules, "webview", wv)
    desktop.run()

    assert wv.windows[0].url == f"http://127.0.0.1:{port}"
    assert seen["api"] == "Wednesday"
    assert wv.started["private_mode"] is False
    assert not (home / ".env").exists()  # the desktop app is set up from the dashboard, not a .env
    assert (home / "data" / "xau.db").is_file()
    assert (home / "logs" / "screener.log").is_file()
    # The server stopped with the window, and the next run can have the same port.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert desktop._pick_port("127.0.0.1", port) == port


def test_windows_come_back_where_they_were_and_on_a_screen(home, monkeypatch):
    monkeypatch.setenv("XAU_SOURCE", "synthetic")
    monkeypatch.setenv("XAU_PORT", str(_free_port()))

    def first_session(wv):  # dashboard moved, a 5M chart on the second monitor, then the app closes
        main = wv.windows[0]
        main.move(40, 30)
        main.resize(1600, 1000)
        main.js_api.open_chart("5M", "A")
        pop = wv.windows[1]
        pop.move(2100, 50)
        pop.url = pop.url.replace("5M", "15M")  # its timeframe was switched

    two = FakeWebview(screens=((0, 0, 1920, 1080), (1920, 0, 1920, 1080)), during=first_session)
    monkeypatch.setitem(sys.modules, "webview", two)
    desktop.run()
    assert [w.destroyed for w in two.windows] == [False, True]  # the pop-out closed with the dashboard

    one = FakeWebview(screens=((0, 0, 1920, 1080),))  # the second monitor is gone
    monkeypatch.setitem(sys.modules, "webview", one)
    desktop.run()
    main, pop = one.windows
    assert (main.x, main.y, main.width, main.height) == (40, 30, 1600, 1000)
    assert pop.url.endswith("#popout/15M/A")
    assert 0 <= pop.x and pop.x + pop.width <= 1920  # moved onto the screen that's there


def test_fit_on_screen_and_arrange():
    from wednesday.workspaces import fit_on_screen

    screens = [{"x": 0, "y": 0, "width": 1920, "height": 1080}]
    on = {"x": 100, "y": 100, "width": 800, "height": 600}
    assert fit_on_screen(on, screens) == on
    off = fit_on_screen({"x": 3000, "y": 100, "width": 2500, "height": 600}, screens)
    assert off["width"] == 1920 and 0 <= off["x"] and off["x"] + off["width"] <= 1920


def test_arrange_replaces_the_pop_outs(monkeypatch):
    wv = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", wv)
    api = desktop.DesktopApi("http://h")
    main = wv.create_window("Wednesday", "http://h")
    api.tracker.main = main
    api.tracker.track(main, "#")
    api.open_chart("1H")
    assert api.arrange([{"route": "#", "x": 0, "y": 0, "width": 1000, "height": 700},
                        {"route": "#popout/4H/B", "x": 1000, "y": 0, "width": 900, "height": 700}])
    assert wv.windows[1].destroyed and wv.windows[2].url == "http://h/#popout/4H/B"
    assert (main.width, main.height) == (1000, 700)
    assert [w["route"] for w in api.window_layout()] == ["#", "#popout/4H/B"]
    assert api.arrange([{"route": "javascript:alert(1)", "x": 0, "y": 0, "width": 1, "height": 1}]) is False
