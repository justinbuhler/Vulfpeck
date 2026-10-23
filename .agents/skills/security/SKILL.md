---
name: security
description: Use when reviewing a diff or change for vulnerabilities (injection, authorization, secrets, unsafe deserialization, supply chain), before committing security-sensitive code, or when threat-modelling a component.
---

# Security review

Review the changed lines against the taxonomy in `references/coverage.md`. Report only findings with a concrete path from untrusted input to a dangerous sink.

## Steps

1. **Scope.** List the changed files for the range you were given, `<base>...<sha>`, defaulting to `origin/main...HEAD`: `git diff --name-only <base>...<sha>`. Review a committed candidate by that range; for uncommitted work also use `git diff --name-only HEAD` and `git diff --cached --name-only`. If empty, report "no changes to review" and stop. Read the repo `AGENTS.md` for secrets and allowed-pattern rules.
2. **Quick sweep.** Search the changed lines for the obvious: hardcoded secrets and tokens, string-built SQL, `shell=True` or `sh -c` with variable input, `eval` or `exec`, `pickle` or unsafe YAML loads, disabled TLS verification, wildcard CORS, permissive file modes. Use `rg` on the diff, not the whole tree.
3. **Depth review.** Load `references/coverage.md`. Walk each changed file against its classes, and finish with its high-miss checklist. Check authorization on every new entry point, and data flow through every new parser, path join and subprocess.
4. **Verify each finding.** Reproduce the input sequence or show the call path. Drop findings an existing guard already covers.
5. **Verdict.** Any CRITICAL: BLOCK. HIGH but no CRITICAL: FIX before commit. Only MEDIUM or LOW: APPROVE with suggestions. Never issue a verdict from an incomplete review.

## Report

```
Severity | Count
Critical | N
High     | N
Medium   | N
Findings: [severity] description - file:line - fix
VERDICT: BLOCK | FIX | APPROVE - one-sentence rationale
```

## Threat model

For a whole component, use `references/threat-model.md`.

## Rules

- A test fixture or documented example is not a finding; say why you downgraded it.
- Never print a discovered secret. Name the file and line, and say it must be rotated.
- A finding that needs a live system to confirm is labeled an unverified hypothesis.
