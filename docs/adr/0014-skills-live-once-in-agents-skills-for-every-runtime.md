# 0014. Skills live once in .agents/skills for every runtime

Status: proposed
Date: 2026-10-23
Components: .agents/skills/building-with-jev/SKILL.md, .agents/skills/building-with-jev/references/composition-patterns.md, .agents/skills/building-with-jev/references/decision-card.md, .agents/skills/building-with-jev/references/improve-and-calibrate.md, .agents/skills/building-with-jev/references/primitives.md, .agents/skills/building-with-jev/references/question-design.md, .agents/skills/building-with-jev/references/state-and-budget.md, .agents/skills/debugging/SKILL.md, .agents/skills/debugging/references/forensics.md, .agents/skills/grill-jev/SKILL.md, .agents/skills/grill-jev/references/question-battery.md, .agents/skills/land/SKILL.md, .agents/skills/pr-workflow/SKILL.md, .agents/skills/pr-workflow/references/commit.md, .agents/skills/quick/SKILL.md, .agents/skills/review/SKILL.md, .agents/skills/review/references/architecture-smell-baseline.md, .agents/skills/review/references/receiving-feedback.md, .agents/skills/review/references/report-template.md, .agents/skills/security/SKILL.md, .agents/skills/security/references/coverage.md, .agents/skills/security/references/threat-model.md, .agents/skills/stop-and-reassess/SKILL.md, .agents/skills/stop-and-reassess/references/loop-detail.md, .agents/skills/testing/SKILL.md, .agents/skills/testing/references/audit-test-value.md, .agents/skills/worktree-agent/SKILL.md, .claude/agents/dual-derive.md, .claude/agents/live-proof-auditor.md, .claude/agents/python-general-engineer.md, .claude/agents/reviewer-code.md, .claude/agents/reviewer-system.md, .claude/agents/testing-automation-engineer.md, .claude/skills

## Context

Claude Code, Codex and Gemini CLI each load repository skills from a different directory. Codex 0.164 reads `.agents/skills` and ignores `.claude/skills`. Gemini CLI 0.55.1 documents `.agents/skills` as a workspace alias for `.gemini/skills`, but loads project content only from a trusted folder. Claude Code reads `.claude/skills`. Keeping one copy per runtime drifts, and the harness is meant to run on all three.

## Decision

Skills are written once, as `.agents/skills/<name>/SKILL.md`, with `name` and `description` as the only frontmatter fields (the subset all three CLIs accept). `.claude/skills` is a symlink to `../.agents/skills`, so Claude Code loads the same files; there is no copy and no sync step. `adr.require_for` gains `.agents/skills/**`, so a skill needs a proposed or accepted ADR before it is created, as `.claude/skills/**` already did.

Agents stay in `.claude/agents/` in Claude format, with `.codex/agents/<name>.toml` role files that point Codex at the same definition and `.codex/config.toml` declaring each role. Gemini subagents are not provided: its project agents use a different tool vocabulary and need a separate decision. Vendored agent names are pinned in both tables of `model_router.py` and its test (Rule 11).

## Consequences

One edit changes a skill for all three runtimes, and the land gate covers the files. Gemini and Codex load project content only after the owner trusts the folder in each tool; nothing in the repository can do that. A user-level skill with the same name as a project skill wins in Claude Code on a machine that has one, so a project skill needs a name the owner's user-level set does not use. A symlink needs a filesystem that keeps it (not a Windows checkout without symlink support). This does not cover hooks or rules files.
