# Changelog

## v0.3.0: human approval for risky writes

Companion to *Integration to Intelligence* issue 3, "Where the human belongs in an AI agent workflow (and where they don't)".

- `MQ_APPROVAL_QUEUES`: puts to these queues return `approval_required` and an `approval_id` instead of sending
- Approvals are decided outside the agent (`mcp-ibm-mq-approvals` CLI) with a name and a reason; the requester cannot approve its own request
- An approval is single use, bound to the queue and the exact body (SHA-256), and expires (`MQ_APPROVAL_TTL_SECONDS`); expiry never turns into approval
- New read-only tool `approval_status`; requests, decisions and sends are all audited
- 14 new eval cases (44 in total), including forged and reused approvals, a body swapped after approval, and self-approval; gate tests for approvals that skip the body check or never expire

## v0.2.0: release gate

Companion to *Integration to Intelligence* issue 2, "Your AI agent needs a release gate, not a demo".

- `evals/`: 30 deterministic cases (9 functional, 9 policy, 12 attack) with per-case checks for audit coverage, payload leakage and side effects of refused calls
- Agent evals: 10 prompts where Claude drives the tools, scored for tool accuracy and refusal; runs only when `ANTHROPIC_API_KEY` is set
- `evals/thresholds.yaml` and a CI job (`eval-gate`) that blocks merges below the floors and writes a score table to the job summary
- Tests that weaken the policy on purpose and prove the gate catches it

## v0.1.0: governed MCP server for IBM MQ

Companion to issue 1, "Your AI agent shouldn't touch your core systems directly". Five tools, deny-by-default policy, read-only by default, JSON audit log with payload digests only.
