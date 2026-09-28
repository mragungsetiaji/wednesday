"""Model files: a zip with ``manifest.json`` (readable without loading anything) and
``model.pkl`` (the trained estimators).

Loading a pickle can run code, so a model file is handled in two steps: the
manifest is read and shown first, and the pickle is only loaded after that, by
an unpickler that accepts nothing but numpy and scikit-learn classes. The
manifest carries the pickle's SHA-256; a file whose pickle doesn't match is
refused. Only import model files from people you trust.
"""

from __future__ import annotations

import hashlib
import io
import json
import pickle
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "wednesday-model"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 200 * 1024 * 1024

# Functions a scikit-learn / numpy pickle needs; classes are allowed by module prefix below.
_ALLOWED_FUNCS = {
    ("numpy", "dtype"),
    ("numpy", "ndarray"),
    ("numpy.core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy.core.numeric", "_frombuffer"),
    ("numpy._core.numeric", "_frombuffer"),
    ("numpy.random._pickle", "__bit_generator_ctor"),
    ("numpy.random._pickle", "__generator_ctor"),
    ("numpy.random._pickle", "__randomstate_ctor"),
    ("numpy.random.bit_generator", "__pyx_unpickle_SeedSequence"),
    ("builtins", "set"),
    ("builtins", "frozenset"),
    ("builtins", "slice"),
    ("builtins", "complex"),
    ("builtins", "bytearray"),
    ("collections", "OrderedDict"),
}
_CLASS_MODULES = ("sklearn.", "numpy.random.", "numpy.dtypes")


class ModelFileError(ValueError):
    pass


class _SafeUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        if (module, name) in _ALLOWED_FUNCS:
            return super().find_class(module, name)
        if module.startswith(_CLASS_MODULES):
            obj = super().find_class(module, name)
            if isinstance(obj, type):
                return obj
        raise ModelFileError(f"The model file wants {module}.{name}, which a Wednesday model never needs; not loading it")


def safe_loads(data: bytes):
    return _SafeUnpickler(io.BytesIO(data)).load()


@dataclass
class ModelBundle:
    manifest: dict
    models: dict = field(default_factory=dict)  # tag -> classifier
    outcome: object | None = None  # order block outcome classifier
    features: list[str] = field(default_factory=list)
    outcome_features: list[str] = field(default_factory=list)
    # Training medians per feature ({"tags": {...}, "outcome": {...}}), for explaining a single call;
    # empty in files from before 0.1.7.
    medians: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.manifest["id"]

    def to_bytes(self) -> bytes:
        payload = pickle.dumps({"models": self.models, "outcome": self.outcome, "features": self.features,
                                "outcome_features": self.outcome_features, "medians": self.medians}, protocol=5)
        manifest = {**self.manifest, "format": FORMAT, "format_version": FORMAT_VERSION,
                    "sha256": hashlib.sha256(payload).hexdigest()}
        self.manifest = manifest
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("model.pkl", payload)
        return buf.getvalue()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self.to_bytes())
        tmp.replace(path)


def read_manifest(data: bytes) -> dict:
    """The manifest of a model file, without touching the pickle."""
    if len(data) > MAX_FILE_BYTES:
        raise ModelFileError("The file is larger than 200 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            if not {"manifest.json", "model.pkl"} <= names:
                raise ModelFileError("Not a Wednesday model file (needs manifest.json and model.pkl)")
            manifest = json.loads(zf.read("manifest.json"))
    except zipfile.BadZipFile as exc:
        raise ModelFileError("Not a Wednesday model file (not a zip)") from exc
    except json.JSONDecodeError as exc:
        raise ModelFileError("The manifest isn't valid JSON") from exc
    if manifest.get("format") != FORMAT:
        raise ModelFileError("Not a Wednesday model file")
    if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
        raise ModelFileError("This model was made by a newer Wednesday; update to load it")
    for key in ("id", "name", "tags", "params", "sha256"):
        if key not in manifest:
            raise ModelFileError(f"The manifest has no {key!r}")
    return manifest


def load_bytes(data: bytes) -> ModelBundle:
    manifest = read_manifest(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        payload = zf.read("model.pkl")
    if hashlib.sha256(payload).hexdigest() != manifest["sha256"]:
        raise ModelFileError("The model doesn't match its manifest (file changed or damaged)")
    try:
        obj = safe_loads(payload)
    except ModelFileError:
        raise
    except Exception as exc:  # noqa: BLE001 - any unpickling problem means a bad file
        raise ModelFileError(f"Can't read the model: {type(exc).__name__}: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("models"), dict):
        raise ModelFileError("The model file has no models in it")
    medians = obj.get("medians")
    return ModelBundle(manifest, obj["models"], obj.get("outcome"), list(obj.get("features") or []),
                       list(obj.get("outcome_features") or []), medians if isinstance(medians, dict) else {})


def load(path: Path) -> ModelBundle:
    return load_bytes(path.read_bytes())
