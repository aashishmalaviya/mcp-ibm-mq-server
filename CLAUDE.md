# CLAUDE.md

Guidance for Claude (and people) working in this repository.

## What this is

A governed MCP server that lets AI agents work with IBM MQ without bypassing enterprise controls. It is the open-source reference code for Aashish Malaviya's LinkedIn newsletter *Integration to Intelligence* (Arch SI, archsi.io): each newsletter issue ships with a matching release here.

## Layout

- `src/mcp_ibm_mq/`: `server.py` (MCP wiring), `service.py` (tool logic), `policy.py` (deny-by-default rules), `audit.py` (JSON audit lines, payload digests only), `mq_client.py` (pymqi client and in-memory fake). Later releases add `approvals.py` and `approve_cli.py` (v0.3.0) and `identity.py` and `auth.py` (v0.4.0).
- `evals/`: the release gate. `cases.yaml` (deterministic cases), `run_evals.py`, `agent_cases.yaml` and `run_agent_evals.py` (Claude drives the tools; needs `ANTHROPIC_API_KEY`), `thresholds.yaml` (score floors).
- `tests/`: pytest, all against the in-memory client. `tests/test_evals.py` weakens the code on purpose and asserts the gate blocks it.

## Commands

```bash
pip install -e ".[dev]"                     # or ".[evals]" for the agent evals
pytest -q
PYTHONPATH=src python evals/run_evals.py    # must end with "Gate: **open**"
```

## Rules for changes

- Security behaviour changes need eval cases in `evals/cases.yaml`, and ideally a gate test in `tests/test_evals.py` showing a plausible bug is caught. Deterministic floors are 100%; do not lower them to make a change pass.
- Deny by default. New tools need a policy check, an audit line on every outcome (ok and denied), and no message bodies in the audit log.
- Keep stdio defaults backward compatible; new controls are opt-in through environment variables documented in the README Configuration table.
- Update `CHANGELOG.md` and the README with each release; bump `version` in `pyproject.toml`.
- Use the REST API (`gh api`) for PRs and releases; GraphQL is not available in Claude sessions.

## Release process

One release per newsletter issue, published the morning the article goes out (07:00 Arizona time, article at 07:30):

1. Build on a `release/vX.Y.Z` branch cut from the previous release branch; open a PR into `main`.
2. CI (`.github/workflows/ci.yml`) must pass: the pytest matrix and the `eval-gate` job.
3. On the release date: confirm the previous release is out, merge the PR with a merge commit, tag `vX.Y.Z`, publish a GitHub release whose body is the version's CHANGELOG section plus a link to the newsletter, delete the branch.
4. If anything fails, do not merge or tag; report instead.

| Version | Feature | Newsletter issue | Release date |
| --- | --- | --- | --- |
| v0.1.0 | Governed tool layer | 1: "Your AI agent shouldn't touch your core systems directly" | Nov 2026 (released) |
| v0.2.0 | Eval release gate in CI | 2: "Your AI agent needs a release gate, not a demo" | Tue Nov 17, 2026 |
| v0.3.0 | Human approval for risky writes | 3: "Where the human belongs in an AI agent workflow" | Tue Nov 24, 2026 |
| v0.4.0 | Agent identity, OAuth, per-tool scopes | 4: "Your AI agent needs an identity, not a service account" | Tue Dec 1, 2026 |
| next | Traces correlated across hops | 5: tracing and audit for agents | Tue Dec 8, 2026 (planned) |
