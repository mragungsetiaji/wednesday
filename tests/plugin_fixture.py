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
