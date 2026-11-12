import io
import json
from types import SimpleNamespace

from mcp_ibm_mq import trace_cli
from mcp_ibm_mq.approvals import ApprovalStore
from mcp_ibm_mq.audit import AuditLog
from mcp_ibm_mq.mq_client import InMemoryClient
from mcp_ibm_mq.policy import Policy
from mcp_ibm_mq.service import MQService
from mcp_ibm_mq.tracing import incoming_traceparent, parse_traceparent, start_span, traceparent_from_context

TP = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_parse_and_continue_trace():
    assert parse_traceparent(TP) == ("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7", "01")
    span = start_span(TP)
    assert span.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736" and span.parent_span_id == "00f067aa0ba902b7"
    assert span.traceparent.startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-") and span.span_id != "00f067aa0ba902b7"


def test_bad_traceparents_start_fresh():
    for bad in (None, "", "garbage", "01-" + TP[3:], TP.upper() + "x", 42):
        span = start_span(bad)
        assert span.parent_span_id is None and len(span.trace_id) == 32


def test_traceparent_from_meta_then_headers():
    meta_ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"traceparent": TP}), headers={})
    hdr_ctx = SimpleNamespace(request_context=SimpleNamespace(meta=None), headers={"traceparent": TP})
    none_ctx = SimpleNamespace(request_context=SimpleNamespace(meta=None), headers=None)
    assert traceparent_from_context(meta_ctx) == TP
    assert traceparent_from_context(hdr_ctx) == TP
    assert traceparent_from_context(none_ctx) is None


def test_approval_flow_and_replay_share_one_trace(tmp_path):
    log = tmp_path / "audit.jsonl"
    policy = Policy(write_queues=frozenset({"PAY.OUT"}), approval_queues=frozenset({"PAY.OUT"}), read_only=False)
    audit = AuditLog(path=str(log))
    audit.actor = "agent-1"
    svc = MQService(InMemoryClient(), policy, audit, ApprovalStore())
    token = incoming_traceparent.set(TP)
    try:
        req = svc.put_message("PAY.OUT", "x")["approval_id"]
        assert svc.approvals.get(req)["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        svc.approvals.decide(req, approver="jane", approve=True, reason="ok")
        assert svc.put_message("PAY.OUT", "x", approval_id=req)["ok"]
    finally:
        incoming_traceparent.reset(token)
    entries = trace_cli.load(log.read_text().splitlines(), "4bf92f3577b34da6a3ce929d0e0e4736")
    assert [e["outcome"] for e in entries] == ["approval_required", "ok"]
    text = trace_cli.render(entries)
    assert "2 event(s)" in text and "PAY.OUT" in text


def test_trace_cli(tmp_path, capsys):
    log = tmp_path / "audit.jsonl"
    log.write_text(json.dumps({"ts": "2026-12-08T14:00:00.000Z", "trace_id": "abc", "actor": "a", "tool": "queue_depth",
                               "outcome": "ok", "duration_ms": 1.5}) + "\nnot json\n")
    assert trace_cli.main(["abc", "--log", str(log)]) == 0
    assert "queue_depth" in capsys.readouterr().out
    assert trace_cli.main(["zzz", "--log", str(log)]) == 1


def test_audit_timestamps_have_milliseconds():
    stream = io.StringIO()
    AuditLog(path="", stream=stream).record("t", "ok")
    assert json.loads(stream.getvalue())["ts"].endswith("Z") and "." in json.loads(stream.getvalue())["ts"]
