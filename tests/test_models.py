import json
import unittest
from dataclasses import replace
from decimal import Decimal

from member_agent.agent import Agent
from member_agent.errors import UpstreamError
from member_agent.routing import model_features
from tests.support import Fixture


class ModelTransport:
    def __init__(self, provider="openai", answer=None, error=False):
        self.calls = []
        self.provider = provider
        self.answer = answer or {"intent": "benefit", "benefit": "specialist"}
        self.error = error

    def request(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error:
            raise UpstreamError(503)
        content = json.dumps(self.answer)
        if self.provider == "openai":
            return {"status": "completed", "usage": {"input_tokens": 120, "output_tokens": 8},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": content}]}]}
        if self.provider == "ollama":
            return {"message": {"content": content}, "prompt_eval_count": 120, "eval_count": 8}
        return {"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8}}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def model_agent(self, provider="openai", model_transport=None, **overrides):
        settings = replace(self.fx.settings, model_provider=provider, model_name="test-model",
                           model_key="test-model-key", input_rate=Decimal("0.4"), output_rate=Decimal("1.6"),
                           model_endpoint="https://model.invalid/v1/responses", **overrides)
        return Agent(settings, transport=self.fx.transport, model_transport=model_transport or ModelTransport(provider))

    def test_openai_payload_contract_and_usage(self):
        for effort in ("", "none"):
            with self.subTest(reasoning_effort=effort):
                transport = ModelTransport()
                agent = self.model_agent(model_transport=transport, reasoning_effort=effort)
                scope = agent.new_session("red", "M900001")
                reply = agent.ask(scope, "What does a specialist visit cost?")
                self.assertEqual("answer", reply.behaviour)
                payload = json.loads(transport.calls[0]["body"])
                self.assertIs(False, payload["store"])
                self.assertTrue(payload["text"]["format"]["strict"])
                self.assertFalse(payload["text"]["format"]["schema"]["additionalProperties"])
                self.assertNotIn("previous_response_id", payload)
                if effort:
                    self.assertEqual({"effort": effort}, payload["reasoning"])
                else:
                    self.assertNotIn("reasoning", payload)
                self.assertEqual(128, reply.metrics["model_tokens"])
                self.assertEqual(Decimal("0.0000608"), Decimal(reply.metrics["cost_usd"]))
                self.assertIn("$47 copay", reply.text)
                route = next(e for e in agent.store.events(scope) if e["event"] == "model.route")
                self.assertEqual("benefit", route["code"])
                self.assertEqual("specialist", route["decision"])

    def test_raw_question_records_and_identifiers_never_reach_model(self):
        transport = ModelTransport()
        agent = self.model_agent(model_transport=transport)
        question = ("What does Example Person M900001 pay for a specialist visit? "
                    "Email example@invalid.example; birthday 1980-02-02; address 123 Test Road")
        reply = agent.ask(self.fx.scope, question)
        self.assertEqual("answer", reply.behaviour)
        payload = transport.calls[0]["body"].decode()
        for forbidden in ("Example Person", "M900001", "example@invalid.example", "1980", "123", "Test Road",
                          "PRIVATE-GROUP", "111-22-3333", "RED-NET"):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn(question, payload)

    def test_model_cannot_supply_tenant_or_write_action(self):
        for answer in ({"intent": "file_grievance", "benefit": ""},
                       {"intent": "benefit", "benefit": "specialist", "tenant": "blue"}):
            with self.subTest(answer=answer):
                scope = self.fx.agent.new_session("red", "M900001")
                agent = self.model_agent(model_transport=ModelTransport(answer=answer))
                reply = agent.ask(scope, "What does a specialist visit cost?")
                self.assertEqual("cannot_answer", reply.behaviour)
        self.assertEqual([], self.fx.transport.records)

    def test_model_failure_keeps_reservation_and_does_not_retry(self):
        transport = ModelTransport(error=True)
        agent = self.model_agent(model_transport=transport)
        reply = agent.ask(self.fx.scope, "What does a specialist visit cost?")
        self.assertEqual("unavailable", reply.behaviour)
        self.assertEqual(1, len(transport.calls))
        self.assertGreater(Decimal(reply.metrics["cost_usd"]), 0)
        self.assertGreater(reply.metrics["model_tokens"], 0)
        error = next(e for e in agent.store.events(self.fx.scope) if e["event"] == "model.http_error")
        self.assertEqual(503, error["status"])

    def test_cost_budget_blocks_model_request_before_network(self):
        transport = ModelTransport()
        agent = self.model_agent(model_transport=transport, max_cost=Decimal("0.0000001"))
        reply = agent.ask(self.fx.scope, "What does a specialist visit cost?")
        self.assertEqual("budget_exceeded", reply.behaviour)
        self.assertEqual([], transport.calls)

    def test_token_budget_blocks_model_request_before_network(self):
        transport = ModelTransport()
        agent = self.model_agent(model_transport=transport, max_model_tokens=20)
        self.assertEqual("budget_exceeded", agent.ask(self.fx.scope, "What does a specialist visit cost?").behaviour)
        self.assertEqual([], transport.calls)

    def test_provider_replacement_preserves_agent_contract(self):
        for provider in ("ollama", "chat-completions"):
            with self.subTest(provider=provider):
                scope = self.fx.agent.new_session("red", "M900001")
                agent = self.model_agent(provider)
                self.assertEqual("answer", agent.ask(scope, "What does a specialist visit cost?").behaviour)

    def test_write_confirmation_never_delegated_to_model(self):
        transport = ModelTransport()
        agent = self.model_agent(model_transport=transport)
        self.assertEqual("confirm_required", agent.ask(self.fx.scope, "File a billing grievance about the bill").behaviour)
        self.assertEqual("filed", agent.ask(self.fx.scope, "confirm").behaviour)
        self.assertEqual([], transport.calls)

    def test_model_features_have_fixed_vocabulary(self):
        features = model_features("Name EXFILTRATE token sk-secret 111-22-3333. What does a specialist cost?")
        self.assertEqual(["what", "specialist", "cost"], features["words"])


if __name__ == "__main__":
    unittest.main()
