#!/usr/bin/env bash
# Publish a release: tag main's latest commit and push the tag. The Release Windows workflow
# (.github/workflows/release-windows.yml) then builds the installer, attaches it to a GitHub
# release and writes the version on main.
#
#   scripts/release.sh v0.3.2      (or: make release v0.3.2)
set -euo pipefail

raw="${1:-}"
if [[ -z "$raw" ]]; then
  echo "Which version? e.g. make release v0.3.2" >&2
  exit 1
fi
version="${raw#[vV]}"
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Not a version: $raw (use v1.2.3)" >&2
  exit 1
fi
tag="v$version"

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" != "main" ]]; then
  echo "On $branch: releases are tagged on main. Run: git switch main" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Uncommitted changes: commit or stash them first, so the release is what's on main." >&2
  exit 1
fi

git fetch --quiet --tags origin main
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
  echo "main differs from origin/main: pull or push first (git pull --rebase / git push)." >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null || git ls-remote --exit-code --tags origin "$tag" >/dev/null 2>&1; then
  echo "$tag already exists. Pick the next version." >&2
  exit 1
fi

# Never move the version backwards.
current="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
newest="$(printf '%s\n%s\n' "${current%%-*}" "$version" | sort -V | tail -1)"
if [[ "$version" == "${current%%-*}" || "$newest" != "$version" ]]; then
  echo "$tag isn't newer than main's $current. Pick a higher version." >&2
  exit 1
fi

echo "Releasing $tag from $(git log -1 --format='%h %s')"
git tag -a "$tag" -m "Wednesday $version"
git push origin "$tag"

repo="$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
echo "Pushed $tag. The installer builds in about 15 minutes:"
echo "  https://github.com/$repo/actions/workflows/release-windows.yml"
echo "  then https://github.com/$repo/releases/tag/$tag"
