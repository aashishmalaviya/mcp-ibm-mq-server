"""MCP server entry point.

Run with:  mcp-ibm-mq-server                 (stdio, real IBM MQ via pymqi)
           MQ_FAKE=true mcp-ibm-mq-server    (in-memory demo, no MQ needed)
           MQ_TRANSPORT=streamable-http ...  (HTTP with OAuth; see README)
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .approvals import ApprovalStore
from .audit import AuditLog
from .identity import Entitlements, Identity
from .mq_client import InMemoryClient, PyMQIClient
from .policy import Policy
from .service import MQService


def build_service(identity=None) -> MQService:
    policy = Policy.from_env()
    if os.environ.get("MQ_FAKE", "").lower() in {"1", "true", "yes"}:
        seed = {q: [b'{"orderId": "A-1001", "status": "SHIPPED"}'] for q in policy.read_queues}
        client = InMemoryClient(seed)
    else:
        client = PyMQIClient()
    if identity is None:
        fixed = Identity.from_env()
        identity = lambda: fixed  # noqa: E731
    return MQService(client, policy, AuditLog(), ApprovalStore.from_env(),
                     identity=identity, entitlements=Entitlements.from_env())


def http_auth_from_env():
    """Token verifier and identity source for streamable HTTP. Every request must carry a valid token."""
    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp.server.auth.settings import AuthSettings

    from .auth import JWTVerifier

    missing = [v for v in ("MQ_AUTH_ISSUER", "MQ_AUTH_AUDIENCE", "MQ_AUTH_JWKS_URL") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"HTTP transport needs OAuth settings: {', '.join(missing)}")
    verifier = JWTVerifier(
        issuer=os.environ["MQ_AUTH_ISSUER"],
        audience=os.environ["MQ_AUTH_AUDIENCE"],
        jwks_url=os.environ["MQ_AUTH_JWKS_URL"],
    )
    settings = AuthSettings(
        issuer_url=os.environ["MQ_AUTH_ISSUER"],
        resource_server_url=os.environ["MQ_AUTH_AUDIENCE"],
        validate_token_resource=False,  # the verifier checks the audience itself
    )

    def identity() -> Identity:
        token = get_access_token()
        if token is None or not token.claims:
            return Identity.anonymous()
        return JWTVerifier.identity_from_claims(token.claims)

    return verifier, settings, identity


def create_server(service: MQService | None = None, *, token_verifier=None, auth=None) -> MCPServer:
    svc = service or build_service()
    mcp = MCPServer("ibm-mq", token_verifier=token_verifier, auth=auth)
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
    consume = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

    @mcp.tool(annotations=read)
    def list_queues() -> dict:
        """List the IBM MQ queues this agent is allowed to read or write."""
        return svc.list_queues()

    @mcp.tool(annotations=read)
    def queue_depth(queue: str) -> dict:
        """Return the current number of messages on an allowed queue."""
        return svc.queue_depth(queue)

    @mcp.tool(annotations=read)
    def browse_messages(queue: str, max_messages: int = 5) -> dict:
        """Read messages without removing them (non-destructive browse)."""
        return svc.browse_messages(queue, max_messages)

    @mcp.tool(annotations=write)
    def put_message(
        queue: str, body: str, correl_id: str | None = None, approval_id: str | None = None
    ) -> dict:
        """Send a text message to a write-allowed queue. Disabled when read-only.

        Some queues need a person's approval. For those, the first call returns
        status "approval_required" and an approval_id; nothing is sent. Once a
        person approves, call again with the same queue, the same body and that
        approval_id. You cannot approve requests yourself.
        """
        return svc.put_message(queue, body, correl_id, approval_id)

    @mcp.tool(annotations=read)
    def approval_status(approval_id: str) -> dict:
        """Check whether a person has approved, rejected or let expire an approval request."""
        return svc.approval_status(approval_id)

    @mcp.tool(annotations=consume)
    def get_message(queue: str) -> dict:
        """Consume (remove) the next message. Disabled unless explicitly enabled."""
        return svc.get_message(queue)

    return mcp


def main() -> None:
    if os.environ.get("MQ_TRANSPORT", "stdio") == "streamable-http":
        verifier, settings, identity = http_auth_from_env()
        server = create_server(build_service(identity), token_verifier=verifier, auth=settings)
        server.run(
            transport="streamable-http",
            host=os.environ.get("MQ_HTTP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MQ_HTTP_PORT", "8000")),
        )
    else:
        create_server().run()


if __name__ == "__main__":
    main()
