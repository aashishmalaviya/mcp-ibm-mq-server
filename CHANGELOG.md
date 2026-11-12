# Changelog

## v0.2.0: release gate

Companion to *Integration to Intelligence* issue 2, "Your AI agent needs a release gate, not a demo".

- `evals/`: 30 deterministic cases (9 functional, 9 policy, 12 attack) with per-case checks for audit coverage, payload leakage and side effects of refused calls
- Agent evals: 10 prompts where Claude drives the tools, scored for tool accuracy and refusal; runs only when `ANTHROPIC_API_KEY` is set
- `evals/thresholds.yaml` and a CI job (`eval-gate`) that blocks merges below the floors and writes a score table to the job summary
- Tests that weaken the policy on purpose and prove the gate catches it

## v0.1.0: governed MCP server for IBM MQ

Companion to issue 1, "Your AI agent shouldn't touch your core systems directly". Five tools, deny-by-default policy, read-only by default, JSON audit log with payload digests only.
