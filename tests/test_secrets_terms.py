"""Telegram token and LLM keys set from Settings, and the disclaimer the trader must accept."""

import pytest
from fastapi.testclient import TestClient

from wednesday import secret_store
from wednesday.alerts import AlertManager, AlertSettings, telegram_client
from wednesday.brief import BriefRunner, provider_status
from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store
from wednesday.terms import TERMS_VERSION

CFG = ScanConfig(lookback=20, params=DetectorParams(swing_length=2))


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def delete_password(self, service, account):
        self.store.pop((service, account))


@pytest.fixture
def keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(secret_store, "_keyring", lambda: kr)
    for var in secret_store.APP_SECRETS.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    return kr


@pytest.fixture
def api(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'a.db'}")
    runtime = Runtime(CFG, DataSettings(source="synthetic"), store, alerts=AlertManager(store, None),
                      brief=BriefRunner(store))
    yield runtime, TestClient(create_app(runtime, ui_dir=tmp_path))
    runtime.stop()


def test_secret_sources_and_env_fallback(keyring, monkeypatch):
    assert secret_store.get_secret("openai_api_key") == (None, None)
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert secret_store.get_secret("openai_api_key") == ("from-env", "env")
    assert secret_store.set_secret("openai_api_key", "from-settings") == "saved"
    assert secret_store.get_secret("openai_api_key") == ("from-settings", "saved")  # Settings win over .env
    secret_store.forget_secret("openai_api_key")
    assert secret_store.get_secret("openai_api_key") == ("from-env", "env")


def test_telegram_token_and_chat_from_settings(keyring, api):
    runtime, client = api
    body = client.get("/api/alerts").json()
    assert body["configured"] is False and body["token_source"] is None

    res = client.put("/api/alerts", json={"enabled": True, "chat_id": "123456", "telegram_bot_token": "1:ABC"})
    assert res.status_code == 200 and "1:ABC" not in res.text
    body = res.json()
    assert body["configured"] is True and body["token_source"] == "saved"
    assert body["settings"]["chat_id"] == "123456"
    assert runtime.alerts.client.token == "1:ABC" and runtime.alerts.client.chat_id == "123456"
    assert keyring.store == {("Wednesday", "telegram_bot_token"): "1:ABC"}
    assert "1:ABC" not in str(runtime.store.get_setting("alerts"))

    assert client.put("/api/alerts", json={"chat_id": "not a chat"}).status_code == 422
    body = client.put("/api/alerts", json={"chat_id": "123456", "forget_telegram_bot_token": True}).json()
    assert body["configured"] is False and runtime.alerts.client is None


def test_telegram_chats_endpoint(keyring, api, monkeypatch):
    runtime, client = api
    assert client.get("/api/alerts/chats").status_code == 409  # no token yet
    client.put("/api/alerts", json={"telegram_bot_token": "1:ABC"})
    monkeypatch.setattr(type(runtime.alerts.client), "chats", lambda self: [{"id": 7, "type": "private", "name": "me"}])
    assert client.get("/api/alerts/chats").json() == {"chats": [{"id": 7, "type": "private", "name": "me"}]}


def test_chat_id_falls_back_to_env(keyring, monkeypatch):
    secret_store.set_secret("telegram_bot_token", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "99")
    assert telegram_client(AlertSettings()).chat_id == "99"
    assert telegram_client(AlertSettings(chat_id="5")).chat_id == "5"


def test_llm_key_from_settings(keyring, api):
    _, client = api
    res = client.put("/api/brief", json={"provider": "anthropic", "urls": ["https://example.com"],
                                         "anthropic_api_key": "sk-ant-x"})
    assert res.status_code == 200 and "sk-ant-x" not in res.text
    claude = next(p for p in res.json()["providers"] if p["id"] == "anthropic")
    assert claude["key_set"] is True and claude["key_source"] == "saved"
    assert next(p for p in provider_status() if p["id"] == "openai")["key_set"] is False

    body = client.put("/api/brief", json={"provider": "anthropic", "forget_anthropic_api_key": True}).json()
    assert next(p for p in body["providers"] if p["id"] == "anthropic")["key_set"] is False


def test_terms_must_be_accepted_once_per_version(api):
    runtime, client = api
    body = client.get("/api/terms").json()
    assert body["accepted"] is False and body["version"] == TERMS_VERSION
    assert "not financial advice" in body["text"].lower()
    body = client.post("/api/terms/accept").json()
    assert body["accepted"] is True and body["accepted_at"]
    assert client.get("/api/terms").json()["accepted"] is True

    runtime.store.set_setting("terms", {"version": "older", "accepted_at": "2026-01-01T00:00:00+00:00"})
    assert client.get("/api/terms").json()["accepted"] is False  # new terms: asked again
