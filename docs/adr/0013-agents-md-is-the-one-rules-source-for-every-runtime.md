# 0013. AGENTS.md is the one rules source for every runtime

Status: proposed
Date: 2026-10-23
Components: AGENTS.md, CLAUDE.md, .gemini/settings.json

## Context

Rules were split between `CLAUDE.md` and `AGENTS.md`, so Codex and Gemini CLI missed the commands, Rule 11 and the control principles. The harness is becoming a multi-model autonomous harness where speed and accuracy outrank provenance, and agents drift, widen scope and loop without short stop rules. Codex reads `AGENTS.md` natively; Claude Code reads `CLAUDE.md`; Gemini CLI 0.55.1 reads `GEMINI.md` unless `context.fileName` says otherwise. Gemini ignores workspace `.gemini/settings.json` in an untrusted folder (only when its Trusted Folders feature is enabled, which is off by default), so there context loading falls back to `GEMINI.md` and finds nothing.

## Decision

Root `AGENTS.md` holds every rule that is not Claude-specific, plus Scope, Stop and reassess, Proportional testing, Dispatch and report, Turn completion and Working rules sections. `CLAUDE.md` starts with `@AGENTS.md` and keeps only hook, model-routing and workflow notes. `.gemini/settings.json` sets `context.fileName` to `["AGENTS.md"]`; no `GEMINI.md` copy exists. New rules not yet enforced in code are marked "(prose; enforcement planned)".

## Consequences

One edit changes the rules for all three CLIs. Gemini in an untrusted folder does not load them. The new prose rules are not controls until code enforces them. Comments and tests that cite "CLAUDE.md Rule 11" now point at the wrong file and are left for a follow-up.
