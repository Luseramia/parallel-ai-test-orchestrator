from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.api.schemas import PrepareRequest, RunnerEvent, VerifyRequest
from app.config import Settings
from app.domain.test_jobs import (
    ArtifactType,
    EventPhase,
    FailureClass,
    InvalidStateTransition,
    JobStatus,
    TestJob,
)
from app.repositories.notification_repository import NotificationRepository
from app.repositories.test_job_repository import (
    IdempotencyConflictError,
    NotFoundError,
    TestJobRepository,
)
from app.services.artifact_service import (
    ArtifactError,
    ArtifactLinkSigner,
    FilesystemArtifactStore,
)
from app.services.git_verifier import GitVerificationError, GitVerifier
from app.services.job_launcher import (
    GenerateLaunch,
    JobDispatcher,
    JobLauncher,
    PrepareLaunch,
    TestLaunch,
)
from app.services.notification_service import NotificationDispatcher
from app.services.notification_template import (
    build_terminal_payload,
    render_completion_message,
)
from app.services.plan_validation import (
    PlanValidationError,
    TestPlanValidator,
    expected_prepare_idempotency_key,
)
from app.services.repository_policy import (
    RepositoryPolicyRegistry,
)


@dataclass(frozen=True, slots=True)
class PrepareResult:
    job: TestJob
    created: bool


class JobService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        policies: RepositoryPolicyRegistry,
        plan_validator: TestPlanValidator,
        artifact_store: FilesystemArtifactStore,
        artifact_signer: ArtifactLinkSigner,
        dispatcher: JobDispatcher,
        git_verifier: GitVerifier,
        notification_dispatcher: NotificationDispatcher,
        launcher: JobLauncher,
    ) -> None:
        self._session = session
        self._settings = settings
        self._policies = policies
        self._plan_validator = plan_validator
        self._artifact_store = artifact_store
        self._artifact_signer = artifact_signer
        self._dispatcher = dispatcher
        self._git_verifier = git_verifier
        self._notification_dispatcher = notification_dispatcher
        self._launcher = launcher
        self._repository = TestJobRepository(session)
        self._notifications = NotificationRepository(session)

    def prepare(self, request: PrepareRequest, idempotency_key: str) -> PrepareResult:
        policy = self._policies.get(request.repository)
        self._validate_callback_url(request.callback_url)
        canonical_plan, plan_digest = self._plan_validator.validate(
            request.plan,
            task_id=request.task_id,
            repository=request.repository,
            branch=request.branch,
            base_sha=request.base_sha,
            policy=policy,
        )
        expected_key = expected_prepare_idempotency_key(
            request.task_id, request.base_sha, plan_digest
        )
        if idempotency_key != expected_key:
            raise PlanValidationError(
                "Idempotency-Key must be task_id:base_sha:canonical_plan_digest"
            )
        request_digest = hashlib.sha256(
            canonical_plan + b"\n" + request.callback_url.encode("utf-8")
        ).hexdigest()

        job, created = self._repository.create_or_get_prepare(
            task_id=request.task_id,
            repository=request.repository,
            branch=request.branch,
            base_sha=request.base_sha,
            plan_digest=plan_digest,
            callback_url=request.callback_url,
        )
        key_record, key_created = self._repository.claim_idempotency_key(
            scope="prepare",
            key=idempotency_key,
            request_sha256=request_digest,
            resource_id=job.id,
        )
        if not key_created and key_record.resource_id != job.id:
            raise IdempotencyConflictError(
                "idempotency key is already associated with another job"
            )

        plan_object_key = f"jobs/{job.id}/attempts/1/plan-{plan_digest}.json"
        if created:
            stored = self._artifact_store.put_bytes(plan_object_key, canonical_plan)
            self._repository.add_artifact(
                job_id=job.id,
                attempt=1,
                artifact_type=ArtifactType.PLAN,
                object_key=stored.object_key,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                media_type="application/json",
            )
        self._session.commit()

        if created:
            self._dispatcher.submit_prepare(
                PrepareLaunch(
                    job_id=job.id,
                    task_id=job.task_id,
                    repository=job.repository,
                    clone_url=policy.clone_url,
                    base_sha=job.base_sha,
                    plan_object_key=plan_object_key,
                )
            )
        return PrepareResult(job=self._repository.get(job.id), created=created)

    def verify(self, job_id: str, request: VerifyRequest) -> tuple[TestJob, int, bool]:
        job = self._repository.get(job_id)
        policy = self._policies.get(request.repository)
        if job.repository != request.repository or job.branch != request.branch:
            raise IdempotencyConflictError(
                "verify repository or branch does not match the prepared job"
            )
        if not policy.allows_branch(request.branch):
            raise GitVerificationError("branch is not allowlisted for this repository")

        if job.status != JobStatus.WAITING_FOR_CODE:
            if job.code_sha == request.code_sha:
                attempt, created = self._repository.claim_verify_attempt(
                    job_id=job_id,
                    code_sha=request.code_sha,
                    delivery_id=request.push_event_id,
                )
                self._session.commit()
                return self._repository.get(job_id), attempt.attempt, created
            return self._supersede_and_verify(job, request, policy.clone_url)

        self._git_verifier.verify_reachable(policy, request.branch, request.code_sha)
        attempt, created = self._repository.claim_verify_attempt(
            job_id=job_id,
            code_sha=request.code_sha,
            delivery_id=request.push_event_id,
        )
        if not created:
            self._session.commit()
            return self._repository.get(job_id), attempt.attempt, False

        event_id = f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, request.push_event_id).hex}"
        transitioned, _ = self._repository.record_event(
            event_id=event_id,
            job_id=job_id,
            expected_version=job.version,
            attempt=attempt.attempt,
            phase=EventPhase.VERIFY,
            to_status=JobStatus.VERIFY_QUEUED,
            occurred_at=datetime.now(UTC),
            code_sha=request.code_sha,
            payload=request.model_dump(mode="json"),
            verify_attempt=attempt.attempt,
        )
        artifacts = self._repository.list_artifacts(job_id)
        plan = next((item for item in reversed(artifacts) if item.type == "PLAN"), None)
        draft = next(
            (item for item in reversed(artifacts) if item.type == "DRAFT"), None
        )
        if plan is None or draft is None:
            raise PlanValidationError("prepare artifacts are incomplete")
        self._session.commit()
        self._dispatcher.submit_generate(
            GenerateLaunch(
                job_id=job_id,
                task_id=job.task_id,
                base_sha=job.base_sha,
                repository=job.repository,
                clone_url=policy.clone_url,
                code_sha=request.code_sha,
                attempt=attempt.attempt,
                plan_object_key=plan.object_key,
                draft_object_key=draft.object_key,
                patch_policy=self._generation_patch_policy(policy),
            )
        )
        return transitioned, attempt.attempt, True

    def _supersede_and_verify(
        self, job: TestJob, request: VerifyRequest, clone_url: str
    ) -> tuple[TestJob, int, bool]:
        if not job.code_sha or job.verify_attempt < 1:
            raise InvalidStateTransition(job.status, JobStatus.VERIFY_QUEUED)
        policy = self._policies.get(request.repository)
        self._git_verifier.verify_reachable(policy, request.branch, request.code_sha)
        attempt, created = self._repository.claim_verify_attempt(
            job_id=job.id,
            code_sha=request.code_sha,
            delivery_id=request.push_event_id,
        )
        if not created:
            self._session.commit()
            return self._repository.get(job.id), attempt.attempt, False

        occurred_at = datetime.now(UTC)
        superseded, _ = self._repository.record_event(
            event_id=f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, f'supersede:{request.push_event_id}').hex}",
            job_id=job.id,
            expected_version=job.version,
            attempt=job.verify_attempt,
            phase=EventPhase.SYSTEM,
            to_status=JobStatus.SUPERSEDED,
            occurred_at=occurred_at,
            code_sha=job.code_sha,
            payload={"replaced_by_sha": request.code_sha},
        )
        notification_payload: dict[str, object] = {
            "schema_version": "1.0",
            "job_id": job.id,
            "task_id": job.task_id,
            "status": JobStatus.SUPERSEDED.value,
            "base_sha": job.base_sha,
            "code_sha": job.code_sha,
            "tested_sha": job.tested_sha,
            "attempt": job.verify_attempt,
            "summary": {"replaced_by_sha": request.code_sha},
            "status_url": f"/api/v1/test-jobs/{job.id}",
            "occurred_at": occurred_at.isoformat(),
        }
        notification_payload["message"] = render_completion_message(
            notification_payload
        )
        notification, notification_created = self._notifications.enqueue(
            job_id=job.id,
            event_type=f"terminal:{job.verify_attempt}:SUPERSEDED:{job.code_sha}",
            destination=job.callback_url,
            payload=notification_payload,
        )
        transitioned, _ = self._repository.record_event(
            event_id=f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, request.push_event_id).hex}",
            job_id=job.id,
            expected_version=superseded.version,
            attempt=attempt.attempt,
            phase=EventPhase.VERIFY,
            to_status=JobStatus.VERIFY_QUEUED,
            occurred_at=occurred_at,
            code_sha=request.code_sha,
            payload=request.model_dump(mode="json"),
            verify_attempt=attempt.attempt,
            clear_result=True,
        )
        artifacts = self._repository.list_artifacts(job.id)
        plan = next((item for item in reversed(artifacts) if item.type == "PLAN"), None)
        draft = next(
            (item for item in reversed(artifacts) if item.type == "DRAFT"), None
        )
        if plan is None or draft is None:
            raise PlanValidationError("prepare artifacts are incomplete")
        self._session.commit()
        if notification_created:
            self._notification_dispatcher.submit(notification.id)
        self._dispatcher.submit_generate(
            GenerateLaunch(
                job_id=job.id,
                task_id=job.task_id,
                base_sha=job.base_sha,
                repository=job.repository,
                clone_url=clone_url,
                code_sha=request.code_sha,
                attempt=attempt.attempt,
                plan_object_key=plan.object_key,
                draft_object_key=draft.object_key,
                patch_policy=self._generation_patch_policy(policy),
            )
        )
        return transitioned, attempt.attempt, True

    def status(self, job_id: str) -> dict[str, object]:
        job = self._repository.get(job_id)
        artifacts = self._repository.list_artifacts(job_id)
        return {
            "schema_version": "1.0",
            "job_id": job.id,
            "task_id": job.task_id,
            "repository": job.repository,
            "branch": job.branch,
            "base_sha": job.base_sha,
            "code_sha": job.code_sha,
            "tested_sha": job.tested_sha,
            "status": job.status.value,
            "prepare_attempt": job.prepare_attempt,
            "verify_attempt": job.verify_attempt,
            "version": job.version,
            "failure_class": job.failure_class.value if job.failure_class else None,
            "failure_message": job.failure_message,
            "artifacts": [
                {
                    "id": artifact.id,
                    "type": artifact.type,
                    "object_key": artifact.object_key,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "media_type": artifact.media_type,
                    "created_at": artifact.created_at,
                    "purged_at": artifact.purged_at,
                    # Retention removes the bytes but keeps the digest, so a
                    # purged artifact is still listed - without a dead link.
                    "url": (
                        None
                        if artifact.purged_at
                        else self._artifact_signer.create_url(job.id, artifact.id)
                    ),
                }
                for artifact in artifacts
            ],
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }

    def cancel(self, job_id: str) -> tuple[TestJob, bool]:
        job = self._repository.get(job_id)
        if job.status.is_terminal:
            return job, False
        if job.latest_k8s_job_name:
            self._launcher.delete_job(job.latest_k8s_job_name, job.id)
        attempt = job.verify_attempt or job.prepare_attempt
        occurred_at = datetime.now(UTC)
        transitioned, created = self._repository.record_event(
            event_id=f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, f'cancel:{job.id}:{job.version}').hex}",
            job_id=job.id,
            expected_version=job.version,
            attempt=attempt,
            phase=EventPhase.SYSTEM,
            to_status=JobStatus.CANCELLED,
            occurred_at=occurred_at,
            code_sha=job.code_sha,
            payload={"reason": "operator_cancelled"},
            failure_class=FailureClass.CANCELLED,
            failure_message="job cancelled by operator",
        )
        notification_payload = build_terminal_payload(
            job=job,
            status=JobStatus.CANCELLED,
            attempt=attempt,
            summary={"reason": "operator_cancelled"},
            occurred_at=occurred_at,
        )
        notification, notification_created = self._notifications.enqueue(
            job_id=job.id,
            event_type=f"terminal:{attempt}:CANCELLED",
            destination=job.callback_url,
            payload=notification_payload,
        )
        self._session.commit()
        if notification_created:
            self._notification_dispatcher.submit(notification.id)
        return transitioned, created

    def record_runner_event(
        self, job_id: str, event: RunnerEvent
    ) -> tuple[TestJob, bool]:
        job = self._repository.get(job_id)
        if job.code_sha and event.code_sha != job.code_sha:
            raise IdempotencyConflictError(
                "event code SHA does not match the current job"
            )
        expected_attempt = (
            job.prepare_attempt
            if event.phase == EventPhase.PREPARE
            else job.verify_attempt
        )
        if event.attempt != expected_attempt:
            raise IdempotencyConflictError("event attempt is not current for this job")
        payload = event.model_dump(mode="json")
        failure_class = self._failure_class(event.status)
        transitioned, created = self._repository.record_event(
            event_id=event.event_id,
            job_id=job_id,
            expected_version=job.version,
            attempt=event.attempt,
            phase=event.phase,
            to_status=event.status,
            occurred_at=event.occurred_at,
            code_sha=event.code_sha,
            payload=payload,
            tested_sha=event.code_sha if event.phase.value == "TEST" else None,
            failure_class=failure_class,
            failure_message=self._failure_message(event.summary),
        )
        if created:
            for artifact in event.artifacts:
                stored = self._artifact_store.describe(artifact.object_key)
                if (
                    stored.sha256 != artifact.sha256
                    or stored.size_bytes != artifact.size_bytes
                ):
                    raise ArtifactError(
                        "callback artifact metadata does not match storage"
                    )
                self._repository.add_artifact(
                    job_id=job_id,
                    attempt=event.attempt,
                    artifact_type=artifact.type,
                    object_key=artifact.object_key,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                )
        test_launch = None
        notification_id = None
        if created and event.status == JobStatus.TEST_QUEUED:
            test_launch = self._build_test_launch(job_id, event.attempt)
        if created and event.status.is_terminal:
            notification_payload = {
                "schema_version": "1.0",
                "job_id": job.id,
                "task_id": job.task_id,
                "status": event.status.value,
                "base_sha": job.base_sha,
                "code_sha": event.code_sha or job.code_sha,
                "tested_sha": (
                    event.code_sha
                    if event.phase == EventPhase.TEST
                    else transitioned.tested_sha
                ),
                "attempt": event.attempt,
                "summary": event.summary,
                "status_url": f"/api/v1/test-jobs/{job.id}",
                "occurred_at": event.occurred_at.isoformat(),
            }
            notification_payload["message"] = render_completion_message(
                notification_payload
            )
            notification, notification_created = self._notifications.enqueue(
                job_id=job_id,
                event_type=f"terminal:{event.attempt}:{event.status.value}",
                destination=job.callback_url,
                payload=notification_payload,
            )
            if notification_created:
                notification_id = notification.id
        self._session.commit()
        if test_launch is not None:
            self._dispatcher.submit_test(test_launch)
        if notification_id is not None:
            self._notification_dispatcher.submit(notification_id)
        return transitioned, created

    def download_artifact(
        self,
        job_id: str,
        artifact_id: str,
        expires: int,
        signature: str,
    ) -> tuple[bytes, str]:
        self._artifact_signer.verify(job_id, artifact_id, expires, signature)
        artifact = self._repository.get_artifact(job_id, artifact_id)
        if artifact.purged_at:
            raise NotFoundError("artifact bytes were removed by retention")
        content = self._artifact_store.read_bytes(artifact.object_key)
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ArtifactError("artifact checksum verification failed")
        return content, artifact.media_type or "application/octet-stream"

    def _validate_callback_url(self, callback_url: str) -> None:
        parsed = urlsplit(callback_url)
        if not parsed.hostname or parsed.username or parsed.password:
            raise PlanValidationError(
                "callback URL must contain a host and no user info"
            )
        if parsed.scheme != "https" and not (
            self._settings.allow_http_callbacks and parsed.scheme == "http"
        ):
            raise PlanValidationError("callback URL must use HTTPS")
        if (
            self._settings.callback_allowed_hosts
            and parsed.hostname.lower() not in self._settings.callback_allowed_hosts
        ):
            raise PlanValidationError("callback host is not allowlisted")

    def _build_test_launch(self, job_id: str, attempt: int) -> TestLaunch:
        job = self._repository.get(job_id)
        if not job.code_sha:
            raise PlanValidationError("test execution requires a code SHA")
        policy = self._policies.get(job.repository)
        artifacts = self._repository.list_artifacts(job_id)
        patch = next(
            (item for item in reversed(artifacts) if item.type == "PATCH"), None
        )
        plan_artifact = next(
            (item for item in reversed(artifacts) if item.type == "PLAN"), None
        )
        if patch is None or plan_artifact is None:
            raise PlanValidationError("test execution artifacts are incomplete")
        plan = json.loads(
            self._artifact_store.read_bytes(plan_artifact.object_key).decode("utf-8")
        )
        command_slots = plan["commands"]
        command_argv = {
            command_id: list(policy.command_argv[command_id])
            for command_id in command_slots.values()
        }
        command_timeouts = {
            slot: policy.limits.get(f"{slot}_timeout_seconds", 300)
            for slot in command_slots
        }
        runner_policy: dict[str, object] = {
            "command_slots": command_slots,
            "command_argv": command_argv,
            "command_timeouts": command_timeouts,
            "allowed_paths": list(policy.patch.allowed_paths),
            "denied_paths": list(policy.patch.denied_paths),
            "junit_globs": list(policy.junit_globs),
            "coverage_globs": list(policy.coverage_globs),
            "allow_skipped_required_tests": plan["policy"][
                "allow_skipped_required_tests"
            ],
            "max_log_bytes": policy.limits.get("max_log_bytes", 10_485_760),
        }
        return TestLaunch(
            job_id=job.id,
            task_id=job.task_id,
            repository=job.repository,
            clone_url=policy.clone_url,
            base_sha=job.base_sha,
            code_sha=job.code_sha,
            attempt=attempt,
            patch_object_key=patch.object_key,
            patch_sha256=patch.sha256,
            runner_policy=runner_policy,
        )

    @staticmethod
    def _generation_patch_policy(policy: object) -> dict[str, object]:
        patch = policy.patch
        return {
            "allowedPaths": list(patch.allowed_paths),
            "deniedPaths": list(patch.denied_paths),
            "allowNewFiles": patch.allow_new_files,
            "allowDeletes": patch.allow_deletes,
            "allowRenames": patch.allow_renames,
            "allowBinary": patch.allow_binary,
            "allowSymlinks": patch.allow_symlinks,
            "allowSubmodules": patch.allow_submodules,
            "allowModeChanges": patch.allow_mode_changes,
            "maxFiles": patch.max_files,
            "maxBytes": patch.max_bytes,
            "allowFocusedTests": False,
        }

    @staticmethod
    def _failure_class(status: JobStatus) -> FailureClass | None:
        if status == JobStatus.FAILED:
            return FailureClass.TEST_FAILURE
        if status == JobStatus.BLOCKED:
            return FailureClass.POLICY
        if status == JobStatus.TIMEOUT:
            return FailureClass.TIMEOUT
        if status == JobStatus.ERROR:
            return FailureClass.INFRASTRUCTURE
        if status == JobStatus.CANCELLED:
            return FailureClass.CANCELLED
        return None

    @staticmethod
    def _failure_message(summary: dict[str, object]) -> str | None:
        message = summary.get("error") or summary.get("reason_code")
        return str(message)[:2000] if message else None


def default_contract_path() -> Path:
    return Path(__file__).resolve().parents[3] / "contracts" / "test-plan.schema.json"
