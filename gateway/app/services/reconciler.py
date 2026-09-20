from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.domain.test_jobs import (
    EventPhase,
    FailureClass,
    InvalidStateTransition,
    JobStatus,
)
from app.repositories.notification_repository import NotificationRepository
from app.repositories.test_job_repository import (
    ConcurrentUpdateError,
    NotFoundError,
    TestJobRepository,
)
from app.services.artifact_service import ArtifactError, FilesystemArtifactStore
from app.services.job_launcher import JobLauncher
from app.services.notification_service import NotificationDispatcher
from app.services.notification_template import build_terminal_payload

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    timed_out: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    purged_artifacts: int = 0
    purged_bytes: int = 0
    notifications_retried: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "timed_out": list(self.timed_out),
            "released": list(self.released),
            "purged_artifacts": self.purged_artifacts,
            "purged_bytes": self.purged_bytes,
            "notifications_retried": self.notifications_retried,
        }


class JobReconciler:
    """Periodic cleanup for work that no callback will ever finish.

    Runners report their own outcome, so every duty here exists for the case
    where that report never arrives: a Pod evicted mid-test, a deleted
    namespace, a gateway restarted between creating a Job and recording its
    name. Each duty is idempotent, so running the sweep twice costs nothing.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        launcher: JobLauncher,
        artifact_store: FilesystemArtifactStore,
        notification_dispatcher: NotificationDispatcher,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._launcher = launcher
        self._artifact_store = artifact_store
        self._notification_dispatcher = notification_dispatcher
        self._settings = settings

    def run(self, now: datetime | None = None) -> ReconcileReport:
        moment = now or datetime.now(UTC)
        timed_out = self.expire_stale_jobs(moment)
        released = self.release_finished_jobs()
        purged_artifacts, purged_bytes = self.purge_expired_artifacts(moment)
        return ReconcileReport(
            timed_out=tuple(timed_out),
            released=tuple(released),
            purged_artifacts=purged_artifacts,
            purged_bytes=purged_bytes,
            # Retried last so that notifications enqueued by the timeout sweep
            # in this same run go out without waiting for the next one.
            notifications_retried=self.retry_notifications(),
        )

    def expire_stale_jobs(self, now: datetime) -> list[str]:
        runner_cutoff = now - timedelta(
            seconds=self._settings.runner_active_deadline_seconds
            + self._settings.reconcile_grace_seconds
        )
        waiting_cutoff = now - timedelta(
            seconds=self._settings.waiting_for_code_ttl_seconds
        )
        with self._session_factory() as session:
            candidates = [
                job.id
                for job in TestJobRepository(session).list_stale_active(
                    runner_cutoff=runner_cutoff,
                    waiting_cutoff=waiting_cutoff,
                )
            ]
        return [job_id for job_id in candidates if self._expire_one(job_id, now)]

    def release_finished_jobs(self) -> list[str]:
        with self._session_factory() as session:
            finished = [
                (job.id, job.latest_k8s_job_name)
                for job in TestJobRepository(session).list_finished_with_k8s_job()
                if job.latest_k8s_job_name
            ]
        released: list[str] = []
        for job_id, k8s_job_name in finished:
            if not self._delete_k8s_job(job_id, k8s_job_name):
                continue
            with self._session_factory() as session:
                TestJobRepository(session).clear_k8s_job(
                    job_id=job_id, k8s_job_name=k8s_job_name
                )
                session.commit()
            released.append(job_id)
        return released

    def purge_expired_artifacts(self, now: datetime) -> tuple[int, int]:
        cutoff = now - timedelta(days=self._settings.artifact_retention_days)
        purged = 0
        purged_bytes = 0
        with self._session_factory() as session:
            repository = TestJobRepository(session)
            for artifact in repository.list_purgeable_artifacts(cutoff=cutoff):
                try:
                    self._artifact_store.delete(artifact.object_key)
                except ArtifactError:
                    logger.exception(
                        "could not purge artifact",
                        extra={"artifact_id": artifact.id},
                    )
                    continue
                # The row survives with its digest and size; only the bytes go,
                # so a purged result stays auditable and its immutable key can
                # never come back holding different content.
                repository.mark_artifact_purged(artifact.id, now)
                purged += 1
                purged_bytes += artifact.size_bytes
            session.commit()
        return purged, purged_bytes

    def retry_notifications(self) -> int:
        return self._notification_dispatcher.reconcile_pending()

    def _expire_one(self, job_id: str, now: datetime) -> bool:
        with self._session_factory() as session:
            repository = TestJobRepository(session)
            notifications = NotificationRepository(session)
            try:
                job = repository.get(job_id)
            except NotFoundError:
                return False
            if job.status.is_terminal:
                return False
            if job.latest_k8s_job_name and self._delete_k8s_job(
                job_id, job.latest_k8s_job_name
            ):
                # Forget it here so the release sweep in the same run does not
                # ask the API to delete the same Job a second time.
                repository.clear_k8s_job(
                    job_id=job.id, k8s_job_name=job.latest_k8s_job_name
                )
            attempt = job.verify_attempt or job.prepare_attempt or 1
            reason = (
                "waiting_for_code_expired"
                if job.status == JobStatus.WAITING_FOR_CODE
                else "runner_deadline_exceeded"
            )
            event_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"timeout:{job.id}:{job.version}"
            ).hex
            try:
                transitioned, created = repository.record_event(
                    event_id=f"evt_{event_id}",
                    job_id=job.id,
                    expected_version=job.version,
                    attempt=attempt,
                    phase=EventPhase.SYSTEM,
                    to_status=JobStatus.TIMEOUT,
                    occurred_at=now,
                    code_sha=job.code_sha,
                    payload={"reason": reason, "last_status": job.status.value},
                    failure_class=FailureClass.TIMEOUT,
                    failure_message=f"job exceeded its deadline in {job.status.value}",
                )
            except (ConcurrentUpdateError, InvalidStateTransition):
                # A real callback landed between the scan and this write; the
                # runner's own outcome wins.
                logger.info("stale job resolved itself", extra={"job_id": job.id})
                return False
            if not created:
                return False
            notification, notification_created = notifications.enqueue(
                job_id=job.id,
                event_type=f"terminal:{attempt}:{JobStatus.TIMEOUT.value}",
                destination=job.callback_url,
                payload=build_terminal_payload(
                    job=transitioned,
                    status=JobStatus.TIMEOUT,
                    attempt=attempt,
                    summary={"reason": reason, "last_status": job.status.value},
                    occurred_at=now,
                ),
            )
            notification_id = notification.id
            session.commit()
        if notification_created:
            self._notification_dispatcher.submit(notification_id)
        return True

    def _delete_k8s_job(self, job_id: str, k8s_job_name: str) -> bool:
        try:
            self._launcher.delete_job(k8s_job_name, job_id)
        except Exception:
            # Leave the name recorded so the next sweep tries again.
            logger.exception(
                "could not delete Kubernetes Job",
                extra={"job_id": job_id, "k8s_job_name": k8s_job_name},
            )
            return False
        return True
