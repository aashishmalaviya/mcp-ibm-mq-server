import io
import json

import pytest

from mcp_ibm_mq import approve_cli
from mcp_ibm_mq.approvals import ApprovalError, ApprovalStore
from mcp_ibm_mq.audit import AuditLog
from mcp_ibm_mq.mq_client import InMemoryClient
from mcp_ibm_mq.policy import Policy
from mcp_ibm_mq.service import MQService


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make(store=None):
    policy = Policy(write_queues=frozenset({"PAY.OUT"}), approval_queues=frozenset({"PAY.OUT"}),
                    read_only=False, approval_ttl_seconds=60)
    stream = io.StringIO()
    audit = AuditLog(path="", stream=stream)
    audit.actor = "agent-1"
    return MQService(InMemoryClient(), policy, audit, store or ApprovalStore()), stream


def test_full_approval_flow_is_audited():
    svc, stream = make()
    first = svc.put_message("PAY.OUT", '{"amount": 5}')
    assert first["status"] == "approval_required" and svc.client.depth("PAY.OUT") == 0
    svc.approvals.decide(first["approval_id"], approver="jane", approve=True, reason="ok")
    done = svc.put_message("PAY.OUT", '{"amount": 5}', approval_id=first["approval_id"])
    assert done["ok"] and done["approved_by"] == "jane" and svc.client.depth("PAY.OUT") == 1
    outcomes = [json.loads(line)["outcome"] for line in stream.getvalue().splitlines()]
    assert outcomes == ["approval_required", "ok"]


def test_decision_needs_name_and_reason():
    store = ApprovalStore()
    req = store.create(queue="Q", sha256="x", size=1, requested_by="a", on_behalf_of=None, ttl_seconds=60)
    with pytest.raises(ApprovalError):
        store.decide(req["id"], approver=" ", approve=True, reason="ok")
    with pytest.raises(ApprovalError):
        store.decide(req["id"], approver="jane", approve=True, reason="")


def test_cannot_decide_twice_or_after_expiry():
    clock = Clock()
    store = ApprovalStore(clock=clock)
    a = store.create(queue="Q", sha256="x", size=1, requested_by="a", on_behalf_of=None, ttl_seconds=60)
    store.decide(a["id"], approver="jane", approve=False, reason="no")
    with pytest.raises(ApprovalError, match="rejected"):
        store.decide(a["id"], approver="jane", approve=True, reason="changed my mind")
    b = store.create(queue="Q", sha256="x", size=1, requested_by="a", on_behalf_of=None, ttl_seconds=60)
    clock.now += 61
    with pytest.raises(ApprovalError, match="expired"):
        store.decide(b["id"], approver="jane", approve=True, reason="late")


def test_file_store_is_shared_with_cli(tmp_path, monkeypatch, capsys):
    path = tmp_path / "approvals.json"
    monkeypatch.setenv("MQ_APPROVAL_STORE", str(path))
    monkeypatch.setenv("MQ_AUDIT_LOG", str(tmp_path / "audit.log"))
    svc, _ = make(ApprovalStore(str(path)))
    req = svc.put_message("PAY.OUT", "hello")["approval_id"]

    assert approve_cli.main(["list"]) == 0
    assert req in capsys.readouterr().out
    assert approve_cli.main(["approve", req, "--approver", "agent-1", "--reason", "self"]) == 1
    assert approve_cli.main(["approve", req, "--approver", "jane", "--reason", "invoice 4411"]) == 0
    assert svc.put_message("PAY.OUT", "hello", approval_id=req)["ok"] is True
    decision = json.loads((tmp_path / "audit.log").read_text().splitlines()[-1])
    assert decision["actor"] == "human:jane" and decision["outcome"] == "approved"


def test_cli_requires_store(monkeypatch, capsys):
    monkeypatch.delenv("MQ_APPROVAL_STORE", raising=False)
    assert approve_cli.main(["list"]) == 2
