from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    api_token: str
    runner_token: str
    artifact_signing_key: str
    artifact_root: Path
    repository_policy_path: Path
    max_request_bytes: int = 1_048_576
    max_artifact_bytes: int = 10_485_760
    artifact_link_ttl_seconds: int = 300
    allow_http_callbacks: bool = False
    callback_allowed_hosts: tuple[str, ...] = ()
    github_token: str | None = None
    completion_webhook_secret: str = ""
    job_launcher_backend: str = "fake"
    kubernetes_api_url: str = "https://kubernetes.default.svc"
    kubernetes_namespace: str = "ai-test-runners"
    kubernetes_token_file: Path = Path(
        "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )
    kubernetes_ca_file: Path = Path(
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    )
    gateway_internal_url: str = "http://gateway.ai-test-system.svc.cluster.local:8080"
    codex_runner_image: str = "parallel-ai-test-codex-runner:latest"
    test_runner_image: str = "parallel-ai-test-runner:latest"
    artifact_pvc_name: str = "ai-test-artifacts"
    runner_token_secret_name: str = "ai-test-runner-callback"
    codex_secret_name: str = "ai-test-codex-auth"
    runner_active_deadline_seconds: int = 1800
    runner_ttl_seconds: int = 3600
    # Callback credentials are separated per workload type so one can be
    # rotated or revoked without stopping the other. Each falls back to the
    # shared runner token, which is what local tests and single-secret
    # deployments use.
    codex_runner_token: str = ""
    test_runner_token: str = ""
    codex_token_secret_name: str = ""
    test_token_secret_name: str = ""
    reconcile_grace_seconds: int = 300
    waiting_for_code_ttl_seconds: int = 86_400
    artifact_retention_days: int = 30

    @property
    def runner_tokens(self) -> tuple[str, ...]:
        """Every bearer token a runner callback may legitimately present."""

        return tuple(
            token
            for token in (
                self.runner_token,
                self.codex_runner_token,
                self.test_runner_token,
            )
            if token
        )

    @property
    def codex_callback_secret_name(self) -> str:
        return self.codex_token_secret_name or self.runner_token_secret_name

    @property
    def test_callback_secret_name(self) -> str:
        return self.test_token_secret_name or self.runner_token_secret_name

    @classmethod
    def from_env(cls) -> Settings:
        codex_runner_token = os.getenv("GATEWAY_CODEX_RUNNER_TOKEN", "")
        test_runner_token = os.getenv("GATEWAY_TEST_RUNNER_TOKEN", "")
        shared_runner_token = os.getenv("GATEWAY_RUNNER_TOKEN", "")
        if not shared_runner_token and not (codex_runner_token and test_runner_token):
            raise RuntimeError(
                "set GATEWAY_RUNNER_TOKEN, or both GATEWAY_CODEX_RUNNER_TOKEN "
                "and GATEWAY_TEST_RUNNER_TOKEN"
            )
        return cls(
            database_url=_required("DATABASE_URL"),
            api_token=_required("GATEWAY_API_TOKEN"),
            runner_token=shared_runner_token,
            artifact_signing_key=_required("ARTIFACT_SIGNING_KEY"),
            artifact_root=Path(
                os.getenv("ARTIFACT_ROOT", "/var/lib/ai-test/artifacts")
            ),
            repository_policy_path=Path(
                os.getenv(
                    "REPOSITORY_POLICY_PATH", "/etc/ai-test/repository-policies.yaml"
                )
            ),
            max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", "1048576")),
            max_artifact_bytes=int(os.getenv("MAX_ARTIFACT_BYTES", "10485760")),
            artifact_link_ttl_seconds=int(
                os.getenv("ARTIFACT_LINK_TTL_SECONDS", "300")
            ),
            allow_http_callbacks=_boolean("ALLOW_HTTP_CALLBACKS", False),
            callback_allowed_hosts=tuple(
                host.strip().lower()
                for host in os.getenv("CALLBACK_ALLOWED_HOSTS", "").split(",")
                if host.strip()
            ),
            github_token=os.getenv("GITHUB_READ_TOKEN"),
            completion_webhook_secret=_required("COMPLETION_WEBHOOK_SECRET"),
            job_launcher_backend=os.getenv("JOB_LAUNCHER_BACKEND", "kubernetes"),
            kubernetes_api_url=os.getenv(
                "KUBERNETES_API_URL", "https://kubernetes.default.svc"
            ),
            kubernetes_namespace=os.getenv(
                "KUBERNETES_RUNNER_NAMESPACE", "ai-test-runners"
            ),
            kubernetes_token_file=Path(
                os.getenv(
                    "KUBERNETES_TOKEN_FILE",
                    "/var/run/secrets/kubernetes.io/serviceaccount/token",
                )
            ),
            kubernetes_ca_file=Path(
                os.getenv(
                    "KUBERNETES_CA_FILE",
                    "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                )
            ),
            gateway_internal_url=os.getenv(
                "GATEWAY_INTERNAL_URL",
                "http://gateway.ai-test-system.svc.cluster.local:8080",
            ),
            codex_runner_image=os.getenv(
                "CODEX_RUNNER_IMAGE", "parallel-ai-test-codex-runner:latest"
            ),
            test_runner_image=os.getenv(
                "TEST_RUNNER_IMAGE", "parallel-ai-test-runner:latest"
            ),
            artifact_pvc_name=os.getenv("ARTIFACT_PVC_NAME", "ai-test-artifacts"),
            runner_token_secret_name=os.getenv(
                "RUNNER_TOKEN_SECRET_NAME", "ai-test-runner-callback"
            ),
            codex_secret_name=os.getenv("CODEX_SECRET_NAME", "ai-test-codex-auth"),
            runner_active_deadline_seconds=int(
                os.getenv("RUNNER_ACTIVE_DEADLINE_SECONDS", "1800")
            ),
            runner_ttl_seconds=int(os.getenv("RUNNER_TTL_SECONDS", "3600")),
            codex_runner_token=codex_runner_token,
            test_runner_token=test_runner_token,
            codex_token_secret_name=os.getenv("CODEX_TOKEN_SECRET_NAME", ""),
            test_token_secret_name=os.getenv("TEST_TOKEN_SECRET_NAME", ""),
            reconcile_grace_seconds=int(os.getenv("RECONCILE_GRACE_SECONDS", "300")),
            waiting_for_code_ttl_seconds=int(
                os.getenv("WAITING_FOR_CODE_TTL_SECONDS", "86400")
            ),
            artifact_retention_days=int(os.getenv("ARTIFACT_RETENTION_DAYS", "30")),
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.lower() in {"1", "true", "yes"}:
        return True
    if raw.lower() in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"environment variable {name} must be a boolean")
