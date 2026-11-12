"""Replay one trace from the audit log.

    mcp-ibm-mq-trace TRACE_ID [--log /var/log/mcp-ibm-mq/audit.jsonl] [--json]

Prints every audit line for that trace in time order: which agent, acting for
whom, called which tool on which queue, what the policy decided and why, how
long it took, and any human approval decision on the same trace. This is the
"what did the agent actually do?" view for an incident review.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable


def load(lines: Iterable[str], trace_id: str) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("trace_id") == trace_id:
            out.append(entry)
    return sorted(out, key=lambda e: e.get("ts", ""))


def render(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No audit lines for this trace."
    rows = []
    for e in entries:
        who = e.get("actor", "?") + (f" for {e['on_behalf_of']}" if e.get("on_behalf_of") else "")
        what = e.get("tool", "?") + (f" {e['queue']}" if e.get("queue") else "")
        took = f"{e['duration_ms']:.1f} ms" if isinstance(e.get("duration_ms"), (int, float)) else ""
        why = e.get("reason") or (f"approval {e['approval_id']}" if e.get("approval_id") else "")
        rows.append(f"{e.get('ts', ''):<25} {e.get('outcome', ''):<17} {what:<34} {who:<30} {took:>9}  {why}".rstrip())
    first = entries[0]
    head = f"Trace {first['trace_id']}: {len(entries)} event(s)"
    return "\n".join([head, *rows])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-ibm-mq-trace", description=__doc__.splitlines()[0])
    parser.add_argument("trace_id")
    parser.add_argument("--log", default=os.environ.get("MQ_AUDIT_LOG"), help="audit log file (default MQ_AUDIT_LOG)")
    parser.add_argument("--json", action="store_true", help="print the raw audit lines")
    args = parser.parse_args(argv)
    if not args.log:
        print("Give --log or set MQ_AUDIT_LOG.", file=sys.stderr)
        return 2
    with open(args.log, encoding="utf-8") as fh:
        entries = load(fh, args.trace_id.lower())
    if args.json:
        for e in entries:
            print(json.dumps(e, sort_keys=True))
    else:
        print(render(entries))
    return 0 if entries else 1


if __name__ == "__main__":
    sys.exit(main())
