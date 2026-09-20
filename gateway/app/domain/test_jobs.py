from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    PREPARE_QUEUED = "PREPARE_QUEUED"
    PREPARING = "PREPARING"
    WAITING_FOR_CODE = "WAITING_FOR_CODE"
    VERIFY_QUEUED = "VERIFY_QUEUED"
    GENERATING_TESTS = "GENERATING_TESTS"
    TEST_QUEUED = "TEST_QUEUED"
    TESTING = "TESTING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


class EventPhase(StrEnum):
    PREPARE = "PREPARE"
    VERIFY = "VERIFY"
    GENERATE = "GENERATE"
    TEST = "TEST"
    SYSTEM = "SYSTEM"


class FailureClass(StrEnum):
    TEST_FAILURE = "TEST_FAILURE"
    CONTRACT = "CONTRACT"
    POLICY = "POLICY"
    AGENT = "AGENT"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class ArtifactType(StrEnum):
    PLAN = "PLAN"
    DRAFT = "DRAFT"
    PATCH = "PATCH"
    CODEX_JSONL = "CODEX_JSONL"
    RAW_LOG = "RAW_LOG"
    JUNIT = "JUNIT"
    COVERAGE = "COVERAGE"
    RESULT = "RESULT"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.PASSED,
        JobStatus.FAILED,
        JobStatus.BLOCKED,
        JobStatus.ERROR,
        JobStatus.TIMEOUT,
        JobStatus.SUPERSEDED,
        JobStatus.CANCELLED,
    }
)

PRIMARY_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PREPARE_QUEUED: frozenset({JobStatus.PREPARING, JobStatus.ERROR}),
    JobStatus.PREPARING: frozenset({JobStatus.WAITING_FOR_CODE, JobStatus.ERROR}),
    JobStatus.WAITING_FOR_CODE: frozenset({JobStatus.VERIFY_QUEUED}),
    JobStatus.VERIFY_QUEUED: frozenset({JobStatus.GENERATING_TESTS, JobStatus.ERROR}),
    JobStatus.GENERATING_TESTS: frozenset(
        {JobStatus.TEST_QUEUED, JobStatus.BLOCKED, JobStatus.ERROR}
    ),
    JobStatus.TEST_QUEUED: frozenset({JobStatus.TESTING, JobStatus.ERROR}),
    JobStatus.TESTING: frozenset({JobStatus.PASSED, JobStatus.FAILED, JobStatus.ERROR}),
    # SUPERSEDED is recorded as a terminal result for the old SHA. A newer,
    # explicitly verified SHA may then start the next attempt for the same plan.
    JobStatus.SUPERSEDED: frozenset({JobStatus.VERIFY_QUEUED}),
}


class InvalidStateTransition(ValueError):
    def __init__(self, from_status: JobStatus, to_status: JobStatus) -> None:
        super().__init__(
            f"job cannot transition from {from_status.value} to {to_status.value}"
        )
        self.from_status = from_status
        self.to_status = to_status


def is_transition_allowed(from_status: JobStatus, to_status: JobStatus) -> bool:
    """Return whether the state machine permits a persisted status change.

    Any non-terminal job can time out or be cancelled. Any historical result can
    be superseded when a newer commit becomes authoritative. Self transitions
    are deliberately rejected; idempotent callbacks are identified by event ID.
    """

    if from_status == to_status:
        return False
    if to_status == JobStatus.SUPERSEDED:
        return from_status != JobStatus.SUPERSEDED
    if not from_status.is_terminal and to_status in {
        JobStatus.TIMEOUT,
        JobStatus.CANCELLED,
    }:
        return True
    return to_status in PRIMARY_TRANSITIONS.get(from_status, frozenset())


def validate_transition(from_status: JobStatus, to_status: JobStatus) -> None:
    if not is_transition_allowed(from_status, to_status):
        raise InvalidStateTransition(from_status, to_status)


@dataclass(frozen=True, slots=True)
class TestJob:
    id: str
    task_id: str
    repository: str
    branch: str
    base_sha: str
    plan_digest: str
    callback_url: str
    status: JobStatus
    prepare_attempt: int
    verify_attempt: int
    version: int
    created_at: datetime
    updated_at: datetime
    code_sha: str | None = None
    tested_sha: str | None = None
    latest_k8s_job_name: str | None = None
    failure_class: FailureClass | None = None
    failure_message: str | None = None
    completed_at: datetime | None = None
