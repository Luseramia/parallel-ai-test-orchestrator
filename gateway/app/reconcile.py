"""One-shot reconciliation sweep, run by the reconciler CronJob.

Usage: ``python -m app.reconcile``. It reads the same environment as the API
process, prints a JSON report on stdout and exits non-zero only when the sweep
itself could not run.
"""

from __future__ import annotations

import json
import logging
import sys

from app.config import Settings
from app.db import create_database_engine, create_session_factory
from app.services.artifact_service import FilesystemArtifactStore
from app.services.launcher_factory import create_launcher
from app.services.notification_service import (
    NotificationDispatcher,
    SignedHttpNotificationSender,
)
from app.services.reconciler import JobReconciler


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    settings = Settings.from_env()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    launcher = create_launcher(settings)
    notification_dispatcher = NotificationDispatcher(
        session_factory,
        SignedHttpNotificationSender(
            settings.completion_webhook_secret or settings.artifact_signing_key
        ),
    )
    try:
        report = JobReconciler(
            session_factory,
            launcher,
            FilesystemArtifactStore(settings.artifact_root),
            notification_dispatcher,
            settings,
        ).run()
        print(json.dumps(report.as_dict(), sort_keys=True))
    finally:
        # close() drains the delivery pool, so retried notifications finish
        # before the Pod exits.
        notification_dispatcher.close()
        close_launcher = getattr(launcher, "close", None)
        if close_launcher is not None:
            close_launcher()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
