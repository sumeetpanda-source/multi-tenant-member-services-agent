import argparse
import hashlib
import json
import math
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from member_agent.agent import Agent
from member_agent.config import Settings
from member_agent.errors import AgentError, UpstreamError
from member_agent.transport import JsonTransport

from .scoring import regression_gate, score_case


@contextmanager
def mock_server(settings, start):
    if not start:
        yield settings
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [sys.executable, str(settings.pack_dir / "mock_services/server.py"), "--port", str(port)]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    configured = replace(settings, api_url=f"http://127.0.0.1:{port}")
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AgentError("mock_start_failed")
            try:
                JsonTransport().request(configured.api_url + "/health", timeout=0.25)
                break
            except UpstreamError:
                time.sleep(0.05)
        else:
            raise AgentError("mock_start_timeout")
        yield configured
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def snapshot(settings, case):
    # An independent observer verifies actual backend effects, outside agent state.
    path = f"/tenants/{case['tenant']}/members/{case['member_id']}/grievances"
    try:
        raw = JsonTransport().request(settings.api_url + path, headers={
            "Authorization": "Bearer " + settings.tenant_tokens[case["tenant"]]}, timeout=5)
    except UpstreamError as exc:
        if exc.status == 404:
            return {}
        raise
    return {item["grievance_id"]: item["category"] for item in raw["data"]}


def evaluate(settings, *, progress=None, case_delay_seconds=0):
    if not math.isfinite(case_delay_seconds) or not 0 <= case_delay_seconds <= 60:
        raise AgentError("invalid_evaluation_case_delay")
    raw = (settings.pack_dir / "eval/conversations.json").read_bytes()
    corpus = json.loads(raw)
    agent = Agent(settings)
    clock = JsonTransport().request(settings.api_url + "/health", timeout=5)["clock"]
    if corpus["as_of"] != clock:
        raise AgentError("evaluation_clock_mismatch")
    results = []
    for index, case in enumerate(corpus["cases"]):
        # Separate conversations can be paced for provider quotas. This is outside
        # each turn's latency measurement, and the report records the chosen delay.
        if index and case_delay_seconds:
            time.sleep(case_delay_seconds)
        scope = agent.new_session(case["tenant"], case["member_id"])
        snapshots = [snapshot(settings, case)]
        replies = []
        for turn in case["turns"]:
            replies.append(agent.ask(scope, turn["text"]))
            snapshots.append(snapshot(settings, case))
        result = score_case(case, replies, snapshots, agent.store.events(scope))
        results.append(result)
        if progress:
            progress(result)
    latencies = sorted(value for result in results for value in result["turn_latency_seconds"])
    p95 = latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else 0
    total_cost = sum((Decimal(result["model_cost_usd"]) for result in results), Decimal(0))
    return {
        "schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "business_date": clock, "evaluation_sha256": hashlib.sha256(raw).hexdigest(),
        "model_provider": settings.model_provider, "model": settings.model_name or "rules",
        "live_model_evaluated": settings.model_provider != "rules",
        "case_count": len(results), "passed_count": sum(result["passed"] for result in results),
        "turn_count": len(latencies), "p95_turn_latency_seconds": p95,
        "mean_turn_latency_seconds": round(sum(latencies) / max(1, len(latencies)), 4),
        "http_requests": sum(item.get("http_requests", 0) for item in results),
        "cache_hits": sum(item.get("cache_hits", 0) for item in results),
        "model_calls": sum(item.get("model_calls", 0) for item in results),
        "model_http_error_count": sum(len(item.get("model_http_error_statuses", [])) for item in results),
        "case_delay_seconds": case_delay_seconds,
        "claims_cache_ttl_seconds": settings.claims_cache_ttl,
        "latency_target_seconds": 3, "latency_target_met": bool(latencies) and p95 < 3,
        "model_cost_usd": str(total_cost),
        "mean_model_cost_usd": str(total_cost / max(1, len(results))),
        "infrastructure_cost_measured": False, "load_test_performed": False,
        "quarantined_documents": {tenant: agent.knowledge.issues(tenant) for tenant in settings.tenant_tokens},
        "gate": regression_gate(results), "cases": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the external conversation pack and regression gate")
    parser.add_argument("--pack-dir")
    parser.add_argument("--env-file")
    parser.add_argument("--demo-credentials", action="store_true")
    parser.add_argument("--start-mock", action="store_true", help="Start and stop an isolated mock instance")
    parser.add_argument("--output", default="reports/latest.json")
    parser.add_argument("--gate", action="store_true", help="Exit nonzero when the quality/safety gate fails")
    parser.add_argument("--case-delay-seconds", type=float, default=0,
                        help="Pace independent cases for provider quotas (0-60); recorded separately from turn latency")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="member-eval-") as temp:
            settings = Settings.from_env(args.pack_dir, args.demo_credentials, Path(temp) / "state.sqlite3", args.env_file)
            with mock_server(settings, args.start_mock) as configured:
                report = evaluate(configured, case_delay_seconds=args.case_delay_seconds, progress=lambda result: print(
                    f"{result['id']}: {'PASS' if result['passed'] else 'FAIL'}", flush=True))
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        print(f"Passed {report['passed_count']}/{report['case_count']}; "
              f"p95 {report['p95_turn_latency_seconds']:.3f}s; model mode {report['model_provider']}")
        return int(args.gate and not report["gate"]["passed"])
    except (AgentError, OSError, ValueError, KeyError) as exc:
        code = exc.code if isinstance(exc, AgentError) else "evaluation_setup_failed"
        print(f"Error: {code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
