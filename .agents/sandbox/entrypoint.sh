#!/bin/bash
# Give the worker a private git dir over the mounted worktree: one baseline commit of the
# current tree, so `git status`, `git diff` and `git commit` work. The commits are discarded
# when the container exits; the runner reads the files, not this repository.
set -eu
git init -q "$GIT_DIR"
git config user.email worker@sandbox.invalid
git config user.name worker
git add -A
git commit -q --allow-empty -m "sandbox baseline"
exec "$@"
