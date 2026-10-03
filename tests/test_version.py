"""One version everywhere (scripts/set_version.py), so a release never ships mismatched numbers."""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("set_version", ROOT / "scripts" / "set_version.py")
sv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sv)


def test_every_file_has_the_same_version():
    found = sv.current()
    assert {v for vs in found.values() for v in vs} == {found["pyproject.toml"][0]}, found
    from wednesday import __version__
    assert __version__ == found["pyproject.toml"][0]


@pytest.fixture
def copy(tmp_path):
    for name in {n for n, _, _ in sv.PLACES}:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def test_set_version_writes_every_place_and_nothing_else(copy):
    before = {n: (copy / n).read_text() for n, _, _ in sv.PLACES}
    changed = sv.set_version("9.8.7", copy)
    assert set(changed) == set(before)
    assert {v for vs in sv.current(copy).values() for v in vs} == {"9.8.7"}
    for name, text in before.items():  # only the version lines moved
        old, new = text.splitlines(), (copy / name).read_text().splitlines()
        assert len(old) == len(new) and sum(a != b for a, b in zip(old, new)) == (2 if name.endswith("lock.json") else 1)
    assert sv.set_version("9.8.7", copy) == []  # already set


def test_bad_versions_are_refused(copy):
    for bad in ("latest", "1.2", "v1.2.3; rm -rf"):
        with pytest.raises(ValueError):
            sv.set_version(bad, copy)
    sv.set_version("1.2.3-dev.45", copy)  # the workflow's test builds
