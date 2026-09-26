import pytest

from wednesday import secret_store


@pytest.fixture(autouse=True)
def no_credential_store(monkeypatch):
    """Tests never touch the real Keychain / Credential Manager; secrets live in memory per test."""
    monkeypatch.setattr(secret_store, "_keyring", lambda: None)
    monkeypatch.setattr(secret_store, "_session", {})
