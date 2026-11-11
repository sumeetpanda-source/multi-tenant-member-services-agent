import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

from member_agent.config import Settings
from member_agent.errors import AgentError
from .runner import evaluate, mock_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="CI entry point for tests and optional external-pack regression")
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--pack-dir")
    parser.add_argument("--env-file")
    parser.add_argument("--demo-credentials", action="store_true")
    parser.add_argument("--start-mock", action="store_true")
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args(argv)
    tests_dir = Path(__file__).resolve().parent.parent / "tests"
    if not tests_dir.is_dir():
        print("Error: test_sources_not_found", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.discover(str(tests_dir))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    security = {"test_count": result.testsRun, "failure_count": len(result.failures),
                "error_count": len(result.errors), "skipped_count": len(result.skipped),
                "passed": passed, "live_model_tested": False}
    (output / "checks.json").write_text(json.dumps(security, indent=2) + "\n", encoding="utf-8")
    if not passed:
        return 1
    if args.integration:
        try:
            with tempfile.TemporaryDirectory(prefix="member-checks-") as temp:
                settings = Settings.from_env(args.pack_dir, args.demo_credentials,
                                             Path(temp) / "state.sqlite3", args.env_file)
                with mock_server(settings, args.start_mock) as configured:
                    report = evaluate(configured, progress=lambda item: print(
                        f"{item['id']}: {'PASS' if item['passed'] else 'FAIL'}", flush=True))
            (output / "latest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"Conversation cases: {report['passed_count']}/{report['case_count']}. "
                  f"Regression gate: {'PASS' if report['gate']['passed'] else 'FAIL'}.")
            return int(not report["gate"]["passed"])
        except (AgentError, OSError, ValueError, KeyError) as exc:
            code = exc.code if isinstance(exc, AgentError) else "integration_setup_failed"
            print(f"Error: {code}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
