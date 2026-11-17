"""Deterministic release gate for mcp-ibm-mq-server.

Runs every case in evals/cases.yaml against an in-memory queue manager,
scores the results, compares them with evals/thresholds.yaml and exits
non-zero when any score is below its floor, so CI blocks the merge.

No model or API key is needed: this checks the control layer itself.

    python evals/run_evals.py                  # human-readable report
    python evals/run_evals.py --json out.json  # also write a JSON report
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from mcp_ibm_mq.audit import AuditLog
from mcp_ibm_mq.mq_client import InMemoryClient
from mcp_ibm_mq.policy import Policy
from mcp_ibm_mq.service import MQService

HERE = Path(__file__).resolve().parent
TOOLS = {"list_queues", "queue_depth", "browse_messages", "put_message", "get_message"}


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((HERE / name).read_text(encoding="utf-8"))


def build(profile: dict[str, Any], seed: dict[str, list[str]]):
    policy = Policy(
        read_queues=frozenset(profile.get("read_queues", [])),
        write_queues=frozenset(profile.get("write_queues", [])),
        read_only=bool(profile.get("read_only", True)),
        allow_destructive_get=bool(profile.get("allow_destructive_get", False)),
        max_message_bytes=int(profile.get("max_message_bytes", 64 * 1024)),
        max_browse=int(profile.get("max_browse", 20)),
    )
    queues = {q: [m.encode("utf-8") for m in (msgs or [])] for q, msgs in seed.items()}
    stream = io.StringIO()
    service = MQService(InMemoryClient(queues), policy, AuditLog(path="", stream=stream))
    return service, stream


def expand(args: dict[str, Any]) -> dict[str, Any]:
    """Allow {repeat: "x", times: N} so large payloads stay readable in YAML."""
    out = {}
    for key, value in args.items():
        if isinstance(value, dict) and "repeat" in value:
            value = str(value["repeat"]) * int(value["times"])
        out[key] = value
    return out


def depths(service: MQService, queues: list[str]) -> dict[str, int]:
    return {q: service.client.depth(q) for q in queues}


def check(case: dict[str, Any], result: dict[str, Any], after: dict[str, int]) -> list[str]:
    exp, problems = case.get("expect", {}), []
    if result.get("ok") is not exp.get("ok"):
        problems.append(f"ok={result.get('ok')} expected {exp.get('ok')}")
    if "error_contains" in exp and exp["error_contains"] not in str(result.get("error", "")):
        problems.append(f"error does not mention '{exp['error_contains']}'")
    for field, want in (exp.get("field_equals") or {}).items():
        if result.get(field) != want:
            problems.append(f"{field}={result.get(field)!r} expected {want!r}")
    msgs = result.get("messages")
    if "message_count" in exp and (msgs is None or len(msgs) != exp["message_count"]):
        problems.append(f"returned {None if msgs is None else len(msgs)} messages, expected {exp['message_count']}")
    if "message_count_max" in exp and (msgs is None or len(msgs) > exp["message_count_max"]):
        problems.append(f"returned {None if msgs is None else len(msgs)} messages, cap is {exp['message_count_max']}")
    for queue, want in (exp.get("depth_after") or {}).items():
        if after.get(queue) != want:
            problems.append(f"{queue} depth {after.get(queue)} expected {want}")
    return problems


def run() -> dict[str, Any]:
    spec, thresholds = load("cases.yaml"), load("thresholds.yaml")["deterministic"]
    seed, markers = spec["seed"], spec.get("sensitive_markers", [])
    queues = sorted(seed)
    results, tally = [], {}

    def bump(metric: str, passed: bool) -> None:
        good, total = tally.get(metric, (0, 0))
        tally[metric] = (good + passed, total + 1)

    for case in spec["cases"]:
        if case["tool"] not in TOOLS:
            raise SystemExit(f"{case['id']}: unknown tool {case['tool']}")
        service, stream = build(spec["profiles"][case["profile"]], seed)
        before = depths(service, queues)
        result = getattr(service, case["tool"])(**expand(case.get("args", {})))
        after = depths(service, queues)
        log = stream.getvalue()

        problems = check(case, result, after)
        audited = any(json.loads(line).get("tool") == case["tool"] for line in log.splitlines())
        leaked = [m for m in markers if m in log]
        side_effect = (result.get("ok") is False) and before != after

        bump(case["category"], not problems)
        bump("audit_coverage", audited)
        bump("no_leakage", not leaked)
        if result.get("ok") is False:
            bump("no_side_effects", not side_effect)
        if not audited:
            problems.append("no audit line")
        if leaked:
            problems.append(f"audit log leaked {leaked}")
        if side_effect:
            problems.append(f"refused call changed queues {before} -> {after}")
        results.append({"id": case["id"], "category": case["category"], "passed": not problems, "problems": problems})

    scores = {m: round(g / t, 4) for m, (g, t) in tally.items()}
    failures = {m: (scores.get(m), floor) for m, floor in thresholds.items() if scores.get(m, 0.0) < floor}
    return {"scores": scores, "counts": tally, "thresholds": thresholds, "failures": failures, "cases": results}


def report(r: dict[str, Any]) -> str:
    lines = ["## Release gate: deterministic evals", "", "| Score | Result | Floor | Status |", "| --- | --- | --- | --- |"]
    for metric, floor in r["thresholds"].items():
        good, total = r["counts"].get(metric, (0, 0))
        status = "FAIL" if metric in r["failures"] else "pass"
        lines.append(f"| {metric} | {good}/{total} ({r['scores'].get(metric, 0):.0%}) | {floor:.0%} | {status} |")
    failed = [c for c in r["cases"] if not c["passed"]]
    lines += ["", f"{len(r['cases']) - len(failed)}/{len(r['cases'])} cases passed."]
    for c in failed:
        lines.append(f"- **{c['id']}** ({c['category']}): " + "; ".join(c["problems"]))
    lines.append("")
    lines.append("Gate: **BLOCKED**" if r["failures"] else "Gate: **open**")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", help="write the full report to this file")
    opts = parser.parse_args()
    r = run()
    text = report(r)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if opts.json:
        Path(opts.json).write_text(json.dumps(r, indent=2), encoding="utf-8")
    return 1 if r["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
