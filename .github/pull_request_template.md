## What changed

<!-- One bounded outcome (AGENTS.md ORCH-07). -->

## Consequence and review

- Consequence: Low / Medium / High / Critical (AGENTS.md table)
- Review performed:
- Mutants added or run (High and above):

## Verification actually run

```
./scripts/run.sh --tests
uvx ruff check .
```

- [ ] Every count in this description was derived twice (`.claude/agents/dual-derive.md`)
- [ ] Every external-API claim names its retained artifact (`.claude/agents/live-proof-auditor.md`)
- [ ] No secrets, `.env`, HAR captures, or files over 10 MB
