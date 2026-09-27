"""Plugins: extra features in a separately installed package.

A plugin is a Python package that declares an entry point in the
``wednesday.plugins`` group. The entry point names an object with:

* ``api``: the plugin API version it was written for (see :data:`PLUGIN_API`)
* ``register(ctx)``: called once at startup with a :class:`PluginContext`
* optionally ``name`` and ``version`` (default: the entry point's name and the
  package's version) and ``features``: feature ids it provides, e.g.
  ``["llm.second_brain"]``

A module works as that object::

    # pyproject.toml of the plugin package
    [project.entry-points."wednesday.plugins"]
    ee = "wednesday_ee:plugin"

    # wednesday_ee/__init__.py
    from wednesday.plugins import Plugin
    def register(ctx): ctx.add_router(router); ctx.on_after_scan(check)
    plugin = Plugin(name="ee", api=1, register=register, features=["llm.recap"])

A plugin can also be downloaded rather than installed: ``plugin_install.py``
fetches a signed wheel from the licence server and unpacks it under
``data/plugins/``, which is added to ``sys.path`` before plugins are looked up.

Wednesday runs the same without any plugin. A plugin written for another API
version, or one that raises while registering, is skipped with a log line; a
hook that raises is logged and never stops scanning.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Callable, Iterable

from fastapi import APIRouter, FastAPI
from starlette.routing import Mount

log = logging.getLogger(__name__)

GROUP = "wednesday.plugins"
PLUGIN_API = 1  # bump when the context's surface changes incompatibly
ROUTE_PREFIX = "/api/ee"


@dataclass
class Plugin:
    """Convenience for plugin authors; any object with the same attributes works."""

    name: str
    register: Callable[[PluginContext], None]
    api: int = PLUGIN_API
    version: str | None = None
    features: list[str] = field(default_factory=list)


@dataclass
class PluginInfo:
    name: str
    version: str | None
    api: int | None
    features: list[str]
    loaded: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "api": self.api, "features": self.features,
                "loaded": self.loaded, "error": self.error}


class Hooks:
    """Callbacks the runtime calls. Every call is isolated: one failing plugin can't stop the rest."""

    def __init__(self):
        self.after_scan: list[Callable] = []  # fn(result, engine)
        self.on_alert: list[Callable] = []  # fn(keys, result): order block alerts just sent
        self.stop = threading.Event()  # set on shutdown; jobs wait on it
        self.licence = None  # a licence provider (see PluginContext.set_licence_provider), at most one
        self._jobs: list[threading.Thread] = []

    @staticmethod
    def _call(fns: list[Callable], *args) -> None:
        for fn in list(fns):
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - a plugin must never stop the scan loop
                log.exception("plugin hook %s failed", getattr(fn, "__qualname__", fn))

    def run_after_scan(self, result, engine) -> None:
        self._call(self.after_scan, result, engine)

    def run_on_alert(self, keys: list[str], result) -> None:
        if keys:
            self._call(self.on_alert, keys, result)

    def start_job(self, name: str, every_seconds: float, fn: Callable[[], None]) -> None:
        def loop():
            while not self.stop.wait(every_seconds):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("plugin job %s failed", name)

        t = threading.Thread(target=loop, name=f"plugin-job-{name}", daemon=True)
        self._jobs.append(t)
        t.start()

    def shutdown(self) -> None:
        self.stop.set()


class PluginContext:
    """What a plugin gets to work with. Only this surface is stable across versions of the same API."""

    api = PLUGIN_API

    def __init__(self, app: FastAPI, runtime, info: PluginInfo):
        self.app = app
        self.runtime = runtime  # engine snapshots, store, bias, calendar, brief, lab (see engine.Runtime)
        self._info = info

    @property
    def store(self):
        return self.runtime.store

    def snapshot(self):
        """(version, scan result, M1 bars) of the running engine."""
        return self.runtime.engine.snapshot()

    def add_router(self, router: APIRouter) -> None:
        """Routes are mounted under ``/api/ee/<plugin name>``."""
        self.app.include_router(router, prefix=f"{ROUTE_PREFIX}/{self._info.name}")
        # A plugin loaded after startup (just downloaded) would land behind the dashboard's
        # catch-all mount at "/"; keep mounts last so its routes are reachable.
        routes = self.app.router.routes
        mounts = [r for r in routes if isinstance(r, Mount)]
        for m in mounts:
            routes.remove(m)
        routes.extend(mounts)

    def on_after_scan(self, fn: Callable) -> None:
        self.runtime.hooks.after_scan.append(fn)

    def on_alert(self, fn: Callable) -> None:
        self.runtime.hooks.on_alert.append(fn)

    def add_job(self, name: str, every_seconds: float, fn: Callable[[], None]) -> None:
        """Run ``fn`` every ``every_seconds`` on its own thread until shutdown."""
        if every_seconds < 1:
            raise ValueError("jobs run at most once a second")
        self.runtime.hooks.start_job(f"{self._info.name}.{name}", every_seconds, fn)

    def provide(self, *features: str) -> None:
        """Declare feature ids at runtime (e.g. only when a licence allows them)."""
        self._info.features.extend(f for f in features if f not in self._info.features)

    def set_features(self, features: Iterable[str]) -> None:
        """Replace this plugin's feature ids, e.g. when a licence is entered, removed or expires."""
        self._info.features[:] = list(dict.fromkeys(features))

    def set_licence_provider(self, provider) -> None:
        """Serve the dashboard's licence screen (``/api/licence``). The provider has
        ``status() -> dict``, ``activate(key: str) -> dict`` (``ValueError`` for a bad key)
        and ``clear() -> dict``. Only one plugin can provide it."""
        if self.runtime.hooks.licence is not None:
            raise RuntimeError("another plugin already provides the licence")
        self.runtime.hooks.licence = provider


def _version_of(ep: EntryPoint) -> str | None:
    dist = getattr(ep, "dist", None)
    return getattr(dist, "version", None)


def _add_installed_to_path() -> None:
    """Make downloaded plugins (``data/plugins/<name>/``) importable, after anything pip installed."""
    from .plugin_install import installed_dirs

    for d in installed_dirs():
        if str(d) not in sys.path:
            sys.path.append(str(d))


def _load_one(app: FastAPI, runtime, ep: EntryPoint) -> PluginInfo:
    info = PluginInfo(ep.name, _version_of(ep), None, [], loaded=False)
    try:
        obj = ep.load()
        info.name = str(getattr(obj, "name", None) or ep.name)
        info.version = getattr(obj, "version", None) or info.version
        info.api = getattr(obj, "api", None)
        info.features = list(getattr(obj, "features", None) or [])
        register = getattr(obj, "register", None)
        if not callable(register):
            info.error = "has no register(ctx)"
            log.warning("plugin %s skipped: %s", info.name, info.error)
            return info
        if info.api != PLUGIN_API:
            info.error = f"written for plugin API {info.api}, this Wednesday has {PLUGIN_API}"
            log.warning("plugin %s skipped: %s", info.name, info.error)
            return info
        register(PluginContext(app, runtime, info))
        info.loaded = True
        log.info("plugin %s %s loaded", info.name, info.version or "")
    except Exception as exc:  # noqa: BLE001 - a broken plugin must not stop startup
        info.error = f"{type(exc).__name__}: {exc}"
        info.features = []
        log.exception("plugin %s failed to load", info.name)
    return info


def load_plugins(app: FastAPI, runtime, eps: Iterable[EntryPoint] | None = None) -> list[PluginInfo]:
    """Load and register every installed plugin. Never raises."""
    if eps is None:
        _add_installed_to_path()
    found = list(entry_points(group=GROUP) if eps is None else eps)
    return [_load_one(app, runtime, ep) for ep in found]


def load_new_plugins(app: FastAPI, runtime, infos: list[PluginInfo]) -> list[PluginInfo]:
    """After a download: load plugins that weren't there at startup, adding them to ``infos``."""
    _add_installed_to_path()
    importlib.invalidate_caches()
    known = {i.name for i in infos}
    new = [_load_one(app, runtime, ep) for ep in entry_points(group=GROUP) if ep.name not in known]
    infos.extend(new)
    return new


def features(infos: list[PluginInfo]) -> list[str]:
    return sorted({f for i in infos if i.loaded for f in i.features})
