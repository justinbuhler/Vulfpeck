#!/usr/bin/env bash
# One-time GitHub setup for a repository that already has an `origin` remote.
#
#   ./scripts/github_bootstrap.sh            print what would change, change nothing
#   ./scripts/github_bootstrap.sh --apply    create the labels and protect main
#
# Two things the harness assumes and git cannot carry:
#   1. The five triage labels named in docs/agents/triage-labels.md.
#   2. origin/main accepts only changes that passed the `tests` job in
#      .github/workflows/tests.yml (AGENTS.md ORCH-12: origin/main is the one
#      integration target, and it is moved by merging a pull request).
#
# Idempotent: labels are created with --force, and branch protection is a PUT.
# Branch protection on a private repository needs a paid plan; when GitHub refuses
# it, this says so and exits non-zero rather than reporting the repo as protected.
set -uo pipefail

say(){ printf '%s\n' "$*"; }
die(){ printf '✗ github_bootstrap: %s\n' "$*" >&2; exit 1; }

APPLY=0
[ "${1:-}" = --apply ] && APPLY=1

command -v gh >/dev/null || die "gh is required: https://cli.github.com/"
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner) \
  || die "no GitHub repository for this checkout; add an origin remote first"
[ -n "$REPO" ] || die "gh returned an empty repository name"

# Literal copy of docs/agents/triage-labels.md. tests/test_github_bootstrap.py
# checks the two agree, so a label renamed in one place fails there.
LABELS=(
  "needs-triage|d4c5f9|Not yet assessed"
  "needs-info|fbca04|Waiting on information from the reporter"
  "ready-for-agent|0e8a16|Specified well enough for an agent to take"
  "ready-for-human|1d76db|Needs a human decision or human work"
  "wontfix|ffffff|Will not be worked on"
)

PROTECTION='{
  "required_status_checks": {"strict": true, "contexts": ["tests"]},
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}'

say "▶ repository: $REPO"
for entry in "${LABELS[@]}"; do
  IFS='|' read -r name color description <<<"$entry"
  if [ "$APPLY" = 1 ]; then
    gh label create "$name" --repo "$REPO" --color "$color" \
      --description "$description" --force >/dev/null \
      || die "could not create label $name"
    say "  label $name: ok"
  else
    say "  would create label $name"
  fi
done

if [ "$APPLY" = 1 ]; then
  out=$(printf '%s' "$PROTECTION" | gh api --method PUT \
        "repos/$REPO/branches/main/protection" --input - 2>&1) \
    || die "branch protection was refused for main:
$out"
  say "  main: protected, requires the tests check"
else
  say "  would protect main: require the tests check, no force-push, no deletion"
  say "▶ dry run; re-run with --apply to make these changes"
fi
