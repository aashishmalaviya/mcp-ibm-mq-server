import asyncio
import io
import json

import pytest

from mcp_ibm_mq.audit import AuditLog
from mcp_ibm_mq.identity import ALL_SCOPES, Entitlements, Identity, parse_scopes
from mcp_ibm_mq.mq_client import InMemoryClient
from mcp_ibm_mq.policy import Policy, PolicyError
from mcp_ibm_mq.server import create_server, http_auth_from_env
from mcp_ibm_mq.service import MQService


def test_env_identity_defaults_keep_v03_behaviour():
    who = Identity.from_env({})
    assert who.scopes == ALL_SCOPES and who.user_id is None and who.agent_id == "unknown-agent"
    assert Identity.from_env({"MQ_AGENT_SCOPES": "mq.read"}).scopes == {"mq.read"}


def test_parse_scopes_drops_unknown():
    assert parse_scopes("mq.read admin,mq.write") == {"mq.read", "mq.write"}
    assert parse_scopes(None) == frozenset()


def test_intersection_not_union():
    ents = Entitlements.from_dict({"users": {"u": {"scopes": ["mq.read", "mq.write"],
                                                   "read_queues": ["A"], "write_queues": ["W"]}}})
    policy = Policy(read_queues=frozenset({"A", "B"}), write_queues=frozenset({"W", "X"}), read_only=False)
    narrowed = ents.effective(Identity("agent", "u", frozenset({"mq.read"})), policy, "browse_messages")
    assert narrowed.read_queues == {"A"} and narrowed.write_queues == {"W"}
    with pytest.raises(PolicyError, match="Agent 'agent' lacks scope 'mq.write'"):
        ents.effective(Identity("agent", "u", frozenset({"mq.read"})), policy, "put_message")


def test_per_request_identity_in_audit():
    stream = io.StringIO()
    callers = iter([Identity("a1", "alice", ALL_SCOPES), Identity("a2", "bob", ALL_SCOPES)])
    svc = MQService(InMemoryClient({"Q": [b"x"]}), Policy(read_queues=frozenset({"Q"})),
                    AuditLog(path="", stream=stream), identity=lambda: next(callers))
    svc.queue_depth("Q")
    svc.queue_depth("Q")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [(l["actor"], l["on_behalf_of"]) for l in lines] == [("a1", "alice"), ("a2", "bob")]


def test_anonymous_identity_can_do_nothing():
    svc = MQService(InMemoryClient(), Policy(read_queues=frozenset({"Q"})), AuditLog(path="/dev/null"),
                    identity=Identity.anonymous)
    assert svc.list_queues()["ok"] is False


def test_http_requires_oauth_settings(monkeypatch):
    for var in ("MQ_AUTH_ISSUER", "MQ_AUTH_AUDIENCE", "MQ_AUTH_JWKS_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit, match="MQ_AUTH_ISSUER"):
        http_auth_from_env()


def test_server_accepts_token_verifier(monkeypatch):
    monkeypatch.setenv("MQ_AUTH_ISSUER", "https://login.example.com/t1")
    monkeypatch.setenv("MQ_AUTH_AUDIENCE", "https://mq-mcp.example.com")
    monkeypatch.setenv("MQ_AUTH_JWKS_URL", "https://login.example.com/t1/keys")
    verifier, settings, identity = http_auth_from_env()
    svc = MQService(InMemoryClient(), Policy(), AuditLog(path="/dev/null"), identity=identity)
    server = create_server(svc, token_verifier=verifier, auth=settings)
    assert {t.name for t in asyncio.run(server.list_tools())} >= {"list_queues", "put_message"}
    assert identity().agent_id == "anonymous"  # no token in context: no access


def test_verify_token_returns_access_token():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
    import run_evals

    from mcp_ibm_mq.auth import JWTVerifier

    settings = {"issuer": "https://login.example.com/t1", "audience": "https://mq-mcp.example.com"}
    verifier = JWTVerifier(jwks=run_evals._keys()["jwks"], **settings)
    good = run_evals.make_token({"claims": {"sub": "alice", "azp": "agent-1", "scope": "mq.read"}}, settings)
    bad = run_evals.make_token({"claims": {"sub": "alice", "azp": "agent-1"}, "sign": "attacker"}, settings)
    token = asyncio.run(verifier.verify_token(good))
    assert token.client_id == "agent-1" and token.subject == "alice" and token.scopes == ["mq.read"]
    assert asyncio.run(verifier.verify_token(bad)) is None


def test_verifier_refuses_symmetric_algorithms():
    from mcp_ibm_mq.auth import JWTVerifier

    with pytest.raises(ValueError):
        JWTVerifier(issuer="i", audience="a", jwks={"keys": []}, algorithms=("HS256",))
