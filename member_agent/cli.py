import argparse
import json
import sys

from .agent import Agent
from .config import Settings
from .errors import AgentError


def parser():
    result = argparse.ArgumentParser(description="Member-services representative console")
    result.add_argument("--pack-dir", help="External assignment pack directory (or PACK_DIR)")
    result.add_argument("--state", help="Private local state database")
    result.add_argument("--env-file", help="Local environment file (defaults to .env when present)")
    result.add_argument("--demo-credentials", action="store_true", help="Read only mock credentials from the local pack")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("chat", "ask", "trace", "operation"):
        command = commands.add_parser(name)
        command.add_argument("--tenant", required=True)
        command.add_argument("--member", required=True)
        command.add_argument("--session", required=name in {"trace", "operation"}, help="Resume an existing scoped conversation")
        if name == "operation":
            command.add_argument("--operation-id", help="Inspect a specific operation; defaults to the latest")
        if name == "ask":
            command.add_argument("--message", help="Question; omit to read from standard input")
            command.add_argument("--json", action="store_true")
    command = commands.add_parser("check-kb")
    command.add_argument("--tenant", required=True)
    return result


def show(reply, as_json=False):
    if as_json:
        print(json.dumps(reply.to_dict(), ensure_ascii=False))
    else:
        print(reply.text)
        for citation in reply.citations:
            print(f"  Source: {citation.source} / {citation.section} ({citation.plan_year})")
        print(f"  Session: {reply.session_id}")
        if reply.operation_id:
            print(f"  Operation: {reply.operation_id}")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = Settings.from_env(args.pack_dir, args.demo_credentials, args.state, args.env_file)
        agent = Agent(settings)
        if args.command == "check-kb":
            if args.tenant not in settings.tenant_tokens:
                raise AgentError("unknown_tenant")
            print(json.dumps({"tenant": args.tenant, "quarantined": agent.knowledge.issues(args.tenant)}, indent=2))
            return 0
        scope = (agent.resume_session(args.session, args.tenant, args.member) if args.session
                 else agent.new_session(args.tenant, args.member))
        if args.command == "trace":
            print(json.dumps(agent.store.events(scope), indent=2))
        elif args.command == "operation":
            operation = agent.store.operation(scope, args.operation_id)
            print(json.dumps(None if operation is None else {
                "operation_id": operation["id"], "status": operation["status"],
                "result": json.loads(operation["result"]) if operation["result"] else None,
            }, indent=2))
        elif args.command == "ask":
            message = args.message if args.message is not None else sys.stdin.read()
            show(agent.ask(scope, message), args.json)
        else:
            print(f"Session: {scope.session_id} | model mode: {settings.model_provider}")
            print("Enter a question. Use confirm or cancel for a grievance draft. /exit closes the console.")
            while True:
                try:
                    message = input("Rep> ").strip()
                except EOFError:
                    break
                if message == "/exit":
                    break
                if message:
                    show(agent.ask(scope, message))
        return 0
    except AgentError as exc:
        print(f"Error: {exc.code}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print("Error: invalid_configuration_or_state", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
