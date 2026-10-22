#!/usr/bin/env bash
# The only sanctioned path to LOCAL `main`; it never pushes - see AGENTS.md ORCH-12.
#
#   reset a per-branch lander worktree to main -> merge --no-ff -> gate the
#   MERGED tree -> take a short lock -> compare-and-swap refs/heads/main ->
#   verify -> release.
#
# A descriptor-held common-Git verification mutex spans scratch preparation,
# the gate, and the final update. Mutation and verification callers share it.
# The separate integration mutex covers the final read-check-update section;
# compare-and-swap retries still detect unrelated Git writers that ignore locks.
#
# The lander worktree is per-branch and persistent: it keeps its .venv and its
# copied inputs between lands, which is worth more than the full checkout and
# `uv sync` it would otherwise repeat every time.
#
# Exits 0 only after re-reading main and proving the reviewed candidate is
# reachable from it. Anything else exits non-zero and writes nothing.
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
land_receipt(){
  [ -n "${CONTROL:-}" ] && [ -n "${CALLER:-}" ] || return 0
  python3 "$CONTROL/harness_receipts.py" record-land --root "$CALLER" --candidate "${CANDIDATE:-unknown}" \
    --receipt "${RECEIPT:-}" --base "${BASE:-}" --origin "${ORIGIN:-}" --merged "${MERGED:-}" \
    --status "$1" --reason "$2" ${LAND_GATE_LOG:+--gate-log "$LAND_GATE_LOG"} \
    || say "land evidence could not be recorded"
}
die(){
  land_receipt REFUSED "$*"
  printf '✗ land: %s\n' "$*" >&2
  event refused "$(printf '%s' "$1" | head -1 | cut -c1-60)"
  exit 1
}

# --expect SHA: the commit the review approved. A reviewed candidate must land
# exactly as reviewed; a tip that moved after the review (a later commit, or a
# reviewer's own) is refused rather than landed under the review's approval.
EXPECT=${LAND_EXPECT:-}
RECEIPT=${LAND_RECEIPT:-}
while [ $# -gt 0 ]; do
  case $1 in
    --receipt) [ $# -ge 2 ] || die "--receipt needs a registered receipt"; RECEIPT=$2; shift 2 ;;
    --expect) [ $# -ge 2 ] || die "--expect needs a commit"; EXPECT=$2; shift 2 ;;
    *) die "unknown argument: $1 (usage: land.sh --expect <reviewed sha> --receipt <registered receipt>)" ;;
  esac
done

git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository"
COMMON=$(cd "$(git rev-parse --git-common-dir)" && pwd) || die "cannot resolve the shared git dir"
MAIN=$(dirname "$COMMON")
CALLER=$(git rev-parse --show-toplevel) || die "cannot resolve the candidate checkout"

CONTROL=$(cd "$(dirname "$0")" && pwd)
LOCK_TOOL="$CONTROL/harness_lock.py"
if ! python3 "$LOCK_TOOL" --root "$CALLER" --name verification --check-held; then
  exec python3 "$LOCK_TOOL" --root "$CALLER" --name verification -- bash "$0" --expect "$EXPECT" --receipt "$RECEIPT"
fi

prepare_codegraph(){
  [ -d "$MAIN/.codegraph" ] || [ -d "$1/.codegraph" ] || return 0
  cg="$CALLER/scripts/codegraph_freshness.py"
  [ "$(basename "$(dirname "$CONTROL")")" != .vulfpeck ] || cg="$CONTROL/codegraph_freshness.py"
  uv run --no-sync python3 "$cg" --ensure --root "$1" \
    || return 1
}

cleanup_codegraph(){
  [ -d "$1/.codegraph" ] || return 0
  cg="$CALLER/scripts/codegraph_freshness.py"
  [ "$(basename "$(dirname "$CONTROL")")" != .vulfpeck ] || cg="$CONTROL/codegraph_freshness.py"
  if ! uv run --no-sync python3 "$cg" --cleanup --root "$1"; then
    say "integration succeeded; CodeGraph cleanup deferred, local index retained at $1"
  fi
}

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
[ -n "$EXPECT" ] || die "--expect is required"
[ -n "$RECEIPT" ] || die "--receipt is required"
if [ -n "$EXPECT" ]; then
  reviewed=$(git rev-parse --verify -q "$EXPECT^{commit}") || die "--expect $EXPECT is not a commit"
  [ "$reviewed" = "$CANDIDATE" ] || die "the candidate moved since review: $BRANCH is at $CANDIDATE, the review approved $reviewed
       land the reviewed commit, or have the new commits reviewed"
fi

python3 "$CONTROL/harness_receipts.py" validate --root "$CALLER" --candidate "$CANDIDATE" --receipt "$RECEIPT" \
  || die "independent candidate evidence was refused"

# The candidate's harness.toml, read by the reader beside this script.
. "$(dirname "$0")/harness_env.sh"
harness_load "$(git rev-parse --show-toplevel)" || die "harness.toml could not be read"

SLUG=$(printf '%s' "$BRANCH" | tr -c 'A-Za-z0-9._-' '-')
# NOT under $COMMON: the lander worktree would then sit inside `.git/`, and any
# test that indexes the tree while skipping "/.git/" paths sees an empty index.
# Keep this path clear of "/.git/", "/output/", "/.venv/" and "/worktrees/".
LWT="${LAND_WORKTREE_ROOT:-$HOME/.cache/${HARNESS_NAME:-$(basename "$MAIN")}-land}/$SLUG"
LOCK="$COMMON/harness/locks/integration.lock"
mkdir -p "$(dirname "$LOCK")"
exec 9>"$LOCK"
ATTEMPTS=${LAND_ATTEMPTS:-3}
# scripts/gate.sh beside this script: setup, lint and suite from harness.toml,
# the same gate CI and the pre-commit hook run.
GATE=${LAND_GATE:-$(printf '%q' "$(cd "$(dirname "$0")" && pwd)/gate.sh")}

LOCK_HELD=0
lock(){
  python3 "$LOCK_TOOL" --root "$CALLER" --name integration --acquire-fd 9 || die "integration mutex failed"
  LOCK_HELD=1
}
unlock(){
  exec 9>&-
  exec 9>"$LOCK"
  LOCK_HELD=0
}

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
  python3 "$CONTROL/harness_inputs.py" --source "$MAIN" --target "$LWT" --config-root "$CALLER" \
    > "$COMMON/harness/land-inputs-$SLUG.json" || die "explicit input provisioning failed"
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
  ORIGIN=""
  git rev-parse --verify -q refs/remotes/origin/main >/dev/null 2>&1 || return 0
  git fetch -q origin main || say "… could not reach origin; using the recorded origin ref"
  ORIGIN=$(git rev-parse --verify refs/remotes/origin/main) || die "cannot resolve origin/main"
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

if git -C "$LWT" rev-parse --git-dir >/dev/null 2>&1; then
  say "▶ reusing lander worktree $LWT"
else
  rm -rf "$LWT"
  git worktree prune
  mkdir -p "$(dirname "$LWT")"
  prepare_codegraph "$MAIN" || die "CodeGraph is not ready at $MAIN; refusing to continue"
  git worktree add --detach "$LWT" "$CANDIDATE" >/dev/null 2>&1 \
    || die "could not create the lander worktree at $LWT"
  say "▶ created lander worktree $LWT"
fi

attempt=1
while :; do
  sync_with_origin
  BASE=$(git rev-parse main)
  export LAND_BASE="$BASE"   # exported for a gate step that diffs against the main this attempt merges onto
  say "▶ attempt $attempt/$ATTEMPTS: landing $BRANCH ($CANDIDATE) onto main ($BASE)"

  git -C "$LWT" merge --abort >/dev/null 2>&1
  git -C "$LWT" reset -q --hard "$BASE" || die "could not reset the lander worktree to $BASE"
  git -C "$LWT" clean -qfd
  provision

  if [ -n "$ORIGIN" ] && ! git merge-base --is-ancestor "$ORIGIN" "$BASE"; then
    say "▶ reconciling origin/main in scratch"
    git -C "$LWT" -c user.name=land -c user.email=land@local merge --no-edit "$ORIGIN" \
      || die "origin reconciliation conflicts; main is unchanged"
  fi

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
    land_receipt ALREADY_LANDED "candidate already reachable"
    event noop already-landed "$BASE"
    exit 0
  fi

  # Every prior gate result is stale: the pre-commit hook tested each of your
  # commits against the OLD main, and git runs pre-merge-commit rather than
  # pre-commit for the merge above. Verification ownership spans this gate.
  prepare_codegraph "$LWT" || die "CodeGraph is not ready at $LWT; refusing to continue"
  say "▶ gate on the merged tree: $GATE"
  LAND_GATE_LOG="$COMMON/harness/gate-$SLUG-$attempt.log"
  ( cd "$LWT" && env -u VERIFY_LINT_ONLY -u VERIFY_FAST bash -c "$GATE" ) >"$LAND_GATE_LOG" 2>&1
  gate_status=$?
  cat "$LAND_GATE_LOG"
  [ "$gate_status" = 0 ] || die "the gate failed on the merged tree; main is unchanged"

  MERGED=$(git -C "$LWT" rev-parse HEAD)

  # --- critical section: read, swap, and verify indivisibly ------------------
  lock
  [ "$(git rev-parse HEAD)" = "$CANDIDATE" ] || die "candidate moved during the gate"
  python3 "$CONTROL/harness_receipts.py" validate --root "$CALLER" --candidate "$CANDIDATE" --receipt "$RECEIPT" >/dev/null \
    || die "review evidence moved during the gate"
  if [ "$(git rev-parse main)" != "$BASE" ]; then
    unlock
    attempt=$((attempt + 1))
    [ "$attempt" -le "$ATTEMPTS" ] || die "main moved during every gate attempt"
    continue
  fi
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
    land_receipt LANDED "gate_exit=0 candidate_reachable=true CAS=$BASE->$MERGED"
    unlock
    event landed "attempts=$attempt" "$MERGED"
    cleanup_codegraph "$CALLER"
    cleanup_codegraph "$LWT"
    prepare_codegraph "$MAIN" || say "land succeeded; CodeGraph refresh pending at $MAIN"
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
