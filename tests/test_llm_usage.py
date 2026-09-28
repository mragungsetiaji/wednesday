import sys
import time
import types
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from wednesday import brief as brief_mod
from wednesday.brief import BriefRunner, BriefSettings
from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.llm_usage import BudgetExceeded, UsageLog, clean_prices
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.timeframes import TIMEFRAMES_BY_NAME

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 500_000, "cache_write_tokens": 0}


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'u.db'}")


def test_cost_from_the_price_table(store):
    log = UsageLog(store)
    # claude-sonnet-4-5: $3 in, $15 out, $0.30 cache read per million tokens.
    assert log.cost("claude-sonnet-4-5", TOKENS) == pytest.approx(3 + 1.5 + 0.15)
    assert log.cost("claude-sonnet-4-5-20250929", TOKENS) == pytest.approx(4.65)  # dated snapshot
    assert log.cost("some-new-model", TOKENS) is None
    log.set_prices({"some-new-model": {"input": 2, "output": 8}})
    assert log.prices() == {"some-new-model": {"input": 2.0, "output": 8.0, "cache_read": 0.2, "cache_write": 2.5}}
    assert log.cost("some-new-model", TOKENS) == pytest.approx(2 + 0.8 + 0.1)
    with pytest.raises(ValueError):
        clean_prices({"x": {"input": "cheap"}})
    with pytest.raises(ValueError):
        clean_prices({"x": {"input": -1, "output": 1}})


def test_record_uses_provider_counts_or_estimates(store):
    log = UsageLog(store)
    row = log.record("brief", "anthropic", "claude-haiku-4-5", {"input_tokens": 1200, "output_tokens": 300,
                                                                "cache_read_tokens": 800})
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (1200, 300, 800)
    assert not row["estimated"] and row["cost"] == pytest.approx((1200 * 1 + 300 * 5 + 800 * 0.1) / 1e6)
    guess = log.record("brief", "anthropic", "claude-haiku-4-5", None, text_in="x" * 4000, text_out="y" * 400)
    assert guess["estimated"] and (guess["input_tokens"], guess["output_tokens"]) == (1000, 100)
    assert len(store.llm_usage_since("2000-01-01")) == 2


def test_budget_levels_and_guard(store):
    log = UsageLog(store)
    log.set_prices({"m": {"input": 1, "output": 1}})
    assert log.state()["level"] == "none"
    log.guard("brief", scheduled=True)  # no budget: never refused
    log.set_budget(10)
    log.record("brief", "anthropic", "m", {"input_tokens": 7_000_000})
    assert log.state()["level"] == "ok"
    log.record("recap", "anthropic", "m", {"output_tokens": 1_500_000})
    st = log.state()
    assert st["level"] == "warn" and st["spent"] == pytest.approx(8.5) and st["share"] == pytest.approx(0.85)
    log.record("brief", "anthropic", "m", {"input_tokens": 2_000_000})
    assert log.state()["level"] == "over"
    with pytest.raises(BudgetExceeded, match="Scheduled"):
        log.guard("recap", scheduled=True)
    with pytest.raises(BudgetExceeded, match="Confirm"):
        log.guard("brief")
    with pytest.raises(BudgetExceeded):
        log.guard("recap", scheduled=True, confirmed=True)  # a schedule can't confirm
    log.guard("brief", confirmed=True)  # a manual run the user confirmed
    last_month = datetime(2026, 8, 31, 23, 0, tzinfo=timezone.utc)
    log.record("brief", "anthropic", "m", {"input_tokens": 50_000_000}, when=last_month)
    assert log.state()["spent"] == pytest.approx(10.5)  # only this month counts
    with pytest.raises(ValueError):
        log.set_budget(-1)


def test_report(store):
    log = UsageLog(store)
    log.set_prices({"a": {"input": 1, "output": 2}, "b": {"input": 10, "output": 20}})
    log.record("brief", "anthropic", "a", {"input_tokens": 1_000_000}, when=NOW)
    log.record("recap", "openai", "b", {"output_tokens": 1_000_000}, when=NOW)
    log.record("brief", "openai", "b", {"input_tokens": 100_000}, when=NOW.replace(day=19))
    log.record("brief", "anthropic", "zzz", {"input_tokens": 5}, when=NOW)  # no price
    r = log.report(NOW)
    assert r["month"] == "2026-09"
    assert [(g["key"], g["calls"]) for g in r["by_feature"]] == [("recap", 1), ("brief", 3)]
    assert r["by_model"][0]["key"] == "b" and r["by_model"][0]["cost"] == pytest.approx(21)
    assert len(r["daily"]) == 30 and r["daily"][-1] == ["2026-09-20", pytest.approx(21)]
    assert r["top"][0]["cost"] == pytest.approx(20) and r["budget"]["unpriced_calls"] == 1


def test_brief_logs_usage_and_trims_unchanged_pages(monkeypatch, store):
    pages = {"https://a/ok": "a" * 5000, "https://b/ok": "b" * 5000}
    monkeypatch.setattr(brief_mod, "fetch_source", lambda url, n: {
        "url": url, "ok": True, "error": None, "chars": len(pages[url]), "truncated": False, "text": pages[url]})
    sent = []

    def fake_ask(model, system, user):
        sent.append(user)
        return "- DXY up\nBIAS: BEARISH", {"input_tokens": 2000, "output_tokens": 50, "cache_read_tokens": 900}

    monkeypatch.setitem(brief_mod.ASK, "anthropic", fake_ask)
    runner = BriefRunner(store, BriefSettings(urls=list(pages)))
    runner.last = runner.generate({"Price": "2470"})
    assert runner.last["usage"]["input_tokens"] == 2000 and not runner.last["usage"]["estimated"]
    row = store.llm_usage_since("2000-01-01")[0]
    assert (row["feature"], row["model"], row["cache_read_tokens"]) == ("brief", "claude-opus-5", 900)

    pages["https://b/ok"] = "c" * 5000  # a changed; b's page is new
    pages["https://a/ok"] = "a" * 5000
    out = runner.generate({"Price": "2471"})
    assert [s.get("unchanged", False) for s in out["sources"]] == [True, False]
    second = sent[-1]
    assert "a" * 1500 in second and "a" * 1501 not in second and "c" * 5000 in second
    assert "<previous_brief" in second and "unchanged since the previous brief" in second
    assert "<previous_brief" not in sent[0]


def test_brief_reply_without_usage_is_estimated(monkeypatch, store):
    monkeypatch.setattr(brief_mod, "fetch_source", lambda url, n: {
        "url": url, "ok": True, "error": None, "chars": 4, "truncated": False, "text": "news"})
    monkeypatch.setitem(brief_mod.ASK, "anthropic", lambda model, system, user: "BIAS: NEUTRAL")
    out = BriefRunner(store, BriefSettings(urls=["https://a"])).generate({})
    assert out["usage"]["estimated"] and out["usage"]["input_tokens"] > 0


def test_claude_call_caches_the_system_prompt_and_reads_usage(monkeypatch):
    seen = {}

    class Messages:
        def create(self, **kw):
            seen.update(kw)
            usage = types.SimpleNamespace(input_tokens=120, output_tokens=40, cache_read_input_tokens=1500,
                                          cache_creation_input_tokens=0)
            return types.SimpleNamespace(stop_reason="end_turn", usage=usage,
                                         content=[types.SimpleNamespace(type="text", text="BIAS: BULLISH")])

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda api_key=None: types.SimpleNamespace(messages=Messages(), beta=types.SimpleNamespace(messages=Messages()))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(brief_mod, "get_secret", lambda name: (None, None))
    text, usage = brief_mod.ask_claude("claude-haiku-4-5", "SYSTEM", "USER")
    assert text == "BIAS: BULLISH"
    assert usage == {"input_tokens": 120, "output_tokens": 40, "cache_read_tokens": 1500, "cache_write_tokens": 0}
    assert seen["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]


def test_openai_call_reads_usage(monkeypatch):
    class Responses:
        def create(self, **kw):
            usage = types.SimpleNamespace(input_tokens=2000, output_tokens=80,
                                          input_tokens_details=types.SimpleNamespace(cached_tokens=1500))
            return types.SimpleNamespace(output_text="BIAS: BEARISH", usage=usage)

    fake = types.ModuleType("openai")
    fake.OpenAI = lambda api_key=None: types.SimpleNamespace(responses=Responses())
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setattr(brief_mod, "get_secret", lambda name: (None, None))
    text, usage = brief_mod.ask_openai("gpt-5", "SYSTEM", "USER")
    assert usage == {"input_tokens": 500, "output_tokens": 80, "cache_read_tokens": 1500, "cache_write_tokens": 0}


def test_budget_is_enforced_by_the_api(tmp_path, monkeypatch, store):
    cfg = ScanConfig(lookback=20, timeframes=(TIMEFRAMES_BY_NAME["1H"],), params=DetectorParams(swing_length=2))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store,
                      brief=BriefRunner(store, BriefSettings(urls=["https://a"])))
    api = TestClient(create_app(runtime, ui_dir=tmp_path))
    monkeypatch.setattr(brief_mod, "provider_status", lambda: [
        {"id": "anthropic", "title": "Claude", "installed": True, "key_set": True}])
    monkeypatch.setattr(brief_mod, "fetch_source", lambda url, n: {
        "url": url, "ok": True, "error": None, "chars": 4, "truncated": False, "text": "news"})
    monkeypatch.setitem(brief_mod.ASK, "anthropic",
                        lambda m, s, u: ("BIAS: NEUTRAL", {"input_tokens": 1_000_000, "output_tokens": 0}))

    assert api.put("/api/llm/prices", json={"models": {"claude-opus-5": {"input": 5, "output": 25}}}).status_code == 200
    assert api.put("/api/llm/budget", json={"monthly_usd": "lots"}).status_code == 422
    assert api.put("/api/llm/budget", json={"monthly_usd": 4}).json()["budget"]["limit"] == 4
    assert api.post("/api/brief/generate").status_code == 202
    for _ in range(100):
        if not runtime.brief.running:
            break
        time.sleep(0.05)
    report = api.get("/api/llm/usage").json()
    assert report["budget"]["level"] == "over" and report["by_feature"][0]["key"] == "brief"
    refused = api.post("/api/brief/generate")
    assert refused.status_code == 409 and refused.headers["x-over-budget"] == "1"
    assert "budget" in refused.json()["detail"]
    assert api.post("/api/brief/generate", json={"confirm_over_budget": True}).status_code == 202
    assert api.get("/api/brief").json()["budget"]["level"] == "over"
