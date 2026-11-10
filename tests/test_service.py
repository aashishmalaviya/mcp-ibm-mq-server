import io
import json

from mcp_ibm_mq.audit import AuditLog
from mcp_ibm_mq.mq_client import InMemoryClient
from mcp_ibm_mq.policy import Policy
from mcp_ibm_mq.service import MQService


def make(policy: Policy, queues=None):
    stream = io.StringIO()
    svc = MQService(InMemoryClient(queues or {}), policy, AuditLog(path="", stream=stream))
    return svc, stream


def audit_lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_browse_does_not_remove_messages():
    svc, _ = make(Policy(read_queues=frozenset({"Q1"})), {"Q1": [b"a", b"b"]})
    res = svc.browse_messages("Q1", 5)
    assert [m["body"] for m in res["messages"]] == ["a", "b"]
    assert svc.queue_depth("Q1")["depth"] == 2


def test_denied_calls_are_audited_and_do_not_touch_mq():
    svc, stream = make(Policy(read_queues=frozenset({"Q1"})), {"SECRET.Q": [b"x"]})
    res = svc.browse_messages("SECRET.Q")
    assert res["ok"] is False
    entry = audit_lines(stream)[-1]
    assert entry["outcome"] == "denied" and entry["queue"] == "SECRET.Q"


def test_put_logs_digest_not_body():
    policy = Policy(write_queues=frozenset({"OUT.Q"}), read_only=False)
    svc, stream = make(policy)
    res = svc.put_message("OUT.Q", '{"ssn": "123-45-6789"}')
    assert res["ok"] is True
    raw = stream.getvalue()
    assert "123-45-6789" not in raw
    assert len(audit_lines(stream)[-1]["sha256"]) == 64


def test_read_only_blocks_writes():
    svc, _ = make(Policy(write_queues=frozenset({"OUT.Q"}), read_only=True))
    assert svc.put_message("OUT.Q", "hi")["ok"] is False


def test_destructive_get_when_enabled():
    policy = Policy(read_queues=frozenset({"Q1"}), read_only=False, allow_destructive_get=True)
    svc, _ = make(policy, {"Q1": [b"only"]})
    assert svc.get_message("Q1")["message"]["body"] == "only"
    assert svc.get_message("Q1")["message"] is None
