import base64
import hashlib
import io
import json
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from wednesday import plugin_install, plugin_keys, plugins, server
from wednesday.engine import Runtime
from wednesday.journal.service import Journals
from wednesday.scanner import ScanConfig
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME

PRIV = Ed25519PrivateKey.generate()
PUB = PRIV.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
PACKAGE = "wed_fake"

PLUGIN_SOURCE = '''
from fastapi import APIRouter
from wednesday.plugins import Plugin


class Licence:
    def __init__(self, ctx):
        self.ctx, self.plan = ctx, None

    def status(self):
        return {"valid": bool(self.plan), "plan": self.plan, "features": self.ctx._info.features}

    def activate(self, key):
        if key != "WEDK-GOOD":
            raise ValueError("That licence key isn't known")
        self.plan = "pro"
        self.ctx.set_features(["journal.multi"])
        return self.status()

    def clear(self):
        self.plan = None
        self.ctx.set_features([])
        return self.status()


def register(ctx):
    router = APIRouter()
    router.get("/ping")(lambda: {"pong": True})
    ctx.add_router(router)
    ctx.set_licence_provider(Licence(ctx))


plugin = Plugin(name="fake", version="0.2.0", register=register)
'''


def wheel(files: dict[str, str] | None = None) -> bytes:
    files = files or {
        f"{PACKAGE}/__init__.py": PLUGIN_SOURCE,
        f"{PACKAGE}-0.2.0.dist-info/METADATA": f"Metadata-Version: 2.1\nName: {PACKAGE}\nVersion: 0.2.0\n",
        f"{PACKAGE}-0.2.0.dist-info/entry_points.txt": f"[wednesday.plugins]\nfake = {PACKAGE}:plugin\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def answer(data: bytes, priv=PRIV, **overrides) -> dict:
    manifest = {"kid": "test", "package": PACKAGE, "version": "0.2.0", "filename": f"{PACKAGE}-0.2.0-py3-none-any.whl",
                "sha256": hashlib.sha256(data).hexdigest(), **overrides}
    body = b64e(json.dumps(manifest).encode())
    return {"kid": "test", "manifest": body, "signature": b64e(priv.sign(f"WEDPKG1.{body}".encode())),
            "wheel": base64.b64encode(data).decode()}


@pytest.fixture
def trusted(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_keys, "TRUSTED", {"test": base64.b64encode(PUB).decode()})
    monkeypatch.setattr(plugin_install, "ALLOWED", {PACKAGE})
    monkeypatch.setenv(plugin_install.PLUGINS_DIR_ENV, str(tmp_path / "plugins"))
    yield tmp_path / "plugins"
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]
    for d in plugin_install.installed_dirs():
        if str(d) in sys.path:
            sys.path.remove(str(d))


def test_only_a_genuine_download_is_unpacked(trusted):
    data = wheel()
    manifest, got = plugin_install.verify(answer(data))
    assert got == data and manifest["version"] == "0.2.0"

    with pytest.raises(plugin_install.InstallError, match="genuine"):
        plugin_install.verify(answer(data, priv=Ed25519PrivateKey.generate()))
    tampered = answer(data)
    tampered["wheel"] = base64.b64encode(wheel({f"{PACKAGE}/__init__.py": "import os"})).decode()
    with pytest.raises(plugin_install.InstallError, match="doesn't match"):
        plugin_install.verify(tampered)
    with pytest.raises(plugin_install.InstallError, match="doesn't know"):
        plugin_install.verify(answer(data, kid="other"))
    with pytest.raises(plugin_install.InstallError, match="doesn't install"):
        plugin_install.verify(answer(data, package="evil"))
    with pytest.raises(plugin_install.InstallError, match="damaged"):
        plugin_install.verify({"manifest": "x"})

    dest = plugin_install.unpack(manifest, got)
    assert (dest / PACKAGE / "__init__.py").is_file() and plugin_install.installed_dirs() == [dest]
    assert json.loads((dest / ".wednesday-plugin.json").read_text())["version"] == "0.2.0"
    plugin_install.unpack(manifest, got)  # again: replaces the old copy whole
    assert plugin_install.installed_dirs() == [dest]


def test_a_wheel_cant_write_outside_its_folder(trusted):
    data = wheel({"../escaped.py": "x = 1"})
    manifest, got = plugin_install.verify(answer(data))
    with pytest.raises(plugin_install.InstallError, match="outside"):
        plugin_install.unpack(manifest, got)
    assert not (trusted / "escaped.py").exists() and plugin_install.installed_dirs() == []


def test_device_id_is_kept(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'd.db'}")
    first = plugin_install.device_id(store)
    assert plugin_install.device_id(store) == first and len(first) == 32


def test_licence_key_downloads_the_plugin_and_unlocks_features(trusted, tmp_path, monkeypatch):
    real = plugins.entry_points
    # Only plugins from the download folder: ignore whatever is pip installed in this environment.
    monkeypatch.setattr(plugins, "entry_points", lambda group: [
        ep for ep in real(group=group) if ep.dist and str(trusted) in str(ep.dist.locate_file(""))])
    monkeypatch.setattr(server, "load_plugins", lambda app, rt: plugins.load_plugins(app, rt, []))
    fetched = []
    monkeypatch.setattr(plugin_install, "fetch", lambda key, device, url=None: fetched.append((key, device))
                        or answer(wheel()))
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<html></html>")
    store = Store(f"sqlite:///{tmp_path / 'p.db'}")
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, journals=Journals(store))
    api = TestClient(server.create_app(runtime, ui_dir=tmp_path / "ui"))

    assert api.get("/api/licence").json() == {"available": False}
    assert api.post("/api/journals", json={"name": "One"}).status_code == 200
    assert api.post("/api/journals", json={"name": "Two"}).status_code == 402  # free: one journal

    assert "WEDK" in api.put("/api/licence", json={"key": "WED1.abc.def"}).json()["detail"]
    assert not fetched

    st = api.put("/api/licence", json={"key": "WEDK-GOOD"})
    assert st.status_code == 200, st.text
    assert st.json()["valid"] and st.json()["plan"] == "pro"
    assert fetched == [("WEDK-GOOD", store.get_setting("ee_device")["id"])]
    body = api.get("/api/plugins").json()
    assert body["plugins"][0]["name"] == "fake" and body["features"] == ["journal.multi"]
    assert api.get("/api/ee/fake/ping").json() == {"pong": True}  # not swallowed by the dashboard's mount
    assert api.post("/api/journals", json={"name": "Two"}).status_code == 200
    assert api.get("/api/journals").json()["multi"]

    # Installed now: the next key goes straight to the plugin, no download.
    assert api.put("/api/licence", json={"key": "WEDK-BAD"}).status_code == 422
    assert len(fetched) == 1
    runtime.stop()


def test_download_refused(trusted, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "load_plugins", lambda app, rt: plugins.load_plugins(app, rt, []))

    def refuse(key, device, url=None):
        raise plugin_install.InstallError("This licence was revoked")

    monkeypatch.setattr(plugin_install, "fetch", refuse)
    store = Store(f"sqlite:///{tmp_path / 'p.db'}")
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store)
    api = TestClient(server.create_app(runtime, ui_dir=tmp_path))
    res = api.put("/api/licence", json={"key": "WEDK-GOOD"})
    assert res.status_code == 422 and "revoked" in res.json()["detail"]
    assert plugin_install.installed_dirs() == []
    runtime.stop()


def test_fetch_reports_an_unreachable_server():
    with pytest.raises(plugin_install.InstallError, match="reach"):
        plugin_install.fetch("WEDK-X", "d", url="http://127.0.0.1:9", timeout=1)
