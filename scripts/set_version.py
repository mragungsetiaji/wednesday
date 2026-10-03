"""Set Wednesday's version everywhere it's written, or check they all agree.

    python scripts/set_version.py 0.2.0             # write 0.2.0 into every file below
    python scripts/set_version.py 0.2.0 --if-newer  # only if it's newer than pyproject.toml's
    python scripts/set_version.py --check           # exit 1 when the files disagree

The release workflow runs it for each ``vX.Y.Z`` tag, for the build and to commit the bump to main.
Standard library only, so it runs on a bare CI runner.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.\-]+)?$")

# (file, pattern with the version in group 2, how many times it must match)
PLACES: list[tuple[str, re.Pattern, int]] = [
    ("pyproject.toml", re.compile(r'^(version = ")([^"]+)(")', re.M), 1),
    ("src/wednesday/__init__.py", re.compile(r'^(__version__ = ")([^"]+)(")', re.M), 1),
    # uv.lock: the version line of the project's own package entry
    ("uv.lock", re.compile(r'(\[\[package\]\]\nname = "wednesday"\nversion = ")([^"]+)(")'), 1),
    ("web/package.json", re.compile(r'^(  "version": ")([^"]+)(")', re.M), 1),
    # package-lock.json: the top-level version and the root package's ("" entry)
    ("web/package-lock.json", re.compile(r'^(  "version": ")([^"]+)(")', re.M), 1),
    ("web/package-lock.json", re.compile(r'(\n    "": \{\n      "name": "[^"]+",\n      "version": ")([^"]+)(")'), 1),
]


def current(root: Path = ROOT) -> dict[str, list[str]]:
    """file -> the versions found in it."""
    found: dict[str, list[str]] = {}
    for name, pattern, _ in PLACES:
        found.setdefault(name, []).extend(m.group(2) for m in pattern.finditer((root / name).read_text(encoding="utf-8")))
    return found


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0].split("+")[0])[:3])


def set_version(version: str, root: Path = ROOT) -> list[str]:
    """Write ``version`` everywhere; the files that changed."""
    if not VERSION.match(version):
        raise ValueError(f"Not a version: {version!r} (use 1.2.3)")
    changed = []
    texts: dict[str, str] = {}
    for name, pattern, count in PLACES:
        text = texts.get(name) or (root / name).read_text(encoding="utf-8")
        new, n = pattern.subn(lambda m: m.group(1) + version + m.group(3), text)
        if n != count:
            raise RuntimeError(f"{name}: expected {count} version line, found {n}")
        texts[name] = new
    for name, text in texts.items():
        path = root / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8", newline="\n")
            changed.append(name)
    return changed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("version", nargs="?", help="the new version, e.g. 0.2.0 (a leading v is dropped)")
    p.add_argument("--check", action="store_true", help="only check every file has the same version")
    p.add_argument("--if-newer", action="store_true", help="skip unless the version is newer than pyproject.toml's")
    args = p.parse_args(argv)

    found = current()
    versions = {v for vs in found.values() for v in vs}
    if args.check or not args.version:
        if len(versions) == 1:
            print(f"version {versions.pop()} everywhere")
            return 0
        print("versions disagree: " + ", ".join(f"{f} {vs}" for f, vs in found.items()), file=sys.stderr)
        print("fix with: python scripts/set_version.py <version>", file=sys.stderr)
        return 1

    version = args.version.removeprefix("v")
    now = found["pyproject.toml"][0]
    if args.if_newer and _key(version) <= _key(now):
        print(f"{version} isn't newer than {now}: nothing to do")
        return 0
    changed = set_version(version)
    print(f"version {version}: " + (", ".join(changed) if changed else "already set"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
