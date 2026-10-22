#!/usr/bin/env bash
# Reports every worktree's drift from origin/main and, with --prune, removes
# the ones that are clean and fully landed - zero commits origin/main does not
# already have. That combination means the branch's work already shipped and
# nobody ran `git worktree remove` afterward; it is the one case pruning is
# unambiguously safe. See AGENTS.md ORCH-12. Mirrors land.sh's prune_landers(),
# for feature worktrees rather than land.sh's own lander scratch dirs - so a
# lander worktree (detached HEAD, no branch) is never a candidate here.
set -uo pipefail

say(){ printf '%s\n' "$*"; }
die(){ printf '✗ worktree_status: %s\n' "$*" >&2; exit 1; }

git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository"
git fetch -q origin main 2>/dev/null || say "… could not reach origin; using the last-known origin/main"
git rev-parse -q --verify refs/remotes/origin/main >/dev/null 2>&1 || die "no origin/main to compare against"

PRUNE=0
[ "${1:-}" = --prune ] && PRUNE=1

SELF=$(git rev-parse --show-toplevel)
dead=()

printf '%-70s %-40s %6s %6s %6s  %s\n' PATH BRANCH AHEAD BEHIND DIRTY STATUS
while IFS=$'\t' read -r wt branch; do
  [ -n "$branch" ] || continue                # detached: land.sh's own scratch dirs
  short=${branch#refs/heads/}
  [ "$short" = main ] && continue              # never a prune candidate

  dirty=$(git -C "$wt" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  ahead=$(git -C "$wt" rev-list --count "origin/main..$short" 2>/dev/null || echo '?')
  behind=$(git -C "$wt" rev-list --count "$short..origin/main" 2>/dev/null || echo '?')

  status=""
  if [ "$wt" = "$SELF" ]; then
    status="(current worktree, never a candidate)"
  elif [ "$dirty" = 0 ] && [ "$ahead" = 0 ]; then
    status="STALE - fully landed, safe to remove"
    dead+=("$wt")
  fi
  printf '%-70s %-40s %6s %6s %6s  %s\n' "$wt" "$short" "$ahead" "$behind" "$dirty" "$status"
done < <(git worktree list --porcelain \
         | awk '/^worktree /{wt=substr($0,10)} /^branch /{print wt"\t"substr($0,8)}')

if [ "$PRUNE" != 1 ]; then
  [ "${#dead[@]}" -gt 0 ] \
    && { say ""; say "▶ ${#dead[@]} stale worktree(s). Re-run with --prune to remove them."; }
  exit 0
fi

for wt in "${dead[@]}"; do
  # A short-lived Bash subprocess won't show up here (land.sh's prune_landers
  # has the same gap) - "ahead=0" is the real safety bar, this only catches a
  # human terminal or a long-lived process sitting in the directory.
  if lsof -a -d cwd -- "$wt" >/dev/null 2>&1; then
    say "▶ keeping $wt: a process is working in it"
    continue
  fi
  # No --force: `git worktree remove` refuses on its own if anything is
  # actually dirty, which is the backstop if the check above raced a write.
  if git worktree remove "$wt" 2>/dev/null; then
    say "▶ removed $wt"
  else
    say "▶ could not remove $wt (git refused); skipping"
  fi
done
git worktree prune
