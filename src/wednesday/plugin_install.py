"""Download a paid plugin (Wednesday EE) from the licence server, check it's genuine, unpack it.

Wednesday EE isn't published: whoever has a licence key gets it from the licence
server. ``POST /v1/download`` with the key answers::

    {"kid": "momentum-2026", "manifest": "<base64url JSON>", "signature": "<base64url>",
     "wheel": "<base64 of the .whl>"}

The manifest names the package, its version, the wheel's file name and SHA-256; the
signature is Ed25519 over ``WEDPKG1.<manifest>`` with a key listed in
``plugin_keys.py``. Only a wheel that matches a signed manifest is unpacked, into
``data/plugins/<package>/``, which ``plugins.py`` puts on ``sys.path``. Nothing
downloaded runs before that check.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

PREFIX = "WEDPKG1"
DEFAULT_SERVER = "http://127.0.0.1:8790"
SERVER_ENV = "WEDNESDAY_LICENCE_SERVER"
PLUGINS_DIR_ENV = "WEDNESDAY_PLUGINS_DIR"
DEVICE_SETTING = "ee_device"  # shared with Wednesday EE's licence, which binds tokens to it
ALLOWED = {"wednesday_ee"}  # packages Wednesday will download and run
MAX_BYTES = 50 * 1024 * 1024


class InstallError(ValueError):
    """The plugin couldn't be downloaded or isn't genuine; the message is for the user."""


def plugins_dir() -> Path:
    return Path(os.environ.get(PLUGINS_DIR_ENV) or "data/plugins")


def installed_dirs() -> list[Path]:
    root = plugins_dir()
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")) if root.is_dir() else []


def device_id(store) -> str:
    """This install's id, kept in the database, made on first use."""
    stored = store.get_setting(DEVICE_SETTING) if store else None
    if stored and stored.get("id"):
        return str(stored["id"])
    device = uuid.uuid4().hex
    if store:
        store.set_setting(DEVICE_SETTING, {"id": device})
    return device


def server_url() -> str:
    return (os.environ.get(SERVER_ENV) or DEFAULT_SERVER).rstrip("/")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def fetch(key: str, device: str, url: str | None = None, timeout: float = 60) -> dict:
    url = url or server_url()
    body = json.dumps({"key": key, "device_id": device}).encode()
    req = urllib.request.Request(f"{url}/v1/download", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read(MAX_BYTES * 2))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise InstallError(str(detail or f"The licence server refused the download ({exc.code})")) from exc
    except (OSError, ValueError) as exc:
        raise InstallError(f"Can't reach the licence server at {url} to download Wednesday EE") from exc


def verify(answer: dict, keys: dict[str, bytes] | None = None) -> tuple[dict, bytes]:
    """(manifest, wheel bytes) when the signature and hash check out; InstallError otherwise."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if keys is None:
        from .plugin_keys import TRUSTED

        keys = {kid: base64.b64decode(b64) for kid, b64 in TRUSTED.items()}
    try:
        manifest_b64, signature = str(answer["manifest"]), _b64d(str(answer["signature"]))
        manifest = json.loads(_b64d(manifest_b64))
        wheel = base64.b64decode(str(answer["wheel"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("The download is damaged; try again") from exc
    public = keys.get(str(manifest.get("kid", "")))
    if public is None:
        raise InstallError("The download was signed with a key this Wednesday doesn't know; update Wednesday")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, f"{PREFIX}.{manifest_b64}".encode())
    except InvalidSignature as exc:
        raise InstallError("The download isn't genuine (its signature doesn't match)") from exc
    if hashlib.sha256(wheel).hexdigest() != manifest.get("sha256"):
        raise InstallError("The download doesn't match its signature; try again")
    if manifest.get("package") not in ALLOWED:
        raise InstallError(f"Wednesday doesn't install {manifest.get('package')!r}")
    return manifest, wheel


def unpack(manifest: dict, wheel: bytes, root: Path | None = None) -> Path:
    """Unpack a verified wheel to ``<root>/<package>/``, replacing an older copy whole."""
    root = root or plugins_dir()
    package = manifest["package"]
    if not re.fullmatch(r"[a-z_]+", package):
        raise InstallError("Bad package name")
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".download-", dir=root))
    try:
        with zipfile.ZipFile(BytesIO(wheel)) as zf:
            for name in zf.namelist():
                target = (tmp / name).resolve()
                if name.startswith(("/", "\\")) or not target.is_relative_to(tmp.resolve()):
                    raise InstallError("The download has a file outside its folder")
            zf.extractall(tmp)
        (tmp / ".wednesday-plugin.json").write_text(json.dumps(manifest, indent=2))
        dest = root / package
        old = root / f".old-{package}"
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            dest.rename(old)
        tmp.rename(dest)
        shutil.rmtree(old, ignore_errors=True)
        return dest
    except zipfile.BadZipFile as exc:
        raise InstallError("The download is damaged; try again") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def install(key: str, store, url: str | None = None, root: Path | None = None) -> dict:
    """Download, check and unpack the plugin a licence key gives; returns its manifest."""
    manifest, wheel = verify(fetch(key, device_id(store), url))
    unpack(manifest, wheel, root)
    return manifest
