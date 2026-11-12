#!/usr/bin/env bash
# Bring the upstream project's new commits into this fork's working branch.
#
# Remotes:  origin = the upstream project   fork = your fork
# Branches: main   = a clean mirror of upstream main (fast-forward only)
#           nitish/main = upstream plus your changes (the branch you run and push)
#
# Run from the repo root. When the merge has conflicts it stops and says so:
# fix them, run the tests, then `git commit` and `git push`.
set -euo pipefail

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Commit or stash your changes first." >&2
  exit 1
fi

git fetch origin
git fetch fork

# Keep the fork's main an exact copy of upstream main. Never forced.
git push fork origin/main:refs/heads/main

git switch nitish/main
if git merge --no-edit origin/main; then
  echo
  echo "Merged cleanly. Before pushing, run the tests:  python -m pytest tests -q"
  echo "Then:  git push fork nitish/main"
else
  echo
  echo "Merge conflicts. Resolve them, run the tests, then git commit and git push fork nitish/main."
  echo "In sellbird/state.py, MIGRATIONS is order-sensitive: new upstream migrations and this fork's"
  echo "(antigravity v17, partnerships, research/review) must not end up in a different order."
  exit 1
fi
