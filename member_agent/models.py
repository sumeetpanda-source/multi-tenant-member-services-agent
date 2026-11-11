import json
import time

from .errors import AgentError
from .routing import BENEFITS, INTENTS, model_features, parse_model_intent, rule_intent
from .transport import JsonTransport


INTENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "benefit": {"type": "string", "enum": ["", *BENEFITS]},
    },
    "required": ["intent", "benefit"],
}
INSTRUCTIONS = (
    "Classify the member-services request from its minimised words. Return JSON with intent and benefit. "
    "Use benefit only with intent=benefit; otherwise benefit must be an empty string. "
    "deductible_remaining and oop_remaining ask what is left; pt_remaining asks visits left. "
    "claims_count requires a complete count; claim is status, payment or denial. "
    "appeal checks a denial appeal window. grievance_deadline checks a resolution deadline. "
    "prior_auth is a request for an authorisation decision. providers is a directory search. "
    "There are no writing tools. Choose unknown when insufficient information is available. "
    "Allowed intents: " + ", ".join(INTENTS) + ". Benefits: " + ", ".join(BENEFITS) + "."
)


class ModelRouter:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport or JsonTransport()

    def classify(self, text, budget, store, scope):
        provider = self.settings.model_provider
        if provider == "rules":
            budget.step()
            store.event(scope, "router.local", mode="rules")
            return rule_intent(text)
        features = json.dumps(model_features(text), sort_keys=True)
        messages = [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": features}]
        max_output = 512
        if provider == "openai":
            payload = {
                "model": self.settings.model_name, "store": False,
                "instructions": INSTRUCTIONS, "input": features, "max_output_tokens": max_output,
                "text": {"format": {"type": "json_schema", "name": "member_intent",
                                    "strict": True, "schema": INTENT_SCHEMA}},
            }
        elif provider == "ollama":
            payload = {"model": self.settings.model_name, "messages": messages, "stream": False,
                       "format": INTENT_SCHEMA, "options": {"temperature": 0, "num_predict": max_output}}
        else:
            payload = {"model": self.settings.model_name, "messages": messages, "stream": False,
                       "max_tokens": max_output, "temperature": 0, "store": False,
                       "response_format": {"type": "json_schema", "json_schema": {
                           "name": "member_intent", "strict": True, "schema": INTENT_SCHEMA}}}
        body = json.dumps(payload).encode("utf-8")
        # UTF-8 bytes plus framing/schema overhead is deliberately conservative.
        reservation = budget.reserve_model(len(body) + 1024, max_output)
        store.event(scope, "model.start", mode=provider)
        started = time.monotonic()
        status = "failed"
        try:
            headers = {"Content-Type": "application/json"}
            if self.settings.model_key:
                headers["Authorization"] = "Bearer " + self.settings.model_key
            result = self.transport.request(self.settings.model_endpoint, headers=headers, body=body,
                                            timeout=budget.remaining)
            if provider == "openai":
                usage = result.get("usage", {})
                input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
            elif provider == "ollama":
                input_tokens, output_tokens = result.get("prompt_eval_count"), result.get("eval_count")
            else:
                usage = result.get("usage", {})
                input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if (type(input_tokens) is not int or type(output_tokens) is not int
                    or min(input_tokens, output_tokens) < 0):
                raise AgentError("model_usage_missing")
            cost = budget.settle_model(reservation, input_tokens, output_tokens)
            store.event(scope, "model.usage", input_tokens=input_tokens,
                        output_tokens=output_tokens, cost_usd=format(cost, "f"))
            if provider == "openai":
                if result.get("status") != "completed":
                    raise AgentError("model_response_incomplete")
                texts = [part["text"] for item in result.get("output", [])
                         if item.get("type") == "message" for part in item.get("content", [])
                         if part.get("type") == "output_text"]
                if len(texts) != 1:
                    raise AgentError("model_response_invalid")
                answer = texts[0]
            elif provider == "ollama":
                answer = result["message"]["content"]
            else:
                answer = result["choices"][0]["message"]["content"]
            intent = parse_model_intent(answer)
            status = "completed"
            return intent
        except (KeyError, TypeError, IndexError):
            raise AgentError("model_response_invalid") from None
        finally:
            store.event(scope, "model.end", status=status,
                        elapsed_ms=round((time.monotonic() - started) * 1000))
