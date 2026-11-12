# mcp-ibm-mq-server

**A governed [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets AI agents read and send IBM MQ messages without bypassing enterprise controls.**

![Architecture](docs/architecture.png)

Most AI agent demos connect straight to core systems with a service account. That never gets past security in a bank, a hospital or a government agency. This server puts a narrow, audited tool layer between the agent and IBM MQ, so Claude, Copilot, OpenAI or Gemini agents can work with existing queues while the queue manager, its channels and the core systems stay unchanged.

It is the reference implementation for my newsletter, [*Integration to Intelligence*](https://www.linkedin.com/in/aashish-malaviya):

- Issue 1, *"Your AI agent shouldn't touch your core systems directly"*: the governed tool layer (v0.1.0)
- Issue 2, *"Your AI agent needs a release gate, not a demo"*: the eval gate in CI (v0.2.0, [below](#release-gate))
- Issue 3, *"Where the human belongs in an AI agent workflow"*: human approval for risky writes (v0.3.0, [below](#human-approval))

## Why this design

| Principle | How it is enforced |
| --- | --- |
| **Tools, not tables** | Agents get five business-level tools, never raw MQI access |
| **Deny by default** | Only queues on an allow-list are visible; everything else is refused |
| **Read-only by default** | Writes need `MQ_READ_ONLY=false` **and** the queue on the write list |
| **People approve risky writes** | Puts to queues on `MQ_APPROVAL_QUEUES` wait for a named person; approvals are single-use, bound to the exact body, and expire |
| **Consuming is a separate decision** | Destructive `get_message` needs a third switch, `MQ_ALLOW_DESTRUCTIVE_GET=true` |
| **Bounded** | Message size limit and a cap on how many messages one browse returns |
| **Auditable** | Every call, allowed or denied, is a JSON audit line; bodies are never logged, only their SHA-256 |
| **Model-agnostic** | Any MCP client: Claude Desktop, Claude Code, Copilot Studio, OpenAI Agents SDK, Gemini |

## Tools

| Tool | What it does | MCP annotation |
| --- | --- | --- |
| `list_queues` | Queues this agent may read or write | read-only |
| `queue_depth` | Current depth of an allowed queue | read-only |
| `browse_messages` | Read up to N messages **without** removing them | read-only |
| `put_message` | Send a text message (optional correlation ID) | write |
| `get_message` | Consume the next message | destructive |
| `approval_status` | Check whether a person approved a pending put | read-only |

## Quick start (no IBM MQ needed)

```bash
pip install -e ".[dev]"
MQ_FAKE=true MQ_READ_QUEUES=ORDERS.STATUS mcp-ibm-mq-server
```

`MQ_FAKE=true` uses an in-memory queue manager seeded with a sample message, so you can try the tools from any MCP client in a minute.

## Run against a real IBM MQ

1. Start IBM's free developer queue manager (creates `QM1`, `DEV.APP.SVRCONN` and `DEV.QUEUE.1`):

   ```bash
   cp .env.example .env        # set MQ_PASSWORD
   docker compose up -d
   ```

2. Install the IBM MQ client libraries ([IBM MQ redistributable client](https://www.ibm.com/support/pages/downloading-ibm-mq-redistributable-clients)) and the `mq` extra:

   ```bash
   pip install -e ".[mq]"
   ```

3. Point the server at the queue manager and choose what the agent may do:

   ```bash
   export MQ_QMGR=QM1 MQ_CONN_INFO="localhost(1414)" MQ_CHANNEL=DEV.APP.SVRCONN
   export MQ_USER=app MQ_PASSWORD=...           # from .env, never committed
   export MQ_READ_QUEUES=DEV.QUEUE.1
   mcp-ibm-mq-server
   ```

## Connect it to Claude Desktop

Add to `claude_desktop_config.json` (see [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json)):

```json
{
  "mcpServers": {
    "ibm-mq": {
      "command": "mcp-ibm-mq-server",
      "env": {
        "MQ_FAKE": "true",
        "MQ_READ_QUEUES": "ORDERS.STATUS",
        "MQ_AGENT_ID": "claude-desktop"
      }
    }
  }
}
```

Then ask: *"What's on the ORDERS.STATUS queue?"*

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MQ_READ_QUEUES` | *(none)* | Comma-separated queues the agent may browse and inspect |
| `MQ_WRITE_QUEUES` | *(none)* | Queues the agent may put to |
| `MQ_READ_ONLY` | `true` | Master switch for all writes |
| `MQ_ALLOW_DESTRUCTIVE_GET` | `false` | Allow `get_message` to consume |
| `MQ_MAX_MESSAGE_BYTES` | `65536` | Largest message an agent may send |
| `MQ_MAX_BROWSE` | `20` | Most messages one browse returns |
| `MQ_APPROVAL_QUEUES` | *(none)* | Write queues where every put needs a person's approval |
| `MQ_APPROVAL_TTL_SECONDS` | `3600` | How long a request (or an unused approval) stays valid |
| `MQ_APPROVAL_STORE` | in memory | JSON file shared by the server and the approver CLI |
| `MQ_AUDIT_LOG` | stderr | Path for JSON-lines audit log |
| `MQ_AGENT_ID` | `unknown-agent` | Identity recorded in the audit log |
| `MQ_QMGR`, `MQ_CONN_INFO`, `MQ_CHANNEL`, `MQ_USER`, `MQ_PASSWORD` | | Queue manager connection |
| `MQ_FAKE` | `false` | Use the in-memory demo queue manager |

## Production notes

- Run the server behind your **API gateway** (Apigee, Azure API Management, Kong, IBM API Connect) when exposing it over HTTP, so agents inherit OAuth identity, rate limits and logging. Use stdio for local clients.
- Give the MQ user **least privilege** with `setmqaut` (browse/inq on read queues, put on write queues only) and enable **TLS** and **CHLAUTH** on the channel. The policy here is a second line of defence, not a replacement for MQ security.
- Ship the audit log to your SIEM; correlate on `audit_id`, `msg_id` and `MQ_AGENT_ID`.
- Put high-risk queues (payments, account changes) on `MQ_APPROVAL_QUEUES` and keep the approval store on durable, access-controlled storage.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

Tests use the in-memory client, so they run anywhere, including CI.

## Human approval

For queues where a wrong message costs real money or harm (payments, account changes), the agent can propose a put but cannot send it on its own:

1. The agent calls `put_message`. Nothing is sent. The server records a request and returns `status: approval_required` with an `approval_id`.
2. A person reviews it outside the agent and decides, with their name and a reason:

   ```bash
   export MQ_APPROVAL_STORE=/var/lib/mcp-ibm-mq/approvals.json   # same file as the server
   mcp-ibm-mq-approvals list
   mcp-ibm-mq-approvals show apr_3f9c2a1b7d4e6f80
   mcp-ibm-mq-approvals approve apr_3f9c2a1b7d4e6f80 --approver jane.doe --reason "Matches invoice 4411"
   ```

3. The agent calls `put_message` again with the same queue, the same body and the `approval_id`.

The server only sends it if the request was approved, by someone other than the requesting agent, has not expired, matches the approved queue and body (SHA-256), and has not been used before. Expiry never turns into approval. The agent has no tool that can approve anything, and every request, decision and send is in the audit log.

The CLI is the simplest approver; in production, point your own workflow tool (ServiceNow, a Teams approval, a case system) at the same store or replace `ApprovalStore` with one backed by it.

## Release gate

Every pull request runs an eval gate (`.github/workflows/ci.yml`, job `eval-gate`). A change does not merge if any score drops below its floor in [`evals/thresholds.yaml`](evals/thresholds.yaml).

| Layer | What it checks | Needs |
| --- | --- | --- |
| Deterministic ([`run_evals.py`](evals/run_evals.py)) | 44 cases in [`cases.yaml`](evals/cases.yaml): allowed actions work, forbidden ones are refused, 18 attack cases (planted instructions, queue-name smuggling, case and whitespace tricks, admin queue, draining, forged or reused approvals, a body swapped after approval, self-approval) are refused. On every case: an audit line exists, no sensitive text reaches the audit log, and a refused call changes nothing on any queue | Nothing; runs free on every PR |
| Agent ([`run_agent_evals.py`](evals/run_agent_evals.py)) | Claude drives the tools for 10 prompts in [`agent_cases.yaml`](evals/agent_cases.yaml): right tool and queue, and no obeying instructions planted in message bodies | `ANTHROPIC_API_KEY` repository secret; skipped without it |

Run it locally:

```bash
pip install -e ".[evals]"
python evals/run_evals.py                              # deterministic gate
ANTHROPIC_API_KEY=... python evals/run_agent_evals.py  # agent evals
```

The gate is itself tested: `tests/test_evals.py` weakens the policy on purpose (prefix matching on queue names, ignoring read-only, approvals that skip the body check or never expire) and checks that the gate blocks it.

## Roadmap

- Streamable HTTP transport with OAuth (MCP authorization spec)
- JSON-schema validation of message bodies per queue
- IBM MQ REST API client option (no native client libraries)

## About

Built by **Aashish Malaviya**, Principal Architect at [Arch SI](https://archsi.io) (Architecture · System Intelligence). 20+ years of enterprise integration on IBM MQ, ACE and DataPower across banking, security services and government, now connecting AI agents to the systems they act on.

[LinkedIn](https://www.linkedin.com/in/aashish-malaviya) · [archsi.io](https://archsi.io)

## License

MIT
