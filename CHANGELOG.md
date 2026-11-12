# Changelog

## v0.5.0: traces correlated across hops

Companion to *Integration to Intelligence* issue 5, "What did the agent actually do? Tracing AI agents for incident review".

- Every tool call is a span: it joins a W3C `traceparent` from the MCP request `_meta` or HTTP headers, or starts a new trace; malformed, all-zero and oversized headers are ignored
- Audit lines carry `trace_id`, `span_id`, `parent_span_id`, `duration_ms` and millisecond timestamps; denied calls are traced too; tool responses return the `trace_id`
- `MQ_TRACE_TO_CORRELID=true` puts the trace ID into the CorrelId of sent messages that have no correlation ID of their own
- Approval requests and decisions carry the trace ID of the call that asked
- `mcp-ibm-mq-trace` replays one trace from the audit log for an incident review
- 9 new eval cases (74 in total) and a `trace_coverage` floor of 100%; gate tests for trusting unvalidated trace headers and for untraced audit lines

## v0.4.0: agent identity, OAuth and least privilege

Companion to *Integration to Intelligence* issue 4, "Your AI agent needs an identity, not a service account".

- Every call resolves a named agent and the user it acts for; both go into the audit log with the scope used
- Per-tool scopes (`mq.read`, `mq.write`, `mq.consume`) and optional user entitlements (`MQ_ENTITLEMENTS`); effective access is the intersection of server policy, agent grant and user grant
- Streamable HTTP transport with OAuth 2.1 bearer tokens (`MQ_TRANSPORT=streamable-http`): JWKS signature, issuer, audience and expiry checks; agent from `azp` / `client_id` / RFC 8693 `act`, user from `sub`
- 21 new eval cases (65 in total), including 13 token attacks (unsigned, forged, expired, wrong audience or issuer, algorithm confusion); gate tests for a union of rights and for skipping the audience check
- Defaults unchanged for stdio: without `MQ_AGENT_SCOPES` or `MQ_ENTITLEMENTS` the server behaves as in v0.3.0

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
