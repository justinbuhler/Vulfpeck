# Review report template

```text
Review Summary:
  Candidate: <sha>   Files: N   Lines: +X/-Y
  Checks run: <command -> PASS | FAIL | SKIPPED>
  Risk: LOW | MEDIUM | HIGH | CRITICAL

BLOCKING:
  1. Issue and consequence - path/file.ext:42
SHOULD FIX:
  1. Issue and consequence - path/file.ext:52
SUGGESTIONS:
  1. Optional improvement - path/file.ext:62

Scope and limits: what was and was not examined.
Verdict: APPROVE | FIX | REQUEST-CHANGES | NEEDS-DISCUSSION
Rationale: evidence for the verdict.
```

Every finding carries `file:line`, the failing input or sequence, and the consequence. Mark an unreproduced claim as an unverified hypothesis.
