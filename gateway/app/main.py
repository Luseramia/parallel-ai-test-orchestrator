from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine, text

from app.api.test_jobs import router
from app.config import Settings
from app.db import create_database_engine, create_session_factory
from app.middleware import RequestSizeLimitMiddleware
from app.services.artifact_service import ArtifactLinkSigner, FilesystemArtifactStore
from app.services.git_verifier import GitHubApiVerifier, GitVerifier
from app.services.job_launcher import JobDispatcher, JobLauncher
from app.services.job_service import default_contract_path
from app.services.launcher_factory import create_launcher
from app.services.notification_service import (
    NotificationDispatcher,
    NotificationSender,
    SignedHttpNotificationSender,
)
from app.services.plan_validation import TestPlanValidator
from app.services.repository_policy import RepositoryPolicyRegistry


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    launcher: JobLauncher | None = None,
    git_verifier: GitVerifier | None = None,
    notification_sender: NotificationSender | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_engine = engine or create_database_engine(resolved_settings.database_url)
    owns_engine = engine is None
    session_factory = create_session_factory(resolved_engine)
    resolved_launcher = launcher or create_launcher(resolved_settings)
    resolved_git_verifier = git_verifier or GitHubApiVerifier(
        resolved_settings.github_token
    )
    dispatcher = JobDispatcher(session_factory, resolved_launcher)
    resolved_notification_sender = notification_sender or SignedHttpNotificationSender(
        resolved_settings.completion_webhook_secret
        or resolved_settings.artifact_signing_key
    )
    notification_dispatcher = NotificationDispatcher(
        session_factory, resolved_notification_sender
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        notification_dispatcher.reconcile_pending()
        yield
        dispatcher.close()
        notification_dispatcher.close()
        close_launcher = getattr(resolved_launcher, "close", None)
        if close_launcher is not None:
            close_launcher()
        if owns_engine:
            resolved_engine.dispose()

    app = FastAPI(
        title="Parallel AI Test Orchestrator",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.engine = resolved_engine
    app.state.session_factory = session_factory
    app.state.policies = RepositoryPolicyRegistry.load(
        resolved_settings.repository_policy_path
    )
    app.state.plan_validator = TestPlanValidator(default_contract_path())
    app.state.artifact_store = FilesystemArtifactStore(resolved_settings.artifact_root)
    app.state.artifact_signer = ArtifactLinkSigner(
        resolved_settings.artifact_signing_key,
        resolved_settings.artifact_link_ttl_seconds,
    )
    app.state.launcher = resolved_launcher
    app.state.dispatcher = dispatcher
    app.state.git_verifier = resolved_git_verifier
    app.state.notification_dispatcher = notification_dispatcher
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=resolved_settings.max_request_bytes,
        artifact_max_bytes=resolved_settings.max_artifact_bytes,
    )
    app.include_router(router)

    @app.get("/health/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready() -> dict[str, str]:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ready"}

    return app
