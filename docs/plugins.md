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

plugin = Plugin(name="ee", version="0.1.0", api=1, register=register, features=["lab.signed_models"])
```

The entry point can name any object with `register(ctx)` and `api`; `name`,
`version` and `features` are optional.

## The context

| Member | What it is |
| --- | --- |
| `ctx.add_router(router)` | Mount a FastAPI router under `/api/ee/<plugin name>` |
| `ctx.on_after_scan(fn)` | `fn(result, engine)` after every scan (after alerts) |
| `ctx.on_alert(fn)` | `fn(keys, result)` when order block alerts were just sent |
| `ctx.add_job(name, every_seconds, fn)` | Run `fn` on its own thread every N seconds (≥ 1) until shutdown |
| `ctx.provide(*features)` | Add feature ids at runtime |
| `ctx.snapshot()` | `(version, scan result, M1 bars)` of the running engine |
| `ctx.store` | The database (`storage.Store`) |
| `ctx.runtime` | The `engine.Runtime`: settings, bias, calendar, brief, lab |

Hooks and jobs are isolated: an exception is logged and never stops the scan
loop or other plugins. A plugin that raises in `register` is skipped and its
features are not offered.

## Licences and paid features

One plugin can handle licences with `ctx.set_licence_provider(provider)`. The
provider has `status()`, `activate(key)` (raise `ValueError` with a message for
a bad key) and `clear()`, and Wednesday serves it as `GET / PUT / DELETE
/api/licence` for **Settings > Plan**. The provider switches the plugin's
features with `ctx.set_features(...)`: all of them off without a valid licence.

`wednesday/features.py` lists the paid feature ids with a title and a line on
what each does, so the Plan screen can show them, locked, even when no plugin is
installed. A plugin can provide ids that aren't in the list; they show too.

Free, with or without a licence: the screener, alerts, the bias, the news card,
the news brief with your own API key, and labelling and training your own models.

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

Installing from a private git repository:
`uv pip install "git+https://<token>@github.com/<owner>/wednesday-ee"`.

## What a plugin can and can't protect

Code in a plugin is installed on the user's machine, where it can be read.
Keeping it in a private repository keeps it out of this public one, not out of
the hands of whoever installs it. Anything that must be enforced (licences for
hosted services, model downloads) belongs on a server the plugin talks to.
