import json
import math
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .errors import AgentError


def environment(env_file=None):
    values = {}
    path = Path(env_file or ".env")
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
                raise AgentError("invalid_env_file")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key.strip()] = value
    elif env_file is not None:
        raise AgentError("env_file_not_found")
    values.update(os.environ)
    return values


def validate_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or (parsed.scheme == "http" and not local) or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise AgentError("invalid_endpoint")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    pack_dir: Path
    api_url: str = "http://127.0.0.1:8090"
    tenant_tokens: dict[str, str] = field(default_factory=dict, repr=False)
    directory_key: str = field(default="", repr=False)
    state_path: Path = Path("runtime/member_agent.sqlite3")
    model_provider: str = "rules"
    model_name: str = ""
    model_endpoint: str = "http://127.0.0.1:11434/api/chat"
    model_key: str = field(default="", repr=False)
    reasoning_effort: str = ""
    input_rate: Decimal = Decimal("0")
    output_rate: Decimal = Decimal("0")
    max_steps: int = 80
    max_model_tokens: int = 16000
    max_cost: Decimal = Decimal("0.025")
    max_active_seconds: float = 30
    turn_timeout: float = 15
    approval_ttl: float = 600
    max_retries: int = 2
    claims_cache_ttl: float = 30

    def __post_init__(self):
        validate_endpoint(self.api_url)
        validate_endpoint(self.model_endpoint)
        if self.model_provider not in {"rules", "ollama", "openai", "chat-completions"}:
            raise AgentError("invalid_model_provider")
        if self.model_provider != "rules" and not self.model_name:
            raise AgentError("model_name_required")
        if self.reasoning_effort not in {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise AgentError("invalid_model_reasoning_effort")
        if (not all(isinstance(v, Decimal) and v.is_finite()
                    for v in (self.input_rate, self.output_rate, self.max_cost))
                or min(self.input_rate, self.output_rate, self.max_cost) < 0):
            raise AgentError("invalid_budget_configuration")
        if self.model_provider in {"openai", "chat-completions"} and (self.input_rate <= 0 or self.output_rate <= 0):
            raise AgentError("model_pricing_required")
        if self.model_provider == "openai" and not self.model_key:
            raise AgentError("openai_api_key_required")
        if not isinstance(self.tenant_tokens, dict) or not self.tenant_tokens or any(
            not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key)
            or not isinstance(value, str) or not value.strip()
            for key, value in self.tenant_tokens.items()
        ):
            raise AgentError("tenant_credentials_required")
        if any(not isinstance(value, str) or "\r" in value or "\n" in value
               for value in (*self.tenant_tokens.values(), self.model_key, self.directory_key)):
            raise AgentError("invalid_credentials")
        if any(type(value) is not int for value in (self.max_steps, self.max_model_tokens, self.max_retries)):
            raise AgentError("invalid_budget_configuration")
        if (min(self.max_steps, self.max_model_tokens, self.max_active_seconds,
                self.turn_timeout, self.approval_ttl) <= 0
                or not self.max_cost.is_finite() or self.max_cost < 0
                or not self.input_rate.is_finite() or not self.output_rate.is_finite()
                or min(self.input_rate, self.output_rate, self.max_retries) < 0
                or not all(math.isfinite(v) for v in (self.max_active_seconds, self.turn_timeout, self.approval_ttl,
                                                     self.claims_cache_ttl))
                or not 0 <= self.claims_cache_ttl <= 60):
            raise AgentError("invalid_budget_configuration")

    @classmethod
    def from_env(cls, pack_dir=None, demo_credentials=False, state_path=None, env_file=None):
        env = environment(env_file)
        pack = Path(pack_dir or env.get("PACK_DIR") or ".").resolve()
        tokens = json.loads(env.get("TENANT_TOKENS_JSON") or "{}")
        directory = env.get("DIRECTORY_API_KEY", "")
        if demo_credentials:
            # Read configuration only. Patient records are accessed over HTTP.
            cfg = json.loads((pack / "mock_services/data/tenants.json").read_text(encoding="utf-8"))
            tokens = {tid: info["api_token"] for tid, info in cfg["tenants"].items()}
            directory = cfg["directory"]["api_key"]
        provider = env.get("MODEL_PROVIDER", "rules")
        default_endpoint = ("https://api.openai.com/v1/responses" if provider == "openai"
                            else "http://127.0.0.1:11434/api/chat")
        return cls(
            pack_dir=pack, api_url=env.get("MEMBER_API_URL", "http://127.0.0.1:8090"),
            tenant_tokens=tokens, directory_key=directory,
            state_path=Path(state_path or env.get("STATE_PATH") or "runtime/member_agent.sqlite3"),
            model_provider=provider,
            model_name=env.get("MODEL_NAME", ""),
            model_endpoint=env.get("MODEL_ENDPOINT") or default_endpoint,
            model_key=env.get("MODEL_API_KEY") or env.get("OPENAI_API_KEY", ""),
            reasoning_effort=env.get("MODEL_REASONING_EFFORT", ""),
            input_rate=Decimal(env.get("MODEL_INPUT_USD_PER_MILLION", "0")),
            output_rate=Decimal(env.get("MODEL_OUTPUT_USD_PER_MILLION", "0")),
            max_steps=int(env.get("MAX_STEPS", "80")),
            max_model_tokens=int(env.get("MAX_MODEL_TOKENS", "16000")),
            max_cost=Decimal(env.get("MAX_MODEL_COST_USD", "0.025")),
            max_active_seconds=float(env.get("MAX_ACTIVE_SECONDS", "30")),
            turn_timeout=float(env.get("TURN_TIMEOUT_SECONDS", "15")),
            approval_ttl=float(env.get("APPROVAL_TTL_SECONDS", "600")),
            claims_cache_ttl=float(env.get("CLAIMS_CACHE_TTL_SECONDS", "30")),
        )
