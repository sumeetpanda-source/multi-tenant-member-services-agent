import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from member_agent.config import Settings, environment
from member_agent.errors import AgentError
from tests.support import Fixture


class ConfigTests(unittest.TestCase):
    def test_env_file_is_data_and_process_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "local.env"
            file.write_text("# Local values\nMODEL_PROVIDER=rules\nMODEL_NAME='$(some-command)'\n")
            with patch.dict("os.environ", {"MODEL_PROVIDER": "openai"}, clear=True):
                env = environment(file)
            self.assertEqual("openai", env["MODEL_PROVIDER"])
            self.assertEqual("$(some-command)", env["MODEL_NAME"])

    def test_explicit_missing_env_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(AgentError):
                environment(Path(temp) / "missing.env")

    def test_model_key_required_without_exposing_secrets(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        with self.assertRaises(AgentError) as error:
            replace(fx.settings, model_provider="openai", model_name="test-model", model_key="",
                    input_rate=Decimal("1"), output_rate=Decimal("2"))
        self.assertEqual("openai_api_key_required", error.exception.code)
        self.assertNotIn("red-secret", repr(fx.settings))

    def test_model_prices_required_for_budgeting(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        with self.assertRaises(AgentError):
            replace(fx.settings, model_provider="openai", model_name="test-model", model_key="test-key")

    def test_nonfinite_budgets_and_invalid_credentials_rejected(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        changes = ({"max_cost": Decimal("NaN")}, {"turn_timeout": float("inf")},
                   {"tenant_tokens": {"red": 123}}, {"tenant_tokens": {"red": "token\ninjected"}})
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(AgentError):
                    replace(fx.settings, **change)

    def test_openai_key_alias_and_default_responses_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "local.env"
            file.write_text('MODEL_PROVIDER=openai\nMODEL_NAME=test-model\nOPENAI_API_KEY=test-key\n'
                            'TENANT_TOKENS_JSON={"red":"test-tenant-token"}\n'
                            'MODEL_INPUT_USD_PER_MILLION=1\nMODEL_OUTPUT_USD_PER_MILLION=2\nMODEL_ENDPOINT=\n')
            with patch.dict("os.environ", {}, clear=True):
                settings = Settings.from_env(pack_dir=temp, env_file=file)
            self.assertEqual("test-key", settings.model_key)
            self.assertEqual("https://api.openai.com/v1/responses", settings.model_endpoint)
            self.assertNotIn("test-key", repr(settings))
