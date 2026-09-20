from __future__ import annotations

import unittest
from itertools import pairwise

from app.domain.test_jobs import (
    PRIMARY_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidStateTransition,
    JobStatus,
    is_transition_allowed,
    validate_transition,
)


class StateTransitionTests(unittest.TestCase):
    def test_primary_happy_path_is_allowed(self) -> None:
        path = [
            JobStatus.PREPARE_QUEUED,
            JobStatus.PREPARING,
            JobStatus.WAITING_FOR_CODE,
            JobStatus.VERIFY_QUEUED,
            JobStatus.GENERATING_TESTS,
            JobStatus.TEST_QUEUED,
            JobStatus.TESTING,
            JobStatus.PASSED,
        ]
        for from_status, to_status in pairwise(path):
            with self.subTest(from_status=from_status, to_status=to_status):
                validate_transition(from_status, to_status)

    def test_declared_failure_paths_are_allowed(self) -> None:
        cases = {
            (JobStatus.PREPARING, JobStatus.ERROR),
            (JobStatus.GENERATING_TESTS, JobStatus.BLOCKED),
            (JobStatus.GENERATING_TESTS, JobStatus.ERROR),
            (JobStatus.TESTING, JobStatus.FAILED),
            (JobStatus.TESTING, JobStatus.ERROR),
        }
        for from_status, to_status in cases:
            with self.subTest(from_status=from_status, to_status=to_status):
                self.assertTrue(is_transition_allowed(from_status, to_status))

    def test_every_non_terminal_state_can_timeout_or_cancel(self) -> None:
        for status in set(JobStatus) - TERMINAL_STATUSES:
            with self.subTest(status=status):
                self.assertTrue(is_transition_allowed(status, JobStatus.TIMEOUT))
                self.assertTrue(is_transition_allowed(status, JobStatus.CANCELLED))

    def test_any_historical_job_can_be_superseded(self) -> None:
        for status in set(JobStatus) - {JobStatus.SUPERSEDED}:
            with self.subTest(status=status):
                self.assertTrue(is_transition_allowed(status, JobStatus.SUPERSEDED))

    def test_superseded_sha_can_start_a_new_verify_attempt(self) -> None:
        self.assertTrue(
            is_transition_allowed(JobStatus.SUPERSEDED, JobStatus.VERIFY_QUEUED)
        )

    def test_self_transitions_are_rejected(self) -> None:
        for status in JobStatus:
            with self.subTest(status=status), self.assertRaises(InvalidStateTransition):
                validate_transition(status, status)

    def test_undeclared_transitions_are_rejected(self) -> None:
        for from_status, to_status in [
            (JobStatus.PREPARE_QUEUED, JobStatus.PASSED),
            (JobStatus.WAITING_FOR_CODE, JobStatus.TESTING),
            (JobStatus.PASSED, JobStatus.FAILED),
            (JobStatus.ERROR, JobStatus.PREPARING),
        ]:
            with (
                self.subTest(from_status=from_status, to_status=to_status),
                self.assertRaises(InvalidStateTransition),
            ):
                validate_transition(from_status, to_status)

    def test_all_primary_transition_targets_are_declared_statuses(self) -> None:
        for from_status, targets in PRIMARY_TRANSITIONS.items():
            self.assertIsInstance(from_status, JobStatus)
            self.assertTrue(targets)
            self.assertTrue(all(isinstance(target, JobStatus) for target in targets))


if __name__ == "__main__":
    unittest.main()
