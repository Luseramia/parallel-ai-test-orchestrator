from __future__ import annotations

import unittest
from unittest import mock

from app.config import Settings

BASE_ENVIRONMENT = {
    "DATABASE_URL": "postgresql+psycopg://gateway@postgres/ai_test",
    "GATEWAY_API_TOKEN": "api-token",
    "ARTIFACT_SIGNING_KEY": "artifact-signing-key-value",
    "COMPLETION_WEBHOOK_SECRET": "completion-webhook-secret-value",
}


class SettingsTests(unittest.TestCase):
    def test_per_workload_callback_tokens_are_both_accepted(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                **BASE_ENVIRONMENT,
                "GATEWAY_CODEX_RUNNER_TOKEN": "codex-token",
                "GATEWAY_TEST_RUNNER_TOKEN": "test-token",
                "CODEX_TOKEN_SECRET_NAME": "ai-test-codex-callback",
                "TEST_TOKEN_SECRET_NAME": "ai-test-test-callback",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(("codex-token", "test-token"), settings.runner_tokens)
        self.assertEqual("ai-test-codex-callback", settings.codex_callback_secret_name)
        self.assertEqual("ai-test-test-callback", settings.test_callback_secret_name)

    def test_a_single_shared_token_still_works(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {**BASE_ENVIRONMENT, "GATEWAY_RUNNER_TOKEN": "shared-token"},
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(("shared-token",), settings.runner_tokens)
        # Both workloads fall back to the one callback Secret.
        self.assertEqual(
            settings.codex_callback_secret_name, settings.test_callback_secret_name
        )

    def test_one_workload_token_alone_is_rejected(self) -> None:
        # Starting the gateway with only half the pair would silently refuse
        # every callback from the other workload, so it fails at startup.
        environment = {
            **BASE_ENVIRONMENT,
            "GATEWAY_CODEX_RUNNER_TOKEN": "codex-token",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            self.assertRaises(RuntimeError, Settings.from_env)


if __name__ == "__main__":
    unittest.main()
