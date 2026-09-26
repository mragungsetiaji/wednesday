"""Secrets entered in the dashboard: the MT5 password, the Telegram bot token and LLM API keys.

They are kept in the operating system's credential store (Windows Credential
Manager, macOS Keychain; through ``keyring``), never in the database, logs or
API responses. Without a usable store a secret is kept in memory until the app
closes. The environment (``.env``) still works as a fallback, for VPS setups.
"""

from __future__ import annotations

import os

SERVICE = "Wednesday"

# Secrets set from Settings, with the environment variable each falls back to.
APP_SECRETS = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}

_session: dict[tuple[str, str], str] = {}


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def keyring_get(service: str, account: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(service, account)
    except Exception:  # a broken backend is the same as nothing saved
        return None


def keyring_set(service: str, account: str, value: str) -> bool:
    """Save in the credential store; False when there is none."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(service, account, value)
    except Exception:
        return False
    return True


def keyring_delete(service: str, account: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(service, account)
    except Exception:  # not saved: nothing to forget
        pass


def remember(service: str, account: str, value: str) -> str:
    """Store a secret; returns where it went: "saved" (credential store) or "session" (memory)."""
    if keyring_set(service, account, value):
        _session.pop((service, account), None)
        return "saved"
    _session[(service, account)] = value
    return "session"


def recall(service: str, account: str) -> tuple[str | None, str | None]:
    """The secret and where it came from ("session" or "saved"), or (None, None)."""
    if value := _session.get((service, account)):
        return value, "session"
    if value := keyring_get(service, account):
        return value, "saved"
    return None, None


def forget(service: str, account: str) -> None:
    _session.pop((service, account), None)
    keyring_delete(service, account)


def get_secret(name: str) -> tuple[str | None, str | None]:
    """An app secret and its source: "session", "saved" or "env"; (None, None) when unset."""
    value, source = recall(SERVICE, name)
    if value:
        return value, source
    if value := os.environ.get(APP_SECRETS[name]):
        return value, "env"
    return None, None


def set_secret(name: str, value: str) -> str:
    return remember(SERVICE, name, value)


def forget_secret(name: str) -> None:
    forget(SERVICE, name)


def update_secrets(body: dict, names: list[str]) -> bool:
    """Apply write-only secret fields from a request: ``<name>`` sets it, ``forget_<name>`` removes it.

    Returns whether anything changed.
    """
    changed = False
    for name in names:
        if body.get(f"forget_{name}"):
            forget_secret(name)
            changed = True
        if value := str(body.get(name) or "").strip():
            set_secret(name, value)
            changed = True
    return changed

