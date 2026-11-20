# Plugins

Features can live outside this repository, in a separately installed Python
package. Wednesday finds plugins through the `wednesday.plugins` entry point
group and runs exactly the same without any. **Settings > Plugins** lists what's
installed and why a plugin didn't load. `GET /api/plugins` gives the same list
plus the feature ids the loaded plugins provide, which the dashboard uses to
unlock screens.

## Writing one

```toml
# pyproject.toml of the plugin package
[project]
name = "wednesday-ee"
dependencies = ["wednesday"]

[project.entry-points."wednesday.plugins"]
ee = "wednesday_ee:plugin"
```

```python
# wednesday_ee/__init__.py
from fastapi import APIRouter
from wednesday.plugins import Plugin

router = APIRouter()

@router.get("/hello")
def hello() -> dict:
    return {"hello": "ee"}

def register(ctx):
    ctx.add_router(router)                       # served at /api/ee/ee/hello
    ctx.on_after_scan(lambda result, engine: ...)
    ctx.add_job("recap", 3600, lambda: ...)
    ctx.provide("llm.recap")                     # e.g. only when a licence allows it

plugin = Plugin(name="ee", version="0.1.0", api=1, register=register, features=["journal.multi"])
```

The entry point can name any object with `register(ctx)` and `api`; `name`,
`version` and `features` are optional.

## The context

| Member | What it is |
| --- | --- |
| `ctx.add_router(router)` | Mount a FastAPI router under `/api/ee/<plugin name>` |
| `ctx.on_after_scan(fn)` | `fn(result, engine)` after every scan (after alerts), on the scan worker thread: use `engine.call(lambda feed: ...)` for anything that reads the feed |
| `ctx.on_alert(fn)` | `fn(keys, result)` when order block alerts were just sent |
| `ctx.add_job(name, every_seconds, fn)` | Run `fn` on its own thread every N seconds (≥ 1) until shutdown |
| `ctx.provide(*features)` | Add feature ids at runtime |
| `ctx.snapshot()` | `(version, scan result, M1 bars)` of the running engine |
| `ctx.store` | The database (`storage.Store`) |
| `ctx.runtime` | The `engine.Runtime`: settings, bias, calendar, journals. Avoid in new code: it has no HTTP equivalent (see below) |

Hooks and jobs are isolated: an exception is logged and never stops the scan
loop or other plugins. A plugin that raises in `register` is skipped and its
features are not offered.

## Plugins and a server in another language (#96)

If the server moves to Rust, a plugin can't be a Python package it imports. The
plan is to run a plugin **as its own process that talks HTTP**, the way the
dashboard talks to the server:

- The server starts the plugin's executable with a port on `127.0.0.1`, the
  server's address and a token for this run, and reads its manifest (`GET
  /manifest`: name, version, plugin API, feature ids, whether it handles
  licences).
- Requests to `/api/ee/<name>/...` are forwarded to the plugin; so is
  `/api/licence` when the plugin handles licences.
- The server posts events to the plugin: after each scan (the `/api/scan`
  payload) and after order block alerts (their keys).
- The plugin calls the server's API back with the token: the scan and candles,
  its own settings (namespaced to the plugin) and its feature ids.
- Timers (the licence check) run inside the plugin.

That keeps the plugin in any language (Wednesday EE can stay Python, built into
its own executable and downloaded only with a licence), and a crash in it can't
stop the scan. A dynamic library was ruled out: an unstable ABI, a build per OS,
code signing on macOS, and a crash takes the server down. WebAssembly was ruled
out for now: the plugin needs the network (the licence server, LLM providers)
and the LLM SDKs, so it would need host functions for all of that and a rewrite.

To keep that move small, the context stays small. Wednesday EE uses only
`add_router`, `add_job`, `set_features`, `set_licence_provider` and settings
through `ctx.store` (`get_setting`, `set_setting`, `delete_setting`). Every
member of the context has to map onto an HTTP call or an event above: `ctx.app`
and `ctx.runtime` don't, so new plugin code shouldn't use them, and
`tests/test_plugins.py` fails if the context gains a member without this
section being updated.

## Licences and paid features

One plugin can handle licences with `ctx.set_licence_provider(provider)`. The
provider has `status()`, `activate(key)` (raise `ValueError` with a message for
a bad key) and `clear()`, and Wednesday serves it as `GET / PUT / DELETE
/api/licence` for **Settings > Plan**. The provider switches the plugin's
features with `ctx.set_features(...)`: all of them off without a valid licence.

`wednesday/features.py` lists the paid feature ids with a title and a line on
what each does, so the Plan screen can show them, locked, even when no plugin is
installed. A plugin can provide ids that aren't in the list; they show too.

## Downloading Wednesday EE with a licence key

Wednesday EE isn't published; a licence key brings it. When no plugin handles
licences and a key from the licence server (`WEDK-...`) is entered in
**Settings > Plan**, Wednesday (`plugin_install.py`):

1. sends the key and this install's device id to `POST /v1/download` on the
   licence server (`WEDNESDAY_LICENCE_SERVER`, default `http://127.0.0.1:8790`);
2. checks the answer: a manifest (package, version, file name, SHA-256) signed
   with Ed25519 by a key in `plugin_keys.py`, and a wheel whose hash matches it.
   Only `wednesday_ee` is accepted, and nothing downloaded runs before this check;
3. unpacks the wheel to `data/plugins/wednesday_ee/` (`WEDNESDAY_PLUGINS_DIR`),
   refusing files that would land outside it, replacing an older copy whole;
4. loads it straight away, without a restart, and hands it the key to activate.

At startup every folder in `data/plugins/` is added to `sys.path` after what pip
installed, so a local `uv pip install -e ../wednesday-ee` wins while developing.
The desktop app keeps `data/` in its home folder (`%LOCALAPPDATA%\Wednesday`).

The public keys in `plugin_keys.py` are safe to publish; the matching private key
stays with whoever signs. A free user never gets Wednesday EE's code: the licence
server hands it out only for a usable key.

Free, with or without a licence: the screener, alerts, the bias, the news card
and one journal.

## Versioning

`wednesday.plugins.PLUGIN_API` (now `1`) is bumped when the context changes
incompatibly. A plugin declares the version it was written for in `api`; a
mismatch is skipped with a clear message in Settings > Plugins instead of
failing at runtime. Adding members to the context doesn't bump it.

## Developing against a local checkout

```bash
cd wednesday
uv add --editable ../wednesday-ee   # or: uv pip install -e ../wednesday-ee
make serve
```

Users don't install it by hand: entering a licence key downloads it (above).

## What a plugin can and can't protect

Code in a plugin is installed on the user's machine, where it can be read.
Keeping it in a private repository, and handing it out only for a licence key,
keeps it away from free users, not from whoever has a licence. Anything that must be enforced (licences for
hosted services, downloads) belongs on a server the plugin talks to.
