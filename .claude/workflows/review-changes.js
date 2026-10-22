export const meta = {
  name: 'review-changes',
  description: 'Review the current diff on two dimensions, then verify each finding independently',
  phases: [
    { title: 'Review', detail: 'one reviewer per dimension reads the diff and reports findings' },
    { title: 'Verify', detail: 'each finding is re-derived by a fresh agent that tries to refute it' },
  ],
}

// Every agent call spreads exactly one ROLE, and every ROLE names one MODEL
// capability. tests/test_workflow_routing.py pins both tables from its own
// literals, never from this file (CLAUDE.md Rule 11).
const MODEL = Object.freeze({
  REASONING_STRONG: 'opus',
  MECHANICAL: 'haiku',
})

const ROLE = Object.freeze({
  REVIEWER: { model: MODEL.REASONING_STRONG },
  VERIFIER: { model: MODEL.REASONING_STRONG },
})

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          title: { type: 'string' },
          failure_scenario: { type: 'string' },
        },
        required: ['file', 'title', 'failure_scenario'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    is_real: { type: 'boolean' },
    evidence: { type: 'string' },
  },
  required: ['is_real', 'evidence'],
}

const DIMENSIONS = [
  { key: 'correctness', prompt: 'Review `git diff main...HEAD` for correctness bugs. Report only defects with a concrete failure scenario.' },
  { key: 'tests', prompt: 'Review `git diff main...HEAD` for behaviour the tests do not pin. Report only gaps a plausible regression would slip through.' },
]

const results = await pipeline(
  DIMENSIONS,
  d => agent(d.prompt, { ...ROLE.REVIEWER, label: `review:${d.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }),
  review => parallel(review.findings.map(f => () =>
    agent(`Try to refute this finding by reading the code and running what you need. Read-only.\n${JSON.stringify(f)}`,
      { ...ROLE.VERIFIER, label: `verify:${f.file}`, phase: 'Verify', schema: VERDICT_SCHEMA })
      .then(v => ({ ...f, verdict: v }))
  ))
)

// No findings is not approval (AGENTS.md ORCH-10): the caller still owns the verdict.
return { confirmed: results.flat().filter(Boolean).filter(f => f.verdict?.is_real) }
