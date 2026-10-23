#!/usr/bin/env bash
# The only sanctioned path to LOCAL `main`; it never pushes - see AGENTS.md ORCH-12.
#
#   reset a per-branch lander worktree to main -> merge --no-ff -> gate the
#   MERGED tree -> take a short lock -> compare-and-swap refs/heads/main ->
#   verify -> release.
#
# The gate runs OUTSIDE the lock: it is the expensive step (tens of minutes on
# this repo), so serialising it would cap the whole repository at a handful of
# lands per hour. Correctness does not depend on the lock - `update-ref` with an
# expected old value is a compare-and-swap, and a land that loses it re-merges
# onto the new main and re-gates. The lock only keeps the read-check-swap-verify
# section indivisible, so a land that succeeds is never reported as a failure
# because someone else landed a millisecond later.
#
# The lander worktree is per-branch and persistent: it keeps its .venv and its
# copied inputs between lands, which is worth more than the full checkout and
# `uv sync` it would otherwise repeat every time.
#
# Exits 0 only after re-reading main and proving the reviewed candidate is
# reachable from it. Anything else exits non-zero without landing; it may already
# have pruned landers and synced with origin, which happen before the gate.
set -uo pipefail

# git reads these from the environment and ignores the directory it is run from.
# Inheriting them - from a hook, or from a caller working around something - would
# point every git command below at another repository while the paths still look
# local. The lander must act on the worktree it was invoked in, and nothing else.
unset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX GIT_OBJECT_DIRECTORY \
      GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE \
      GIT_CEILING_DIRECTORIES

say(){ printf '%s\n' "$*"; }
# Record the outcome in the harness decision log (scripts/harness_events.py).
# Never fails the land: the outcome is decided before it is recorded.
event(){
  python3 "$(dirname "$0")/harness_events.py" record --hook land \
    --decision "$1" --reason "$2" --subject "${BRANCH:-?}@${CANDIDATE:-?}${3:+ -> $3}" \
    >/dev/null || true
}
die(){
  printf '✗ land: %s\n' "$*" >&2
  event refused "$(printf '%s' "$1" | head -1 | cut -c1-60)"
  exit 1
}

# --expect SHA: the commit the review approved. A reviewed candidate must land
# exactly as reviewed; a tip that moved after the review (a later commit, or a
# reviewer's own) is refused rather than landed under the review's approval.
EXPECT=${LAND_EXPECT:-}
while [ $# -gt 0 ]; do
  case $1 in
    --expect) [ $# -ge 2 ] || die "--expect needs a commit"; EXPECT=$2; shift 2 ;;
    *) die "unknown argument: $1 (usage: land.sh [--expect <reviewed sha>])" ;;
  esac
done

git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository"
COMMON=$(cd "$(git rev-parse --git-common-dir)" && pwd) || die "cannot resolve the shared git dir"
MAIN=$(dirname "$COMMON")

BRANCH=$(git symbolic-ref --quiet --short HEAD) \
  || die "detached HEAD is not a landable candidate; check out your branch first"
[ "$BRANCH" = main ] && die "refusing to land main onto itself; run this from an agent branch"

if ! git diff --quiet || ! git diff --cached --quiet; then
  die "working tree is dirty; commit or discard before landing"
fi
# Untracked files are never part of the candidate, so this guards a human
# expectation ("I forgot to git add it"), not correctness. It has to name the
# files and be overridable - tool droppings appear in every worktree, and a
# guard that always fires is a guard everyone learns to bypass.
untracked=$(git ls-files --others --exclude-standard)
if [ -n "$untracked" ] && [ "${LAND_ALLOW_UNTRACKED:-0}" != 1 ]; then
  die "untracked files present - these will NOT be included in the land:
$(printf '%s\n' "$untracked" | sed 's/^/         /')
       commit them, add them to .gitignore, or re-run with LAND_ALLOW_UNTRACKED=1
       if they are unrelated to this candidate"
fi
[ -n "$untracked" ] && say "⚠ landing with $(printf '%s\n' "$untracked" | wc -l | tr -d ' ') untracked file(s) excluded (LAND_ALLOW_UNTRACKED=1)"

# `GIT_DIR=... GIT_WORK_TREE=... git init` - the pair a pre-commit hook exports -
# writes core.worktree into the SHARED config, aiming every worktree in this
# repository at one directory. Reproduced 2026-09-02. Unlike core.bare, which
# makes every command fail loudly, this one fails silently: commands succeed
# against the wrong tree and nothing reports it. The lander merges and moves
# refs, so a land under it writes into whatever directory that value names.
poisoned=$(git config --file "$COMMON/config" --get core.worktree 2>/dev/null || true)
if [ -n "$poisoned" ]; then
  die "the shared config sets core.worktree = $poisoned
       Every worktree in this repository is aimed at that directory, and no git
       command will tell you so. Refusing to land until it is cleared:
         git config --file '$COMMON/config' --unset core.worktree"
fi

git rev-parse --verify -q main >/dev/null || die "no local main to land onto"
CANDIDATE=$(git rev-parse HEAD)
if [ -n "$EXPECT" ]; then
  reviewed=$(git rev-parse --verify -q "$EXPECT^{commit}") || die "--expect $EXPECT is not a commit"
  [ "$reviewed" = "$CANDIDATE" ] || die "the candidate moved since review: $BRANCH is at $CANDIDATE, the review approved $reviewed
       land the reviewed commit, or have the new commits reviewed"
fi

# The candidate's harness.toml, read by the reader beside this script.
. "$(dirname "$0")/harness_env.sh"
harness_load "$(git rev-parse --show-toplevel)" || die "harness.toml could not be read"

SLUG=$(printf '%s' "$BRANCH" | tr -c 'A-Za-z0-9._-' '-')
# NOT under $COMMON: the lander worktree would then sit inside `.git/`, and any
# test that indexes the tree while skipping "/.git/" paths sees an empty index.
# Keep this path clear of "/.git/", "/output/", "/.venv/" and "/worktrees/".
LWT="${LAND_WORKTREE_ROOT:-$HOME/.cache/${HARNESS_NAME:-$(basename "$MAIN")}-land}/$SLUG"
LOCK="$COMMON/land.lock"
ATTEMPTS=${LAND_ATTEMPTS:-3}
# scripts/gate.sh beside this script: setup, lint and suite from harness.toml,
# the same gate CI and the pre-commit hook run.
GATE=${LAND_GATE:-$(printf '%q' "$(cd "$(dirname "$0")" && pwd)/gate.sh")}

LOCK_HELD=0
trap '[ "$LOCK_HELD" = 1 ] && rm -rf "$LOCK"' EXIT

lock(){
  waited=0
  until mkdir "$LOCK" 2>/dev/null; do
    [ "$waited" -ge "${LAND_LOCK_WAIT_SECS:-300}" ] && die "timed out waiting for the land lock.
       holder: $(cat "$LOCK/owner" 2>/dev/null || echo unknown)
       if that process is gone, clear it with: rmdir '$LOCK'"
    # ponytail: no stale-lock reaper. Held for milliseconds now, so a stuck lock
    # means a crash, and one rmdir clears it - safer than stealing from a live PID.
    sleep 1; waited=$((waited + 1))
  done
  printf 'pid=%s branch=%s at=%s\n' "$$" "$BRANCH" "$(date -u +%FT%TZ)" >"$LOCK/owner"
  LOCK_HELD=1
}
unlock(){ rm -rf "$LOCK"; LOCK_HELD=0; }

# The gate may need files git does not carry: generated data, downloaded inputs.
# harness.toml land.copy_untracked names them, as paths relative to the
# repository, and each is copied from the main checkout into the lander.
#
# A COPY, never a link: a write in the gate must not reach the main checkout
# through a shared inode or a symlink. And never over a file the candidate
# tracks: the copy comes from the main CHECKOUT, so an uncommitted edit there
# would otherwise enter the gate. Tracked files are excluded per file, not per
# directory, because a directory can hold both kinds.
#
# Nothing is copied unless it is listed. Guessing at what a suite needs copied
# another session's live state into the gate.
provision(){
  [ "${#HARNESS_COPY_UNTRACKED[@]}" -gt 0 ] || return 0
  n=0
  TRACKED=$(mktemp "${TMPDIR:-/tmp}/harness-land-tracked.XXXXXX")
  trap 'rm -f "$TRACKED"; [ "$LOCK_HELD" = 1 ] && rm -rf "$LOCK"' EXIT
  for rel in "${HARNESS_COPY_UNTRACKED[@]}"; do
    src="$MAIN/$rel"
    if [ ! -e "$src" ]; then
      say "⚠ land.copy_untracked lists $rel, which is absent from $MAIN; not copied"
      continue
    fi
    if [ -d "$src" ]; then
      mkdir -p "$LWT/$rel"
      # Anchored ("/name") so a tracked file excludes only itself, not every
      # file of the same name deeper in the tree.
      git -C "$LWT" -c core.quotePath=false ls-files -- "$rel" \
        | sed "s|^$rel/|/|" >"$TRACKED"
      rsync -a --exclude-from="$TRACKED" "$src/" "$LWT/$rel/" \
        || die "could not copy $rel into the lander worktree"
    else
      if git -C "$LWT" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
        say "⚠ land.copy_untracked lists $rel, which the candidate tracks; not copied"
        continue
      fi
      mkdir -p "$LWT/$(dirname "$rel")"
      cp "$src" "$LWT/$rel" || die "could not copy $rel into the lander worktree"
    fi
    n=$((n + 1))
  done
  say "▶ copied $n untracked input(s) from $MAIN"
  return 0
}

# Move local main to $1, keeping whatever worktree holds it in step. $2, when
# given, is the expected old value for the update-ref path's compare-and-swap;
# the merge path is its own swap, because a fast-forward only applies when the
# current tip is an ancestor of the target.
main_worktree(){
  git worktree list --porcelain \
    | awk '/^worktree /{w=substr($0,10)} /^branch refs\/heads\/main$/{print w; exit}'
}

swap_main(){
  MAIN_WT=$(main_worktree)
  if [ -n "$MAIN_WT" ]; then
    swap_err=$(git -C "$MAIN_WT" merge --ff-only "$1" 2>&1)
  elif [ -n "${2:-}" ]; then
    swap_err=$(git update-ref refs/heads/main "$1" "$2" 2>&1)
  else
    swap_err=$(git update-ref refs/heads/main "$1" 2>&1)
  fi
}

# Two trunks exist: this gates local refs/heads/main, while origin/main is what
# everyone clones and what the merge path writes. Nothing reconciled them and
# they were observed 57 commits apart. Landing onto a base that is behind origin
# produces commits that can never converge. Being AHEAD of origin is fine - the
# push path carries those - so only "behind" is corrected here, and a genuine
# divergence is refused rather than guessed at.
sync_with_origin(){
  git rev-parse --verify -q refs/remotes/origin/main >/dev/null 2>&1 || return 0
  git fetch -q origin main 2>/dev/null \
    || say "… could not reach origin; landing against the local ref only"
  om=$(git rev-parse -q --verify refs/remotes/origin/main 2>/dev/null) || return 0
  lm=$(git rev-parse main)
  [ "$om" = "$lm" ] && return 0
  git merge-base --is-ancestor "$om" "$lm" && return 0        # local ahead: fine
  if git merge-base --is-ancestor "$lm" "$om"; then
    swap_main "$om"
    [ "$(git rev-parse main)" = "$om" ] \
      || die "could not fast-forward local main to origin/main: $swap_err"
    say "▶ fast-forwarded local main to origin/main ($(git rev-parse --short "$lm") -> $(git rev-parse --short "$om"))"
    return 0
  fi
  # Two-way divergence is the steady state here, not an incident: local main
  # takes lands while origin/main takes merges of the same work, so both move
  # independently and neither is wrong. Refusing meant the lander blocked almost
  # every time and a human reconciled by hand - and a hand-rolled reconciliation
  # is what silently reverted ten paths of origin's work on 2026-09-02, by
  # recording origin as a parent while keeping only the local tree.
  #
  # So merge, but verify CONTENT before moving the ref: a merge that "succeeds"
  # while discarding one side is the failure this replaces.
  say "▶ main and origin/main diverged ($(git rev-list --count "$om..$lm") local, $(git rev-list --count "$lm..$om") origin); merging origin/main"
  mw=$(main_worktree)
  if [ -n "$mw" ]; then
    # --no-verify: pre-merge-commit blocks merge commits on main, and this is the
    # lander - the one path ORCH-12 sanctions to move it.
    merr=$(git -C "$mw" -c user.name=land -c user.email=land@local \
           merge --no-verify --no-edit origin/main 2>&1) \
      || { git -C "$mw" merge --abort >/dev/null 2>&1
           die "merging origin/main into main conflicts; main is unchanged:
$(printf '%s\n' "$merr" | sed 's/^/       /')"; }
    merged_main=$(git rev-parse main)
  else
    mtmp=$(mktemp -d)/wt
    git worktree add -q --detach "$mtmp" main \
      || die "could not create a worktree to merge origin/main in"
    if ! merr=$(git -C "$mtmp" -c user.name=land -c user.email=land@local \
                merge --no-edit origin/main 2>&1); then
      git worktree remove --force "$mtmp" >/dev/null 2>&1
      die "merging origin/main into main conflicts; main is unchanged:
$(printf '%s\n' "$merr" | sed 's/^/       /')"
    fi
    merged_main=$(git -C "$mtmp" rev-parse HEAD)
    git worktree remove --force "$mtmp" >/dev/null 2>&1
    git worktree prune
  fi
  # Both must hold before the ref moves. Ancestry alone is not enough - the bad
  # reconciliation satisfied it while carrying the wrong tree.
  git merge-base --is-ancestor "$om" "$merged_main" \
    || die "the merge does not contain origin/main; refusing to move main"
  git merge-base --is-ancestor "$lm" "$merged_main" \
    || die "the merge does not contain the previous main; refusing to move main"
  if [ "$(git rev-parse main)" != "$merged_main" ]; then
    git update-ref refs/heads/main "$merged_main" "$lm" \
      || die "main moved while merging origin/main; re-run"
  fi
  say "▶ merged origin/main into main ($(git rev-parse --short "$lm") -> $(git rev-parse --short main))"
}

# Lander worktrees are per-branch and persistent, so one accumulates for every
# branch ever landed - roughly 1.3 GB of real disk each, almost all of it the
# output/ copy - and nothing reclaimed them. Four had piled up before this
# existed. A worktree whose branch is gone can never be reused, which makes it
# the precise signal; age is not, because an idle branch may be landed tomorrow.
# Directory names are slugs, and a slug is not reversible to a branch name, so
# compare against the slugs of the branches that currently exist.
prune_landers(){
  parent=$(dirname "$LWT")
  [ -d "$parent" ] || return 0
  live=$(git for-each-ref --format='%(refname:short)' refs/heads \
         | tr -c 'A-Za-z0-9._\n-' '-')
  pruned=0
  for stale in "$parent"/*/; do
    [ -d "$stale" ] || continue
    name=$(basename "$stale")
    printf '%s\n' "$live" | grep -qxF "$name" && continue
    # The branch being gone makes it eligible; it does not make it idle. A
    # control run or a debug session can still be live in there, and deleting
    # the tree under it does not raise - the run continues against a vanishing
    # directory and reports a plausible wrong answer, which reads as a defect in
    # whatever is being tested rather than a lost workspace. ~350ms per stale
    # directory, once per land, against a gate measured in minutes.
    if lsof -a -d cwd -- "${stale%/}" >/dev/null 2>&1; then
      say "▶ keeping $name: a process is working in it despite its branch being gone"
      continue
    fi
    git worktree remove --force "${stale%/}" >/dev/null 2>&1 || rm -rf "${stale%/}"
    say "▶ pruned lander worktree $name (its branch no longer exists)"
    pruned=$((pruned + 1))
  done
  [ "$pruned" -gt 0 ] && git worktree prune
  return 0
}
prune_landers
sync_with_origin

if git -C "$LWT" rev-parse --git-dir >/dev/null 2>&1; then
  say "▶ reusing lander worktree $LWT"
else
  rm -rf "$LWT"
  git worktree prune
  mkdir -p "$(dirname "$LWT")"
  git worktree add --detach "$LWT" "$CANDIDATE" >/dev/null 2>&1 \
    || die "could not create the lander worktree at $LWT"
  say "▶ created lander worktree $LWT"
fi

attempt=1
while :; do
  BASE=$(git rev-parse main)
  export LAND_BASE="$BASE"   # exported for a gate step that diffs against the main this attempt merges onto
  say "▶ attempt $attempt/$ATTEMPTS: landing $BRANCH ($CANDIDATE) onto main ($BASE)"

  git -C "$LWT" merge --abort >/dev/null 2>&1
  git -C "$LWT" reset -q --hard "$BASE" || die "could not reset the lander worktree to $BASE"
  git -C "$LWT" clean -qfd
  provision

  if ! git -C "$LWT" -c user.name=land -c user.email=land@local \
       merge --no-ff --no-edit -m "land: $BRANCH" "$CANDIDATE" >/dev/null 2>&1; then
    conflicts=$(git -C "$LWT" diff --name-only --diff-filter=U | sed 's/^/         /')
    git -C "$LWT" merge --abort >/dev/null 2>&1
    die "merge conflict landing '$BRANCH' onto main. main is unchanged.
       conflicting paths:
$conflicts
       merge main into your branch, resolve, re-run your gate, then land again"
  fi

  # `merge --no-ff` creates nothing and reports "Already up to date" when the
  # candidate is already reachable from main. Everything below then succeeds on
  # an unchanged tree - the swap is a no-op fast-forward, all three verifications
  # pass - and the receipt says LANDED for work that did not happen. Exit 0,
  # because re-landing is idempotent by contract (tests/test_land.py:140), but
  # say which of the two happened. Placed before the gate: the merged tree IS
  # main, so there is nothing new to test and tens of minutes to save.
  if [ "$(git -C "$LWT" rev-parse HEAD)" = "$BASE" ]; then
    say "✓ already landed candidate=$CANDIDATE main=$BASE (main did not move)"
    event noop already-landed "$BASE"
    exit 0
  fi

  # Every prior gate result is stale: the pre-commit hook tested each of your
  # commits against the OLD main, and git runs pre-merge-commit rather than
  # pre-commit for the merge above. Nobody has tested this tree. No lock held.
  say "▶ gate on the merged tree: $GATE"
  ( cd "$LWT" && env -u VERIFY_LINT_ONLY -u VERIFY_FAST bash -c "$GATE" ) \
    || die "the gate failed on the merged tree; main is unchanged"

  MERGED=$(git -C "$LWT" rev-parse HEAD)

  # --- critical section: read, swap, and verify indivisibly ------------------
  lock
  # `main` is normally checked out in the main working tree, and moving a
  # checked-out ref with update-ref leaves that tree's index and work tree still
  # describing the old commit - so every landed change reads there as an
  # uncommitted reversal. git refuses exactly this for `git branch -f`;
  # update-ref bypasses the safety, which is how 68 phantom paths appeared.
  #
  # A fast-forward merge run INSIDE that checkout is git's own primitive for the
  # job: it moves ref, index and work tree together, and refuses rather than
  # discard a local modification. It remains a compare-and-swap, because it only
  # fast-forwards when the current tip is an ancestor of $MERGED - a peer who
  # landed first makes it fail, and we re-merge onto their result.
  #
  # When main is checked out nowhere there is no tree to keep in step, and
  # update-ref with an expected old value is the equivalent swap.
  swap_main "$MERGED" "$BASE"; swapped=$?
  if [ "$swapped" = 0 ]; then
    # A land is not landed until main has been re-read. Three independent reads,
    # all inside the lock so a peer's next land cannot make a success look false.
    [ "$(git rev-parse main)" = "$MERGED" ] \
      || die "main reads $(git rev-parse main), expected $MERGED"
    git log main --oneline --no-abbrev-commit -1 | grep -q "$MERGED" \
      || die "$MERGED is not the tip of git log main"
    git merge-base --is-ancestor "$CANDIDATE" main \
      || die "the reviewed candidate $CANDIDATE is NOT reachable from main"
    unlock
    event landed "attempts=$attempt" "$MERGED"
    say "✓ LANDED candidate=$CANDIDATE base=$BASE main=$MERGED attempts=$attempt"
    # The decisions this land touched, found through each ADR's Components.
    # Informational: coverage was already enforced by the gate (tests/test_adr.py).
    # In the lander worktree: it holds the merged tree main now points at,
    # while main itself is often only a ref, checked out nowhere.
    if [ -f "$LWT/scripts/adr.py" ]; then
      (cd "$LWT" && "${HARNESS_PY[@]}" scripts/adr.py touched "$BASE" "$MERGED" 2>/dev/null \
        | sed 's/^/  /') || true
    fi
    exit 0
  fi
  moved=$(git rev-parse main)
  unlock

  # A swap can fail for two different reasons and they need different advice.
  if [ "$moved" = "$BASE" ]; then
    die "the swap was refused although main has not moved. This is usually a local
       modification in $MAIN_WT to a file this land updates - git will not
       discard it. Commit or stash it there, then re-run.
       git said: $swap_err"
  fi
  attempt=$((attempt + 1))
  [ "$attempt" -gt "$ATTEMPTS" ] \
    && die "main moved under all $ATTEMPTS attempts (now $moved); nothing was written - re-run"
  say "… main moved ($BASE -> $moved); re-merging and re-gating"
done
