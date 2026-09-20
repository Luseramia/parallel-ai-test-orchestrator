from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.domain.test_jobs import NotificationStatus
from app.repositories.notification_repository import NotificationRepository


class NotificationDeliveryError(RuntimeError):
    pass


class NotificationSender(Protocol):
    def send(self, destination: str, payload: dict[str, object]) -> None: ...


class SignedHttpNotificationSender:
    def __init__(self, secret: str, timeout_seconds: float = 10.0) -> None:
        if len(secret) < 16:
            raise ValueError("completion webhook secret must be at least 16 characters")
        self._secret = secret.encode()
        self._timeout_seconds = timeout_seconds

    def send(self, destination: str, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self._secret, timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        try:
            response = httpx.post(
                destination,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-AI-Test-Timestamp": timestamp,
                    "X-AI-Test-Signature": f"sha256={signature}",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise NotificationDeliveryError(
                "completion webhook request failed"
            ) from error
        if response.status_code >= 500 or response.status_code == 429:
            raise NotificationDeliveryError(
                "completion webhook returned retryable status"
            )
        if not response.is_success:
            raise NotificationDeliveryError("completion webhook was rejected")


class FakeNotificationSender:
    def __init__(self, failures_before_success: int = 0) -> None:
        self.failures_before_success = failures_before_success
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._condition = threading.Condition()

    def send(self, destination: str, payload: dict[str, object]) -> None:
        with self._condition:
            self.calls.append((destination, payload))
            self._condition.notify_all()
            if len(self.calls) <= self.failures_before_success:
                raise NotificationDeliveryError("fake transient failure")

    def wait_for_calls(self, count: int, timeout: float = 3.0) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: len(self.calls) >= count, timeout)


class NotificationDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sender: NotificationSender,
        *,
        max_attempts: int = 4,
        initial_delay_seconds: float = 0.05,
    ) -> None:
        self._session_factory = session_factory
        self._sender = sender
        self._max_attempts = max_attempts
        self._initial_delay_seconds = initial_delay_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="notification-dispatch"
        )

    def submit(self, notification_id: str) -> None:
        self._executor.submit(self._deliver, notification_id)

    def reconcile_pending(self) -> int:
        with self._session_factory() as session:
            identifiers = [
                row.id
                for row in NotificationRepository(session).pending(datetime.now(UTC))
            ]
        for notification_id in identifiers:
            self.submit(notification_id)
        return len(identifiers)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _deliver(self, notification_id: str) -> None:
        while True:
            with self._session_factory() as session:
                repository = NotificationRepository(session)
                row = repository.get(notification_id)
                if row.status != NotificationStatus.PENDING.value:
                    return
                try:
                    self._sender.send(row.destination, row.payload)
                except NotificationDeliveryError as error:
                    dead = row.attempts + 1 >= self._max_attempts
                    delay = self._initial_delay_seconds * 2**row.attempts
                    repository.record_failure(
                        row,
                        error=str(error),
                        next_attempt_at=None
                        if dead
                        else datetime.now(UTC) + timedelta(seconds=delay),
                        dead=dead,
                    )
                    session.commit()
                    if dead:
                        return
                else:
                    repository.record_delivered(row)
                    session.commit()
                    return
            time.sleep(delay)
