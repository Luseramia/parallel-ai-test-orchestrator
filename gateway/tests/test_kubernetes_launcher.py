from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from app.config import Settings
from app.services.job_launcher import GenerateLaunch, PrepareLaunch, TestLaunch
from app.services.kubernetes_launcher import (
    KubernetesApiError,
    KubernetesApiJobLauncher,
)


class KubernetesApiJobLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "POST":
                manifest = json.loads(request.content)
                return httpx.Response(
                    201,
                    json={"metadata": {"name": manifest["metadata"]["name"]}},
                )
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "metadata": {
                            "labels": {"ai-test.openai.com/job-id": self.job_id}
                        }
                    },
                )
            return httpx.Response(200, json={})

        self.client = httpx.Client(
            base_url="https://kubernetes.example.test",
            transport=httpx.MockTransport(handler),
        )
        root = Path(self.temporary_directory.name)
        self.settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            api_token="api-token-for-tests",
            runner_token="runner-token-for-tests",
            artifact_signing_key="artifact-signing-key-for-tests",
            artifact_root=root / "artifacts",
            repository_policy_path=root / "policy.yaml",
            kubernetes_namespace="ai-test-runners",
        )
        self.launcher = KubernetesApiJobLauncher(self.settings, client=self.client)
        self.job_id = "atj_" + "a" * 32

    def tearDown(self) -> None:
        self.client.close()
        self.temporary_directory.cleanup()

    def test_prepare_job_is_hardened_and_namespace_scoped(self) -> None:
        name = self.launcher.launch_prepare(
            PrepareLaunch(
                job_id=self.job_id,
                task_id="TASK-1",
                repository="github.com/example/repo",
                clone_url="https://github.com/example/repo.git",
                base_sha="b" * 40,
                plan_object_key=f"jobs/{self.job_id}/attempts/1/plan.json",
            )
        )
        request = self.requests[-1]
        manifest = json.loads(request.content)
        pod = manifest["spec"]["template"]["spec"]
        container = pod["containers"][0]

        self.assertIn("/namespaces/ai-test-runners/jobs", str(request.url))
        self.assertEqual(name, manifest["metadata"]["name"])
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual("codex-runner", pod["serviceAccountName"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(["ALL"], container["securityContext"]["capabilities"]["drop"])
        self.assertEqual(0, manifest["spec"]["backoffLimit"])
        env = {item["name"]: item for item in container["env"]}
        self.assertIn("OPENAI_API_KEY", env)
        self.assertIn("secretKeyRef", env["RUNNER_TOKEN"]["valueFrom"])

    def test_test_runner_has_no_model_credentials(self) -> None:
        self.launcher.launch_test(
            TestLaunch(
                job_id=self.job_id,
                task_id="TASK-1",
                repository="github.com/example/repo",
                clone_url="https://github.com/example/repo.git",
                base_sha="b" * 40,
                code_sha="c" * 40,
                attempt=2,
                patch_object_key=f"jobs/{self.job_id}/attempts/2/tests.patch",
                patch_sha256="d" * 64,
                runner_policy={"command_slots": {}},
            )
        )
        manifest = json.loads(self.requests[-1].content)
        pod = manifest["spec"]["template"]["spec"]
        env = {item["name"] for item in pod["containers"][0]["env"]}
        self.assertEqual("test-runner", pod["serviceAccountName"])
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertFalse(pod["automountServiceAccountToken"])

    def test_generate_uses_exact_sha_and_serialized_patch_policy(self) -> None:
        self.launcher.launch_generate(
            GenerateLaunch(
                job_id=self.job_id,
                task_id="TASK-1",
                repository="github.com/example/repo",
                clone_url="https://github.com/example/repo.git",
                base_sha="b" * 40,
                code_sha="c" * 40,
                attempt=3,
                plan_object_key=f"jobs/{self.job_id}/attempts/1/plan.json",
                draft_object_key=f"jobs/{self.job_id}/attempts/1/draft.json",
                patch_policy={"allowedPaths": ["tests/**"]},
            )
        )
        manifest = json.loads(self.requests[-1].content)
        env = {
            item["name"]: item.get("value")
            for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual("c" * 40, env["CODE_SHA"])
        self.assertEqual(
            {"allowedPaths": ["tests/**"]}, json.loads(env["PATCH_POLICY_JSON"])
        )

    def test_delete_reads_and_validates_exact_job_owner_first(self) -> None:
        self.launcher.delete_job("test-runner-safe-1", self.job_id)
        self.assertEqual(["GET", "DELETE"], [item.method for item in self.requests])
        self.assertTrue(
            all(
                "/namespaces/ai-test-runners/jobs/" in str(item.url)
                for item in self.requests
            )
        )

    def test_delete_refuses_job_owned_by_another_logical_job(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "metadata": {"labels": {"ai-test.openai.com/job-id": "atj_other"}}
                },
            )

        other_client = httpx.Client(
            base_url="https://kubernetes.example.test",
            transport=httpx.MockTransport(handler),
        )
        launcher = KubernetesApiJobLauncher(self.settings, client=other_client)
        try:
            with self.assertRaises(KubernetesApiError):
                launcher.delete_job("test-runner-safe-1", self.job_id)
        finally:
            other_client.close()


if __name__ == "__main__":
    unittest.main()
