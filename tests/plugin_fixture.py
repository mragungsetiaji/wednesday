"""Plugins used by test_plugins.py (loaded through fake entry points)."""

from fastapi import APIRouter

from wednesday.plugins import Plugin

calls: dict[str, list] = {"after_scan": [], "on_alert": [], "job": []}


def _register(ctx):
    router = APIRouter()

    @router.get("/hello")
    def hello() -> dict:
        version, result, _ = ctx.snapshot()
        return {"hello": "ee", "version": version, "has_store": ctx.store is not None}

    ctx.add_router(router)
    ctx.on_after_scan(lambda result, engine: calls["after_scan"].append(engine.symbol))
    ctx.on_alert(lambda keys, result: calls["on_alert"].append(keys))
    ctx.on_after_scan(lambda result, engine: 1 / 0)  # a failing hook must not stop the others
    ctx.add_job("tick", 1, lambda: calls["job"].append(1))
    ctx.provide("llm.recap")


good = Plugin(name="ee", version="0.1.0", register=_register, features=["lab.signed_models"])
future = Plugin(name="future", api=99, register=_register)


def _boom(ctx):
    raise RuntimeError("licence server down")


broken = Plugin(name="broken", register=_boom, features=["should.not.show"])
not_a_plugin = object()


class FakeLicence:
    """Accepts the key "good"; switches the plugin's features with it."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.plan = None

    def status(self):
        return {"plan": self.plan, "valid": self.plan is not None}

    def activate(self, key):
        if key != "good":
            raise ValueError("That key isn't valid")
        self.plan = "pro"
        self.ctx.set_features(["llm.recap", "vendor.extra"])
        return self.status()

    def clear(self):
        self.plan = None
        self.ctx.set_features([])
        return self.status()


def _register_licensed(ctx):
    ctx.set_licence_provider(FakeLicence(ctx))


licensed = Plugin(name="lic", register=_register_licensed)
second_licence = Plugin(name="lic2", register=_register_licensed)
