import argparse
import json
import math
import statistics
import tempfile
from dataclasses import replace
from pathlib import Path

from member_agent.agent import Agent
from member_agent.config import Settings
from member_agent.errors import AgentError
from member_agent.routing import claim_reference, rule_intent
from .runner import mock_server


def percentile(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def measure(settings, case, samples):
    agent = Agent(settings)
    cold, follow_up, network, hits = [], [], [], []
    for _ in range(samples):
        scope = agent.new_session(case["tenant"], case["member_id"])
        message = case["turns"][0]["text"]
        first = agent.ask(scope, message)
        second = agent.ask(scope, message)
        if first.behaviour != "answer" or second.behaviour != "answer":
            raise AgentError("benchmark_answer_failed")
        cold.append(first.metrics["turn_seconds"])
        follow_up.append(second.metrics["turn_seconds"])
        network.append(second.metrics["http_requests"])
        hits.append(second.metrics["cache_hits"])
    return {"samples": samples, "cold_median_seconds": statistics.median(cold),
            "cold_p95_seconds": percentile(cold), "follow_up_median_seconds": statistics.median(follow_up),
            "follow_up_p95_seconds": percentile(follow_up), "follow_up_http_requests": sum(network),
            "follow_up_cache_hits": sum(hits), "cache_ttl_seconds": settings.claims_cache_ttl}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Paired fresh versus scoped-cache claim benchmark")
    parser.add_argument("--pack-dir")
    parser.add_argument("--env-file")
    parser.add_argument("--demo-credentials", action="store_true")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--output", default="reports/performance.json")
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 20:
        parser.error("samples must be between 1 and 20")
    try:
        settings = Settings.from_env(args.pack_dir, args.demo_credentials, env_file=args.env_file)
        cases = json.loads((settings.pack_dir / "eval/conversations.json").read_text())["cases"]
        case = next((item for item in cases if len(item["turns"]) == 1
                     and claim_reference(item["turns"][0]["text"])
                     and rule_intent(item["turns"][0]["text"]).kind == "claim"), None)
        if case is None:
            raise AgentError("benchmark_claim_case_required")
        results = {}
        with tempfile.TemporaryDirectory(prefix="member-perf-") as temp:
            for name, ttl in (("fresh_reads", 0), ("scoped_cache", 30)):
                cfg = replace(settings, state_path=Path(temp) / (name + ".sqlite3"), claims_cache_ttl=ttl)
                # Separate mock instances give each configuration the same initial quota state.
                with mock_server(cfg, True) as configured:
                    results[name] = measure(configured, case, args.samples)
        before = results["fresh_reads"]["follow_up_median_seconds"]
        after = results["scoped_cache"]["follow_up_median_seconds"]
        report = {"model_provider": settings.model_provider, "live_model_evaluated": settings.model_provider != "rules",
                  "case_id": case["id"], "writes_performed": False, "results": results,
                  "median_follow_up_reduction_percent": round(100 * (before - after) / before, 2),
                  "limitations": "Small sequential local sample of one direct-claim question. "
                                 "Cold paginated counts, 250-rep scale and production availability are not certified."}
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0
    except (AgentError, OSError, ValueError, KeyError) as exc:
        print("Error: " + (exc.code if isinstance(exc, AgentError) else "benchmark_setup_failed"))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
