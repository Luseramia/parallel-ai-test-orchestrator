from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import Settings
from app.services.job_launcher import GenerateLaunch, PrepareLaunch, TestLaunch

KUBERNETES_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


class KubernetesApiError(RuntimeError):
    pass


class KubernetesApiJobLauncher:
    """Create and delete runner Jobs in one fixed namespace."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        bearer_token: str | None = None,
    ) -> None:
        self._settings = settings
        self._namespace = settings.kubernetes_namespace
        self._validate_name(self._namespace, "namespace")
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            token = (
                bearer_token
                or settings.kubernetes_token_file.read_text(encoding="utf-8").strip()
            )
            if not token:
                raise KubernetesApiError("Kubernetes service account token is empty")
            self._client = httpx.Client(
                base_url=settings.kubernetes_api_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                verify=str(settings.kubernetes_ca_file),
                timeout=10.0,
            )

    def launch_prepare(self, request: PrepareLaunch) -> str:
        name = self._job_name("codex-prepare", request.job_id, 1)
        env = self._base_env(
            request.job_id,
            request.task_id,
            request.clone_url,
            request.base_sha,
            1,
            self._settings.codex_callback_secret_name,
        )
        env.extend(
            [
                self._plain_env("PLAN_PATH", "/artifacts/input/test-plan.json"),
                self._plain_env("PLAN_OBJECT_KEY", request.plan_object_key),
                self._plain_env("OUTPUT_SCHEMA", "/contracts/test-draft.schema.json"),
            ]
        )
        manifest = self._manifest(
            name=name,
            job_id=request.job_id,
            phase="prepare",
            image=self._settings.codex_runner_image,
            service_account="codex-runner",
            command=["node", "/runner/dist/src/prepare.js"],
            env=env,
            model_credentials=True,
        )
        return self._create_job(name, request.job_id, manifest)

    def launch_generate(self, request: GenerateLaunch) -> str:
        name = self._job_name("codex-generate", request.job_id, request.attempt)
        env = self._base_env(
            request.job_id,
            request.task_id,
            request.clone_url,
            request.base_sha,
            request.attempt,
            self._settings.codex_callback_secret_name,
        )
        env.extend(
            [
                self._plain_env("CODE_SHA", request.code_sha),
                self._plain_env("PLAN_PATH", "/artifacts/input/test-plan.json"),
                self._plain_env("PLAN_OBJECT_KEY", request.plan_object_key),
                self._plain_env("DRAFT_PATH", "/artifacts/input/test-draft.json"),
                self._plain_env("DRAFT_OBJECT_KEY", request.draft_object_key),
                self._plain_env(
                    "PATCH_POLICY_JSON",
                    json.dumps(
                        request.patch_policy, sort_keys=True, separators=(",", ":")
                    ),
                ),
                self._plain_env(
                    "OUTPUT_SCHEMA", "/contracts/generation-result.schema.json"
                ),
            ]
        )
        manifest = self._manifest(
            name=name,
            job_id=request.job_id,
            phase="generate",
            image=self._settings.codex_runner_image,
            service_account="codex-runner",
            command=["node", "/runner/dist/src/generate-tests.js"],
            env=env,
            model_credentials=True,
            code_sha=request.code_sha,
        )
        return self._create_job(name, request.job_id, manifest)

    def launch_test(self, request: TestLaunch) -> str:
        name = self._job_name("test-runner", request.job_id, request.attempt)
        env = self._base_env(
            request.job_id,
            request.task_id,
            request.clone_url,
            request.base_sha,
            request.attempt,
            self._settings.test_callback_secret_name,
        )
        env.extend(
            [
                self._plain_env("CODE_SHA", request.code_sha),
                self._plain_env("PATCH_PATH", "/artifacts/input/tests.patch"),
                self._plain_env("PATCH_OBJECT_KEY", request.patch_object_key),
                self._plain_env("PATCH_SHA256", request.patch_sha256),
                self._plain_env(
                    "RUNNER_POLICY_JSON",
                    json.dumps(
                        request.runner_policy, sort_keys=True, separators=(",", ":")
                    ),
                ),
            ]
        )
        manifest = self._manifest(
            name=name,
            job_id=request.job_id,
            phase="test",
            image=self._settings.test_runner_image,
            service_account="test-runner",
            command=["python", "/runner/runner.py"],
            env=env,
            model_credentials=False,
            code_sha=request.code_sha,
        )
        return self._create_job(name, request.job_id, manifest)

    def delete_job(self, job_name: str, job_id: str) -> None:
        self._validate_name(job_name, "job name")
        path = self._job_path(job_name)
        existing = self._client.get(path)
        if existing.status_code == 404:
            return
        self._raise_for_status(existing, "read Kubernetes Job before deletion")
        labels = existing.json().get("metadata", {}).get("labels", {})
        if labels.get("ai-test.openai.com/job-id") != job_id:
            raise KubernetesApiError(
                "refusing to delete a Job owned by another test job"
            )
        response = self._client.delete(
            path,
            params={"propagationPolicy": "Background"},
        )
        if response.status_code != 404:
            self._raise_for_status(response, "delete Kubernetes Job")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _create_job(self, name: str, job_id: str, manifest: dict[str, Any]) -> str:
        response = self._client.post(self._jobs_path(), json=manifest)
        if response.status_code == 409:
            existing = self._client.get(self._job_path(name))
            self._raise_for_status(existing, "read existing Kubernetes Job")
            labels = existing.json().get("metadata", {}).get("labels", {})
            if labels.get("ai-test.openai.com/job-id") != job_id:
                raise KubernetesApiError("Kubernetes Job name collision")
            return name
        self._raise_for_status(response, "create Kubernetes Job")
        returned_name = response.json().get("metadata", {}).get("name")
        if returned_name != name:
            raise KubernetesApiError("Kubernetes API returned an unexpected Job name")
        return name

    def _manifest(
        self,
        *,
        name: str,
        job_id: str,
        phase: str,
        image: str,
        service_account: str,
        command: list[str],
        env: list[dict[str, Any]],
        model_credentials: bool,
        code_sha: str | None = None,
    ) -> dict[str, Any]:
        labels = {
            "app.kubernetes.io/name": "parallel-ai-test-runner",
            "app.kubernetes.io/component": phase,
            "ai-test.openai.com/job-id": job_id,
        }
        if model_credentials:
            env.append(
                {
                    "name": "OPENAI_API_KEY",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": self._settings.codex_secret_name,
                            "key": "api-key",
                        }
                    },
                }
            )
        metadata: dict[str, Any] = {"name": name, "labels": labels}
        if code_sha:
            metadata["annotations"] = {"ai-test.openai.com/code-sha": code_sha}
        volumes: list[dict[str, Any]] = [
            {"name": "artifacts", "emptyDir": {"sizeLimit": "256Mi"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}},
        ]
        mounts = [
            {"name": "artifacts", "mountPath": "/artifacts"},
            {"name": "tmp", "mountPath": "/tmp"},
        ]
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": metadata,
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": self._settings.runner_active_deadline_seconds,
                "ttlSecondsAfterFinished": self._settings.runner_ttl_seconds,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": service_account,
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "securityContext": {
                            "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "runner",
                                "image": image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": command,
                                "env": env,
                                "resources": {
                                    "requests": {"cpu": "250m", "memory": "512Mi"},
                                    "limits": {"cpu": "2", "memory": "2Gi"},
                                },
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "volumeMounts": mounts,
                            }
                        ],
                        "volumes": volumes,
                    },
                },
            },
        }

    def _base_env(
        self,
        job_id: str,
        task_id: str,
        clone_url: str,
        base_sha: str,
        attempt: int,
        callback_secret_name: str,
    ) -> list[dict[str, Any]]:
        return [
            self._plain_env("JOB_ID", job_id),
            self._plain_env("TASK_ID", task_id),
            self._plain_env("GIT_CLONE_URL", clone_url),
            self._plain_env("BASE_SHA", base_sha),
            self._plain_env("ATTEMPT", str(attempt)),
            self._plain_env("ARTIFACT_ROOT", "/artifacts"),
            self._plain_env("GATEWAY_URL", self._settings.gateway_internal_url),
            self._plain_env(
                "ARTIFACT_GATEWAY_URL",
                f"{self._settings.gateway_internal_url.rstrip('/')}/internal/v1/"
                f"test-jobs/{job_id}/artifacts",
            ),
            {
                "name": "RUNNER_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": callback_secret_name,
                        "key": "token",
                    }
                },
            },
        ]

    @staticmethod
    def _plain_env(name: str, value: str) -> dict[str, str]:
        return {"name": name, "value": value}

    @staticmethod
    def _job_name(prefix: str, job_id: str, attempt: int) -> str:
        suffix = job_id.removeprefix("atj_")[-20:]
        name = f"{prefix}-{suffix}-{attempt}".lower()
        if len(name) > 63:
            name = name[:63].rstrip("-")
        KubernetesApiJobLauncher._validate_name(name, "generated job name")
        return name

    def _jobs_path(self) -> str:
        return f"/apis/batch/v1/namespaces/{self._namespace}/jobs"

    def _job_path(self, name: str) -> str:
        return f"{self._jobs_path()}/{name}"

    @staticmethod
    def _validate_name(value: str, description: str) -> None:
        if len(value) > 63 or not KUBERNETES_NAME.fullmatch(value):
            raise KubernetesApiError(f"invalid Kubernetes {description}")

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise KubernetesApiError(f"could not {action}") from error
