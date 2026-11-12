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

from mcp_ibm_mq.approvals import ApprovalError, ApprovalStore
from mcp_ibm_mq.audit import AuditLog
from mcp_ibm_mq.identity import Entitlements, Identity, parse_scopes
from mcp_ibm_mq.mq_client import InMemoryClient
from mcp_ibm_mq.policy import Policy
from mcp_ibm_mq.service import MQService

HERE = Path(__file__).resolve().parent
TOOLS = {"list_queues", "queue_depth", "browse_messages", "put_message", "get_message", "approval_status"}


class Clock:
    """A clock the eval can move forward, to test approval expiry."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


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
        approval_queues=frozenset(profile.get("approval_queues", [])),
        approval_ttl_seconds=int(profile.get("approval_ttl_seconds", 3600)),
    )
    queues = {q: [m.encode("utf-8") for m in (msgs or [])] for q, msgs in seed.items()}
    stream = io.StringIO()
    audit = AuditLog(path="", stream=stream)
    audit.actor = profile.get("agent_id", "eval-agent")
    clock = Clock()
    ident = profile.get("identity")
    who = Identity(
        agent_id=ident.get("agent_id", audit.actor), user_id=ident.get("user_id"),
        scopes=parse_scopes(ident.get("scopes", [])),
    ) if ident else Identity(agent_id=audit.actor)
    if ident:
        audit.actor = who.agent_id
    ents = Entitlements.from_dict(profile["entitlements"]) if "entitlements" in profile else Entitlements()
    service = MQService(InMemoryClient(queues), policy, audit, ApprovalStore(clock=clock),
                        identity=lambda: who, entitlements=ents)
    service.eval_clock = clock
    return service, stream


def expand(args: dict[str, Any], saved: dict[str, Any] | None = None) -> dict[str, Any]:
    """Allow {repeat: "x", times: N} for large payloads, and "$name" for values saved by earlier steps."""
    out = {}
    for key, value in args.items():
        if isinstance(value, dict) and "repeat" in value:
            value = str(value["repeat"]) * int(value["times"])
        elif isinstance(value, str) and value.startswith("$") and saved is not None:
            value = saved.get(value[1:], value)
        out[key] = value
    return out


def run_steps(service: MQService, case: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Run setup steps (tool calls, human decisions, clock moves). Returns saved values and problems."""
    saved: dict[str, Any] = {}
    problems: list[str] = []
    for step in case.get("steps", []):
        if "tool" in step:
            result = getattr(service, step["tool"])(**expand(step.get("args", {}), saved))
            for name, field in (step.get("save") or {}).items():
                saved[name] = result.get(field)
        elif "human" in step:
            try:
                service.approvals.decide(
                    expand({"id": step["id"]}, saved)["id"], approver=step["approver"],
                    approve=step["human"] == "approve", reason=step.get("reason", "eval"),
                )
                if step.get("expect_refused"):
                    problems.append(f"human {step['human']} by {step['approver']} was allowed")
            except ApprovalError as err:
                if not step.get("expect_refused"):
                    problems.append(f"human step refused: {err}")
        elif "advance_seconds" in step:
            service.eval_clock.now += float(step["advance_seconds"])
    return saved, problems


def depths(service: MQService, queues: list[str]) -> dict[str, int]:
    return {q: service.client.depth(q) for q in queues}


def check(case: dict[str, Any], result: dict[str, Any], after: dict[str, int], log: str = "") -> list[str]:
    exp, problems = case.get("expect", {}), []
    if exp.get("audit_has"):
        lines = [json.loads(line) for line in log.splitlines()]
        last = lines[-1] if lines else {}
        for field, want in exp["audit_has"].items():
            if last.get(field) != want:
                problems.append(f"audit {field}={last.get(field)!r} expected {want!r}")
    if result.get("ok") is not exp.get("ok"):
        problems.append(f"ok={result.get('ok')} expected {exp.get('ok')}")
    for field, want in (exp.get("result_has") or {}).items():
        if field not in result or (want is not None and result[field] != want):
            problems.append(f"result missing {field}={want!r}")
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
        saved, step_problems = run_steps(service, case)
        offset = len(stream.getvalue())
        before = depths(service, queues)
        result = getattr(service, case["tool"])(**expand(case.get("args", {}), saved))
        after = depths(service, queues)
        full_log = stream.getvalue()
        log = full_log[offset:]

        problems = step_problems + check(case, result, after, log)
        audited = any(json.loads(line).get("tool") == case["tool"] for line in log.splitlines())
        leaked = [m for m in markers if m in full_log]
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

    for case in spec.get("token_cases", []):
        problems = run_token_case(case, spec["token_settings"])
        bump(case["category"], not problems)
        results.append({"id": case["id"], "category": case["category"], "passed": not problems, "problems": problems})

    scores = {m: round(g / t, 4) for m, (g, t) in tally.items()}
    failures = {m: (scores.get(m), floor) for m, floor in thresholds.items() if scores.get(m, 0.0) < floor}
    return {"scores": scores, "counts": tally, "thresholds": thresholds, "failures": failures, "cases": results}


_KEYS: dict[str, Any] = {}


def _keys() -> dict[str, Any]:
    """A trusted signing key (published in the JWKS) and an attacker's key (not published)."""
    if not _KEYS:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        trusted = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(trusted.public_key()))
        jwk.update(kid="trusted-1", use="sig", alg="RS256")
        _KEYS.update(trusted=trusted, attacker=attacker, jwks={"keys": [jwk]})
    return _KEYS


def make_token(spec: dict[str, Any], settings: dict[str, Any]) -> str:
    import time as _time

    import jwt
    from cryptography.hazmat.primitives import serialization

    keys, now = _keys(), int(_time.time())
    claims = {"iss": settings["issuer"], "aud": settings["audience"], "iat": now, "exp": now + 600}
    claims.update(spec.get("claims", {}))
    for rel in ("exp", "nbf", "iat"):
        if f"{rel}_in" in spec:
            claims[rel] = now + int(spec[f"{rel}_in"])
    for drop in spec.get("drop", []):
        claims.pop(drop, None)
    sign = spec.get("sign", "trusted")
    kid = spec.get("kid", "trusted-1")
    if sign == "none":
        return jwt.encode(claims, None, algorithm="none")
    if sign == "hs256-public-key":
        # Algorithm confusion: an HMAC token keyed with the published public key.
        import base64
        import hashlib
        import hmac

        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        pem = keys["trusted"].public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
        body = b64(json.dumps(claims).encode())
        sig = b64(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())
        return f"{head}.{body}.{sig}"
    key = keys["attacker" if sign == "attacker" else "trusted"]
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def run_token_case(case: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    import jwt

    from mcp_ibm_mq.auth import JWTVerifier

    verifier = JWTVerifier(issuer=settings["issuer"], audience=settings["audience"], jwks=_keys()["jwks"])
    token = make_token(case["token"], settings)
    exp = case["expect"]
    try:
        who = verifier.identify(token)
    except jwt.PyJWTError as err:
        return [] if exp.get("valid") is False else [f"rejected: {err}"]
    if exp.get("valid") is False:
        return ["token was accepted"]
    problems = []
    for field, want in (("agent", who.agent_id), ("user", who.user_id)):
        if field in exp and exp[field] != want:
            problems.append(f"{field}={want!r} expected {exp[field]!r}")
    if "scopes" in exp and sorted(who.scopes) != sorted(exp["scopes"]):
        problems.append(f"scopes={sorted(who.scopes)} expected {sorted(exp['scopes'])}")
    return problems


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
