"""Signed model files: who made this model, checked with Ed25519.

The manifest's SHA-256 only proves the pickle wasn't changed after the manifest was
written; anyone can rebuild a zip with a new pickle, a matching hash and any author.
A signature closes that gap. The trainer signs the canonical JSON of ``manifest.json``
(which carries the pickle's SHA-256, so the model is covered too) and adds
``signature.json`` to the zip::

    {"key_id": "...", "algorithm": "ed25519", "public_key": "<base64>", "signature": "<base64>"}

On import the file is in one of four states:

* ``trusted``: signed by a key Wednesday trusts (momentum.id ships built in, the user
  can add more);
* ``unknown``: correctly signed, by a key that isn't trusted;
* ``unsigned``: no signature;
* ``invalid``: a signature that doesn't match, or a pickle that doesn't match the
  manifest. Never loaded.

Private keys never go in the repo, the database or ``.env.example``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

from ..plugin_keys import TRUSTED as BUILTIN_KEYS

ALGORITHM = "ed25519"
SIGNATURE_FILE = "signature.json"
TRUST_KEY = "lab_trust"  # settings row: {"keys": [{"name", "public_key"}], "only_signed": bool}
BUILTIN_NAMES = {"momentum-2026": "momentum.id"}


class SigningError(ValueError):
    pass


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def key_id(public: bytes) -> str:
    """Short fingerprint of a raw public key, shown to users."""
    return hashlib.sha256(public).hexdigest()[:16]


def _b64d(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise SigningError("Not valid base64") from exc


def parse_public_key(text: str) -> bytes:
    """A public key as base64 of the 32 raw bytes, or as a PEM block."""
    text = text.strip()
    if text.startswith("-----BEGIN"):
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_public_key(text.encode())
        return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    raw = _b64d(text)
    if len(raw) != 32:
        raise SigningError("An Ed25519 public key is 32 bytes (44 characters of base64)")
    return raw


def trusted_keys(user_keys: list[dict] | None = None) -> dict[bytes, str]:
    """Raw public key -> the name shown for it: the built-in keys, then the user's."""
    out = {base64.b64decode(v): BUILTIN_NAMES.get(k, k) for k, v in BUILTIN_KEYS.items()}
    for k in user_keys or []:
        try:
            out.setdefault(parse_public_key(k["public_key"]), str(k.get("name") or "your key"))
        except (SigningError, ValueError, KeyError):
            continue
    return out


def load_private_key(path: str | Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise SigningError(f"Can't read the private key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("The signing key must be an Ed25519 private key")
    return key


def new_key(path: str | Path) -> str:
    """Write a new private key (PEM, readable by the owner only); returns the public key in base64."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = Path(path)
    if path.exists():
        raise SigningError(f"{path} already exists; pick another path so no key is overwritten")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.write_bytes(pem)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _members(data: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return {n: zf.read(n) for n in zf.namelist()}
    except zipfile.BadZipFile as exc:
        raise SigningError("Not a Wednesday model file (not a zip)") from exc


def sign(data: bytes, private_key) -> bytes:
    """The model file with ``signature.json`` added (or replaced)."""
    from cryptography.hazmat.primitives import serialization

    files = _members(data)
    if "manifest.json" not in files:
        raise SigningError("Not a Wednesday model file (no manifest.json)")
    manifest = json.loads(files["manifest.json"])
    public = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sig = {"key_id": key_id(public), "algorithm": ALGORITHM, "public_key": base64.b64encode(public).decode(),
           "signature": base64.b64encode(private_key.sign(canonical(manifest))).decode()}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if name != SIGNATURE_FILE:
                zf.writestr(name, content)
        zf.writestr(SIGNATURE_FILE, json.dumps(sig, indent=2))
    return buf.getvalue()


def verify(data: bytes, trusted: dict[bytes, str] | None = None) -> dict:
    """{state, key_id, signer, reason} of a model file; see the module docstring for the states."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    trusted = trusted_keys() if trusted is None else trusted
    files = _members(data)
    try:
        manifest = json.loads(files["manifest.json"])
    except (KeyError, ValueError) as exc:
        raise SigningError("Not a Wednesday model file (no readable manifest.json)") from exc
    if hashlib.sha256(files.get("model.pkl", b"")).hexdigest() != manifest.get("sha256"):
        return {"state": "invalid", "key_id": None, "signer": None,
                "reason": "The model doesn't match its manifest (file changed or damaged)"}
    if SIGNATURE_FILE not in files:
        return {"state": "unsigned", "key_id": None, "signer": None, "reason": None}
    try:
        sig = json.loads(files[SIGNATURE_FILE])
        if sig.get("algorithm") != ALGORITHM:
            raise SigningError(f"Unsupported signature algorithm {sig.get('algorithm')!r}")
        public = _b64d(sig["public_key"])
        Ed25519PublicKey.from_public_bytes(public).verify(_b64d(sig["signature"]), canonical(manifest))
    except (InvalidSignature, SigningError, KeyError, ValueError, TypeError) as exc:
        reason = "The signature doesn't match the manifest" if isinstance(exc, InvalidSignature) else str(exc)
        return {"state": "invalid", "key_id": None, "signer": None, "reason": reason or "Bad signature"}
    kid = key_id(public)
    if public in trusted:
        return {"state": "trusted", "key_id": kid, "signer": trusted[public], "reason": None}
    return {"state": "unknown", "key_id": kid, "signer": None, "reason": None}
