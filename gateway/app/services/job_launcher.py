from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.domain.test_jobs import EventPhase, FailureClass, JobStatus
from app.repositories.test_job_repository import TestJobRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrepareLaunch:
    job_id: str
    task_id: str
    repository: str
    clone_url: str
    base_sha: str
    plan_object_key: str


@dataclass(frozen=True, slots=True)
class GenerateLaunch:
    job_id: str
    task_id: str
    base_sha: str
    repository: str
    clone_url: str
    code_sha: str
    attempt: int
    plan_object_key: str
    draft_object_key: str
    patch_policy: dict[str, object]


@dataclass(frozen=True, slots=True)
class TestLaunch:
    job_id: str
    task_id: str
    repository: str
    clone_url: str
    base_sha: str
    code_sha: str
    attempt: int
    patch_object_key: str
    patch_sha256: str
    runner_policy: dict[str, object]


class JobLauncher(Protocol):
    def launch_prepare(self, request: PrepareLaunch) -> str: ...

    def launch_generate(self, request: GenerateLaunch) -> str: ...

    def launch_test(self, request: TestLaunch) -> str: ...

    def delete_job(self, job_name: str, job_id: str) -> None: ...


class FakeKubernetesJobLauncher:
    def __init__(self) -> None:
        self.launches: list[PrepareLaunch] = []
        self.generate_launches: list[GenerateLaunch] = []
        self.test_launches: list[TestLaunch] = []
        self.deleted_jobs: list[tuple[str, str]] = []
        self._condition = threading.Condition()

    def launch_prepare(self, request: PrepareLaunch) -> str:
        with self._condition:
            self.launches.append(request)
            self._condition.notify_all()
        return f"codex-prepare-{request.job_id[-20:]}"

    def launch_generate(self, request: GenerateLaunch) -> str:
        with self._condition:
            self.generate_launches.append(request)
            self._condition.notify_all()
        return f"codex-generate-{request.job_id[-18:]}-{request.attempt}"

    def launch_test(self, request: TestLaunch) -> str:
        with self._condition:
            self.test_launches.append(request)
            self._condition.notify_all()
        return f"test-runner-{request.job_id[-20:]}-{request.attempt}"

    def delete_job(self, job_name: str, job_id: str) -> None:
        with self._condition:
            self.deleted_jobs.append((job_name, job_id))
            self._condition.notify_all()

    def wait_for_launches(self, count: int, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.launches) >= count, timeout
            )

    def wait_for_generate_launches(self, count: int, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.generate_launches) >= count, timeout
            )

    def wait_for_test_launches(self, count: int, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.test_launches) >= count, timeout
            )


class JobDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        launcher: JobLauncher,
        max_workers: int = 2,
    ) -> None:
        self._session_factory = session_factory
        self._launcher = launcher
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="job-dispatch"
        )

    def submit_prepare(self, launch: PrepareLaunch) -> None:
        self._executor.submit(self._run_prepare, launch)

    def submit_generate(self, launch: GenerateLaunch) -> None:
        self._executor.submit(self._run_generate, launch)

    def submit_test(self, launch: TestLaunch) -> None:
        self._executor.submit(self._run_test, launch)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run_prepare(self, launch: PrepareLaunch) -> None:
        try:
            name = self._launcher.launch_prepare(launch)
            with self._session_factory() as session:
                repository = TestJobRepository(session)
                job = repository.get(launch.job_id)
                repository.set_latest_k8s_job(
                    job_id=job.id,
                    expected_version=job.version,
                    k8s_job_name=name,
                )
                session.commit()
        except Exception:
            logger.exception(
                "prepare job dispatch failed", extra={"job_id": launch.job_id}
            )
            self._record_dispatch_error(launch.job_id)

    def _run_generate(self, launch: GenerateLaunch) -> None:
        try:
            name = self._launcher.launch_generate(launch)
            with self._session_factory() as session:
                repository = TestJobRepository(session)
                job = repository.get(launch.job_id)
                repository.set_latest_k8s_job(
                    job_id=job.id,
                    expected_version=job.version,
                    k8s_job_name=name,
                )
                session.commit()
        except Exception:
            logger.exception(
                "generate job dispatch failed", extra={"job_id": launch.job_id}
            )
            self._record_dispatch_error(launch.job_id)

    def _run_test(self, launch: TestLaunch) -> None:
        try:
            name = self._launcher.launch_test(launch)
            with self._session_factory() as session:
                repository = TestJobRepository(session)
                job = repository.get(launch.job_id)
                repository.set_latest_k8s_job(
                    job_id=job.id,
                    expected_version=job.version,
                    k8s_job_name=name,
                )
                session.commit()
        except Exception:
            logger.exception(
                "test job dispatch failed", extra={"job_id": launch.job_id}
            )
            self._record_dispatch_error(launch.job_id)

    def _record_dispatch_error(self, job_id: str) -> None:
        try:
            with self._session_factory() as session:
                repository = TestJobRepository(session)
                job = repository.get(job_id)
                repository.record_event(
                    event_id=f"evt_{uuid.uuid4().hex}",
                    job_id=job.id,
                    expected_version=job.version,
                    attempt=job.prepare_attempt,
                    phase=EventPhase.SYSTEM,
                    to_status=JobStatus.ERROR,
                    occurred_at=job.updated_at,
                    failure_class=FailureClass.INFRASTRUCTURE,
                    failure_message="failed to dispatch prepare workload",
                )
                session.commit()
        except Exception:
            logger.exception(
                "could not persist prepare dispatch failure", extra={"job_id": job_id}
            )
