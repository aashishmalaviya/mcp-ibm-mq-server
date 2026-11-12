"""Approver CLI: the human side of the approval hook.

Runs outside the agent and reads the same store as the server
(MQ_APPROVAL_STORE). The agent has no tool that can call any of this.

    mcp-ibm-mq-approvals list [--all]
    mcp-ibm-mq-approvals show APPROVAL_ID
    mcp-ibm-mq-approvals approve APPROVAL_ID --approver "Jane Doe" --reason "Matches invoice 4411"
    mcp-ibm-mq-approvals reject  APPROVAL_ID --approver "Jane Doe" --reason "Amount looks wrong"

Every decision is written to the audit log with the approver's name and reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .approvals import PENDING, ApprovalError, ApprovalStore
from .audit import AuditLog


def _fmt(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts)) if ts else "-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-ibm-mq-approvals", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="list pending requests")
    ls.add_argument("--all", action="store_true", help="include decided and expired requests")
    sh = sub.add_parser("show", help="show one request")
    sh.add_argument("approval_id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a pending request")
        p.add_argument("approval_id")
        p.add_argument("--approver", required=True, help="your name or user ID")
        p.add_argument("--reason", required=True, help="why; recorded in the audit log")
    args = parser.parse_args(argv)

    if not os.environ.get("MQ_APPROVAL_STORE"):
        print("Set MQ_APPROVAL_STORE to the same file the server uses.", file=sys.stderr)
        return 2
    store = ApprovalStore.from_env()

    if args.cmd == "list":
        rows = store.list(None if args.all else PENDING)
        if not rows:
            print("No requests." if args.all else "No pending requests.")
        for r in rows:
            print(f"{r['id']}  {r['status']:<9} {r['queue']:<24} {r['bytes']:>7} bytes  "
                  f"by {r['requested_by']}  expires {_fmt(r['expires_at'])}")
        return 0

    if args.cmd == "show":
        req = store.get(args.approval_id)
        if req is None:
            print(f"No approval request '{args.approval_id}'.", file=sys.stderr)
            return 1
        print(json.dumps(req, indent=2, sort_keys=True))
        return 0

    approve = args.cmd == "approve"
    try:
        req = store.decide(args.approval_id, approver=args.approver, approve=approve, reason=args.reason)
    except ApprovalError as err:
        print(f"Refused: {err}", file=sys.stderr)
        return 1
    audit = AuditLog()
    audit.actor = f"human:{args.approver}"
    audit.record("approval_decision", req["status"], approval_id=req["id"], queue=req["queue"],
                 requested_by=req["requested_by"], reason=req["reason"], trace_id=req.get("trace_id"))
    print(f"{req['id']} {req['status']} by {req['decided_by']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
