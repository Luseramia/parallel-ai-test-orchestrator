from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.schemas import (
    CancelResponse,
    EventResponse,
    PrepareRequest,
    PrepareResponse,
    RunnerEvent,
    VerifyRequest,
    VerifyResponse,
)
from app.domain.test_jobs import InvalidStateTransition
from app.repositories.test_job_repository import (
    ConcurrentUpdateError,
    IdempotencyConflictError,
    NotFoundError,
)
from app.security.auth import require_api_auth, require_runner_auth
from app.services.artifact_service import ArtifactError
from app.services.git_verifier import GitVerificationError
from app.services.job_service import JobService
from app.services.plan_validation import PlanValidationError
from app.services.repository_policy import PolicyValidationError

router = APIRouter()


def get_session(request: Request):
    with request.app.state.session_factory() as session:
        yield session


def get_service(request: Request, session: Annotated[Session, Depends(get_session)]):
    return JobService(
        session=session,
        settings=request.app.state.settings,
        policies=request.app.state.policies,
        plan_validator=request.app.state.plan_validator,
        artifact_store=request.app.state.artifact_store,
        artifact_signer=request.app.state.artifact_signer,
        dispatcher=request.app.state.dispatcher,
        git_verifier=request.app.state.git_verifier,
        notification_dispatcher=request.app.state.notification_dispatcher,
        launcher=request.app.state.launcher,
    )


Service = Annotated[JobService, Depends(get_service)]


@router.post(
    "/api/v1/test-jobs/prepare",
    response_model=PrepareResponse,
    status_code=202,
    dependencies=[Depends(require_api_auth)],
)
def prepare_job(
    body: PrepareRequest,
    service: Service,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", max_length=500)],
) -> PrepareResponse:
    try:
        result = service.prepare(body, idempotency_key)
    except (PlanValidationError, PolicyValidationError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IdempotencyConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return PrepareResponse(
        job_id=result.job.id,
        status=result.job.status,
        status_url=f"/api/v1/test-jobs/{result.job.id}",
    )


@router.post(
    "/api/v1/test-jobs/{job_id}/verify",
    response_model=VerifyResponse,
    status_code=202,
    dependencies=[Depends(require_api_auth)],
)
def verify_job(job_id: str, body: VerifyRequest, service: Service) -> VerifyResponse:
    try:
        job, attempt, created = service.verify(job_id, body)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except GitVerificationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (IdempotencyConflictError, InvalidStateTransition) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PlanValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return VerifyResponse(
        job_id=job.id,
        status=job.status,
        attempt=attempt,
        duplicate=not created,
    )


@router.get(
    "/api/v1/test-jobs/{job_id}",
    dependencies=[Depends(require_api_auth)],
)
def get_job_status(job_id: str, service: Service) -> dict[str, object]:
    try:
        return service.status(job_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/api/v1/test-jobs/{job_id}/cancel",
    response_model=CancelResponse,
    dependencies=[Depends(require_api_auth)],
)
def cancel_job(job_id: str, service: Service) -> CancelResponse:
    try:
        job, created = service.cancel(job_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ConcurrentUpdateError, InvalidStateTransition) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return CancelResponse(job_id=job.id, status=job.status, duplicate=not created)


@router.post(
    "/internal/v1/test-jobs/{job_id}/events",
    response_model=EventResponse,
    dependencies=[Depends(require_runner_auth)],
)
def record_event(job_id: str, body: RunnerEvent, service: Service) -> EventResponse:
    try:
        job, created = service.record_runner_event(job_id, body)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (
        ArtifactError,
        ConcurrentUpdateError,
        IdempotencyConflictError,
        InvalidStateTransition,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return EventResponse(
        job_id=job.id,
        status=job.status,
        version=job.version,
        duplicate=not created,
    )


@router.put(
    "/internal/v1/test-jobs/{job_id}/artifacts/{object_key:path}",
    dependencies=[Depends(require_runner_auth)],
)
async def upload_runner_artifact(
    job_id: str, object_key: str, request: Request
) -> dict[str, object]:
    expected_prefix = f"jobs/{job_id}/"
    if not object_key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="artifact key does not belong to job")
    try:
        stored = request.app.state.artifact_store.put_bytes(
            object_key, await request.body()
        )
    except ArtifactError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "object_key": stored.object_key,
        "sha256": stored.sha256,
        "size_bytes": stored.size_bytes,
    }


@router.get(
    "/internal/v1/test-jobs/{job_id}/artifacts/{object_key:path}",
    dependencies=[Depends(require_runner_auth)],
)
def download_runner_artifact(
    job_id: str, object_key: str, request: Request
) -> Response:
    expected_prefix = f"jobs/{job_id}/"
    if not object_key.startswith(expected_prefix):
        raise HTTPException(status_code=403, detail="artifact key does not belong to job")
    try:
        content = request.app.state.artifact_store.read_bytes(object_key)
    except ArtifactError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(
        content,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/v1/test-jobs/{job_id}/artifacts/{artifact_id}")
def download_artifact(
    job_id: str,
    artifact_id: str,
    service: Service,
    expires: Annotated[int, Query(ge=1)],
    signature: Annotated[str, Query(min_length=64, max_length=64)],
) -> Response:
    try:
        content, media_type = service.download_artifact(
            job_id, artifact_id, expires, signature
        )
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ArtifactError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return Response(
        content,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
