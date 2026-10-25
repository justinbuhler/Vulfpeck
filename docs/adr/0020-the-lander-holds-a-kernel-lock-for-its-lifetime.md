# 0020. The lander holds a kernel lock for its lifetime

Status: proposed
Date: 2026-10-25
Components: .claude/harness/agent_harness.py

## Context

This extends ADR 0006, which serializes landing through one queue but leaves ownership of the queue to a PID file.
`acquire_lander_lock` creates `lander.lock` with `O_EXCL` and only then writes its PID.
Between those two steps a second lander reads an empty file, finds no live PID, unlinks the file and creates its own.
The first lander then writes into the unlinked inode and also returns as owner, so two landers run the queue at once (audit finding H-03, reproduced with separate processes).
`cmd_land` also unlinks the lock when it exits, and `reconcile` reports a holder whenever the file exists, whether or not anyone holds it.

## Decision

The lander takes an advisory `fcntl.flock(LOCK_EX | LOCK_NB)` on `lander.lock` and keeps the descriptor open for its whole life, including idle polling and every gate.
The file is opened without truncation and the PID is written only after the lock is held; the PID is diagnostic.
Nothing unlinks the lock file, on takeover or on exit; closing the descriptor releases it, and the kernel releases it if the process dies.
The descriptor is non-inheritable and is not passed to the gate.
On contention the lander refuses with the existing "another lander already holds" message plus the recorded PID if readable.
`reconcile` reports a holder only when a non-blocking probe of the lock fails, and releases the probe at once.
`enqueue` never takes the lander lock.

A second lock, `meta.lock` in the state root, is taken blocking and only around short state transitions.
It is released before any gate, git commit, sleep or network call.
Lock order is lander lock, then `meta.lock`; no path takes the lander lock while holding `meta.lock`.

`provision` writes `"generation": 1` into the task manifest.
`enqueue`, under `meta.lock`, re-reads the manifest, refuses if it is missing or its worktree, branch or baseline differ, and writes the request with the manifest's generation.
The lander fails a request whose generation is absent or differs from the manifest's, and names it in the ledger.

The lander claims a request under `meta.lock` with `os.replace(queue/X, processing/X)` and writes `processing/X.owner.json` (lander PID, a per-lander random token, claim time).
It then releases `meta.lock` and runs the land.
Acknowledgement, under `meta.lock`, verifies the owner token is still its own, moves `processing/X` to `done/X` and removes the owner record.
`state_root` creates `processing/`.
`reconcile` shows a task with a request in `processing/` as `PROCESSING`, which outranks `QUEUED`, with the owner PID; if no live lander holds the lander lock, the detail says the request is stale and awaits recovery.
`reconcile` mutates nothing.

## Consequences

Two landers cannot hold the queue at once, a crashed lander cannot wedge it, and the lock file is never replaced under a holder.
A request is in exactly one of `queue/`, `processing/` or `done/`, and only the lander that claimed it can acknowledge it.
`enqueue` stays live while a lander runs, and `meta.lock` is never held across a gate.

This does not cover:

- A stale `processing/` request is reported, not recovered; replay, reset or quarantine belongs to R2.
- The generation is never bumped, so the check only catches absent or mismatched values until S1 adds a bump.
- Advisory locks bind only processes that take them; a process that ignores the lock is not stopped.
- The lock is local to one host; it gives no exclusion across machines or over a network filesystem.
